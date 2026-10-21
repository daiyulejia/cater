"""Native return health must preserve host handling and reported usage."""

from collections import UserDict
from contextlib import contextmanager
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from cater.availability import team_availability
from cater.runtime import Runtime, load_runtime_config
from cater.tool_protocol import ToolCall, ToolTurn
from cater.work import WorkConflict


CHANNEL = "native_contract_fixture"


def duck(**changes):
    fields = {"text": "Synthetic reply", "calls": [], "tokens": 7,
              "meta": {"token_usage": "reported"},
              "message": {"role": "assistant", "content": "Synthetic reply", "tool_calls": []}}
    fields.update(changes)
    return SimpleNamespace(**fields)


class Unreadable(SimpleNamespace):
    def __getattribute__(self, name):
        if name == object.__getattribute__(self, "blocked_field"):
            raise object.__getattribute__(self, "read_error")
        return super().__getattribute__(name)


def unreadable_field(fields, field, error, access="override"):
    if access == "override":
        return Unreadable(**fields, blocked_field=field, read_error=error)
    fields = {key: value for key, value in fields.items() if key != field}
    if access == "property":
        def read(self):
            raise error

        result_type = type("PropertyReturn", (SimpleNamespace,), {field: property(read)})
    else:
        def missing(self, name):
            if name == field:
                raise error
            raise AttributeError(name)

        result_type = type("DynamicReturn", (SimpleNamespace,), {"__getattr__": missing})
    return result_type(**fields)


class FixedDriver:
    name = CHANNEL

    def __init__(self, *returns):
        self.returns = list(returns)
        self.requests = 0

    def tool_turn(self, **kwargs):
        self.requests += 1
        result = self.returns.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class DictMeta(dict):
    pass


class FirstGetConflict(UserDict):
    def __init__(self):
        super().__init__(token_usage="reported")
        self.reads = []

    def get(self, key, default=None):
        self.reads.append(key)
        if len(self.reads) == 1:
            raise WorkConflict("Synthetic original first Mapping.get conflict")
        return super().get(key, default)


class InvalidatingGet(UserDict):
    def __init__(self):
        super().__init__(token_usage="reported")
        self.reads = []

    def get(self, key, default=None):
        self.reads.append(key)
        if key == "incomplete":
            self.data.pop("token_usage", None)
        return super().get(key, default)


