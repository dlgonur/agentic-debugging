"""Repo-aware, provider-neutral Transport Qualification V2 orchestration.

The Ollama command adapter remains a standalone provider-completion process.
This module owns the synthetic model-visible contract and delegates directive
validation to the canonical live parser in :mod:`agentic_debugger.evaluation.live`.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.evaluation.live import (
    LIVE_PROTOCOL_VERSION,
    PROVIDER_COMPLETION_ENVELOPE_SCHEMA,
    validate_synthetic_qualification_content,
)

TRANSPORT_QUALIFICATION_SCHEMA_VERSION = "transport-qualification-v2"
SYNTHETIC_DIRECTIVE_KINDS = frozenset({"action"})
SYNTHETIC_LEGAL_TRANSITION_TARGETS = frozenset()
MAX_ADAPTER_STDOUT_BYTES = 512 * 1024
MAX_ADAPTER_STDERR_BYTES = 32 * 1024
PREFLIGHT_SCHEMA_VERSION = "ollama-cloud-preflight-v1"
PREFLIGHT_PROCESS_TIMEOUT_SECONDS = 30.0
PROCESS_SHUTDOWN_GRACE_SECONDS = 5.0


class TransportQualificationError(RuntimeError):
    """A bounded failure before a complete qualification measurement exists."""


def synthetic_action_contracts() -> dict[str, dict[str, Any]]:
    """Return the one action contract exposed by Qualification V2."""

    return {
        "run_reproduction": {
            "properties": {
                "phase": {"type": "string", "enum": ["baseline"]},
            },
            "required": ["phase"],
            "additional_properties": False,
        },
    }


def synthetic_directive_schema() -> dict[str, dict[str, Any]]:
    """Return the live-protocol schema for exactly the accepted directive kind."""

    return {
        "action": {
            "kind": "action",
            "required": ["name", "arguments"],
        },
    }


def build_synthetic_qualification_request() -> dict[str, Any]:
    """Build the truthful model-visible request used by Qualification V2."""

    return {
        "protocol": {
            "name": "agentic-debugger-live-jsonl",
            "version": LIVE_PROTOCOL_VERSION,
            "request_id": "qualify:model-call:0:attempt:1",
            "logical_model_call_index": 0,
            "transport_attempt_index": 1,
        },
        "identity": {
            "evaluation_id": "qualify",
            "case_id": "qualify:synthetic",
            "run_id": "qualify",
            "trajectory_id": "qualify",
        },
        "task": {
            "task_id": "qualify-synthetic",
            "instruction": "Return the one synthetic action directive described by this request.",
        },
        "policy": "static-baseline",
        "directive_schema": synthetic_directive_schema(),
        "action_contracts": synthetic_action_contracts(),
        "controller": {
            "state": ControllerState.REPRODUCE.value,
            "task_id": "qualify-synthetic",
            "model_call_index": 0,
            "allowed_actions": ["run_reproduction"],
            "legal_transition_targets": sorted(SYNTHETIC_LEGAL_TRANSITION_TARGETS),
            "budget_limits": {},
            "budget_state": {},
            "hypotheses": [],
            "last_observation": None,
        },
        "history": [],
        "directive_feedback": None,
        "instructions": (
            "Return exactly one action directive JSON object: "
            '{"kind":"action","name":"run_reproduction",'
            '"arguments":{"phase":"baseline"}}'
        ),
    }


def _adapter_script() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "ollama_cloud_command_adapter.py"


def _decode_bounded_output(raw: bytes, *, maximum: int, label: str) -> str:
    if len(raw) > maximum:
        raise TransportQualificationError(f"adapter {label} exceeded the configured bound")
    return raw.decode("utf-8", errors="replace")


def _read_provider_completion(stdout: bytes) -> dict[str, Any]:
    text = _decode_bounded_output(
        stdout,
        maximum=MAX_ADAPTER_STDOUT_BYTES,
        label="stdout",
    )
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise TransportQualificationError("adapter did not return exactly one completion envelope")
    try:
        value = json.loads(lines[0])
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TransportQualificationError("adapter completion envelope was not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise TransportQualificationError("adapter completion envelope was not an object")
    if value.get("provider_completion_schema_version") != PROVIDER_COMPLETION_ENVELOPE_SCHEMA:
        raise TransportQualificationError("adapter returned an unsupported completion envelope")
    if type(value.get("directive_content")) is not str:
        raise TransportQualificationError("adapter completion content was not text")
    if not isinstance(value.get("transport_activity"), Mapping):
        raise TransportQualificationError("adapter completion omitted transport activity")
    return dict(value)


def _read_preflight(stdout: bytes) -> dict[str, Any]:
    text = _decode_bounded_output(
        stdout,
        maximum=MAX_ADAPTER_STDOUT_BYTES,
        label="preflight stdout",
    )
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise TransportQualificationError("adapter did not return exactly one preflight record")
    try:
        value = json.loads(lines[0])
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TransportQualificationError("adapter preflight was not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise TransportQualificationError("adapter preflight was not an object")
    if value.get("schema_version") != PREFLIGHT_SCHEMA_VERSION or value.get("ok") is not True:
        raise TransportQualificationError("adapter preflight did not establish provenance")
    return dict(value)


def _run_adapter_process(
    command: Sequence[str],
    *,
    request: bytes,
    process_timeout_seconds: float,
    cwd: str | None,
    failure_label: str,
) -> bytes:
    try:
        completed = subprocess.run(
            list(command),
            input=request,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            timeout=process_timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TransportQualificationError(f"standalone provider adapter {failure_label} did not complete") from exc
    stderr_text = _decode_bounded_output(
        completed.stderr,
        maximum=MAX_ADAPTER_STDERR_BYTES,
        label=f"{failure_label} stderr",
    )
    if completed.returncode != 0:
        kind = f"adapter {failure_label} failed"
        if stderr_text:
            try:
                error = json.loads(stderr_text.splitlines()[-1])
            except (UnicodeError, json.JSONDecodeError):
                error = None
            if isinstance(error, Mapping) and isinstance(error.get("kind"), str):
                kind = f"{kind}: {error['kind']}"
        raise TransportQualificationError(kind)
    return completed.stdout


def _preflight_timeout(value: Mapping[str, Any], field: str) -> float:
    raw = value.get(field)
    if type(raw) not in (int, float) or not math.isfinite(float(raw)) or raw <= 0:
        raise TransportQualificationError(f"preflight omitted a valid {field}")
    return float(raw)


def run_transport_qualification(
    *,
    endpoint: str,
    model: str,
    adapter_command: Sequence[str] | None = None,
    cwd: str | None = None,
    preflight_process_timeout_seconds: float = PREFLIGHT_PROCESS_TIMEOUT_SECONDS,
    process_shutdown_grace_seconds: float = PROCESS_SHUTDOWN_GRACE_SECONDS,
) -> dict[str, Any]:
    """Run the standalone adapter, then apply canonical V2 protocol validation.

    A completed provider response with an invalid directive is still a
    successful measurement: its result has ``stream_transport_ok=True`` and
    ``directive_protocol_ok=False``.  Process failure or malformed provider
    completion raises ``TransportQualificationError`` instead.
    """

    if type(endpoint) is not str or not endpoint:
        raise TransportQualificationError("endpoint must be a non-empty string")
    if type(model) is not str or not model:
        raise TransportQualificationError("model must be a non-empty string")
    if (
        type(preflight_process_timeout_seconds) not in (int, float)
        or preflight_process_timeout_seconds <= 0
    ):
        raise TransportQualificationError("preflight process timeout must be positive")
    if (
        type(process_shutdown_grace_seconds) not in (int, float)
        or process_shutdown_grace_seconds < 0
    ):
        raise TransportQualificationError("process shutdown grace must be non-negative")
    base_command = list(adapter_command or (sys.executable, str(_adapter_script())))
    command = base_command + [
        "--endpoint",
        endpoint,
        "--model",
        model,
    ]
    preflight_command = command + ["--preflight"]
    try:
        preflight_stdout = _run_adapter_process(
            preflight_command,
            request=b"",
            process_timeout_seconds=float(preflight_process_timeout_seconds),
            cwd=cwd,
            failure_label="preflight",
        )
        preflight = _read_preflight(preflight_stdout)
    except TransportQualificationError as exc:
        raise TransportQualificationError(f"qualification preflight failed: {exc}") from exc

    idle_timeout_seconds = _preflight_timeout(preflight, "idle_timeout_seconds")
    request_timeout_seconds = _preflight_timeout(preflight, "request_timeout_seconds")
    completion_command = command + ["--timeout", str(idle_timeout_seconds)]
    request = build_synthetic_qualification_request()
    provider_completion = _read_provider_completion(
        _run_adapter_process(
            completion_command,
            request=(json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
            process_timeout_seconds=request_timeout_seconds + float(process_shutdown_grace_seconds),
            cwd=cwd,
            failure_label="completion",
        )
    )
    content = provider_completion["directive_content"]
    directive_protocol = validate_synthetic_qualification_content(
        content,
        action_contracts=synthetic_action_contracts(),
        directive_kinds=set(SYNTHETIC_DIRECTIVE_KINDS),
        legal_transition_targets=set(SYNTHETIC_LEGAL_TRANSITION_TARGETS),
        directive_schema=synthetic_directive_schema(),
    )
    result: dict[str, Any] = {
        "qualification_schema_version": TRANSPORT_QUALIFICATION_SCHEMA_VERSION,
        "measurement_completed": True,
        "preflight_ok": True,
        "effective_idle_timeout_seconds": idle_timeout_seconds,
        "effective_request_timeout_seconds": request_timeout_seconds,
        "stream_transport_ok": True,
        "directive_protocol_ok": directive_protocol["directive_protocol_ok"],
        "directive_protocol": directive_protocol,
        "qualification": {
            "model": model,
            "preflight": preflight,
            "effective_idle_timeout_seconds": idle_timeout_seconds,
            "effective_request_timeout_seconds": request_timeout_seconds,
            "synthetic_contract": {
                "directive_kinds": sorted(SYNTHETIC_DIRECTIVE_KINDS),
                "legal_transition_targets": sorted(SYNTHETIC_LEGAL_TRANSITION_TARGETS),
                "directive_schema": synthetic_directive_schema(),
                "action_contracts": synthetic_action_contracts(),
            },
            "provider_completion": provider_completion,
        },
    }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repo-aware Transport Qualification V2")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--preflight-process-timeout",
        type=float,
        default=PREFLIGHT_PROCESS_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--process-shutdown-grace",
        type=float,
        default=PROCESS_SHUTDOWN_GRACE_SECONDS,
    )
    parser.add_argument("--adapter-script", default=str(_adapter_script()))
    parser.add_argument("--adapter-cwd", default=None)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    if not args.confirm_live:
        print(json.dumps({"schema_version": "command-error-v1", "kind": "configuration", "message": "transport qualification requires --confirm-live"}), file=sys.stderr)
        return 1
    try:
        result = run_transport_qualification(
            endpoint=args.endpoint,
            model=args.model,
            adapter_command=(sys.executable, args.adapter_script),
            cwd=args.adapter_cwd,
            preflight_process_timeout_seconds=args.preflight_process_timeout,
            process_shutdown_grace_seconds=args.process_shutdown_grace,
        )
    except TransportQualificationError as exc:
        print(json.dumps({"schema_version": "command-error-v1", "kind": "qualification_error", "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    # Exit 0 means the bounded measurement completed.  It does not mean that
    # the directive protocol qualified; callers must inspect the typed field.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
