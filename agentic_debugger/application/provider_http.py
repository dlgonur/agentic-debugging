"""Bounded direct-HTTP client for the built-in provider connections.

One module owns every provider HTTP boundary so the safety rules are
stated (and testable) exactly once:

- explicit URLs: ``https`` is required for every non-loopback host;
  plain ``http`` is accepted only for loopback addresses so local fake
  provider servers can exercise the real code path in tests;
- explicit timeout on every request; a request never outlives it;
- bounded request payload and bounded response capture: an oversized
  response is a typed failure, never a silent truncation;
- exactly ONE request per call: no hidden provider retries and no
  engine re-issuing; the caller decides the engine deterministically
  before the request;
- zero credential leakage: the credential travels only inside request
  headers (engine memory / child stdin pipe), and error text is
  redacted and bounded before it ever leaves this module.

Two engines exist for one honest reason: both built-in subscription
provider endpoints sit behind a bot-protection layer that rejects the
Python stdlib TLS signature with HTTP 403 ``error code: 1010``
(verified 2026-08-30 against ``opencode.ai`` and
``api.commandcode.ai``).  The OS ``curl`` client is accepted by the
same endpoints, so the provider connection contract may declare
``tls_signature_blocked`` and the deterministic engine selection uses
``curl`` when it is present.  Engine selection happens BEFORE the
request and is never a fallback loop; when ``curl`` is absent the
stdlib attempt fails closed with the sanitized provider error.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "ProviderHttpError",
    "curl_executable",
    "describe_url",
    "request_json",
]

#: Default bounded response capture (4 MiB): generous enough for any
#: documented catalog/completion payload, tight enough to fail fast on
#: runaway responses.
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: Hard ceilings for caller-controlled transport bounds.  Current provider
#: requests and captures use smaller limits; these caps keep the common HTTP
#: boundary bounded even if a future caller supplies an unsafe value.
_MAX_REQUEST_BYTES = 4 * 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024

#: Bounded diagnostic snippet carried by sanitized provider errors.
_MAX_ERROR_SNIPPET_CHARS = 200

#: Maximum URL length accepted (bounded input; provider URLs are short).
_MAX_URL_CHARS = 2048

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_SECRET_VALUE = re.compile(
    r"(?i)\b(?:bearer|basic)\s+\S+|"
    r"\b(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|token)\s*[:=]\s*\S+|"
    r"sk-[A-Za-z0-9_-]{10,}"
)


class ProviderHttpError(RuntimeError):
    """Typed, credential-safe provider HTTP failure.

    ``kind`` is a closed vocabulary so callers classify failures without
    parsing text: ``invalid_url``, ``invalid_request``, ``timeout``,
    ``connection_error``, ``tls_blocked``, ``http_status``,
    ``response_too_large``, ``invalid_response``, ``engine_unavailable``.
    The message is bounded and redacted; response bodies are never
    echoed beyond a bounded, redacted snippet.
    """

    def __init__(self, message: str, *, kind: str, status: Optional[int] = None) -> None:
        super().__init__(sanitize_text(message))
        self.kind = kind
        self.status = status


def sanitize_text(text: str, limit: int = _MAX_ERROR_SNIPPET_CHARS) -> str:
    """Redact credential-shaped tokens and bound a diagnostic string."""

    if type(text) is not str:
        text = str(text)
    redacted = _SECRET_VALUE.sub("<redacted>", text)
    redacted = redacted.replace("\r", " ").replace("\n", " ")
    encoded = redacted.encode("utf-8", errors="replace")
    if len(encoded) > limit:
        redacted = encoded[: limit - 3].decode("utf-8", errors="ignore") + "..."
    return redacted


def curl_executable() -> Optional[str]:
    """The OS curl client path, or ``None`` when absent."""

    found = shutil.which("curl") or shutil.which("curl.exe")
    return str(found) if found else None


def describe_url(url: str) -> str:
    """Credential-free endpoint identity (scheme + host + path)."""

    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path or ''}"


def _validate_url(url: str) -> None:
    if type(url) is not str or not url or len(url) > _MAX_URL_CHARS:
        raise ProviderHttpError("provider URL is missing or oversized", kind="invalid_url")
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise ProviderHttpError(
            "provider URL contains control characters", kind="invalid_url"
        )
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise ProviderHttpError("provider URL scheme is not allowed", kind="invalid_url")
    if parts.scheme == "http":
        host = (parts.hostname or "").lower()
        if host not in _LOOPBACK_HOSTS:
            raise ProviderHttpError(
                "provider URL must use HTTPS (plain HTTP is accepted only for loopback tests)",
                kind="invalid_url",
            )
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ProviderHttpError(
            "provider URL must not embed credentials, queries, or fragments",
            kind="invalid_url",
        )


def _body_bytes(
    json_payload: Optional[Mapping[str, Any]], method: str
) -> Optional[bytes]:
    if json_payload is None:
        return None
    if method != "POST":
        raise ProviderHttpError(
            "a request body requires the POST method", kind="invalid_request"
        )
    try:
        encoded = json.dumps(
            json_payload, ensure_ascii=False, allow_nan=False, sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProviderHttpError(
            f"request payload could not be serialized: {exc}", kind="invalid_request"
        ) from None
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise ProviderHttpError(
            "request payload exceeded the transport bound", kind="invalid_request"
        )
    return encoded


def _is_tls_block(body: str) -> bool:
    lowered = body.lower()
    return "error code: 1010" in lowered or '"error_code": 1010' in lowered.replace(
        " ", ""
    )


def _stdlib_request(
    method: str,
    url: str,
    *,
    credential: Optional[str],
    body: Optional[bytes],
    timeout_seconds: float,
    max_response_bytes: int,
) -> Mapping[str, Any]:
    headers = {"Accept": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            payload_bytes = response.read(max_response_bytes + 1)
    except urllib.error.HTTPError as exc:
        snippet = ""
        try:
            snippet = exc.read(4096).decode("utf-8", errors="replace")
        except Exception:
            pass
        if exc.code == 403 and _is_tls_block(snippet):
            raise ProviderHttpError(
                "provider endpoint rejected this client's TLS signature "
                "(bot-protection 1010); the OS curl engine is required",
                kind="tls_blocked",
                status=int(exc.code),
            ) from None
        raise ProviderHttpError(
            f"provider returned HTTP {exc.code}: {sanitize_text(snippet)}",
            kind="http_status",
            status=int(exc.code),
        ) from None
    except TimeoutError as exc:
        raise ProviderHttpError("provider request timed out", kind="timeout") from exc
    except Exception as exc:
        reason = getattr(exc, "reason", None) or exc
        if isinstance(reason, (TimeoutError, OSError)) and "timed out" in str(reason).lower():
            raise ProviderHttpError("provider request timed out", kind="timeout") from None
        raise ProviderHttpError(
            f"provider request failed: {sanitize_text(str(reason))}",
            kind="connection_error",
        ) from None
    return _parse_response(payload_bytes, status, max_response_bytes)


def _curl_config(
    method: str,
    url: str,
    *,
    credential: Optional[str],
    body: Optional[bytes],
) -> str:
    """Build a curl stdin config; the credential never touches argv."""

    def quote(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    lines = [f'url = "{quote(url)}"', f'request = "{quote(method)}"']
    if credential:
        lines.append(f'header = "Authorization: Bearer {quote(credential)}"')
    lines.append('header = "Accept: application/json"')
    if body is not None:
        lines.append('header = "Content-Type: application/json"')
        lines.append(f'data = "{quote(body.decode("utf-8"))}"')
    return "\n".join(lines) + "\n"


_STATUS_MARKER = "HTTP_STATUS:"


def _curl_request(
    method: str,
    url: str,
    *,
    credential: Optional[str],
    body: Optional[bytes],
    timeout_seconds: float,
    max_response_bytes: int,
    executable: str,
) -> Mapping[str, Any]:
    argv = [
        executable,
        "--silent",
        "--show-error",
        "--max-time",
        str(max(1, int(timeout_seconds))),
        "--proto-redir",
        "=https",
        "--output",
        "-",
        "--write-out",
        _STATUS_MARKER + "%{http_code}",
        "--config",
        "-",
    ]
    if url.lower().startswith("https://"):
        argv.extend(["--proto", "=https"])
    config = _curl_config(method, url, credential=credential, body=body).encode("utf-8")
    stdout = _BoundedCapture(max_response_bytes + 64)
    stderr = _BoundedCapture(4096)
    process: Optional[subprocess.Popen] = None
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except (OSError, ValueError) as exc:
        raise ProviderHttpError(
            f"curl engine could not be launched: {sanitize_text(str(exc))}",
            kind="engine_unavailable",
        ) from None
    assert process is not None
    threads = [
        threading.Thread(target=_drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    write_error: list[BaseException] = []

    def write_stdin() -> None:
        try:
            assert process is not None and process.stdin is not None
            process.stdin.write(config)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError) as exc:
            write_error.append(exc)

    writer = threading.Thread(target=write_stdin, daemon=True)
    writer.start()
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds + 5.0)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process)
    writer.join(timeout=2.0)
    for thread in threads:
        thread.join(timeout=2.0)
    if timed_out:
        raise ProviderHttpError("provider request timed out", kind="timeout")
    if stdout.truncated:
        raise ProviderHttpError(
            "provider response exceeded the capture bound",
            kind="response_too_large",
        )
    if write_error and process.returncode != 0:
        raise ProviderHttpError(
            "provider request could not be delivered",
            kind="connection_error",
        )
    text = stdout.text()
    _, marker, status_text = text.rpartition(_STATUS_MARKER)
    body = text[: len(text) - len(marker) - len(status_text)] if marker else ""
    if not marker:
        raise ProviderHttpError(
            "curl engine returned no HTTP status: "
            f"{sanitize_text(stderr.text())}",
            kind="connection_error",
        )
    try:
        status = int(status_text.strip())
    except ValueError:
        raise ProviderHttpError(
            "curl engine returned an invalid HTTP status",
            kind="invalid_response",
        ) from None
    if status == 403 and _is_tls_block(body):
        raise ProviderHttpError(
            "provider endpoint rejected this client's TLS signature "
            "(bot-protection 1010)",
            kind="tls_blocked",
            status=status,
        )
    if not 200 <= status < 300:
        raise ProviderHttpError(
            f"provider returned HTTP {status}: {sanitize_text(body)}",
            kind="http_status",
            status=status,
        )
    return _parse_response(body.encode("utf-8"), status, max_response_bytes)


def _parse_response(
    payload_bytes: bytes, status: int, max_response_bytes: int
) -> Mapping[str, Any]:
    if len(payload_bytes) > max_response_bytes:
        raise ProviderHttpError(
            "provider response exceeded the capture bound",
            kind="response_too_large",
        )
    try:
        value = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ProviderHttpError(
            "provider response was not valid JSON", kind="invalid_response"
        ) from None
    if not isinstance(value, Mapping):
        raise ProviderHttpError(
            "provider response was not a JSON object", kind="invalid_response"
        )
    return value


class _BoundedCapture:
    """Thread-safe bounded byte capture (same shape as the accepted
    command transports use for subprocess pipes)."""

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


def _drain(pipe: Any, capture: _BoundedCapture) -> None:
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                return
            capture.add(chunk)
    except Exception:
        return


def _terminate(process: subprocess.Popen) -> None:
    try:
        process.terminate()
        process.wait(timeout=1)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            pass


def request_json(
    method: str,
    url: str,
    *,
    credential: Optional[str] = None,
    json_payload: Optional[Mapping[str, Any]] = None,
    timeout_seconds: float = 30.0,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    engine: Optional[str] = None,
    tls_signature_blocked: bool = False,
) -> Mapping[str, Any]:
    """One bounded JSON request; exactly one network attempt.

    ``engine`` selects the transport deterministically before the
    request: ``"stdlib"`` (urllib), ``"curl"`` (OS curl client), or
    ``None`` for the automatic choice, which uses ``curl`` only when the
    endpoint contract declares a TLS-signature block and a curl client
    exists.  There is no engine fallback and no retry: a failure raises
    the typed :class:`ProviderHttpError` once.
    """

    if type(method) is not str or method not in ("GET", "POST"):
        raise ProviderHttpError("method must be GET or POST", kind="invalid_request")
    _validate_url(url)
    if type(timeout_seconds) is not float and type(timeout_seconds) is not int:
        raise ProviderHttpError("timeout must be a number", kind="invalid_request")
    if not 0 < float(timeout_seconds) <= 3600:
        raise ProviderHttpError("timeout is out of bounds", kind="invalid_request")
    if credential is not None and (
        type(credential) is not str
        or not credential
        or len(credential) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in credential)
    ):
        raise ProviderHttpError("credential is missing or oversized", kind="invalid_request")
    if (
        type(max_response_bytes) is not int
        or isinstance(max_response_bytes, bool)
        or not 1 <= max_response_bytes <= _MAX_RESPONSE_BYTES
    ):
        raise ProviderHttpError(
            "response capture bound is invalid", kind="invalid_request"
        )
    body = _body_bytes(json_payload, method)
    chosen = engine or (
        "curl" if tls_signature_blocked and curl_executable() else "stdlib"
    )
    if chosen == "curl":
        executable = curl_executable()
        if executable is None:
            raise ProviderHttpError(
                "the OS curl client is required for this endpoint but was not found",
                kind="engine_unavailable",
            )
        return _curl_request(
            method,
            url,
            credential=credential,
            body=body,
            timeout_seconds=float(timeout_seconds),
            max_response_bytes=max_response_bytes,
            executable=executable,
        )
    if chosen == "stdlib":
        return _stdlib_request(
            method,
            url,
            credential=credential,
            body=body,
            timeout_seconds=float(timeout_seconds),
            max_response_bytes=max_response_bytes,
        )
    raise ProviderHttpError(f"unknown HTTP engine: {chosen!r}", kind="invalid_request")
