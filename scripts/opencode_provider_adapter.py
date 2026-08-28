"""OpenCode provider command adapter for Local Application.

Generalizes the accepted frozen OpenCode Go adapter boundary
(``opencode_go_command_adapter.py``) to the operator's OpenCode Go model
catalog WITHOUT touching the frozen campaign contract: every reusable
mechanism (verified executable identity, operator auth-store handling,
isolation, bounded capture, process-tree termination, directive
validation) is imported from the frozen script, and only the model
identity is opened up.

Contract (identical to the frozen adapter):

1. Read exactly one protocol-1.3 JSON request object from stdin.
2. Validate the request context and the logical-call envelope.
3. Build the instruction-wrapped prompt (frozen adapter's shaping).
4. Resolve and prove the explicit OpenCode executable identity.
5. Execute ONE bounded non-interactive ``opencode run`` inference with
   the operator's auth store (credential bytes only inside the child
   environment; never disk, argv, history, or evidence).
6. Extract and strictly validate the model directive.
7. Emit ``{"directive_content": ...}`` (the preferred response envelope)
   on stdout and exit 0; typed stderr JSON envelope and exit 1 on failure.

Zero provider retries: exactly ONE OpenCode process per transport
request; the accepted ``LiveModelAdapter`` owns bounded retry attempts
above this boundary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

try:
    import opencode_executable_identity as executable_identity
    import opencode_go_command_adapter as frozen
except ImportError:  # pragma: no cover - defensive import path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import opencode_executable_identity as executable_identity
    import opencode_go_command_adapter as frozen

OPENCODE_PROVIDER_NAME = "opencode_go"
PROVIDER_COMPLETION_SCHEMA_VERSION = "opencode-provider-v1"
TOOL_VERSION = "opencode-provider-adapter-v1"

DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_LOGICAL_MODEL_CALLS = 64

#: OpenCode Go plan models are ``opencode-go/<id>``; free-tier models are
#: explicitly excluded so a subscription route is never silently degraded.
_MODEL_ID_PATTERN = __import__("re").compile(r"^opencode-go/[a-z0-9][a-z0-9._-]{0,63}$")


class OpenCodeProviderAdapterError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "adapter_error") -> None:
        redacted = frozen.redact(message)[:400]
        super().__init__(redacted)
        self.kind = kind


def validate_model_id(model_id: str) -> str:
    if type(model_id) is not str or not _MODEL_ID_PATTERN.match(model_id):
        raise OpenCodeProviderAdapterError(
            "model id must be an opencode-go/<id> subscription model",
            kind="configuration",
        )
    return model_id


def read_request(stdin_stream: Any) -> Mapping[str, Any]:
    raw = stdin_stream.buffer.readline(frozen.MAX_PUBLIC_REQUEST_BYTES + 1)
    if not raw:
        raise OpenCodeProviderAdapterError("no request on stdin", kind="invalid_request")
    if len(raw) > frozen.MAX_PUBLIC_REQUEST_BYTES:
        raise OpenCodeProviderAdapterError(
            "request exceeds the public request ceiling", kind="request_too_large"
        )
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OpenCodeProviderAdapterError(f"request is not valid JSON: {exc}", kind="invalid_request") from None
    if not isinstance(request, Mapping):
        raise OpenCodeProviderAdapterError("request must be a JSON object", kind="invalid_request")
    return request


def validate_logical_call_index(request: Mapping[str, Any], maximum: int) -> None:
    error = frozen.validate_logical_call_index(request, maximum)
    if error is not None:
        kind = "logical_call_limit" if "exceeds" in error else "invalid_request"
        raise OpenCodeProviderAdapterError(error, kind=kind)


def resolve_default_opencode_executable() -> Optional[str]:
    """PATH default for the verified launcher (absolute, platform-named).

    The frozen identity authority still proves the installation (npm
    layout, native target, version equality); this only supplies the
    starting point the product runtime previously required operators to
    pass by hand.
    """

    import shutil

    if sys.platform == "win32":
        return shutil.which("opencode.cmd") or shutil.which("opencode")
    return shutil.which("opencode")


def run_adapter(
    stdin_stream: Any = sys.stdin,
    stdout_stream: Any = sys.stdout,
    *,
    model: Optional[str] = None,
    opencode_executable: Optional[str] = None,
    auth_file: Optional[str] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_logical_calls: int = DEFAULT_MAX_LOGICAL_MODEL_CALLS,
) -> int:
    if model is None:
        raise OpenCodeProviderAdapterError("--model is required", kind="configuration")
    model = validate_model_id(model)
    request = read_request(stdin_stream)
    validate_logical_call_index(request, max_logical_calls)
    message = frozen.build_protocol_message(request)
    launcher = opencode_executable
    if launcher is None:
        launcher = resolve_default_opencode_executable()
        if launcher is None:
            raise OpenCodeProviderAdapterError(
                "opencode launcher not found on PATH", kind="configuration"
            )
    identity = executable_identity.resolve_verified_opencode_executable(launcher)
    auth_content = frozen.resolve_auth_store(auth_file)
    try:
        text, usage, _provider_telemetry = frozen.execute_inference(
            identity,
            model,
            message,
            timeout_seconds,
            auth_content=auth_content,
        )
    except TimeoutError as exc:
        raise OpenCodeProviderAdapterError(str(exc), kind="timeout") from None
    except (RuntimeError, ValueError) as exc:
        raise OpenCodeProviderAdapterError(str(exc), kind="http_error") from None
    if not isinstance(text, str) or not text.strip():
        raise OpenCodeProviderAdapterError(
            "OpenCode run returned an empty completion", kind="invalid_completion"
        )
    if len(text.encode("utf-8")) > frozen.MAX_RAW_RESPONSE_BYTES:
        raise OpenCodeProviderAdapterError(
            "OpenCode completion exceeds the response bound", kind="response_too_large"
        )
    # Bounded sanity check only: the completion must contain at least one
    # JSON object so the app-side resolver has directive material to
    # normalize.  Directive-shape validation, normalization, and rejection
    # feedback belong to the accepted LiveModelAdapter boundary (it accepts
    # both the ``kind``-tagged and the ``action``-keyed directive styles);
    # freezing the campaign's strict single-shape validation here would
    # reject directives the product runtime handles natively.
    try:
        candidate_found = bool(frozen._json_objects(text))
    except Exception:
        candidate_found = False
    if not candidate_found:
        raise OpenCodeProviderAdapterError(
            "completion does not contain a JSON directive object",
            kind="invalid_directive",
        )
    response: dict[str, Any] = {
        "provider_completion_schema_version": PROVIDER_COMPLETION_SCHEMA_VERSION,
        "directive_content": text,
    }
    if isinstance(usage, Mapping):
        response["usage"] = dict(usage)
    stdout_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
    stdout_stream.flush()
    return 0


def build_opencode_live_config(
    model_id: str,
    *,
    logical_call_ceiling: int = 32,
    request_timeout_seconds: Optional[float] = None,
    opencode_executable: Optional[str] = None,
    auth_file: Optional[str] = None,
):
    """Canonical OpenCode LiveModelConfig construction (no CLI contact)."""

    import sys as _sys
    from pathlib import Path as _Path

    from agentic_debugger.evaluation.live import LiveModelConfig as _LiveModelConfig

    model = validate_model_id(model_id)
    if type(logical_call_ceiling) is not int or isinstance(logical_call_ceiling, bool) or not 1 <= logical_call_ceiling <= 512:
        raise OpenCodeProviderAdapterError("logical call ceiling is invalid", kind="configuration")
    request_timeout = (
        DEFAULT_TIMEOUT_SECONDS if request_timeout_seconds is None else float(request_timeout_seconds)
    )
    if not 1.0 <= request_timeout <= 3600.0:
        raise OpenCodeProviderAdapterError("request timeout must be within [1, 3600] seconds", kind="configuration")
    root = _Path(__file__).resolve().parents[1]
    command = [
        _sys.executable,
        str(root / "scripts" / "opencode_provider_adapter.py"),
        "--model", model,
        "--timeout", str(int(request_timeout)),
        "--max-logical-model-calls", str(int(logical_call_ceiling)),
    ]
    if opencode_executable is not None:
        command.extend(("--opencode-executable", str(opencode_executable)))
    if auth_file is not None:
        command.extend(("--auth-file", str(auth_file)))
    return _LiveModelConfig(
        model_name=model,
        command=tuple(command),
        request_timeout_seconds=request_timeout,
        tool_version=TOOL_VERSION,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenCode provider protocol-1.3 command adapter")
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-logical-model-calls", type=int, default=DEFAULT_MAX_LOGICAL_MODEL_CALLS)
    parser.add_argument("--opencode-executable", default=None)
    parser.add_argument("--auth-file", default=None)
    args = parser.parse_args()
    try:
        raise SystemExit(
            run_adapter(
                sys.stdin,
                sys.stdout,
                model=args.model,
                opencode_executable=args.opencode_executable,
                auth_file=args.auth_file,
                timeout_seconds=args.timeout,
                max_logical_calls=args.max_logical_model_calls,
            )
        )
    except OpenCodeProviderAdapterError as exc:
        sys.stderr.write(
            json.dumps(
                {
                    "schema_version": "command-error-v1",
                    "kind": exc.kind,
                    "message": str(exc)[:400].replace("\n", " ").replace("\r", " "),
                }
            )
            + "\n"
        )
        sys.stderr.flush()
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
