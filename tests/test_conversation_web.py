from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cater.conversation import ConversationService, SYSTEM
from cater.events import Actor
from cater.runtime import Runtime
from cater.web import ActivityRunner
from test_runtime import start_test_web


class NoModelCalls:
    name = "semantic"

    def complete(self, **kwargs):
        raise AssertionError("this test must not call a model")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ConversationWebTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "conversation-web.db"
        self.roles = Path(tmp.name) / "roles.json"
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "semantic"
        self.roles.write_text(json.dumps(config), encoding="utf-8")
        self.driver = NoModelCalls()
        self.entered, self.release = threading.Event(), threading.Event()
        self.resolved = []
        self.runners = []
        self.next_action = "none"
        with self.factory() as rt:
            self.task = rt.create_task("Original goal", ["Original acceptance"])
            self.scope = rt.work.scope(self.task)

        original_init = ActivityRunner.__init__

        def runner_init(runner, *args, **kwargs):
            original_init(runner, *args, **kwargs)
            self.runners.append(runner)

        def resolve(service, task_id):
            pending = service.pending(task_id)
            if not pending:
                return None
            seq = pending[0]
            started = service.rt.log.emit(type="conversation.started", task_id=task_id, actor=SYSTEM,
                                          payload={"conversation_seq": seq, "lease_until": time.time() + 60})
            self.resolved.append((seq, threading.get_ident()))
            self.entered.set()
            if not self.release.wait(5):
                raise AssertionError("test did not release the simulated resolver")
            scope_before = service.rt.work.scope(task_id)
            command = None
            if self.next_action == "run":
                command = service.rt.log.emit(type="human.commanded", task_id=task_id, actor=Actor.human(),
                    payload={"conversation_seq": seq, "instruction": "Continue", "intent": "resume", "next_action": "run"})
            finished = service.rt.log.emit(type="conversation.finished", task_id=task_id, actor=SYSTEM,
                payload={"conversation_seq": seq, "attempt_seq": started.seq, "status": "finished",
                         "reply": "Simulated routing result", "scope_before": scope_before,
                         "action": "resume_task" if command else "discuss", "next_action": self.next_action,
                         **({"command_seq": command.seq} if command else {})})
            return {**finished.payload, "finish_seq": finished.seq}

        self.enterContext(patch.object(ActivityRunner, "__init__", runner_init))
        self.enterContext(patch.object(ConversationService, "resolve_next", resolve))
        self.collaborate = self.enterContext(patch.object(Runtime, "collaborate", side_effect=AssertionError("direct run")))
        self.battle = self.enterContext(patch.object(Runtime, "battle", side_effect=AssertionError("direct battle")))
        self.schedule = self.enterContext(patch.object(ActivityRunner, "continue_after_current"))
        self.start = self.enterContext(patch.object(ActivityRunner, "_start"))
        self.explain = self.enterContext(patch.object(ActivityRunner, "answer_questions"))
        self.base = start_test_web(self, str(self.db), runtime_factory=self.factory)
        self.addCleanup(self.finish_queue)

    def factory(self):
        return Runtime(self.db, roles_path=self.roles, drivers={"semantic": self.driver}, close_drivers_on_close=False)

    def finish_queue(self):
        self.release.set()
        deadline = time.monotonic() + 5
        while any(runner._conversation_tasks for runner in self.runners) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(all(not runner._conversation_tasks for runner in self.runners), "conversation queue did not stop")

    def post(self, data, *, form=False, expected_status=202):
        path = f"/tasks/{self.task}/command" if form else f"/api/tasks/{self.task}/command"
        body = urlencode(data).encode() if form else json.dumps(data).encode()
        request = Request(self.base + path, data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded" if form else "application/json"})
        try:
            response = build_opener(NoRedirect()).open(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            content = response.read()
            self.assertEqual(response.status, 303 if form else expected_status, content.decode())

    def assert_received_without_execution(self):
        self.assertTrue(self.entered.wait(2))
        self.assertFalse(self.release.is_set())
        self.collaborate.assert_not_called()
        self.battle.assert_not_called()
        self.schedule.assert_not_called()
        self.start.assert_not_called()
        with self.factory() as rt:
            self.assertEqual(rt.work.scope(self.task), self.scope)
            self.assertEqual(rt.task(self.task).goal, "Original goal")
            self.assertEqual(rt.task(self.task).acceptance, ["Original acceptance"])
            self.assertEqual(rt.task(self.task).commands, [])
            rows = rt.conversation.view(self.task)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "answering")
            self.assertTrue(rows[0]["continue_work"])

    def test_json_receives_and_queues_without_running_on_request_thread(self):
        self.post({"instruction": "Can you explain the current result?", "continue": True})
        self.assert_received_without_execution()
        self.finish_queue()
        self.assertEqual(len(self.resolved), 1)
        self.schedule.assert_not_called()

    def test_form_receives_and_queues_without_running_on_request_thread(self):
        self.post({"instruction": "Can you explain the current result?", "run_now": "on"}, form=True)
        self.assert_received_without_execution()
        self.finish_queue()
        self.assertEqual(len(self.resolved), 1)
        self.schedule.assert_not_called()

    def test_repeated_queue_wakeups_do_not_duplicate_or_overlap_resolution(self):
        self.post({"instruction": "First message"})
        self.assertTrue(self.entered.wait(2))
        for _ in range(3):
            self.runners[0].answer_conversations(self.task)
        self.post({"instruction": "Second message"})
        self.assertEqual(len(self.resolved), 1)
        self.finish_queue()
        with self.factory() as rt:
            received = [row["seq"] for row in rt.conversation.view(self.task)]
        self.assertEqual([seq for seq, _ in self.resolved], received)
        self.assertEqual(len({thread_id for _, thread_id in self.resolved}), 1)

    def test_json_request_id_retry_does_not_create_second_message(self):
        data = {"instruction": "Explain current progress", "request_id": "json-retry", "continue": True}
        self.post(data)
        self.assertTrue(self.entered.wait(2))
        self.post(data)
        self.finish_queue()
        with self.factory() as rt:
            self.assertEqual(len(rt.conversation.view(self.task)), 1)
        self.assertEqual(len(self.resolved), 1)

    def test_form_request_id_retry_does_not_create_second_message(self):
        data = {"instruction": "Explain current progress", "request_id": "form-retry", "run_now": "on"}
        self.post(data, form=True)
        self.assertTrue(self.entered.wait(2))
        self.post(data, form=True)
        self.finish_queue()
        with self.factory() as rt:
            self.assertEqual(len(rt.conversation.view(self.task)), 1)
        self.assertEqual(len(self.resolved), 1)

    def test_execution_is_scheduled_once_only_after_semantic_result(self):
        self.next_action = "run"
        self.post({"instruction": "Please continue the current work", "continue": True})
        self.assert_received_without_execution()
        self.finish_queue()
        self.schedule.assert_not_called()
        self.start.assert_called_once()
        with self.factory() as rt:
            queued = list(rt.log.iter_events(task_id=self.task, event_types=("activity.queued",)))
            finished = list(rt.log.iter_events(task_id=self.task, event_types=("conversation.finished",)))[-1]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].payload["conversation_seq"], self.resolved[0][0])
        self.assertEqual(queued[0].payload["expected_scope"], self.scope)
        self.assertEqual(queued[0].payload["finished_seq"], finished.seq)
        self.runners[0].dispatch_conversation(self.task, {**finished.payload, "finish_seq": finished.seq})
        self.start.assert_called_once()
        self.collaborate.assert_not_called()
        self.battle.assert_not_called()

    def test_explanation_is_queued_only_after_semantic_result(self):
        self.next_action = "explain"
        self.post({"instruction": "Explain what was delivered"})
        self.assertTrue(self.entered.wait(2))
        self.explain.assert_not_called()
        self.finish_queue()
        self.explain.assert_called_once_with(self.task)
        self.schedule.assert_not_called()

    def test_structured_json_goal_uses_explicit_command_without_semantic_call(self):
        self.post({"instruction": "Apply these explicit fields", "goal": "New structured goal"}, expected_status=200)
        with self.factory() as rt:
            self.assertEqual(rt.task(self.task).goal, "New structured goal")
            self.assertEqual(rt.task(self.task).commands[-1]["intent"], "direction")
            self.assertEqual(rt.conversation.view(self.task), [])
        self.assertEqual(self.resolved, [])
        self.collaborate.assert_not_called()

    def test_structured_json_empty_acceptance_uses_explicit_command(self):
        self.post({"instruction": "Clear explicit acceptance", "acceptance": []}, expected_status=200)
        with self.factory() as rt:
            self.assertEqual(rt.task(self.task).goal, "Original goal")
            self.assertEqual(rt.task(self.task).acceptance, [])
            self.assertEqual(rt.task(self.task).commands[-1]["intent"], "direction")
            self.assertEqual(rt.conversation.view(self.task), [])
        self.assertEqual(self.resolved, [])

    def test_structured_form_fields_use_explicit_command_without_semantic_call(self):
        self.post({"instruction": "Apply explicit fields", "goal": "Form goal", "acceptance": "Form acceptance"}, form=True)
        with self.factory() as rt:
            self.assertEqual(rt.task(self.task).goal, "Form goal")
            self.assertEqual(rt.task(self.task).acceptance, ["Form acceptance"])
            self.assertEqual(rt.task(self.task).commands[-1]["intent"], "direction")
            self.assertEqual(rt.conversation.view(self.task), [])
        self.assertEqual(self.resolved, [])


if __name__ == "__main__":
    unittest.main()
