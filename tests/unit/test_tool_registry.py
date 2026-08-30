from __future__ import annotations

import json
import math

import pytest

from agentic_debugger.agent.controller_policy import ActionName
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import (
    MAX_TOOL_ARGUMENT_BYTES,
    MAX_TOOL_DIAGNOSTIC_BYTES,
    MAX_TOOL_JSON_DEPTH,
    MAX_TOOL_JSON_NODES,
    MAX_TOOL_RESULT_PAYLOAD_BYTES,
    MAX_TOOL_SUMMARY_BYTES,
    DuplicateToolError,
    ToolDispatchReason,
    ToolExecutionError,
    ToolRejectedError,
    ToolRegistry,
    ToolRegistryError,
    ToolResult,
    ToolSpec,
    ToolTimeoutError,
)
from agentic_debugger.events.schema import (
    Action,
    Observation,
    ObservationStatus,
)


def action(
    *,
    name: str = ActionName.RUN_TESTS.value,
    state: ControllerState = ControllerState.REPRODUCE,
    arguments: dict[str, object] | object | None = None,
) -> Action:
    if arguments is None:
        arguments = {"argv": ["pytest"]}
    return Action(
        action_id="act-1",
        run_id="run-1",
        task_id="task-1",
        state=state,
        name=name,
        arguments=arguments,  # type: ignore[arg-type]
    )


def spec(
    name: ActionName = ActionName.RUN_TESTS,
    validator=None,
    handler=None,
    version: str = "1",
) -> ToolSpec:
    return ToolSpec(
        name,
        validator or (lambda args: args),
        handler or (
            lambda _action, _args: ToolResult(
                ObservationStatus.OK, {"value": 1}, "done"
            )
        ),
        version,
    )


def registry_for(action_name: ActionName = ActionName.RUN_TESTS, **kwargs):
    return ToolRegistry((spec(action_name, **kwargs),))


class DictSubclass(dict):
    def items(self):
        raise AssertionError("hostile items hook")


class ListSubclass(list):
    def __iter__(self):
        raise AssertionError("hostile iterator hook")


class HostileIterable:
    def __iter__(self):
        raise AssertionError("must not iterate")


class HostileScalar:
    def __str__(self):
        raise AssertionError("must not stringify")

    def __repr__(self):
        raise AssertionError("must not repr")


@pytest.mark.parametrize("status", list(ObservationStatus))
def test_tool_result_accepts_exact_statuses(status):
    result = ToolResult(status, {"ok": True}, "summary")
    assert result.status is status


def test_tool_result_detaches_payload_and_rejects_invalid_shapes():
    source = {"nested": [1]}
    result = ToolResult(ObservationStatus.OK, source, "summary")
    source["nested"].append(2)
    assert result.payload == {"nested": [1]}

    invalid = [
        "ok",
        DictSubclass(),
        {1: "bad"},
        {"x": HostileScalar()},
        {"x": ()},
        {"x": set()},
        {"x": b"x"},
        {"x": math.nan},
        {"x": math.inf},
        {"x": -math.inf},
    ]
    for payload in invalid:
        with pytest.raises(ValueError):
            ToolResult(ObservationStatus.OK, payload, "summary")  # type: ignore[arg-type]


def test_tool_result_rejects_cycles_depth_nodes_and_payload_bytes():
    cyclic_list = []
    cyclic_list.append(cyclic_list)
    cyclic_dict = {}
    cyclic_dict["self"] = cyclic_dict
    with pytest.raises(ValueError):
        ToolResult(ObservationStatus.OK, {"x": cyclic_list}, "summary")
    with pytest.raises(ValueError):
        ToolResult(ObservationStatus.OK, cyclic_dict, "summary")

    value: object = "leaf"
    for _ in range(MAX_TOOL_JSON_DEPTH - 3):
        value = [value]
    ToolResult(ObservationStatus.OK, {"x": value}, "summary")
    value = "leaf"
    for _ in range(MAX_TOOL_JSON_DEPTH - 1):
        value = [value]
    with pytest.raises(ValueError):
        ToolResult(ObservationStatus.OK, {"x": value}, "summary")

    with pytest.raises(ValueError):
        ToolResult(
            ObservationStatus.OK,
            {str(i): i for i in range(MAX_TOOL_JSON_NODES)},
            "summary",
        )
    with pytest.raises(ValueError):
        ToolResult(ObservationStatus.OK, {"x": "é" * MAX_TOOL_RESULT_PAYLOAD_BYTES}, "summary")


