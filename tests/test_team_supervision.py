"""Supervision regressions using fake models and disposable task data only."""

from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest

from test_simple_team import SimpleTeamTests, turn

from cater.team import HOST
from cater.tool_protocol import anthropic_history, openai_history, prepare_tool_turn
from cater.work import WorkConflict
from cater.workspaces import WorkspaceService


class TeamSupervisionTests(unittest.TestCase):
    events = SimpleTeamTests.events

    def setUp(self):
        SimpleTeamTests.setUp(self)
        self._turn_number = 0
        self.protocol_errors = []
        self.native_exchanges = {}
        self.validated_continuations = 0
        original = self.driver.tool_turn

        def checked_turn(*, role, prompt, history, tools):
            try:
                system, transcript, _, _ = prepare_tool_turn(
                    role=role, prompt=prompt, history=history, tools=tools)
                anthropic_history(prompt, transcript)
                openai_history(system, prompt, transcript)
                context = json.loads(prompt.rsplit("\n", 1)[1])
                key = (context["you"]["id"], context["message"]["seq"])
                if key in self.native_exchanges:
                    previous_history, previous_turn = self.native_exchanges[key]
                    self.assertEqual(history[:len(previous_history)], previous_history,
                                     "A continuing native exchange lost or rewrote its earlier history")
                    self.assertTrue(previous_turn.message in history,
                                    "A continuing exchange lost its complete native assistant message")
                    for call in previous_turn.calls:
                        replies = [message for message in history
                                   if message.get("role") == "tool" and message.get("tool_call_id") == call.id]
                        self.assertEqual(len(replies), 1, "A native tool call lost its unique settled result")
                    self.validated_continuations += 1
                result = original(role=role, prompt=prompt, history=history, tools=tools)
                self.native_exchanges[key] = (copy.deepcopy(history), result)
                return result
            except Exception as exc:
                self.protocol_errors.append(f"{type(exc).__name__}: {exc}")
                raise

        self.driver.tool_turn = checked_turn

    def native_turn(self, text="", *calls):
        self._turn_number += 1
        result = turn(text, *calls)
        native_calls = [replace(call, id=f"supervision-{self._turn_number}-{i}")
                        for i, call in enumerate(result.calls)]
        message = copy.deepcopy(result.message)
        message["tool_calls"] = [{"id": call.id, "name": call.name, "arguments": call.arguments}
                                 for call in native_calls]
        message["provider_content"] = [
            {"type": "thinking", "thinking": "private-native-state",
             "signature": f"private-signature-{self._turn_number}"},
            *([{"type": "text", "text": text}] if text else []),
            *[{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
              for call in native_calls],
        ]
        return replace(result, calls=native_calls, message=message)

    def run_team(self):
        self.rt.collaborate(self.task)
        self.assertEqual(self.protocol_errors, [], "The fake model received invalid native context")
        self.assertFalse([row for row in self.rt.team.view(self.task)["runs"]
                          if row["status"] == "failed"], "A model/tool protocol failure hid the behavior under test")
        self.assertTrue(self.rt.verify_chain().ok)

    def bind_workspace(self):
        root = self.root / "project"
        root.mkdir()
        config = self.root / "supervision-workspaces.json"
        config.write_text(json.dumps({"workspaces": [{
            "id": "supervision", "title": "Supervision fixture", "root": str(root), "mode": "edit",
        }]}), encoding="utf-8")
        self.rt.workspaces = WorkspaceService(self.rt, config)
        binding = self.rt.workspaces.bind(self.task, "supervision", expected_revision=None)
        return Path(binding["root"])

    def orphan_write(self, root):
        content = "effect already applied\n"
        (root / "effect.txt").write_text(content, encoding="utf-8", newline="\n")
        message = self.rt.team.receive(self.task, "Original interrupted request", request_id="orphan-supervision")
        self.rt.log.emit(type="team.run_started", actor=HOST, task_id=self.task, payload={
            "message_seq": message.seq, "role": self.manager, "run_id": "orphan-supervision-run",
            "control_seq": self.rt.execution_control.view(self.task)["seq"],
        })
        started = self.rt.log.emit(type="team.tool_started", actor=HOST, task_id=self.task, payload={
            "message_seq": message.seq, "role": self.manager, "run_id": "orphan-supervision-run",
            "name": "workspace_write", "call_id": "orphan-write", "root": str(root),
            "arguments": {"path": "effect.txt", "content": content, "expected_sha256": None},
        })
        return started, content

    def results(self, name):
        return [event.payload["result"] for event in self.events("team.tool_finished")
                if event.payload["name"] == name]

    def assert_one_run_per_role(self):
        active = {}
        for event in self.rt.log.read(task_id=self.task):
            if event.actor != HOST:
                continue
            if event.type == "team.run_started":
                role = event.payload["role"]
                self.assertNotIn(role, active, f"Concurrent runs for {role} at event {event.seq}")
                active[role] = event.payload["message_seq"]
            elif event.type == "team.run_finished":
                role = event.payload["role"]
                self.assertEqual(active.pop(role, None), event.payload["message_seq"])
        self.assertEqual(active, {})

    def test_orphan_team_tool_is_visible_and_reconcilable_without_replay(self):
        root = self.bind_workspace()
        started, content = self.orphan_write(root)
        self.assertIn(started.seq, [event.seq for event in self.rt.workspaces.pending(task_id=self.task)])
        self.assertIn(started.seq, [row["seq"] for row in self.rt.workspaces.view(self.task)["pending"]])
        with self.assertRaises(ValueError):
            self.rt.workspaces.reconcile(self.task, started.seq, reason="Inspect original effect")
        with self.rt.workspaces._owner(str(root)) as owned:
            self.assertTrue(owned)
            with self.assertRaises(WorkConflict):
                self.rt.workspaces.reconcile(self.task, started.seq, reason="Original owner still active",
                                             invocation_stopped=True)
        receipt = self.rt.workspaces.reconcile(
            self.task, started.seq, reason="Original process stopped; effect.txt matches the original write",
            invocation_stopped=True)
        self.assertEqual(receipt.payload["started_seq"], started.seq)
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), [])
        self.assertEqual(self.rt.workspaces.view(self.task)["pending"], [])
        self.driver.steps[self.manager] = [
            self.native_turn("", ("workspace_read", {"path": "effect.txt"})),
            self.native_turn("Inspected the preserved effect."),
        ]
        self.rt.team.receive(self.task, "Inspect after reconciliation", request_id="inspect-after-reconcile")
        self.run_team()
        self.rt.team.run(self.task)
        self.assertEqual((root / "effect.txt").read_text(encoding="utf-8"), content)
        self.assertEqual(self.results("workspace_read")[-1]["status"], "ok")
        self.assertEqual(len([event for event in self.events("team.tool_started")
                              if event.payload["name"] == "workspace_write"]), 1)

    def test_unknown_effect_allows_read_only_inspection_but_blocks_mutations(self):
        root = self.bind_workspace()
        started, content = self.orphan_write(root)
        self.driver.steps[self.manager] = [self.native_turn("",
            ("workspace_read", {"path": "effect.txt"}),
            ("workspace_write", {"path": "effect.txt", "content": "replayed effect",
                                 "expected_sha256": hashlib.sha256(content.encode()).hexdigest()}),
            ("workspace_write", {"path": "new-effect.txt", "content": "new mutation", "expected_sha256": None})),
            self.native_turn("Inspection complete; unknown effects require reconciliation.")]
        self.rt.team.receive(self.task, "Inspect unknown effects without repeating them", request_id="inspect-unknown-effect")
        self.run_team()
        with self.subTest("read remains available"):
            read, = self.results("workspace_read")
            self.assertEqual(read["status"], "ok")
            self.assertEqual(read["content"], content)
        with self.subTest("writes remain blocked"):
            self.assertTrue(all(result["status"] == "error" for result in self.results("workspace_write")))
            self.assertEqual(len(self.results("workspace_write")), 2)
            self.assertEqual((root / "effect.txt").read_text(encoding="utf-8"), content)
            self.assertFalse((root / "new-effect.txt").exists())
        with self.subTest("unknown call stays pending"):
            self.assertIn(started.seq, [event.seq for event in self.rt.workspaces.pending(task_id=self.task)])
        call_count = len(self.driver.calls)
        self.rt.team.run(self.task)
        self.assertEqual(len(self.driver.calls), call_count, "Recovery replayed a model request")

    def test_finish_work_advances_the_objective_revision(self):
        before = self.rt.team.view(self.task)["revision"]
        self.driver.steps[self.manager] = [
            self.native_turn("", ("finish_work", {"summary": "Delivered", "verification": "Fixture inspection only"})),
            self.native_turn("Delivered with the stated verification limits."),
        ]
        self.rt.team.receive(self.task, "Finish this work", request_id="finish-revision-supervision")
        self.run_team()
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertGreater(self.rt.team.view(self.task)["revision"], before)

    def test_finish_work_stops_late_side_effects_after_running_worker_returns(self):
        root = self.bind_workspace()
        (root / "input.txt").write_text("fixture input", encoding="utf-8")

        def return_result(context, history, tools):
            return self.native_turn("", ("send_message", {
                "recipient": self.manager, "text": "Final result returned for your decision",
                "reply_to": context["active_assignment"]["seq"],
            }))

        self.driver.steps = {
            self.manager: [
                self.native_turn("", ("delegate", {"recipient": self.worker, "instruction": "Prepare the result"})),
                self.native_turn("", ("wait_for_reply", {})),
                self.native_turn("", ("finish_work", {"summary": "Current work is complete", "verification": "Fixture only"})),
                self.native_turn("Completion recorded."),
                self.native_turn("Worker interruption acknowledged."),
            ],
            self.worker: [
                return_result,
                self.native_turn("", ("workspace_read", {"path": "input.txt"})),
                self.native_turn("",
                    ("workspace_write", {"path": "obsolete.txt", "content": "obsolete effect", "expected_sha256": None}),
                    ("publish_artifact", {"filename": "obsolete.txt", "content": "obsolete artifact",
                                          "summary": "Obsolete", "format": "text"})),
                self.native_turn("Old work ended."),
            ],
        }
        self.rt.team.receive(self.task, "Start delegated work", request_id="finish-active-worker")
        self.run_team()
        completed, = self.events("team.objective_completed")
        worker_started = [event for event in self.events("team.run_started") if event.payload["role"] == self.worker]
        self.assertTrue(worker_started)
        self.assertLess(worker_started[0].seq, completed.seq, "The worker must already be in flight")
        with self.subTest("old workspace write blocked"):
            self.assertFalse((root / "obsolete.txt").exists())
        with self.subTest("old publication blocked"):
            self.assertFalse([artifact for artifact in self.rt.task(self.task).deliverables
                              if artifact.get("filename") == "obsolete.txt"])
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")

    def test_manager_finish_batch_allows_reads_but_no_new_writes(self):
        root = self.bind_workspace()
        (root / "input.txt").write_text("readable after completion", encoding="utf-8")
        self.driver.steps[self.manager] = [self.native_turn("",
            ("finish_work", {"summary": "Complete", "verification": "Fixture inspection"}),
            ("workspace_read", {"path": "input.txt"}),
            ("workspace_write", {"path": "after-finish.txt", "content": "must not be written", "expected_sha256": None}),
            ("publish_artifact", {"filename": "after-finish.txt", "content": "must not be published",
                                  "summary": "Late publication", "format": "text"})),
            self.native_turn("Completion and read-only inspection recorded.")]
        self.rt.team.receive(self.task, "Finish and inspect", request_id="finish-manager-batch")
        self.run_team()
        with self.subTest("read executes"):
            read, = self.results("workspace_read")
            self.assertEqual(read["status"], "ok")
            self.assertEqual(read["content"], "readable after completion")
        with self.subTest("workspace mutation denied"):
            write, = self.results("workspace_write")
            self.assertEqual(write["status"], "error")
            self.assertFalse((root / "after-finish.txt").exists())
        with self.subTest("publication denied"):
            publish, = self.results("publish_artifact")
            self.assertEqual(publish["status"], "error")
            self.assertEqual(self.rt.task(self.task).deliverables, [])

    def test_busy_manager_prioritizes_new_direction_with_complete_native_history(self):
        root = self.bind_workspace()
        (root / "input.txt").write_text("native history input", encoding="utf-8")
        incoming_text = "NEW_DIRECTION_SENTINEL: replace the obsolete implementation"
        first_turn = self.native_turn("Delegating the original request.",
            ("delegate", {"recipient": self.worker, "instruction": "Implement the old direction"}),
            ("workspace_read", {"path": "input.txt"}))
        state = {"manager_turns": 0, "adopted": False}

        def manager(context, history, tools):
            state["manager_turns"] += 1
            if state["manager_turns"] == 1:
                return first_turn
            visible = json.dumps({"context": context, "history": history})
            if incoming_text in visible and not state["adopted"]:
                state["adopted"] = True
                return self.native_turn("Adopting the new direction.", ("set_objective", {"goal": "New implementation direction"}))
            if state["adopted"] or state["manager_turns"] >= 6:
                return self.native_turn("No further old operations.")
            return self.native_turn("", ("workspace_read", {"path": "input.txt"}))

        def worker_started(context, history, tools):
            self.rt.team.receive(self.task, incoming_text, request_id="busy-manager-new-direction")
            return self.native_turn("", ("workspace_read", {"path": "input.txt"}))

        self.driver.steps = {
            self.manager: [manager] * 16,
            self.worker: [worker_started,
                self.native_turn("", ("workspace_write", {"path": "obsolete.txt", "content": "old direction", "expected_sha256": None})),
                self.native_turn("Old worker finished.")],
        }
        self.rt.team.receive(self.task, "Start original direction", request_id="busy-manager-original")
        self.run_team()
        with self.subTest("direction wins before another worker operation"):
            self.assertTrue(state["adopted"])
            self.assertEqual(self.rt.team.view(self.task)["objective"], "New implementation direction")
            self.assertFalse((root / "obsolete.txt").exists())
            self.assertEqual(sum(call["context"]["you"]["id"] == self.worker for call in self.driver.calls), 1)
        with self.subTest("continued native exchanges retain complete provider blocks and results"):
            self.assertGreater(self.validated_continuations, 0,
                               "The scenario must actually exercise a continuing native exchange")
            manager_histories = [call["history"] for call in self.driver.calls
                                 if call["context"]["you"]["id"] == self.manager and call["history"]]
            self.assertTrue(manager_histories)
            self.assertTrue(any(message.get("provider_content") for history in manager_histories
                                for message in history if message["role"] == "assistant"))
        self.assert_one_run_per_role()
        ledger = json.dumps([event.payload for event in self.rt.log.read(task_id=self.task)])
        self.assertNotIn("private-native-state", ledger)
        self.assertNotIn("private-signature-", ledger)

    def test_nested_delegation_wait_eventually_reports_to_original_manager_once(self):
        helper = next(role.id for role in self.rt.worker_roles if role.id != self.worker)
        final_text = "Implementation corrected after the independent review."
        self.driver.steps = {
            self.manager: [
                self.native_turn("", ("delegate", {"recipient": self.worker, "instruction": "Implement and ask for review"})),
                self.native_turn("", ("wait_for_reply", {})),
                self.native_turn("I received the completed implementation and review."),
                self.native_turn("No duplicate report is needed."),
            ],
            self.worker: [
                self.native_turn("", ("delegate", {"recipient": helper, "instruction": "Review this implementation"})),
                self.native_turn("", ("wait_for_reply", {})),
                self.native_turn(final_text),
            ],
            helper: [self.native_turn("Review complete: the correction is sufficient.")],
        }
        self.rt.team.receive(self.task, "Implement and review", request_id="nested-supervision")
        self.run_team()
        assignment, = [event for event in self.events("team.message_received")
                       if event.payload["kind"] == "assignment" and event.actor.id == self.manager]
        reports = [event for event in self.events("team.message_received")
                   if event.payload["kind"] == "report" and event.actor.id == self.worker
                   and event.payload["recipient"] == self.manager
                   and event.payload.get("reply_to") == assignment.seq]
        self.assertEqual(len(reports), 1, "The original delegation must receive exactly one final automatic report")
        self.assertIn(final_text, reports[0].payload["text"])
        self.assertTrue(any(call["context"]["you"]["id"] == self.manager
                            and call["context"]["message"]["seq"] == reports[0].seq for call in self.driver.calls))
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 0)
        self.assert_one_run_per_role()

    def failed_receipt(self, task_id, marker):
        role = self.rt._role_by_id(self.worker)
        message = self.rt.log.emit(type="team.message_received", actor=self.rt._leader_actor(), task_id=task_id,
            payload={"recipient": self.worker, "text": "Run a fixture check", "kind": "assignment",
                     "revision": self.rt.team.view(task_id)["revision"]})
        run_id = f"receipt-run-{message.seq}"
        self.rt.log.emit(type="team.run_started", actor=HOST, task_id=task_id, payload={
            "message_seq": message.seq, "role": role.id, "run_id": run_id,
            "control_seq": self.rt.execution_control.view(task_id)["seq"],
        })
        started = self.rt.log.emit(type="team.tool_started", actor=HOST, task_id=task_id, payload={
            "message_seq": message.seq, "role": role.id, "run_id": run_id,
            "name": "workspace_run", "call_id": f"call-{marker}", "root": None,
            "arguments": {"command": "fixture-check", "args": []},
        })
        result = {"status": "error", "complete": True, "outcome": "failed", "exit_code": 7,
                  "stdout": marker + "\n" + "full-output-line\n" * 400 + marker + "-END",
                  "stderr": marker + "-STDERR", "error": {"code": "failed", "message": "Fixture check failed"}}
        finished = self.rt.log.emit(type="team.tool_finished", actor=HOST, task_id=task_id, payload={
            "started_seq": started.seq, "message_seq": message.seq, "role": role.id, "run_id": run_id,
            "name": "workspace_run", "call_id": f"call-{marker}", "result": result,
        })
        self.rt.log.emit(type="team.run_finished", actor=HOST, task_id=task_id, payload={
            "message_seq": message.seq, "role": role.id, "run_id": run_id, "status": "completed", "reason": "",
        })
        return started, finished, result

    @staticmethod
    def contains_result(value, expected):
        if isinstance(value, dict):
            return value == expected or any(TeamSupervisionTests.contains_result(item, expected)
                                            for item in value.values())
        if isinstance(value, list):
            return any(TeamSupervisionTests.contains_result(item, expected) for item in value)
        return False

    def test_read_receipts_gives_manager_the_complete_local_failure(self):
        started, finished, failure = self.failed_receipt(self.task, "LOCAL_FAILURE_SENTINEL")
        self.driver.steps[self.manager] = [
            self.native_turn("", ("read_receipts", {})),
            self.native_turn("The recorded check failed; a correction is needed."),
        ]
        self.rt.team.receive(self.task, "Inspect actual execution evidence", request_id="read-local-receipts")
        self.run_team()
        result, = self.results("read_receipts")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(self.contains_result(result, failure), "The manager needs the full result, including stderr and output tail")
        serialized = json.dumps(result)
        self.assertIn(finished.payload["run_id"], serialized)
        self.assertIn(str(started.seq), serialized)
        self.assertIn("workspace_run", serialized)
        calls = [call for call in self.driver.calls if call["context"]["you"]["id"] == self.manager]
        self.assertIn("read_receipts", {tool["name"] for tool in calls[0]["tools"]})
        tool_reply, = [message for message in calls[-1]["history"] if message.get("role") == "tool"]
        self.assertTrue(self.contains_result(json.loads(tool_reply["content"]), failure))

    def test_read_receipts_cannot_disclose_another_tasks_execution(self):
        self.failed_receipt(self.task, "LOCAL_VISIBLE_RECEIPT")
        other = self.rt.create_task("Separate private task")
        self.rt.team.enable(other)
        foreign_start, foreign_finish, _ = self.failed_receipt(other, "FOREIGN_PRIVATE_SENTINEL")
        self.driver.steps[self.manager] = [self.native_turn("",
            ("read_receipts", {}),
            ("read_receipts", {"task_id": other}),
            ("read_receipts", {"run_id": foreign_finish.payload["run_id"]}),
            ("read_receipts", {"started_seq": foreign_start.seq})),
            self.native_turn("Only this task's evidence is available.")]
        self.rt.team.receive(self.task, "Inspect only this group's receipts", request_id="read-scoped-receipts")
        self.run_team()
        results = self.results("read_receipts")
        self.assertEqual(len(results), 4, "Every query must receive a tool result without leaking another task")
        self.assertEqual(results[0]["status"], "ok")
        self.assertIn("LOCAL_VISIBLE_RECEIPT", json.dumps(results[0]))
        for index, result in enumerate(results):
            with self.subTest(query=index):
                self.assertNotIn("FOREIGN_PRIVATE_SENTINEL", json.dumps(result))
        local_calls = [call for call in self.driver.calls if call["context"]["you"]["id"] == self.manager]
        self.assertTrue("FOREIGN_PRIVATE_SENTINEL" not in json.dumps(local_calls),
                        "Another task's receipt appeared in the manager's context or tool history")


def load_tests(loader, tests, pattern):
    # Import the shared fixture without discovering its original nine tests twice.
    return loader.loadTestsFromTestCase(TeamSupervisionTests)


if __name__ == "__main__":
    unittest.main()
