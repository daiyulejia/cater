"""Combined ACP writer/run-budget regressions, memory-only and no model calls."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from cater.acp import AcpSession, AcpTimeout
from cater.external_agent import ExternalRunLimit
import test_acp_duplex_boundaries as memory


class ExternalInputBudgetTests(unittest.TestCase):
    def fixture(self):
        fixture = memory.AcpDuplexBoundaryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_budget_expiring_during_unfinished_input_is_transport_failure_not_execution_checkpoint(self):
        for cooperative in (False, True):
            for blocked_at in ("write", "flush"):
                for boundary in ("before_bridge", "after_bridge"):
                    for total in (2, 5):
                        with self.subTest(cooperative=cooperative, blocked_at=blocked_at,
                                          boundary=boundary, total=total):
                            self.check_unfinished_input(cooperative, blocked_at, boundary, total)

    def check_unfinished_input(self, cooperative, blocked_at, boundary, total):
        fixture = self.fixture()
        now = [100.0]
        entered = threading.Event()

        class StalledInput(memory.MemoryInput):
            def __init__(self):
                super().__init__(blocked=blocked_at == "write", reject_cancel=True)

            def write(self, data):
                if blocked_at == "write":
                    entered.set()
                return super().write(data)

            def flush(self):
                if blocked_at == "flush":
                    self.release.clear()
                    entered.set()
                    if not self.release.wait(memory.WAIT):
                        raise OSError("Memory flush watchdog expired")
                return super().flush()

        pipe = StalledInput()
        original_prompt = AcpSession._iter_prompt

        def prompt(session, *args, **kwargs):
            # Budget starts at 100; the existing pipe deadline starts at 100.1.
            now[0] = 100.1
            return (yield from original_prompt(session, *args, **kwargs))

        def iter_open(session, **kwargs):
            if False:
                yield
            return session.open(**kwargs)

        pumping = []

        def pump(*args, **kwargs):
            pumping.append(threading.get_ident())
            if boundary == "after_bridge":
                now[0] = 102.0
            return False

        with patch("cater.acp.time.monotonic", lambda: now[0]), \
                patch.object(AcpSession, "_iter_prompt", prompt), \
                patch.object(AcpSession, "iter_open", iter_open), \
                patch.object(memory.MemoryBridge, "pump", pump), \
                patch.object(memory.MemoryBridge, "poll", pump, create=True):
            run, bridges, execute, updates = fixture.external(pipe=pipe, total=total)
            observations = []

            def current():
                if not fixture.sessions:
                    return
                session = fixture.sessions[-1]
                done = session._write_done
                if done is None:
                    return
                self.assertTrue(entered.wait(memory.WAIT))
                self.assertFalse(done.is_set())
                observations.append((session._write_owner, done.is_set()))
                now[0] = 102.0 if boundary == "before_bridge" else 101.9

            run._check_current = current
            with self.assertRaises(AcpTimeout) as caught:
                if cooperative:
                    list(run.iter_run("Only an offline input boundary fixture"))
                else:
                    run.run("Only an offline input boundary fixture")
            session = fixture.sessions[-1]
            self.assertNotIsInstance(caught.exception, ExternalRunLimit)
            self.assertEqual(caught.exception.phase, "input")
            self.assertTrue(caught.exception.prompt_may_have_run)
            self.assertIs(run._failure, caught.exception)
            self.assertEqual(run._budget.started, 100.0)
            self.assertEqual(run._budget.total, total)
            self.assertEqual(now[0], 102.0)
            self.assertTrue(observations)
            self.assertTrue(all(owner == threading.get_ident() and not done for owner, done in observations))
            self.assertEqual(bool(pumping), boundary == "after_bridge")
            self.assertTrue(run.state.cleanup_complete)
            self.assertEqual(run.state.phase, "failed")
            self.assertTrue(all(bridge.revoked and bridge.closed for bridge in bridges))
            self.assertEqual([message["method"] for message in pipe.attempts], ["session/prompt"])
            self.assertEqual(updates, [])
            execute.assert_not_called()
            self.assertTrue(session._input_failed)
            fixture.terminal(session)
            with self.assertRaises(RuntimeError):
                run.run("No retry")
        fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
