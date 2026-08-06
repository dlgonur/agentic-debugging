import json
import math

import pytest

from agentic_debugger.runtime.pdb_protocol import (
    PROTOCOL_VERSION,
    MAX_LINE_LENGTH,
    SUPPORTED_OPERATIONS,
    PdbRequest,
    PdbResponse,
    PdbWorkerInfo,
    serialize_request,
    deserialize_request,
    serialize_response,
    deserialize_response,
)
from agentic_debugger.runtime.exceptions import PdbProtocolError


_VALID_REQUEST_MAPPING = {
    "protocol_version": PROTOCOL_VERSION,
    "request_id": 1,
    "operation": "hello",
    "payload": {},
}

_VALID_RESPONSE_MAPPING = {
    "protocol_version": PROTOCOL_VERSION,
    "request_id": 1,
    "success": True,
    "result": {"pid": 12345, "protocol_version": PROTOCOL_VERSION},
    "error": "",
}

_VALID_WORKER_INFO_MAPPING = {
    "pid": 42,
    "protocol_version": PROTOCOL_VERSION,
}


class TestPdbRequest:
    def test_from_mapping_round_trip(self):
        req = PdbRequest.from_mapping(_VALID_REQUEST_MAPPING)
        assert req.protocol_version == PROTOCOL_VERSION
        assert req.request_id == 1
        assert req.operation == "hello"
        assert req.payload == {}

    def test_to_mapping_round_trip(self):
        req = PdbRequest.from_mapping(_VALID_REQUEST_MAPPING)
        m = req.to_mapping()
        assert m == _VALID_REQUEST_MAPPING

    def test_deterministic_mapping(self):
        req1 = PdbRequest.from_mapping(_VALID_REQUEST_MAPPING)
        req2 = PdbRequest.from_mapping(_VALID_REQUEST_MAPPING)
        assert req1.to_mapping() == req2.to_mapping()
        assert json.dumps(req1.to_mapping(), sort_keys=True) == json.dumps(
            req2.to_mapping(), sort_keys=True
        )

    def test_missing_field_rejected(self):
        fields = list(_VALID_REQUEST_MAPPING.keys())
        for field in fields:
            m = dict(_VALID_REQUEST_MAPPING)
            del m[field]
            with pytest.raises(PdbProtocolError, match="Missing required"):
                PdbRequest.from_mapping(m)

    def test_unknown_field_rejected(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["extra"] = "value"
        with pytest.raises(PdbProtocolError, match="Unknown fields"):
            PdbRequest.from_mapping(m)

    def test_boolean_request_id_rejected(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["request_id"] = True
        with pytest.raises(PdbProtocolError, match="request_id"):
            PdbRequest.from_mapping(m)

    def test_negative_request_id_rejected(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["request_id"] = -1
        with pytest.raises(PdbProtocolError, match="non-negative"):
            PdbRequest.from_mapping(m)

    def test_unsupported_operation_allowed_for_worker_dispatch(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "execute"
        req = PdbRequest.from_mapping(m)
        assert req.operation == "execute"

    def test_operation_empty_string_rejected(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = ""
        with pytest.raises(PdbProtocolError, match="non-empty"):
            PdbRequest.from_mapping(m)

    def test_not_a_mapping(self):
        with pytest.raises(PdbProtocolError, match="must be a mapping"):
            PdbRequest.from_mapping("not a dict")

    def test_direct_constructor_valid(self):
        req = PdbRequest(protocol_version=1, request_id=0, operation="ping", payload={})
        assert req.request_id == 0

    def test_direct_constructor_boolean_version_rejected(self):
        with pytest.raises(PdbProtocolError, match="protocol_version"):
            PdbRequest(protocol_version=True, request_id=1, operation="hello", payload={})

    def test_direct_constructor_boolean_request_id_rejected(self):
        with pytest.raises(PdbProtocolError, match="request_id"):
            PdbRequest(protocol_version=1, request_id=True, operation="hello", payload={})

    def test_direct_constructor_negative_request_id(self):
        with pytest.raises(PdbProtocolError, match="non-negative"):
            PdbRequest(protocol_version=1, request_id=-1, operation="hello", payload={})

    def test_direct_constructor_empty_operation(self):
        with pytest.raises(PdbProtocolError, match="non-empty"):
            PdbRequest(protocol_version=1, request_id=1, operation="", payload={})

    def test_direct_constructor_nan_payload(self):
        with pytest.raises(PdbProtocolError, match="Non-finite"):
            PdbRequest(protocol_version=1, request_id=1, operation="hello", payload={"x": float("nan")})

    def test_direct_constructor_inf_payload(self):
        with pytest.raises(PdbProtocolError, match="Non-finite"):
            PdbRequest(protocol_version=1, request_id=1, operation="hello", payload={"x": float("inf")})

    def test_direct_constructor_tuple_payload(self):
        with pytest.raises(PdbProtocolError, match="Tuple"):
            PdbRequest(protocol_version=1, request_id=1, operation="hello", payload={"items": (1, 2)})

    def test_boolean_operation_rejected(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = True
        with pytest.raises(PdbProtocolError):
            PdbRequest.from_mapping(m)


class TestPdbResponse:
    def test_from_mapping_round_trip(self):
        resp = PdbResponse.from_mapping(_VALID_RESPONSE_MAPPING)
        assert resp.protocol_version == PROTOCOL_VERSION
        assert resp.request_id == 1
        assert resp.success is True
        assert resp.result == {"pid": 12345, "protocol_version": PROTOCOL_VERSION}
        assert resp.error == ""

    def test_to_mapping_round_trip(self):
        resp = PdbResponse.from_mapping(_VALID_RESPONSE_MAPPING)
        m = resp.to_mapping()
        assert m == _VALID_RESPONSE_MAPPING

    def test_deterministic_mapping(self):
        resp1 = PdbResponse.from_mapping(_VALID_RESPONSE_MAPPING)
        resp2 = PdbResponse.from_mapping(_VALID_RESPONSE_MAPPING)
        assert resp1.to_mapping() == resp2.to_mapping()

    def test_missing_field_rejected(self):
        fields = list(_VALID_RESPONSE_MAPPING.keys())
        for field in fields:
            m = dict(_VALID_RESPONSE_MAPPING)
            del m[field]
            with pytest.raises(PdbProtocolError, match="Missing required"):
                PdbResponse.from_mapping(m)

    def test_unknown_field_rejected(self):
        m = dict(_VALID_RESPONSE_MAPPING)
        m["extra"] = "value"
        with pytest.raises(PdbProtocolError, match="Unknown fields"):
            PdbResponse.from_mapping(m)

    def test_success_must_be_boolean(self):
        m = dict(_VALID_RESPONSE_MAPPING)
        m["success"] = 1
        with pytest.raises(PdbProtocolError, match="success must be a boolean"):
            PdbResponse.from_mapping(m)

    def test_result_must_be_mapping(self):
        m = dict(_VALID_RESPONSE_MAPPING)
        m["result"] = "not a dict"
        with pytest.raises(PdbProtocolError, match="result must be a mapping"):
            PdbResponse.from_mapping(m)

    def test_error_must_be_string(self):
        m = dict(_VALID_RESPONSE_MAPPING)
        m["error"] = 123
        with pytest.raises(PdbProtocolError, match="error must be a string"):
            PdbResponse.from_mapping(m)

    def test_success_with_error_rejected(self):
        m = dict(_VALID_RESPONSE_MAPPING)
        m["success"] = True
        m["error"] = "should not be here"
        with pytest.raises(PdbProtocolError, match="success is True"):
            PdbResponse.from_mapping(m)

    def test_failure_without_error_rejected(self):
        m = dict(_VALID_RESPONSE_MAPPING)
        m["success"] = False
        m["result"] = {}
        m["error"] = ""
        with pytest.raises(PdbProtocolError, match="success is False"):
            PdbResponse.from_mapping(m)

    def test_direct_constructor_success_with_error_rejected(self):
        with pytest.raises(PdbProtocolError, match="success is True"):
            PdbResponse(protocol_version=1, request_id=1, success=True, result={}, error="has error")

    def test_direct_constructor_failure_without_error_rejected(self):
        with pytest.raises(PdbProtocolError, match="success is False"):
            PdbResponse(protocol_version=1, request_id=1, success=False, result={}, error="")

    def test_error_response_round_trip(self):
        m = dict(_VALID_RESPONSE_MAPPING)
        m["success"] = False
        m["result"] = {}
        m["error"] = "something went wrong"
        resp = PdbResponse.from_mapping(m)
        assert resp.success is False
        assert resp.error == "something went wrong"


class TestPdbWorkerInfo:
    def test_to_mapping(self):
        info = PdbWorkerInfo(pid=42, protocol_version=PROTOCOL_VERSION)
        m = info.to_mapping()
        assert m["pid"] == 42
        assert m["protocol_version"] == PROTOCOL_VERSION

    def test_from_mapping(self):
        info = PdbWorkerInfo.from_mapping(_VALID_WORKER_INFO_MAPPING)
        assert info.pid == 42
        assert info.protocol_version == PROTOCOL_VERSION

    def test_from_mapping_round_trip(self):
        info = PdbWorkerInfo.from_mapping(_VALID_WORKER_INFO_MAPPING)
        assert info.to_mapping() == _VALID_WORKER_INFO_MAPPING

    def test_from_mapping_unknown_field_rejected(self):
        m = dict(_VALID_WORKER_INFO_MAPPING)
        m["extra"] = True
        with pytest.raises(PdbProtocolError, match="Unknown fields"):
            PdbWorkerInfo.from_mapping(m)

    def test_from_mapping_missing_pid(self):
        m = dict(_VALID_WORKER_INFO_MAPPING)
        del m["pid"]
        with pytest.raises(PdbProtocolError, match="Missing required"):
            PdbWorkerInfo.from_mapping(m)

    def test_direct_constructor_boolean_pid_rejected(self):
        with pytest.raises(PdbProtocolError, match="pid must be an integer"):
            PdbWorkerInfo(pid=True, protocol_version=1)

    def test_direct_constructor_zero_pid_rejected(self):
        with pytest.raises(PdbProtocolError, match="pid must be positive"):
            PdbWorkerInfo(pid=0, protocol_version=1)

    def test_direct_constructor_negative_pid_rejected(self):
        with pytest.raises(PdbProtocolError, match="pid must be positive"):
            PdbWorkerInfo(pid=-1, protocol_version=1)

    def test_from_mapping_not_a_mapping(self):
        with pytest.raises(PdbProtocolError, match="must be a mapping"):
            PdbWorkerInfo.from_mapping(42)

    def test_from_mapping_boolean_pid_rejected(self):
        m = dict(_VALID_WORKER_INFO_MAPPING)
        m["pid"] = True
        with pytest.raises(PdbProtocolError, match="pid must be an integer"):
            PdbWorkerInfo.from_mapping(m)

    def test_from_mapping_zero_pid_rejected(self):
        m = dict(_VALID_WORKER_INFO_MAPPING)
        m["pid"] = 0
        with pytest.raises(PdbProtocolError, match="pid must be positive"):
            PdbWorkerInfo.from_mapping(m)


class TestNanInfinity:
    def test_nan_in_request_payload_rejected(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["payload"] = {"value": float("nan")}
        with pytest.raises(PdbProtocolError, match="Non-finite"):
            PdbRequest.from_mapping(m)

    def test_inf_in_request_payload_rejected(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["payload"] = {"value": float("inf")}
        with pytest.raises(PdbProtocolError, match="Non-finite"):
            PdbRequest.from_mapping(m)

    def test_nan_in_response_result_rejected(self):
        m = dict(_VALID_RESPONSE_MAPPING)
        m["result"] = {"value": float("nan")}
        with pytest.raises(PdbProtocolError, match="Non-finite"):
            PdbResponse.from_mapping(m)

    def test_inf_in_response_result_rejected(self):
        m = dict(_VALID_RESPONSE_MAPPING)
        m["result"] = {"value": float("inf")}
        with pytest.raises(PdbProtocolError, match="Non-finite"):
            PdbResponse.from_mapping(m)

    def test_nan_nested_in_list_rejected(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["payload"] = {"items": [1.0, float("nan"), 3.0]}
        with pytest.raises(PdbProtocolError, match="Non-finite"):
            PdbRequest.from_mapping(m)


class TestSerialization:
    def test_serialize_deserialize_request_round_trip(self):
        req = PdbRequest.from_mapping(_VALID_REQUEST_MAPPING)
        data = serialize_request(req)
        req2 = deserialize_request(data)
        assert req2.to_mapping() == req.to_mapping()

    def test_serialize_deserialize_response_round_trip(self):
        resp = PdbResponse.from_mapping(_VALID_RESPONSE_MAPPING)
        data = serialize_response(resp)
        resp2 = deserialize_response(data)
        assert resp2.to_mapping() == resp.to_mapping()

    def test_serialize_allow_nan_rejected(self):
        with pytest.raises(PdbProtocolError, match="Non-finite"):
            PdbRequest(protocol_version=1, request_id=1, operation="hello",
                       payload={"x": float("nan")})

    def test_malformed_json_rejected(self):
        with pytest.raises(PdbProtocolError, match="not valid JSON"):
            deserialize_request(b"not json\n")

    def test_oversized_line_rejected(self):
        big_data = b"x" * (MAX_LINE_LENGTH + 1)
        with pytest.raises(PdbProtocolError, match="exceeds maximum length"):
            deserialize_request(big_data)

    def test_oversized_response_line_rejected(self):
        big_data = b"x" * (MAX_LINE_LENGTH + 1)
        with pytest.raises(PdbProtocolError, match="exceeds maximum length"):
            deserialize_response(big_data)

    def test_utf8_content_in_request(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["payload"] = {"greeting": "héllo wörld 日本語"}
        req = PdbRequest.from_mapping(m)
        data = serialize_request(req)
        req2 = deserialize_request(data)
        assert req2.payload["greeting"] == "héllo wörld 日本語"

    def test_non_utf8_bytes_rejected(self):
        with pytest.raises(PdbProtocolError, match="not valid UTF-8"):
            deserialize_request(b"\xff\xfe\x00\x01\n")

    def test_json_valid_but_wrong_type(self):
        with pytest.raises(PdbProtocolError, match="must be a mapping"):
            deserialize_request(b'123\n')

    def test_deserialize_response_json_array_rejected(self):
        with pytest.raises(PdbProtocolError, match="must be a mapping"):
            deserialize_response(b'["a", "b"]\n')

    def test_serialize_oversized_request_rejected(self):
        req = PdbRequest(
            protocol_version=1, request_id=1, operation="hello",
            payload={"x": "a" * 70000},
        )
        with pytest.raises(PdbProtocolError, match="MAX_LINE_LENGTH"):
            serialize_request(req)

    def test_serialize_oversized_response_rejected(self):
        resp = PdbResponse(
            protocol_version=1, request_id=1, success=True,
            result={"x": "a" * 70000}, error="",
        )
        with pytest.raises(PdbProtocolError, match="MAX_LINE_LENGTH"):
            serialize_response(resp)

    def test_supported_operations(self):
        assert SUPPORTED_OPERATIONS == frozenset(
            {"hello", "ping", "shutdown", "run_to_breakpoint",
             "start_paused_target", "continue_paused_target",
             "get_target_status", "terminate_paused_target",
             "get_stack_summary", "get_frame", "get_frame_locals",
             "safe_eval_expression", "run_post_mortem"}
        )

    def test_run_to_breakpoint_operation_accepted(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "run_to_breakpoint"
        m["payload"] = {
            "script": "test.py",
            "breakpoints": [1],
            "argv": [],
        }
        req = PdbRequest.from_mapping(m)
        assert req.operation == "run_to_breakpoint"
        assert req.payload["script"] == "test.py"
        assert req.payload["breakpoints"] == [1]
        assert req.payload["argv"] == []

    def test_run_to_breakpoint_payload_round_trip(self):
        payload = {
            "script": "subdir/target.py",
            "breakpoints": [5, 10, 15],
            "argv": ["arg1", "arg2"],
        }
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "run_to_breakpoint"
        m["payload"] = payload
        req = PdbRequest.from_mapping(m)
        assert req.payload["script"] == "subdir/target.py"
        assert req.payload["breakpoints"] == [5, 10, 15]
        assert req.payload["argv"] == ["arg1", "arg2"]

    def test_protocol_version(self):
        assert PROTOCOL_VERSION == 1

    def test_max_line_length(self):
        assert MAX_LINE_LENGTH == 65536

    def test_tuple_in_payload_rejected(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["payload"] = {"items": (1, 2, 3)}
        with pytest.raises(PdbProtocolError, match="Tuple"):
            PdbRequest.from_mapping(m)

    def test_tuple_in_response_result_rejected(self):
        m = dict(_VALID_RESPONSE_MAPPING)
        m["result"] = {"items": (4, 5, 6)}
        with pytest.raises(PdbProtocolError, match="Tuple"):
            PdbResponse.from_mapping(m)

    def test_deterministic_serialization(self):
        req = PdbRequest(protocol_version=1, request_id=5, operation="ping", payload={"a": 1, "b": 2})
        d1 = serialize_request(req)
        d2 = serialize_request(req)
        assert d1 == d2

    def test_deterministic_serialization_key_order(self):
        import json as _json
        payload = {"z": 1, "a": 2, "m": 3}
        req = PdbRequest(protocol_version=1, request_id=1, operation="ping", payload=payload)
        data = serialize_request(req)
        decoded = _json.loads(data.decode("utf-8"))
        keys = list(decoded["payload"].keys())
        assert keys == ["a", "m", "z"], f"Expected sorted keys, got {keys}"

    def test_duplicate_key_top_level_rejected(self):
        raw = '{"protocol_version":1,"request_id":1,"operation":"ping","payload":{},"protocol_version":2}'
        with pytest.raises(PdbProtocolError, match="Duplicate key"):
            import agentic_debugger.runtime.pdb_protocol as pp
            pp.deserialize_request(raw.encode("utf-8"))

    def test_duplicate_key_in_payload_rejected(self):
        raw = '{"protocol_version":1,"request_id":1,"operation":"ping","payload":{"a":1,"a":2}}'
        with pytest.raises(PdbProtocolError, match="Duplicate key"):
            import agentic_debugger.runtime.pdb_protocol as pp
            pp.deserialize_request(raw.encode("utf-8"))

    def test_post_construction_mutation_payload(self):
        payload = {}
        req = PdbRequest(protocol_version=1, request_id=1, operation="ping", payload=payload)
        payload["x"] = float("nan")
        try:
            data = serialize_request(req)
        except PdbProtocolError:
            pass
        else:
            assert b"nan" not in data, "Mutation should not affect frozen record"

    def test_to_mapping_returns_copy(self):
        req = PdbRequest(protocol_version=1, request_id=1, operation="ping", payload={"a": [1, 2]})
        m = req.to_mapping()
        m["payload"]["a"].append(3)
        m2 = req.to_mapping()
        assert m2["payload"]["a"] == [1, 2], "to_mapping should not expose internal state"

    def test_deterministic_serialization_nested(self):
        req = PdbRequest(protocol_version=1, request_id=1, operation="ping",
                         payload={"nested": {"z": 1, "a": 2}})
        data = serialize_request(req)
        import json as _json
        decoded = _json.loads(data.decode("utf-8"))
        assert list(decoded["payload"]["nested"].keys()) == ["a", "z"]


class TestPdbRequestWithPayload:
    def test_with_payload_round_trip(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["payload"] = {"key": "value", "count": 42}
        req = PdbRequest.from_mapping(m)
        assert req.payload["key"] == "value"
        assert req.payload["count"] == 42

    def test_none_payload_rejected(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["payload"] = None
        with pytest.raises(PdbProtocolError, match="payload must be a mapping"):
            PdbRequest.from_mapping(m)

    def test_serialized_includes_newline(self):
        req = PdbRequest.from_mapping(_VALID_REQUEST_MAPPING)
        data = serialize_request(req)
        assert data.endswith(b"\n")

    def test_response_serialized_includes_newline(self):
        resp = PdbResponse.from_mapping(_VALID_RESPONSE_MAPPING)
        data = serialize_response(resp)
        assert data.endswith(b"\n")


class TestNewPersistentOperations:
    """Tests for start_paused_target, get_target_status, terminate_paused_target payload validation."""

    def test_start_paused_target_operation_accepted(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "start_paused_target"
        m["payload"] = {
            "script": "test.py",
            "breakpoints": [4, 8],
            "argv": ["argument"],
        }
        req = PdbRequest.from_mapping(m)
        assert req.operation == "start_paused_target"
        assert req.payload["script"] == "test.py"
        assert req.payload["breakpoints"] == [4, 8]
        assert req.payload["argv"] == ["argument"]

    def test_start_paused_target_payload_round_trip(self):
        payload = {
            "script": "subdir/target.py",
            "breakpoints": [5, 10, 15],
            "argv": ["arg1", "arg2"],
        }
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "start_paused_target"
        m["payload"] = payload
        req = PdbRequest.from_mapping(m)
        ser = serialize_request(req)
        req2 = deserialize_request(ser)
        assert req2.payload == payload

    def test_start_paused_target_missing_script(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "start_paused_target"
        m["payload"] = {"breakpoints": [1], "argv": []}
        req = PdbRequest.from_mapping(m)
        assert req.operation == "start_paused_target"

    def test_start_paused_target_unknown_field(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "start_paused_target"
        m["payload"] = {"script": "x.py", "breakpoints": [1], "argv": [], "extra": 1}
        req = PdbRequest.from_mapping(m)
        assert req.operation == "start_paused_target"

    def test_start_paused_target_wrong_field_type(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "start_paused_target"
        m["payload"] = {"script": 123, "breakpoints": [1], "argv": []}
        req = PdbRequest.from_mapping(m)
        assert req.operation == "start_paused_target"

    def test_get_target_status_operation_accepted(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "get_target_status"
        m["payload"] = {}
        req = PdbRequest.from_mapping(m)
        assert req.operation == "get_target_status"

    def test_get_target_status_with_fields_rejected_by_worker(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "get_target_status"
        m["payload"] = {"unknown": "value"}
        req = PdbRequest.from_mapping(m)
        assert req.operation == "get_target_status"
        assert req.payload == {"unknown": "value"}

    def test_terminate_paused_target_operation_accepted(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "terminate_paused_target"
        m["payload"] = {}
        req = PdbRequest.from_mapping(m)
        assert req.operation == "terminate_paused_target"

    def test_terminate_paused_target_with_fields_rejected_by_worker(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "terminate_paused_target"
        m["payload"] = {"unknown": "value"}
        req = PdbRequest.from_mapping(m)
        assert req.operation == "terminate_paused_target"
        assert req.payload == {"unknown": "value"}

    def test_run_to_breakpoint_still_accepted(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "run_to_breakpoint"
        m["payload"] = {"script": "test.py", "breakpoints": [1], "argv": []}
        req = PdbRequest.from_mapping(m)
        assert req.operation == "run_to_breakpoint"

    def test_hello_still_accepted(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "hello"
        req = PdbRequest.from_mapping(m)
        assert req.operation == "hello"

    def test_ping_still_accepted(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "ping"
        req = PdbRequest.from_mapping(m)
        assert req.operation == "ping"

    def test_shutdown_still_accepted(self):
        m = dict(_VALID_REQUEST_MAPPING)
        m["operation"] = "shutdown"
        req = PdbRequest.from_mapping(m)
        assert req.operation == "shutdown"

    def test_malformed_request_still_rejected(self):
        with pytest.raises(PdbProtocolError, match="not valid JSON"):
            deserialize_request(b"not json\n")


class TestInspectionProtocolOperations:
    @pytest.mark.parametrize(
        ("operation", "payload"),
        [
            ("get_stack_summary", {}),
            ("get_frame", {"frame_id": 0, "pause_generation": 1}),
            ("get_frame_locals", {"frame_id": 0, "pause_generation": 1}),
        ],
    )
    def test_canonical_operation_round_trip(self, operation, payload):
        request = PdbRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=901,
            operation=operation,
            payload=payload,
        )
        restored = deserialize_request(serialize_request(request))
        assert restored.operation == operation
        assert restored.payload == payload

    @pytest.mark.parametrize(
        "alias",
        ["stack", "get_stack", "frame", "locals", "get_locals"],
    )
    def test_operation_aliases_are_not_supported(self, alias):
        assert alias not in SUPPORTED_OPERATIONS


class TestSafeEvaluationProtocolOperation:
    def test_canonical_operation_and_exact_payload_round_trip(self):
        payload = {
            "frame_id": 0,
            "pause_generation": 1,
            "expression": "items[0]",
        }
        request = PdbRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=902,
            operation="safe_eval_expression",
            payload=payload,
        )
        restored = deserialize_request(serialize_request(request))
        assert "safe_eval_expression" in SUPPORTED_OPERATIONS
        assert restored.operation == "safe_eval_expression"
        assert restored.payload == payload
        assert set(restored.payload) == {
            "frame_id", "pause_generation", "expression",
        }

    @pytest.mark.parametrize(
        "alias",
        ["eval", "evaluate", "safe_eval", "pdb_eval", "expression"],
    )
    def test_aliases_are_not_supported(self, alias):
        assert alias not in SUPPORTED_OPERATIONS
