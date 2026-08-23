from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from agentic_debugger.evaluation.live import validate_synthetic_qualification_content
from agentic_debugger.evaluation.transport_qualification import (
    SYNTHETIC_DIRECTIVE_KINDS,
    SYNTHETIC_LEGAL_TRANSITION_TARGETS,
    build_synthetic_qualification_request,
    synthetic_action_contracts,
    synthetic_directive_schema,
    TransportQualificationError,
    run_transport_qualification,
)
from scripts import ollama_cloud_command_adapter as adapter


def _valid_action() -> str:
    return json.dumps(
        {
            "kind": "action",
            "name": "run_reproduction",
            "arguments": {"phase": "baseline"},
        },
        separators=(",", ":"),
    )


def _fake_adapter_command(tmp_path: Path, *, idle: float, request: float, delays: list[float]) -> tuple[str, str]:
    script = tmp_path / "fake_provider_adapter.py"
    script.write_text(
        "import json, sys, time\n"
        f"content = {_valid_action()!r}\n"
        f"if '--preflight' in sys.argv:\n"
        f"    print(json.dumps({{'schema_version': 'ollama-cloud-preflight-v1', 'ok': True, 'idle_timeout_seconds': {idle!r}, 'request_timeout_seconds': {request!r}}}))\n"
        f"else:\n"
        f"    for delay in {delays!r}:\n"
        f"        time.sleep(delay)\n"
        f"    print(json.dumps({{'provider_completion_schema_version': 'provider-completion-v1', 'directive_content': content, 'transport_activity': {{'thinking_bytes': 0}}}}))\n",
        encoding="utf-8",
    )
    return (sys.executable, str(script))


def test_synthetic_model_visible_contract_matches_validator_exactly() -> None:
    request = build_synthetic_qualification_request()
    assert set(request["directive_schema"]) == SYNTHETIC_DIRECTIVE_KINDS
    assert set(request["controller"]["legal_transition_targets"]) == SYNTHETIC_LEGAL_TRANSITION_TARGETS
    assert set(request["controller"]["allowed_actions"]) == set(synthetic_action_contracts())
    assert set(request["directive_schema"]) == set(synthetic_directive_schema())

    accepted = validate_synthetic_qualification_content(
        _valid_action(),
        action_contracts=synthetic_action_contracts(),
        directive_kinds=set(SYNTHETIC_DIRECTIVE_KINDS),
        legal_transition_targets=set(SYNTHETIC_LEGAL_TRANSITION_TARGETS),
        directive_schema=synthetic_directive_schema(),
    )
    assert accepted["directive_protocol_ok"] is True
    assert accepted["action_name"] == "run_reproduction"


def test_transition_is_rejected_only_when_not_advertised_as_legal() -> None:
    transition = json.dumps(
        {"kind": "transition", "target_state": "Understand", "reason": "synthetic"},
        separators=(",", ":"),
    )
    not_advertised = validate_synthetic_qualification_content(
        transition,
        action_contracts=synthetic_action_contracts(),
        directive_kinds=set(SYNTHETIC_DIRECTIVE_KINDS),
        legal_transition_targets=set(SYNTHETIC_LEGAL_TRANSITION_TARGETS),
        directive_schema=synthetic_directive_schema(),
    )
    assert not_advertised["directive_protocol_ok"] is False
    assert not_advertised["category"] == "DIRECTIVE_SEMANTIC_REJECTED"

    advertised_schema = {
        **synthetic_directive_schema(),
        "transition": {
            "kind": "transition",
            "required": ["target_state", "reason"],
        },
    }
    advertised = validate_synthetic_qualification_content(
        transition,
        action_contracts=synthetic_action_contracts(),
        directive_kinds={"action", "transition"},
        legal_transition_targets={"Understand"},
        directive_schema=advertised_schema,
    )
    assert advertised["directive_protocol_ok"] is True
    assert advertised["directive_kind"] == "transition"


def test_canonical_operator_hint_includes_the_required_endpoint() -> None:
    root = Path(__file__).resolve().parents[2]
    operator = (root / "scripts" / "run_cookiecutter_967_pdb_proof.py").read_text(encoding="utf-8")
    documentation = (root / "docs" / "architecture" / "ollama-cloud-command-adapter-v1.md").read_text(encoding="utf-8")
    command_fragment = "--endpoint http://127.0.0.1:11434/api"
    assert command_fragment in operator
    assert command_fragment in documentation
    assert adapter.TRANSPORT_QUALIFICATION_COMMAND == (
        "python -m agentic_debugger.evaluation.transport_qualification "
        "--endpoint http://127.0.0.1:11434/api --model <alias> --confirm-live --json"
    )


def test_active_progress_may_exceed_idle_timeout_total(tmp_path: Path) -> None:
    command = _fake_adapter_command(tmp_path, idle=0.05, request=0.25, delays=[0.04, 0.04, 0.04])
    started = time.monotonic()
    result = run_transport_qualification(
        endpoint="http://127.0.0.1:11434/api",
        model="synthetic-profile",
        adapter_command=command,
        cwd=str(tmp_path),
        preflight_process_timeout_seconds=1.0,
        process_shutdown_grace_seconds=0.05,
    )
    elapsed = time.monotonic() - started
    assert elapsed > 0.1
    assert result["preflight_ok"] is True
    assert result["stream_transport_ok"] is True
    assert result["directive_protocol_ok"] is True
    assert result["effective_idle_timeout_seconds"] == 0.05
    assert result["effective_request_timeout_seconds"] == 0.25


def test_outer_request_deadline_remains_bounded(tmp_path: Path) -> None:
    command = _fake_adapter_command(tmp_path, idle=0.01, request=0.06, delays=[0.3])
    started = time.monotonic()
    with pytest.raises(TransportQualificationError, match="completion"):
        run_transport_qualification(
            endpoint="http://127.0.0.1:11434/api",
            model="synthetic-profile",
            adapter_command=command,
            cwd=str(tmp_path),
            preflight_process_timeout_seconds=1.0,
            process_shutdown_grace_seconds=0.02,
        )
    assert time.monotonic() - started < 0.8
