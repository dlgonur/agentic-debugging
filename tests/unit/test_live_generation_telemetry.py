from __future__ import annotations

import json

from agentic_debugger.evaluation.live import (
    COMMAND_ADAPTER_TELEMETRY_SCHEMA_VERSION,
    PROVIDER_GENERATION_TELEMETRY_EVENT,
    parse_command_adapter_error,
    parse_provider_generation_telemetry,
)


def _telemetry(**overrides):
    value = {
        "schema_version": COMMAND_ADAPTER_TELEMETRY_SCHEMA_VERSION,
        "event": PROVIDER_GENERATION_TELEMETRY_EVENT,
        "first_response_chunk_latency_seconds": 0.25,
        "last_chunk_elapsed_seconds": 1.5,
        "completion_elapsed_seconds": 1.6,
        "timeout_phase": None,
        "progress_occurred": True,
        "progress_before_timeout": False,
        "wire_bytes_observed": 4096,
        "retained_content_bytes": 128,
        "discarded_thinking_bytes": 3968,
        "discarded_thinking_chunks": 4,
        "stream_chunks_observed": 5,
    }
    value.update(overrides)
    return value


def test_generation_telemetry_is_numeric_and_error_parser_ignores_it():
    stderr = "\n".join(
        [
            json.dumps(_telemetry()),
            json.dumps({
                "schema_version": "live-command-error-v1",
                "kind": "timeout",
                "message": "bounded provider failure",
            }),
        ]
    )
    parsed = parse_provider_generation_telemetry(stderr)
    assert parsed["wire_bytes_observed"] == 4096
    assert parsed["discarded_thinking_bytes"] == 3968
    assert parse_command_adapter_error(stderr) == ("timeout", "bounded provider failure")


def test_generation_telemetry_rejects_negative_or_unknown_values():
    assert parse_provider_generation_telemetry(
        json.dumps(_telemetry(wire_bytes_observed=-1))
    ) is None
    assert parse_provider_generation_telemetry(
        json.dumps(_telemetry(timeout_phase="unknown"))
    ) is None
