"""Independent cooperative lifecycle probes; synthetic tools and temporary DBs only."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cater.acp import AcpPromptResult, AcpProtocolError, AcpSession, AcpTimeout, _exhaust
from cater.external_agent import ExternalYield
from cater.team import HOST
from cater.work import WorkConflict
import test_acp_duplex_boundaries as memory
import test_team_external as fixture


class ReviewRun(fixture.ScriptedExternalRun):
    def __init__(self, driver, **kwargs):
        super().__init__(driver, **kwargs)
        self.ordinal = len(driver.runs)
        self.revoke_error = None
        self.close_error = None

    def revoke_tools(self):
        super().revoke_tools()
        if self.revoke_error is not None:
            raise self.revoke_error

    def call(self, name, **arguments):
        if self.revoked:
            raise AssertionError("Revoked review role attempted another tool")
        call = fixture.ToolCall(f"review-{self.ordinal}-{len(self.receipts)}", name, arguments)
        result = self.execute(call)
        self.receipts.append((call, result))
        self.check_current()
        return result

    def iter_run(self, prompt):
        self.context = json.loads(prompt.rsplit("\n", 1)[-1])
        self.state = replace(self.state, phase="prompting", prompt_attempted=True,
                             session_id=f"review-{self.ordinal}")
        try:
            action = self.driver.scripts[self.context["you"]["id"]].pop(0)
            value = action(self)
            if hasattr(value, "__next__"):
                value = yield from value
            self.state = replace(self.state, phase="finished", stop_reason="end_turn")
            return AcpPromptResult(value or "", "end_turn")
        except ExternalYield:
            self.state = replace(self.state, phase="yielded")
            raise
        except BaseException:
            self.state = replace(self.state, phase="failed")
            raise
        finally:
            self.state = replace(self.state, cleanup_complete=self.close_error is None)
            if self.close_error is not None:
                raise self.close_error


class ReviewDriver(fixture.ScriptedExternalDriver):
    def agent_run(self, **kwargs):
        run = ReviewRun(self, **kwargs)
        self.runs.append(run)
        return run


class CooperativeFatalBoundaries(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.TeamExternalTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.driver = self.driver = ReviewDriver()
        self.fx.rt.drivers[self.driver.name] = self.driver
        self.launch = self.enterContext(patch("subprocess.Popen", side_effect=AssertionError(
            "Independent team review must never launch a process")))
        self.addCleanup(self.launch.assert_not_called)

    def assert_clean(self):
        self.assertEqual(self.fx.rt.team.external._live, {})
        self.assertIsNone(self.fx.rt.team.external._scheduler)
        self.assertTrue(all(run.revoked for run in self.driver.runs))

    def fatal(self, *, event=None, revoke_error=None, finish_error=None):
        fx = self.fx
        marker = KeyboardInterrupt("review-primary-control")

        def action(run):
            run.revoke_error = revoke_error
            raise marker

        self.driver.scripts = {fx.manager: [action]}
        fx.receive("Synthetic fatal lifecycle")
        original_emit = fx.rt.log.emit

        def emit(**kwargs):
            if kwargs["type"] == event:
                raise OSError("review-secondary-event-write")
            return original_emit(**kwargs)

        with patch.object(fx.rt.log, "emit", side_effect=emit), patch.object(
                fx.rt.team.external_runs, "finish",
                side_effect=finish_error, wraps=None if finish_error else fx.rt.team.external_runs.finish):
            caught = None
            try:
                fx.rt.collaborate(fx.task)
            except BaseException as exc:
                caught = exc
        self.assert_clean()
        self.assertTrue(self.driver.runs[0].state.cleanup_complete)
        self.assertIs(caught, marker, "A secondary settlement failure replaced the original control object")

    def test_fatal_survives_revoke_and_external_finish_failures(self):
        self.fatal(revoke_error=OSError("review-revoke"), finish_error=OSError("review-finish"))

    def test_fatal_survives_model_called_receipt_failure(self):
        self.fatal(event="team.model_called")

    def test_fatal_survives_host_run_finished_receipt_failure(self):
        self.fatal(event="team.run_finished")

    def test_model_receipt_failure_cannot_resume_another_role_after_fatal(self):
        fx = self.fx
        marker = KeyboardInterrupt("review-stop-all-before-worker-write")
        effects = []

        def manager(run):
            fx.rt.team._send(fx.task, fx.rt.organization.leader, fx.worker, "Hold", "assignment")
            yield None
            raise marker

        def worker(run):
            effects.append(run.call("publish_artifact", filename="after-fatal.txt", content="old work",
                                    summary="must not execute after fatal", format="text"))

        self.driver.scripts = {fx.manager: [manager, lambda run: run.call("wait_for_reply")],
                               fx.worker: [worker]}
        original_emit = fx.rt.log.emit

        def emit(**kwargs):
            if kwargs["type"] == "team.model_called" and kwargs["payload"]["role"] == fx.manager:
                raise OSError("review-secondary-model-receipt")
            return original_emit(**kwargs)

        fx.receive()
        tick = iter(range(1000, 100000))
        caught = None
        with patch.object(fx.rt.log, "emit", side_effect=emit), patch("cater.team.time", SimpleNamespace(
                monotonic=lambda: next(tick), sleep=lambda _: None)):
            try:
                fx.rt.collaborate(fx.task)
            except BaseException as exc:
                caught = exc
        self.assert_clean()
        self.assertEqual(effects, [], "Another role executed a write after fatal control was raised")
        self.assertIs(caught, marker)

    def test_shutdown_closes_remaining_run_even_if_its_close_and_revoke_fail(self):
        fx = self.fx
        marker = KeyboardInterrupt("review-scheduler-fatal")

        def manager(run):
            fx.rt.team._send(fx.task, fx.rt.organization.leader, fx.worker, "Hold", "assignment")
            while len(self.driver.runs) < 2:
                yield None
            raise marker

        def worker(run):
            run.revoke_error = OSError("review-revoke")
            run.close_error = OSError("review-close")
            while True:
                yield None

        self.driver.scripts = {fx.manager: [manager], fx.worker: [worker]}
        fx.receive()
        tick = iter(range(1000, 100000))
        with patch("cater.team.time", SimpleNamespace(monotonic=lambda: next(tick), sleep=lambda _: None)):
            with self.assertRaises(KeyboardInterrupt) as caught:
                fx.rt.collaborate(fx.task)
        self.assertIs(caught.exception, marker)
        self.assert_clean()
        self.assertEqual(len(self.driver.runs), 2)
        self.assertEqual(self.driver.runs[1].state.phase, "failed")
        pending, = fx.rt.team.external_runs.pending(fx.task)
        self.assertEqual(pending["role"], fx.worker)
        self.assertEqual(pending["outcome"], "unknown")
        self.assertFalse(pending["replay_authorized"])

    def test_claimed_but_unstarted_generator_gets_admission_finish(self):
        fx = self.fx
        marker = KeyboardInterrupt("review-before-new-role-first-step")

        def manager(run):
            fx.rt.team._send(fx.task, fx.rt.organization.leader, fx.worker, "Hold", "assignment")
            yield None
            raise marker

        self.driver.scripts = {fx.manager: [manager]}
        fx.receive()
        tick = iter(range(1000, 100000))
        with patch("cater.team.time", SimpleNamespace(monotonic=lambda: next(tick), sleep=lambda _: None)):
            with self.assertRaises(KeyboardInterrupt) as caught:
                fx.rt.collaborate(fx.task)
        self.assertIs(caught.exception, marker)
        self.assertEqual(len(self.driver.runs), 1)
        worker_end, = [e for e in fx.events("team.run_finished") if e.payload["role"] == fx.worker]
        self.assertTrue(worker_end.payload["admission_not_started"])
        self.assertEqual(worker_end.payload["status"], "interrupted")
        self.assert_clean()

    def test_failing_peer_cleanup_does_not_skip_later_peer(self):
        fx = self.fx
        marker = KeyboardInterrupt("review-three-live-roles")
        closed = []

        def manager(run):
            for recipient in (fx.worker, fx.reviewer):
                fx.rt.team._send(fx.task, fx.rt.organization.leader, recipient, "Hold", "assignment")
            while len(self.driver.runs) < 3:
                yield None
            raise marker

        def peer(run):
            try:
                run.revoke_error = OSError("review-peer-revoke")
                while True:
                    yield None
            finally:
                if not closed:
                    run.close_error = OSError("review-first-peer-close")
                closed.append(run.context["you"]["id"])
                self.assertTrue(all(other.revoked for other in self.driver.runs[1:]))

        self.driver.scripts = {fx.manager: [manager], fx.worker: [peer], fx.reviewer: [peer]}
        fx.receive()
        tick = iter(range(1000, 100000))
        with patch("cater.team.time", SimpleNamespace(monotonic=lambda: next(tick), sleep=lambda _: None)):
            with self.assertRaises(KeyboardInterrupt) as caught:
                fx.rt.collaborate(fx.task)
        self.assertIs(caught.exception, marker)
        self.assertCountEqual(closed, [fx.worker, fx.reviewer])
        by_role = {run.context["you"]["id"]: run for run in self.driver.runs}
        self.assertFalse(by_role[closed[0]].state.cleanup_complete)
        self.assertTrue(by_role[closed[1]].state.cleanup_complete)
        self.assertEqual(len(fx.events("team.run_started")), len(fx.events("team.run_finished")))
        self.assert_clean()

    def test_four_regular_roles_leave_fifth_slot_for_customer(self):
        fx = self.fx
        recipients = [role.id for role in fx.rt.worker_roles]
        recipients.append(next(role.id for role in fx.rt.roles if role.id not in recipients))
        release = []
        waiting = []
        sizes = []

        def peer(run):
            recipient = run.context["you"]["id"]
            waiting.append(recipient)
            sizes.append(len(fx.rt.team.external._live))
            if len(waiting) == 4:
                fx.receive("Use the reserved customer position")
            while not release:
                yield None
            run.call("send_message", recipient=fx.manager,
                     reply_to=run.context["active_assignment"]["seq"], text="Formal return")

        def customer(run):
            if not release:
                self.assertEqual(len(waiting), 4)
                self.assertNotIn(recipients[-1], waiting)
                self.assertEqual(len(fx.rt.team.external._live), 5)
                sizes.append(len(fx.rt.team.external._live))
                release.append(True)
            return ""

        self.driver.scripts = {recipient: [peer] for recipient in recipients}
        self.driver.scripts[fx.manager] = [customer] * 8
        for recipient in recipients:
            fx.rt.team._send(fx.task, fx.rt.organization.leader, recipient, "Hold", "assignment")
        tick = iter(range(1000, 100000))
        with patch("cater.team.time", SimpleNamespace(monotonic=lambda: next(tick), sleep=lambda _: None)):
            fx.rt.collaborate(fx.task)
        self.assertEqual(release, [True])
        self.assertEqual(waiting, recipients)
        self.assertEqual(max(sizes), 5)
        self.assertEqual(len(fx.events("team.external_run_started")),
                         len(fx.events("team.external_run_finished")))
        self.assertEqual(fx.rt.team.external_runs.pending(fx.task), [])
        self.assert_clean()

    def test_same_role_new_input_closes_old_before_new_factory(self):
        fx = self.fx
        sequence = []
        old = []

        def first(run):
            old.append(run)
            fx.receive("New input to same manager")
            yield None
            run.check_current(force=True)
            self.fail("Stale role continued")

        def second(run):
            self.assertTrue(old[0].revoked and old[0].state.cleanup_complete)
            sequence.append("new")
            return "Observed new input"

        self.driver.scripts = {fx.manager: [first, second]}
        fx.receive("Old input")
        fx.rt.collaborate(fx.task)
        self.assertEqual(sequence, ["new"])
        starts = fx.events("team.external_run_started")
        ends = fx.events("team.external_run_finished")
        self.assertLess(ends[0].seq, starts[1].seq)
        self.assertEqual(len(starts), 2)
        self.assert_clean()

    def test_force_owns_after_goal_change_does_not_authorize_old_write(self):
        fx = self.fx
        rows = []

        def worker(run):
            row, = fx.rt.team.external_runs.pending(fx.task)
            self.assertTrue(fx.rt.team.external.owns(fx.task, row))
            fx.rt.log.emit(type="team.objective_updated", task_id=fx.task, actor=HOST,
                           payload={"goal": "Changed by independent fixture"})
            self.assertFalse(fx.rt.team.external.owns(fx.task, row))
            rows.append(row)
            with self.assertRaises(WorkConflict):
                run.execute(fixture.ToolCall("review-old-write", "publish_artifact", {
                    "filename": "obsolete.txt", "content": "must not execute",
                    "summary": "obsolete", "format": "text"}))
            yield None

        self.driver.scripts = {fx.worker: [worker]}
        fx.rt.team._send(fx.task, fx.rt.organization.leader, fx.worker, "Hold", "assignment")
        fx.rt.collaborate(fx.task)
        self.assertEqual(len(rows), 1)
        self.assertEqual(fx.events("deliverable.published"), [])
        self.assertEqual(fx.events("team.tool_started"), [])
        self.assert_clean()


class CooperativeAcpBoundaries(unittest.TestCase):
    def setUp(self):
        self.fx = memory.AcpDuplexBoundaryTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def staged_input(self, incoming, *, timeout=2, request_handler=None):
        pipe = memory.MemoryInput(blocked=True, reject_cancel=True)
        session = self.fx.session(pipe=pipe, on_request=request_handler)
        session.timeout = timeout
        pending = iter(incoming)
        owner_calls = []

        def service():
            owner_calls.append(session._write_owner)
            try:
                value = next(pending)
            except StopIteration:
                pipe.release.set()
            else:
                session._frames.put_nowait(memory.frame(value))

        return session, pipe, service, owner_calls

    def test_cooperative_input_defers_request_public_terminal_in_order_once(self):
        callbacks = []
        session, pipe, service, _ = self.staged_input(
            [memory.request(), memory.update("before"), memory.result(), memory.update("after")],
            request_handler=lambda *_: callbacks.append("request") or {})
        result = _exhaust(session.iter_prompt("single prompt", service=service,
            on_update=lambda update: callbacks.append(update["content"]["text"])))
        self.assertEqual(result.text, "before")
        self.assertEqual(callbacks, ["request", "before"])
        self.assertEqual([m.get("method") for m in pipe.attempts], ["session/prompt", None])
        self.assertEqual(len(pipe.attempts), 2)
        self.assertEqual(json.loads(session._frames.get_nowait())["params"]["update"]["content"]["text"], "after")

    def test_cooperative_input_deferred_limit_prevents_any_permission_dispatch(self):
        callbacks = []
        incoming = [memory.request(str(i)) for i in range(128)] + [memory.result()]
        session, pipe, _, _ = self.staged_input(incoming,
            request_handler=lambda *_: callbacks.append("request") or {})
        pending = iter(incoming)

        def service():
            for _ in range(16):
                try:
                    value = next(pending)
                except StopIteration:
                    return
                session._frames.put_nowait(memory.frame(value))

        with self.assertRaises(AcpProtocolError):
            _exhaust(session.iter_prompt("single prompt", service=service))
        self.assertEqual(callbacks, [])
        self.assertEqual([m.get("method") for m in pipe.attempts], ["session/prompt"])
        self.assertIsNotNone(session._proc.poll())

    def test_cooperative_close_during_input_does_not_append_cancel_to_partial_frame(self):
        pipe = memory.MemoryInput(blocked=True, reject_cancel=True)
        session = self.fx.session(pipe=pipe)
        iterator = session.iter_prompt("single prompt")
        self.assertIsNone(next(iterator))
        iterator.close()
        self.assertEqual([m.get("method") for m in pipe.attempts], ["session/prompt"])
        self.assertIsNotNone(session._proc.poll())
        self.assertFalse(session._prompt_active)
        self.assertTrue(session._input_failed)

    def test_cooperative_input_deadline_is_not_reset_after_unscheduled_yield(self):
        pipe = memory.MemoryInput(blocked=True)
        session = self.fx.session(pipe=pipe)
        with patch("cater.acp.time.monotonic", return_value=100) as clock:
            iterator = session.iter_prompt("single prompt", timeout=20)
            self.assertIsNone(next(iterator))
            clock.return_value = 103
            with self.assertRaises(AcpTimeout) as caught:
                next(iterator)
        self.assertEqual(caught.exception.phase, "input")
        self.assertTrue(caught.exception.prompt_may_have_run)
        self.assertEqual([m.get("method") for m in pipe.attempts], ["session/prompt"])
        self.assertIsNotNone(session._proc.poll())


if __name__ == "__main__":
    unittest.main()
