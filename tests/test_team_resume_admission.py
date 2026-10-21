"""Offline drivers, real Runtime/HTTP, and barriers around resume admission."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cater.events import Actor
from cater.external_agent import QoderAcpDriver
from cater.runtime import Runtime
from cater.team import HOST, TeamService
from cater.web import ActivityRunner, make_handler
from cater.work import WorkConflict
from test_simple_team import turn
from test_team_external import ScriptedExternalDriver


class _Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait(self):
        self.entered.set()
        if not self.release.wait(10):
            raise AssertionError("resume test did not release its barrier")


class _NativeDriver:
    name = "resume-admission-offline"
    model = "offline-fixed-driver"

    def __init__(self):
        self.calls = []
        self.gate = None
        self.outcome = "reply"
        self.write_first = False

    def tool_turn(self, *, role, prompt, history, tools):
        context = json.loads(prompt.rsplit("\n", 1)[-1])
        self.calls.append(context)
        if self.write_first and len(self.calls) == 1:
            return turn("", ("workspace_write", {
                "path": "once.txt", "content": "one effect", "expected_sha256": None}))
        if self.gate:
            self.gate.wait()
        if self.outcome == "unknown":
            raise WorkConflict("Offline fixture: request sent, result unknown")
        return turn("", ("reply_to_customer", {"text": "Answer: " + context["message"]["text"]}))


class _Scenario:
    def __init__(self, case, root, external):
        self.case, self.root = case, root
        self.db, self.roles = root / "runtime.db", root / "roles.json"
        self.workspaces = root / "workspaces.json"
        self.driver = ScriptedExternalDriver() if external else _NativeDriver()
        config = json.loads((Path(__file__).resolve().parents[1]
                             / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = self.driver.name
        for role in config["roles"]:
            role["driver"] = self.driver.name
        self.roles.write_text(json.dumps(config), encoding="utf-8")
        (root / "project").mkdir()
        self.workspaces.write_text(json.dumps({"workspaces": [{"id": "project", "title": "Test",
            "root": str(root / "project"), "mode": "edit"}]}), encoding="utf-8")
        with self.factory() as rt:
            self.task = rt.create_task("Admission regression")
            self.manager = rt.organization.leader.id
            self.worker_name = rt.worker_roles[0].name
            rt.team.enable(self.task)
        if external:
            def answer(run):
                run.call("reply_to_customer", text="Answer: " + run.context["message"]["text"])
            self.driver.scripts[self.manager] = [answer] * 4
        self.runner = ActivityRunner(self.factory)

    def factory(self):
        return Runtime(self.db, roles_path=self.roles, workspaces_path=self.workspaces,
                       drivers={self.driver.name: self.driver}, close_drivers_on_close=False)

    def request(self, action, data, expected=200):
        req = Request(f"{self.base}/api/tasks/{self.task}/{action}",
                      data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
        try:
            response = urlopen(req, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            self.case.assertEqual(response.status, expected, body)
            return json.loads(body)

    def control(self, action, key=None, expected=200):
        return self.request("control", {"action": action, "request_id": key or f"test-control-{action}"}, expected)

    def send(self, text="Original customer input", key="test-original-input"):
        self.request("command", {"instruction": text, "request_id": key}, 202)
        return next(e for e in self.events() if e.type == "team.message_received"
                    and e.actor == Actor.human() and e.payload.get("request_id") == key)

    def events(self):
        with self.factory() as rt:
            self.case.assertTrue(rt.verify_chain().ok)
            return rt.log.read(task_id=self.task)

    def contexts(self):
        if isinstance(self.driver, _NativeDriver):
            return self.driver.calls
        return [run.context for run in self.driver.runs if run.context is not None]

    def drain(self, runner=None):
        runner = runner or self.runner
        for _ in range(6):
            activity = runner.status(self.task)
            if activity["state"] != "queued":
                return
            runner._run(self.task, activity["id"], activity["action"])
        self.case.fail("Activity queue did not settle within six rounds")

    @contextmanager
    def blocked(self, source, gate, *, at_claim=True):
        errors = []
        original = TeamService._claim
        intercepted = False

        def claim(team, task):
            nonlocal intercepted
            result = original(team, task)
            if result and task == self.task and result[0].seq == source.seq and not intercepted:
                intercepted = True
                gate.wait()
            return result

        def work():
            try:
                activity = self.runner.status(self.task)
                self.runner._run(self.task, activity["id"], activity["action"])
            except BaseException as error:
                errors.append(error)

        with patch.object(TeamService, "_claim", claim) if at_claim else nullcontext():
            worker = threading.Thread(target=work, name="resume-admission-test")
            worker.start()
            try:
                self.case.assertTrue(gate.entered.wait(10), "Execution did not reach the requested boundary")
                yield
            finally:
                gate.release.set()
                worker.join(10)
                self.case.assertFalse(worker.is_alive(), "Test worker failed to stop")
        self.case.assertEqual(errors, [])

    def assert_preserved(self, prefix):
        self.case.assertEqual(self.events()[:len(prefix)], prefix, "Existing audit events were changed")

    def assert_answered_once(self, source):
        self.case.assertEqual([c["message"]["seq"] for c in self.contexts()], [source.seq])
        answers = [e for e in self.events() if e.type == "msg.said"
                   and e.payload.get("reply_to") == source.seq and e.payload.get("audience") == "customer"]
        self.case.assertEqual(len(answers), 1, "Original input must receive one customer answer")
        self.case.assertEqual(answers[0].payload["text"], "Answer: " + source.payload["text"])
        self.case.assertEqual([e for e in self.events() if e.type == "team.message_received"
                               and e.actor == Actor.human()], [source], "Do not resend a synthetic human input")


class ResumeAdmissionTests(unittest.TestCase):
    @contextmanager
    def scenario(self, *, external=False):
        evidence = Path(__file__).resolve().parents[1] / ".cater/qa/resume-admission-20261022"
        evidence.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="test-runtime-", dir=evidence) as directory:
            fixture = _Scenario(self, Path(directory), external)
            with patch("cater.web.ActivityRunner", return_value=fixture.runner):
                handler = make_handler(fixture.db, runtime_factory=fixture.factory)
            with patch.object(fixture.runner, "_start"), patch.object(handler, "log_message"):
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                fixture.base = f"http://127.0.0.1:{server.server_port}"
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
                thread.start()
                try:
                    yield fixture
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(10)
                    self.assertFalse(thread.is_alive())

    def test_http_resume_between_claim_and_first_call_keeps_same_input(self):
        for external in (False, True):
            with self.subTest(external=external), self.scenario(external=external) as s:
                s.control("pause")
                source, gate = s.send(), _Gate()
                with s.blocked(source, gate):
                    prefix = s.events()
                    self.assertTrue(any(e.type == "team.run_started"
                        and e.payload["message_seq"] == source.seq for e in prefix))
                    self.assertEqual(s.contexts(), [])
                    self.assertFalse([e for e in prefix if e.type in {
                        "team.model_called", "team.external_run_started", "team.tool_started"}])
                    s.control("resume")
                s.drain()
                s.assert_preserved(prefix)
                s.assert_answered_once(source)
                self.assertFalse(s.runner.status(s.task)["active"])

    def test_restart_recovers_original_seq_with_fresh_runner_only(self):
        for external in (False, True):
            with self.subTest(external=external), self.scenario(external=external) as s:
                s.control("pause")
                source, gate = s.send(), _Gate()
                with s.blocked(source, gate):
                    s.control("resume")
                prefix = s.events()
                replacement = ActivityRunner(s.factory)
                with patch.object(replacement, "_start"), patch.object(s.runner, "_run",
                        side_effect=AssertionError("The old runner cannot survive a restart")):
                    for _ in range(2):
                        replacement.recover_interrupted()
                        s.drain(replacement)
                        s.assert_preserved(prefix)
                        s.assert_answered_once(source)
                with s.factory() as rt:
                    self.assertEqual(rt.team.view(s.task)["pending_count"], 0)
                self.assertFalse(replacement.status(s.task)["active"])

    def test_distinct_resume_requests_preserve_original_seq_once(self):
        for external in (False, True):
            with self.subTest(external=external), self.scenario(external=external) as s:
                s.control("pause")
                source, gate = s.send(), _Gate()
                with s.blocked(source, gate):
                    prefix = s.events()
                    s.control("resume")
                    s.control("resume", "test-second-resume")
                s.drain()
                s.assert_preserved(prefix)
                s.assert_answered_once(source)
                s.control("resume", "test-second-resume")
                s.drain()
                s.assert_preserved(prefix)
                s.assert_answered_once(source)
                with s.factory() as rt:
                    self.assertEqual(rt.team.view(s.task)["pending_count"], 0)
                self.assertFalse(s.runner.status(s.task)["active"])

    def test_later_customer_input_takes_priority_without_reviving_old_input(self):
        for same_recipient in (True, False):
            with self.subTest(same_recipient=same_recipient), self.scenario() as s:
                s.control("pause")
                source, gate = s.send(), _Gate()
                with s.blocked(source, gate):
                    s.control("resume")
                    text = ("Replace old request: discuss a new direction" if same_recipient
                            else f"@{s.worker_name} An independent customer question")
                    newer = s.send(text, "test-newer-input")
                self.assertEqual(source.payload["recipient"] == newer.payload["recipient"], same_recipient)
                s.drain()
                with s.factory() as rt:
                    rt.collaborate(s.task)
                called = [c["message"]["seq"] for c in s.contexts()]
                self.assertCountEqual(called, [newer.seq] if same_recipient else [source.seq, newer.seq])
                for message in (source, newer):
                    replies = [e for e in s.events() if e.type == "msg.said"
                               and e.payload.get("reply_to") == message.seq
                               and e.payload.get("audience") == "customer"]
                    self.assertEqual(len(replies), 0 if same_recipient and message == source else 1)

    def test_cancel_or_delete_blocks_unstarted_input_and_preserves_audit(self):
        for action in ("cancel", "delete"):
            with self.subTest(action=action), self.scenario() as s:
                s.control("pause")
                source, gate = s.send(), _Gate()
                with s.blocked(source, gate):
                    prefix = s.events()
                    s.control("resume")
                    if action == "delete":
                        s.request("remove", {"request_id": "test-delete-input"})
                    else:
                        s.control("cancel")
                s.drain()
                s.runner.recover_interrupted()
                with s.factory() as rt:
                    rt.collaborate(s.task)
                self.assertEqual(s.contexts(), [])
                s.assert_preserved(prefix)
                self.assertFalse([e for e in s.events() if e.type == "team.tool_started"])

    def test_changed_scope_never_replays_old_authorization(self):
        for change in ("objective", "rules", "workspace", "channel"):
            with self.subTest(change=change), self.scenario() as s:
                s.control("pause")
                source, gate = s.send(), _Gate()
                with s.blocked(source, gate):
                    s.control("resume")
                prefix = s.events()
                with s.factory() as rt:
                    if change == "objective":
                        rt.log.emit(type="team.objective_updated", task_id=s.task, actor=HOST,
                                    payload={"goal": "A different authorized objective"})
                    elif change == "rules":
                        rt.rulebook.record("Discuss only; do not implement old work", scope="task", task_id=s.task)
                    elif change == "workspace":
                        rt.workspaces.bind(s.task, "project", expected_revision=None)
                if change == "channel":
                    s.driver.model = "different-offline-model"
                s.drain()
                s.runner.recover_interrupted()
                with s.factory() as rt:
                    rt.collaborate(s.task)
                self.assertEqual(s.contexts(), [], "Scope changes must not authorize old input")
                s.assert_preserved(prefix)

    def test_native_called_or_unknown_or_effectful_run_is_not_blindly_replayed(self):
        for outcome in ("reply", "unknown", "written"):
            with self.subTest(outcome=outcome), self.scenario() as s:
                if outcome == "written":
                    with s.factory() as rt:
                        rt.workspaces.bind(s.task, "project", expected_revision=None)
                    s.driver.write_first = True
                s.control("pause")
                if outcome == "written":
                    s.control("resume", "test-running-before-write")
                    s.drain()
                source, gate = s.send(), _Gate()
                s.driver.gate, s.driver.outcome = gate, outcome
                with s.blocked(source, gate, at_claim=False):
                    before = len(s.contexts())
                    if outcome == "written":
                        self.assertEqual((s.root / "project/once.txt").read_text(), "one effect")
                        s.control("pause", "test-pause-after-write")
                    else:
                        self.assertFalse([e for e in s.events() if e.type == "team.model_called"])
                    s.control("resume")
                prefix = s.events()
                s.drain()
                s.runner.recover_interrupted()
                with s.factory() as rt:
                    rt.collaborate(s.task)
                self.assertEqual(len(s.contexts()), before, "An actual request is not an unstarted admission")
                if outcome == "unknown":
                    self.assertFalse([e for e in s.events() if e.type == "team.model_called"],
                                     "Missing model_called alone cannot prove a request was never sent")
                if outcome == "written":
                    writes = [e for e in s.events() if e.type == "team.tool_finished"
                              and e.payload.get("name") == "workspace_write"]
                    self.assertEqual(len(writes), 1)
                    self.assertEqual(writes[0].payload["result"]["status"], "ok")
                s.assert_preserved(prefix)

    def test_external_prompt_attempt_or_unknown_result_is_not_blindly_replayed(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown), self.scenario(external=True) as s:
                s.control("pause")
                source, gate = s.send(), _Gate()

                def sent(run):
                    gate.wait()
                    if unknown:
                        raise WorkConflict("Offline fixture: external prompt sent, result unknown")
                    return "Offline late response"

                s.driver.scripts[s.manager] = [sent] * 3
                with s.blocked(source, gate, at_claim=False):
                    self.assertEqual(len(s.driver.runs), 1)
                    self.assertTrue(s.driver.runs[0].state.prompt_attempted)
                    self.assertTrue([e for e in s.events() if e.type == "team.external_run_started"])
                    self.assertFalse([e for e in s.events() if e.type == "team.model_called"])
                    s.control("resume")
                prefix = s.events()
                s.drain()
                s.runner.recover_interrupted()
                with s.factory() as rt:
                    rt.collaborate(s.task)
                self.assertEqual(len(s.driver.runs), 1)
                self.assertEqual(s.driver.errors, [])
                s.assert_preserved(prefix)

    def test_legacy_no_call_receipt_requires_known_external_protocol(self):
        for external in (False, True):
            with self.subTest(external=external), self.scenario(external=external) as s:
                scripted = s.driver
                if external:
                    s.driver = QoderAcpDriver(scripted.name, "offline-never-launched", "offline")
                    s.driver.runs = scripted.runs
                with patch.object(s.driver, "agent_run", side_effect=scripted.agent_run) if external else nullcontext():
                    s.control("pause")
                    with s.factory() as rt:
                        source = rt.team.receive(s.task, "Legacy customer input", request_id="test-legacy-input")
                        _, _, started = rt.team._claim(s.task)
                    s.control("resume")
                    with s.factory() as rt:
                        # Reconstruct the old persisted protocol, without any modern admission proof.
                        rt.log.emit(type="team.run_finished", task_id=s.task, actor=HOST, payload={
                            "message_seq": source.seq, "role": s.manager, "run_id": started.payload["run_id"],
                            "status": "interrupted", "reason": "Legacy control transition before execution"})
                        self.assertEqual(rt.team.view(s.task)["pending_count"], 1 if external else 0)
                    prefix = s.events()
                    s.drain()
                    with s.factory() as rt:
                        rt.collaborate(s.task)
                    s.assert_preserved(prefix)
                    if external:
                        s.assert_answered_once(source)
                    else:
                        self.assertEqual(s.contexts(), [], "Old native absence of receipts is not proof")


if __name__ == "__main__":
    unittest.main()
