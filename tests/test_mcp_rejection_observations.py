"""Fixed rejection observations over the authenticated loopback transport."""

import json
import socket
import time
import unittest
from unittest.mock import Mock, patch

from cater import mcp_host
from cater.tool_protocol import ToolCall, json_utf8


PRIVATE = "private-payload-id-name-token"
TOOLS = [{"name": "record", "description": "Fixture", "input_schema": {
    "type": "object", "additionalProperties": False,
}}]


class McpRejectionObservationTests(unittest.TestCase):
    def setUp(self):
        self.observer = Mock()
        self.execute = Mock(return_value={"status": "ok"})
        self.cooperative = False

    def bridge(self, **kwargs):
        kwargs.setdefault("on_rejection", self.observer)
        bridge = mcp_host.McpToolBridge(TOOLS, self.execute, **kwargs)
        self.addCleanup(bridge.close)
        return bridge

    @staticmethod
    def request(identifier, method="tools/call", **params):
        return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}

    def peer(self, bridge):
        env = {item["name"]: item["value"] for item in bridge.server_config()["env"]}
        peer = socket.create_connection(("127.0.0.1", int(env["CATER_MCP_PORT"])), timeout=2)
        self.addCleanup(peer.close)
        return peer, env["CATER_MCP_CAPABILITY"]

    def advance(self, bridge):
        advance = bridge.poll if self.cooperative else bridge.pump
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if advance(0.01):
                while bridge.has_pending_output:
                    self.assertLess(time.monotonic(), deadline)
                    advance(0.01)
                return
        self.fail("Queued frame was not handled")

    def send(self, peer, value):
        peer.sendall(value if isinstance(value, bytes) else json_utf8(value) + b"\n")

    def exchange(self, bridge, peer, value):
        self.send(peer, value)
        self.advance(bridge)
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = peer.recv(65536)
            self.assertTrue(chunk, "Response ended prematurely")
            data.extend(chunk)
        return json.loads(data)

    def authenticated(self, bridge):
        peer, token = self.peer(bridge)
        self.assertEqual(self.exchange(bridge, peer, {"capability": token}), {"authenticated": True})
        return peer

    def initialize(self, bridge, peer):
        self.exchange(bridge, peer, self.request("init", "initialize", protocolVersion=mcp_host.VERSION))
        self.assertEqual(self.exchange(bridge, peer, {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }), {"response": None})

    def session(self, bridge):
        peer = self.authenticated(bridge)
        self.initialize(bridge, peer)
        bridge.activate()
        return peer

    def rejection(self, bridge, peer, identifier, reason, code, message, **params):
        before = self.observer.call_count
        reply = self.exchange(bridge, peer, self.request(identifier, **params))
        self.assertEqual(reply, {"response": {"jsonrpc": "2.0", "id": identifier,
            "error": {"code": code, "message": message}}})
        self.assertEqual(self.observer.call_count, before + 1)
        self.assertEqual(self.observer.call_args.args, (reason,))
        self.assertEqual(self.observer.call_args.kwargs, {})
        self.assertNotIn(PRIVATE, repr(self.observer.call_args_list))

    def test_constructor_validates_observer_before_opening_socket(self):
        for callback in (False, 0, "callback", [], {}):
            with self.subTest(callback=callback), patch.object(mcp_host.socket, "socket") as sock:
                with self.assertRaisesRegex(ValueError, "rejection observer must be callable"):
                    self.bridge(on_rejection=callback)
                sock.assert_not_called()

    def test_default_and_none_keep_existing_rejections(self):
        for kwargs in ({}, {"on_rejection": None}):
            with self.subTest(kwargs=kwargs):
                bridge = mcp_host.McpToolBridge(TOOLS, self.execute, **kwargs)
                self.addCleanup(bridge.close)
                peer = self.session(bridge)
                reply = self.exchange(bridge, peer, self.request(1, name=PRIVATE))
                self.assertEqual(reply["response"]["error"], {
                    "code": -32602, "message": "Unknown tool or invalid arguments"})
        self.execute.assert_not_called()

    def test_rejection_order_and_fixed_reasons_on_pump_and_poll(self):
        for cooperative in (False, True):
            with self.subTest(cooperative=cooperative):
                self.cooperative = cooperative
                bridge = self.bridge()
                peer = self.authenticated(bridge)
                self.rejection(bridge, peer, PRIVATE, "not_initialized", -32600,
                    "MCP session is not initialized", name=PRIVATE, arguments=[PRIVATE])
                self.exchange(bridge, peer, self.request("init", "initialize", protocolVersion=mcp_host.VERSION))
                self.rejection(bridge, peer, 1, "not_initialized", -32600,
                    "MCP session is not initialized", name="record")
                self.exchange(bridge, peer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                self.rejection(bridge, peer, 2, "inactive", -32600,
                    "Host tools are not active for this run", name=PRIVATE, arguments=None)
                bridge.activate()
                for identifier, name in enumerate((PRIVATE, None, [], {}, "RECORD"), 3):
                    self.rejection(bridge, peer, identifier, "unknown_tool", -32602,
                        "Unknown tool or invalid arguments", name=name, arguments=[PRIVATE])
                for identifier, arguments in enumerate(([], None, PRIVATE, 1, True), 10):
                    self.rejection(bridge, peer, identifier, "arguments_not_object", -32602,
                        "Unknown tool or invalid arguments", name="record", arguments=arguments)
                bridge.revoke()
                self.rejection(bridge, peer, 20, "inactive", -32600,
                    "Host tools are not active for this run", name="record")
        self.execute.assert_not_called()

    def test_non_tool_methods_and_notifications_never_observe(self):
        bridge = self.bridge()
        peer = self.authenticated(bridge)
        for identifier, method in enumerate(("tools/list", "tools/Call", "unknown", "ping")):
            self.exchange(bridge, peer, self.request(identifier, method))
        self.initialize(bridge, peer)
        for identifier, method in enumerate(("tools/list", "tools/Call", "unknown", "ping"), 10):
            self.exchange(bridge, peer, self.request(identifier, method, cursor=PRIVATE))
        self.exchange(bridge, peer, {"jsonrpc": "2.0", "method": "notifications/cancelled"})
        self.observer.assert_not_called()
        self.execute.assert_not_called()

    def test_unauthenticated_requests_drop_without_observing_and_allow_authentication(self):
        bridge = self.bridge()
        for frame in (self.request(PRIVATE, name=PRIVATE), {"capability": PRIVATE}, b"invalid\n"):
            peer, _ = self.peer(bridge)
            self.send(peer, frame)
            self.assertFalse(bridge.pump(0.1))
            self.assertEqual(peer.recv(1), b"")
        self.observer.assert_not_called()
        self.execute.assert_not_called()
        peer = self.session(bridge)
        self.exchange(bridge, peer, self.request(1, name="record"))
        self.execute.assert_called_once()
        self.observer.assert_not_called()

    def test_malformed_envelopes_ids_and_frames_do_not_observe(self):
        request = self.request(1, name=PRIVATE, arguments=None)
        invalid = [[], {**request, "jsonrpc": "1.0"}, {**request, "params": []},
            {**request, "result": {}}, {**request, "error": {}}, {**request, "method": []},
            {key: value for key, value in request.items() if key != "id"},
            b'{"jsonrpc":"2.0","id":1,"id":2,"method":"tools/call"}\n',
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"arguments":NaN}}\n',
            b"\xff\n"]
        invalid.extend({**request, "id": identifier} for identifier in (
            None, True, 1.5, [], {}, "x" * 257, 2**53, -(2**53)))
        for frame in invalid:
            with self.subTest(frame=frame):
                bridge = self.bridge()
                peer = self.session(bridge)
                self.send(peer, frame)
                with self.assertRaises(mcp_host.ToolBridgeError):
                    self.advance(bridge)
                self.assertEqual(peer.recv(1), b"")
        self.observer.assert_not_called()
        self.execute.assert_not_called()

    def test_duplicate_id_and_request_limit_do_not_create_rejection(self):
        for first_method in ("tools/call", "ping"):
            with self.subTest(first_method=first_method):
                bridge = self.bridge()
                peer = self.session(bridge)
                self.exchange(bridge, peer, self.request(PRIVATE, first_method, name=PRIVATE))
                before = self.observer.call_count
                self.send(peer, self.request(PRIVATE, name=PRIVATE))
                with self.assertRaises(mcp_host.ToolBridgeError):
                    self.advance(bridge)
                self.assertEqual(self.observer.call_count, before)
        bridge = self.bridge()
        peer = self.session(bridge)
        before = self.observer.call_count
        with patch.object(mcp_host, "MAX_REQUESTS", 1):
            self.send(peer, self.request(1, name=PRIVATE))
            with self.assertRaises(mcp_host.ToolBridgeError):
                self.advance(bridge)
        self.assertEqual(self.observer.call_count, before)
        self.execute.assert_not_called()

    def test_private_dispatch_without_authenticated_pump_is_not_an_observation(self):
        bridge = self.bridge()
        bridge._dispatch(self.request(1, name=PRIVATE))
        self.authenticated(bridge)
        bridge._dispatch(self.request(2, name=PRIVATE))
        self.observer.assert_not_called()
        self.execute.assert_not_called()

    def test_observer_exceptions_preserve_rejection_then_valid_call_and_cleanup(self):
        for failure in (RuntimeError(PRIVATE), OSError(PRIVATE), mcp_host.ToolBridgeError(PRIVATE),
                        KeyboardInterrupt(PRIVATE), SystemExit(PRIVATE), GeneratorExit(PRIVATE)):
            with self.subTest(failure=type(failure).__name__):
                self.execute.reset_mock()
                self.observer.side_effect = failure
                bridge = self.bridge()
                peer = self.session(bridge)
                self.rejection(bridge, peer, 1, "unknown_tool", -32602,
                    "Unknown tool or invalid arguments", name=PRIVATE, arguments=[PRIVATE])
                self.execute.assert_not_called()
                response = self.exchange(bridge, peer, self.request(2, name="record"))
                self.assertFalse(response["response"]["result"]["isError"])
                self.execute.assert_called_once_with(ToolCall("mcp-1", "record", {}))
                before = self.observer.call_count
                self.send(peer, self.request(1, name=PRIVATE))
                with self.assertRaises(mcp_host.ToolBridgeError):
                    self.advance(bridge)
                self.assertEqual(self.observer.call_count, before)
                bridge.close()
                self.assertEqual(peer.recv(1), b"")

    def test_valid_arguments_are_not_schema_validated_and_host_errors_are_results(self):
        bridge = self.bridge()
        peer = self.session(bridge)
        receipt = {"status": "error", "complete": True, "outcome": "not_started"}
        self.execute.return_value = receipt
        arguments = {"nested_unknown": {PRIVATE: [PRIVATE]}}
        reply = self.exchange(bridge, peer, self.request(1, name="record", arguments=arguments))
        self.assertTrue(reply["response"]["result"]["isError"])
        self.assertEqual(json.loads(reply["response"]["result"]["content"][0]["text"]), receipt)
        self.execute.assert_called_once_with(ToolCall("mcp-1", "record", arguments))
        self.observer.assert_not_called()

    def test_executor_exceptions_propagate_without_observations(self):
        for failure in (RuntimeError(PRIVATE), mcp_host.ToolBridgeError(PRIVATE), KeyboardInterrupt(PRIVATE)):
            with self.subTest(failure=type(failure).__name__):
                self.execute.reset_mock()
                self.execute.side_effect = failure
                bridge = self.bridge()
                peer = self.session(bridge)
                self.send(peer, self.request(1, name="record"))
                with self.assertRaises(type(failure)) as raised:
                    self.advance(bridge)
                self.assertIs(raised.exception, failure)
                self.assertEqual(peer.recv(1), b"")
                self.execute.assert_called_once()
        self.observer.assert_not_called()

    def test_serialization_failures_do_not_observe_or_replay(self):
        for receipt in ([], {"value": float("nan")}, {"value": object()}, {"value": PRIVATE * 300}):
            with self.subTest(receipt_type=type(receipt).__name__):
                self.execute.reset_mock()
                self.execute.return_value = receipt
                bridge = self.bridge()
                peer = self.session(bridge)
                with patch.object(mcp_host, "MAX_BYTES", 1024):
                    reply = self.exchange(bridge, peer, self.request(1, name="record", arguments={PRIVATE: PRIVATE}))
                result = reply["response"]["result"]
                self.assertTrue(result["isError"])
                self.assertIn("effects may have occurred", result["content"][0]["text"])
                self.assertNotIn(PRIVATE, json.dumps(reply))
                self.execute.assert_called_once()
        self.observer.assert_not_called()

    def test_revoke_during_execution_preserves_receipt_and_only_observes_next_request(self):
        bridge = self.bridge()
        peer = self.session(bridge)

        def execute(call):
            bridge.revoke()
            return {"status": "ok", "saved": True}

        self.execute.side_effect = execute
        reply = self.exchange(bridge, peer, self.request(1, name="record"))
        self.assertFalse(reply["response"]["result"]["isError"])
        self.observer.assert_not_called()
        self.rejection(bridge, peer, 2, "inactive", -32600,
            "Host tools are not active for this run", name="record")
        self.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
