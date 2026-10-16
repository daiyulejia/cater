"""MCP entrypoint wiring with local services and no external processes."""

from __future__ import annotations

import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cater import cli, web
from cater.runtime import Runtime


class FakeMcpService:
    def __init__(self, runtime, config_path=None, pool=None):
        self.runtime = runtime
        self.config_path = config_path
        self.owner = pool is None
        self.pool = Mock(spec=["close"]) if pool is None else pool
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        if self.owner:
            self.pool.close()


class McpEntrypointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "tasks.db"
        self.config = self.root / "mcp.json"
        self.services = []

        def service(*args, **kwargs):
            instance = FakeMcpService(*args, **kwargs)
            self.services.append(instance)
            return instance

        self.constructor = self.enterContext(patch("cater.runtime.McpService", side_effect=service))
        self.process = self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("external process")))

    def arguments(self, *options, command="show"):
        return cli.build_parser().parse_args([
            "--db", str(self.db), "--audit-key", "fixture-key", *options, command,
        ])

    def test_runtime_delegates_construction_and_close_to_service(self):
        with Runtime(mcp_path=self.config) as runtime:
            service = runtime.mcp
            self.constructor.assert_called_once_with(runtime, self.config, pool=None)
            self.assertEqual(service.close_calls, 0)
            self.assertIs(service.runtime, runtime)
        self.assertEqual(service.close_calls, 1)
        service.pool.close.assert_called_once_with()
        self.process.assert_not_called()

    def test_runtime_borrower_delegates_close_without_closing_shared_pool(self):
        pool = Mock(spec=["close"])
        with Runtime(mcp_path=self.config, mcp_pool=pool) as runtime:
            service = runtime.mcp
            self.constructor.assert_called_once_with(runtime, self.config, pool=pool)
        self.assertEqual(service.close_calls, 1)
        pool.close.assert_not_called()

    def test_runtime_attempts_all_cleanup_when_service_or_driver_fails(self):
        for stage in ("mcp", "driver"):
            with self.subTest(stage=stage):
                driver = Mock(spec=["close"])
                runtime = Runtime(drivers={"fixture": driver})
                with patch.object(runtime.log, "close", wraps=runtime.log.close) as close_log:
                    if stage == "mcp":
                        runtime.mcp.pool.close.side_effect = OSError("fixture cleanup failure")
                    else:
                        driver.close.side_effect = OSError("fixture cleanup failure")
                    with self.assertRaises(OSError):
                        runtime.close()
                    runtime.mcp.pool.close.assert_called_once_with()
                    driver.close.assert_called_once_with()
                    close_log.assert_called_once_with()

    def test_cli_global_option_and_open_runtime_forward_path(self):
        args = self.arguments("--mcp", str(self.config))
        self.assertEqual(args.mcp, str(self.config))
        with cli.open_runtime(args) as runtime:
            self.assertEqual(runtime.mcp.config_path, str(self.config))

    def test_cli_default_and_older_namespace_use_no_configuration(self):
        for older in (False, True):
            with self.subTest(older=older):
                args = self.arguments()
                self.assertIsNone(args.mcp)
                if older:
                    del args.mcp
                with cli.open_runtime(args) as runtime:
                    self.assertIsNone(runtime.mcp.config_path)
                args = self.arguments(command="web")
                if older:
                    del args.mcp
                with patch.object(cli, "serve") as serve:
                    self.assertEqual(cli.cmd_web(args), 0)
                self.assertIsNone(serve.call_args.kwargs["mcp_path"])

    def test_cmd_web_forwards_mcp_and_existing_configuration(self):
        args = self.arguments("--mcp", str(self.config), "--skills", "skills.json",
                              "--workspaces", "workspaces.json", command="web")
        with patch.object(cli, "serve") as serve:
            self.assertEqual(cli.cmd_web(args), 0)
        self.assertEqual(serve.call_args.args, (self.db,))
        self.assertEqual(serve.call_args.kwargs["mcp_path"], str(self.config))
        self.assertEqual(serve.call_args.kwargs["skills_path"], "skills.json")
        self.assertEqual(serve.call_args.kwargs["workspaces_path"], "workspaces.json")

    def test_handler_shares_only_pool_and_keeps_thread_local_runtimes(self):
        pool = Mock(spec=["close"])
        handler = web.make_handler(self.db, mcp_path=self.config, shared_mcp_pool=pool)
        runtimes = []
        failures = []

        def request():
            try:
                with handler.__new__(handler)._runtime() as runtime:
                    self.assertEqual(runtime.log.read(), [])
                    self.assertEqual(runtime.mcp.config_path, self.config)
                    self.assertIs(runtime.mcp.pool, pool)
                    runtimes.append(runtime)
            except BaseException as error:
                failures.append(error)

        request()
        thread = threading.Thread(target=request)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(runtimes), 2)
        self.assertIsNot(runtimes[0], runtimes[1])
        self.assertIsNot(runtimes[0].log, runtimes[1].log)
        self.assertTrue(all(service.close_calls == 1 for service in self.services))
        pool.close.assert_not_called()

    def test_handler_defaults_to_independent_service(self):
        handler = web.make_handler(self.db)
        with handler.__new__(handler)._runtime() as runtime:
            self.assertIsNone(runtime.mcp.config_path)
            self.assertTrue(runtime.mcp.owner)
        runtime.mcp.pool.close.assert_called_once_with()

    def test_custom_factory_keeps_its_configuration_and_pool(self):
        custom_pool = Mock(spec=["close"])
        ignored_pool = Mock(spec=["close"])
        factory = Mock(side_effect=lambda: Runtime(self.db, mcp_path="custom.json", mcp_pool=custom_pool))
        with patch.object(web, "Runtime", side_effect=AssertionError("custom factory replaced")):
            handler = web.make_handler(self.db, runtime_factory=factory,
                                       mcp_path=self.config, shared_mcp_pool=ignored_pool)
            with handler.__new__(handler)._runtime() as runtime:
                self.assertEqual(runtime.mcp.config_path, "custom.json")
                self.assertIs(runtime.mcp.pool, custom_pool)
        self.assertTrue(factory.called)
        self.assertTrue(all(call.args == () and call.kwargs == {} for call in factory.call_args_list))
        custom_pool.close.assert_not_called()
        ignored_pool.close.assert_not_called()

    def test_serve_owns_pool_until_server_close(self):
        def server_close():
            self.assertEqual(self.services[0].close_calls, 0)
            self.services[0].pool.close.assert_not_called()

        with patch.object(web, "make_handler", wraps=web.make_handler) as make_handler, \
                patch.object(web, "ThreadingHTTPServer") as server, \
                patch.object(web, "_workroom_dist", return_value=None), \
                patch("sys.stdout", new=io.StringIO()):
            server.return_value.server_close.side_effect = server_close
            web.serve(self.db, mcp_path=self.config, port=0)
        owner, *borrowers = self.services
        self.assertTrue(owner.owner)
        self.assertTrue(borrowers)
        self.assertTrue(all(not service.owner and service.pool is owner.pool for service in borrowers))
        self.assertTrue(all(service.config_path == self.config and service.close_calls == 1
                            for service in self.services))
        self.assertEqual(make_handler.call_args.kwargs["mcp_path"], self.config)
        self.assertIs(make_handler.call_args.kwargs["shared_mcp_pool"], owner.pool)
        owner.pool.close.assert_called_once_with()
        server.return_value.serve_forever.assert_called_once_with()
        server.return_value.server_close.assert_called_once_with()

    def test_serve_cleans_owner_on_setup_loop_and_close_failures(self):
        for stage in ("handler", "bind", "loop", "interrupt", "close"):
            with self.subTest(stage=stage):
                self.services.clear()
                with patch.object(web, "make_handler") as handler, \
                        patch.object(web, "ThreadingHTTPServer") as server, \
                        patch.object(web, "_workroom_dist", return_value=None), \
                        patch("sys.stdout", new=io.StringIO()):
                    if stage == "handler":
                        handler.side_effect = OSError("fixture handler failure")
                    elif stage == "bind":
                        server.side_effect = OSError("fixture bind failure")
                    elif stage == "loop":
                        server.return_value.serve_forever.side_effect = OSError("fixture loop failure")
                    elif stage == "interrupt":
                        server.return_value.serve_forever.side_effect = KeyboardInterrupt
                    else:
                        server.return_value.server_close.side_effect = OSError("fixture close failure")
                    if stage == "interrupt":
                        web.serve(self.db, mcp_path=self.config, port=0)
                    else:
                        with self.assertRaises(OSError):
                            web.serve(self.db, mcp_path=self.config, port=0)
                    owner, = self.services
                    self.assertEqual(owner.close_calls, 1)
                    owner.pool.close.assert_called_once_with()
                    if stage not in {"handler", "bind"}:
                        server.return_value.server_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
