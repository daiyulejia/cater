"""Frozen duplex boundary regressions; memory-only transport, no external process.

Private transport seams schedule incoming frames and attach a fake pipe. The
assertions concern public results, callbacks, wire attempts and resource cleanup.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import queue
import threading
import unittest
from unittest.mock import Mock, patch

from cater.acp import (
    AcpProtocolError, AcpRequestRejected, AcpSession, AcpTimeout, AcpUnavailable,
    MAX_FRAME_BYTES, rejection_details,
)
from cater.external_agent import ExternalRunLimit, QoderAcpDriver


SID = "boundary-memory-session"
WAIT = 3
PRIVATE_ERROR = "SYNTHETIC_SERVER_ERROR_MUST_NOT_ESCAPE"


def frame(value):
    return json.dumps(value, ensure_ascii=True).encode("ascii") + b"\n"


def result(identifier="cater-1"):
    return {"jsonrpc": "2.0", "id": identifier, "result": {"stopReason": "end_turn"}}


def update(text="public", *, private=False):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": SID, "update": {
            "sessionUpdate": "agent_thought_chunk" if private else "agent_message_chunk",
            "content": {"type": "text", "text": "" if private else text},
        },
    }}


def request(identifier="request-1"):
    return {"jsonrpc": "2.0", "id": identifier, "method": "fixture/noop",
            "params": {"sessionId": SID}}


class MemoryInput:
    def __init__(self, *, blocked=False, reject_cancel=False):
        self.started = threading.Event()
        self.release = threading.Event()
        self.killed = threading.Event()
        self.closed = False
        self.reject_cancel = reject_cancel
        self.attempts = []
        self.threads = []
        self.lock = threading.Lock()
        if not blocked:
            self.release.set()

    def write(self, data):
        message = json.loads(data)
        with self.lock:
            self.attempts.append(message)
            self.threads.append(threading.current_thread())
        self.started.set()
        if self.reject_cancel and message.get("method") == "session/cancel":
            raise OSError("synthetic cancel writer must not have started")
        if not self.release.wait(WAIT):
            raise OSError("synthetic pipe watchdog expired")
        if self.killed.is_set() or self.closed:
            raise OSError("synthetic process closed its pipe")
        return len(data)

    def flush(self):
        if self.killed.is_set() or self.closed:
            raise OSError("synthetic process closed its pipe")

    def close(self):
        self.closed = True
        self.release.set()


class MemoryProcess:
    def __init__(self, pipe):
        self.stdin = pipe
        self.stdout = io.BytesIO()

    def poll(self):
        return 0 if self.stdin.killed.is_set() else None

    def kill(self):
        self.stdin.killed.set()
        self.stdin.release.set()

    def wait(self, timeout=None):
        if not self.stdin.killed.wait(WAIT if timeout is None else timeout):
            raise AssertionError("cleanup did not terminate the synthetic process")
        return 0


class MemoryBridge:
    def __init__(self, *args, **kwargs):
        self.revoked = False
        self.closed = False

    def server_config(self, *args):
        return {}

    def activate(self):
        pass

    def pump(self, *args):
        return False

    def revoke(self):
        self.revoked = True

    def close(self):
        self.revoke()
        self.closed = True


class AcpDuplexBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.launch = self.enterContext(patch(
            "subprocess.Popen", side_effect=AssertionError("No subprocess may start")))
        self.sessions = []
        self.processes = []
        self.addCleanup(self.check_cleanup)

    def check_cleanup(self):
        for session in self.sessions:
            session.close()
        for process in self.processes:
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdin.closed and process.stdout.closed)
            for thread in process.stdin.threads:
                thread.join(WAIT)
                self.assertFalse(thread.is_alive(), "synthetic writer survived cleanup")
        self.launch.assert_not_called()

    def session(self, *, on_request=None, pipe=None):
        session = AcpSession(["never-launch"], cwd=Path(__file__).resolve().parent,
                             timeout=2, on_request=on_request)
        session.session_id = SID
        if pipe is not None:
            process = MemoryProcess(pipe)
            session._proc = process
            self.processes.append(process)
        self.sessions.append(session)
        return session

    def terminal(self, session):
        self.assertFalse(session.cancel())
        with self.assertRaises(AcpUnavailable):
            session.prompt("must not replay")

    def staged(self, session, incoming, *, after_send=None):
        """Finish a deterministic write only after servicing its inbound frames."""
        outgoing = []

        def send(message, deadline, *, service=None):
            outgoing.append(message)
            if message.get("method") == "session/prompt":
                for item in incoming(message):
                    session._frames.put_nowait(frame(item))
                if service is not None:
                    service()
                if after_send is not None:
                    after_send()
            elif service is not None:
                service()

        self.enterContext(patch.object(session, "_send", send))
        return outgoing

    def external(self, *, pipe, timeout=2, total=None, incoming=(), send=None, tools=None):
        bridges = []
        execute = Mock(side_effect=AssertionError("No real tool may execute"))

        def bridge(*args, **kwargs):
            instance = MemoryBridge()
            bridges.append(instance)
            return instance

        def session_factory(command, **kwargs):
            session = self.session(on_request=kwargs["on_request"], pipe=pipe)
            session.timeout = kwargs["timeout"]
            session._on_activity = kwargs.get("on_activity")

            def opened(**options):
                session.agent_info = {"name": "qoder-cli-cn"}
                for item in incoming:
                    session._frames.put_nowait(item)
                return {"sessionId": SID, "models": {
                    "currentModelId": "memory-model",
                    "availableModels": [{"modelId": "memory-model", "name": "ExactModel"}],
                }}

            self.enterContext(patch.object(session, "open", opened))
            if send is not None:
                self.enterContext(patch.object(session, "_send", lambda *a, **kw: send(session, *a, **kw)))
            return session

        self.enterContext(patch("cater.external_agent.AcpSession", side_effect=session_factory))
        self.enterContext(patch("cater.external_agent.McpToolBridge", side_effect=bridge))
        driver = QoderAcpDriver("offline", "never-launch", "ExactModel",
                                timeout=timeout, max_run_seconds=total)
        updates = []
        run = driver.agent_run(cwd=Path(__file__).resolve().parent, tools=[] if tools is None else tools, execute=execute,
                               check_current=lambda: None, on_update=updates.append)
        return run, bridges, execute, updates

    def test_queue_empty_service_error_keeps_identity_and_cannot_succeed(self):
        pipe = MemoryInput(blocked=True)
        session = self.session(pipe=pipe)
        session._frames.put_nowait(frame(result()))
        marker = queue.Empty("synthetic host control error")
        raised = False
        owner = threading.get_ident()
        callback_threads = []

        def service():
            nonlocal raised
            callback_threads.append(threading.get_ident())
            if not raised:
                self.assertTrue(pipe.started.wait(WAIT))
                raised = True
                pipe.release.set()
                raise marker

        with self.assertRaises(queue.Empty) as caught:
            session.prompt("memory", service=service)
        self.assertIs(caught.exception, marker)
        self.assertEqual(callback_threads, [owner])
        self.terminal(session)

    def test_terminal_result_preserves_duplicate_for_next_turn_and_waits_for_write(self):
        with self.subTest(boundary="duplicate_response"):
            session = self.session()
            self.staged(session, lambda message: (
                [result(), result()] if message["id"] == "cater-1"
                else [result(message["id"])]))
            self.assertEqual(session.prompt("first").stop_reason, "end_turn")
            with self.assertRaises(AcpProtocolError) as caught:
                session.prompt("second")
            self.assertTrue(caught.exception.prompt_may_have_run)
            self.terminal(session)

        with self.subTest(boundary="unfinished_write"):
            session = self.session()
            marker = AcpUnavailable("synthetic input failure after early result")

            def failed():
                raise marker

            self.staged(session, lambda _: [result()], after_send=failed)
            with self.assertRaises(AcpUnavailable) as caught:
                session.prompt("memory")
            self.assertIs(caught.exception, marker)
            self.assertTrue(caught.exception.prompt_may_have_run)
            self.terminal(session)

    def test_public_update_after_terminal_is_delivered_once_on_next_receive(self):
        session = self.session()
        outgoing = self.staged(session, lambda message: (
            [result(), update("late-public")] if message["id"] == "cater-1"
            else [result(message["id"])]))
        first_updates, next_updates = [], []
        first = session.prompt("first", on_update=first_updates.append)
        second = session.prompt("second", on_update=next_updates.append)
        self.assertEqual((first.text, first.stop_reason), ("", "end_turn"))
        self.assertEqual(first_updates, [])
        self.assertEqual((second.text, second.stop_reason), ("late-public", "end_turn"))
        self.assertEqual([body["content"]["text"] for body in next_updates], ["late-public"])
        self.assertEqual([m["method"] for m in outgoing], ["session/prompt", "session/prompt"])

    def test_valid_rejection_follows_request_and_public_callbacks_in_wire_order(self):
        events = []
        owner = threading.get_ident()

        def on_request(method, params):
            self.assertEqual((method, params["sessionId"]), ("fixture/noop", SID))
            events.append(("request", threading.get_ident()))
            return {"ok": True}

        session = self.session(on_request=on_request)
        error = {"jsonrpc": "2.0", "id": "cater-1", "error": {
            "code": -32000, "message": PRIVATE_ERROR, "data": {"secret": PRIVATE_ERROR}}}
        outgoing = self.staged(session, lambda _: [request(), update(), error])
        with self.assertRaises(AcpRequestRejected) as caught:
            session.prompt("memory", on_update=lambda _: events.append(("public", threading.get_ident())))
        self.assertEqual(events, [("request", owner), ("public", owner)])
        replies = [message for message in outgoing if "method" not in message]
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": "request-1", "result": {"ok": True}}])
        self.assertEqual(rejection_details(caught.exception), {
            "external_rpc_code": -32000, "external_rpc_method": "session/prompt"})
        self.assertNotIn(PRIVATE_ERROR, str(caught.exception))
        self.assertNotIn(PRIVATE_ERROR, repr(vars(caught.exception)))
        self.assertNotIn(PRIVATE_ERROR, json.dumps(outgoing))
        self.assertTrue(caught.exception.prompt_may_have_run)
        self.terminal(session)

    def test_abandoned_write_aborts_without_starting_a_cancel_writer(self):
        pipe = MemoryInput(blocked=True, reject_cancel=True)
        run, bridges, execute, _ = self.external(pipe=pipe, incoming=[b"invalid-json\n"])
        with self.assertRaises(AcpProtocolError):
            run.run("memory")
        self.assertEqual([m["method"] for m in pipe.attempts], ["session/prompt"])
        self.assertTrue(run.state.prompt_attempted)
        self.assertTrue(run.state.cleanup_complete)
        self.assertEqual(run.state.cancel_failures, ())
        self.assertFalse(run.state.cancel_failed)
        self.assertEqual(run.state.phase, "failed")
        self.assertTrue(all(bridge.closed and bridge.revoked for bridge in bridges))
        execute.assert_not_called()
        self.terminal(self.sessions[-1])
        with self.assertRaises(RuntimeError):
            run.run("must not replay")

    def test_deferred_delivery_checks_close_and_absolute_deadline_around_service(self):
        for boundary in ("closed_before", "closed_by_service", "expired_before", "expired_after_service"):
            with self.subTest(boundary=boundary):
                session = self.session()
                now = [100.0]
                calls = []

                def service():
                    calls.append(now[0])
                    if len(calls) == 2:
                        if boundary == "closed_by_service":
                            session.close()
                        elif boundary == "expired_after_service":
                            now[0] = 105.0

                def after_send():
                    if boundary == "closed_before":
                        session.close()
                    elif boundary == "expired_before":
                        now[0] = 105.0

                self.staged(session, lambda _: [result()], after_send=after_send)
                expected = AcpUnavailable if boundary.startswith("closed") else AcpTimeout
                with patch("cater.acp.time.monotonic", lambda: now[0]), self.assertRaises(expected) as caught:
                    session.prompt("memory", service=service, timeout=5)
                self.assertTrue(caught.exception.prompt_may_have_run)
                if expected is AcpTimeout:
                    self.assertEqual(caught.exception.phase, "response")
                self.assertEqual(len(calls), 1 if boundary.endswith("before") else 2)
                self.terminal(session)

    def test_stream_activity_extends_idle_but_is_not_useful_work(self):
        now = [100.0]
        pipe = MemoryInput()
        write_finished = False

        def send(session, message, deadline, *, service=None):
            nonlocal write_finished
            if message.get("method") != "session/prompt":
                return
            now[0] = 101.0
            session._frames.put_nowait(frame(update()))
            session._frames.put_nowait(frame(update(private=True)))
            service()
            write_finished = True

        def pump(*args):
            if write_finished:
                now[0] += 1
            return False

        with patch("cater.acp.time.monotonic", lambda: now[0]), patch.object(MemoryBridge, "pump", pump):
            run, bridges, execute, updates = self.external(pipe=pipe, total=5, send=send)
            with self.assertRaises(ExternalRunLimit) as caught:
                run.run("memory")
        self.assertEqual(caught.exception.limit, "idle")
        self.assertEqual(now[0], 103.0)
        self.assertEqual([u["sessionUpdate"] for u in updates], ["agent_message_chunk"])
        self.assertEqual(run.state.text, "public")
        self.assertNotIn("progress_receipts", dict(run.state.observations))
        self.assertEqual(dict(run.state.observations)["last_activity_ms"], 1000)
        self.assertTrue(run.state.cleanup_complete)
        self.assertTrue(all(bridge.closed and bridge.revoked for bridge in bridges))
        execute.assert_not_called()

    def test_deferred_count_and_wire_byte_limits_are_independent(self):
        for boundary in ("count_exact", "count_over", "bytes_exact", "bytes_over"):
            with self.subTest(boundary=boundary):
                callbacks = []
                session = self.session(on_request=lambda *args: callbacks.append(args) or {})
                if boundary.startswith("count"):
                    count = 127 if boundary == "count_exact" else 128
                    incoming = [request("request-" + str(i)) for i in range(count)] + [result()]
                else:
                    message = request()
                    message["padding"] = ""
                    target = MAX_FRAME_BYTES - (len(frame(result())) - 1)
                    if boundary == "bytes_over":
                        target += 1
                    message["padding"] = "x" * (target - len(frame(message)) + 1)
                    self.assertEqual(len(frame(message)) - 1, target)
                    incoming = [message, result()]

                def send(message, deadline, *, service=None):
                    if message.get("method") == "session/prompt":
                        for item in incoming:
                            session._frames.put_nowait(frame(item))
                            service()
                    elif service is not None:
                        service()

                with patch.object(session, "_send", send):
                    if boundary.endswith("over"):
                        with self.assertRaises(AcpProtocolError) as caught:
                            session.prompt("memory")
                        self.assertTrue(caught.exception.prompt_may_have_run)
                        self.assertEqual(callbacks, [])
                        self.terminal(session)
                    else:
                        self.assertEqual(session.prompt("memory").stop_reason, "end_turn")
                        self.assertEqual(len(callbacks), 127 if boundary == "count_exact" else 1)


if __name__ == "__main__":
    unittest.main()
