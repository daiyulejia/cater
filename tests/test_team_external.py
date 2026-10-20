"""Offline integration of the production external team execution branch."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cater.acp import AcpPromptResult
from cater.events import Actor, EventLog
from cater.external_agent import ExternalAgentState, ExternalYield
from cater.runtime import Runtime
from cater.team import HOST
from cater.tool_protocol import ToolCall
from cater.work import WorkConflict


class ScriptedExternalDriver:
    name = "external_fixture"
    execution_profile = "qoder-managed-v1"
    command = ["offline-fixture-never-launched"]
    model = "offline"

    def __init__(self):
        self.scripts = {}
        self.runs = []
        self.errors = []
        self.factory_observer = None

    def agent_run(self, **kwargs):
        if self.factory_observer:
            self.factory_observer()
        run = ScriptedExternalRun(self, **kwargs)
        self.runs.append(run)
        return run

    def tool_turn(self, **kwargs):
        raise AssertionError("External roles must never use native tool_turn")


class ScriptedExternalRun:
    def __init__(self, driver, *, cwd, tools, execute, check_current, on_update):
        self.driver, self.cwd, self.tools = driver, cwd, tools
        self.execute, self.check_current = execute, check_current
        self.state = ExternalAgentState()
        self.context = None
        self.receipts = []
        self.revoked = False
        self.cleanup_complete = True

    def revoke_tools(self):
        self.revoked = True

    def call(self, name, **arguments):
        if self.revoked:
            raise AssertionError("Script attempted a tool after revocation")
        call = ToolCall(f"external-{len(self.driver.runs)}-{len(self.receipts)}", name, arguments)
        result = self.execute(call)
        self.receipts.append((call, result))
        self.check_current()
        return result

    def run(self, prompt):
        self.context = json.loads(prompt.rsplit("\n", 1)[-1])
        self.state = replace(self.state, phase="prompting", prompt_attempted=True,
                             session_id=f"offline-{len(self.driver.runs)}")
        try:
            scripts = self.driver.scripts.get(self.context["you"]["id"], [])
            if not scripts:
                raise AssertionError(f"Unexpected external input: {self.context['message']}")
            text = scripts.pop(0)(self) or ""
            self.state = replace(self.state, phase="finished", text=text, stop_reason="end_turn")
            return AcpPromptResult(text, "end_turn")
        except ExternalYield:
            self.state = replace(self.state, phase="yielded")
            raise
        except BaseException as exc:
            if isinstance(exc, AssertionError):
                self.driver.errors.append(exc)
            self.state = replace(self.state, phase="failed")
            raise
        finally:
            self.state = replace(self.state, cleanup_complete=self.cleanup_complete)


class TeamExternalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = json.loads((Path(__file__).resolve().parents[1]
                             / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "external_fixture"
        for role in config["roles"]:
            role["driver"] = "external_fixture"
        self.roles = self.root / "roles.json"
        self.roles.write_text(json.dumps(config), encoding="utf-8")
        self.driver = ScriptedExternalDriver()
        self.rt = Runtime(self.root / "team.db", roles_path=self.roles,
                          drivers={self.driver.name: self.driver})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Offline external team regression")
        self.rt.team.enable(self.task)
        self.manager = self.rt.organization.leader.id
        self.worker, self.reviewer = [r.id for r in self.rt.worker_roles[:2]]

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def results(self, name):
        return [e.payload["result"] for e in self.events("team.tool_finished")
                if e.payload["name"] == name]

    def receive(self, text="Start", *, role=None):
        prefix = f"@{self.rt.team._role(role).name} " if role else ""
        return self.rt.team.receive(self.task, prefix + text)

    def drain(self):
        self.rt.collaborate(self.task)
        self.assertEqual(self.driver.errors, [])
        self.assertTrue(all(not scripts for scripts in self.driver.scripts.values()),
                        "Every scripted input must be consumed")
        self.assertTrue(self.rt.verify_chain().ok)
        self.assert_receipt_chain()

    def assert_receipt_chain(self):
        events = self.rt.log.read(task_id=self.task)
        by_seq = {e.seq: e for e in events}
        starts = {e.payload["run_id"]: e for e in events if e.type == "team.run_started"}
        for e in events:
            if e.type in {"team.run_started", "team.run_finished", "team.external_run_started",
                          "team.external_run_finished", "team.tool_started", "team.tool_finished",
                          "team.review_requested", "team.review_recorded", "team.objective_completed"}:
                self.assertEqual(e.actor, HOST, e)
            if e.type == "team.external_run_started":
                source = starts[e.payload["run_id"]]
                self.assertLess(source.seq, e.seq)
                self.assertEqual(e.payload["run_started_hash"], source.hash)
                for key in ("role", "run_id", "message_seq"):
                    self.assertEqual(e.payload[key], source.payload[key])
            if e.type == "team.tool_started":
                external = by_seq[e.payload["external_started_seq"]]
                self.assertEqual(external.type, "team.external_run_started")
                self.assertLess(external.seq, e.seq)
                for key in ("role", "run_id", "message_seq"):
                    self.assertEqual(e.payload[key], external.payload[key])
            if e.type == "team.tool_finished":
                source = by_seq[e.payload["started_seq"]]
                self.assertEqual(source.type, "team.tool_started")
                self.assertLess(source.seq, e.seq)
                for key in ("role", "run_id", "name"):
                    self.assertEqual(e.payload[key], source.payload[key])
                actual = [(call, result) for run in self.driver.runs for call, result in run.receipts
                          if call.id == source.payload["call_id"]]
                self.assertEqual(len(actual), 1)
                call, result = actual[0]
                self.assertEqual(call.name, source.payload["name"])
                self.assertEqual({k: v for k, v in call.arguments.items() if k != "content"},
                                 source.payload["arguments"])
                self.assertEqual(result, e.payload["result"])
            if e.type == "team.external_run_finished":
                source = by_seq[e.payload["started_seq"]]
                self.assertEqual(e.payload["started_hash"], source.hash)

    def publish(self, run, content="version one\n"):
        result = run.call("publish_artifact", filename="result.txt", content=content,
                          summary="Offline fixture", format="text")
        self.assertEqual(result["status"], "ok", result)
        return result["artifact_seq"]

    def delegate_wait(self, run):
        result = run.call("delegate", recipient=self.worker,
                          instruction="Publish the requested file and formally return this assignment.")
        self.assertEqual(result["status"], "ok", result)
        run.call("wait_for_reply")
        self.fail("Successful wait must yield before further work")

    def return_assignment(self, run):
        active = run.context["active_assignment"]
        self.assertEqual(active["sender"], self.manager)
        run.call("send_message", recipient=self.manager, reply_to=active["seq"],
                 text="The exact published version is ready.")
        self.fail("Formal return must yield before further work")

    def deliver(self, run):
        run.call("finish_work", summary="Offline fixture delivered",
                 verification="Scripted file inspection; no real model")
        self.fail("Successful delivery must yield before further work")

    def test_delegate_wait_publish_return_and_delivery(self):
        def worker(run):
            self.publish(run)
            self.return_assignment(run)
        self.driver.scripts = {self.manager: [self.delegate_wait, self.deliver], self.worker: [worker]}
        self.receive()
        self.drain()
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertEqual([e.payload["outcome"] for e in self.events("team.external_run_finished")],
                         ["yielded"] * 3)
        self.assertTrue(all(r.revoked and r.state.cleanup_complete for r in self.driver.runs))
        assignment, = [e for e in self.events("team.message_received") if e.payload["kind"] == "assignment"]
        reports = [e for e in self.events("team.message_received") if e.payload["kind"] == "report"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].payload["reply_to"], assignment.seq)
        self.assertEqual(reports[0].actor, Actor.agent(self.worker, self.rt.team._role(self.worker).role))
        self.assertEqual(self.rt.team.external_runs.pending(self.task), [])
        self.assertEqual([r["status"] for r in self.results("finish_work")], ["ok"])

    def test_two_review_rejections_third_version_approval_then_formal_return(self):
        versions = ["negative missing\n", "zero missing\n", "both supported\n"]
        original, author_feedback = [], []

        def author(run):
            context = run.context
            active = context["active_assignment"]
            self.assertIsNotNone(active)
            if not original:
                original.append(active["seq"])
            self.assertEqual(active["seq"], original[0])
            reviews = context["reviews"]
            if reviews:
                author_feedback.append(reviews[0]["status"])
            if reviews and reviews[0]["status"] == "approved":
                self.return_assignment(run)
            version = len(self.results("publish_artifact"))
            seq = self.publish(run, versions[version])
            args = {"reviewer": self.reviewer, "artifact_seqs": [seq],
                    "instruction": "Read exact bytes and check negative and zero cases."}
            if reviews:
                args["review_seq"] = reviews[0]["request_seq"]
            run.call("request_review", **args)
            self.fail("Review request must yield")

        def reviewer(run):
            row, = run.context["reviews"]
            artifact = run.call("read_artifact", artifact_seq=row["artifacts"][0]["seq"])
            index = versions.index(artifact["content"])
            self.assertEqual(artifact["content_sha256"], hashlib.sha256(versions[index].encode()).hexdigest())
            run.call("record_review", review_seq=row["request_seq"],
                     verdict="approved" if index == 2 else "changes_requested",
                     feedback=f"Inspected version {index + 1}")
            self.fail("Review decision must yield")

        self.driver.scripts = {self.manager: [self.delegate_wait, self.deliver],
                               self.worker: [author] * 4, self.reviewer: [reviewer] * 3}
        self.receive()
        self.drain()
        decisions = self.events("team.review_recorded")
        self.assertEqual([e.payload["verdict"] for e in decisions],
                         ["changes_requested", "changes_requested", "approved"])
        self.assertEqual(author_feedback, ["changes_requested", "changes_requested", "approved"])
        requests = self.events("team.review_requested")
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(r["root_seq"] == requests[0].seq for r in self.results("request_review")))
        returns = [e for e in self.events("team.message_received")
                   if e.actor.id == self.worker and e.payload["kind"] == "report"
                   and e.payload.get("reply_to") == original[0]]
        self.assertEqual(len(returns), 1)
        completed, = self.events("team.objective_completed")
        self.assertLess(decisions[-1].seq, returns[0].seq)
        self.assertLess(returns[0].seq, completed.seq)
        final_seq = self.results("publish_artifact")[-1]["artifact_seq"]
        approved, = self.rt.team_reviews.view(self.task)
        self.assertEqual(completed.payload["artifacts"], approved["artifacts"])
        self.assertEqual(completed.payload["artifacts"][0]["seq"], final_seq)
        self.assertEqual(completed.payload["artifacts"][0]["content_sha256"],
                         hashlib.sha256(versions[2].encode()).hexdigest())
        for request, result in zip(requests, self.results("request_review")):
            reviewer_reports = [e for e in self.events("team.message_received")
                                if e.actor.id == self.reviewer and e.payload["kind"] == "report"
                                and e.payload.get("reply_to") == result["message_seq"]]
            self.assertEqual(len(reviewer_reports), 1)
            self.assertEqual(reviewer_reports[0].payload["review_seq"], request.seq)
            self.assertEqual(reviewer_reports[0].payload["recipient"], self.worker)
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")

    def test_end_turn_text_does_not_report_or_close_assignment(self):
        def worker(run):
            self.publish(run)
            return "Finished everything; this is only model text."
        def manager_progress(run):
            self.assertEqual(run.context["message"]["kind"], "progress")
            result = run.call("finish_work", summary="Attempt early delivery", verification="Text only")
            self.assertEqual((result["status"], result["code"]), ("error", "work_pending"))
            run.call("wait_for_reply")
        self.driver.scripts = {self.manager: [self.delegate_wait, manager_progress], self.worker: [worker]}
        self.receive()
        self.drain()
        self.assertFalse([e for e in self.events("team.message_received")
                          if e.actor.id == self.worker and e.payload["kind"] == "report"])
        assignment, = self.rt.team.recovery.assignments(self.task)
        self.assertTrue(assignment["requires_return"])
        self.assertEqual(assignment["status"], "interrupted")
        self.assertEqual(self.events("team.objective_completed"), [])
        worker_finish, = [e for e in self.events("team.external_run_finished")
                          if e.payload["role"] == self.worker]
        self.assertEqual(worker_finish.payload["outcome"], "returned")

    def test_external_start_commits_before_factory_and_tools_across_connections(self):
        seen = []
        def observe():
            self.assertFalse(self.rt.log.db.in_transaction)
            with_log = EventLog(self.rt.log.path)
            try:
                starts = list(with_log.iter_events(task_id=self.task, event_types=("team.external_run_started",)))
                self.assertEqual(len(starts), 1)
                self.assertEqual(list(with_log.iter_events(task_id=self.task, event_types=("team.tool_started",))), [])
                seen.append(starts[0].seq)
            finally:
                with_log.close()
        self.driver.factory_observer = observe
        self.driver.scripts = {self.manager: [lambda run: run.call("wait_for_reply")]}
        self.receive()
        self.drain()
        self.assertEqual(len(seen), 1)
        self.assertLess(seen[0], self.events("team.tool_started")[0].seq)

    def test_customer_interruption_prevents_old_tool(self):
        def interrupted(run):
            self.receive("New customer direction")
            self.publish(run, "must not be published")
        self.driver.scripts = {self.manager: [interrupted, lambda run: "New input received"]}
        self.receive()
        self.drain()
        self.assertEqual(self.events("team.tool_started"), [])
        self.assertEqual(self.rt.task(self.task).deliverables, [])
        self.assertEqual(self.events("team.run_finished")[0].payload["status"], "interrupted")

    def test_pause_prevents_old_tool(self):
        def paused(run):
            self.rt.execution_control.change(self.task, "pause", request_id="offline-pause")
            self.publish(run, "must not be published")
        self.driver.scripts = {self.manager: [paused]}
        self.receive()
        self.drain()
        self.assertEqual(self.events("team.tool_started"), [])
        self.assertEqual(self.rt.task(self.task).deliverables, [])
        self.assertEqual(self.events("team.run_finished")[0].payload["status"], "interrupted")

    def test_objective_change_on_other_connection_prevents_old_worker_tool(self):
        other = EventLog(self.rt.log.path)
        self.addCleanup(other.close)
        def worker(run):
            # Inject a concurrent trusted host revision, not a model tool receipt.
            other.emit(type="team.objective_updated", actor=HOST, task_id=self.task,
                       payload={"goal": "New independently adopted objective"})
            self.publish(run, "stale worker output")
        self.driver.scripts = {self.worker: [worker]}
        self.receive(role=self.worker)
        self.drain()
        self.assertEqual(self.rt.team.view(self.task)["objective"], "New independently adopted objective")
        self.assertEqual(self.results("publish_artifact"), [])
        self.assertFalse([e for e in self.events("team.tool_started") if e.payload["role"] == self.worker])

    def test_failed_prompt_with_confirmed_cleanup_keeps_unknown_fact_but_allows_delivery(self):
        def failure(run):
            raise RuntimeError("Offline remote result is unknown")
        self.driver.scripts = {self.manager: [failure]}
        self.receive("Fail once")
        self.drain()
        failed, = self.events("team.external_run_finished")
        self.assertEqual(failed.payload["outcome"], "unknown")
        self.assertTrue(failed.payload["prompt_attempted"])
        self.assertTrue(failed.payload["cleanup_complete"])
        self.assertEqual(self.rt.team.external_runs.pending(self.task), [])
        def recovered(run):
            self.publish(run, "Published after confirmed local cleanup\n")
            self.deliver(run)
        self.driver.scripts[self.manager] = [recovered]
        self.receive("Publish and deliver after cleanup")
        self.drain()
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        artifact, = self.rt.task(self.task).deliverables
        self.assertEqual(Path(artifact["local_path"]).read_text(encoding="utf-8"),
                         "Published after confirmed local cleanup\n")

    def test_unconfirmed_cleanup_stays_pending_and_blocks_finish(self):
        def failure(run):
            run.cleanup_complete = False
            raise RuntimeError("Offline cleanup remains unknown")
        self.driver.scripts = {self.manager: [failure]}
        self.receive("Fail cleanup")
        self.drain()
        pending, = self.rt.team.external_runs.pending(self.task)
        self.assertEqual(pending["outcome"], "unknown")
        self.driver.scripts[self.manager] = [self.deliver]
        self.receive("Try to deliver")
        self.drain()
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertEqual(self.results("finish_work"), [])
        self.assertEqual([r["started_seq"] for r in self.rt.team.external_runs.pending(self.task)],
                         [pending["started_seq"]])
        self.assertEqual(self.events("team.run_finished")[-1].payload["status"], "interrupted")

    def test_outer_transaction_never_calls_external_driver(self):
        self.receive()
        with self.rt.log.atomic():
            with self.assertRaises(WorkConflict):
                self.rt.collaborate(self.task)
        self.assertEqual(self.driver.runs, [])
        self.assertEqual(self.events("team.external_run_started"), [])
        self.assertEqual(self.events("team.tool_started"), [])


if __name__ == "__main__":
    unittest.main()
