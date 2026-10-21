"""Startup recovery belongs to the runner behind a successfully bound listener."""

from contextlib import redirect_stdout
import http.client
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from cater import web
from cater.mcp import McpService
from cater.runtime import Runtime
from cater.workspaces import WorkspaceService


class WebStartupBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "history.db"
        self.driver = Mock(spec=["complete", "tool_turn", "token_budget"])
        self.driver.token_budget = None
        self.guards = [self.driver.complete, self.driver.tool_turn]
        for call in self.guards:
            call.side_effect = AssertionError("unexpected model execution")
        # Replace loading itself so a missed spy cannot fall through to a real driver.
        self.enterContext(patch("cater.runtime.load_drivers", side_effect=lambda *a, **k: {
            name: self.driver for name in ("product", "design", "dev", "test")}))
        for target in ("subprocess.Popen", "urllib.request.OpenerDirector.open"):
            self.guards.append(self.enterContext(patch(
                target, side_effect=AssertionError("unexpected process or API execution"))))
        for owner, method in ((WorkspaceService, "complete"), (McpService, "execute")):
            self.guards.append(self.enterContext(patch.object(
                owner, method, side_effect=AssertionError("unexpected tool execution"))))
        self.addCleanup(self.assert_no_execution)
        self.enterContext(patch.object(web, "_workroom_dist", return_value=None))

        self.scheduling = {name: self.enterContext(patch.object(
            web.ActivityRunner, name, autospec=True)) for name in (
                "_start", "answer_questions", "answer_conversations", "continue_team")}
        with Runtime(self.db) as runtime:
            self.question_task = runtime.create_task("Pending explanation")
            runtime.questions.ask(self.question_task, "What is saved?", request_id="startup-question")
            self.conversation_task = runtime.create_task("Pending conversation")
            runtime.conversation.receive(self.conversation_task, "Discuss the goal",
                                         request_id="startup-conversation")
            self.control_task = runtime.create_task("Pending resume")
            runtime.execution_control.change(self.control_task, "pause", request_id="startup-pause")
            self.resume, _ = runtime.execution_control.change(
                self.control_task, "resume", request_id="startup-resume")
            self.activity_task = runtime.create_task("Interrupted activity")
        self.old_activity = web.ActivityRunner(lambda: Runtime(self.db)).submit(self.activity_task, "run")
        for call in self.scheduling.values():
            call.reset_mock()
        self.baseline = self.database_dump()
        self.original_recover = web.ActivityRunner.recover_interrupted
        self.recover = self.enterContext(patch.object(web.ActivityRunner, "recover_interrupted",
            autospec=True, side_effect=self.original_recover))
        self.owners = []

        def runtime_factory(path, **options):
            runtime = Runtime(path, **options)
            if str(path) == ":memory:":
                runtime.close = Mock(wraps=runtime.close)
                self.owners.append(runtime)
            return runtime

        self.enterContext(patch.object(web, "Runtime", side_effect=runtime_factory))

    def assert_no_execution(self):
        # Recovery catches some worker errors, so raising in a spy alone is insufficient.
        for call in self.guards:
            call.assert_not_called()

    def database_dump(self):
        connection = sqlite3.connect(self.db)
        try:
            return tuple(connection.iterdump())
        finally:
            connection.close()

    def assert_owner_closed(self):
        owner, = self.owners
        owner.close.assert_called_once_with()
        with self.assertRaises(sqlite3.ProgrammingError):
            owner.log.db.execute("SELECT 1")

    def assert_recovered(self, runner):
        self.assertEqual(runner.status(self.activity_task)["state"], "failed")
        self.assertEqual(runner.status(self.control_task)["state"], "queued")
        self.scheduling["answer_questions"].assert_called_once_with(runner, self.question_task)
        self.scheduling["answer_conversations"].assert_called_once_with(runner, self.conversation_task)
        start = self.scheduling["_start"]
        start.assert_called_once()
        self.assertIs(start.call_args.args[0], runner)
        self.assertEqual(start.call_args.args[1], self.control_task)
        with Runtime(self.db) as runtime:
            failures = list(runtime.log.iter_events(task_id=self.activity_task,
                                                    event_types=("activity.failed",)))
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0].payload["activity_id"], self.old_activity["id"])
            self.assertEqual(failures[0].payload["error_type"], "InterruptedActivity")
            queues = list(runtime.log.iter_events(task_id=self.control_task,
                                                  event_types=("activity.queued",)))
            self.assertEqual(len(queues), 1)
            self.assertEqual(queues[0].payload["resume_seq"], self.resume.seq)

    def get_json(self, server, path):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(response.status, 200, body)
            return json.loads(body)
        finally:
            connection.close()

    def check_gets_use_runner(self, server, runner):
        def activity():
            return self.get_json(server, f"/api/tasks/{self.activity_task}")["activity"]

        self.assertEqual(activity(), runner.status(self.activity_task))
        # A second, new GET must observe live changes to the recovered closure runner.
        queued = runner.submit(self.activity_task, "run")
        self.assertEqual(activity(), runner.status(self.activity_task))
        self.assertEqual(activity()["id"], queued["id"])
        self.assertTrue(runner._mark_running(self.activity_task, queued["id"]))
        rows = self.get_json(server, "/api/task-summaries")["tasks"]
        summary = next(row for row in rows if row["id"] == self.activity_task)
        expected = runner.status(self.activity_task)
        self.assertEqual(summary["activity"], {key: expected[key] for key in (
            "state", "active", "revision", "instance_id")})
        self.assertEqual(activity(), expected)

    def run_http_checks(self, server, runner):
        thread = threading.Thread(target=ThreadingHTTPServer.serve_forever,
                                  args=(server,), kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            self.check_gets_use_runner(server, runner)
        finally:
            server.shutdown()
            thread.join(5)
            self.assertFalse(thread.is_alive(), "HTTP loop did not stop")

    def check_startup_failure(self, port, exception, *, servers=(), **options):
        output = io.StringIO()
        real_connect = sqlite3.connect
        history_opens = []

        def memory_only(path, *args, **kwargs):
            if str(path) != ":memory:":
                history_opens.append(path)
                raise AssertionError("failed startup opened old history")
            return real_connect(path, *args, **kwargs)

        with patch("sqlite3.connect", side_effect=memory_only), \
                patch.object(threading.Thread, "start",
                             side_effect=AssertionError("failed startup started a thread")) as start, \
                redirect_stdout(output):
            with self.assertRaises(exception) as raised:
                web.serve(self.db, host="127.0.0.1", port=port, **options)
        self.assertEqual(history_opens, [])
        self.recover.assert_not_called()
        start.assert_not_called()
        for call in self.scheduling.values():
            call.assert_not_called()
        self.assertNotIn("Cater web listening", output.getvalue())
        self.assert_owner_closed()
        for server in servers:
            self.assertEqual(server.fileno(), -1)
        self.assertEqual(self.database_dump(), self.baseline)
        return raised.exception

    def test_default_make_handler_recovers_once_and_exposes_same_runner(self):
        handler = web.make_handler(self.db)
        self.recover.assert_called_once()
        runner, = self.recover.call_args.args
        self.assertIs(handler._recover_interrupted.__self__, runner)
        self.assert_recovered(runner)
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            self.run_http_checks(server, runner)
        self.recover.assert_called_once_with(runner)

    def test_false_make_handler_defers_history_and_recovery_to_explicit_hook(self):
        factory = Mock(side_effect=lambda: Runtime(self.db))
        handler = web.make_handler(self.db, runtime_factory=factory, recover_on_startup=False)
        factory.assert_not_called()
        self.recover.assert_not_called()
        self.assertEqual(self.database_dump(), self.baseline)
        for call in self.scheduling.values():
            call.assert_not_called()
        self.assertEqual(handler._recover_interrupted(), 1)
        runner, = self.recover.call_args.args
        self.assertIs(handler._recover_interrupted.__self__, runner)
        self.assert_recovered(runner)
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            self.run_http_checks(server, runner)
        self.recover.assert_called_once_with(runner)

    def test_held_real_loopback_port_leaves_history_unopened_and_unchanged(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                held.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            for options in ({}, {"recover_on_startup": True}, {"recover_on_startup": False}):
                with self.subTest(options=options):
                    self.owners.clear()
                    self.check_startup_failure(held.getsockname()[1], OSError, **options)

    def test_real_socket_rejects_invalid_ports_before_history_or_dispatch(self):
        for port, exception in ((-1, OverflowError), (65536, OverflowError), ("invalid", TypeError)):
            with self.subTest(port=port):
                self.owners.clear()
                self.check_startup_failure(port, exception)

    def test_constructor_bind_and_activate_errors_close_socket_without_recovery(self):
        for phase in ("bind", "activate"):
            with self.subTest(phase=phase):
                self.owners.clear()
                servers = []
                error = OSError(f"injected server_{phase} failure")

                class FailingServer(ThreadingHTTPServer):
                    def __init__(server, *args, **kwargs):
                        servers.append(server)
                        super().__init__(*args, **kwargs)

                    def server_bind(server):
                        super().server_bind()
                        if phase == "bind":
                            raise error

                    def server_activate(server):
                        super().server_activate()
                        if phase == "activate":
                            raise error

                with patch.object(web, "ThreadingHTTPServer", FailingServer):
                    raised = self.check_startup_failure(0, OSError, servers=servers)
                self.assertIs(raised, error)
                self.assertEqual(len(servers), 1)

    def test_serve_binds_and_listens_before_single_recovery_on_handler_runner(self):
        events, servers = [], []
        output = io.StringIO()
        case = self

        class ObservedServer(ThreadingHTTPServer):
            def __init__(server, *args, **kwargs):
                super().__init__(*args, **kwargs)
                servers.append(server)
                events.append("constructed")

            def server_bind(server):
                super().server_bind()
                events.append("bound")

            def server_activate(server):
                super().server_activate()
                events.append("listening")

            def serve_forever(server):
                events.append("loop")
                runner, = case.recover.call_args.args
                case.assertIs(server.RequestHandlerClass._recover_interrupted.__self__, runner)
                case.assert_recovered(runner)
                case.assertIn(f"http://127.0.0.1:{server.server_port}", output.getvalue())
                case.assertNotIn("http://127.0.0.1:0\n", output.getvalue())
                case.run_http_checks(server, runner)

        def recover(runner):
            self.assertEqual(events, ["bound", "listening", "constructed"])
            server, = servers
            self.assertGreater(server.server_port, 0)
            self.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 1)
            self.assertIs(server.RequestHandlerClass._recover_interrupted.__self__, runner)
            self.assertNotIn("Cater web listening", output.getvalue())
            events.append("recover")
            return self.original_recover(runner)

        self.recover.side_effect = recover
        with patch.object(web, "ThreadingHTTPServer", ObservedServer), \
                patch.object(web, "make_handler", wraps=web.make_handler) as make, redirect_stdout(output):
            web.serve(self.db, host="127.0.0.1", port=0)
        self.assertIs(make.call_args.kwargs["recover_on_startup"], False)
        self.recover.assert_called_once()
        self.assertEqual(events, ["bound", "listening", "constructed", "recover", "loop"])
        self.assertEqual(servers[0].fileno(), -1)
        self.assert_owner_closed()

    def test_recovery_error_closes_listener_and_owner_before_ready_or_loop(self):
        servers = []
        error = RuntimeError("injected startup recovery failure")
        output = io.StringIO()

        class ObservedServer(ThreadingHTTPServer):
            def __init__(server, *args, **kwargs):
                super().__init__(*args, **kwargs)
                servers.append(server)
                server.server_close = Mock(wraps=server.server_close)
                server.serve_forever = Mock(side_effect=AssertionError("loop before recovery succeeded"))

        def recover(runner):
            server, = servers
            self.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 1)
            raise error

        self.recover.side_effect = recover
        with patch.object(web, "ThreadingHTTPServer", ObservedServer), redirect_stdout(output):
            with self.assertRaises(RuntimeError) as raised:
                web.serve(self.db, host="127.0.0.1", port=0)
        self.assertIs(raised.exception, error)
        self.recover.assert_called_once()
        server, = servers
        server.server_close.assert_called_once_with()
        server.serve_forever.assert_not_called()
        self.assertEqual(server.fileno(), -1)
        self.assert_owner_closed()
        self.assertNotIn("Cater web listening", output.getvalue())
        self.assertEqual(self.database_dump(), self.baseline)

    def test_false_serve_listens_without_recovery_and_get_keeps_pending_history(self):
        servers = []
        output = io.StringIO()
        case = self

        class ObservedServer(ThreadingHTTPServer):
            def serve_forever(server):
                servers.append(server)
                case.recover.assert_not_called()
                case.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 1)
                case.assertIn(f"http://127.0.0.1:{server.server_port}", output.getvalue())
                runner = server.RequestHandlerClass._recover_interrupted.__self__
                thread = threading.Thread(target=ThreadingHTTPServer.serve_forever,
                    args=(server,), kwargs={"poll_interval": 0.01}, daemon=True)
                thread.start()
                try:
                    for task in (case.activity_task, case.question_task,
                                 case.conversation_task, case.control_task):
                        detail = case.get_json(server, f"/api/tasks/{task}")
                        case.assertEqual(detail["activity"], runner.status(task))
                        case.assertEqual(detail["activity"]["state"], "idle")
                    case.assertEqual(case.database_dump(), case.baseline)
                finally:
                    server.shutdown()
                    thread.join(5)
                    case.assertFalse(thread.is_alive(), "HTTP loop did not stop")

        with patch.object(web, "ThreadingHTTPServer", ObservedServer), redirect_stdout(output):
            web.serve(self.db, host="127.0.0.1", port=0, recover_on_startup=False)
        self.recover.assert_not_called()
        for call in self.scheduling.values():
            call.assert_not_called()
        self.assertEqual(servers[0].fileno(), -1)
        self.assert_owner_closed()
        self.assertEqual(self.database_dump(), self.baseline)


if __name__ == "__main__":
    unittest.main()
