"""NDJSON process containment and pool lifecycle, not MCP protocol emulation."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from cater.drivers import CliDriver, DriverError, DriverProtocolError, DriverUnavailable
from cater.mcp import McpPool, McpService, _base_environment


class McpProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def driver(self, source, **kwargs):
        driver = CliDriver("opaque-fixture", [sys.executable, "-u", "-c", source], timeout=5,
                           restart_limit=4, **kwargs)
        self.addCleanup(driver.close)
        return driver

    def test_explicit_environment_is_copied_and_does_not_inherit_parent_secret(self):
        source = "import sys,json,os; r=json.loads(sys.stdin.readline()); print(json.dumps({'id':r['id'],'ok':True,'result':{'type':r['type'],'marker':os.environ.get('MCP_FIXTURE_VISIBLE'),'secret':os.environ.get('MCP_FIXTURE_PARENT_SECRET')}}),flush=True)"
        env = {**_base_environment(), "MCP_FIXTURE_VISIBLE": "explicit"}
        with patch.dict(os.environ, {"MCP_FIXTURE_PARENT_SECRET": "must-not-inherit"}):
            driver = self.driver(source, env=env)
            env["MCP_FIXTURE_VISIBLE"] = "mutated"
            result = driver.exchange({"type": "ping"})["result"]
        self.assertEqual(result, {"type": "complete", "marker": "explicit", "secret": None})

    def test_default_environment_preserves_existing_cli_behavior(self):
        source = "import sys,json,os; r=json.loads(sys.stdin.readline()); print(json.dumps({'id':r['id'],'ok':True,'result':os.environ.get('MCP_FIXTURE_VISIBLE')}),flush=True)"
        with patch.dict(os.environ, {"MCP_FIXTURE_VISIBLE": "inherited"}):
            self.assertEqual(self.driver(source).exchange({})["result"], "inherited")

    def test_isolated_completion_keeps_explicit_environment_and_closes_worker(self):
        source = "import sys,json,os; r=json.loads(sys.stdin.readline()); print(json.dumps({'id':r['id'],'ok':True,'text':os.environ.get('MCP_FIXTURE_VISIBLE')}),flush=True)"
        driver = self.driver(source, env={**_base_environment(), "MCP_FIXTURE_VISIBLE": "isolated"})
        for context in ({"context_scope": "request"}, {"task_id": "child", "background": {}}):
            with self.subTest(context=context):
                self.assertEqual(driver.complete(role="tester", prompt="fixture", context=context).text, "isolated")
                self.assertIsNone(driver._proc)

    def test_raw_exchange_never_replays_lost_write_despite_restart_setting(self):
        marker = self.root / "calls.txt"
        source = ("import sys,json; from pathlib import Path; json.loads(sys.stdin.readline()); "
                  f"p=Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); sys.exit(7)")
        driver = self.driver(source)
        with self.assertRaises(DriverUnavailable):
            driver.exchange({"type": "ping", "action": "write"})
        self.assertEqual(marker.read_text(), "x")
        self.assertIsNone(driver._proc)
        self.assertIsNone(driver._scope)

    def test_raw_exchange_rejects_mismatched_response_id_and_reaps_process(self):
        driver = self.driver("import sys,json; sys.stdin.readline(); print(json.dumps({'id':'wrong','ok':True}),flush=True)")
        with self.assertRaises(DriverProtocolError):
            driver.exchange({})
        self.assertIsNone(driver._proc)
        self.assertIsNone(driver._io_thread)

    def test_pool_reuses_connection_rotates_configuration_and_environment_and_owns_close(self):
        pool = McpPool()
        self.addCleanup(pool.close)
        entry = {"id": "s", "configuration_sha256": "first", "bridge_command": [sys.executable], "timeout": 5}
        clients = []
        def new_client(*args, **kwargs):
            client = Mock(spec=["exchange", "close"])
            client.exchange.return_value = {"ok": True, "result": {}}
            clients.append(client)
            self.assertNotIn("MCP_FIXTURE_PARENT_SECRET", kwargs["env"])
            self.assertEqual(kwargs["restart_limit"], 0)
            return client
        with patch("cater.mcp.CliDriver", side_effect=new_client):
            pool.exchange(entry, {"action": "list"}, {})
            pool.exchange(entry, {"action": "call"}, {})
            self.assertEqual(len(clients), 1)
            self.assertEqual(clients[0].exchange.call_count, 2)
            pool.exchange(entry, {}, {"TOKEN": "test-only-new"})
            self.assertEqual(len(clients), 2)
            clients[0].close.assert_called_once_with()
            pool.exchange({**entry, "configuration_sha256": "second"}, {}, {"TOKEN": "test-only-new"})
            self.assertEqual(len(clients), 3)
            clients[1].close.assert_called_once_with()
            borrower = McpService(Mock(), pool=pool)
            borrower.close()
            clients[2].close.assert_not_called()
            pool.close()
            clients[2].close.assert_called_once_with()
            with self.assertRaises(DriverError):
                pool.exchange(entry, {}, {})

    def test_pool_close_attempts_every_client_even_after_cleanup_failure(self):
        pool = McpPool()
        self.addCleanup(pool.close)
        clients = [Mock(spec=["exchange", "close"]), Mock(spec=["exchange", "close"])]
        with patch("cater.mcp.CliDriver", side_effect=clients):
            for name in ("first", "second"):
                pool.exchange({"id": name, "configuration_sha256": name,
                               "bridge_command": [sys.executable], "timeout": 5}, {}, {})
        clients[0].close.side_effect = OSError("private cleanup detail")
        with self.assertRaisesRegex(DriverError, "MCP process cleanup failed") as raised:
            pool.close()
        self.assertNotIn("private cleanup detail", str(raised.exception))
        for client in clients:
            client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
