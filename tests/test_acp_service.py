"""Host servicing while waiting for an external Agent, without another runtime."""

from pathlib import Path
import sys
import tempfile
import threading
import unittest

from cater.acp import AcpSession, AcpUnavailable


PEER = Path(__file__).resolve().parent / "fixtures" / "acp_peer.py"


class AcpServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def session(self, mode="normal"):
        client = AcpSession([sys.executable, "-u", str(PEER), mode, str(self.root)],
                            cwd=self.root, timeout=3)
        self.addCleanup(client.close)
        return client

    def test_service_runs_on_calling_thread_during_open_and_prompt(self):
        client = self.session()
        calls = []
        service = lambda: calls.append(threading.get_ident())
        client.open(service=service)
        opening = len(calls)
        self.assertGreater(opening, 0)
        result = client.prompt("hello", service=service)
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertGreater(len(calls), opening)
        self.assertEqual(set(calls), {threading.get_ident()})

    def test_service_can_unblock_agent_waiting_for_host_during_initialization(self):
        client = self.session("initialize-timeout")
        calls = []

        def service():
            calls.append(1)
            (self.root / "release").touch()

        client.open(service=service)
        self.assertTrue(calls)
        self.assertEqual(client.session_id, "fixture-session")

    def test_scope_exception_stops_prompt_without_replay(self):
        client = self.session("timeout")
        client.open()

        class ScopeChanged(Exception):
            pass

        def service():
            raise ScopeChanged("scope changed")

        with self.assertRaises(ScopeChanged):
            client.prompt("must stop", service=service)
        self.assertIsNotNone(client._proc.poll())
        with self.assertRaises(AcpUnavailable):
            client.prompt("must not replay")

    def test_service_can_close_session_without_hanging(self):
        client = self.session("timeout")
        client.open()
        with self.assertRaises(AcpUnavailable) as caught:
            client.prompt("stop", service=client.close)
        self.assertTrue(caught.exception.prompt_may_have_run)
        self.assertIsNotNone(client._proc.poll())

    def test_invalid_service_is_rejected_before_start_or_prompt(self):
        client = self.session()
        with self.assertRaises(ValueError):
            client.open(service=42)
        self.assertIsNone(client._proc)
        client.open()
        with self.assertRaises(ValueError):
            client.prompt("not sent", service=42)
        self.assertEqual(client.prompt("valid").stop_reason, "end_turn")

    def test_startup_service_control_errors_keep_original_identity(self):
        for error in (ValueError("scope changed"), OSError("scope unavailable")):
            with self.subTest(kind=type(error).__name__):
                client = self.session()

                def service():
                    raise error

                with self.assertRaises(type(error)) as caught:
                    client.open(service=service)
                self.assertIs(caught.exception, error)
                self.assertIsNotNone(client._proc.poll())


if __name__ == "__main__":
    unittest.main()
