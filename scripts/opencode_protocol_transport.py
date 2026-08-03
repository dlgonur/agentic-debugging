"""Adapt one protocol-1.3 JSON request to the installed OpenCode CLI.

This is a command for the existing JsonlCommandTransport contract, not a
controller or model adapter. OpenCode runs in a fresh empty directory and
receives the request only as a public attached file. The wrapper returns the
model's one JSON directive and records bounded, credential-redacted evidence.
In OpenCode Go route mode the wrapper independently recomputes the exact
selected catalog entry's deterministic fingerprint and compares it with the
authorization-bound expected fingerprint before any model process may run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any


_SECRET_KEY = re.compile(r"(?:api[_-]?key|access[_-]?key|auth(?:orization)?|credential|password|secret|token|private[_-]?key)", re.I)
_SECRET_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+\S+|\b(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|token)\s*[:=]\s*\S+")
_MAX_EVIDENCE_CHARS = 1_000_000
_MAX_EVIDENCE_FIELD_CHARS = 16_384
PROTOCOL_INSTRUCTION = (
    "Return exactly one protocol-1.3 directive JSON object. "
    "The attached public JSON request is the sole task context. "
    "Do not inspect repositories, edit files, run shell commands, or use unrelated tools."
)
_ISOLATION_PERMISSION_DENIALS = {
    "*": "deny",
    "read": "deny",
    "write": "deny",
    "edit": "deny",
    "bash": "deny",
    "external_directory": "deny",
    "webfetch": "deny",
    "websearch": "deny",
    "question": "deny",
    "task": "deny",
}

#: Explicit route modes.  ``legacy`` (default) preserves the historical
#: OpenCode Zen zero-price route behavior unchanged; ``opencode-go`` binds
#: the exact model, variant, OpenCode version, catalog fingerprint, runtime
#: model identity, account status, and billing-route evidence already
#: validated by the outer authorization/preflight contract and does not
#: require zero catalog prices.
ROUTE_MODES = ("legacy", "opencode-go")
_AGENTS_CONTENT = (
    "This task-owned workspace carries only the bounded public protocol request. "
    "Return one protocol directive; do not use tools, inspect repositories, edit files, or run shell commands."
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name in {"prompt_tokens", "completion_tokens", "total_tokens"} and (type(item) is int or item is None):
                result[name] = item
            else:
                result[name] = "<redacted>" if _SECRET_KEY.search(name) else _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("<redacted>", value)
    return value


class JsonExtractionError(ValueError):
    def __init__(self, classification: str, detail: str) -> None:
        super().__init__(detail)
        self.classification = classification


def _json_from_text(text: str) -> dict[str, Any]:
    """Extract exactly one top-level JSON object without protocol validation."""
    decoder = json.JSONDecoder()
    clean = _strip_ansi(text)
    candidates: list[dict[str, Any]] = []
    offset = 0
    while offset < len(clean):
        if clean[offset] != "{":
            offset += 1
            continue
        try:
            value, end = decoder.raw_decode(clean[offset:])
        except json.JSONDecodeError:
            offset += 1
            continue
        if isinstance(value, dict):
            candidates.append(value)
            offset += end
        else:
            offset += 1
    if not candidates:
        raise JsonExtractionError("no_json_object", "OpenCode output did not contain a directive JSON object")
    if len(candidates) > 1:
        raise JsonExtractionError("ambiguous_json_output", "OpenCode output contained multiple JSON objects")
    return candidates[0]


def _event_text(event: Any) -> list[str]:
    found: list[str] = []
    if isinstance(event, dict):
        if isinstance(event.get("text"), str):
            found.append(event["text"])
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            found.append(part["text"])
        for value in event.values():
            if value is event.get("text") or value is part:
                continue
            found.extend(_event_text(value))
    elif isinstance(event, list):
        for value in event:
            found.extend(_event_text(value))
    return found


def _provider_events(value: str) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    non_json: list[str] = []
    for line in _strip_ansi(value).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            non_json.append(line)
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            non_json.append(line)
    return events, non_json


def _event_type_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = event.get("type")
        name = str(event_type) if event_type is not None else "<missing>"
        counts[name] = counts.get(name, 0) + 1
    return counts


def _structured_error_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("type", "")).lower()
        if "error" in event_type or "error" in event:
            found.append(event)
    return found


def _parse_failure_classification(
    stdout: str,
    events: list[dict[str, Any]],
    text_parts: list[str],
    structured_errors: list[dict[str, Any]],
) -> str:
    if not stdout.strip():
        return "empty_output"
    if structured_errors:
        return "structured_provider_error"
    if text_parts:
        return "text_without_protocol_directive"
    if events:
        known_types = {"text", "step_finish", "step_start", "message", "assistant", "tool_use", "patch"}
        if any(str(event.get("type", "")) not in known_types for event in events):
            return "unsupported_event_shape"
        return "no_text_event"
    return "text_without_protocol_directive"


def _provider_diagnostics(stdout: str, stderr: str, returncode: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[str], dict[str, Any] | None]:
    events, non_json = _provider_events(stdout)
    text_parts: list[str] = []
    for event in events:
        text_parts.extend(_event_text(event))
    structured_errors = _structured_error_events(events)
    usage = _usage(events)
    telemetry = _provider_telemetry(events)
    record = {
        "event": "provider_result_diagnostics",
        "provider_exit_code": returncode,
        "provider_stdout": stdout,
        "provider_stderr": stderr,
        "provider_stdout_character_count": len(stdout),
        "provider_stderr_character_count": len(stderr),
        "provider_stdout_truncated": len(stdout) > _MAX_EVIDENCE_FIELD_CHARS,
        "provider_stderr_truncated": len(stderr) > _MAX_EVIDENCE_FIELD_CHARS,
        "parsed_event_count": len(events),
        "event_type_counts": _event_type_counts(events),
        "parsed_events": events,
        "non_json_line_count": len(non_json),
        "non_json_samples": non_json[:8],
        "extracted_text_part_count": len(text_parts),
        "extracted_text_values": text_parts,
        "structured_error_events": structured_errors,
        "provider_telemetry": telemetry,
        "usage": usage,
    }
    return record, events, text_parts, structured_errors, telemetry


def _usage(events: list[Any]) -> dict[str, Any] | None:
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        candidates = [event.get("usage")]
        part = event.get("part")
        if isinstance(part, dict):
            candidates.append(part.get("tokens"))
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            result: dict[str, Any] = {}
            for target, names in {
                "prompt_tokens": ("prompt_tokens", "promptTokens", "input_tokens", "inputTokens", "input"),
                "completion_tokens": ("completion_tokens", "completionTokens", "output_tokens", "outputTokens", "output"),
                "total_tokens": ("total_tokens", "totalTokens", "total"),
            }.items():
                for name in names:
                    if type(candidate.get(name)) is int and candidate[name] >= 0:
                        result[target] = candidate[name]
                        break
            if result:
                return result
    return None


def _numeric(value: Any) -> int | float | None:
    if type(value) in (int, float):
        return value
    return None


def _provider_telemetry(events: list[Any]) -> dict[str, Any] | None:
    """Collect fields emitted by OpenCode without filling absent fields.

    Independently observed identity fields (``observed_model``,
    ``observed_billing_route``, ``observed_model_substitution``) are passed
    through when a provider emits them so the outer execution adapter can
    revalidate the runtime identity binding from provider-reported state.
    """
    result: dict[str, Any] = {}
    _OBSERVED_IDENTITY_KEYS = ("observed_model", "observed_billing_route", "observed_model_substitution")

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"input", "output", "reasoning", "cost"}:
                    numeric = _numeric(item)
                    if numeric is not None:
                        result[key] = numeric
                elif key in _OBSERVED_IDENTITY_KEYS and item is not None:
                    result[key] = item
                elif key == "cache" and isinstance(item, dict):
                    cache: dict[str, Any] = dict(result.get("cache", {}))
                    for cache_key in ("read", "write"):
                        numeric = _numeric(item.get(cache_key))
                        if numeric is not None:
                            cache[cache_key] = numeric
                    if cache:
                        result["cache"] = cache
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for event in events:
        visit(event)
    return result or None


def _bound_evidence(value: Any, limit: int = _MAX_EVIDENCE_FIELD_CHARS) -> Any:
    """Bound strings before JSON serialization while retaining truncation facts."""
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return {"value": value[:limit], "truncated": True, "original_character_count": len(value)}
    if isinstance(value, dict):
        return {str(key): _bound_evidence(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_bound_evidence(item, limit) for item in value]
    return value


def _record(path: Path | None, record: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    redacted = _redact(record)
    payload = json.dumps(_bound_evidence(redacted), ensure_ascii=False, sort_keys=True)
    if len(payload) > _MAX_EVIDENCE_CHARS:
        original_character_count = len(payload)
        compact = {"truncated": True, "original_character_count": original_character_count, "record": _bound_evidence(redacted, limit=1_024)}
        payload = json.dumps(compact, ensure_ascii=False, sort_keys=True)
        if len(payload) > _MAX_EVIDENCE_CHARS:
            payload = json.dumps({
                "truncated": True, "original_character_count": original_character_count,
                "record_type": record.get("event", "transport"),
            }, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(payload + "\n")


def build_opencode_command(
    model: str,
    variant: str,
    root: Path,
    request_file: Path,
    message: str = PROTOCOL_INSTRUCTION,
) -> list[str]:
    if not isinstance(message, str) or not message.strip():
        raise ValueError("OpenCode positional protocol message must be non-empty")
    return [
        "opencode.cmd", "run", message, "--pure", "--format", "json", "--model", model, "--variant", variant,
        "--dir", str(root), "--file", str(request_file),
    ]


def _windows_profile_path() -> Path:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, 40, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise RuntimeError("required Windows user profile could not be determined")
    profile = Path(buffer.value)
    if not profile.is_absolute() or not profile.is_dir():
        raise RuntimeError("Windows profile API returned an untrusted profile path")
    return profile


def _profile_path() -> Path:
    try:
        profile = Path.home()
        if profile.is_absolute() and profile.is_dir():
            return profile
    except RuntimeError:
        pass
    if os.name != "nt":
        raise RuntimeError("required user profile could not be determined")
    return _windows_profile_path()


def _auth_state_path() -> Path:
    profile = _profile_path()
    return profile / ".local" / "share" / "opencode" / "auth.json"


def _isolation_config() -> dict[str, Any]:
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": dict(_ISOLATION_PERMISSION_DENIALS),
        "mcp": {"*": {"enabled": False}},
        "plugin": [],
        "instructions": [],
        "share": "disabled",
        "enabled_providers": ["opencode"],
        "autoupdate": False,
    }


def _prepare_isolation(root: Path) -> dict[str, Any]:
    isolation_root = root / "opencode-isolation"
    config_home = isolation_root / "config-home"
    data_home = isolation_root / "data-home"
    state_home = isolation_root / "state-home"
    cache_home = isolation_root / "cache-home"
    for path in (config_home, data_home, state_home, cache_home):
        path.mkdir(parents=True, exist_ok=True)
    config_path = isolation_root / "opencode.json"
    config = _isolation_config()
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    agents_path = root / "AGENTS.md"
    agents_path.write_text(_AGENTS_CONTENT + "\n", encoding="utf-8")
    auth_source = _auth_state_path()
    if not auth_source.is_file():
        raise RuntimeError("required OpenCode authentication state is unavailable")
    auth_copy = data_home / "opencode" / "auth.json"
    auth_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(auth_source, auth_copy)
    inherited_names = ("PATH", "PATHEXT", "SystemRoot", "WINDIR", "COMSPEC")
    environment = {name: os.environ[name] for name in inherited_names if os.environ.get(name)}
    home_drive, home_path = os.path.splitdrive(str(isolation_root))
    environment.update({
        "HOME": str(isolation_root),
        "USERPROFILE": str(isolation_root),
        "HOMEDRIVE": home_drive,
        "HOMEPATH": home_path,
        "APPDATA": str(isolation_root / "appdata"),
        "LOCALAPPDATA": str(isolation_root / "localappdata"),
        "TEMP": str(isolation_root / "tmp"),
        "TMP": str(isolation_root / "tmp"),
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_CONFIG_DIR": str(config_home),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
        "XDG_CACHE_HOME": str(cache_home),
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
    })
    Path(environment["TEMP"]).mkdir(parents=True, exist_ok=True)
    return {
        "environment": environment,
        "config_path": config_path,
        "config": config,
        "agents_path": agents_path,
        "agents_sha256": hashlib.sha256(agents_path.read_bytes()).hexdigest(),
        "auth_copy": auth_copy,
        "auth_sha256": hashlib.sha256(auth_copy.read_bytes()).hexdigest(),
    }


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def _validate_effective_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise RuntimeError("OpenCode effective configuration was not an object")
    permission = config.get("permission")
    if not isinstance(permission, dict):
        raise RuntimeError("OpenCode effective configuration has no permission object")
    required_denials = ("read", "write", "edit", "bash", "task", "webfetch", "websearch", "external_directory")
    if permission.get("*") != "deny" or any(permission.get(name) != "deny" for name in required_denials):
        raise RuntimeError("OpenCode effective configuration does not deny required permissions")
    mcp = config.get("mcp")
    if not isinstance(mcp, dict) or any(
        not isinstance(value, dict) or value.get("enabled") is not False
        for value in mcp.values()
    ):
        raise RuntimeError("OpenCode effective configuration enables an MCP server")
    if config.get("plugin") != []:
        raise RuntimeError("OpenCode effective configuration enables a plugin")
    if config.get("instructions") != []:
        raise RuntimeError("OpenCode effective configuration includes unrelated instructions")
    if config.get("share") != "disabled":
        raise RuntimeError("OpenCode sharing is not disabled")
    if config.get("enabled_providers") != ["opencode"]:
        raise RuntimeError("OpenCode enabled provider allowlist is not exactly opencode")
    if config.get("autoupdate") is not False:
        raise RuntimeError("OpenCode autoupdate is not disabled")
    return {
        "permission_default_denied": True,
        "required_permissions_denied": list(required_denials),
        "mcp_servers_disabled": True,
        "plugins_empty": True,
        "instructions_empty": True,
        "sharing_disabled": True,
        "enabled_providers": ["opencode"],
        "autoupdate_disabled": True,
    }


def verify_opencode_effective_config(
    environment: dict[str, str],
    cwd: Path,
) -> dict[str, Any]:
    completed = subprocess.run(
        ["opencode.cmd", "debug", "config", "--pure"],
        cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        env=environment,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"OpenCode effective config failed with exit code {completed.returncode}")
    try:
        config = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenCode effective config was not valid JSON") from exc
    return _validate_effective_config(config)


def _preflight(args: argparse.Namespace) -> int:
    """Perform all local wrapper gates without invoking ``opencode run``."""
    root = Path(tempfile.gettempdir()) / f"agentic-opencode-preflight-{uuid.uuid4().hex}"
    root.mkdir()
    evidence_path = Path(args.evidence_file) if args.evidence_file else None
    try:
        isolation = _prepare_isolation(root)
        route_binding = _route_binding_evidence(args) if args.route_mode == "opencode-go" else None
        launcher = verify_opencode_launcher(
            isolation["environment"],
            expected_version=route_binding["expected_opencode_version"] if route_binding is not None else None,
        )
        catalog = verify_opencode_catalog(
            args.model, args.variant, isolation["environment"], cwd=root,
            route_mode=args.route_mode,
            expected_runtime_model_id=route_binding["expected_runtime_model_id"] if route_binding is not None else None,
            expected_catalog_fingerprint=route_binding["expected_catalog_fingerprint"] if route_binding is not None else None,
        )
        effective_config = verify_opencode_effective_config(isolation["environment"], cwd=root)
        request_file = root / "public-request.json"
        request_file.write_text("{}\n", encoding="utf-8")
        command = build_opencode_command(args.model, args.variant, root, request_file)
        if not command[command.index("run") + 1].strip() or command.index(command[2]) > command.index("--file"):
            raise RuntimeError("final OpenCode command failed message ordering validation")
        assertions = {
            "preflight": "passed",
            "provider_inference_started": False,
            "route_mode": args.route_mode,
            "launcher": launcher,
            "catalog": catalog,
            "effective_config": effective_config,
            "message_nonempty": bool(command[2].strip()),
            "message_before_file": command.index(command[2]) < command.index("--file"),
            "trailing_positional_values_after_file": command.index("--file") + 2 < len(command),
            "agents_present_during_preflight": isolation["agents_path"].is_file(),
            "config_copy_present_during_preflight": isolation["config_path"].is_file(),
            "auth_copy_present_during_preflight": isolation["auth_copy"].is_file(),
            "auth_source_resolved": True,
            "auth_sha256": isolation["auth_sha256"],
            "config_sha256": hashlib.sha256(isolation["config_path"].read_bytes()).hexdigest(),
            "agents_sha256": isolation["agents_sha256"],
            "command": command,
        }
        if route_binding is not None:
            assertions["route_binding"] = route_binding
        _record(evidence_path, assertions)
        print(json.dumps(assertions, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        failure = {"preflight": "blocked", "provider_inference_started": False, "error": f"{type(exc).__name__}: {exc}"}
        _record(evidence_path, failure)
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        return 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _expected_opencode_version(args: argparse.Namespace) -> str | None:
    if args.route_mode != "opencode-go":
        return None
    value = (args.expected_opencode_version or "").strip()
    if not value:
        raise RuntimeError("OpenCode Go mode requires --expected-opencode-version")
    return value


def _expected_runtime_model_id(args: argparse.Namespace) -> str | None:
    if args.route_mode != "opencode-go":
        return None
    value = (args.expected_runtime_model_id or "").strip()
    if not value:
        raise RuntimeError("OpenCode Go mode requires --expected-runtime-model-id")
    return value


def _route_binding_evidence(args: argparse.Namespace) -> dict[str, Any]:
    """The identity/route binding carried into OpenCode Go mode.

    Every value was already validated by the outer authorization/preflight
    contract; the wrapper requires them explicitly (fail closed on absence)
    and records them, without re-querying any catalog/account/entitlement
    service and without inferring Zen/free-tier use.
    """
    fingerprint = (args.expected_catalog_fingerprint or "").strip()
    if not (len(fingerprint) == 64 and all(char in "0123456789abcdef" for char in fingerprint)):
        raise RuntimeError("OpenCode Go mode requires --expected-catalog-fingerprint as a 64-hex string")
    account_status = (args.expected_account_status or "").strip()
    if not account_status:
        raise RuntimeError("OpenCode Go mode requires --expected-account-status")
    billing_route = (args.expected_billing_route or "").strip()
    if not billing_route:
        raise RuntimeError("OpenCode Go mode requires --expected-billing-route")
    expected_version = _expected_opencode_version(args)
    expected_runtime_model_id = _expected_runtime_model_id(args)
    if args.model != expected_runtime_model_id:
        raise RuntimeError(
            f"model identity {args.model!r} does not match the expected runtime model identity {expected_runtime_model_id!r}"
        )
    return {
        "expected_runtime_model_id": expected_runtime_model_id,
        "expected_opencode_version": expected_version,
        "expected_catalog_fingerprint": fingerprint,
        "expected_account_status": account_status,
        "expected_billing_route": billing_route,
    }


def _json_objects(value: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    clean = _strip_ansi(value)
    for offset, char in enumerate(clean):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(clean[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            objects.append(candidate)
    return objects


def catalog_entry_fingerprint(entry: Mapping[str, Any]) -> str:
    """The deterministic catalog-entry fingerprint contract.

    The exact selected catalog entry is serialized with the project's
    canonical JSON rules (sorted keys, compact separators, ASCII-escaped,
    strict finite JSON — the same canonical rules used by the paired-pilot
    validators) and SHA-256 of that canonical representation is returned.
    The same independently recomputed fingerprint is used in route evidence,
    the operator authorization, the adapter configuration, and the wrapper's
    OpenCode Go preflight comparison.
    """
    return hashlib.sha256(
        json.dumps(
            entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def select_catalog_entry(catalog_stdout: str, model: str) -> dict[str, Any]:
    """Locate exactly one active catalog entry for the exact model identity.

    ``model`` must be a catalog-qualified identity (``provider/id``).  Zero
    matches, multiple matches, or a non-object catalog shape fail closed.
    """
    provider, model_id = model.split("/", 1) if "/" in model else ("", model)
    entries = [
        item for item in _json_objects(catalog_stdout)
        if item.get("providerID") == provider and item.get("id") == model_id
    ]
    if len(entries) != 1:
        raise RuntimeError("exact OpenCode model was not uniquely present in the local catalog")
    return entries[0]


def catalog_entry_facts(entry: Mapping[str, Any], variant: str) -> dict[str, Any]:
    """Observed status, variant availability, and finite pricing metadata for
    the exact selected catalog entry.

    Shared by the wrapper's OpenCode Go preflight and the operator route
    capture so the observed facts and the fingerprint always come from one
    coherent parsing path.  Rejects inactive status, malformed or non-finite
    pricing metadata, and a missing requested variant.
    """
    costs = entry.get("cost")
    cache = costs.get("cache") if isinstance(costs, dict) else None
    if entry.get("status") != "active" or not isinstance(costs, dict) or not isinstance(cache, dict):
        raise RuntimeError("exact OpenCode model is not active or has incomplete pricing metadata")
    for name in ("input", "output"):
        value = costs.get(name)
        if type(value) not in (int, float) or isinstance(value, bool) or value < 0:
            raise RuntimeError("exact OpenCode model has malformed pricing metadata")
    for name in ("read", "write"):
        value = cache.get(name)
        if type(value) not in (int, float) or isinstance(value, bool) or value < 0:
            raise RuntimeError("exact OpenCode model has malformed cache pricing metadata")
    variants = entry.get("variants")
    if not isinstance(variants, dict) or variant not in variants:
        raise RuntimeError("requested OpenCode model variant is unavailable")
    return {
        "active_model_status": "ACTIVE",
        "variant_available": True,
        "input_price": costs["input"],
        "output_price": costs["output"],
        "cache_read_price": cache["read"],
        "cache_write_price": cache["write"],
    }


def verify_opencode_launcher(environment: dict[str, str] | None = None, *, expected_version: str | None = None) -> dict[str, Any]:
    """Verify the installed Windows launcher without contacting a model.

    ``expected_version`` (OpenCode Go mode) requires the observed launcher
    version to equal the authorization-bound version exactly; the legacy mode
    keeps the historical behavior (any non-empty version).
    """
    launcher = shutil.which("opencode.cmd")
    if not launcher:
        raise RuntimeError("opencode.cmd was not found on PATH")
    completed = subprocess.run(
        ["opencode.cmd", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", timeout=30, check=False, env=environment,
    )
    version = (completed.stdout or completed.stderr or "").strip()
    evidence = {"launcher": "opencode.cmd", "resolved_path": launcher, "returncode": completed.returncode, "version": version}
    if completed.returncode != 0 or not version:
        raise RuntimeError(f"opencode.cmd version preflight failed: {_redact(evidence)}")
    if expected_version is not None:
        if version != expected_version:
            raise RuntimeError(f"OpenCode version drift: observed {version!r} != expected {expected_version!r}")
        evidence["version_matches_expected"] = True
    return evidence


def verify_opencode_catalog(
    model: str,
    variant: str,
    environment: dict[str, str],
    cwd: Path | None = None,
    *,
    route_mode: str = "legacy",
    expected_runtime_model_id: str | None = None,
    expected_catalog_fingerprint: str | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        ["opencode.cmd", "models", "opencode", "--verbose", "--pure"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", timeout=30, check=False, env=environment,
        cwd=str(cwd) if cwd is not None else None,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"OpenCode model catalog failed with exit code {completed.returncode}")
    entry = select_catalog_entry(completed.stdout, model)
    facts = catalog_entry_facts(entry, variant)
    fingerprint = catalog_entry_fingerprint(entry)
    if route_mode not in ROUTE_MODES:
        raise RuntimeError(f"unsupported route mode: {route_mode!r}")
    if route_mode == "legacy":
        costs = entry.get("cost")
        if any(costs.get(name) != 0 for name in ("input", "output")) or any(costs["cache"].get(name) != 0 for name in ("read", "write")):
            raise RuntimeError("exact OpenCode model is not zero-cost")
    elif route_mode == "opencode-go":
        if expected_runtime_model_id is not None and model != expected_runtime_model_id:
            raise RuntimeError(
                f"model identity {model!r} does not match the expected runtime model identity {expected_runtime_model_id!r}"
            )
        if expected_catalog_fingerprint is None:
            raise RuntimeError("OpenCode Go mode requires --expected-catalog-fingerprint")
        recomputed = fingerprint
        if recomputed != expected_catalog_fingerprint:
            raise RuntimeError(
                f"catalog fingerprint drift: independently recomputed fingerprint {recomputed} "
                f"does not equal the authorization-bound expected fingerprint {expected_catalog_fingerprint}"
            )
        # OpenCode Go mode: catalog prices are preserved as observed; no
        # hidden fallback, model selection, or Zen/free-tier inference.
    return {
        "model": model,
        "active": True,
        "zero_cost": facts["input_price"] == 0 and facts["output_price"] == 0,
        "catalog_fingerprint": fingerprint,
        "variant": variant,
        "variant_available": True,
        "route_mode": route_mode,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--evidence-file")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--route-mode", choices=ROUTE_MODES, default="legacy")
    parser.add_argument("--expected-opencode-version")
    parser.add_argument("--expected-catalog-fingerprint")
    parser.add_argument("--expected-runtime-model-id")
    parser.add_argument("--expected-account-status")
    parser.add_argument("--expected-billing-route")
    args = parser.parse_args(argv)
    if args.preflight:
        return _preflight(args)
    request_line = sys.stdin.readline()
    try:
        request = json.loads(request_line)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"invalid protocol request: {exc}", file=sys.stderr)
        return 2

    root = Path(tempfile.gettempdir()) / f"agentic-opencode-transport-{uuid.uuid4().hex}"
    root.mkdir()
    command: list[str] = []
    try:
        request_file = root / "public-request.json"
        request_file.write_text(json.dumps(request, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        isolation = _prepare_isolation(root)
        route_binding = _route_binding_evidence(args) if args.route_mode == "opencode-go" else None
        launcher = verify_opencode_launcher(
            isolation["environment"],
            expected_version=route_binding["expected_opencode_version"] if route_binding is not None else None,
        )
        catalog = verify_opencode_catalog(
            args.model, args.variant, isolation["environment"], cwd=root,
            route_mode=args.route_mode,
            expected_runtime_model_id=route_binding["expected_runtime_model_id"] if route_binding is not None else None,
            expected_catalog_fingerprint=route_binding["expected_catalog_fingerprint"] if route_binding is not None else None,
        )
        effective_config = verify_opencode_effective_config(isolation["environment"], cwd=root)
        command = build_opencode_command(args.model, args.variant, root, request_file)
        evidence_path = Path(args.evidence_file) if args.evidence_file else None
        _record(evidence_path, {
            "event": "transport_preflight",
            "route_mode": args.route_mode,
            "route_binding": route_binding,
            "launcher": launcher,
            "catalog": catalog,
            "effective_config": effective_config,
            "command": command,
            "message_length": len(PROTOCOL_INSTRUCTION),
            "message_before_file": command.index(PROTOCOL_INSTRUCTION) < command.index("--file"),
            "isolation": {
                "config_path": str(isolation["config_path"]),
                "config_sha256": hashlib.sha256(isolation["config_path"].read_bytes()).hexdigest(),
                "agents_path": str(isolation["agents_path"]),
                "agents_sha256": isolation["agents_sha256"],
                "agents_present_during_preflight": isolation["agents_path"].is_file(),
                "auth_state_copied": True,
                "auth_sha256": isolation["auth_sha256"],
                "mcp_disabled": True,
                "plugins_disabled": True,
                "instructions_cleared": True,
                "project_config_disabled": True,
            },
        })
        completed = subprocess.run(
            command, cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=300, check=False,
            env=isolation["environment"],
        )
        raw_stdout = completed.stdout
        raw_stderr = completed.stderr
        diagnostics, events, text_parts, structured_errors, telemetry = _provider_diagnostics(
            raw_stdout, raw_stderr, completed.returncode,
        )
        _record(Path(args.evidence_file) if args.evidence_file else None, diagnostics)
        if completed.returncode != 0:
            _record(Path(args.evidence_file) if args.evidence_file else None, {
                "event": "provider_exit_failure", "model": args.model, "variant": args.variant,
                "command": command, "request": request, "provider_exit_code": completed.returncode,
                "provider_stdout": raw_stdout, "provider_stderr": raw_stderr,
                "error": "OpenCode exited nonzero; directive parsing was not attempted",
            })
            print(f"OpenCode transport failed: provider exited with code {completed.returncode}", file=sys.stderr)
            return 1
        if structured_errors or (events and not text_parts):
            classification = _parse_failure_classification(raw_stdout, events, text_parts, structured_errors)
            _record(Path(args.evidence_file) if args.evidence_file else None, {
                "event": "directive_extraction_failure",
                "failure_classification": classification,
                "error": "provider output did not contain an extractable assistant directive text",
            })
            raise JsonExtractionError(classification, "provider output did not contain an extractable assistant directive text")
        text = "\n".join(text_parts) if text_parts else raw_stdout
        try:
            directive = _json_from_text(text)
        except ValueError as exc:
            classification = (
                "ambiguous_json_output"
                if getattr(exc, "classification", None) == "ambiguous_json_output"
                else _parse_failure_classification(raw_stdout, events, text_parts, structured_errors)
            )
            _record(Path(args.evidence_file) if args.evidence_file else None, {
                "event": "directive_extraction_failure",
                "failure_classification": classification,
                "error": f"{type(exc).__name__}: {exc}",
            })
            raise
        response: dict[str, Any] = {"directive": directive}
        usage = _usage(events)
        telemetry = _provider_telemetry(events)
        if usage:
            response["usage"] = usage
        if telemetry:
            response["provider_telemetry"] = telemetry
        _record(Path(args.evidence_file) if args.evidence_file else None, {
            "model": args.model, "variant": args.variant, "command": command, "request": request,
            "provider_exit_code": completed.returncode, "provider_stdout": raw_stdout,
            "provider_stderr": raw_stderr, "response": response, "usage": usage,
            "provider_telemetry": telemetry,
        })
        print(json.dumps(response, ensure_ascii=False), flush=True)
        return 0
    except subprocess.TimeoutExpired as exc:
        _record(Path(args.evidence_file) if args.evidence_file else None, {
            "event": "provider_timeout", "model": args.model, "variant": args.variant,
            "command": command, "request": request, "provider_stdout": exc.stdout or exc.output or "",
            "provider_stderr": exc.stderr or "", "error": f"TimeoutExpired: {exc}",
        })
        print(f"OpenCode transport failed: TimeoutExpired: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        _record(Path(args.evidence_file) if args.evidence_file else None, {
            "model": args.model, "variant": args.variant, "command": command,
            "request": request, "error": f"{type(exc).__name__}: {exc}",
        })
        print(f"OpenCode transport failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
