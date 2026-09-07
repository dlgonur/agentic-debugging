"""Task-28 regression: the strict durable ``model.configured`` schema
accepts and preserves the SAFE runtime provenance emitted by
``ModelBinding.model_configured_payload()``.

Fails on the pre-Task-28 baseline (``897eea6``): the strict validator
rejects the five safe ``ModelBinding`` provenance fields
(``model_binding_fingerprint``, ``effective_protocol``,
``endpoint_contract``, ``transport_profile``,
``provider_runtime_identity``) with::

    SchemaValidationError: Unknown fields in model.configured payload: [...]

A normal Local Project registry/direct-provider session must be able to
emit its real ``ModelBinding`` provenance through the real
``SessionEventEmitter`` / ``SessionEvent`` / journal path without
filtering fields, monkeypatching the event validator, or using a
test-only schema accommodation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agentic_debugger import SchemaValidationError  # noqa: E402
from agentic_debugger.application.events import (  # noqa: E402
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
    SessionEventKind,
    SourceKind,
)
from agentic_debugger.application.model_gateway import ModelBinding  # noqa: E402
from agentic_debugger.application.provider_connections import (  # noqa: E402
    AUTH_NONE,
    PROTOCOL_CHAT_COMPLETIONS,
    TRANSPORT_GENERIC,
)

CONFIG_FINGERPRINT = "a" * 64
RUNTIME_IDENTITY = "b" * 64


def _direct_binding() -> ModelBinding:
    """A legitimate direct-API ModelBinding (all safe provenance present)."""
    return ModelBinding(
        provider_id="t28_direct_provider",
        model_id="t28-model-x",
        provider_model_id="t28-model-x",
        display_name="T28 Model X",
        route="direct_api",
        effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
        endpoint_contract=TRANSPORT_GENERIC,
        endpoint="http://127.0.0.1:8000",
        auth_mode=AUTH_NONE,
        config_fingerprint=CONFIG_FINGERPRINT,
        tool_version="live-command-v1",
        provider_runtime_identity=RUNTIME_IDENTITY,
    )


def _event_mapping(payload: dict) -> dict:
    return {
        "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        "session_id": "sess-t28-schema",
        "task_id": "local-project-debug",
        "run_id": None,
        "sequence": 0,
        "timestamp_utc": "2026-08-14T08:00:00Z",
        "source_kind": SourceKind.LOCAL_PROJECT.value,
        "event_kind": SessionEventKind.MODEL_CONFIGURED.value,
        "controller_phase": None,
        "payload": payload,
    }


def test_complete_binding_payload_survives_strict_event_roundtrip() -> None:
    binding = _direct_binding()
    payload = binding.model_configured_payload()

    # The complete, unfiltered payload passes strict validation and the
    # safe provenance survives the SessionEvent roundtrip unchanged.
    event = SessionEvent.from_mapping(_event_mapping(dict(payload)))
    stored = dict(event.payload)
    for key, value in payload.items():
        assert stored[key] == value
    assert stored["model_binding_fingerprint"] == binding.fingerprint()
    assert stored["provider_runtime_identity"] == binding.provider_runtime_identity
    assert stored["effective_protocol"] == binding.effective_protocol
    assert stored["endpoint_contract"] == binding.endpoint_contract
    assert stored["transport_profile"] == binding.endpoint_contract


def test_complete_binding_payload_survives_durable_roundtrip() -> None:
    binding = _direct_binding()
    payload = binding.model_configured_payload()

    event = SessionEvent.from_mapping(_event_mapping(dict(payload)))
    revived = SessionEvent.from_mapping(event.to_mapping())
    assert revived == event
    durable = dict(revived.payload)
    assert durable["model_binding_fingerprint"] == binding.fingerprint()
    assert durable["provider_runtime_identity"] == binding.provider_runtime_identity
    assert durable["effective_protocol"] == binding.effective_protocol
    assert durable["endpoint_contract"] == binding.endpoint_contract
    assert durable["transport_profile"] == binding.endpoint_contract
    assert durable["api_protocol"] == binding.effective_protocol


def test_agreeing_aliases_are_accepted() -> None:
    binding = _direct_binding()
    payload = dict(binding.model_configured_payload())
    assert payload["api_protocol"] == payload["effective_protocol"]
    assert payload["endpoint_contract"] == payload["transport_profile"]
    event = SessionEvent.from_mapping(_event_mapping(payload))
    assert dict(event.payload)["effective_protocol"] == PROTOCOL_CHAT_COMPLETIONS


@pytest.mark.parametrize("field", ["model_binding_fingerprint", "provider_runtime_identity"])
@pytest.mark.parametrize(
    "bad", ["", "xyz", "a" * 63, "a" * 65, "A" * 64, "g" * 64, 12345, None]
)
def test_invalid_fingerprint_shape_fails(field: str, bad: object) -> None:
    payload = dict(_direct_binding().model_configured_payload())
    payload[field] = bad  # type: ignore[assignment]
    with pytest.raises(SchemaValidationError):
        SessionEvent.from_mapping(_event_mapping(payload))


def test_contradictory_protocol_aliases_fail() -> None:
    payload = dict(_direct_binding().model_configured_payload())
    assert payload["api_protocol"] != "messages"
    payload["effective_protocol"] = "messages"
    with pytest.raises(SchemaValidationError):
        SessionEvent.from_mapping(_event_mapping(payload))


def test_contradictory_endpoint_contract_aliases_fail() -> None:
    payload = dict(_direct_binding().model_configured_payload())
    assert payload["endpoint_contract"] != "ollama_cloud"
    payload["transport_profile"] = "ollama_cloud"
    with pytest.raises(SchemaValidationError):
        SessionEvent.from_mapping(_event_mapping(payload))


def test_historical_minimal_payload_remains_valid() -> None:
    historical = {
        "profile_id": "local-dummy",
        "config_fingerprint": CONFIG_FINGERPRINT,
        "display_name": "Dummy command model",
        "protocol_version": "1.3",
        "tool_version": "live-command-v1",
    }
    event = SessionEvent.from_mapping(_event_mapping(dict(historical)))
    assert dict(event.payload) == historical


def test_single_alias_member_remains_valid() -> None:
    """A historical payload carrying only one member of an alias pair
    (e.g. only ``api_protocol``) stays valid: coherence is only enforced
    when both members are present."""
    historical = {
        "profile_id": "local-dummy",
        "config_fingerprint": CONFIG_FINGERPRINT,
        "display_name": "Dummy command model",
        "protocol_version": "1.3",
        "tool_version": "live-command-v1",
        "provider": "some-provider",
        "route": "direct_api",
        "api_protocol": PROTOCOL_CHAT_COMPLETIONS,
    }
    event = SessionEvent.from_mapping(_event_mapping(dict(historical)))
    assert dict(event.payload) == historical