@pytest.mark.parametrize(
    "summary",
    ["", " surrounded ", "line\nfeed", "delete\x7f", "é" * MAX_TOOL_SUMMARY_BYTES],
)
def test_tool_result_rejects_invalid_summary(summary):
    with pytest.raises(ValueError):
        ToolResult(ObservationStatus.OK, {}, summary)


def test_tool_result_requires_exact_bool_truncated():
    with pytest.raises(ValueError):
        ToolResult(ObservationStatus.OK, {}, "summary", 1)  # type: ignore[arg-type]


def test_tool_spec_and_registry_are_strict_and_immutable():
    calls = []
    valid = ToolSpec(
        ActionName.RUN_TESTS,
        lambda args: calls.append("validator") or args,
        lambda _action, _args: calls.append("handler") or ToolResult(
            ObservationStatus.OK, {}, "ok"
        ),
        "v1-test",
    )
    assert calls == []
    with pytest.raises(ValueError):
        ToolSpec("run_tests", valid.argument_validator, valid.handler)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ToolSpec(ActionName.RUN_TESTS, object(), valid.handler)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ToolSpec(ActionName.RUN_TESTS, valid.argument_validator, object())  # type: ignore[arg-type]
    for version in ["", " v1", "v1 ", "v/1", "é" * 65]:
        with pytest.raises(ValueError):
            ToolSpec(ActionName.RUN_TESTS, valid.argument_validator, valid.handler, version)

    registry = ToolRegistry((valid,))
    assert registry.names() == (ActionName.RUN_TESTS,)
    assert registry.get(ActionName.RUN_TESTS) is valid
    with pytest.raises(ValueError):
        registry.get("run_tests")  # type: ignore[arg-type]
    with pytest.raises(ToolRegistryError):
        registry.get(ActionName.SEARCH_CODE)
    with pytest.raises(DuplicateToolError):
        ToolRegistry((valid, valid))
    with pytest.raises(DuplicateToolError):
        registry.register(valid)
    with pytest.raises(ValueError):
        ToolRegistry([valid])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ToolRegistry((object(),))  # type: ignore[arg-type]
    extended = registry.register(spec(ActionName.GET_FAILURE_TRACE))
    assert registry.names() == (ActionName.RUN_TESTS,)
    assert extended.names() == (ActionName.RUN_TESTS, ActionName.GET_FAILURE_TRACE)


def test_tool_contracts_are_fully_detached_at_constructor_and_accessor_boundaries():
    source_contract = {
        "required": ["value"],
        "properties": {
            "value": {
                "type": "string",
                "enum": ["first", "second"],
            }
        },
        "additional_properties": False,
    }
    subject = ToolSpec(
        ActionName.RUN_TESTS,
        lambda args: args,
        lambda _action, _args: ToolResult(ObservationStatus.OK, {}, "ok"),
        argument_contract=source_contract,
    )

    source_contract["properties"]["value"]["enum"].append("caller-mutated")
    source_contract["required"].append("caller-mutated")
    assert subject.argument_contract == {
        "required": ["value"],
        "properties": {
            "value": {"type": "string", "enum": ["first", "second"]}
        },
        "additional_properties": False,
    }

    registry = ToolRegistry((subject,))
    first = registry.argument_contracts()
    first[ActionName.RUN_TESTS.value]["properties"]["value"]["enum"].append(
        "accessor-mutated"
    )
    first[ActionName.RUN_TESTS.value]["required"].append("accessor-mutated")
    second = registry.argument_contracts()

    assert second == {
        ActionName.RUN_TESTS.value: {
            "required": ["value"],
            "properties": {
                "value": {"type": "string", "enum": ["first", "second"]}
            },
            "additional_properties": False,
        }
    }
    assert subject.argument_contract == second[ActionName.RUN_TESTS.value]


