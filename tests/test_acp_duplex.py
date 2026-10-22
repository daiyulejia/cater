"""Public ACP calls over real pipes, with IPC gates and empty private chunks.

The fixture is composed with this suite, never inherited from existing tests.
Private attributes are read only to inspect process/thread/pipe cleanup.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import unittest

from cater.acp import AcpError, AcpProtocolError, AcpSession, AcpTimeout, AcpUnavailable
from cater.mcp_host import McpToolBridge


PEER = Path(__file__).resolve().parent / "fixtures" / "acp_duplex_peer.py"
SID = "duplex-synthetic-session"
WAIT = 10


def large_prompt():
    unit = "\u6d4b\u8bd5\U0001f680\n\"\\\r\t"
    size = 70 * 1024
    count, rest = divmod(size, len(unit.encode("utf-8")))
    return unit * count + "x" * rest


def process_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return False
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # Job termination is asynchronous: wait on process exit, not a PID poll.
        status = kernel.WaitForSingleObject(handle, 3000)
        if status not in (0, 258):
            raise ctypes.WinError(ctypes.get_last_error())
        return status == 258
    finally:
        kernel.CloseHandle(handle)


class PauseSignal(BaseException):
    """A host control exception must keep its identity through bridge and ACP."""


class PipeCase:
    def __init__(self, test, mode="normal", *, timeout=6, on_request=None, bridge=None):
        self.test = test
        self.mode = mode
        self.bridge = bridge
        self.events = []
        self.condition = threading.Condition()
        self.eof = threading.Event()
        self.control = None
        self.reader = None
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(WAIT)
        self.send_lock = threading.Lock()
        self.owner = threading.get_ident()
        self.service_threads = []
        self.callback_threads = []
        self.prefix_ack = False
        self.client = AcpSession(
            [sys.executable, "-B", "-u", str(PEER), mode, str(self.listener.getsockname()[1])],
            cwd=PEER.parent, timeout=timeout, on_request=on_request,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if not hasattr(test, "duplex_cases"):
            test.duplex_cases = []
        test.duplex_cases.append(self)
        test.addCleanup(self.close)

    def read_control(self):
        try:
            with self.control.makefile("rb") as stream:
                for line in stream:
                    value = json.loads(line)
                    with self.condition:
                        self.events.append(value)
                        self.condition.notify_all()
        except OSError:
            pass
        finally:
            self.eof.set()
            with self.condition:
                self.condition.notify_all()

    def seen(self, name):
        with self.condition:
            return [row for row in self.events if row["event"] == name]

    def wait(self, name, *, count=1, timeout=WAIT):
        with self.condition:
            ready = self.condition.wait_for(
                lambda: len(self.seen(name)) >= count or self.eof.is_set(), timeout=timeout)
            rows = self.seen(name)
        self.test.assertTrue(ready and len(rows) >= count, "missing IPC event: " + name)
        return rows[count - 1]

    def command(self, name):
        with self.send_lock:
            self.control.sendall(json.dumps({"command": name}).encode("ascii") + b"\n")

    def open(self, *, on_update=None, defaults=False):
        options = {} if defaults else {"service": self.service}
        if self.bridge is not None:
            options["mcp_servers"] = [self.bridge.server_config()]
        if on_update is not None:
            options["on_update"] = on_update
        opened = self.client.open(**options)
        self.control, _ = self.listener.accept()
        self.control.settimeout(None)
        self.reader = threading.Thread(target=self.read_control, name="duplex-control", daemon=True)
        self.reader.start()
        self.wait("opened")
        self.test.assertEqual(opened, {"sessionId": SID})
        self.test.assertEqual(self.client.session_id, SID)
        return opened

    def service(self):
        self.service_threads.append(threading.get_ident())
        if self.bridge is not None:
            self.bridge.pump(0.001)

    def update(self, body):
        self.callback_threads.append(threading.get_ident())
        return body.get("content", {}).get("text")

    def prompt(self, *, service=None, on_update=None, text=None, timeout=None):
        self.command("go")
        # A control helper acknowledges prefix independently of the transport.
        # Service-specific assertions can therefore distinguish baseline input timeout.
        def acknowledge():
            with self.condition:
                ready = self.condition.wait_for(
                    lambda: bool(self.seen("input-prefix")) or self.eof.is_set(), timeout=WAIT)
            if ready and not self.eof.is_set():
                with self.send_lock:
                    if not self.prefix_ack:
                        self.prefix_ack = True
                        self.control.sendall(b'{"command":"prefix-ack"}\n')

        helper = threading.Thread(target=acknowledge, name="duplex-prefix", daemon=True)
        helper.start()
        try:
            return self.client.prompt(large_prompt() if text is None else text,
                                      service=self.service if service is None else service,
                                      on_update=on_update, timeout=timeout)
        finally:
            helper.join(WAIT)
            self.test.assertFalse(helper.is_alive(), "prefix helper did not stop")

    def close(self):
        try:
            self.client.close()
        finally:
            if self.bridge is not None:
                self.bridge.close()
            if self.control is not None:
                try:
                    self.control.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.control.close()
            self.listener.close()
            if self.reader is not None:
                self.reader.join(WAIT)

    def assert_clean(self, *, terminal=True):
        self.client.close()
        self.client.close()
        self.test.assertTrue(self.eof.wait(WAIT), "peer control socket survived process cleanup")
        proc = self.client._proc
        self.test.assertIsNotNone(proc.poll(), "driver process survived cleanup")
        self.test.assertFalse(self.client._reader.is_alive(), "ACP reader survived cleanup")
        self.test.assertTrue(all(not writer.is_alive() for writer in self.client._writers))
        self.test.assertTrue(proc.stdin.closed and proc.stdout.closed)
        self.reader.join(WAIT)
        self.test.assertFalse(self.reader.is_alive())
        for event in self.seen("peer-started") + self.seen("relay-started"):
            self.test.assertFalse(process_alive(event["pid"]), "fixture descendant survived cleanup")
        self.test.assertEqual(self.seen("peer-error"), [])
        if self.bridge is not None:
            self.bridge.close()
            self.test.assertEqual(self.bridge._listener.fileno(), -1)
            if self.bridge._connection is not None:
                self.test.assertEqual(self.bridge._connection.fileno(), -1)
        if terminal:
            with self.test.assertRaises(AcpUnavailable):
                self.client.prompt("must never replay")
            with self.test.assertRaises(AcpUnavailable):
                self.client.open()
            self.test.assertFalse(self.client.cancel())

    def assert_exact(self, text, *, count=1):
        row = self.wait("prompt", count=count)
        raw = text.encode("utf-8")
        self.test.assertEqual(row["bytes"], len(raw))
        self.test.assertEqual(row["sha256"], hashlib.sha256(raw).hexdigest())


class AcpDuplexTests(unittest.TestCase):
    def bridge(self, execute):
        bridge = McpToolBridge([{"name": "memory_echo", "description": "Synthetic memory tool",
                                 "input_schema": {"type": "object"}}], execute, timeout=8)
        self.addCleanup(bridge.close)
        bridge.activate()
        return bridge

    def test_large_input_services_actual_mcp_before_drain_on_owner(self):
        executions, requests = [], []

        def execute(call):
            executions.append((threading.get_ident(), call.name, call.arguments))
            return {"status": "ok", "value": 7}

        def request(method, params):
            requests.append((threading.get_ident(), method, params))
            return {"ok": True}

        case = PipeCase(self, "mcp", on_request=request, bridge=self.bridge(execute))
        case.open()
        updates = []
        result = case.prompt(on_update=lambda body: updates.append(case.update(body)))
        self.assertEqual((result.text, result.stop_reason), ("ok", "end_turn"))
        case.wait("owner-request-complete")
        self.assertEqual([row["method"] for row in case.seen("mcp-response")],
                         ["initialize", "tools/list", "ping", "tools/call"])
        self.assertEqual(executions, [(case.owner, "memory_echo", {"value": 7})])
        self.assertEqual(requests, [(case.owner, "fixture/owner", {"sessionId": SID})])
        self.assertEqual(updates, ["ok"])
        self.assertEqual(set(case.service_threads + case.callback_threads), {case.owner})
        self.assertLess(case.events.index(case.seen("mcp-complete-before-drain")[0]),
                        case.events.index(case.seen("prompt")[0]))
        case.assert_exact(large_prompt())
        case.assert_clean()

    def test_512_empty_private_notifications_before_input_drain(self):
        case = PipeCase(self, "private")
        case.open()
        updates = []
        result = case.prompt(on_update=lambda body: updates.append(case.update(body)))
        self.assertEqual(result.text, "ok")
        self.assertEqual(updates, ["ok"])
        self.assertEqual(case.wait("private-complete")["count"], 512)
        self.assertEqual(case.callback_threads, [case.owner])
        case.assert_exact(large_prompt())
        case.assert_clean()

    def test_more_than_128_public_updates_are_ordered_on_owner_during_write(self):
        case = PipeCase(self, "public")
        case.open()
        updates, reentrant_cancel = [], []

        def update(body):
            text = case.update(body)
            updates.append(text)
            if text != "ok" and (int(text) + 1) % 32 == 0:
                reentrant_cancel.append(case.client.cancel())
                case.command("public-" + str(int(text) + 1))

        result = case.prompt(on_update=update)
        self.assertEqual(updates, [str(index) for index in range(384)] + ["ok"])
        self.assertEqual(result.text, "".join(updates))
        self.assertEqual(reentrant_cancel, [False] * 12)
        self.assertEqual(set(case.callback_threads + case.service_threads), {case.owner})
        self.assertEqual(case.wait("public-complete")["count"], 384)
        case.assert_exact(large_prompt())
        case.assert_clean()

    def test_unicode_prompt_bytes_are_exact_across_two_turns(self):
        case = PipeCase(self)
        case.open()
        text = large_prompt()
        self.assertEqual(len(text.encode("utf-8")), 71680)
        self.assertEqual(case.prompt(text=text).text, "ok")
        case.assert_exact(text)
        second = " \u96ea\U0001f680\r\n\"\\\t "
        self.assertEqual(case.client.prompt(second, service=case.service).text, "ok")
        case.assert_exact(second, count=2)
        self.assertEqual(len(case.seen("session-new")), 1)
        case.assert_clean()

    def test_midwrite_service_exception_is_preserved_without_replay(self):
        case = PipeCase(self, "service-failure")
        case.open()
        failure = RuntimeError("synthetic service failure")
        calls = []

        def service():
            case.service()
            if case.seen("holding-input"):
                calls.append(threading.get_ident())
                raise failure

        with self.assertRaises(RuntimeError) as caught:
            case.prompt(service=service)
        self.assertIs(caught.exception, failure)
        self.assertEqual(calls, [case.owner])
        case.assert_clean()
        self.assertEqual(case.seen("prompt"), [])
        self.assertEqual(len(case.seen("input-prefix")), 1)
        self.assertEqual(len(case.seen("session-new")), 1)

    def test_midwrite_bridge_pause_baseexception_is_preserved_without_replay(self):
        failure = PauseSignal("synthetic pause")
        executions = []

        def execute(call):
            executions.append(threading.get_ident())
            raise failure

        case = PipeCase(self, "pause", bridge=self.bridge(execute))
        case.open()
        with self.assertRaises(PauseSignal) as caught:
            case.prompt()
        self.assertIs(caught.exception, failure)
        self.assertEqual(executions, [case.owner])
        case.assert_clean()
        self.assertEqual(case.seen("prompt"), [])
        self.assertEqual(len(case.seen("input-prefix")), 1)
        self.assertEqual(len(case.seen("session-new")), 1)

    def deadline_stream(self, private):
        case = PipeCase(self, "deadline-private" if private else "deadline-public", timeout=5)
        case.open()
        updates = []
        next_count = 0
        hard_stop = threading.Event()
        watchdog_fired = []

        def service():
            nonlocal next_count
            case.service()
            if case.seen("deadline-stream-started"):
                frames = case.seen("deadline-frame")
                if len(frames) >= next_count:
                    next_count += 1
                    case.command("tick")

        def watchdog():
            if not hard_stop.wait(WAIT):
                watchdog_fired.append(True)
                case.client.close()

        watcher = threading.Thread(target=watchdog, name="duplex-deadline-guard", daemon=True)
        watcher.start()
        try:
            with self.assertRaises(AcpTimeout) as caught:
                case.prompt(service=service, on_update=lambda body: updates.append(case.update(body)), timeout=1.5)
            self.assertEqual(caught.exception.phase, "input")
            self.assertTrue(caught.exception.prompt_may_have_run)
        finally:
            hard_stop.set()
            watcher.join(WAIT)
        self.assertFalse(watcher.is_alive())
        self.assertEqual(watchdog_fired, [], "stream renewed deadline until external guard closed session")
        self.assertGreater(len(case.seen("deadline-frame")), 5)
        if private:
            self.assertEqual(updates, [])
        else:
            self.assertGreater(len(updates), 5)
            self.assertEqual(updates, [str(index) for index in range(len(updates))])
            self.assertEqual(set(case.callback_threads), {case.owner})
        case.assert_clean()
        self.assertEqual(case.seen("prompt"), [])

    def test_input_deadline_not_renewed_by_empty_private_frames(self):
        self.deadline_stream(True)

    def test_input_deadline_not_renewed_by_public_frames(self):
        self.deadline_stream(False)

    def test_invalid_notifications_never_reach_update_or_request_callbacks(self):
        for mode in ("bad-envelope", "bad-shape", "bad-wrong-public", "bad-wrong-private", "bad-thought-id"):
            with self.subTest(mode=mode):
                updates, requests = [], []
                case = PipeCase(self, mode, timeout=2,
                                on_request=lambda *args: requests.append(args) or {})
                case.open()
                with self.assertRaises(AcpProtocolError) as caught:
                    case.prompt(on_update=updates.append)
                self.assertTrue(caught.exception.prompt_may_have_run)
                self.assertEqual(updates, [])
                self.assertEqual(requests, [])
                self.assertEqual(len(case.seen("invalid-sent")), 1)
                case.assert_clean()

    def test_default_and_startup_session_new_still_work(self):
        for mode in ("normal", "startup"):
            with self.subTest(mode=mode):
                case = PipeCase(self, mode)
                updates = []
                case.open(defaults=True, on_update=lambda body: updates.append(case.update(body)))
                self.assertEqual(updates, ["startup"] if mode == "startup" else [])
                self.assertEqual(case.wait("initialize")["capabilities"], {})
                self.assertEqual(case.wait("session-new")["servers"], 0)
                self.assertEqual(Path(case.wait("session-new")["cwd"]), PEER.parent)
                self.assertFalse(case.client.cancel())
                self.assertEqual(case.prompt().stop_reason, "end_turn")
                self.assertFalse(case.client.cancel())
                self.assertEqual(len(case.seen("session-new")), 1)
                case.assert_clean()

    def test_invalid_response_ids_fail_during_input(self):
        for suffix in ("null", "bool", "unknown"):
            with self.subTest(identifier=suffix):
                case = PipeCase(self, "invalid-id-" + suffix, timeout=2)
                case.open()
                updates = []
                with self.assertRaises(AcpProtocolError) as caught:
                    case.prompt(on_update=updates.append)
                self.assertTrue(caught.exception.prompt_may_have_run)
                self.assertEqual(updates, [])
                self.assertEqual(len(case.seen("invalid-sent")), 1)
                case.assert_clean()

    def test_close_cancel_race_and_owner_callback_close_are_bounded(self):
        for iteration in range(3):
            with self.subTest(race=iteration):
                case = PipeCase(self, "race", timeout=3)
                case.open()
                barrier = threading.Barrier(2, timeout=WAIT)
                outcomes = []

                def compete(cancel):
                    try:
                        case.wait("holding-input")
                        barrier.wait()
                        outcomes.append(case.client.cancel() if cancel else case.client.close())
                    except BaseException as error:
                        outcomes.append(error)

                racers = [threading.Thread(target=compete, args=(cancel,), daemon=True,
                                            name="duplex-race-" + str(cancel)) for cancel in (True, False)]
                for racer in racers:
                    racer.start()
                try:
                    with self.assertRaises(AcpUnavailable):
                        case.prompt()
                finally:
                    for racer in racers:
                        racer.join(WAIT)
                self.assertTrue(all(not racer.is_alive() for racer in racers))
                self.assertEqual(len(outcomes), 2)
                self.assertTrue(all(value is None or type(value) is bool or isinstance(value, AcpError)
                                    for value in outcomes), repr(outcomes))
                case.assert_clean()
                self.assertEqual(case.seen("prompt"), [])

        case = PipeCase(self, "callback-close", timeout=3)
        case.open()
        callbacks = []

        def close_from_owner(body):
            callbacks.append((threading.get_ident(), case.update(body), case.client.cancel()))
            case.client.close()

        with self.assertRaises(AcpUnavailable):
            case.prompt(on_update=close_from_owner)
        self.assertEqual(callbacks, [(case.owner, "close-now", False)])
        case.assert_clean()


if __name__ == "__main__":
    unittest.main()
