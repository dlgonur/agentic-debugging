from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agentic_debugger.evaluation.professor_trace import validate_trace


SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "agentic_debugger"
    / "evaluation"
    / "professor_debug_trace_schema_v1.json"
)


def _minimal_value(schema):
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    type_value = schema.get("type")
    types = type_value if isinstance(type_value, list) else [type_value]
    selected = next((item for item in types if item != "null"), "null")
    if selected == "object":
        properties = schema.get("properties", {})
        return {
            name: _minimal_value(properties[name]) if name in properties else "x"
            for name in schema.get("required", [])
        }
    if selected == "array":
        return []
    if selected == "string":
        return "x" * max(1, schema.get("minLength", 0))
    if selected == "integer":
        return max(1, schema.get("minimum", 0))
    if selected == "number":
        return max(1, schema.get("minimum", 0))
    if selected == "boolean":
        return False
    if selected == "null":
        return None
    raise AssertionError(f"unsupported schema type in fixture builder: {selected}")


def test_validate_trace_uses_complete_checked_in_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    trace = _minimal_value(schema)

    validate_trace(trace)

    invalid = copy.deepcopy(trace)
    del invalid["model"]["base_revision"]
    with pytest.raises(ValueError, match=r"\$trace\.model: missing required fields"):
        validate_trace(invalid)


def test_validate_trace_rejects_nested_minimum_and_enum() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    trace = _minimal_value(schema)
    trace["debugger_path"] = "fabricated"
    with pytest.raises(ValueError, match="not in enum"):
        validate_trace(trace)

    trace = _minimal_value(schema)
    trace["debugger_trace"] = [
        {
            "turn": 0,
            "phase": "RuntimeEvidence",
            "model_command": "break 2",
            "status": "ok",
        }
    ]
    with pytest.raises(ValueError, match="below minimum"):
        validate_trace(trace)
