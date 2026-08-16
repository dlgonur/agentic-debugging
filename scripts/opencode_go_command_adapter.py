"""OpenCode Go DeepSeek V4 Pro command adapter for Local Application V1.

Adapts the accepted protocol-1.3 JSON-lines command-model contract of Local
Application V1 to ONE bounded OpenCode Go DeepSeek V4 Pro inference.

Responsibilities:
1. Read exactly one protocol-1.3 JSON request object from stdin.
2. Validate the request context (directive schema, action contracts, controller state).
3. Enforce the 25-logical-call micro-run envelope (``protocol.logical_model_call_index``).
4. Enforce the exact model identity: ``deepseek-v4-pro`` / ``opencode-go/deepseek-v4-pro``.
5. Construct a compact, instruction-wrapped prompt embedding the canonical public request
   (bounded by the Local-Application-specific ``MAX_PUBLIC_REQUEST_BYTES`` ceiling).
6. Resolve and prove the explicit OpenCode executable identity (absolute verified launcher
   on Windows, absolute verified executable elsewhere; never a bare PATH lookup).
7. Execute ONE bounded non-interactive ``opencode run`` inference in a fresh isolated
   directory with a fail-closed wildcard-permission OpenCode configuration.
8. Authenticate through the accepted OpenCode Go CLI contract WITHOUT any adapter-owned
   plaintext credential artifact: the operator-owned auth store is read in place and its
   bytes travel only inside the child process environment (``OPENCODE_AUTH_CONTENT``),
   never to disk, argv, history, or evidence.
9. Extract and strictly validate the model directive against the request schema and state.
10. Emit the exact transport-level response envelope (``{"directive": ..., "usage": ...}``)
    on stdout.
11. Enforce hard bounds: max 64 KiB response, 20s timeout, zero retries/fallbacks, clean up.
12. POSIX external-cancellation containment: SIGTERM/SIGINT handlers terminate the
    in-flight detached OpenCode child group before the adapter exits, so a Local
    Application cancel cannot orphan the OpenCode tree.

Zero provider retries: the adapter performs exactly ONE OpenCode process per
transport request.  The accepted ``LiveModelAdapter`` may still perform its
existing bounded directive-feedback/correction attempts, so total provider
process attempts across a session may exceed logical model calls.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
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
    import opencode_executable_identity as executable_identity
except ImportError:  # pragma: no cover - defensive import path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import opencode_executable_identity as executable_identity


# --- Configuration & Bounds ---

DEFAULT_MODEL_ID = "deepseek-v4-pro"
OPENCODE_MODEL_REF = "opencode-go/deepseek-v4-pro"
ALLOWED_MODEL_IDENTIFIERS = frozenset({"deepseek-v4-pro", "opencode-go/deepseek-v4-pro"})

DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_RAW_RESPONSE_BYTES = 64 * 1024  # 64 KiB
#: The Local-Application-specific bounded canonical public request ceiling.
#: Distinct from the historical QuixBugs campaign 20,000-byte
#: ``max_public_evidence_bytes`` budget; this ceiling admits the complete
#: measured ``curated-none-handling-001`` pdb-on-uncertainty reference
#: trajectory (21 requests, max 23,824 canonical bytes) while the exact
#: constructed prompt plus the Windows command line stays below the
#: separate 30,000-character native command-line guard.
MAX_PUBLIC_REQUEST_BYTES = 25_000
MAX_NATIVE_COMMAND_LINE_CHARS = 30_000  # Conservative Windows limit (< 32,767)

#: The planned micro-run hard bound: maximum logical model calls = 25.
DEFAULT_MAX_LOGICAL_MODEL_CALLS = 25
#: Bounded auth-store size: larger stores fail closed.
MAX_AUTH_STORE_BYTES = 64 * 1024

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

_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:bearer\s+[a-zA-Z0-9._~+/-]+=*|sk-[a-zA-Z0-9_-]{10,}|(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|token)\s*[:=]\s*\S+)",
)

#: Fail-closed OpenCode permission isolation.  The wildcard ``*`` deny is the
#: authoritative catch-all (OpenCode's actual permission vocabulary includes
#: capabilities beyond the explicitly named entries, e.g. webfetch, websearch,
#: skill, lsp, question, external_directory); the explicit named denials are
#: retained for documentary clarity and defense in depth.
_ISOLATION_PERMISSION_DENIALS = (
    ("*", "deny"),
    ("read", "deny"),
    ("write", "deny"),
    ("edit", "deny"),
    ("bash", "deny"),
    ("glob", "deny"),
    ("grep", "deny"),
    ("list", "deny"),
    ("terminal", "deny"),
    ("browser", "deny"),
    ("task", "deny"),
    ("webfetch", "deny"),
    ("websearch", "deny"),
    ("skill", "deny"),
    ("lsp", "deny"),
    ("question", "deny"),
    ("external_directory", "deny"),
)

#: The permission names the effective-configuration verification additionally
#: requires to be explicitly denied (beyond the authoritative wildcard).
_REQUIRED_EFFECTIVE_DENIALS = (
    "read",
    "write",
    "edit",
    "bash",
    "task",
    "webfetch",
    "websearch",
    "external_directory",
)


# --- Redaction & Diagnostics ---

def redact(text: str) -> str:
    """Redact credential-shaped tokens from strings before output."""
    if not isinstance(text, str) or not text:
        return ""
    return _SECRET_PATTERN.sub("<redacted_secret>", text)


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from terminal output."""
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