def test_dispatch_success_detaches_at_each_boundary_and_correlates():
    original = {"nested": [1]}
    seen = {}

    def validate(arguments):
        seen["validator"] = arguments
        arguments["mutated"] = True
        return {"validated": arguments["nested"]}

    def handle(received_action, arguments):
        seen["action"] = received_action
        seen["handler"] = arguments
        arguments["handler_mutation"] = True
        return ToolResult(
            ObservationStatus.TIMEOUT,
            {"first": 1, "second": [2]},
            "bounded summary",
            True,
        )

    subject = action(arguments=original)
    registry = registry_for(validator=validate, handler=handle)
    before = registry.names()
    observation = registry.dispatch(subject, observation_id="obs-exact")
    original["nested"].append(9)
    assert seen["action"] is subject
    assert seen["validator"] is not subject.arguments
    assert seen["handler"] is not seen["validator"]
    assert "mutated" not in subject.arguments
    assert "handler_mutation" not in seen["validator"]
    assert observation.observation_id == "obs-exact"
    assert observation.action_id == subject.action_id
    assert observation.run_id == subject.run_id
    assert observation.task_id == subject.task_id
    assert observation.name == subject.name
    assert observation.status is ObservationStatus.TIMEOUT
    assert observation.summary == "bounded summary"
    assert observation.truncated is True
    assert observation.payload == {
        "first": 1, "second": [2], "dispatch_reason": "ok"
    }
    assert Observation.from_mapping(observation.to_mapping()) == observation
    assert registry.names() == before


def test_dispatch_is_deterministic_for_pure_handler():
    registry = registry_for()
    first = registry.dispatch(action(), observation_id="obs-1").to_mapping()
    second = registry.dispatch(action(), observation_id="obs-2").to_mapping()
    first["observation_id"] = "obs-2"
    assert first == second


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (action(name="unknown"), ToolDispatchReason.UNKNOWN_ACTION),
        (
            action(name=ActionName.RUN_TESTS.value, state=ControllerState.DONE),
            ToolDispatchReason.STATE_ACTION_NOT_ALLOWED,
        ),
        (
            action(name=ActionName.RUN_REPRODUCTION.value),
            ToolDispatchReason.TOOL_NOT_REGISTERED,
        ),
    ],
)
def test_dispatch_rejection_precedence_before_validator(candidate, reason):
    calls = []
    subject = ToolRegistry((spec(handler=lambda *_: calls.append("handler")),))
    observation = subject.dispatch(candidate, observation_id="obs")
    assert observation.status is ObservationStatus.REJECTED
    assert observation.payload == {"dispatch_reason": reason.value}
    assert calls == []


def test_dispatch_rejects_raw_and_validated_arguments_without_handler():
    calls = []
    validator = lambda args: calls.append("validator") or args
    handler = lambda *_: calls.append("handler")
    registry = registry_for(validator=validator, handler=handler)
    cyclic = {}
    cyclic["self"] = cyclic
    observation = registry.dispatch(action(arguments=cyclic), observation_id="obs")
    assert observation.payload["dispatch_reason"] == "invalid_arguments"
    assert calls == []

    def bad_validator(_args):
        calls.append("validator")
        raise RuntimeError("secret validator message")

    observation = registry_for(validator=bad_validator, handler=handler).dispatch(
        action(), observation_id="obs"
    )
    assert observation.payload["dispatch_reason"] == "invalid_arguments"
    assert calls[-1] == "validator"
    assert calls.count("handler") == 0

    observation = registry_for(validator=lambda _args: [], handler=handler).dispatch(
        action(), observation_id="obs"
    )
    assert observation.payload["dispatch_reason"] == "invalid_arguments"
    assert calls.count("handler") == 0


@pytest.mark.parametrize(
    ("error", "status", "reason"),
    [
        (ToolRejectedError("secret rejected"), ObservationStatus.REJECTED, ToolDispatchReason.TOOL_REJECTED),
        (ToolTimeoutError("secret timeout"), ObservationStatus.TIMEOUT, ToolDispatchReason.TOOL_TIMEOUT),
        (ToolExecutionError("secret execution"), ObservationStatus.ERROR, ToolDispatchReason.TOOL_ERROR),
        (RuntimeError("secret runtime"), ObservationStatus.ERROR, ToolDispatchReason.TOOL_ERROR),
    ],
)
def test_handler_exception_translation_is_bounded(error, status, reason):
    calls = []

    def handle(*_):
        calls.append("handler")
        raise error

    observation = registry_for(handler=handle).dispatch(action(), observation_id="obs")
    assert calls == ["handler"]
    assert observation.status is status
    assert observation.payload == {"dispatch_reason": reason.value}
    assert "secret" not in observation.summary
    assert type(error).__name__ not in observation.summary
    assert "traceback" not in observation.to_mapping()