class NativeHealthContractTests(unittest.TestCase):
    @contextmanager
    def session(self, *returns, prior="circuit_open"):
        base = load_runtime_config()
        config = replace(base, roles=[replace(role, driver=CHANNEL) for role in base.roles],
            organization=replace(base.organization, leader=replace(base.organization.leader, driver=CHANNEL)))
        driver = FixedDriver(*returns)
        with patch("cater.runtime.load_runtime_config", return_value=config):
            rt = Runtime(":memory:", drivers={CHANNEL: driver})
        with rt:
            health = rt._driver_health
            if prior == "circuit_open":
                health.record_failure(CHANNEL)
                health.record_failure(CHANNEL)
            elif prior == "succeeded":
                health.record_success(CHANNEL)
            else:
                self.fail("Unknown fixture prior state")
            task = rt.create_task("Synthetic native return contract")
            rt.team.enable(task)
            rt.team.receive(task, "Inspect the fixed synthetic input")
            claimed = rt.team._claim(task)
            self.assertIsNotNone(claimed)
            trace = Mock()
            with patch.object(health, "record_success", wraps=health.record_success) as success, \
                    patch.object(health, "record_rejection", wraps=health.record_rejection) as rejection, \
                    patch.object(health, "record_failure", wraps=health.record_failure) as failure:
                trace.attach_mock(success, "record_success")
                trace.attach_mock(rejection, "record_rejection")
                trace.attach_mock(failure, "record_failure")
                running = rt.team._run_message(task, *claimed)
                try:
                    yield SimpleNamespace(rt=rt, task=task, driver=driver, health=health,
                                          trace=trace, running=running)
                finally:
                    running.close()

    def events(self, fixture, kind):
        return list(fixture.rt.log.iter_events(task_id=fixture.task, event_types=(kind,)))

    def finish(self, fixture, status):
        list(fixture.running)
        finished, = self.events(fixture, "team.run_finished")
        self.assertEqual(finished.payload["status"], status)
        self.assertTrue(fixture.rt.verify_chain().ok)
        return finished.payload

    def assert_count(self, fixture, count):
        if count:
            self.assertTrue(fixture.health.is_circuit_open(CHANNEL, count))
        self.assertFalse(fixture.health.is_circuit_open(CHANNEL, count + 1))

    def assert_observation(self, fixture, *, rejected, count):
        name = "record_rejection" if rejected else "record_success"
        self.assertEqual(fixture.trace.mock_calls, [getattr(call, name)(CHANNEL)])
        self.assertEqual(fixture.health.recent_state(CHANNEL), "failed" if rejected else "succeeded")
        self.assert_count(fixture, count)
        availability = team_availability(fixture.rt)
        self.assertEqual(availability["all_available"], not rejected)
        expected = ("on_leave", "circuit_open") if count >= 2 else (
            ("unverified", "recent_failure") if rejected else ("available", "recent_success"))
        self.assertEqual({(member["status"], member["reason"]) for member in availability["members"]}, {expected})

    def assert_reported(self, fixture, tokens=7):
        receipt, = self.events(fixture, "team.model_called")
        self.assertEqual(receipt.payload["tokens"], tokens)
        self.assertEqual(receipt.payload["token_usage"], "reported")
        self.assertEqual(fixture.rt._reported_driver_tokens(CHANNEL), tokens)
        self.assertEqual(fixture.rt.task(fixture.task).tokens, tokens)

    def assert_no_observation(self, fixture, prior):
        self.assertEqual(fixture.trace.mock_calls, [])
        self.assert_count(fixture, 2 if prior == "circuit_open" else 0)
        self.assertEqual(fixture.health.recent_state(CHANNEL), "failed" if prior == "circuit_open" else "succeeded")

    def test_bad_text_or_calls_reject_without_clearing_circuit_or_losing_usage(self):
        for field, value, error in (("text", None, "AttributeError"),
                                    ("calls", None, "TypeError")):
            for prior, count in (("circuit_open", 2), ("succeeded", 0)):
                with self.subTest(field=field, prior=prior), self.session(
                        duck(**{field: value}), prior=prior) as f:
                    finished = self.finish(f, "failed")
                    self.assertEqual(finished["error_type"], error)
                    self.assertEqual(finished["failure_stage"], "response_handling")
                    self.assert_observation(f, rejected=True, count=count)
                    self.assert_reported(f)
                    self.assertEqual(f.driver.requests, 1)
                    self.assertEqual(self.events(f, "team.tool_started"), [])

    def test_reported_bad_toolturn_text_keeps_the_original_model_ledger(self):
        result = ToolTurn(text=None, calls=[], tokens=7, meta={"token_usage": "reported"})
        with self.session(result) as f:
            finished = self.finish(f, "failed")
            self.assertEqual(finished["error_type"], "AttributeError")
            self.assert_reported(f)
            self.assert_observation(f, rejected=True, count=2)
            self.assertLess(self.events(f, "team.model_called")[0].seq,
                            self.events(f, "team.run_finished")[0].seq)

    def test_missing_return_fields_reject_previous_success_and_keep_original_error(self):
        cases = [("none", None), ("mapping", {})]
        for field in ("meta", "tokens", "text", "calls"):
            result = duck()
            delattr(result, field)
            cases.append(("missing_" + field, result))
        for label, result in cases:
            for prior, count in (("succeeded", 0), ("circuit_open", 2)):
                with self.subTest(result=label, prior=prior), self.session(result, prior=prior) as f:
                    finished = self.finish(f, "failed")
                    self.assertEqual(finished["error_type"], "AttributeError")
                    self.assert_observation(f, rejected=True, count=count)
                    expected_receipts = int(label in {"missing_text", "missing_calls"})
                    self.assertEqual(len(self.events(f, "team.model_called")), expected_receipts)
                    self.assertEqual(f.rt._reported_driver_tokens(CHANNEL), 7 * expected_receipts)
                    self.assertEqual(f.driver.requests, 1)

    def test_bad_meta_rejects_without_changing_legacy_truthiness_handling(self):
        for meta in ([1], "invalid", 1, [], "", 0, False):
            for prior, count in (("succeeded", 0), ("circuit_open", 2)):
                with self.subTest(meta=meta, prior=prior), self.session(duck(meta=meta), prior=prior) as f:
                    finished = self.finish(f, "failed" if meta else "completed")
                    self.assert_observation(f, rejected=True, count=count)
                    if meta:
                        self.assertEqual(finished["error_type"], "AttributeError")
                        self.assertEqual(self.events(f, "team.model_called"), [])
                    else:
                        receipt, = self.events(f, "team.model_called")
                        self.assertEqual(receipt.payload["tokens"], 7)
                        self.assertEqual(receipt.payload["token_usage"], "unavailable")
                    self.assertEqual(f.rt._reported_driver_tokens(CHANNEL), 0)

    def test_legal_stored_duck_empty_turn_and_plain_meta_remain_successful(self):
        cases = (("duck", duck()), ("empty_turn", ToolTurn("", [])),
                 ("none_meta", duck(meta=None)),
                 ("tuple_calls", duck(calls=())),
                 ("invocation_flag_is_not_a_new_gate", duck(meta={"invocation_started": False})))
        for label, result in cases:
            with self.subTest(result=label), self.session(result) as f:
                self.finish(f, "completed")
                self.assert_observation(f, rejected=False, count=0)
                self.assertEqual(len(self.events(f, "team.model_called")), 1)
                self.assertEqual(f.driver.requests, 1)

    def test_legal_mapping_meta_keeps_host_success_and_usage_without_observation(self):
        for factory in (UserDict, MappingProxyType, DictMeta):
            for with_tool in (False, True):
                for prior in ("circuit_open", "succeeded"):
                    meta = factory({"token_usage": "reported"})
                    result = duck(meta=meta, text="" if with_tool else "Synthetic reply",
                        calls=[ToolCall("answer-1", "reply_to_customer", {"text": "Synthetic answer"})]
                              if with_tool else [])
                    with self.subTest(mapping=factory.__name__, tool=with_tool, prior=prior), self.session(
                            result, prior=prior) as f:
                        self.finish(f, "completed")
                        self.assert_reported(f)
                        self.assert_no_observation(f, prior)
                        self.assertEqual(f.driver.requests, 1)
                        self.assertEqual(len(self.events(f, "team.tool_started")), int(with_tool))
                        self.assertEqual(len(self.events(f, "team.tool_finished")), int(with_tool))
                        if with_tool:
                            self.assertEqual(self.events(f, "team.tool_finished")[0].payload["result"]["status"], "ok")

    def test_mapping_first_get_conflict_stays_at_original_host_read_without_execution(self):
        for with_tool in (False, True):
            for prior in ("circuit_open", "succeeded"):
                meta = FirstGetConflict()
                result = duck(meta=meta, text="" if with_tool else "Synthetic reply",
                    calls=[ToolCall("answer-1", "reply_to_customer", {"text": "Synthetic answer"})]
                          if with_tool else [])
                with self.subTest(tool=with_tool, prior=prior), self.session(result, prior=prior) as f:
                    finished = self.finish(f, "interrupted")
                    self.assertEqual(finished["reason"], "Synthetic original first Mapping.get conflict")
                    self.assertEqual(meta.reads, ["token_usage"])
                    self.assertEqual(self.events(f, "team.model_called"), [])
                    self.assertEqual(f.rt._reported_driver_tokens(CHANNEL), 0)
                    self.assertEqual(f.rt.task(f.task).tokens, 0)
                    self.assertEqual(self.events(f, "team.tool_started"), [])
                    self.assertEqual(self.events(f, "team.tool_finished"), [])
                    self.assert_no_observation(f, prior)
                    self.assertEqual(f.driver.requests, 1)

    def test_mapping_incomplete_get_cannot_consume_reported_usage_before_host(self):
        for with_tool in (False, True):
            for prior in ("circuit_open", "succeeded"):
                meta = InvalidatingGet()
                result = duck(meta=meta, text="" if with_tool else "Synthetic reply",
                    calls=[ToolCall("answer-1", "reply_to_customer", {"text": "Synthetic answer"})]
                          if with_tool else [])
                with self.subTest(tool=with_tool, prior=prior), self.session(result, prior=prior) as f:
                    self.finish(f, "completed")
                    self.assertEqual(meta.reads, ["token_usage", "usage", "contained"])
                    self.assertEqual(meta.data, {"token_usage": "reported"})
                    self.assert_reported(f)
                    self.assert_no_observation(f, prior)
                    self.assertEqual(len(self.events(f, "team.tool_started")), int(with_tool))
                    self.assertEqual(len(self.events(f, "team.tool_finished")), int(with_tool))
                    if with_tool:
                        self.assertEqual(self.events(f, "team.tool_finished")[0].payload["result"]["status"], "ok")
                    self.assertEqual(f.driver.requests, 1)

    def test_unknown_or_invalid_token_counts_do_not_make_a_connection_failure(self):
        for tokens in (None, "unknown", -1, True, 1.5, 0):
            for usage in ("unavailable", "reported"):
                with self.subTest(tokens=tokens, usage=usage), self.session(
                        duck(tokens=tokens, meta={"token_usage": usage})) as f:
                    self.finish(f, "completed")
                    self.assert_observation(f, rejected=False, count=0)
                    receipt, = self.events(f, "team.model_called")
                    self.assertEqual(receipt.payload["tokens"], 0)
                    self.assertEqual(receipt.payload["token_usage"], usage)
                    self.assertEqual(f.rt._reported_driver_tokens(CHANNEL), 0)

    def test_tuple_tool_calls_and_duck_call_items_keep_host_execution(self):
        result = duck(text="", calls=(SimpleNamespace(id="answer-1", name="reply_to_customer",
                      arguments={"text": "Synthetic final answer"}),))
        with self.session(result) as f:
            self.finish(f, "completed")
            self.assert_observation(f, rejected=False, count=0)
            self.assert_reported(f)
            tool, = self.events(f, "team.tool_finished")
            self.assertEqual(tool.payload["name"], "reply_to_customer")
            self.assertEqual(tool.payload["result"]["status"], "ok")

    def test_missing_call_fields_reject_and_dynamic_reads_preserve_observation_and_usage(self):
        for field in ("id", "name", "arguments"):
            for unreadable in (False, True):
                fields = {"id": "read-1", "name": "read_messages", "arguments": {"limit": 1}}
                if unreadable:
                    item = Unreadable(**fields, blocked_field=field,
                                      read_error=RuntimeError("Synthetic unreadable call field"))
                else:
                    fields.pop(field)
                    item = SimpleNamespace(**fields)
                for prior, count, state in (("circuit_open", 2, "failed"), ("succeeded", 0, "succeeded")):
                    with self.subTest(field=field, unreadable=unreadable, prior=prior), self.session(
                            duck(calls=[item]), prior=prior) as f:
                        finished = self.finish(f, "failed")
                        self.assertEqual(finished["error_type"], "RuntimeError" if unreadable else "AttributeError")
                        self.assertEqual(finished["failure_stage"],
                                         "response_handling" if field == "name" else "tool_dispatch")
                        if unreadable:
                            self.assertEqual(f.trace.mock_calls, [])
                            self.assert_count(f, count)
                            self.assertEqual(f.health.recent_state(CHANNEL), state)
                        else:
                            self.assert_observation(f, rejected=True, count=count)
                        self.assert_reported(f)
                        self.assertEqual(f.driver.requests, 1)

    def test_observation_does_not_revalidate_tool_names_or_argument_contents(self):
        for name, arguments in (("not_registered", {}), ("invalid tool name", {}),
                                ("read_messages", {"unexpected_field": "synthetic"})):
            result = duck(text="", calls=[ToolCall("read-1", name, arguments)])
            with self.subTest(name=name), self.session(result, duck()) as f:
                self.finish(f, "completed" if name == "read_messages" else "failed")
                expected = 2 if name == "read_messages" else 1
                self.assertEqual(f.trace.mock_calls, [call.record_success(CHANNEL)] * expected)
                self.assert_count(f, 0)
                self.assertEqual(f.health.recent_state(CHANNEL), "succeeded")
                self.assertEqual(f.rt._reported_driver_tokens(CHANNEL), 7 * expected)
                if name == "read_messages":
                    tool, = self.events(f, "team.tool_finished")
                    self.assertEqual(tool.payload["result"]["code"], "invalid_arguments")

    def test_unusable_observation_does_not_become_a_new_execution_gate(self):
        result = duck(text="", meta={"token_usage": "reported", "incomplete": True},
            calls=[ToolCall("answer-1", "reply_to_customer", {"text": "Synthetic answer"})])
        with self.session(result) as f:
            self.finish(f, "completed")
            self.assert_observation(f, rejected=True, count=2)
            self.assert_reported(f)
            tool, = self.events(f, "team.tool_finished")
            self.assertEqual(tool.payload["result"]["status"], "ok")

    def test_work_conflict_while_reading_return_or_call_fields_adds_no_observation(self):
        for access in ("override", "property", "getattr"):
            for field in ("text", "calls", "meta", "tokens", "message", "id", "name", "arguments"):
                conflict = WorkConflict("Synthetic original read conflict: " + field)
                if field in {"id", "name", "arguments"}:
                    item = unreadable_field({"id": "read-1", "name": "read_messages", "arguments": {}},
                                            field, conflict, access)
                    result = duck(calls=[item])
                else:
                    fields = vars(duck()).copy()
                    if field == "message":
                        fields["calls"] = [ToolCall("read-1", "read_messages", {})]
                    result = unreadable_field(fields, field, conflict, access)
                for prior, count, state in (("circuit_open", 2, "failed"), ("succeeded", 0, "succeeded")):
                    with self.subTest(access=access, field=field, prior=prior), self.session(result, prior=prior) as f:
                        finished = self.finish(f, "interrupted")
                        self.assertEqual(finished["reason"], str(conflict))
                        self.assertEqual(f.trace.mock_calls, [])
                        self.assert_count(f, count)
                        self.assertEqual(f.health.recent_state(CHANNEL), state)
                        self.assertEqual(self.events(f, "team.tool_started"), [])
                        self.assertEqual(self.events(f, "team.tool_finished"), [])
                        if field in {"meta", "tokens"}:
                            self.assertEqual(self.events(f, "team.model_called"), [])
                            self.assertEqual(f.rt._reported_driver_tokens(CHANNEL), 0)
                            self.assertEqual(f.rt.task(f.task).tokens, 0)
                        else:
                            self.assert_reported(f)

    def test_legal_dynamic_text_is_read_only_after_the_original_model_receipt(self):
        for access in ("override", "property", "getattr"):
            stages = []

            def read_text(self):
                stages.append(len(self_test.events(f, "team.model_called")))
                return "Synthetic dynamic reply"

            def read_attribute(self, name):
                if name == "text":
                    return read_text(self)
                return SimpleNamespace.__getattribute__(self, name)

            def missing_attribute(self, name):
                if name == "text":
                    return read_text(self)
                raise AttributeError(name)

            self_test = self
            attributes = ({"__getattribute__": read_attribute} if access == "override" else
                          {"text": property(read_text)} if access == "property" else
                          {"__getattr__": missing_attribute})
            fields = vars(duck()).copy()
            fields.pop("text")
            result = type("DynamicText", (SimpleNamespace,), attributes)(**fields)
            with self.subTest(access=access), self.session(result) as f:
                self.finish(f, "completed")
                self.assertTrue(stages)
                self.assertEqual(set(stages), {1})
                self.assert_reported(f)
                self.assertEqual(f.trace.mock_calls, [])
                self.assert_count(f, 2)
                self.assertEqual(f.health.recent_state(CHANNEL), "failed")

    def test_host_log_failure_keeps_the_actual_return_observation(self):
        for rejected in (False, True):
            result = duck(text=None) if rejected else duck()
            with self.subTest(rejected=rejected), self.session(result) as f:
                original = f.rt.log.emit

                def fail_model_receipt(**kwargs):
                    if kwargs["type"] == "team.model_called":
                        raise OSError("Synthetic model receipt storage failure")
                    return original(**kwargs)

                with patch.object(f.rt.log, "emit", side_effect=fail_model_receipt):
                    finished = self.finish(f, "failed")
                self.assertEqual(finished["error_type"], "OSError")
                self.assert_observation(f, rejected=rejected, count=2 if rejected else 0)
                self.assertEqual(self.events(f, "team.model_called"), [])
                self.assertEqual(f.rt._reported_driver_tokens(CHANNEL), 0)


if __name__ == "__main__":
    unittest.main()