# --- Bounded Pipe Capture ---

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
    else:
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


# --- POSIX external-cancellation containment --------------------------------
#
# Local Application spawns the adapter into its own request-owned process
# group and can terminate that group externally (cancel/escalation) with
# SIGTERM then SIGKILL.  The adapter spawns OpenCode with
# ``start_new_session`` so the OpenCode tree lives in its OWN detached
# process group; a plain group-kill of the adapter cannot reach it.  The
# adapter therefore registers every in-flight detached child group and
# installs SIGTERM/SIGINT handlers that terminate all registered child
# groups before the adapter's default termination runs.  The internal
# per-request timeout path keeps its own group ladder unchanged.

# RLock: SIGTERM/SIGINT handlers run on the main thread and must be able to
# snapshot the registry even if they interrupt register/unregister while
# that same thread already holds the lock.  A non-reentrant Lock would
# deadlock the handler and leave the detached OpenCode group alive.
_INFLIGHT_CHILD_GROUPS_LOCK = threading.RLock()
_INFLIGHT_CHILD_GROUPS: list[int] = []
#: Bounded grace between the handler's SIGTERM and SIGKILL of a child group.
_CHILD_GROUP_GRACE_SECONDS = 0.2


def register_inflight_child_group(group_id: int) -> None:
    """Track one detached in-flight OpenCode child process group (POSIX)."""
    if sys.platform == "win32":
        return
    if type(group_id) is not int or isinstance(group_id, bool) or group_id <= 0:
        return
    with _INFLIGHT_CHILD_GROUPS_LOCK:
        _INFLIGHT_CHILD_GROUPS.append(group_id)


def unregister_inflight_child_group(group_id: int) -> None:
    """Stop tracking a child group whose request has fully terminated."""
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
    """Best-effort SIGTERM+SIGKILL of every registered detached child group."""
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
    # Terminate every in-flight detached child group, then restore the
    # default disposition and re-raise the signal so the adapter still
    # terminates the normal way.  Never returns to the interrupted frame.
    try:
        _terminate_inflight_child_groups()
    finally:
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except Exception:
            os._exit(128 + int(signum))


def install_child_group_signal_handlers() -> None:
    """Install the external-cancellation containment handlers (POSIX only).

    Windows needs nothing here: ``taskkill /PID <adapter> /T /F`` from the
    outer transport (or the accepted job object) already terminates the whole
    descendant tree.
    """
    if sys.platform == "win32":
        return
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _external_signal_handler)
        except (ValueError, OSError):
            continue


# --- Profile & Auth Helpers ---

def _windows_profile_path() -> Path:
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
    except Exception:
        pass
    if os.name == "nt":
        return _windows_profile_path()
    raise RuntimeError("required user profile could not be determined")


