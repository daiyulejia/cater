"""CLI and web skill configuration wiring, using temporary files and local fakes."""

from __future__ import annotations

import inspect
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

from cater import cli, web
from cater.runtime import Runtime


class SkillConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.db = self.root / "tasks.db"
        self.guide = self.root / "local-guide.md"
        self.guide.write_text("# Local guide\nInspect the local fixture.\n", encoding="utf-8")
        self.skills = self.root / "private-skill-config.json"
        self.skills.write_text(json.dumps({"skills": [{
            "id": "fixture-guide", "title": "Fixture guide",
            "description": "Local fixture guidance", "path": str(self.guide),
        }]}), encoding="utf-8")

    def arguments(self, *options, command="show"):
        return cli.build_parser().parse_args([
            "--db", str(self.db), "--audit-key", "fixture-audit-key", *options, command,
        ])

    def assert_configured(self, runtime, expected=True):
        index = runtime.skills.index(role_id="dev", workspace_id=None, tools=[], limit=20)
        configured = [row for row in index["skills"] if row["source"] == "configured"]
        self.assertEqual([row["id"] for row in configured], ["fixture-guide"] if expected else [])
        if expected:
            self.assertEqual(configured[0]["status"], "ready")

    def test_parser_skills_defaults_to_none_and_accepts_json_path(self):
        self.assertIsNone(self.arguments().skills)
        self.assertEqual(self.arguments("--skills", str(self.skills)).skills, str(self.skills))

    def test_cli_open_runtime_loads_configured_skills(self):
        with cli.open_runtime(self.arguments("--skills", str(self.skills))) as runtime:
            self.assert_configured(runtime)

    def test_cli_open_runtime_accepts_default_and_older_namespace(self):
        for older_namespace in (False, True):
            with self.subTest(older_namespace=older_namespace):
                args = self.arguments()
                if older_namespace:
                    del args.skills
                with cli.open_runtime(args) as runtime:
                    self.assert_configured(runtime, expected=False)

    def test_cmd_web_forwards_skills_alongside_workspaces(self):
        workspaces = str(self.root / "workspaces.json")
        args = self.arguments("--skills", str(self.skills), "--workspaces", workspaces, command="web")
        with patch.object(cli, "serve") as serve:
            self.assertEqual(cli.cmd_web(args), 0)
        self.assertEqual(serve.call_args.args, (self.db,))
        self.assertEqual(serve.call_args.kwargs["skills_path"], str(self.skills))
        self.assertEqual(serve.call_args.kwargs["workspaces_path"], workspaces)

    def test_cmd_web_accepts_default_and_older_namespace(self):
        for older_namespace in (False, True):
            with self.subTest(older_namespace=older_namespace):
                args = self.arguments(command="web")
                if older_namespace:
                    del args.skills
                with patch.object(cli, "serve") as serve:
                    self.assertEqual(cli.cmd_web(args), 0)
                self.assertIsNone(serve.call_args.kwargs["skills_path"])

    def test_web_signatures_keep_skills_optional(self):
        for target in (web.serve, web.make_handler):
            with self.subTest(target=target.__name__):
                parameter = inspect.signature(target).parameters["skills_path"]
                self.assertIsNone(parameter.default)
                self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_handler_default_runtime_loads_skills_and_workspaces(self):
        project = self.root / "project"
        project.mkdir()
        workspaces = self.root / "workspaces.json"
        workspaces.write_text(json.dumps({"workspaces": [{
            "id": "source", "title": "Source", "root": str(project), "mode": "edit",
        }]}), encoding="utf-8")
        handler = web.make_handler(self.db, skills_path=self.skills, workspaces_path=workspaces)
        with handler.__new__(handler)._runtime() as runtime:
            self.assert_configured(runtime)
            self.assertEqual(runtime.workspaces.options()[0]["id"], "source")

    def test_handler_without_skills_keeps_default_runtime(self):
        handler = web.make_handler(self.db)
        with handler.__new__(handler)._runtime() as runtime:
            self.assert_configured(runtime, expected=False)

    def test_custom_runtime_factory_retains_priority_and_receives_no_arguments(self):
        runtimes = []

        def factory():
            runtime = Runtime(self.db, skills_path=self.skills)
            runtimes.append(runtime)
            return runtime

        custom = Mock(side_effect=factory)
        with patch.object(web, "Runtime", side_effect=AssertionError("factory was replaced")):
            handler = web.make_handler(self.db, runtime_factory=custom,
                                       skills_path=self.root / "unused.json")
            with handler.__new__(handler)._runtime() as runtime:
                self.assertIs(runtime, runtimes[-1])
                self.assert_configured(runtime)
        self.assertGreater(custom.call_count, 1)
        self.assertTrue(all(call.args == () and call.kwargs == {} for call in custom.call_args_list))

    def test_serve_forwards_to_real_handler_but_driver_owner_only_owns_drivers(self):
        with patch.object(web, "Runtime", wraps=Runtime) as runtime_constructor, \
                patch.object(web, "make_handler", wraps=web.make_handler) as make_handler, \
                patch.object(web, "ThreadingHTTPServer") as server_constructor, \
                patch("sys.stdout", new=io.StringIO()):
            web.serve(self.db, skills_path=self.skills, port=0)
        self.assertEqual(make_handler.call_args.kwargs["skills_path"], self.skills)
        owner_call, *request_calls = runtime_constructor.call_args_list
        self.assertEqual(owner_call.args, (":memory:",))
        self.assertNotIn("skills_path", owner_call.kwargs)
        self.assertTrue(request_calls)
        self.assertTrue(all(call.kwargs["skills_path"] == self.skills for call in request_calls))
        server_constructor.return_value.serve_forever.assert_called_once_with()
        server_constructor.return_value.server_close.assert_called_once_with()

    def test_configuration_paths_stay_out_of_model_credentials_payload_and_web_responses(self):
        roles = self.root / "roles.json"
        roles.write_text(json.dumps({"roles": [{
            "id": "worker", "name": "Worker", "role": "worker", "driver": "fixture-model",
        }]}), encoding="utf-8")
        drivers = self.root / "drivers.json"
        drivers.write_text(json.dumps({"drivers": [{
            "kind": "openai_compatible", "name": "fixture-model",
            "endpoint": "https://model.invalid/v1/chat/completions", "model": "fixture-model",
            "api_key_env": "CATER_SKILL_CONFIGURATION_TEST_KEY",
        }]}), encoding="utf-8")
        args = self.arguments("--skills", str(self.skills), "--roles", str(roles), "--drivers", str(drivers))
        answer = json.dumps({"stance": "assert", "option_id": "A", "claim": "Fixture checked",
                             "text": "Fixture checked", "evidence": ["local fixture"], "confidence": 0.9})
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({
            "choices": [{"message": {"content": answer}}], "usage": {"total_tokens": 3},
        }).encode()
        with patch.dict("os.environ", {"CATER_SKILL_CONFIGURATION_TEST_KEY": "fixture-secret"}), \
                patch("cater.drivers._open_api_request", return_value=response) as request:
            with cli.open_runtime(args) as runtime:
                self.assert_configured(runtime)
                model = runtime.drivers["fixture-model"]
                self.assertEqual(model.api_key_env, "CATER_SKILL_CONFIGURATION_TEST_KEY")
                task_id = runtime.create_task("Inspect the fixture")
                runtime.collaborate(task_id)
                events = [web.event_to_dict(event) for event in runtime.log.read()]
        self.assertTrue(request.called)
        for call in request.call_args_list:
            outbound = call.args[0]
            self.assertEqual(outbound.get_header("Authorization"), "Bearer fixture-secret")
            self.assert_paths_absent(outbound.header_items())
            self.assert_paths_absent(json.loads(outbound.data))
        self.assert_paths_absent(events)
        handler = web.make_handler(self.db, skills_path=self.skills, roles_path=roles, drivers_path=drivers)
        for path in ("/api/tasks", f"/api/tasks/{task_id}", "/api/organization/profile", "/api/chain"):
            with self.subTest(path=path):
                instance = handler.__new__(handler)
                instance.path = path
                instance.headers = Message()
                instance.headers["Host"] = "localhost"
                with patch.object(instance, "_send_json") as send:
                    instance.do_GET()
                send.assert_called_once()
                self.assert_paths_absent(send.call_args.args[0])

    def assert_paths_absent(self, value):
        serialized = json.dumps(value, ensure_ascii=False)
        for path in (self.skills, self.guide):
            self.assertNotIn(str(path), serialized)
            self.assertNotIn(json.dumps(str(path))[1:-1], serialized)
            self.assertNotIn(path.as_posix(), serialized)
            self.assertNotIn(path.name, serialized)


if __name__ == "__main__":
    unittest.main()
