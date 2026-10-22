"""Independent HTTP boundaries for read-only instance identity."""

from __future__ import annotations

from contextlib import chdir, redirect_stdout
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from cater import __version__, web
from cater.web_assets import capture_web_assets
from test_runtime import start_test_web
from test_web_assets import write_bundle


class WebInstanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "private-history-name.db"
        self.token = "instance-fixture-access-secret"
        self.guards = []
        self.addCleanup(self.assert_no_execution)
        for target in (
            "cater.web.Runtime", "sqlite3.connect", "cater.runtime.load_drivers",
            "cater.drivers.load_drivers", "cater.runtime.load_runtime_config",
            "cater.web.ActivityRunner.recover_interrupted",
            "subprocess.Popen", "urllib.request.OpenerDirector.open",
        ):
            self.guards.append(self.enterContext(patch(
                target, side_effect=AssertionError("identity attempted " + target))))
        self.driver = Mock(spec=["complete", "tool_turn", "token_budget"])
        self.driver.token_budget = None
        for method in (self.driver.complete, self.driver.tool_turn):
            method.side_effect = AssertionError("identity attempted model execution")
            self.guards.append(method)

    def assert_no_execution(self):
        # Worker/recovery code can catch exceptions: also inspect every spy.
        for guard in self.guards:
            guard.assert_not_called()

    def guard_handler(self, handler):
        self.guards.append(self.enterContext(patch.object(
            handler, "_runtime", side_effect=AssertionError("identity opened runtime"))))

    def start(self, db=None, **options):
        options.setdefault("recover_on_startup", False)
        _, server, _ = start_test_web(
            self, self.db if db is None else db, return_server=True, **options)
        self.guard_handler(server.RequestHandlerClass)
        return server

    def listen(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(server.server_close)
        self.guard_handler(handler)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(3)
            self.assertFalse(thread.is_alive(), "identity HTTP loop did not stop")

        self.addCleanup(stop)
        return server

    def request(self, server, path="/api/instance", headers=None, *, hosts=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            if hosts is None:
                connection.request("GET", path, headers=headers or {})
            else:
                connection.putrequest("GET", path, skip_host=True)
                for host in hosts:
                    connection.putheader("Host", host)
                for key, value in (headers or {}).items():
                    connection.putheader(key, value)
                connection.endheaders()
            response = connection.getresponse()
            body = response.read()
            metadata = dict(response.getheaders())
            self.assertEqual(len(body), int(metadata["Content-Length"]))
            return response.status, metadata, body
        finally:
            connection.close()

    def identity(self, server, path="/api/instance", headers=None):
        status, metadata, body = self.request(server, path, headers)
        self.assertEqual(status, 200, body)
        self.assertEqual(metadata["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(metadata["Cache-Control"], "no-store")
        self.assertNotIn("ETag", metadata)
        result = json.loads(body.decode("utf-8"))
        self.assertEqual(set(result), {
            "schema_version", "instance_id", "package_version", "backend_build_id",
            "ui_version", "startup_recovery", "listener", "database",
        })
        self.assertIs(type(result["schema_version"]), int)
        self.assertEqual(result["schema_version"], 1)
        identifier = uuid.UUID(result["instance_id"])
        self.assertEqual(identifier.version, 4)
        self.assertEqual(result["package_version"], __version__)
        self.assertIsNone(result["backend_build_id"])
        self.assertIs(type(result["startup_recovery"]), bool)
        self.assertEqual(set(result["listener"]), {"host", "port"})
        self.assertIs(type(result["listener"]["host"]), str)
        self.assertIs(type(result["listener"]["port"]), int)
        self.assertEqual(result["listener"], {
            "host": server.server_address[0], "port": server.server_address[1],
        })
        self.assertEqual(set(result["database"]), {"kind", "locator_id"})
        self.assertIn(result["database"]["kind"], {"file", "memory", "custom", "unknown"})
        locator = result["database"]["locator_id"]
        if result["database"]["kind"] == "file":
            self.assertIs(type(locator), str)
            self.assertRegex(locator, r"\A[0-9a-f]{64}\Z")
        else:
            self.assertIsNone(locator)
        if result["ui_version"] is not None:
            self.assertIs(type(result["ui_version"]), str)
            self.assertRegex(result["ui_version"], r"\A[0-9a-f]{64}\Z")
        return result

    def assert_rejected(self, server, path, expected, headers=None, *, hosts=None):
        status, metadata, body = self.request(server, path, headers, hosts=hosts)
        self.assertEqual(status, expected, body)
        self.assertEqual(metadata["Cache-Control"], "no-store")
        for private in (self.token, str(self.db), self.db.name, "instance_id", "locator_id"):
            self.assertNotIn(private.encode(), body)

    def test_get_has_exact_schema_no_runtime_database_model_or_private_fields(self):
        absent = self.root / "must-not-be-created" / self.db.name
        private_config = self.root / "private-driver-config.json"
        server = self.start(absent, access_token=self.token, drivers_path=private_config,
                            roles_path=self.root / "private-roles.json",
                            audit_key="fixture-audit-secret", audit_key_id="private-key-name",
                            shared_drivers={"fixture": self.driver})
        result = self.identity(server, headers={"Authorization": "Bearer " + self.token})
        self.assertIsNone(result["ui_version"])
        self.assertIs(result["startup_recovery"], False)
        expected = hashlib.sha256(os.path.normcase(os.path.abspath(absent)).encode("utf-8")).hexdigest()
        self.assertEqual(result["database"], {"kind": "file", "locator_id": expected})
        body = json.dumps(result)
        for private in (str(self.root), absent.name, private_config.name, self.token,
                        "fixture-audit-secret", "private-key-name", "private-roles.json"):
            self.assertNotIn(private, body)
        self.assertFalse(absent.parent.exists())
        self.assertFalse(private_config.exists())
        self.assert_no_execution()

    def test_listener_uses_actual_ephemeral_address_instead_of_host_header(self):
        server = self.start()
        self.assertGreater(server.server_port, 0)
        spoof_port = 1 if server.server_port != 1 else 2
        for host in (f"localhost:{spoof_port}", f"[::1]:{spoof_port}", "LOCALHOST"):
            with self.subTest(host=host):
                result = self.identity(server, headers={"Host": host})
                self.assertEqual(result["listener"], {"host": "127.0.0.1", "port": server.server_port})
                self.assertNotEqual(result["listener"]["port"], spoof_port)

    def test_instance_uuid_is_stable_per_handler_and_shared_across_its_listeners(self):
        runner_type = web.ActivityRunner
        runners = []

        def capture_runner(factory):
            runner = runner_type(factory)
            runners.append(runner)
            return runner

        with patch.object(web, "ActivityRunner", side_effect=capture_runner):
            first = self.start()
            second = self.listen(first.RequestHandlerClass)
            fresh = self.start()
        self.assertEqual(len(runners), 2)
        self.assertNotEqual(first.server_port, second.server_port)
        original = self.identity(first)
        self.assertEqual(original["instance_id"], runners[0].instance_id)
        for server in (first, second, first, second):
            with self.subTest(port=server.server_port):
                current = self.identity(server)
                self.assertEqual(current["instance_id"], original["instance_id"])
                self.assertEqual(current["database"], original["database"])
        replacement = self.identity(fresh)
        self.assertEqual(replacement["instance_id"], runners[1].instance_id)
        self.assertNotEqual(replacement["instance_id"], original["instance_id"])
        self.assertEqual(replacement["database"], original["database"])

    def test_untrusted_missing_duplicate_and_malformed_hosts_are_rejected(self):
        server = self.start()
        hosts_to_reject = (
            [], ["localhost", "localhost"], ["localhost", "foreign.invalid"],
            ["foreign.invalid"], ["localhost.attacker.invalid"], ["localhost@foreign.invalid"],
            ["localhost/path"], ["localhost?query"], ["localhost#fragment"],
            ["localhost:invalid"], ["localhost:65536"],
        )
        for hosts in hosts_to_reject:
            with self.subTest(hosts=hosts):
                self.assert_rejected(server, "/api/instance", 403, hosts=hosts)

    def test_header_tokens_and_host_auth_query_validation_order(self):
        server = self.start(access_token=self.token)
        proxy = {"Host": "proxy.identity.invalid:444"}
        for headers in (proxy, {**proxy, "Authorization": "Bearer wrong"},
                        {**proxy, "X-Cater-Access-Token": "wrong"}):
            with self.subTest(headers=headers):
                self.assert_rejected(server, "/api/instance", 401, headers)
        for credential in ({"Authorization": "Bearer " + self.token},
                           {"X-Cater-Access-Token": self.token}):
            with self.subTest(credential=next(iter(credential))):
                self.identity(server, headers={**proxy, **credential})
        self.assert_rejected(server, "/api/instance?unexpected=1", 403,
                             {"Host": "localhost/path"})
        self.assert_rejected(server, "/api/instance?unexpected=1", 401, proxy)
        self.assert_rejected(server, "/api/instance?unexpected=1", 400,
                             {**proxy, "Authorization": "Bearer " + self.token})
        self.assert_rejected(server, "/api/instance", 403,
                             {"Host": "localhost/path", "X-Cater-Access-Token": self.token})

    def test_query_and_fragment_cannot_supply_credentials_or_change_identity(self):
        server = self.start(access_token=self.token)
        authorized = {"X-Cater-Access-Token": self.token}
        paths = (
            "/api/instance?token=" + self.token,
            "/api/instance?access_token=" + self.token,
            "/api/instance?X-Cater-Access-Token=" + self.token,
            "/api/instance?ignored=1", "/api/instance?ignored=",
            "/api/instance#token=" + self.token,
            "/api/instance/#fragment", "/api/instance?ignored=1#fragment",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assert_rejected(server, path, 401)
                self.assert_rejected(server, path, 400, authorized)
        original = self.identity(server, headers=authorized)
        for suffix in ("/", "//", "?", "#"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.identity(server, "/api/instance" + suffix, authorized), original)

    def test_file_locator_normalizes_configuration_and_is_frozen_before_cwd_changes(self):
        configured = os.path.join("not-created", "..", "private-history-name.db")
        expected = hashlib.sha256(os.path.normcase(
            os.path.abspath(self.root / configured)).encode("utf-8")).hexdigest()
        with chdir(self.root):
            server = self.start(configured)
        other = self.root / "other-working-directory"
        other.mkdir()
        with chdir(other):
            result = self.identity(server)
        absolute = self.start(self.db)
        self.assertEqual(result["database"], {"kind": "file", "locator_id": expected})
        self.assertEqual(self.identity(absolute)["database"], result["database"])
        self.assertFalse(self.db.exists())
        self.assertFalse((self.root / "not-created").exists())

    def test_locator_tracks_configured_location_instead_of_database_contents(self):
        self.db.write_bytes(b"synthetic non-database fixture A")
        server = self.start()
        original = self.identity(server)["database"]
        self.db.write_bytes(b"synthetic non-database fixture B")
        self.assertEqual(self.identity(server)["database"], original)
        self.assertEqual(self.identity(self.start())["database"], original)
        alternate = self.root / "another-private-name.db"
        alternate.write_bytes(self.db.read_bytes())
        different = self.identity(self.start(alternate))["database"]
        self.assertNotEqual(different["locator_id"], original["locator_id"])
        self.assertEqual(self.db.read_bytes(), b"synthetic non-database fixture B")

    def test_memory_custom_and_unknown_locations_have_no_locator(self):
        for configured, custom, expected in (
            (":memory:", False, "memory"),
            (self.db, True, "custom"), (":memory:", True, "custom"),
            ("file:private.db?mode=memory&cache=shared", True, "custom"),
            ("", True, "custom"), ("", False, "unknown"),
            ("file:private.db?mode=ro", False, "unknown"),
            ("file::memory:?cache=shared", False, "unknown"),
        ):
            with self.subTest(configured=configured, custom=custom):
                options = {}
                if custom:
                    factory = Mock(side_effect=AssertionError("identity called custom runtime"))
                    self.guards.append(factory)
                    options["runtime_factory"] = factory
                result = self.identity(self.start(configured, **options))
                self.assertEqual(result["database"], {"kind": expected, "locator_id": None})
        self.assertFalse(self.db.exists())

    def test_ui_version_matches_captured_assets_and_survives_source_mutation(self):
        bundle = self.root / "private-ui-source"
        original_files = write_bundle(bundle, "A")
        expected = capture_web_assets(bundle).version
        server = self.start(static_dir=bundle)
        original = self.identity(server)
        self.assertEqual(original["ui_version"], expected)
        write_bundle(bundle, "B")
        changed = capture_web_assets(bundle).version
        self.assertNotEqual(changed, expected)
        self.assertEqual(self.identity(server), original)
        for resource in ("/index.html", "/assets/main.js"):
            with self.subTest(resource=resource):
                status, _, body = self.request(server, resource)
                self.assertEqual(status, 200)
                self.assertEqual(body, original_files[resource])
        fresh = self.identity(self.start(static_dir=bundle))
        self.assertEqual(fresh["ui_version"], changed)
        self.assertNotEqual(fresh["instance_id"], original["instance_id"])
        self.assertNotIn(str(bundle), json.dumps(original))

    def test_recovery_metadata_inherits_default_and_explicit_constructor_policy(self):
        for options, expected in (
            ({}, True), ({"instance_recovery_policy": None}, True),
            ({"recover_on_startup": False}, False),
            ({"recover_on_startup": False, "instance_recovery_policy": None}, False),
        ):
            with self.subTest(options=options), patch.object(
                    web.ActivityRunner, "recover_interrupted", return_value=0) as recover:
                server = self.listen(web.make_handler(self.db, **options))
                self.assertEqual(recover.call_count, int(expected))
                result = self.identity(server)
                self.assertIs(result["startup_recovery"], expected)
                self.assertEqual(recover.call_count, int(expected))

    def test_explicit_recovery_metadata_overrides_deferred_constructor_policy(self):
        for actual in (False, True):
            for reported in (False, True):
                with self.subTest(actual=actual, reported=reported), patch.object(
                        web.ActivityRunner, "recover_interrupted", return_value=0) as recover:
                    server = self.start(recover_on_startup=actual, instance_recovery_policy=reported)
                    self.assertEqual(recover.call_count, int(actual))
                    self.assertIs(self.identity(server)["startup_recovery"], reported)
                    self.assertEqual(recover.call_count, int(actual))

    def test_recovery_policy_rejects_non_boolean_values_before_runtime_or_recovery(self):
        for field in ("recover_on_startup", "instance_recovery_policy"):
            invalid = (0, 1, 0.0, 1.0, "false", "true", [], {})
            if field == "recover_on_startup":
                invalid += (None,)
            for value in invalid:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        web.make_handler(self.db, **{field: value})
                    self.assert_no_execution()

    def test_actual_serve_forwards_and_reports_recovery_policy_without_real_recovery(self):
        case = self
        for options, expected in (({}, True), ({"recover_on_startup": True}, True),
                                  ({"recover_on_startup": False}, False)):
            with self.subTest(options=options):
                owner = SimpleNamespace(drivers={"fixture": self.driver},
                                        mcp=SimpleNamespace(pool=object()), close=Mock())
                servers = []
                observed = []

                class ProbeServer(ThreadingHTTPServer):
                    def __init__(server, address, handler):
                        super().__init__(address, handler)
                        servers.append(server)
                        case.guard_handler(handler)

                    def serve_forever(server):
                        case.assertEqual(recover.call_count, int(expected))
                        thread = threading.Thread(target=ThreadingHTTPServer.serve_forever,
                                                  args=(server,), kwargs={"poll_interval": 0.01},
                                                  daemon=True)
                        thread.start()
                        try:
                            observed.append(case.identity(server))
                            case.assertEqual(recover.call_count, int(expected))
                            create_owner.assert_called_once()
                        finally:
                            server.shutdown()
                            thread.join(3)
                            case.assertFalse(thread.is_alive(), "serve HTTP loop did not stop")

                with patch.object(web, "Runtime", return_value=owner) as create_owner, \
                        patch.object(web, "make_handler", wraps=web.make_handler) as make, \
                        patch.object(web, "ThreadingHTTPServer", ProbeServer), \
                        patch.object(web, "_workroom_dist", return_value=None), \
                        patch.object(web.ActivityRunner, "recover_interrupted", return_value=0) as recover, \
                        redirect_stdout(io.StringIO()):
                    web.serve(self.db, host="127.0.0.1", port=0, **options)
                create_owner.assert_called_once_with(":memory:", drivers_path=None,
                                                     roles_path=None, mcp_path=None)
                make.assert_called_once()
                self.assertIs(make.call_args.kwargs["recover_on_startup"], False)
                self.assertIs(make.call_args.kwargs["instance_recovery_policy"], expected)
                self.assertIs(make.call_args.kwargs["shared_drivers"], owner.drivers)
                self.assertIs(make.call_args.kwargs["shared_mcp_pool"], owner.mcp.pool)
                self.assertEqual(len(observed), 1)
                self.assertIs(observed[0]["startup_recovery"], expected)
                self.assertEqual(observed[0]["database"]["kind"], "file")
                self.assertEqual(recover.call_count, int(expected))
                owner.close.assert_called_once_with()
                self.assertEqual(len(servers), 1)
                self.assertEqual(servers[0].fileno(), -1)
                self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