def _auth_state_path() -> Path:
    profile = _profile_path()
    return profile / ".local" / "share" / "opencode" / "auth.json"


def resolve_auth_store(auth_file: Optional[str]) -> str:
    """Read the operator-owned OpenCode auth store WITHOUT creating a copy.

    The returned value is the canonical JSON text of the store object; the
    adapter passes it only inside the OpenCode child environment
    (``OPENCODE_AUTH_CONTENT``), so the credential bytes exist only in
    process memory and never in any adapter-owned durable location.  A
    missing, unreadable, oversized, or non-object store fails closed.

    The direct bounded ``open`` is the single filesystem authority: at most
    ``MAX_AUTH_STORE_BYTES + 1`` bytes are ever read into memory.  There is
    no ``is_file()``/``stat`` preflight, so a stale small ``stat`` cannot
    permit an oversized read.  Errors never include raw credential bytes
    or JSON values.
    """
    if auth_file is not None:
        if type(auth_file) is not str or not auth_file:
            raise RuntimeError("auth file path is missing")
        path = Path(auth_file)
        if not path.is_absolute():
            raise RuntimeError("auth file path must be absolute")
    else:
        path = _auth_state_path()
    try:
        with path.open("rb") as handle:
            raw_bytes = handle.read(MAX_AUTH_STORE_BYTES + 1)
    except OSError:
        raise RuntimeError("required OpenCode authentication state is unavailable") from None
    if len(raw_bytes) > MAX_AUTH_STORE_BYTES:
        raise RuntimeError(
            f"OpenCode authentication state exceeds the bound ({MAX_AUTH_STORE_BYTES} bytes)"
        )
    try:
        decoded = raw_bytes.decode("utf-8")
    except UnicodeError:
        raise RuntimeError("OpenCode authentication state is not valid UTF-8") from None
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        raise RuntimeError("OpenCode authentication state is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenCode authentication state is not a JSON object")
    try:
        return json.dumps(
            parsed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise RuntimeError("OpenCode authentication state is not strict finite JSON") from None


# --- Prompt Construction ---

SYSTEM_PROMPT = (
    "You are the debugging decision model for Local Application V1.\n"
    "Your sole task is to inspect the protocol-1.3 request below and return exactly ONE legal JSON protocol directive.\n"
    "Rules:\n"
    "1. Return exactly one JSON object representing the directive.\n"
    "2. Do NOT output any markdown, code blocks (```json), or explanatory text.\n"
    "3. Do NOT make tool calls or attempt file operations.\n"
    "4. Obey the allowed_actions, action_contracts, directive_schema, and legal_transition_targets in the request.\n"
    "5. Preserve exact argument names as declared in action_contracts.\n"
    "6. When proposing a patch, use the 'apply_patch' action with argument 'patch' containing the unified diff."
)


def canonical_public_request(request: Mapping[str, Any]) -> str:
    """Serialize the request to compact, deterministic canonical JSON."""
    return json.dumps(
        request,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def build_protocol_message(request: Mapping[str, Any]) -> str:
    """Build the single user message carrying the instructions and public request."""
    if not isinstance(request, Mapping):
        raise ValueError("protocol request must be a JSON object")
    canonical = canonical_public_request(request)
    byte_count = len(canonical.encode("utf-8"))
    if byte_count > MAX_PUBLIC_REQUEST_BYTES:
        raise ValueError(
            f"canonical public request exceeds the Local Application ceiling ({byte_count} > {MAX_PUBLIC_REQUEST_BYTES} bytes)"
        )
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"{PUBLIC_REQUEST_START}\n"
        f"{canonical}\n"
        f"{PUBLIC_REQUEST_END}"
    )


# --- Micro-run envelope (25-logical-call guard) ---

def validate_logical_call_index(request: Mapping[str, Any], max_logical_calls: int) -> Optional[str]:
    """Validate the incoming protocol metadata against the micro-run envelope.

    ``protocol.logical_model_call_index`` is 1-based (the first logical model
    call is index 1).  Missing, malformed, or out-of-envelope metadata fails
    closed; the guard does NOT limit the accepted transport-attempt index,
    so the accepted LiveModelAdapter directive-feedback corrections are
    unaffected.
    """
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


# --- Strict Directive Validation ---

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


def _json_objects(text: str) -> list[dict[str, Any]]:
    """Enumerate top-level JSON objects from text."""
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    clean = _strip_ansi(text)
    offset = 0
    while offset < len(clean):
        if clean[offset] != "{":
            offset += 1
            continue
        try:
            candidate, end = decoder.raw_decode(clean[offset:])
        except json.JSONDecodeError:
            offset += 1
            continue
        if isinstance(candidate, dict):
            objects.append(candidate)
            offset += end
        else:
            offset += 1
    return objects


def extract_directive(text: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly extract exactly ONE valid protocol directive from raw model text."""
    candidates = _json_objects(text)
    if not candidates:
        raise ValueError("model output did not contain any JSON object")
    valid = [c for c in candidates if validate_directive_candidate(c, request) is None]
    if not valid:
        if len(candidates) == 1:
            reason = validate_directive_candidate(candidates[0], request) or "invalid directive candidate"
        else:
            reason = "none of the JSON candidates in output matched a valid directive"
        raise ValueError(f"no valid protocol directive found: {reason}")
    if len(valid) > 1:
        raise ValueError("ambiguous output: multiple valid protocol directives found")
    return valid[0]


# --- Isolation Configuration & Effective-Config Verification ---

def isolation_config() -> dict[str, Any]:
    """The fail-closed OpenCode isolation configuration.

    The wildcard permission deny is authoritative; MCP is disabled, plugins
    and instructions are empty, sharing is disabled, autoupdate is disabled,
    and the enabled provider set is exactly ``opencode-go``.  Project config
    is disabled through the ``OPENCODE_DISABLE_PROJECT_CONFIG`` environment
    (not a config file key).
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": dict(_ISOLATION_PERMISSION_DENIALS),
        "mcp": {"*": {"enabled": False}},
        "plugin": [],
        "instructions": [],
        "share": "disabled",
        "enabled_providers": ["opencode-go"],
        "autoupdate": False,
    }


def validate_effective_config(config: Any) -> dict[str, Any]:
    """Strictly verify an observed effective OpenCode configuration (no model).

    Requires: the wildcard ``*`` permission deny (the authoritative catch-all
    so unknown/future permission names remain denied), the required named
    denials, MCP disabled, plugins empty, instructions empty, sharing
    disabled, enabled providers exactly ``[opencode-go]``, and autoupdate
    disabled.  Any drift fails closed with a bounded error.
    """
    if not isinstance(config, dict):
        raise RuntimeError("OpenCode effective configuration was not an object")
    permission = config.get("permission")
    if not isinstance(permission, dict):
        raise RuntimeError("OpenCode effective configuration has no permission object")
    if permission.get("*") != "deny":
        raise RuntimeError("OpenCode effective configuration does not deny permissions by default (wildcard catch-all missing)")
    if any(permission.get(name) != "deny" for name in _REQUIRED_EFFECTIVE_DENIALS):
        raise RuntimeError("OpenCode effective configuration does not deny a required permission")
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
    if config.get("enabled_providers") != ["opencode-go"]:
        raise RuntimeError("OpenCode enabled provider set is not exactly [opencode-go]")
    if config.get("autoupdate") is not False:
        raise RuntimeError("OpenCode autoupdate is not disabled")
    return {
        "permission_wildcard_denied": True,
        "required_permissions_denied": list(_REQUIRED_EFFECTIVE_DENIALS),
        "mcp_servers_disabled": True,
        "plugins_empty": True,
        "instructions_empty": True,
        "sharing_disabled": True,
        "enabled_providers": ["opencode-go"],
        "autoupdate_disabled": True,
    }


def observe_effective_config(environment: Mapping[str, str], cwd: Path, native_executable: str) -> dict[str, Any]:
    """Run the zero-inference ``debug config --pure`` observation and verify it.

    Never invokes ``opencode run`` and never contacts a model endpoint.  The
    observed effective configuration is verified against the fail-closed
    isolation contract (:func:`validate_effective_config`).
    """
    completed = subprocess.run(
        [native_executable, "debug", "config", "--pure"],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        shell=False,
        env=dict(environment),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"OpenCode effective config observation failed with exit code {completed.returncode}"
        )
    try:
        config = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("OpenCode effective config observation was not valid JSON") from None
    return validate_effective_config(config)


# --- Subprocess Isolation & Execution ---

def prepare_isolation(root: Path, *, auth_content: Optional[str] = None) -> dict[str, Any]:
    """Prepare a clean, temporary isolated environment for OpenCode execution.

    The credential store is referenced in memory only: when ``auth_content``
    is supplied it is injected exclusively through the ``OPENCODE_AUTH_CONTENT``
    environment value of the spawned child.  No adapter-owned plaintext
    credential artifact is ever created, so no finally-block dependency
    protects it and a forced external termination cannot leave a credential
    copy behind.
    """
    config_home = root / "config-home"
    data_home = root / "data-home"
    state_home = root / "state-home"
    cache_home = root / "cache-home"
    for p in (config_home, data_home, state_home, cache_home):
        p.mkdir(parents=True, exist_ok=True)

    config_path = root / "opencode.json"
    config = isolation_config()
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    agents_path = root / "AGENTS.md"
    agents_path.write_text("No external tool calls or file operations permitted.\n", encoding="utf-8")

    inherited_names = ("PATH", "PATHEXT", "SystemRoot", "WINDIR", "COMSPEC")
    environment = {name: os.environ[name] for name in inherited_names if os.environ.get(name)}
    home_drive, home_path = os.path.splitdrive(str(root))
    environment.update({
        "HOME": str(root),
        "USERPROFILE": str(root),
        "HOMEDRIVE": home_drive,
        "HOMEPATH": home_path,
        "APPDATA": str(root / "appdata"),
        "LOCALAPPDATA": str(root / "localappdata"),
        "TEMP": str(root / "tmp"),
        "TMP": str(root / "tmp"),
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_CONFIG_DIR": str(config_home),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
        "XDG_CACHE_HOME": str(cache_home),
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
    })
    if auth_content is not None:
        environment["OPENCODE_AUTH_CONTENT"] = auth_content
    Path(environment["TEMP"]).mkdir(parents=True, exist_ok=True)
    return {
        "environment": environment,
        "config_path": config_path,
    }


def parse_opencode_output(raw_stdout: str) -> Tuple[str, Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Parse JSON lines from OpenCode output into text parts, usage, and telemetry."""
    text_parts: list[str] = []
    usage: Optional[dict[str, Any]] = None
    telemetry: Optional[dict[str, Any]] = None

    for line in _strip_ansi(raw_stdout).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")
        part = event.get("part")
        if isinstance(part, dict):
            if isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            if isinstance(part.get("tokens"), dict) or "cost" in part:
                usage = usage or {}
                if isinstance(part.get("tokens"), dict):
                    tok = part["tokens"]
                    if "input" in tok and isinstance(tok["input"], (int, float)):
                        usage["prompt_tokens"] = int(tok["input"])
                    if "output" in tok and isinstance(tok["output"], (int, float)):
                        usage["completion_tokens"] = int(tok["output"])
                if "cost" in part and isinstance(part["cost"], (int, float)):
                    usage["cost"] = float(part["cost"])
            for key in ("observed_model", "observed_billing_route", "observed_model_substitution"):
                if key in part:
                    telemetry = telemetry or {}
                    telemetry[key] = part[key]

        if isinstance(event.get("text"), str):
            text_parts.append(event["text"])

    combined_text = "\n".join(text_parts) if text_parts else raw_stdout
    return combined_text, usage, telemetry


def execute_inference(
    identity: Mapping[str, Any],
    model: str,
    message: str,
    timeout_seconds: float,
    *,
    variant: Optional[str] = None,
    max_response_bytes: int = MAX_RAW_RESPONSE_BYTES,
    auth_content: Optional[str] = None,
    work_root: Optional[str] = None,
) -> Tuple[str, Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Execute ONE bounded OpenCode Go DeepSeek V4 Pro inference.

    ``identity`` is the already-verified explicit OpenCode executable
    identity (:func:`opencode_executable_identity.resolve_verified_opencode_executable`).
    On POSIX the OpenCode child runs in its own detached process group and
    that group is registered for the external-cancellation signal handlers;
    on Windows ``taskkill /T /F`` owns the tree.
    """
    native_executable = identity["native_executable"]
    if not isinstance(native_executable, str) or not native_executable:
        raise RuntimeError("verified OpenCode executable identity is unusable")
    if work_root is not None:
        root_dir = Path(work_root)
        if not root_dir.is_absolute() or not root_dir.is_dir():
            raise RuntimeError("work root must be an absolute existing directory")
        work_dir = Path(tempfile.mkdtemp(prefix="opencode-go-run-", dir=str(root_dir)))
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="opencode-go-run-"))
    group_registered = False
    try:
        isolation = prepare_isolation(work_dir, auth_content=auth_content)
        command = [
            native_executable, "run", message, "--pure", "--format", "json",
            "--model", model, "--dir", str(work_dir),
        ]
        if variant:
            command.extend(["--variant", variant])

        command_line = subprocess.list2cmdline(command)
        if len(command_line) > MAX_NATIVE_COMMAND_LINE_CHARS:
            raise ValueError(f"command line exceeds bound ({len(command_line)} > {MAX_NATIVE_COMMAND_LINE_CHARS})")

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
                cwd=str(work_dir),
                start_new_session=sys.platform != "win32",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
        except Exception as exc:
            raise RuntimeError(f"failed to launch OpenCode CLI '{native_executable}': {exc}") from None

        if sys.platform != "win32":
            register_inflight_child_group(process.pid)
            group_registered = True

        threads = [
            threading.Thread(target=_read_pipe, args=(process.stdout, stdout_capture), daemon=True),
            threading.Thread(target=_read_pipe, args=(process.stderr, stderr_capture), daemon=True),
        ]
        for t in threads:
            t.start()

        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                process.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    _terminate_command_tree(process)
                    for t in threads:
                        t.join(timeout=1.0)
                    raise TimeoutError(f"OpenCode inference timed out after {timeout_seconds:.1f}s")

        for t in threads:
            t.join(timeout=1.0)

        if stdout_capture.truncated:
            raise ValueError(f"OpenCode output exceeded maximum response bound ({max_response_bytes} bytes)")

        if process.returncode != 0:
            err_text = redact(stderr_capture.text()[:500].strip())
            raise RuntimeError(f"OpenCode CLI exited with code {process.returncode}: {err_text}")

        raw_stdout = stdout_capture.text()
        return parse_opencode_output(raw_stdout)
    finally:
        if group_registered:
            unregister_inflight_child_group(process.pid)
        shutil.rmtree(work_dir, ignore_errors=True)


