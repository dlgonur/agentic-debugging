"""CommandCode GOAT plan command adapter for Local Application.

Adapts the accepted protocol-1.3 JSON-lines command-model contract to ONE
bounded CommandCode inference on the operator's GOAT plan.

Route design (mirrors the accepted OpenCode Go adapter boundary):

1. Read exactly one protocol-1.3 JSON request object from stdin.
2. Validate the request context and the logical-call envelope
   (``protocol.logical_model_call_index``).
3. Build the canonical instruction-wrapped prompt with the SAME
   battle-tested request shaping as the Ollama Cloud adapter (imported,
   not duplicated).
4. Resolve the operator's ``cmdc`` CLI (``cmdc``/``cmd``/``command-code``
   on PATH).  Authentication is the operator-owned Command Code auth
   store (``~/.commandcode/auth.json``) read in place BY THE CLI; this
   adapter never reads, copies, logs, or transports credential bytes.
5. Execute ONE bounded non-interactive ``cmdc -p`` inference in a fresh
   empty directory (never a workspace with real files: the CLI injects
   directory context into the model prompt), with ``--no-session`` so
   one-shot directive calls do not persist conversation transcripts,
   and ``--no-auto-update`` so a self-update cannot corrupt a live run.
6. Parse the NDJSON event stream and take the final ``{"type": "result"}``
   line: ``finalText`` is the model answer, ``usage`` the token usage.
7. Emit the transport-level response envelope (``directive_content``)
   on stdout and exit 0.
8. On any failure emit the strict typed stderr JSON envelope
   (``command-error-v1``, closed kind vocabulary) and exit 1.

Zero provider retries: the adapter performs exactly ONE CLI inference per
transport request.  The accepted ``LiveModelAdapter`` still owns bounded
directive-feedback retry attempts above this boundary.

Why the CLI route and not the raw provider API: the CommandCode provider
endpoint fronts Cloudflare bot protection that rejects non-browser TLS
fingerprints (observed 403 ``error code: 1010`` for stdlib HTTP clients),
while the operator CLI is the supported, authenticated, stable entry point
— exactly the pattern the frozen OpenCode Go adapter established.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

try:
    import ollama_cloud_command_adapter as ollama_adapter
except ImportError:  # pragma: no cover - defensive import path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ollama_cloud_command_adapter as ollama_adapter

COMMANDCODE_PROVIDER_NAME = "commandcode_goat"
# The accepted provider-completion envelope schema (must match the
# app-side resolver exactly; same constant as the Ollama adapter).
PROVIDER_COMPLETION_SCHEMA_VERSION = "provider-completion-v1"
TOOL_VERSION = "commandcode-goat-adapter-v1"

#: Closed, provider-safe error kinds (subset of the accepted vocabulary).
ERROR_KIND_ADAPTER = "adapter_error"
ERROR_KIND_CONFIGURATION = "configuration"
ERROR_KIND_HTTP = "http_error"
ERROR_KIND_INVALID_COMPLETION = "invalid_completion"
ERROR_KIND_INVALID_REQUEST = "invalid_request"
ERROR_KIND_LOGICAL_CALL = "logical_call_limit"
ERROR_KIND_REQUEST_TOO_LARGE = "request_too_large"
ERROR_KIND_RESPONSE_TOO_LARGE = "response_too_large"
ERROR_KIND_TIMEOUT = "timeout"

DEFAULT_TIMEOUT_SECONDS = 300.0
MAX_TIMEOUT_SECONDS = 3600.0
DEFAULT_MAX_LOGICAL_MODEL_CALLS = 64
DEFAULT_MAX_TURNS = 4
#: The CLI result line echoes the whole conversation (prompt included), so
#: the raw capture bound must exceed the request ceiling comfortably.
MAX_RAW_OUTPUT_BYTES = 512 * 1024
#: The model answer itself is one JSON directive object; anything larger is
#: a runaway completion, not a directive.
MAX_FINAL_TEXT_BYTES = 64 * 1024
MAX_CLI_TURN_SECONDS = 1.0

_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:bearer\s+[a-zA-Z0-9._~+/-]+=*|sk-[a-zA-Z0-9_-]{10,}|"
    r"(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|token)"
    r"\s*[:=]\s*\S+)"
)

_MODEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}(?:/[a-z0-9][a-z0-9._-]{0,63})?$")

# Actual CommandCode executable names only.  The system shell "cmd.exe"
# is deliberately NOT a candidate: on Windows ``shutil.which("cmd")``
# resolves the operating-system shell, which must never be executed as
# the CommandCode provider.
_CANDIDATE_EXECUTABLES = ("cmdc", "command-code", "commandcode")


class CommandCodeAdapterError(RuntimeError):
    """Fail-closed adapter error carrying a closed-vocabulary kind."""

    def __init__(self, message: str, *, kind: str = ERROR_KIND_ADAPTER) -> None:
        redacted = _redact(message)[:400]
        super().__init__(redacted)
        self.kind = kind


def _redact(text: str) -> str:
    return _SECRET_PATTERN.sub("<redacted>", text)


def validate_model_id(model_id: str) -> str:
    if type(model_id) is not str or not _MODEL_ID_PATTERN.match(model_id):
        raise CommandCodeAdapterError(
            "model id must match vendor/slug with [a-z0-9._-] segments",
            kind=ERROR_KIND_CONFIGURATION,
        )
    return model_id


def validate_timeout_seconds(timeout_seconds: float) -> float:
    if (
        type(timeout_seconds) is not float and type(timeout_seconds) is not int
    ) or isinstance(timeout_seconds, bool):
        raise CommandCodeAdapterError("timeout must be numeric", kind=ERROR_KIND_CONFIGURATION)
    value = float(timeout_seconds)
    if not 1.0 <= value <= MAX_TIMEOUT_SECONDS:
        raise CommandCodeAdapterError(
            f"timeout must be within [1, {MAX_TIMEOUT_SECONDS}] seconds",
            kind=ERROR_KIND_CONFIGURATION,
        )
    return value


def resolve_cmdc_command(explicit: Optional[str] = None) -> Tuple[str, ...]:
    """Resolve the CommandCode CLI invocation prefix (argv head).

    npm's ``cmdc.CMD`` shell shim is unusable for this boundary: cmd.exe
    re-parses the shim's ``%*`` and eats prompt characters that cmd.exe
    treats specially (``&``, ``<``, ``>``, ``|``), silently truncating the
    model prompt and flags (observed: the CLI answered a one-line fragment
    in plain text).  The robust route mirrors the frozen OpenCode adapter:
    resolve the package's native entry (``dist/index.mjs``) next to the
    shim and execute it with the verified ``node`` runtime directly.

    An explicit value may be the ``.mjs`` entry (executed with node), a
    real ``.exe``, or a shim whose sibling package is resolved as above.
    """

    import shutil

    node = shutil.which("node")
    if node is None:
        raise CommandCodeAdapterError(
            "node.js runtime not found on PATH (required to run the CommandCode CLI)",
            kind=ERROR_KIND_CONFIGURATION,
        )

    def _package_entry_from_shim(shim: str) -> Optional[str]:
        entry = Path(shim).resolve().parent / "node_modules" / "command-code" / "dist" / "index.mjs"
        return str(entry) if entry.is_file() else None

    if explicit is not None:
        if type(explicit) is not str or not explicit.strip():
            raise CommandCodeAdapterError("explicit executable must be a non-empty path", kind=ERROR_KIND_CONFIGURATION)
        path = Path(explicit)
        suffix = path.suffix.lower()
        if suffix == ".mjs":
            if not path.is_file():
                raise CommandCodeAdapterError(f"explicit entry not found: {explicit}", kind=ERROR_KIND_CONFIGURATION)
            return (node, str(path))
        if suffix == ".exe":
            if not path.is_file():
                raise CommandCodeAdapterError(f"explicit executable not found: {explicit}", kind=ERROR_KIND_CONFIGURATION)
            return (str(path),)
        entry = _package_entry_from_shim(explicit)
        if entry is None:
            raise CommandCodeAdapterError(
                "explicit shim could not be resolved to the command-code package entry",
                kind=ERROR_KIND_CONFIGURATION,
            )
        return (node, entry)

    for name in _CANDIDATE_EXECUTABLES:
        found = shutil.which(name)
        if not found:
            continue
        suffix = Path(found).suffix.lower()
        if suffix == ".exe":
            return (found,)
        entry = _package_entry_from_shim(found)
        if entry is not None:
            return (node, entry)
    raise CommandCodeAdapterError(
        "CommandCode CLI not found (expected one of: " + ", ".join(_CANDIDATE_EXECUTABLES) + " on PATH)",
        kind=ERROR_KIND_CONFIGURATION,
    )


class _BoundedCapture:
    def __init__(self, maximum_bytes: int) -> None:
        self._maximum = maximum_bytes
        self._chunks: List[bytes] = []
        self._size = 0
        self.truncated = False

    def add(self, chunk: bytes) -> None:
        if self.truncated:
            return
        if self._size + len(chunk) > self._maximum:
            self.truncated = True
            return
        self._chunks.append(chunk)
        self._size += len(chunk)

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


def _read_pipe(pipe: Any, capture: _BoundedCapture) -> None:
    try:
        while True:
            chunk = pipe.read(4096)
            if not chunk:
                break
            capture.add(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8", errors="replace"))
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def read_request(stdin_stream: Any) -> Mapping[str, Any]:
    raw = stdin_stream.buffer.readline(ollama_adapter.MAX_PUBLIC_REQUEST_BYTES + 1)
    if not raw:
        raise CommandCodeAdapterError("no request on stdin", kind=ERROR_KIND_INVALID_REQUEST)
    if len(raw) > ollama_adapter.MAX_PUBLIC_REQUEST_BYTES:
        raise CommandCodeAdapterError(
            "request exceeds the public request ceiling", kind=ERROR_KIND_REQUEST_TOO_LARGE
        )
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CommandCodeAdapterError(f"request is not valid JSON: {exc}", kind=ERROR_KIND_INVALID_REQUEST) from None
    if not isinstance(request, Mapping):
        raise CommandCodeAdapterError("request must be a JSON object", kind=ERROR_KIND_INVALID_REQUEST)
    return request


def validate_logical_call_index(request: Mapping[str, Any], maximum: int) -> None:
    protocol = request.get("protocol")
    if not isinstance(protocol, Mapping):
        raise CommandCodeAdapterError("request is missing the protocol envelope", kind=ERROR_KIND_INVALID_REQUEST)
    index = protocol.get("logical_model_call_index")
    # The live Local Application controller uses zero-based model-call
    # indices: the first request is 0 and an N-call envelope is 0..N-1
    # (same contract as the accepted Ollama Cloud adapter).
    if type(index) is not int or isinstance(index, bool) or not 0 <= index < maximum:
        if type(index) is int and not isinstance(index, bool) and index >= maximum:
            raise CommandCodeAdapterError(
                f"logical model call {index} is outside the session envelope (0..{maximum - 1})",
                kind=ERROR_KIND_LOGICAL_CALL,
            )
        raise CommandCodeAdapterError(
            "logical_model_call_index must be an integer within the session envelope",
            kind=ERROR_KIND_INVALID_REQUEST,
        )


def build_prompt(request: Mapping[str, Any]) -> str:
    """One user message: shared system contract + wrapped canonical request."""

    messages = ollama_adapter.build_chat_messages(request)
    system = messages[0]["content"]
    user = messages[1]["content"]
    return (
        f"{system}\n\n"
        "You are answering as a debugging controller through an automated pipe. "
        "Respond with EXACTLY ONE JSON object and nothing else: no prose, no "
        "markdown fence, no tool calls.\n\n"
        f"{user}"
    )


def parse_cli_result(raw_output: str) -> Tuple[str, Optional[Mapping[str, Any]], str]:
    """Extract (final_text, usage, stop_reason) from the NDJSON stream.

    The authoritative record is the LAST ``{"type": "result"}`` line.  Event
    lines before it are progress noise for this boundary.
    """

    result: Optional[Mapping[str, Any]] = None
    for line in raw_output.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, Mapping) and candidate.get("type") == "result":
            result = candidate
    if result is None:
        raise CommandCodeAdapterError(
            "CommandCode CLI produced no result record", kind=ERROR_KIND_INVALID_COMPLETION
        )
    subtype = result.get("subtype")
    if subtype != "success":
        detail = _redact(str(result.get("error") or subtype or "unknown error"))[:300]
        raise CommandCodeAdapterError(f"CommandCode run failed: {detail}", kind=ERROR_KIND_HTTP)
    final_text = result.get("finalText")
    if type(final_text) is not str or not final_text.strip():
        raise CommandCodeAdapterError(
            "CommandCode run returned an empty completion", kind=ERROR_KIND_INVALID_COMPLETION
        )
    if len(final_text.encode("utf-8")) > MAX_FINAL_TEXT_BYTES:
        raise CommandCodeAdapterError(
            "CommandCode completion exceeds the response bound", kind=ERROR_KIND_RESPONSE_TOO_LARGE
        )
    usage = result.get("usage")
    usage_mapping = usage if isinstance(usage, Mapping) else None
    return final_text, usage_mapping, str(result.get("stopReason") or "")


def execute_inference(
    command_prefix: Tuple[str, ...],
    model: str,
    message: str,
    timeout_seconds: float,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_output_bytes: int = MAX_RAW_OUTPUT_BYTES,
) -> Tuple[str, Optional[Mapping[str, Any]], str]:
    """Execute ONE bounded ``cmdc -p`` inference in a fresh empty directory."""

    if type(max_turns) is not int or isinstance(max_turns, bool) or not 1 <= max_turns <= 16:
        raise CommandCodeAdapterError("max turns must be within [1, 16]", kind=ERROR_KIND_CONFIGURATION)
    work_dir = Path(tempfile.mkdtemp(prefix="commandcode-goat-run-"))
    try:
        command = [
            *command_prefix,
            "-p", message,
            "--output-format", "json",
            "--no-session",
            "--no-auto-update",
            "--skip-onboarding",
            "--max-turns", str(max_turns),
            "-m", model,
        ]
        command_line = subprocess.list2cmdline(command)
        if len(command_line) > 30000:
            raise CommandCodeAdapterError(
                "command line exceeds the native bound", kind=ERROR_KIND_REQUEST_TOO_LARGE
            )
        stdout_capture = _BoundedCapture(max_output_bytes)
        stderr_capture = _BoundedCapture(16 * 1024)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                cwd=str(work_dir),
            )
        except Exception as exc:
            raise CommandCodeAdapterError(
                f"failed to launch CommandCode CLI: {exc}", kind=ERROR_KIND_CONFIGURATION
            ) from None
        threads = [
            threading.Thread(target=_read_pipe, args=(process.stdout, stdout_capture), daemon=True),
            threading.Thread(target=_read_pipe, args=(process.stderr, stderr_capture), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            for thread in threads:
                thread.join(timeout=1.0)
            raise CommandCodeAdapterError(
                f"CommandCode inference timed out after {timeout_seconds:.1f}s", kind=ERROR_KIND_TIMEOUT
            ) from None
        for thread in threads:
            thread.join(timeout=1.0)
        if stdout_capture.truncated:
            raise CommandCodeAdapterError(
                "CommandCode output exceeded the response bound", kind=ERROR_KIND_RESPONSE_TOO_LARGE
            )
        if process.returncode != 0:
            detail = _redact(stderr_capture.text()[:300].strip())
            raise CommandCodeAdapterError(
                f"CommandCode CLI exited with code {process.returncode}: {detail}", kind=ERROR_KIND_HTTP
            )
        return parse_cli_result(stdout_capture.text())
    finally:
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)


def run_adapter(
    stdin_stream: Any = sys.stdin,
    stdout_stream: Any = sys.stdout,
    *,
    executable: Optional[str] = None,
    model: Optional[str] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_logical_calls: int = DEFAULT_MAX_LOGICAL_MODEL_CALLS,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> int:
    if model is None:
        raise CommandCodeAdapterError("--model is required", kind=ERROR_KIND_CONFIGURATION)
    model = validate_model_id(model)
    timeout_seconds = validate_timeout_seconds(timeout_seconds)
    request = read_request(stdin_stream)
    validate_logical_call_index(request, max_logical_calls)
    prompt = build_prompt(request)
    cli = resolve_cmdc_command(executable)
    final_text, usage, _stop = execute_inference(
        cli,
        model,
        prompt,
        timeout_seconds,
        max_turns=max_turns,
    )
    response = {
        "provider_completion_schema_version": PROVIDER_COMPLETION_SCHEMA_VERSION,
        "directive_content": final_text,
    }
    if usage is not None:
        response["usage"] = dict(usage)
    stdout_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
    stdout_stream.flush()
    return 0


def run_list_models(
    stdout_stream: Any = sys.stdout,
    *,
    executable: Optional[str] = None,
    timeout_seconds: float = 60.0,
) -> int:
    """Print the operator's live GOAT model catalog as one JSON array.

    Read-only; consumes no generation credits.
    """

    cli = resolve_cmdc_command(executable)
    try:
        result = subprocess.run(
            [*cli, "--list-models"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise CommandCodeAdapterError("model listing timed out", kind=ERROR_KIND_TIMEOUT) from None
    if result.returncode != 0:
        raise CommandCodeAdapterError(
            f"model listing failed: {_redact(result.stderr[:200])}", kind=ERROR_KIND_HTTP
        )
    import re as _re

    ids = sorted(set(_re.findall(r"\b([a-z0-9][a-z0-9._/-]{2,80})\b", result.stdout)))
    models = [item for item in ids if "/" in item or item.startswith("gpt") or item.startswith("claude")]
    stdout_stream.write(json.dumps({"schema_version": "commandcode-models-v1", "models": models}) + "\n")
    stdout_stream.flush()
    return 0


def build_commandcode_live_config(
    model_id: str,
    *,
    logical_call_ceiling: int = 32,
    request_timeout_seconds: Optional[float] = None,
    cmdc_executable: Optional[str] = None,
):
    """Canonical CommandCode LiveModelConfig construction (no CLI contact)."""

    import sys as _sys
    from pathlib import Path as _Path

    from agentic_debugger.evaluation.live import LiveModelConfig as _LiveModelConfig

    model = validate_model_id(model_id)
    if type(logical_call_ceiling) is not int or isinstance(logical_call_ceiling, bool) or not 1 <= logical_call_ceiling <= 512:
        raise CommandCodeAdapterError("logical call ceiling is invalid", kind=ERROR_KIND_CONFIGURATION)
    request_timeout = validate_timeout_seconds(
        DEFAULT_TIMEOUT_SECONDS if request_timeout_seconds is None else request_timeout_seconds
    )
    root = _Path(__file__).resolve().parents[1]
    command: List[str] = [
        _sys.executable,
        str(root / "scripts" / "commandcode_goat_adapter.py"),
        "--model", model,
        "--timeout", str(int(request_timeout)),
        "--max-logical-model-calls", str(int(logical_call_ceiling)),
    ]
    if cmdc_executable is not None:
        command.extend(("--cmdc-executable", str(Path(cmdc_executable))))
    return _LiveModelConfig(
        model_name=model,
        command=tuple(command),
        request_timeout_seconds=request_timeout,
        tool_version=TOOL_VERSION,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="CommandCode GOAT protocol-1.3 command adapter")
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-logical-model-calls", type=int, default=DEFAULT_MAX_LOGICAL_MODEL_CALLS)
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument("--cmdc-executable", default=None)
    parser.add_argument("--list-models", action="store_true")
    args = parser.parse_args()
    try:
        if args.list_models:
            raise SystemExit(run_list_models(sys.stdout, executable=args.cmdc_executable))
        raise SystemExit(
            run_adapter(
                sys.stdin,
                sys.stdout,
                executable=args.cmdc_executable,
                model=args.model,
                timeout_seconds=args.timeout,
                max_logical_calls=args.max_logical_model_calls,
                max_turns=args.max_turns,
            )
        )
    except CommandCodeAdapterError as exc:
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
