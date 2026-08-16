"""AGY Gemini 3.7 Flash command adapter for Local Application V1.

Adapts the accepted protocol-1.3 JSON-lines command-model contract of Local
Application V1 to ONE bounded non-interactive AGY ``--print`` invocation of
the operator-exposed Gemini 3.7 Flash family through Antigravity CLI.

Architecture:

    Local Application protocol-1.3 request
        -> dedicated AGY adapter
        -> one fresh non-interactive ``agy --print``
        -> Gemini 3.7 Flash
        -> strict final directive
        -> accepted configured-command response envelope

The adapter does not execute source tools, tests, PDB, patches, or the
verifier.  AGY is the decision model only.  Each request writes a temporary
capability-free custom MAIN agent and pins ``--agent local-application-decision``.
The decision-only AGY agent may expose only the explicitly audited intrinsic
control-plane capabilities in the init inventory.  No task/execution
capability is accepted.  Any later tool or subagent event is also rejected,
even if the terminal result contains a valid directive.  A temporary AGY
``PreToolUse`` hook independently denies every actual tool invocation before
execution.

Process contract: one Local Application request owns exactly one adapter-owned
``agy --print`` process.  Adapter-level retry is 0.  Adapter-level fallback is
0.  A second AGY process is never spawned for the same request.  There is no
persistent conversation.  AGY-internal transient generation retries are
provider/CLI-owned behavior; this adapter does not implement, request, or
disable them; they remain bounded by the 20-second process timeout.

Authentication uses the operator's existing native OS credential store; the
adapter never copies, parses, or emits Google credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

try:
    import agy_executable_identity as executable_identity
except ImportError:  # pragma: no cover - defensive import path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import agy_executable_identity as executable_identity


DEFAULT_MODEL_ID = "gemini-3.7-flash-medium"
FIRST_RUN_MODEL_ID = "gemini-3.7-flash-medium"
ALLOWED_MODEL_IDENTIFIERS = frozenset({
    "gemini-3.7-flash-low",
    "gemini-3.7-flash-medium",
    "gemini-3.7-flash-high",
})

DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_RAW_RESPONSE_BYTES = 64 * 1024
MAX_PUBLIC_REQUEST_BYTES = 25_000
MAX_NATIVE_COMMAND_LINE_CHARS = 30_000
DEFAULT_MAX_LOGICAL_MODEL_CALLS = 25
EXPECTED_AGY_VERSION = executable_identity.EXPECTED_AGY_VERSION
_PREFLIGHT_TIMEOUT_SECONDS = 15.0

DECISION_AGENT_NAME = "local-application-decision"
DECISION_AGENT_RELATIVE = Path(".gemini") / "config" / "agents" / DECISION_AGENT_NAME / "agent.md"
EMPTY_MCP_CONFIG = {"mcpServers": {}}
MCP_CLI_RELATIVE = Path(".gemini") / "antigravity-cli" / "mcp_config.json"
MCP_WORKSPACE_RELATIVE = Path(".agents") / "mcp_config.json"
MCP_CONFIG_RELATIVE = Path(".gemini") / "config" / "mcp_config.json"
PRE_TOOL_USE_HOOK_SCRIPT_NAME = "agy-decision-only-deny-hook.py"
PRE_TOOL_USE_HOOK_CONFIG_RELATIVE = Path(".gemini") / "config" / "hooks.json"
PRE_TOOL_USE_HOOK_NAME = "local-application-decision-deny-all"
PRE_TOOL_USE_HOOK_REASON = (
    "Local Application decision-only model: tool execution disabled"
)
MAX_PRE_TOOL_USE_HOOK_INPUT_BYTES = 64 * 1024

PUBLIC_REQUEST_START = "=== BEGIN PUBLIC REQUEST ==="
PUBLIC_REQUEST_END = "=== END PUBLIC REQUEST ==="

MAX_DIRECTIVE_ARGUMENT_BYTES = 32_768
MAX_DIRECTIVE_REASON_BYTES = 2_048
MAX_DIRECTIVE_STATEMENT_BYTES = 4_096
MAX_DIRECTIVE_HYPOTHESIS_ID_BYTES = 128
MAX_DIRECTIVE_EVIDENCE_REF_BYTES = 256
MAX_DIRECTIVE_EVIDENCE_REF_COUNT = 64

HYPOTHESIS_CONFIDENCE_VALUES = ("low", "medium", "high")
HYPOTHESIS_STATUS_VALUES = ("supported", "rejected", "discarded")

DIRECTIVE_TOP_LEVEL_FIELDS = {
    "action": frozenset({"kind", "name", "arguments"}),
    "transition": frozenset({"kind", "target_state", "reason"}),
    "add_hypothesis": frozenset({
        "kind", "hypothesis_id", "statement", "confidence",
        "evidence_refs", "requires_runtime_evidence",
    }),
    "revise_hypothesis": frozenset({
        "kind", "hypothesis_id", "statement", "confidence",
        "evidence_refs", "requires_runtime_evidence",
    }),
    "set_hypothesis_status": frozenset({"kind", "hypothesis_id", "status"}),
}

SAFE_STREAM_EVENTS = frozenset({"init", "step_update", "result"})
SAFE_STEP_TYPES = frozenset({
    "user_input",
    "agent_response",
    "checkpoint",
    "reasoning",
    "thinking",
    "thought",
})
DOCUMENTED_INTRINSIC_INIT_CAPABILITIES = frozenset({
    "ask_permission",
    "ask_question",
    "list_permissions",
})
# Compatibility alias for callers that used the prior constant name.  The
# audited set above remains the single source of truth.
ALLOWED_INTRINSIC_INIT_CAPABILITIES = DOCUMENTED_INTRINSIC_INIT_CAPABILITIES
FORBIDDEN_STEP_TYPES = frozenset({
    "tool",
    "subagent",
    "skill",
    "mcp",
    "command",
    "bash",
    "terminal",
    "web",
    "search",
})

_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:bearer\s+[a-zA-Z0-9._~+/-]+=*|sk-[a-zA-Z0-9_-]{10,}|(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|token)\s*[:=]\s*\S+)",
)

SYSTEM_PROMPT = (
    "You are a debugging decision model for Local Application V1.\n"
    "Do not inspect the filesystem. Do not use tools or subagents.\n"
    "Use only the supplied public request.\n"
    "Emit exactly one legal directive matching the supplied contracts.\n"
    "No prose or markdown.\n"
    "Local Application performs every actual action."
)

DECISION_AGENT_MARKDOWN = (
    "---\n"
    f"name: {DECISION_AGENT_NAME}\n"
    "description: Local Application protocol decision model only\n"
    "tools: []\n"
    "mainAgent: true\n"
    "subagent: false\n"
    "commandExecutionPolicy: off\n"
    "mcpServers: []\n"
    "skills: []\n"
    "plugins: []\n"
    "---\n"
    "\n"
    "You are only the decision model for Local Application V1.\n"
    "Use only the supplied request and return its required structured directive.\n"
    "Do not inspect files, execute commands, browse, delegate, or invoke tools.\n"
)

ISOLATION_SETTINGS = {
    "toolPermission": "strict",
    "enableTerminalSandbox": True,
    "allowNonWorkspaceAccess": False,
    "artifactReviewPolicy": "asks-for-review",
    "enableTelemetry": False,
    "permissions": {
        "allow": [],
        "ask": [],
        "deny": [
            "command(*)",
            "write_file(*)",
            "read_file(*)",
            "read_url(*)",
            "execute_url(*)",
            "unsandboxed(*)",
            "mcp(*)",
        ],
    },
}

PRE_TOOL_USE_HOOK_SCRIPT = f'''#!/usr/bin/env python3
"""Temporary Local Application AGY deny-all PreToolUse hook."""

import json
import sys

MAX_INPUT_BYTES = {MAX_PRE_TOOL_USE_HOOK_INPUT_BYTES}
DENY_RESPONSE = {json.dumps(json.dumps({"decision": "deny", "reason": PRE_TOOL_USE_HOOK_REASON}, sort_keys=True))}


def main() -> int:
    # Consume only a bounded amount of the documented JSON request.  The
    # decision is unconditional and does not inspect or persist its content.
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) <= MAX_INPUT_BYTES:
        try:
            json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    sys.stdout.write(DENY_RESPONSE + "\\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


# --- Redaction & Diagnostics ------------------------------------------------

def redact(text: str) -> str:
    """Redact credential-shaped tokens from strings before output."""
    if not isinstance(text, str) or not text:
        return ""
    return _SECRET_PATTERN.sub("<redacted_secret>", text)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


# --- Bounded Pipe Capture ---------------------------------------------------

class _BoundedCapture:
    """Thread-safe bounded byte capture for subprocess pipes."""

    def __init__(self, maximum_bytes: int) -> None:
        self.maximum_bytes = maximum_bytes
        self._data = bytearray()
        self.truncated = False
        self._lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        with self._lock:
            remaining = self.maximum_bytes - len(self._data)
            if remaining > 0:
                self._data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.truncated = True

    def text(self) -> str:
        with self._lock:
            return bytes(self._data).decode("utf-8", errors="replace")


def _read_pipe(pipe: Any, capture: _BoundedCapture) -> None:
    try:
        while True:
            chunk = pipe.read(4096)
            if not chunk:
                return
            capture.add(chunk)
    except Exception:
        return


def _terminate_command_tree(process: subprocess.Popen) -> None:
    """Terminate the process and all descendants promptly."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                shell=False,
            )
        except Exception:
            pass
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=1.0)
            except Exception:
                pass
        return
    pgid = None
    try:
        pgid = os.getpgid(process.pid)
        os.killpg(pgid, signal.SIGTERM)
        process.wait(timeout=1.0)
    except Exception:
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGKILL)
            process.wait(timeout=1.0)
        except Exception:
            pass


