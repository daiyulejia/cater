"""Bounded candidate UI selection and startup-recovery regression checks."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
import http.client
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from cater import cli, web
from cater.mcp import McpService
from cater.runtime import Runtime, load_runtime_config
from cater.web_assets import MANIFEST_NAME, WebAssetsError
from cater.workspaces import WorkspaceService
from test_web_assets import write_bundle


class WebCandidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.db = self.base / "history.db"
        self.root = self.base / "candidate ui"
        self.files = write_bundle(self.root, "candidate-A")
        self.driver = Mock(spec=["complete", "tool_turn"])
        config = load_runtime_config()
        config = replace(config,
            roles=[replace(role, driver="candidate_test") for role in config.roles],
            organization=replace(config.organization,
                leader=replace(config.organization.leader, driver="candidate_test")))
        self.enterContext(patch("cater.runtime.load_runtime_config", return_value=config))
        self.popen = self.enterContext(patch("subprocess.Popen",
            side_effect=AssertionError("candidate tests must not start processes")))
        self.workspace = self.enterContext(patch.object(WorkspaceService, "complete",
            side_effect=AssertionError("unexpected workspace execution")))
        self.mcp = self.enterContext(patch.object(McpService, "execute",
            side_effect=AssertionError("unexpected MCP execution")))
        for call in (self.driver.complete, self.driver.tool_turn):
            call.side_effect = AssertionError("unexpected model call")
        self.addCleanup(self.assert_no_execution)

    def assert_no_execution(self):
        for call in (self.popen, self.workspace, self.mcp,
                     self.driver.complete, self.driver.tool_turn):
            call.assert_not_called()

    def runtime(self):
        return Runtime(self.db, drivers={"candidate_test": self.driver})

    def database_snapshot(self):
        connection = sqlite3.connect(self.db)
        try:
            return tuple(connection.iterdump())
        finally:
            connection.close()

    def seed_pending(self):
        with self.runtime() as runtime:
            question_task = runtime.create_task("Unstarted explanation")
            question = runtime.questions.ask(question_task, "What is recorded?",
                                            request_id="candidate-question-1")
            conversation_task = runtime.create_task("Unstarted conversation")
            conversation = runtime.conversation.receive(conversation_task, "Discuss the current goal",
                                                       request_id="candidate-conversation-1")
            control_task = runtime.create_task("Undispatched control resume")
            runtime.execution_control.change(control_task, "pause", request_id="candidate-pause-1")
            resume, changed = runtime.execution_control.change(
                control_task, "resume", request_id="candidate-resume-1")
            self.assertTrue(changed)
            activity_task = runtime.create_task("Unstarted old activity")
        with patch.object(web.ActivityRunner, "_start") as start:
            activity = web.ActivityRunner(self.runtime).submit(activity_task, "run")
            start.assert_called_once()
        with self.runtime() as runtime:
            self.assertEqual(runtime.questions.pending(question_task), [question.seq])
            self.assertEqual(runtime.conversation.pending(conversation_task), [conversation.seq])
            for task in (question_task, conversation_task, control_task, activity_task):
                self.assertEqual(list(runtime.log.iter_events(task_id=task,
                    event_types=("question.started", "conversation.started", "activity.started"))), [])
            self.assertEqual(list(runtime.log.iter_events(task_id=control_task,
                event_types=("activity.queued",))), [])
        return question_task, conversation_task, control_task, activity_task, resume, activity

    def start_server(self, **options):
        handler = web.make_handler(self.db, static_dir=self.root,
                                   runtime_factory=self.runtime, **options)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(5)
            self.assertFalse(thread.is_alive(), "candidate HTTP thread did not stop")

        self.addCleanup(stop)
        return server

    def request(self, server, path, *, method="GET", payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        request_headers = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_cli_defaults_and_old_namespace_keep_recovery_and_retry_defaults(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                args = cli.build_parser().parse_args(["--db", str(self.db), "web"])
                self.assertIsNone(args.web_assets)
                self.assertIs(args.no_startup_recovery, False)
                if legacy:
                    del args.web_assets
                    del args.no_startup_recovery
                with patch.object(cli, "serve") as serve:
                    self.assertEqual(cli.cmd_web(args), 0)
                serve.assert_called_once()
                self.assertEqual(serve.call_args.args, (self.db,))
                options = serve.call_args.kwargs
                self.assertIsNone(options["web_assets"])
                self.assertIs(options["recover_on_startup"], True)
                self.assertIs(options["text_retry"], True)
                self.assertEqual((options["driver_attempts"], options["circuit_breaker_threshold"]), (2, 2))
                self.assertEqual((options["host"], options["port"], options["allow_remote"]),
                                 ("127.0.0.1", 8765, False))
        self.assertFalse(self.db.exists())

    def test_cli_candidate_flags_forward_independently(self):
        for selected, no_recovery in ((True, False), (False, True), (True, True)):
            with self.subTest(selected=selected, no_recovery=no_recovery):
                argv = ["--db", str(self.db), "web"]
                if selected:
                    argv += ["--web-assets", str(self.root)]
                if no_recovery:
                    argv += ["--no-startup-recovery"]
                args = cli.build_parser().parse_args(argv)
                with patch.object(cli, "serve") as serve:
                    self.assertEqual(args.func(args), 0)
                self.assertEqual(serve.call_args.kwargs["web_assets"], str(self.root) if selected else None)
                self.assertIs(serve.call_args.kwargs["recover_on_startup"], not no_recovery)
                self.assertIs(serve.call_args.kwargs["text_retry"], True)

    def test_recovery_requires_a_real_bool_before_any_side_effect(self):
        for value in (None, 0, 1, -1, "false", "True", [], {}, object()):
            for entry in (web.serve, web.make_handler):
                with self.subTest(value=value, entry=entry.__name__), \
                        patch.object(web, "Runtime") as runtime, \
                        patch.object(web, "capture_web_assets") as capture, \
                        patch.object(web, "_workroom_dist") as default, \
                        patch.object(web, "ThreadingHTTPServer") as listener, \
                        patch.object(web.ActivityRunner, "recover_interrupted") as recover:
                    with self.assertRaisesRegex(ValueError, "recover_on_startup.*boolean"):
                        entry(self.db, recover_on_startup=value)
                    for call in (runtime, capture, default, listener, recover):
                        call.assert_not_called()
        self.assertFalse(self.db.exists())

    def test_serve_selects_explicit_or_default_ui_and_preserves_shared_resources(self):
        for selected in (None, self.root, str(self.root)):
            with self.subTest(selected=selected):
                owner = Mock()
                handler = object()
                server = Mock()
                server.serve_forever.side_effect = KeyboardInterrupt
                default_root = self.base / "default"
                with patch.object(web, "Runtime", return_value=owner) as runtime, \
                        patch.object(web, "make_handler", return_value=handler) as make, \
                        patch.object(web, "_workroom_dist", return_value=default_root) as default, \
                        patch.object(web, "ThreadingHTTPServer", return_value=server) as listener, \
                        redirect_stdout(io.StringIO()):
                    web.serve(self.db, port=0, web_assets=selected, recover_on_startup=False)
                runtime.assert_called_once_with(":memory:", drivers_path=None, roles_path=None, mcp_path=None)
                self.assertEqual(make.call_args.args, (self.db,))
                options = make.call_args.kwargs
                self.assertEqual(options["static_dir"], default_root if selected is None else selected)
                self.assertIs(options["recover_on_startup"], False)
                self.assertIs(options["shared_drivers"], owner.drivers)
                self.assertIs(options["shared_mcp_pool"], owner.mcp.pool)
                self.assertIsInstance(options["shared_driver_health"], web.DriverHealth)
                self.assertEqual((options["driver_attempts"], options["circuit_breaker_threshold"]), (2, 2))
                self.assertIs(options["text_retry"], True)
                if selected is None:
                    default.assert_called_once_with()
                else:
                    default.assert_not_called()
                listener.assert_called_once_with(("127.0.0.1", 0), handler)
                server.server_close.assert_called_once_with()
                owner.close.assert_called_once_with()

    def test_serve_default_recovery_and_no_bundle_legacy_selection(self):
        owner, server = Mock(), Mock()
        server.server_port = 43210
        output = io.StringIO()
        with patch.object(web, "Runtime", return_value=owner), \
                patch.object(web, "_workroom_dist", return_value=None) as default, \
                patch.object(web, "make_handler") as make, \
                patch.object(web, "ThreadingHTTPServer", return_value=server), \
                redirect_stdout(output):
            web.serve(self.db, port=0)
        self.assertEqual(output.getvalue(), "Cater web listening on http://127.0.0.1:43210\n")
        default.assert_called_once_with()
        self.assertIsNone(make.call_args.kwargs["static_dir"])
        self.assertIs(make.call_args.kwargs["recover_on_startup"], False)
        make.return_value._recover_interrupted.assert_called_once_with()
        server.server_close.assert_called_once_with()
        owner.close.assert_called_once_with()

    def test_serve_closes_owner_on_bind_error_and_owner_and_listener_on_loop_error(self):
        for failure in ("bind", "loop"):
            with self.subTest(failure=failure):
                owner, server = Mock(), Mock()
                error = OSError("injected " + failure)
                if failure == "loop":
                    server.serve_forever.side_effect = error
                with patch.object(web, "Runtime", return_value=owner), \
                        patch.object(web, "make_handler"), \
                        patch.object(web, "ThreadingHTTPServer", return_value=server,
                                     side_effect=error if failure == "bind" else None), \
                        redirect_stdout(io.StringIO()):
                    with self.assertRaises(OSError) as raised:
                        web.serve(self.db, port=0, web_assets=self.root)
                self.assertIs(raised.exception, error)
                owner.close.assert_called_once_with()
                if failure == "loop":
                    server.server_close.assert_called_once_with()
                else:
                    server.server_close.assert_not_called()

    def test_invalid_explicit_ui_fails_closed_before_history_or_listener(self):
        with self.runtime() as runtime:
            runtime.create_task("Existing history must stay unopened")
        baseline = self.database_snapshot()
        invalid_file = self.base / "not-a-directory"
        invalid_file.write_bytes(b"invalid")
        incomplete = self.base / "incomplete"
        write_bundle(incomplete)
        (incomplete / "assets/main.js").unlink()
        broken_manifest = self.base / "broken-manifest"
        write_bundle(broken_manifest)
        (broken_manifest / MANIFEST_NAME).write_bytes(b"{")
        for selected in (self.base / "missing", invalid_file, incomplete, broken_manifest, ""):
            for recovery in (False, True):
                with self.subTest(selected=selected, recovery=recovery):
                    owners = []

                    def memory_owner(path, **options):
                        self.assertEqual(path, ":memory:", "history opened before UI validation")
                        owner = Runtime(path, drivers={"candidate_test": self.driver}, **options)
                        owner.close = Mock(wraps=owner.close)
                        owners.append(owner)
                        return owner

                    with patch.object(web, "Runtime", side_effect=memory_owner) as runtime, \
                            patch.object(web, "_workroom_dist", return_value=self.root) as default, \
                            patch.object(web, "ThreadingHTTPServer") as listener, \
                            patch.object(web.ActivityRunner, "recover_interrupted") as recover:
                        with self.assertRaises(WebAssetsError):
                            web.serve(self.db, port=0, web_assets=selected, recover_on_startup=recovery)
                    runtime.assert_called_once()
                    default.assert_not_called()
                    listener.assert_not_called()
                    recover.assert_not_called()
                    self.assertEqual(len(owners), 1)
                    owners[0].close.assert_called_once_with()
                    with self.assertRaises(sqlite3.ProgrammingError):
                        owners[0].log.db.execute("SELECT 1")
                    self.assertEqual(self.database_snapshot(), baseline)

    def test_handler_invalid_ui_precedes_runtime_factory_even_without_recovery(self):
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                factory = Mock(side_effect=AssertionError("history opened"))
                with patch.object(web.ActivityRunner, "recover_interrupted") as recover, \
                        self.assertRaises(WebAssetsError):
                    web.make_handler(self.db, static_dir=self.base / "missing",
                                     runtime_factory=factory, recover_on_startup=recovery)
                factory.assert_not_called()
                recover.assert_not_called()
                self.assertFalse(self.db.exists())

    def test_serve_selected_ui_over_real_loopback_keeps_captured_bytes(self):
        ready = threading.Event()
        servers, errors, owners = [], [], []
        output = io.StringIO()
        (self.root / "private.txt").write_bytes(b"private fixture")

        class ObservedServer(ThreadingHTTPServer):
            def serve_forever(server, **options):
                servers.append(server)
                ready.set()
                return super().serve_forever(poll_interval=0.01)

        def runtime_factory(path, **options):
            options.setdefault("drivers", {"candidate_test": self.driver})
            runtime = Runtime(path, **options)
            if path == ":memory:":
                runtime.close = Mock(wraps=runtime.close)
                owners.append(runtime)
            return runtime

        def run():
            try:
                web.serve(self.db, port=0, web_assets=self.root,
                          recover_on_startup=False, access_token="candidate-token")
            except BaseException as exc:
                errors.append(exc)
                ready.set()

        with patch.object(web, "Runtime", side_effect=runtime_factory), \
                patch.object(web, "ThreadingHTTPServer", ObservedServer), \
                patch.object(web, "_workroom_dist") as default, \
                patch.object(web.ActivityRunner, "recover_interrupted") as recover, \
                redirect_stdout(output):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertTrue(ready.wait(5), "candidate listener did not start")
                self.assertEqual(errors, [])
                server, = servers
                self.assertGreater(server.server_port, 0)
                self.assertIn(f"Cater web listening on http://127.0.0.1:{server.server_port}\n",
                              output.getvalue())
                self.assertNotIn("http://127.0.0.1:0\n", output.getvalue())
                # The notice distinguishes startup recovery from new operations.
                self.assertIn("\u7eed\u63a5\u5df2\u5173\u95ed", output.getvalue())
                self.assertIn("\u65b0\u7684\u987e\u5ba2\u64cd\u4f5c\u4ecd\u53ef\u8c03\u7528", output.getvalue())
                status, headers, body = self.request(server, "/")
                self.assertEqual((status, body), (200, self.files["/index.html"]))
                version = headers["X-Cater-UI-Version"]
                write_bundle(self.root, "candidate-B")
                self.assertEqual(self.request(server, "/private.txt")[0], 404)
                (self.root / "assets/late.js").write_bytes(b"not captured")
                for name in self.files:
                    (self.root / name.lstrip("/")).unlink()
                for name, expected in self.files.items():
                    with self.subTest(name=name):
                        status, headers, body = self.request(server, name)
                        self.assertEqual((status, body), (200, expected))
                        self.assertEqual(headers["X-Cater-UI-Version"], version)
                for name in ("/private.txt", "/" + MANIFEST_NAME, "/assets/late.js"):
                    self.assertEqual(self.request(server, name)[0], 404)
                self.assertEqual(self.request(server, "/api/tasks")[0], 401)
                self.assertFalse(self.db.exists(), "static/auth requests opened history")
                auth = {"X-Cater-Access-Token": "candidate-token"}
                self.assertEqual(self.request(server, "/api/tasks", headers=auth)[0], 200)
                default.assert_not_called()
                recover.assert_not_called()
            finally:
                if servers:
                    servers[0].shutdown()
                thread.join(5)
                self.assertFalse(thread.is_alive(), "serve thread did not stop")
        self.assertEqual(errors, [])
        self.assertEqual(servers[0].fileno(), -1)
        owner, = owners
        owner.close.assert_called_once_with()

    def test_explicit_trusted_legacy_ui_does_not_require_a_manifest(self):
        (self.root / MANIFEST_NAME).unlink()
        legacy = b"<!doctype html><main>Trusted legacy candidate</main>"
        (self.root / "index.html").write_bytes(legacy)
        with patch.object(web, "capture_web_assets", wraps=web.capture_web_assets) as capture, \
                patch.object(web, "_workroom_dist") as default:
            server = self.start_server(recover_on_startup=False)
            status, headers, body = self.request(server, "/")
            self.assertEqual((status, body), (200, legacy))
            self.assertIn("X-Cater-UI-Version", headers)
            capture.assert_called_once_with(self.root)
            default.assert_not_called()
        self.assertFalse(self.db.exists())

    def test_selected_ui_keeps_host_token_and_origin_guards(self):
        server = self.start_server(recover_on_startup=False)
        for path in ("/", "/assets/main.js", "/api/tasks"):
            self.assertEqual(self.request(server, path,
                headers={"Host": "candidate.attacker.invalid"})[0], 403)
        authenticated = self.start_server(recover_on_startup=False, access_token="candidate-token")
        for token in (None, "wrong-token"):
            headers = {} if token is None else {"X-Cater-Access-Token": token}
            for method in ("GET", "POST"):
                self.assertEqual(self.request(authenticated, "/api/tasks", method=method,
                    payload={"goal": "Unauthorized"} if method == "POST" else None,
                    headers=headers)[0], 401)
        self.assertFalse(self.db.exists())
        auth = {"X-Cater-Access-Token": "candidate-token"}
        self.assertEqual(self.request(authenticated, "/", headers={"Host": "localhost/path"})[0], 403)
        self.assertEqual(self.request(authenticated, "/api/tasks", method="POST",
            payload={"goal": "Cross-origin"}, headers={**auth, "Origin": "https://foreign.invalid"})[0], 403)
        status, _, body = self.request(authenticated, "/api/tasks",
            headers={"Authorization": "Bearer candidate-token"})
        self.assertEqual((status, json.loads(body)), (200, []))

    def test_no_startup_recovery_and_gets_leave_real_pending_queues_and_db_unchanged(self):
        question_task, conversation_task, control_task, activity_task, _, _ = self.seed_pending()
        baseline = self.database_snapshot()
        with patch.object(web.ActivityRunner, "recover_interrupted") as recover, \
                patch.object(web.ActivityRunner, "_start") as start, \
                patch.object(web.ActivityRunner, "answer_questions") as questions, \
                patch.object(web.ActivityRunner, "answer_conversations") as conversations:
            server = self.start_server(recover_on_startup=False)
            self.assertEqual(self.database_snapshot(), baseline)
            paths = ("/", "/assets/main.js", "/api/tasks", "/api/task-summaries", "/api/chain",
                     *(f"/api/tasks/{task}" for task in
                       (question_task, conversation_task, control_task, activity_task)))
            for path in paths:
                with self.subTest(path=path):
                    self.assertEqual(self.request(server, path)[0], 200)
                    self.assertEqual(self.database_snapshot(), baseline)
            for call in (recover, start, questions, conversations):
                call.assert_not_called()
        with self.runtime() as runtime:
            self.assertEqual(len(runtime.questions.pending(question_task)), 1)
            self.assertEqual(len(runtime.conversation.pending(conversation_task)), 1)
        self.assert_no_execution()

    def test_default_handler_calls_original_recovery_and_resumes_durable_queues(self):
        question_task, conversation_task, control_task, activity_task, resume, activity = self.seed_pending()
        original = web.ActivityRunner.recover_interrupted
        with patch.object(web.ActivityRunner, "recover_interrupted", autospec=True,
                          side_effect=original) as recover, \
                patch.object(web.ActivityRunner, "_start") as start, \
                patch.object(web.ActivityRunner, "answer_questions") as questions, \
                patch.object(web.ActivityRunner, "answer_conversations") as conversations:
            web.make_handler(self.db, static_dir=self.root, runtime_factory=self.runtime)
        recover.assert_called_once()
        questions.assert_called_once_with(question_task)
        conversations.assert_called_once_with(conversation_task)
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0], control_task)
        with self.runtime() as runtime:
            failures = list(runtime.log.iter_events(task_id=activity_task, event_types=("activity.failed",)))
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0].payload["activity_id"], activity["id"])
            self.assertEqual(failures[0].payload["error_type"], "InterruptedActivity")
            queued = list(runtime.log.iter_events(task_id=control_task, event_types=("activity.queued",)))
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].payload["resume_seq"], resume.seq)

    def test_no_startup_recovery_allows_new_posts_and_explicit_execution_dispatch(self):
        with patch.object(web.ActivityRunner, "recover_interrupted") as recover, \
                patch.object(web.ActivityRunner, "_start") as start, \
                patch.object(web.ActivityRunner, "answer_questions") as questions:
            server = self.start_server(recover_on_startup=False, access_token="candidate-token")
            auth = {"X-Cater-Access-Token": "candidate-token"}
            status, _, body = self.request(server, "/api/tasks", method="POST", headers=auth,
                payload={"goal": "New active request", "request_id": "candidate-new-task"})
            self.assertEqual(status, 201)
            task_id = json.loads(body)["task"]["id"]
            status, _, _ = self.request(server, f"/api/tasks/{task_id}/questions",
                method="POST", headers=auth,
                payload={"question": "What is saved?", "request_id": "candidate-new-question"})
            self.assertEqual(status, 202)
            questions.assert_called_once_with(task_id)
            for action in ("pause", "resume"):
                status, _, body = self.request(server, f"/api/tasks/{task_id}/control",
                    method="POST", headers=auth,
                    payload={"action": action, "request_id": "candidate-new-" + action})
                self.assertEqual(status, 200)
            start.assert_called_once()
            self.assertEqual(start.call_args.args[0], task_id)
            recover.assert_not_called()
        with self.runtime() as runtime:
            self.assertEqual(runtime.task(task_id).goal, "New active request")
            self.assertEqual(len(runtime.questions.pending(task_id)), 1)
            self.assertEqual(runtime.execution_control.view(task_id)["state"], "running")
            queued = list(runtime.log.iter_events(task_id=task_id, event_types=("activity.queued",)))
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].payload["resume_seq"], runtime.execution_control.view(task_id)["seq"])


if __name__ == "__main__":
    unittest.main()