def test_explicit_safe_rejection_diagnostic_is_bounded_redacted_and_actionable():
    observation = registry_for(
        handler=lambda *_: (_ for _ in ()).throw(
            ToolRejectedError(
                "internal detail",
                safe_diagnostic="phase must be baseline or post_patch; token=secret-value" + "x" * 6000,
            )
        )
    ).dispatch(action(), observation_id="obs")
    assert observation.payload["dispatch_reason"] == ToolDispatchReason.TOOL_REJECTED.value
    assert observation.payload["diagnostic"].startswith("phase must be baseline or post_patch")
    assert "secret-value" not in observation.to_mapping().__str__()
    assert len(observation.payload["diagnostic"].encode("utf-8")) <= MAX_TOOL_DIAGNOSTIC_BYTES


@pytest.mark.parametrize("result", [None, {}, object()])
def test_invalid_handler_results_are_generic(result):
    observation = registry_for(handler=lambda *_: result).dispatch(
        action(), observation_id="obs"
    )
    assert observation.status is ObservationStatus.ERROR
    assert observation.payload == {
        "dispatch_reason": "invalid_tool_result"
    }
    assert observation.summary == "Tool returned an invalid result."


def test_reserved_result_payload_and_hostile_result_are_invalid():
    def handle(*_):
        return ToolResult(
            ObservationStatus.OK, {"dispatch_reason": "spoofed"}, "spoof"
        )

    observation = registry_for(handler=handle).dispatch(action(), observation_id="obs")
    assert observation.payload == {"dispatch_reason": "invalid_tool_result"}


@pytest.mark.parametrize("bad_id", ["", None, 1])
def test_observation_id_preflight_precedes_tools(bad_id):
    calls = []
    registry = registry_for(
        validator=lambda args: calls.append("validator") or args,
        handler=lambda *_: calls.append("handler"),
    )
    with pytest.raises(ToolRegistryError):
        registry.dispatch(action(), observation_id=bad_id)  # type: ignore[arg-type]
    assert calls == []


def test_process_control_exceptions_propagate():
    for error in (KeyboardInterrupt, SystemExit, GeneratorExit):
        def handle(*_, error=error):
            raise error

        with pytest.raises(error):
            registry_for(handler=handle).dispatch(action(), observation_id="obs")


def test_hostile_containers_are_rejected_without_hooks_or_stringification():
    for arguments in [
        DictSubclass(x=1),
        ListSubclass([1]),
        {"x": HostileIterable()},
        {"x": HostileScalar()},
    ]:
        observation = registry_for().dispatch(
            action(arguments=arguments), observation_id="obs"
        )
        assert observation.payload["dispatch_reason"] == "invalid_arguments"


def test_shared_json_references_are_copied_independently():
    shared = [1]
    source = {"a": shared, "b": shared}
    result = ToolResult(ObservationStatus.OK, source, "ok")
    assert result.payload["a"] == result.payload["b"]
    assert result.payload["a"] is not result.payload["b"]
    assert result.payload["a"] is not shared


def test_action_name_and_reason_enums_have_no_aliases():
    assert len(ToolDispatchReason.__members__) == len(ToolDispatchReason)
    assert ToolDispatchReason.OK.value == "ok"


class HostileMapping:
    def __init__(self, counts): self.counts = counts
    def __contains__(self, _value): self.counts["contains"] += 1; raise AssertionError
    def __iter__(self): self.counts["iter"] += 1; raise AssertionError
    def items(self): self.counts["items"] += 1; raise AssertionError
    def keys(self): self.counts["keys"] += 1; raise AssertionError
    def __getitem__(self, _key): self.counts["getitem"] += 1; raise AssertionError


class HostileDictSubclass(dict):
    def __contains__(self, _value): self.counts["contains"] += 1; raise AssertionError
    def items(self): self.counts["items"] += 1; raise AssertionError
    def keys(self): self.counts["keys"] += 1; raise AssertionError
    def values(self): self.counts["values"] += 1; raise AssertionError
    def __iter__(self): self.counts["iter"] += 1; raise AssertionError
    def __getitem__(self, _key): self.counts["getitem"] += 1; raise AssertionError