_INFLIGHT_CHILD_GROUPS_LOCK = threading.RLock()
_INFLIGHT_CHILD_GROUPS: list[int] = []
_CHILD_GROUP_GRACE_SECONDS = 0.2


def register_inflight_child_group(group_id: int) -> None:
    if sys.platform == "win32":
        return
    if type(group_id) is not int or isinstance(group_id, bool) or group_id <= 0:
        return
    with _INFLIGHT_CHILD_GROUPS_LOCK:
        _INFLIGHT_CHILD_GROUPS.append(group_id)


def unregister_inflight_child_group(group_id: int) -> None:
    if sys.platform == "win32":
        return
    with _INFLIGHT_CHILD_GROUPS_LOCK:
        try:
            _INFLIGHT_CHILD_GROUPS.remove(group_id)
        except ValueError:
            pass


def _snapshot_inflight_child_groups() -> list[int]:
    with _INFLIGHT_CHILD_GROUPS_LOCK:
        return list(_INFLIGHT_CHILD_GROUPS)


def _terminate_inflight_child_groups() -> None:
    for group_id in _snapshot_inflight_child_groups():
        try:
            os.killpg(group_id, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        time.sleep(_CHILD_GROUP_GRACE_SECONDS)
        try:
            os.killpg(group_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            continue


def _external_signal_handler(signum, frame):  # pragma: no cover - signal path
    try:
        _terminate_inflight_child_groups()
    finally:
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except Exception:
            os._exit(128 + int(signum))


def install_child_group_signal_handlers() -> None:
    if sys.platform == "win32":
        return
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _external_signal_handler)
        except (ValueError, OSError):
            continue


# --- Prompt Construction ----------------------------------------------------

def canonical_public_request(request: Mapping[str, Any]) -> str:
    return json.dumps(
        request,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def build_protocol_message(request: Mapping[str, Any]) -> str:
    if not isinstance(request, Mapping):
        raise ValueError("protocol request must be a JSON object")
    canonical = canonical_public_request(request)
    byte_count = len(canonical.encode("utf-8"))
    if byte_count > MAX_PUBLIC_REQUEST_BYTES:
        raise ValueError(
            f"canonical public request exceeds the Local Application ceiling "
            f"({byte_count} > {MAX_PUBLIC_REQUEST_BYTES} bytes)"
        )
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"{PUBLIC_REQUEST_START}\n"
        f"{canonical}\n"
        f"{PUBLIC_REQUEST_END}"
    )


def validate_logical_call_index(request: Mapping[str, Any], max_logical_calls: int) -> Optional[str]:
    if type(max_logical_calls) is not int or isinstance(max_logical_calls, bool) or max_logical_calls < 1:
        return "max logical model calls must be a positive integer"
    protocol = request.get("protocol")
    if not isinstance(protocol, Mapping):
        return "request carries no protocol metadata"
    index = protocol.get("logical_model_call_index")
    if type(index) is not int or isinstance(index, bool):
        return "protocol.logical_model_call_index must be an integer"
    if index < 1:
        return f"logical model call index {index} is below the first allowed index (1)"
    if index > max_logical_calls:
        return f"logical model call index {index} exceeds the micro-run envelope (max {max_logical_calls})"
    return None


# --- Strict Directive Validation --------------------------------------------

def _validate_text_field(value: Any, maximum_bytes: int) -> Optional[str]:
    if type(value) is not str or not value or value != value.strip():
        return "text field must be a non-empty string without surrounding whitespace"
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return "text field is not valid UTF-8"
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return "text field contains control characters"
    if len(encoded) > maximum_bytes:
        return "text field exceeds the byte bound"
    return None


def _validate_action_arguments(name: str, arguments: Any, contracts: Mapping[str, Any]) -> Optional[str]:
    contract = contracts.get(name)
    if not isinstance(contract, Mapping):
        return f"action '{name}' has no embedded argument contract"
    properties = contract.get("properties")
    if not isinstance(properties, Mapping):
        properties = contract
    required = contract.get("required")
    if isinstance(required, list):
        for field in required:
            if field not in arguments:
                return f"missing required argument '{field}'"
    if contract.get("additional_properties") is False:
        unknown = set(arguments) - set(properties)
        if unknown:
            return f"unknown argument field '{sorted(unknown)[0]}'"
    for field, spec in properties.items():
        if field not in arguments or not isinstance(spec, Mapping):
            continue
        value = arguments[field]
        type_name = spec.get("type")
        type_ok = {
            "string": type(value) is str,
            "integer": type(value) is int and not isinstance(value, bool),
            "number": type(value) in (int, float) and not isinstance(value, bool),
            "boolean": type(value) is bool,
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
            "null": value is None,
        }.get(type_name, True)
        if not type_ok:
            return f"argument '{field}' has the wrong type"
        enum = spec.get("enum")
        if isinstance(enum, list) and value not in enum:
            return f"argument '{field}' must be one of {list(enum)}"
        if type_name == "string" and type(value) is str and isinstance(spec.get("min_length"), int) and len(value) < spec["min_length"]:
            return f"argument '{field}' is too short"
    return None


def validate_directive_candidate(candidate: Any, request: Mapping[str, Any]) -> Optional[str]:
    """Strictly validate one candidate against the embedded request schema."""
    if not isinstance(candidate, Mapping):
        return "directive must be a JSON object"
    schema = request.get("directive_schema")
    if not isinstance(schema, (list, tuple, set, frozenset)) or not schema:
        return "request carries no directive schema"
    kind = candidate.get("kind")
    if type(kind) is not str or kind not in schema:
        return "unrecognized or missing directive 'kind'"
    unknown_fields = set(candidate) - DIRECTIVE_TOP_LEVEL_FIELDS.get(kind, frozenset())
    if unknown_fields:
        return f"unknown top-level field '{sorted(unknown_fields)[0]}'"
    controller = request.get("controller") if isinstance(request.get("controller"), Mapping) else {}
    state = controller.get("state")
    if kind == "action":
        name = candidate.get("name")
        if type(name) is not str:
            return "unrecognized action name"
        contracts = request.get("action_contracts") if isinstance(request.get("action_contracts"), Mapping) else {}
        if name not in contracts:
            return f"action '{name}' is not allowed in state '{state}'"
        allowed = controller.get("allowed_actions")
        if isinstance(allowed, list) and name not in allowed:
            return f"action '{name}' is not allowed in state '{state}'"
        arguments = candidate.get("arguments")
        if not isinstance(arguments, Mapping):
            return "'arguments' must be a JSON object"
        failure = _validate_action_arguments(name, arguments, contracts)
        if failure is not None:
            return failure
        try:
            serialized = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            if len(serialized.encode("utf-8")) > MAX_DIRECTIVE_ARGUMENT_BYTES:
                return "action arguments exceed the byte bound"
        except Exception:
            return "action arguments are not strict finite JSON"
        return None
    if kind == "transition":
        target = candidate.get("target_state")
        legal = controller.get("legal_transition_targets")
        if type(target) is not str or not isinstance(legal, list) or target not in legal:
            return f"'{target}' is not reachable from '{state}'"
        reason = candidate.get("reason")
        failure = _validate_text_field(reason, MAX_DIRECTIVE_REASON_BYTES)
        if failure is not None:
            return "'reason' failed validation"
        return None
    if kind in ("add_hypothesis", "revise_hypothesis"):
        hypothesis_id = candidate.get("hypothesis_id")
        failure = _validate_text_field(hypothesis_id, MAX_DIRECTIVE_HYPOTHESIS_ID_BYTES)
        if failure is not None or any(
            not ("a" <= char <= "z" or "A" <= char <= "Z" or "0" <= char <= "9" or char in "-_.")
            for char in str(hypothesis_id)
        ):
            return "'hypothesis_id' failed validation"
        failure = _validate_text_field(candidate.get("statement"), MAX_DIRECTIVE_STATEMENT_BYTES)
        if failure is not None:
            return "'statement' failed validation"
        confidence = candidate.get("confidence")
        if type(confidence) is not str or confidence not in HYPOTHESIS_CONFIDENCE_VALUES:
            return "'confidence' must be low, medium, or high"
        evidence_refs = candidate.get("evidence_refs")
        if not isinstance(evidence_refs, list) or len(evidence_refs) > MAX_DIRECTIVE_EVIDENCE_REF_COUNT:
            return "'evidence_refs' must be a JSON array within the bound"
        seen: set[str] = set()
        for reference in evidence_refs:
            failure = _validate_text_field(reference, MAX_DIRECTIVE_EVIDENCE_REF_BYTES)
            if failure is not None or reference in seen:
                return "'evidence_refs' failed validation"
            seen.add(reference)
        if type(candidate.get("requires_runtime_evidence")) is not bool:
            return "'requires_runtime_evidence' must be a boolean"
        return None
    if kind == "set_hypothesis_status":
        hypothesis_id = candidate.get("hypothesis_id")
        failure = _validate_text_field(hypothesis_id, MAX_DIRECTIVE_HYPOTHESIS_ID_BYTES)
        if failure is not None or any(
            not ("a" <= char <= "z" or "A" <= char <= "Z" or "0" <= char <= "9" or char in "-_.")
            for char in str(hypothesis_id)
        ):
            return "'hypothesis_id' failed validation"
        status = candidate.get("status")
        if type(status) is not str or status not in HYPOTHESIS_STATUS_VALUES:
            return "unrecognized hypothesis status"
        return None
    return "unrecognized or missing directive 'kind'"


def accept_structured_directive(candidate: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Accept exactly one already-structured legal directive.  No repair."""
    failure = validate_directive_candidate(candidate, request)
    if failure is not None:
        raise ValueError(f"no valid protocol directive found: {failure}")
    return dict(candidate)


# --- JSON Schema from the incoming contract ---------------------------------

def build_directive_json_schema(request: Mapping[str, Any]) -> dict[str, Any]:
    """Generate a JSON Schema for AGY ``--json-schema`` from the request."""
    kinds = request.get("directive_schema")
    if not isinstance(kinds, (list, tuple)) or not kinds:
        raise ValueError("request carries no directive schema")
    controller = request.get("controller") if isinstance(request.get("controller"), Mapping) else {}
    allowed = controller.get("allowed_actions") if isinstance(controller.get("allowed_actions"), list) else []
    legal = controller.get("legal_transition_targets") if isinstance(controller.get("legal_transition_targets"), list) else []
    contracts = request.get("action_contracts") if isinstance(request.get("action_contracts"), Mapping) else {}
    variants: list[dict[str, Any]] = []
    if "action" in kinds and allowed:
        action_names = [name for name in allowed if isinstance(name, str) and name in contracts]
        if action_names:
            variants.append({
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "name", "arguments"],
                "properties": {
                    "kind": {"const": "action"},
                    "name": {"enum": action_names},
                    "arguments": {"type": "object"},
                },
            })
    if "transition" in kinds and legal:
        variants.append({
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "target_state", "reason"],
            "properties": {
                "kind": {"const": "transition"},
                "target_state": {"enum": [item for item in legal if isinstance(item, str)]},
                "reason": {"type": "string"},
            },
        })
    if "add_hypothesis" in kinds:
        variants.append({
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "hypothesis_id", "statement", "confidence", "evidence_refs", "requires_runtime_evidence"],
            "properties": {
                "kind": {"const": "add_hypothesis"},
                "hypothesis_id": {"type": "string"},
                "statement": {"type": "string"},
                "confidence": {"enum": list(HYPOTHESIS_CONFIDENCE_VALUES)},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "requires_runtime_evidence": {"type": "boolean"},
            },
        })
    if "revise_hypothesis" in kinds:
        variants.append({
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "hypothesis_id", "statement", "confidence", "evidence_refs", "requires_runtime_evidence"],
            "properties": {
                "kind": {"const": "revise_hypothesis"},
                "hypothesis_id": {"type": "string"},
                "statement": {"type": "string"},
                "confidence": {"enum": list(HYPOTHESIS_CONFIDENCE_VALUES)},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "requires_runtime_evidence": {"type": "boolean"},
            },
        })
    if "set_hypothesis_status" in kinds:
        variants.append({
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "hypothesis_id", "status"],
            "properties": {
                "kind": {"const": "set_hypothesis_status"},
                "hypothesis_id": {"type": "string"},
                "status": {"enum": list(HYPOTHESIS_STATUS_VALUES)},
            },
        })
    if not variants:
        raise ValueError("request yields no legal directive JSON schema")
    if len(variants) == 1:
        return variants[0]
    return {"oneOf": variants}


# --- Isolation --------------------------------------------------------------

def isolation_settings() -> dict[str, Any]:
    return json.loads(json.dumps(ISOLATION_SETTINGS))


def _hook_command(executable: Path, script: Path) -> str:
    """Return a shell-safe command using the trusted adapter interpreter."""
    parts = [str(executable), str(script)]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


def prepare_isolation(root: Path) -> dict[str, Any]:
    """Prepare a fresh empty AGY workspace and a credential-free temp home.

    The isolated home receives only non-secret safety settings.  Operator
    OAuth files are never copied.  A zero-inference ``agy models`` probe on
    this machine proved Antigravity authenticates through the native OS
    credential store when HOME/USERPROFILE point at an empty temp home.
    """
    home = root / "home"
    workspace = root / "workspace"
    config_dir = home / ".gemini" / "antigravity-cli"
    gemini_config = home / ".gemini" / "config"
    tmp_dir = root / "tmp"
    for path in (home, workspace, config_dir, gemini_config, tmp_dir):
        path.mkdir(parents=True, exist_ok=True)

    settings_path = config_dir / "settings.json"
    settings_path.write_text(
        json.dumps(isolation_settings(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    workspace_settings = workspace / ".agy" / "antigravity-cli"
    workspace_settings.mkdir(parents=True, exist_ok=True)
    (workspace_settings / "settings.json").write_text(
        settings_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    agent_path = home / DECISION_AGENT_RELATIVE
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    agent_path.write_text(DECISION_AGENT_MARKDOWN, encoding="utf-8")
    trusted_python = Path(sys.executable).resolve()
    if not trusted_python.is_absolute() or not trusted_python.is_file():
        raise RuntimeError("trusted hook Python executable is unusable")
    hook_script_path = root / PRE_TOOL_USE_HOOK_SCRIPT_NAME
    hook_script_path.write_text(PRE_TOOL_USE_HOOK_SCRIPT, encoding="utf-8")
    if os.name != "nt":
        hook_script_path.chmod(0o700)
    hooks_config_path = home / PRE_TOOL_USE_HOOK_CONFIG_RELATIVE
    hooks_config_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_config = {
        PRE_TOOL_USE_HOOK_NAME: {
            "PreToolUse": [{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": _hook_command(trusted_python, hook_script_path),
                    "timeout": 1,
                }],
            }],
        },
    }
    hooks_config_path.write_text(
        json.dumps(hooks_config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    empty_mcp = json.dumps(EMPTY_MCP_CONFIG, indent=2, sort_keys=True) + "\n"
    cli_mcp = home / MCP_CLI_RELATIVE
    cli_mcp.parent.mkdir(parents=True, exist_ok=True)
    cli_mcp.write_text(empty_mcp, encoding="utf-8")
    workspace_mcp = workspace / MCP_WORKSPACE_RELATIVE
    workspace_mcp.parent.mkdir(parents=True, exist_ok=True)
    workspace_mcp.write_text(empty_mcp, encoding="utf-8")
    config_mcp = home / MCP_CONFIG_RELATIVE
    config_mcp.parent.mkdir(parents=True, exist_ok=True)
    config_mcp.write_text(empty_mcp, encoding="utf-8")

    inherited_names = ("PATH", "PATHEXT", "SystemRoot", "WINDIR", "COMSPEC")
    environment = {name: os.environ[name] for name in inherited_names if os.environ.get(name)}
    home_drive, home_path = os.path.splitdrive(str(home))
    environment.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": home_drive or os.environ.get("HOMEDRIVE", ""),
        "HOMEPATH": home_path or str(home),
        "APPDATA": str(home / "appdata"),
        "LOCALAPPDATA": str(home / "localappdata"),
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
    })
    if not environment["HOMEDRIVE"]:
        environment.pop("HOMEDRIVE", None)
    return {
        "environment": environment,
        "home": home,
        "workspace": workspace,
        "settings_path": settings_path,
        "agent_path": agent_path,
        "hook_script_path": hook_script_path,
        "hooks_config_path": hooks_config_path,
        "hook_command": _hook_command(trusted_python, hook_script_path),
        "mcp_cli_path": cli_mcp,
        "mcp_workspace_path": workspace_mcp,
    }


def isolation_contains_secrets(root: Path) -> list[str]:
    """Return relative paths of isolation files that look like credentials."""
    hits: list[str] = []
    forbidden_names = {
        "oauth_creds.json",
        "oauth.json",
        "credentials.json",
        "auth.json",
        "token.json",
        "google_accounts.json",
    }
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.lower() in forbidden_names:
            hits.append(str(path.relative_to(root)))
    return hits


# --- Models catalog ---------------------------------------------------------

_MODEL_ID_RE = re.compile(
    r"(?:^|[\s])(" + "|".join(re.escape(model) for model in sorted(ALLOWED_MODEL_IDENTIFIERS, key=len, reverse=True)) + r")(?:[\sA-Z]|$)",
    re.MULTILINE,
)


def parse_models_output(text: str) -> set[str]:
    """Extract allowlisted Gemini 3.7 Flash IDs from ``agy models`` text."""
    clean = _strip_ansi(text or "")
    found = {match.group(1) for match in _MODEL_ID_RE.finditer(clean)}
    for model_id in ALLOWED_MODEL_IDENTIFIERS:
        if clean.startswith(model_id) or f"\n{model_id}" in clean:
            # concatenated form: gemini-3.7-flash-mediumGemini ...
            found.add(model_id)
    return found


def assert_model_available(available: set[str], model: str) -> None:
    if model not in ALLOWED_MODEL_IDENTIFIERS:
        raise RuntimeError(f"unsupported model '{model}'")
    if model not in available:
        raise RuntimeError(
            f"requested model {model!r} is unavailable in the AGY catalog; no fallback"
        )


def format_print_timeout(timeout_seconds: float) -> str:
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    millis = int(round(timeout_seconds * 1000.0))
    if millis % 1000 == 0:
        return f"{millis // 1000}s"
    return f"{millis}ms"


def build_agy_command(
    native_executable: str,
    model: str,
    message: str,
    schema_path: str,
    timeout_seconds: float,
) -> list[str]:
    """Construct the exact one-shot AGY print argv (no conversation reuse)."""
    if model not in ALLOWED_MODEL_IDENTIFIERS:
        raise ValueError(f"unsupported model '{model}'")
    return [
        native_executable,
        "--print",
        message,
        "--model",
        model,
        "--mode",
        "plan",
        "--sandbox",
        "--disable-slash-commands",
        "--output-format",
        "stream-json",
        "--agent",
        DECISION_AGENT_NAME,
        "--json-schema",
        schema_path,
        "--print-timeout",
        format_print_timeout(timeout_seconds),
    ]


def measure_command_line(command: Sequence[str]) -> int:
    return len(subprocess.list2cmdline(list(command)))


def assert_command_is_fresh_print(command: Sequence[str]) -> None:
    tokens = list(command)
    forbidden = {
        "--continue",
        "-c",
        "--conversation",
        "--dangerously-skip-permissions",
        "--add-dir",
        "accept-edits",
    }
    if any(token in forbidden for token in tokens):
        raise RuntimeError("AGY command includes a forbidden conversation or mutation flag")
    if "--print" not in tokens and "-p" not in tokens and "--prompt" not in tokens:
        raise RuntimeError("AGY command is missing --print")
    if "--mode" not in tokens or tokens[tokens.index("--mode") + 1] != "plan":
        raise RuntimeError("AGY command is missing --mode plan")
    if "--sandbox" not in tokens:
        raise RuntimeError("AGY command is missing --sandbox")
    if "--disable-slash-commands" not in tokens:
        raise RuntimeError("AGY command is missing --disable-slash-commands")
    if "--output-format" not in tokens or tokens[tokens.index("--output-format") + 1] != "stream-json":
        raise RuntimeError("AGY command is missing --output-format stream-json")
    if "--json-schema" not in tokens:
        raise RuntimeError("AGY command is missing --json-schema")
    if "--agent" not in tokens:
        raise RuntimeError("AGY command is missing --agent")
    if tokens[tokens.index("--agent") + 1] != DECISION_AGENT_NAME:
        raise RuntimeError("AGY command does not pin the decision-only agent")


# --- Stream parsing ---------------------------------------------------------

def _advertised_init_tools(payload: Mapping[str, Any], event: Mapping[str, Any]) -> list[str]:
    if "tools" in payload:
        raw = payload["tools"]
    elif "tools" in event:
        raw = event["tools"]
    else:
        return []
    if not isinstance(raw, list):
        raise ValueError("AGY init tools field is not a list")
    if any(type(item) is not str for item in raw):
        raise ValueError("AGY init tools entries are not strings")
    return list(raw)


def _init_capability_reason(event: Mapping[str, Any]) -> Optional[str]:
    """Reject unapproved init capabilities and all advertised subagents."""
    payload = event.get("init")
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        return "AGY init payload is not an object"
    try:
        tools = _advertised_init_tools(payload, event)
    except ValueError as exc:
        return str(exc)
    unapproved = sorted(
        set(tools) - DOCUMENTED_INTRINSIC_INIT_CAPABILITIES
    )
    if unapproved:
        names = ", ".join(unapproved)
        return f"AGY init advertises unapproved capability names ({names})"
    for key in ("subagents", "available_subagents", "subagent_info"):
        value = payload.get(key)
        if value:
            return "AGY init advertises subagent capability"
    if payload.get("subagent") is True:
        return "AGY init advertises subagent capability"
    return None


def _tool_or_subagent_reason(event: Mapping[str, Any]) -> Optional[str]:
    event_type = event.get("event")
    if event_type in {"tool", "tool_call", "tool_result", "subagent", "mcp", "skill"}:
        return f"forbidden AGY event {event_type!r}"
    payload = event.get("step_update")
    if not isinstance(payload, Mapping):
        payload = event
    step_type = payload.get("step_type")
    if isinstance(step_type, str) and step_type in FORBIDDEN_STEP_TYPES:
        return f"AGY attempted a {step_type} operation"
    if "tool_name" in payload or "tool_info" in payload:
        return "AGY attempted a tool operation"
    if "subagent_info" in payload or "subagents" in payload:
        return "AGY attempted a subagent operation"
    for key in ("mcp", "skill", "command", "web_search", "webfetch"):
        if key in payload and payload.get(key):
            return f"AGY attempted a {key} operation"
    return None


def parse_agy_stream(raw_stdout: str) -> Tuple[dict[str, Any], Optional[dict[str, Any]]]:
    """Parse AGY stream-json NDJSON and return (structured_output, usage)."""
    if not isinstance(raw_stdout, str) or not raw_stdout.strip():
        raise ValueError("AGY stream is empty")
    result_event: Optional[dict[str, Any]] = None
    last_event_was_result = False
    for line_number, line in enumerate(_strip_ansi(raw_stdout).splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"AGY stream line {line_number} is not valid NDJSON") from exc
        if not isinstance(event, dict):
            raise ValueError(f"AGY stream line {line_number} is not a JSON object")
        event_type = event.get("event")
        if type(event_type) is not str or event_type not in SAFE_STREAM_EVENTS:
            raise ValueError(f"AGY stream contains an unknown unsafe event form: {event_type!r}")
        tool_reason = _tool_or_subagent_reason(event)
        if tool_reason is not None:
            raise ValueError(tool_reason)
        if event_type == "step_update":
            payload = event.get("step_update")
            if not isinstance(payload, Mapping):
                raise ValueError("AGY step_update event is missing its payload")
            step_type = payload.get("step_type")
            if type(step_type) is not str or step_type not in SAFE_STEP_TYPES:
                raise ValueError(f"AGY stream contains an unknown unsafe step type: {step_type!r}")
            last_event_was_result = False
            continue
        if event_type == "init":
            capability_reason = _init_capability_reason(event)
            if capability_reason is not None:
                raise ValueError(capability_reason)
            last_event_was_result = False
            continue
        if event_type == "result":
            if result_event is not None:
                raise ValueError("AGY stream contains a duplicate terminal result")
            payload = event.get("result")
            if not isinstance(payload, Mapping):
                raise ValueError("AGY result event is missing its payload")
            result_event = dict(payload)
            last_event_was_result = True
    if result_event is None:
        raise ValueError("AGY stream is missing a terminal result")
    if not last_event_was_result:
        raise ValueError("AGY terminal result was not the final stream event")
    status = result_event.get("status")
    if status != "SUCCESS":
        raise ValueError(f"AGY terminal status is {status!r}, not SUCCESS")
    structured = result_event.get("structured_output")
    if not isinstance(structured, Mapping):
        raise ValueError("AGY terminal result is missing structured_output")
    usage = _map_usage(result_event.get("usage"))
    return dict(structured), usage


def _map_usage(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return None
    usage: dict[str, Any] = {}
    input_tokens = raw.get("input_tokens")
    output_tokens = raw.get("output_tokens")
    if type(input_tokens) is int and not isinstance(input_tokens, bool):
        usage["prompt_tokens"] = input_tokens
    if type(output_tokens) is int and not isinstance(output_tokens, bool):
        usage["completion_tokens"] = output_tokens
    cost = raw.get("cost")
    if type(cost) in (int, float) and not isinstance(cost, bool):
        usage["cost"] = float(cost)
    return usage or None


# --- Subprocess execution ---------------------------------------------------

def _run_bounded_command(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    cwd: Path,
    timeout_seconds: float,
    max_output_bytes: int,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        shell=False,
        env=dict(environment),
    )
    if len((completed.stdout or "").encode("utf-8", errors="replace")) > max_output_bytes:
        raise ValueError("AGY preflight output exceeded the bound")
    return completed


def parse_agents_output(text: str) -> set[str]:
    """Parse ``agy agents`` / ``agy agent`` stdout into agent names."""
    names: set[str] = set()
    for line in _strip_ansi(text or "").splitlines():
        name = line.strip()
        if name and not name.lower().startswith("fetching") and " " not in name:
            names.add(name)
    return names


def assert_decision_agent_available(available: set[str]) -> None:
    if DECISION_AGENT_NAME not in available:
        raise RuntimeError(
            f"decision-only agent {DECISION_AGENT_NAME!r} is not available; "
            "no default-agent fallback"
        )


def list_available_agents(
    native_executable: str,
    environment: Mapping[str, str],
    cwd: Path,
) -> set[str]:
    completed = _run_bounded_command(
        [native_executable, "agents"],
        environment=environment,
        cwd=cwd,
        timeout_seconds=_PREFLIGHT_TIMEOUT_SECONDS,
        max_output_bytes=MAX_RAW_RESPONSE_BYTES,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"AGY agents preflight failed with exit code {completed.returncode}: "
            f"{redact((completed.stderr or '')[:300].strip())}"
        )
    return parse_agents_output(completed.stdout or "")


def list_available_models(
    native_executable: str,
    environment: Mapping[str, str],
    cwd: Path,
) -> set[str]:
    completed = _run_bounded_command(
        [native_executable, "models"],
        environment=environment,
        cwd=cwd,
        timeout_seconds=_PREFLIGHT_TIMEOUT_SECONDS,
        max_output_bytes=MAX_RAW_RESPONSE_BYTES,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"AGY models preflight failed with exit code {completed.returncode}: "
            f"{redact((completed.stderr or '')[:300].strip())}"
        )
    available = parse_models_output(completed.stdout or "")
    if not available:
        raise RuntimeError("AGY models preflight listed no supported Gemini 3.7 Flash models")
    return available


def execute_print(
    identity: Mapping[str, Any],
    model: str,
    message: str,
    request: Mapping[str, Any],
    timeout_seconds: float,
    *,
    max_response_bytes: int = MAX_RAW_RESPONSE_BYTES,
    work_root: Optional[str] = None,
) -> Tuple[dict[str, Any], Optional[dict[str, Any]]]:
    native_executable = identity["native_executable"]
    if not isinstance(native_executable, str) or not native_executable:
        raise RuntimeError("verified AGY executable identity is unusable")
    if work_root is not None:
        root_dir = Path(work_root)
        if not root_dir.is_absolute() or not root_dir.is_dir():
            raise RuntimeError("work root must be an absolute existing directory")
        work_dir = Path(tempfile.mkdtemp(prefix="agy-gemini-run-", dir=str(root_dir)))
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="agy-gemini-run-"))
    group_registered = False
    process: Optional[subprocess.Popen] = None
    try:
        isolation = prepare_isolation(work_dir)
        secret_hits = isolation_contains_secrets(work_dir)
        if secret_hits:
            raise RuntimeError("isolation home contains credential-named files")
        schema = build_directive_json_schema(request)
        schema_path = isolation["workspace"] / "directive-schema.json"
        schema_path.write_text(
            json.dumps(schema, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        agents = list_available_agents(
            native_executable, isolation["environment"], isolation["workspace"]
        )
        assert_decision_agent_available(agents)
        available = list_available_models(
            native_executable, isolation["environment"], isolation["workspace"]
        )
        assert_model_available(available, model)
        command = build_agy_command(
            native_executable,
            model,
            message,
            str(schema_path),
            timeout_seconds,
        )
        assert_command_is_fresh_print(command)
        command_line_chars = measure_command_line(command)
        if command_line_chars > MAX_NATIVE_COMMAND_LINE_CHARS:
            raise ValueError(
                f"command line exceeds bound ({command_line_chars} > {MAX_NATIVE_COMMAND_LINE_CHARS})"
            )
        stdout_capture = _BoundedCapture(max_response_bytes)
        stderr_capture = _BoundedCapture(max_response_bytes)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=isolation["environment"],
                cwd=str(isolation["workspace"]),
                start_new_session=sys.platform != "win32",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
        except Exception as exc:
            raise RuntimeError(f"failed to launch AGY CLI: {exc}") from None
        if sys.platform != "win32":
            register_inflight_child_group(process.pid)
            group_registered = True
        threads = [
            threading.Thread(target=_read_pipe, args=(process.stdout, stdout_capture), daemon=True),
            threading.Thread(target=_read_pipe, args=(process.stderr, stderr_capture), daemon=True),
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                process.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    _terminate_command_tree(process)
                    for thread in threads:
                        thread.join(timeout=1.0)
                    raise TimeoutError(f"AGY print timed out after {timeout_seconds:.1f}s")
        for thread in threads:
            thread.join(timeout=1.0)
        if stdout_capture.truncated:
            raise ValueError(f"AGY output exceeded maximum response bound ({max_response_bytes} bytes)")
        if process.returncode != 0:
            err_text = redact(stderr_capture.text()[:500].strip())
            raise RuntimeError(f"AGY CLI exited with code {process.returncode}: {err_text}")
        structured, usage = parse_agy_stream(stdout_capture.text())
        directive = accept_structured_directive(structured, request)
        return directive, usage
    finally:
        if group_registered and process is not None:
            unregister_inflight_child_group(process.pid)
        shutil.rmtree(work_dir, ignore_errors=True)


# --- Main entry point -------------------------------------------------------

def run_adapter(
    stdin_stream: Any = sys.stdin,
    stdout_stream: Any = sys.stdout,
    stderr_stream: Any = sys.stderr,
    argv: Optional[Sequence[str]] = None,
) -> int:
    parser = argparse.ArgumentParser(description="AGY Gemini 3.7 Flash command adapter")
    parser.add_argument("--model", default=DEFAULT_MODEL_ID, help="Exact Gemini 3.7 Flash model id")
    parser.add_argument("--executable", required=True, help="Absolute verified AGY executable path")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Per-print timeout in seconds")
    parser.add_argument("--max-response-bytes", type=int, default=MAX_RAW_RESPONSE_BYTES, help="Max structured output bytes")
    parser.add_argument("--work-root", default=None, help="Absolute existing directory for disposable work dirs")
    parser.add_argument("--max-logical-model-calls", type=int, default=DEFAULT_MAX_LOGICAL_MODEL_CALLS)
    parser.add_argument("--expected-version", default=EXPECTED_AGY_VERSION, help="Required AGY --version token")
    parser.add_argument("--preflight", action="store_true", help="Zero-inference identity + models preflight")
    args = parser.parse_args(argv)

    if args.model not in ALLOWED_MODEL_IDENTIFIERS:
        stderr_stream.write(
            f"Error: unsupported model '{redact(args.model)}'. "
            "Only gemini-3.7-flash-low|medium|high are allowed. No fallback.\n"
        )
        stderr_stream.flush()
        return 1

    if args.preflight:
        try:
            identity = executable_identity.resolve_verified_agy_executable(
                args.executable,
                expected_version=args.expected_version,
            )
            root = Path(tempfile.mkdtemp(prefix="agy-gemini-preflight-"))
            try:
                isolation = prepare_isolation(root)
                agents = list_available_agents(
                    identity["native_executable"],
                    isolation["environment"],
                    isolation["workspace"],
                )
                assert_decision_agent_available(agents)
                available = list_available_models(
                    identity["native_executable"],
                    isolation["environment"],
                    isolation["workspace"],
                )
                assert_model_available(available, args.model)
            finally:
                shutil.rmtree(root, ignore_errors=True)
            assertions = {
                "preflight": "passed",
                "provider_inference_started": False,
                "identity": identity,
                "available_models": sorted(available),
                "available_agents": sorted(agents),
                "requested_model": args.model,
                "decision_agent": DECISION_AGENT_NAME,
            }
            stdout_stream.write(json.dumps(assertions, ensure_ascii=False, sort_keys=True) + "\n")
            stdout_stream.flush()
            return 0
        except Exception as exc:
            failure = {
                "preflight": "blocked",
                "provider_inference_started": False,
                "error": f"{type(exc).__name__}: {redact(str(exc))}",
            }
            stdout_stream.write(json.dumps(failure, ensure_ascii=False, sort_keys=True) + "\n")
            stdout_stream.flush()
            return 1

    try:
        line = stdin_stream.readline() if hasattr(stdin_stream, "readline") else stdin_stream.read()
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if not line or not line.strip():
            stderr_stream.write("Error: empty request on stdin.\n")
            stderr_stream.flush()
            return 1
        request = json.loads(line)
        if not isinstance(request, dict):
            stderr_stream.write("Error: request on stdin must be a JSON object.\n")
            stderr_stream.flush()
            return 1
    except Exception as exc:
        stderr_stream.write(f"Error: failed to parse JSON request from stdin: {redact(str(exc))}\n")
        stderr_stream.flush()
        return 1

    envelope_error = validate_logical_call_index(request, args.max_logical_model_calls)
    if envelope_error is not None:
        stderr_stream.write(f"Error: {envelope_error}.\n")
        stderr_stream.flush()
        return 1

    try:
        message = build_protocol_message(request)
    except Exception as exc:
        stderr_stream.write(f"Error: failed to build protocol message: {redact(str(exc))}\n")
        stderr_stream.flush()
        return 1

    try:
        identity = executable_identity.resolve_verified_agy_executable(
            args.executable,
            expected_version=args.expected_version,
        )
    except Exception as exc:
        stderr_stream.write(f"Error: AGY executable identity resolution failed: {redact(str(exc))}\n")
        stderr_stream.flush()
        return 1

    try:
        directive, usage = execute_print(
            identity=identity,
            model=args.model,
            message=message,
            request=request,
            timeout_seconds=args.timeout,
            max_response_bytes=args.max_response_bytes,
            work_root=args.work_root,
        )
    except Exception as exc:
        stderr_stream.write(f"Error: AGY inference failed: {redact(str(exc))}\n")
        stderr_stream.flush()
        return 1

    response_payload: dict[str, Any] = {"directive": directive}
    if usage:
        response_payload["usage"] = usage
    try:
        stdout_stream.write(json.dumps(response_payload, ensure_ascii=False) + "\n")
        stdout_stream.flush()
    except Exception as exc:
        stderr_stream.write(f"Error: failed to write response: {redact(str(exc))}\n")
        stderr_stream.flush()
        return 1
    return 0


def main() -> None:
    install_child_group_signal_handlers()
    sys.exit(run_adapter())


if __name__ == "__main__":
    main()
