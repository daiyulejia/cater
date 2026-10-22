"""Owner-thread cooperative MCP transport, using real loopback backpressure."""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

from cater import mcp_host
from cater.tool_protocol import ToolCall, json_utf8


class McpCooperativeTests(unittest.TestCase):
    def setUp(self):
        self.owner = threading.get_ident()
        self.calls = []

    def execute(self, call):
        self.assertEqual(threading.get_ident(), self.owner)
        self.calls.append(call)
        return {"status": "ok", "arguments": call.arguments}

    def bridge(self, execute=None, *, timeout=5):
        bridge = mcp_host.McpToolBridge([
            {"name": "record", "description": "Offline fixture", "input_schema": {"type": "object"}},
        ], execute or self.execute, timeout=timeout)
        self.addCleanup(bridge.close)
        return bridge

    @staticmethod
    def request(identifier, method="tools/call", **params):
        if method == "tools/call":
            params.setdefault("name", "record")
        return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}

    @staticmethod
    def send(peer, *values):
        peer.sendall(b"".join(json_utf8(value) + b"\n" for value in values))

    def poll_frame(self, bridge):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if bridge.poll():
                return
        self.fail("Queued loopback frame was not handled")

    def receive(self, peer):
        peer.settimeout(2)
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = peer.recv(65536)
            self.assertTrue(chunk, "Response ended prematurely")
            data.extend(chunk)
        self.assertEqual(data.count(b"\n"), 1)
        return bytes(data)

    def exchange(self, bridge, peer, value):
        self.send(peer, value)
        self.poll_frame(bridge)
        while bridge.has_pending_output:
            bridge.poll()
        return json.loads(self.receive(peer))

    def session(self, bridge):
        env = {row["name"]: row["value"] for row in bridge.server_config()["env"]}
        peer = socket.create_connection(("127.0.0.1", int(env["CATER_MCP_PORT"])), timeout=2)
        self.addCleanup(peer.close)
        self.assertEqual(self.exchange(bridge, peer, {"capability": env["CATER_MCP_CAPABILITY"]}),
                         {"authenticated": True})
        self.exchange(bridge, peer, self.request("init", "initialize", protocolVersion=mcp_host.VERSION))
        self.assertEqual(self.exchange(bridge, peer, {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }), {"response": None})
        bridge.activate()
        bridge._connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        return peer

    def large_response(self, *, terminal=False, timeout=5):
        receipt = {"status": "ok", "receipt": "original", "text": "x" * (2 * 1024 * 1024)}

        def execute(call):
            self.execute(call)
            if terminal:
                bridge.revoke()
            return receipt

        bridge = self.bridge(execute, timeout=timeout)
        peer = self.session(bridge)
        self.send(peer, self.request(1))
        self.poll_frame(bridge)
        self.assertTrue(bridge.has_pending_output)
        return bridge, peer, receipt

    def stall(self, bridge):
        """Keep the real peer unread until the TCP send buffer stops advancing."""
        pending = bridge._pending_output
        previous, unchanged = pending.offset, 0
        for _ in range(128):
            started = time.monotonic()
            self.assertFalse(bridge.poll())
            self.assertLess(time.monotonic() - started, 0.15)
            self.assertIs(bridge._pending_output, pending)
            self.assertTrue(bridge.has_pending_output)
            if pending.offset == previous:
                unchanged += 1
                if unchanged == 4:
                    return pending
            else:
                unchanged = 0
            previous = pending.offset
        self.fail("Real loopback fixture did not reach output backpressure")

    def drain_slowly(self, bridge, peer, expected):
        data = bytearray()
        peer.setblocking(False)
        deadline = time.monotonic() + 4
        steps = 0
        while bridge.has_pending_output:
            self.assertLess(time.monotonic(), deadline, "Response did not drain")
            self.assertFalse(bridge.poll())
            steps += 1
            # Read a small piece every few polls to sustain real backpressure.
            if steps % 4 == 0:
                try:
                    chunk = peer.recv(16384)
                except BlockingIOError:
                    continue
                self.assertTrue(chunk)
                data.extend(chunk)
        peer.settimeout(2)
        while len(data) < len(expected):
            chunk = peer.recv(min(65536, len(expected) - len(data)))
            self.assertTrue(chunk)
            data.extend(chunk)
        self.assertEqual(bytes(data), expected)
        self.assertGreater(steps, 4)
        return json.loads(data)

    def test_backpressure_yields_to_other_bridge_and_preserves_original_bytes_once(self):
        bridge, peer, receipt = self.large_response()
        pending = self.stall(bridge)
        expected = pending.data
        self.assertGreater(pending.offset, 0)
        self.assertLess(pending.offset, len(expected))
        other = self.bridge()
        other_peer = self.session(other)
        # Also leave a second request queued on the stalled bridge.
        self.send(peer, self.request(2))
        self.send(other_peer, self.request("other", arguments={"bridge": 2}))
        with patch.object(socket.socket, "sendall", side_effect=AssertionError("poll used sendall")):
            self.assertFalse(bridge.poll())
            started = time.monotonic()
            self.poll_frame(other)
            self.assertLess(time.monotonic() - started, 0.15)
            response = json.loads(self.receive(other_peer))
        self.assertFalse(response["response"]["result"]["isError"])
        self.assertTrue(bridge.has_pending_output)
        self.assertEqual(len(self.calls), 2)
        # Mutating the executor result after dispatch must not re-encode it.
        receipt["receipt"] = "changed after execution"
        with patch.object(bridge, "_encode", side_effect=AssertionError("Response was encoded twice")):
            original = self.drain_slowly(bridge, peer, expected)
        self.assertEqual(json.loads(original["response"]["result"]["content"][0]["text"])["receipt"],
                         "original")
        self.assertNotIn((int, 2), bridge._seen)
        self.assertEqual([call.id for call in self.calls], ["mcp-1", "mcp-1"])
        self.poll_frame(bridge)
        # The second response is large too, so drain it cooperatively.
        second = bridge._pending_output
        self.assertIsNotNone(second)
        self.drain_slowly(bridge, peer, second.data)
        self.assertEqual([call.id for call in self.calls], ["mcp-1", "mcp-1", "mcp-2"])

    def test_terminal_revoke_flushes_receipt_then_rejects_queued_tool(self):
        bridge, peer, receipt = self.large_response(terminal=True)
        pending = self.stall(bridge)
        self.send(peer, self.request(2))
        bridge.revoke()
        with self.assertRaises(mcp_host.ToolBridgeError):
            bridge.activate()
        result = self.drain_slowly(bridge, peer, pending.data)
        self.assertEqual(json.loads(result["response"]["result"]["content"][0]["text"]), receipt)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn((int, 2), bridge._seen)
        self.poll_frame(bridge)
        denied = json.loads(self.receive(peer))["response"]
        self.assertEqual(denied["id"], 2)
        self.assertEqual(denied["error"]["code"], -32600)
        self.assertEqual(len(self.calls), 1)

    def test_output_total_deadline_survives_progress_polls_and_revoke(self):
        bridge, peer, _ = self.large_response(timeout=0.5)
        pending = self.stall(bridge)
        deadline = pending.deadline
        peer.recv(65536)
        with patch.object(mcp_host.time, "monotonic", return_value=deadline - 0.01):
            for _ in range(8):
                self.assertFalse(bridge.poll())
                self.assertIs(bridge._pending_output, pending)
                self.assertEqual(pending.deadline, deadline)
                peer.setblocking(False)
                try:
                    peer.recv(65536)
                except BlockingIOError:
                    pass
        bridge.revoke()
        with patch.object(mcp_host.time, "monotonic", return_value=deadline + 0.001):
            with self.assertRaises(mcp_host.ToolBridgeError):
                bridge.poll()
        self.assertFalse(bridge.has_pending_output)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(mcp_host.ToolBridgeError):
            bridge.server_config()

    def test_close_discards_pending_without_undoing_execution_or_replaying(self):
        bridge, peer, _ = self.large_response()
        self.stall(bridge)
        failures = []

        def close():
            try:
                bridge.close()
            except BaseException as exc:
                failures.append(exc)

        closer = threading.Thread(target=close)
        closer.start()
        closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertEqual(failures, [])
        self.assertFalse(bridge.has_pending_output)
        for step in (bridge.poll, bridge.pump):
            with self.assertRaises(mcp_host.ToolBridgeError):
                step()
        self.assertEqual(len(self.calls), 1)
        bridge.close()

    def test_pending_disconnect_aborts_without_reexecution(self):
        bridge, peer, _ = self.large_response()
        self.stall(bridge)
        linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        peer.close()
        deadline = time.monotonic() + 2
        with self.assertRaises(mcp_host.ToolBridgeError):
            while time.monotonic() < deadline:
                bridge.poll()
            self.fail("Reset peer was not observed")
        self.assertFalse(bridge.has_pending_output)
        self.assertEqual(len(self.calls), 1)

    def test_poll_owner_reentry_and_readonly_pending_property(self):
        bridge = self.bridge()
        failures = []

        def wrong_owner():
            try:
                bridge.poll()
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=wrong_owner)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], mcp_host.ToolBridgeError)
        self.assertIs(bridge.has_pending_output, False)
        with self.assertRaises(AttributeError):
            bridge.has_pending_output = True
        for timeout in (False, True, 0, -1, 1.001, float("nan"), float("inf"), "0.01", None):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                bridge.poll(timeout)

        def execute(call):
            for step in (reentrant.poll, reentrant.pump):
                with self.assertRaises(mcp_host.ToolBridgeError):
                    step()
            return self.execute(call)

        reentrant = self.bridge(execute)
        peer = self.session(reentrant)
        result = self.exchange(reentrant, peer, self.request(1))
        self.assertFalse(result["response"]["result"]["isError"])
        self.assertEqual(self.calls, [ToolCall("mcp-1", "record", {})])

    def test_executor_exception_identity_and_queued_call_never_runs(self):
        for error in (RuntimeError("private"), OSError("private"), mcp_host.ToolBridgeError("private"),
                      KeyboardInterrupt(), SystemExit(7), GeneratorExit()):
            with self.subTest(error=type(error).__name__):
                effects = []

                def execute(call):
                    effects.append(call)
                    raise error

                bridge = self.bridge(execute)
                peer = self.session(bridge)
                self.send(peer, self.request(1), self.request(2))
                with self.assertRaises(type(error)) as raised:
                    self.poll_frame(bridge)
                self.assertIs(raised.exception, error)
                self.assertEqual(len(effects), 1)
                self.assertFalse(bridge.has_pending_output)
                self.assertEqual(peer.recv(65536), b"")
                with self.assertRaises(mcp_host.ToolBridgeError):
                    bridge.poll()

    def test_one_poll_handles_one_frame_and_typed_ids_and_limits_stay_enforced(self):
        bridge = self.bridge()
        peer = self.session(bridge)
        self.send(peer, self.request(1), self.request("1"), self.request(1))
        for count in (1, 2):
            self.poll_frame(bridge)
            self.assertEqual(len(self.calls), count)
            self.assertFalse(json.loads(self.receive(peer))["response"]["result"]["isError"])
        with self.assertRaises(mcp_host.ToolBridgeError):
            self.poll_frame(bridge)
        self.assertEqual(len(self.calls), 2)
        for identifier in (True, None, 2**53, "x" * 257):
            with self.subTest(identifier=identifier):
                invalid = self.bridge()
                invalid_peer = self.session(invalid)
                self.send(invalid_peer, self.request(identifier))
                with self.assertRaises(mcp_host.ToolBridgeError):
                    self.poll_frame(invalid)
        limited = self.bridge()
        limited_peer = self.session(limited)
        with patch.object(mcp_host, "MAX_REQUESTS", 2):
            self.exchange(limited, limited_peer, self.request(1))
            self.send(limited_peer, self.request(2))
            with self.assertRaises(mcp_host.ToolBridgeError):
                self.poll_frame(limited)

    def test_poll_shares_one_wait_budget_between_accept_and_receive(self):
        bridge = self.bridge()
        env = {row["name"]: row["value"] for row in bridge.server_config()["env"]}
        peer = socket.create_connection(("127.0.0.1", int(env["CATER_MCP_PORT"])), timeout=2)
        self.addCleanup(peer.close)
        accept, recv = socket.socket.accept, socket.socket.recv
        observed = []
        with patch.object(mcp_host.time, "monotonic", return_value=100) as clock:
            def delayed_accept(listener):
                connection = accept(listener)
                clock.return_value = 100.0009
                return connection

            def receive(connection, size):
                observed.append(connection.gettimeout())
                return recv(connection, size)

            with patch.object(socket.socket, "accept", delayed_accept), \
                    patch.object(socket.socket, "recv", receive):
                self.assertFalse(bridge.poll(0.001))
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0)
        self.assertLessEqual(observed[0], 0.000101)
        self.assertFalse(bridge.has_pending_output)

    def test_poll_authentication_and_exact_byte_limit(self):
        bridge = self.bridge()
        env = {row["name"]: row["value"] for row in bridge.server_config()["env"]}
        for capability in ("wrong", None, "\u4f2a\u9020", "x" * 129):
            with self.subTest(capability=capability):
                peer = socket.create_connection(("127.0.0.1", int(env["CATER_MCP_PORT"])), timeout=2)
                self.addCleanup(peer.close)
                self.send(peer, {"capability": capability})
                self.assertFalse(bridge.poll())
                self.assertEqual(peer.recv(1024), b"")
                self.assertFalse(bridge.has_pending_output)
                self.assertEqual(self.calls, [])
                self.assertEqual(bridge.server_config()["env"], [
                    {"name": key, "value": value} for key, value in env.items()
                ])
        peer = self.session(bridge)
        with patch.object(mcp_host, "MAX_BYTES", 1024):
            value = json_utf8(self.request(1, arguments={"text": "\u4e2d" * 100}))
            peer.sendall(value + b" " * (1024 - len(value)) + b"\n")
            self.poll_frame(bridge)
            result = json.loads(self.receive(peer))["response"]["result"]
            self.assertFalse(result["isError"])
            self.assertEqual(len(self.calls), 1)
            value = json_utf8(self.request(2))
            peer.sendall(value + b" " * (1025 - len(value)) + b"\n")
            with self.assertRaises(mcp_host.ToolBridgeError):
                self.poll_frame(bridge)
        self.assertEqual(len(self.calls), 1)

    def test_partial_input_deadline_does_not_restart_on_new_bytes(self):
        bridge = self.bridge()
        peer = self.session(bridge)
        with patch.object(mcp_host.time, "monotonic", return_value=100) as clock:
            peer.sendall(b"{")
            self.assertFalse(bridge.poll())
            clock.return_value = 104
            peer.sendall(b" ")
            self.assertFalse(bridge.poll())
            clock.return_value = 105.001
            with self.assertRaises(mcp_host.ToolBridgeError):
                bridge.poll()
        self.assertFalse(bridge.has_pending_output)
        self.assertEqual(self.calls, [])

    def test_close_during_execution_and_send_never_resurrects_pending(self):
        def execute(call):
            self.execute(call)
            bridge.close()
            return {"status": "ok", "saved": True}

        bridge = self.bridge(execute)
        peer = self.session(bridge)
        self.send(peer, self.request(1), self.request(2))
        with self.assertRaises(mcp_host.ToolBridgeError):
            self.poll_frame(bridge)
        self.assertFalse(bridge.has_pending_output)
        self.assertEqual(len(self.calls), 1)

        sending, send_peer, _ = self.large_response()
        pending = self.stall(sending)
        original_send = socket.socket.send

        def close_after_send(connection, data):
            # The saved pending object must survive close clearing the slot.
            send_peer.recv(65536)
            try:
                return original_send(connection, data)
            finally:
                sending.close()

        with patch.object(socket.socket, "send", close_after_send):
            with self.assertRaises(mcp_host.ToolBridgeError):
                sending.poll()
        self.assertFalse(sending.has_pending_output)
        self.assertIsNone(sending._pending_output)
        self.assertEqual(pending.data.count(b"\n"), 1)
        self.assertEqual(len(self.calls), 2)

    def test_pump_drains_pending_from_poll_using_same_response(self):
        bridge, peer, receipt = self.large_response()
        pending = self.stall(bridge)
        received, failures = [], []

        def read():
            try:
                received.append(self.receive(peer))
            except BaseException as exc:
                failures.append(exc)

        reader = threading.Thread(target=read)
        reader.start()
        try:
            # No new frame is handled when pump finishes an earlier poll's send.
            self.assertFalse(bridge.pump(0.001))
            self.assertFalse(bridge.has_pending_output)
        finally:
            reader.join(3)
            if reader.is_alive():
                bridge.close()
                reader.join(2)
        self.assertFalse(reader.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(received, [pending.data])
        result = json.loads(received[0])["response"]["result"]
        self.assertEqual(json.loads(result["content"][0]["text"]), receipt)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