# --- Main Entry Point ---

def run_adapter(
    stdin_stream: Any = sys.stdin,
    stdout_stream: Any = sys.stdout,
    stderr_stream: Any = sys.stderr,
    argv: Optional[Sequence[str]] = None,
) -> int:
    parser = argparse.ArgumentParser(description="OpenCode Go DeepSeek V4 Pro command adapter")
    parser.add_argument("--model", default=DEFAULT_MODEL_ID, help="Model identity (must be deepseek-v4-pro)")
    parser.add_argument("--executable", required=True, help="Absolute verified OpenCode launcher/executable path")
    parser.add_argument("--variant", default=None, help="Optional variant (e.g. max/pro)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Per-request timeout in seconds")
    parser.add_argument("--max-response-bytes", type=int, default=MAX_RAW_RESPONSE_BYTES, help="Max response bytes")
    parser.add_argument("--evidence-file", default=None, help="Optional path to append evidence record")
    parser.add_argument("--auth-file", default=None, help="Explicit absolute auth store path (operator/test override)")
    parser.add_argument("--work-root", default=None, help="Absolute existing directory for disposable work dirs")
    parser.add_argument("--max-logical-model-calls", type=int, default=DEFAULT_MAX_LOGICAL_MODEL_CALLS, help="Micro-run logical call envelope")
    parser.add_argument("--preflight", action="store_true", help="Zero-inference identity + effective-config preflight; never runs a model")

    args = parser.parse_args(argv)

    # 1. Enforce exact model identity
    if args.model not in ALLOWED_MODEL_IDENTIFIERS:
        stderr_stream.write(
            f"Error: unsupported model '{redact(args.model)}'. Only 'deepseek-v4-pro' / 'opencode-go/deepseek-v4-pro' is allowed.\n"
        )
        stderr_stream.flush()
        return 1

    runtime_model_id = OPENCODE_MODEL_REF

    # 2. Zero-inference preflight mode: identity + effective config only.
    if args.preflight:
        try:
            identity = executable_identity.resolve_verified_opencode_executable(args.executable)
            root = Path(tempfile.mkdtemp(prefix="opencode-go-preflight-"))
            try:
                isolation = prepare_isolation(root, auth_content=None)
                effective = observe_effective_config(
                    isolation["environment"], root, identity["native_executable"]
                )
            finally:
                shutil.rmtree(root, ignore_errors=True)
            assertions = {
                "preflight": "passed",
                "provider_inference_started": False,
                "identity": identity,
                "effective_config": effective,
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

    # 3. Read request from stdin
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

    # 4. Enforce the 25-logical-call micro-run envelope
    envelope_error = validate_logical_call_index(request, args.max_logical_model_calls)
    if envelope_error is not None:
        stderr_stream.write(f"Error: {envelope_error}.\n")
        stderr_stream.flush()
        return 1

    # 5. Build prompt message
    try:
        message = build_protocol_message(request)
    except Exception as exc:
        stderr_stream.write(f"Error: failed to build protocol message: {redact(str(exc))}\n")
        stderr_stream.flush()
        return 1

    # 6. Resolve and prove the explicit OpenCode executable identity
    try:
        identity = executable_identity.resolve_verified_opencode_executable(args.executable)
    except Exception as exc:
        stderr_stream.write(f"Error: OpenCode executable identity resolution failed: {redact(str(exc))}\n")
        stderr_stream.flush()
        return 1

    # 7. Resolve the operator-owned auth store (memory-only; never a copy)
    try:
        auth_content = resolve_auth_store(args.auth_file)
    except Exception as exc:
        stderr_stream.write(f"Error: {redact(str(exc))}\n")
        stderr_stream.flush()
        return 1

    # 8. Execute inference
    try:
        combined_text, usage, telemetry = execute_inference(
            identity=identity,
            model=runtime_model_id,
            message=message,
            timeout_seconds=args.timeout,
            variant=args.variant,
            max_response_bytes=args.max_response_bytes,
            auth_content=auth_content,
            work_root=args.work_root,
        )
    except Exception as exc:
        stderr_stream.write(f"Error: OpenCode inference failed: {redact(str(exc))}\n")
        stderr_stream.flush()
        return 1

    # 9. Extract and strictly validate directive
    try:
        directive = extract_directive(combined_text, request)
    except Exception as exc:
        stderr_stream.write(f"Error: directive extraction failed: {redact(str(exc))}\n")
        stderr_stream.flush()
        return 1

    # 10. Format and emit response envelope
    response_payload: dict[str, Any] = {"directive": directive}
    if usage:
        response_payload["usage"] = usage
    if telemetry:
        response_payload["provider_telemetry"] = telemetry

    try:
        stdout_stream.write(json.dumps(response_payload, ensure_ascii=False) + "\n")
        stdout_stream.flush()
    except Exception as exc:
        stderr_stream.write(f"Error: failed to write response: {redact(str(exc))}\n")
        stderr_stream.flush()
        return 1

    # Optional evidence recording
    if args.evidence_file:
        try:
            evidence_path = Path(args.evidence_file)
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            with open(evidence_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "model": runtime_model_id,
                    "variant": args.variant,
                    "timestamp": time.time(),
                    "usage": usage,
                    "telemetry": telemetry,
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    return 0


def main() -> None:
    install_child_group_signal_handlers()
    sys.exit(run_adapter())


if __name__ == "__main__":
    main()
