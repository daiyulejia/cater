"""Black-box ACP transport contract tests, with no model or external service."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cater.acp import (
    AcpError,
    AcpPromptResult,
    AcpProtocolError,
    AcpSession,
    AcpUnavailable,
    failure_code,
)


PEER = Path(__file__).resolve().parent / "fixtures" / "acp_peer.py"
THOUGHT = "PRIVATE_THOUGHT_MUST_NOT_ESCAPE"
SID = "fixture-session"


class CancelLockProbe:
    """Observe cancel's lock attempt, optionally delaying it across a turn change."""

    def __init__(self, lock, *, delay=False):
        self.lock = lock
        self.waiting = threading.Event()
        self.release = threading.Event()
        if not delay:
            self.release.set()

    def __enter__(self):
        if threading.current_thread().name == "fixture-cancel":
            self.waiting.set()
            if not self.release.wait(4):
                raise AssertionError("test did not release cancel's lock attempt")
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


class AcpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.counter = 0

    def session(self, mode="normal", **kwargs):
        self.counter += 1
        root = self.root / str(self.counter)
        root.mkdir()
        client = AcpSession(
            [sys.executable, "-u", str(PEER), mode, str(root)],
            cwd=root, timeout=kwargs.pop("timeout", 5), **kwargs,
        )
        self.addCleanup(client.close)
        return client, root

    def wire(self, root):
        path = root / "wire.jsonl"
        return ([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                if path.exists() else [])

    def requests(self, root, method):
        return [entry for entry in self.wire(root) if entry.get("method") == method]

    def assert_terminal(self, client):
        with self.assertRaises(AcpUnavailable):
            client.open()
        with self.assertRaises(AcpUnavailable):
            client.prompt("must never be sent")
        self.assertFalse(client.cancel())
        client.close()
        client.close()

    def start_prompt(self, client):
        ready = threading.Event()
        cancelled = threading.Event()
        done = threading.Event()
        outcomes = []

        def on_update(value):
            text = value.get("content", {}).get("text")
            if text == "ready":
                ready.set()
            elif text == "cancel-observed":
                cancelled.set()

        def run():
            try:
                outcomes.append(client.prompt("held prompt", on_update=on_update))
            except BaseException as exc:
                outcomes.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()

        def cleanup():
            client.close()
            worker.join(3)

        self.addCleanup(cleanup)
        self.assertTrue(ready.wait(4), "peer did not acknowledge prompt")
        return worker, cancelled, done, outcomes

    def background(self, client, call, *, name="fixture-prompt", releases=()):
        outcomes = []

        def run():
            try:
                outcomes.append(call())
            except BaseException as exc:
                outcomes.append(exc)

        worker = threading.Thread(target=run, name=name, daemon=True)

        def cleanup():
            for event in releases:
                event.set()
            client.close()
            worker.join(3)

        self.addCleanup(cleanup)
        worker.start()
        return worker, outcomes

    def join_background(self, worker, outcomes):
        worker.join(3)
        self.assertFalse(worker.is_alive(), "fixture thread did not finish")
        self.assertEqual(len(outcomes), 1)
        if isinstance(outcomes[0], BaseException):
            raise outcomes[0]
        return outcomes[0]

    def test_error_hierarchy(self):
        self.assertTrue(issubclass(AcpError, RuntimeError))
        self.assertTrue(issubclass(AcpProtocolError, AcpError))
        self.assertTrue(issubclass(AcpUnavailable, AcpError))

    def test_initialize_new_and_repeated_unicode_prompts_share_one_session(self):
        client, root = self.session()
        capabilities = {"fs": {"readTextFile": True}, "terminal": False}
        servers = [{"name": "fixture", "command": "not-executed", "args": [], "env": []}]
        opened = client.open(mcp_servers=servers, client_capabilities=capabilities)
        self.assertEqual(opened, {"sessionId": SID})
        self.assertEqual(client.session_id, SID)
        self.assertEqual(client.agent_info, {"name": "local-acp-fixture", "version": "1"})
        for text in ("\u4e2d\u6587\u63d0\u793a \U0001f680", "second\nline"):
            updates = []
            result = client.prompt(text, on_update=updates.append)
            self.assertIsInstance(result, AcpPromptResult)
            self.assertEqual(result.text, "\u4f60\u597d\uff0c\u4e16\u754c")
            self.assertEqual(result.stop_reason, "end_turn")
            self.assertEqual([item["sessionUpdate"] for item in updates],
                             ["agent_message_chunk", "agent_message_chunk"])
            self.assertNotIn(THOUGHT, repr(result))
            self.assertNotIn(THOUGHT, repr(updates))
        client.close()
        messages = [row for row in self.wire(root) if "method" in row]
        self.assertEqual([row["method"] for row in messages],
                         ["initialize", "session/new", "session/prompt", "session/prompt"])
        self.assertTrue(all(row["jsonrpc"] == "2.0" for row in messages))
        self.assertEqual(messages[0]["params"]["protocolVersion"], 1)
        self.assertEqual(messages[0]["params"]["clientCapabilities"], capabilities)
        self.assertEqual(Path(messages[1]["params"]["cwd"]).resolve(), root.resolve())
        self.assertEqual(messages[1]["params"]["mcpServers"], servers)
        for row, text in zip(messages[2:], ("\u4e2d\u6587\u63d0\u793a \U0001f680", "second\nline")):
            self.assertEqual(row["params"]["sessionId"], SID)
            self.assertEqual(row["params"]["prompt"], [{"type": "text", "text": text}])
        self.assertEqual(len({row["id"] for row in messages}), len(messages))

    def test_default_open_sends_empty_servers_and_idle_cancel_sends_nothing(self):
        client, root = self.session()
        self.assertFalse(client.cancel())
        client.open()
        self.assertFalse(client.cancel())
        client.prompt("complete")
        self.assertFalse(client.cancel())
        client.close()
        self.assertEqual(self.requests(root, "session/new")[0]["params"]["mcpServers"], [])
        self.assertEqual(self.requests(root, "session/cancel"), [])

    def test_reopening_never_allocates_a_second_session(self):
        client, root = self.session()
        client.open()
        # Either idempotent open or explicit rejection preserves the contract.
        try:
            client.open()
        except AcpUnavailable:
            pass
        client.close()
        self.assertEqual(len(self.requests(root, "session/new")), 1)

    def test_default_permission_is_cancelled_and_unknown_request_is_method_not_found(self):
        client, root = self.session("permission")
        client.open()
        self.assertEqual(client.prompt("permissions").stop_reason, "end_turn")
        replies = {row["id"]: row for row in self.wire(root) if "method" not in row and "id" in row}
        self.assertEqual(replies["permission-1"]["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(replies["unknown-1"]["error"]["code"], -32601)
        self.assertTrue(all(row["jsonrpc"] == "2.0" for row in replies.values()))

    def test_callback_responses_are_returned_once_for_permission_and_other_requests(self):
        calls = []

        def on_request(method, params):
            calls.append((method, params))
            if method == "session/request_permission":
                return {"outcome": {"outcome": "selected", "optionId": "allow"}}
            return {"echo": params["value"]}

        client, root = self.session("callback", on_request=on_request)
        client.open()
        client.prompt("permission callback")
        self.assertEqual([call[0] for call in calls],
                         ["session/request_permission", "fixture/unsupported"])
        self.assertEqual(calls[0][1]["sessionId"], SID)
        replies = {row["id"]: row for row in self.wire(root) if "method" not in row and "id" in row}
        self.assertEqual(replies["permission-1"]["result"],
                         {"outcome": {"outcome": "selected", "optionId": "allow"}})
        self.assertEqual(replies["unknown-1"]["result"], {"echo": 7})

    def test_callback_failure_is_sanitized_and_never_retried(self):
        calls = []
        secret = "fixture-private-token-do-not-disclose"

        def on_request(method, params):
            calls.append(method)
            raise RuntimeError(secret)

        client, root = self.session("callback-error", on_request=on_request)
        client.open()
        result = client.prompt("callback exception")
        self.assertEqual(result.stop_reason, "end_turn")
        replies = [row for row in self.wire(root) if "method" not in row and "id" in row]
        self.assertEqual(len(replies), 2)
        self.assertEqual(calls, ["session/request_permission", "fixture/unsupported"])
        for reply in replies:
            self.assertIn("error", reply)
            self.assertIsInstance(reply["error"]["code"], int)
            self.assertNotIn(secret, json.dumps(reply))
            self.assertNotIn("Traceback", json.dumps(reply))

    def test_tool_calls_notify_but_do_not_execute_or_pollute_text(self):
        client, root = self.session("tool")
        client.open()
        updates = []
        result = client.prompt("tool", on_update=updates.append)
        client.close()
        self.assertEqual(result.text, "\u4f60\u597d\uff0c\u4e16\u754c")
        self.assertEqual(len([row for row in updates if row["sessionUpdate"] == "tool_call"]), 1)
        self.assertFalse((root / "must-not-execute").exists())

    def test_matching_early_updates_replay_after_session_confirmation_without_thoughts(self):
        client, _ = self.session("early-update")
        startup = []

        def on_startup(update):
            self.assertEqual(client.session_id, SID)
            startup.append(update)

        client.open(on_update=on_startup)
        self.assertEqual([row["content"]["text"] for row in startup], ["early:"])
        updates = []
        result = client.prompt("early updates", on_update=updates.append)
        self.assertEqual(result.text, "\u4f60\u597d\uff0c\u4e16\u754c")
        self.assertEqual([row["content"]["text"] for row in updates],
                         ["\u4f60\u597d\uff0c", "\u4e16\u754c"])
        self.assertNotIn(THOUGHT, repr(result))
        self.assertNotIn(THOUGHT, repr(updates))

    def test_early_update_with_wrong_session_fails_open(self):
        client, root = self.session("early-wrong-session")
        updates = []
        with self.assertRaises(AcpProtocolError) as caught:
            client.open(on_update=updates.append)
        self.assertEqual(updates, [])
        self.assertIs(caught.exception.prompt_may_have_run, False)
        self.assert_terminal(client)
        self.assertEqual(self.requests(root, "session/prompt"), [])

    def test_wrong_response_id_and_wrong_update_session_are_terminal(self):
        for mode in ("wrong-id", "wrong-session"):
            with self.subTest(mode=mode):
                client, root = self.session(mode)
                client.open()
                updates = []
                with self.assertRaises(AcpProtocolError):
                    client.prompt("invalid peer", on_update=updates.append)
                self.assertEqual(updates, [])
                self.assert_terminal(client)
                self.assertEqual(len(self.requests(root, "session/prompt")), 1)

    def test_stdout_requires_strict_bounded_utf8_json_object_frames(self):
        for kind in ("json", "utf8", "array", "duplicate", "nan", "infinity", "oversized"):
            with self.subTest(kind=kind):
                client, root = self.session("bad-frame:" + kind)
                client.open()
                with self.assertRaises(AcpProtocolError):
                    client.prompt("bad frame")
                self.assert_terminal(client)
                self.assertEqual(len(self.requests(root, "session/prompt")), 1)

    def test_disconnect_after_prompt_never_replays_and_reports_uncertain_execution(self):
        client, root = self.session("disconnect")
        client.open()
        with self.assertRaises(AcpUnavailable) as caught:
            client.prompt("effect may have happened")
        self.assertIs(caught.exception.prompt_may_have_run, True)
        self.assert_terminal(client)
        self.assertEqual(len(self.requests(root, "session/prompt")), 1)
        self.assertEqual(sum(row.get("event") == "started" for row in self.wire(root)), 1)

    def test_disconnect_during_initialize_has_no_prompt_uncertainty(self):
        client, root = self.session("initialize-disconnect")
        with self.assertRaises(AcpUnavailable) as caught:
            client.open()
        self.assertIs(caught.exception.prompt_may_have_run, False)
        self.assert_terminal(client)
        self.assertEqual(self.requests(root, "session/prompt"), [])

    def test_each_rpc_times_out_and_only_sent_prompt_is_uncertain(self):
        for mode in ("initialize-timeout", "new-timeout", "timeout"):
            with self.subTest(mode=mode):
                client, root = self.session(mode, timeout=0.75)
                if mode == "timeout":
                    client.open()
                started = time.monotonic()
                with self.assertRaises(AcpUnavailable) as caught:
                    if mode == "timeout":
                        client.prompt("timeout")
                    else:
                        client.open()
                self.assertLess(time.monotonic() - started, 4)
                self.assertEqual(failure_code(caught.exception), "deadline_exceeded")
                self.assertIs(caught.exception.prompt_may_have_run, mode == "timeout")
                self.assert_terminal(client)
                self.assertEqual(len(self.requests(root, "session/prompt")), int(mode == "timeout"))

    def test_failure_classification_preserves_uncertainty_without_private_text(self):
        for mode, code in (("disconnect", "process_exited"), ("reject", "request_rejected")):
            with self.subTest(mode=mode):
                client, root = self.session(mode)
                client.open()
                with self.assertRaises(AcpUnavailable) as caught:
                    client.prompt("Only one attempt")
                self.assertEqual(failure_code(caught.exception), code)
                self.assertTrue(caught.exception.prompt_may_have_run)
                self.assertNotIn("PRIVATE_CREDENTIAL", str(caught.exception))
                self.assertEqual(len(self.requests(root, "session/prompt")), 1)
                self.assert_terminal(client)

    def test_malformed_errors_are_protocol_failures_not_rejections(self):
        for index in range(5):
            with self.subTest(index=index):
                client, root = self.session(f"bad-error:{index}")
                client.open()
                with self.assertRaises(AcpProtocolError) as caught:
                    client.prompt("Only one attempt")
                self.assertEqual(failure_code(caught.exception), "protocol_error")
                self.assertTrue(caught.exception.prompt_may_have_run)
                self.assertNotIn("PRIVATE_CREDENTIAL", str(caught.exception))
                self.assertEqual(len(self.requests(root, "session/prompt")), 1)
                self.assert_terminal(client)

    def test_cancel_from_other_thread_is_notification_and_not_prompt_completion(self):
        client, root = self.session("cancel")
        client.open()
        worker, cancelled, done, outcomes = self.start_prompt(client)
        self.assertTrue(client.cancel())
        self.assertTrue(cancelled.wait(3), "peer did not observe cancel")
        self.assertFalse(done.is_set(), "cancel must not synthesize prompt completion")
        (root / "release").touch()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], AcpPromptResult)
        self.assertEqual(outcomes[0].stop_reason, "cancelled")
        self.assertFalse(client.cancel())
        rows = self.requests(root, "session/cancel")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("id", rows[0])
        self.assertEqual(rows[0]["params"]["sessionId"], SID)

    def test_cancel_waiting_for_turn_a_cannot_cancel_turn_b(self):
        client, root = self.session("turn-race")
        client.open()
        probe = CancelLockProbe(client._turn_lock, delay=True)
        replacement = patch.object(client, "_turn_lock", probe)
        replacement.start()
        self.addCleanup(replacement.stop)
        a_worker, _, _, a_outcomes = self.start_prompt(client)
        cancel_worker, cancel_outcomes = self.background(
            client, client.cancel, name="fixture-cancel", releases=(probe.release,))
        self.assertTrue(probe.waiting.wait(3), "cancel did not reach turn lock")
        (root / "release-a").touch()
        self.assertEqual(self.join_background(a_worker, a_outcomes).stop_reason, "end_turn")

        b_worker, b_cancelled, b_done, b_outcomes = self.start_prompt(client)
        probe.release.set()
        self.assertIs(self.join_background(cancel_worker, cancel_outcomes), False)
        self.assertFalse(b_done.is_set())
        self.assertFalse(b_cancelled.is_set())
        self.assertEqual(self.requests(root, "session/cancel"), [])

        self.assertTrue(client.cancel())
        self.assertTrue(b_cancelled.wait(3))
        (root / "release").touch()
        self.assertEqual(self.join_background(b_worker, b_outcomes).stop_reason, "cancelled")
        self.assertEqual(len(self.requests(root, "session/prompt")), 2)
        self.assertEqual(len(self.requests(root, "session/cancel")), 1)

    def test_cancel_cannot_be_sent_before_its_prompt(self):
        client, root = self.session("cancel")
        client.open()
        at_send = threading.Event()
        allow_send = threading.Event()
        cancelled = threading.Event()
        probe = CancelLockProbe(client._turn_lock)
        original_send = client._send

        def gated_send(value, deadline):
            if value.get("method") == "session/prompt":
                at_send.set()
                if not allow_send.wait(4):
                    raise AssertionError("test did not release prompt send")
            return original_send(value, deadline)

        def on_update(value):
            if value.get("content", {}).get("text") == "cancel-observed":
                cancelled.set()

        lock_patch = patch.object(client, "_turn_lock", probe)
        send_patch = patch.object(client, "_send", gated_send)
        lock_patch.start()
        send_patch.start()
        self.addCleanup(lock_patch.stop)
        self.addCleanup(send_patch.stop)
        prompt_worker, prompt_outcomes = self.background(
            client, lambda: client.prompt("send first", on_update=on_update),
            releases=(allow_send,))
        self.assertTrue(at_send.wait(3))
        cancel_worker, cancel_outcomes = self.background(
            client, client.cancel, name="fixture-cancel", releases=(allow_send,))
        self.assertTrue(probe.waiting.wait(3))
        self.assertEqual(self.requests(root, "session/prompt"), [])
        self.assertEqual(self.requests(root, "session/cancel"), [])
        allow_send.set()
        self.assertIs(self.join_background(cancel_worker, cancel_outcomes), True)
        self.assertTrue(cancelled.wait(3))
        (root / "release").touch()
        self.assertEqual(self.join_background(prompt_worker, prompt_outcomes).stop_reason, "cancelled")
        self.assertEqual([row["method"] for row in self.wire(root) if "method" in row],
                         ["initialize", "session/new", "session/prompt", "session/cancel"])

    def test_cancel_during_permission_callback_overrides_late_approval(self):
        entered = threading.Event()
        finish_callback = threading.Event()
        calls = []

        def on_request(method, params):
            calls.append(method)
            entered.set()
            if not finish_callback.wait(4):
                raise AssertionError("test did not release permission callback")
            return {"outcome": {"outcome": "selected", "optionId": "allow"}}

        client, root = self.session("permission-cancel", on_request=on_request)
        client.open()
        worker, outcomes = self.background(
            client, lambda: client.prompt("permission"), releases=(finish_callback,))
        self.assertTrue(entered.wait(3))
        self.assertTrue(client.cancel())
        finish_callback.set()
        self.assertEqual(self.join_background(worker, outcomes).stop_reason, "cancelled")
        wire = self.wire(root)
        replies = [row for row in wire if row.get("id") == "permission-1"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertNotIn("error", replies[0])
        self.assertEqual(calls, ["session/request_permission"])
        cancel = self.requests(root, "session/cancel")
        self.assertEqual(len(cancel), 1)
        self.assertLess(wire.index(cancel[0]), wire.index(replies[0]))

    def test_thought_update_with_id_is_rejected_before_any_business_callback(self):
        requests = []
        updates = []

        def on_request(method, params):
            requests.append((method, params))
            return {}

        client, _ = self.session("thought-request", on_request=on_request)
        client.open()
        with self.assertRaises(AcpProtocolError) as caught:
            client.prompt("private thoughts", on_update=updates.append)
        self.assertEqual(requests, [])
        self.assertEqual(updates, [])
        self.assertNotIn(THOUGHT, str(caught.exception))
        self.assert_terminal(client)

    def test_concurrent_prompt_rejected_immediately_without_disturbing_active_prompt(self):
        client, root = self.session("hold")
        client.open()
        worker, cancelled, done, outcomes = self.start_prompt(client)
        started = time.monotonic()
        with self.assertRaises(AcpUnavailable) as caught:
            client.prompt("must not reach peer")
        self.assertLess(time.monotonic() - started, 1)
        self.assertIs(caught.exception.prompt_may_have_run, False)
        self.assertFalse(done.is_set())
        self.assertTrue(client.cancel())
        self.assertTrue(cancelled.wait(3))
        (root / "release").touch()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(outcomes[0], AcpPromptResult)
        self.assertEqual(len(self.requests(root, "session/prompt")), 1)

    def test_prompt_callback_cannot_reenter_prompt(self):
        client, root = self.session()
        client.open()
        errors = []

        def on_update(update):
            try:
                client.prompt("reentrant")
            except AcpUnavailable as exc:
                errors.append(exc)

        result = client.prompt("outer", on_update=on_update)
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(exc.prompt_may_have_run is False for exc in errors))
        self.assertEqual(len(self.requests(root, "session/prompt")), 1)

    def test_close_interrupts_active_prompt_and_is_repeatable(self):
        client, _ = self.session("hold")
        client.open()
        worker, _, done, outcomes = self.start_prompt(client)
        client.close()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(done.is_set())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], AcpUnavailable)
        self.assertIs(outcomes[0].prompt_may_have_run, True)
        self.assert_terminal(client)

    def test_close_terminates_pipe_holding_descendant(self):
        client, root = self.session("tree")
        client.open()
        port = int((root / "child-port").read_text())

        def child_alive():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return True
            except OSError:
                return False

        self.assertTrue(child_alive())
        started = time.monotonic()
        client.close()
        self.assertLess(time.monotonic() - started, 3)
        deadline = time.monotonic() + 3
        while child_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(child_alive(), "close left the fixture descendant alive")
        self.assert_terminal(client)

    def test_close_before_open_is_terminal(self):
        client, root = self.session()
        client.close()
        self.assert_terminal(client)
        self.assertEqual(self.wire(root), [])

    def test_invalid_constructor_parameters_are_rejected_without_starting_peer(self):
        valid = [sys.executable, "-u", str(PEER), "normal", str(self.root)]
        cases = [
            {"command": []}, {"command": "python"}, {"command": [1]},
            {"command": [""]}, {"command": ["bad\0command"]},
            {"timeout": 0}, {"timeout": -1}, {"timeout": float("nan")},
            {"timeout": float("inf")}, {"timeout": "one"},
            {"cwd": None}, {"env": []}, {"env": {"x": 1}},
            {"env": {"bad=key": "value"}}, {"env": {"x": "bad\0value"}},
            {"on_request": 42},
        ]
        for override in cases:
            with self.subTest(override=override):
                options = {"command": valid, "cwd": self.root, "timeout": 5, **override}
                with self.assertRaises((TypeError, ValueError, AcpError)):
                    client = AcpSession(**options)
                    self.addCleanup(client.close)
        self.assertEqual(self.wire(self.root), [])


if __name__ == "__main__":
    unittest.main()