class CollidingKey:
    def __init__(self, counts): self.counts = counts; self.active = False
    def __hash__(self):
        if self.active: self.counts["hash"] += 1; raise AssertionError
        return hash("dispatch_reason")
    def __eq__(self, _other):
        if self.active: self.counts["eq"] += 1; raise AssertionError
        return False


class HostileStringKey(str):
    def __new__(cls, value, counts):
        instance = super().__new__(cls, value); instance.counts = counts; instance.active = False; return instance
    def __hash__(self):
        if self.active: self.counts["hash"] += 1; raise AssertionError
        return super().__hash__()
    def __eq__(self, _other):
        if self.active: self.counts["eq"] += 1; raise AssertionError
        return super().__eq__(_other)


def forged_result(payload):
    result = ToolResult(ObservationStatus.OK, {}, "ok")
    object.__setattr__(result, "payload", payload)
    return result


@pytest.mark.parametrize("kind", ["mapping", "dict_subclass"])
def test_forged_untrusted_payloads_are_detached_before_any_hook(kind):
    counts = {key: 0 for key in ("contains", "iter", "items", "keys", "values", "getitem")}
    if kind == "mapping":
        payload = HostileMapping(counts)
    else:
        payload = HostileDictSubclass({"value": 1})
        payload.counts = counts
    calls = {"handler": 0}
    def handle(*_): calls["handler"] += 1; return forged_result(payload)
    registry = registry_for(handler=handle)
    before = registry.names()
    observation = registry.dispatch(action(), observation_id="forged")
    assert observation.status is ObservationStatus.ERROR
    assert observation.payload == {"dispatch_reason": "invalid_tool_result"}
    assert observation.summary == "Tool returned an invalid result."
    assert calls["handler"] == 1
    assert counts == {key: 0 for key in counts}
    assert registry.names() == before


def test_forged_exact_dict_hostile_keys_are_rejected_without_key_hooks():
    for key in (CollidingKey({"hash": 0, "eq": 0}), HostileStringKey("dispatch_reason", {"hash": 0, "eq": 0})):
        counts = key.counts
        payload = {key: 1}
        key.active = True
        calls = {"handler": 0}
        def handle(*_, payload=payload):
            calls["handler"] += 1
            return forged_result(payload) if calls["handler"] == 1 else ToolResult(ObservationStatus.OK, {"ok": True}, "ok")
        registry = registry_for(handler=handle)
        observation = registry.dispatch(action(), observation_id="forged-key")
        assert observation.status is ObservationStatus.ERROR
        assert observation.payload == {"dispatch_reason": "invalid_tool_result"}
        assert calls["handler"] == 1
        assert counts == {"hash": 0, "eq": 0}
        assert registry.dispatch(action(), observation_id="later").status is ObservationStatus.OK


def compact_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def payload_for_final_bytes(target, character):
    low, high = 0, target
    while low <= high:
        middle = (low + high) // 2
        candidate = {"data": character * middle}
        final = dict(candidate); final["dispatch_reason"] = "ok"
        if compact_bytes(final) <= target: low = middle + 1
        else: high = middle - 1
    candidate = {"data": character * high}
    final = dict(candidate); final["dispatch_reason"] = "ok"
    return candidate, final


@pytest.mark.parametrize("character", ["a", "\u00e9"])
def test_final_payload_byte_boundary_includes_dispatch_reason(character):
    payload, final = payload_for_final_bytes(65536, character)
    accepted = registry_for(handler=lambda *_: forged_result(payload)).dispatch(action(), observation_id="bytes-ok")
    assert compact_bytes(accepted.payload) == 65536
    assert accepted.status is ObservationStatus.OK
    too_large = dict(payload); too_large["data"] += "a"
    rejected = registry_for(handler=lambda *_: forged_result(too_large)).dispatch(action(), observation_id="bytes-bad")
    assert compact_bytes(dict(too_large, dispatch_reason="ok")) == 65537
    assert rejected.status is ObservationStatus.ERROR
    assert rejected.payload == {"dispatch_reason": "invalid_tool_result"}


@pytest.mark.parametrize("count", [4094, 4095])
def test_final_payload_node_boundary_counts_dispatch_reason(count):
    payload = {f"key-{index}": index for index in range(count)}
    observation = registry_for(handler=lambda *_: forged_result(payload)).dispatch(action(), observation_id=f"nodes-{count}")
    if count == 4094:
        assert observation.status is ObservationStatus.OK
        assert count + 2 == 4096
    else:
        assert observation.status is ObservationStatus.ERROR
        assert observation.payload == {"dispatch_reason": "invalid_tool_result"}


def test_forged_overdepth_result_and_reserved_key_have_no_partial_payload():
    nested = "leaf"
    for _ in range(MAX_TOOL_JSON_DEPTH): nested = [nested]
    overdepth = registry_for(handler=lambda *_: forged_result({"nested": nested})).dispatch(action(), observation_id="depth")
    assert overdepth.status is ObservationStatus.ERROR
    assert overdepth.payload == {"dispatch_reason": "invalid_tool_result"}
    reserved = registry_for(handler=lambda *_: forged_result({"dispatch_reason": "spoof"})).dispatch(action(), observation_id="reserved")
    assert reserved.status is ObservationStatus.ERROR
    assert reserved.payload == {"dispatch_reason": "invalid_tool_result"}


def test_invalid_result_followed_by_valid_dispatch_succeeds():
    results = [forged_result({"dispatch_reason": "spoof"}), ToolResult(ObservationStatus.OK, {"value": 1}, "ok")]
    def handle(*_): return results.pop(0)
    registry = registry_for(handler=handle)
    invalid = registry.dispatch(action(), observation_id="invalid")
    valid = registry.dispatch(action(), observation_id="valid")
    assert invalid.payload == {"dispatch_reason": "invalid_tool_result"}
    assert valid.status is ObservationStatus.OK
    assert valid.payload == {"value": 1, "dispatch_reason": "ok"}


def test_tool_rejected_error_with_oversized_payload_data_is_strictly_bounded():
    oversized_data = {
        "huge_blob": "x" * 150_000,
        "applied": False,
        "error": "context_mismatch",
        "recoverable": True,
        "patch_failure": {
            "kind": "context_mismatch",
            "recoverable": True,
            "path": "module.py",
            "line_number": 42,
            "hunk_index": 1,
            "expected": "old line",
            "actual": "actual line",
            "current_source_window": " 42 | source line",
        },
    }

    def handle(*_):
        raise ToolRejectedError(
            "context mismatch",
            safe_diagnostic="context mismatch at module.py:42",
            recoverable=True,
            payload_data=oversized_data,
        )

    registry = registry_for(handler=handle)
    obs = registry.dispatch(action(), observation_id="obs-oversized-rejected")

    assert obs.status is ObservationStatus.REJECTED
    serialized = json.dumps(obs.payload, ensure_ascii=False)
    assert len(serialized.encode("utf-8")) <= MAX_TOOL_RESULT_PAYLOAD_BYTES
    assert obs.payload["dispatch_reason"] == ToolDispatchReason.TOOL_REJECTED.value
    assert obs.payload.get("recoverable") is True
    assert obs.payload.get("applied") is False
    assert obs.payload.get("error") == "context_mismatch"
    pf = obs.payload.get("patch_failure", {})
    assert pf.get("kind") == "context_mismatch"
    assert pf.get("recoverable") is True
    assert pf.get("path") == "module.py"
    assert pf.get("line_number") == 42
    assert pf.get("hunk_index") == 1


def test_tool_execution_error_with_oversized_payload_data_is_strictly_bounded():
    oversized_data = {
        "huge_output": "y" * 150_000,
        "applied": False,
        "error": "apply_error",
        "recoverable": False,
        "patch_failure": {
            "kind": "write_failure",
            "recoverable": False,
            "path": "target.py",
        },
    }

    def handle(*_):
        raise ToolExecutionError(
            "write failure",
            safe_diagnostic="write failure at target.py",
            recoverable=False,
            payload_data=oversized_data,
        )

    registry = registry_for(handler=handle)
    obs = registry.dispatch(action(), observation_id="obs-oversized-exec")

    assert obs.status is ObservationStatus.ERROR
    serialized = json.dumps(obs.payload, ensure_ascii=False)
    assert len(serialized.encode("utf-8")) <= MAX_TOOL_RESULT_PAYLOAD_BYTES
    assert obs.payload["dispatch_reason"] == ToolDispatchReason.TOOL_ERROR.value
    assert obs.payload.get("recoverable") is False
    assert obs.payload.get("applied") is False
    assert obs.payload.get("error") == "apply_error"
    pf = obs.payload.get("patch_failure", {})
    assert pf.get("kind") == "write_failure"
    assert pf.get("recoverable") is False
    assert pf.get("path") == "target.py"


def test_oversized_path_error_kind_fields_cannot_escape_fallback_handling():
    adversarial_data = {
        "error": "E" * 100_000,
        "applied": False,
        "recoverable": False,
        "patch_failure": {
            "kind": "K" * 100_000,
            "path": "P" * 100_000,
            "line_number": 99,
            "recoverable": False,
            "expected": "EX" * 50_000,
            "actual": "AC" * 50_000,
            "current_source_window": "CW" * 50_000,
        },
    }

    def handle(*_):
        raise ToolRejectedError(
            "adversarial rejection",
            safe_diagnostic="adversarial error",
            recoverable=False,
            payload_data=adversarial_data,
        )

    registry = registry_for(handler=handle)
    obs = registry.dispatch(action(), observation_id="obs-adversarial")

    assert obs.status is ObservationStatus.REJECTED
    serialized = json.dumps(obs.payload, ensure_ascii=False)
    assert len(serialized.encode("utf-8")) <= MAX_TOOL_RESULT_PAYLOAD_BYTES
    assert obs.payload["dispatch_reason"] == ToolDispatchReason.TOOL_REJECTED.value
    assert obs.payload.get("recoverable") is False

    err = obs.payload.get("error")
    if err is not None:
        assert len(str(err).encode("utf-8")) <= 200

    pf = obs.payload.get("patch_failure", {})
    assert pf.get("recoverable") is False
    if "kind" in pf:
        assert len(str(pf["kind"]).encode("utf-8")) <= 100
    if "path" in pf:
        assert len(str(pf["path"]).encode("utf-8")) <= 500


def test_json_invalid_and_overdepth_payload_data_degrades_safely():
    cyclic: dict[str, object] = {"a": 1}
    cyclic["self"] = cyclic

    def handle_cyclic(*_):
        raise ToolRejectedError(
            "cyclic failure",
            safe_diagnostic="cyclic diag",
            recoverable=True,
            payload_data={
                "bad_cycle": cyclic,
                "nan_val": math.nan,
                "inf_val": math.inf,
                "error": "validation_error",
                "patch_failure": {
                    "kind": "validation_error",
                    "path": "valid_path.py",
                    "line_number": 5,
                    "recoverable": True,
                },
            },
        )

    registry = registry_for(handler=handle_cyclic)
    obs = registry.dispatch(action(), observation_id="obs-cyclic")

    assert obs.status is ObservationStatus.REJECTED
    # Must be valid json without exceptions
    serialized = json.dumps(obs.payload, ensure_ascii=False)
    assert len(serialized.encode("utf-8")) <= MAX_TOOL_RESULT_PAYLOAD_BYTES
    assert obs.payload["dispatch_reason"] == ToolDispatchReason.TOOL_REJECTED.value
    assert obs.payload.get("recoverable") is True
    pf = obs.payload.get("patch_failure", {})
    assert pf.get("kind") == "validation_error"
    assert pf.get("path") == "valid_path.py"
    assert pf.get("line_number") == 5


def test_essential_fatal_and_recoverable_disposition_is_preserved():
    for rec_state in (True, False):
        def handle(*_, r=rec_state):
            raise ToolExecutionError(
                "test failure",
                safe_diagnostic="diagnostic",
                recoverable=r,
                payload_data={
                    "applied": False,
                    "error": "test_error",
                    "recoverable": r,
                    "patch_failure": {
                        "kind": "test_kind",
                        "recoverable": r,
                        "path": "target.py",
                    },
                },
            )

        registry = registry_for(handler=handle)
        obs = registry.dispatch(action(), observation_id=f"obs-rec-{rec_state}")
        assert obs.status is ObservationStatus.ERROR
        assert obs.payload.get("recoverable") is rec_state
        assert obs.payload.get("patch_failure", {}).get("recoverable") is rec_state
        assert obs.payload.get("patch_failure", {}).get("kind") == "test_kind"
