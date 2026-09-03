"""Provider direct-API command adapter for the general application runtime.

The HTTP sibling of the accepted CLI adapters
(``scripts/opencode_provider_adapter.py``,
``scripts/commandcode_goat_adapter.py``): it speaks the identical
protocol-1.3 JSONL command contract, but performs ONE direct provider
API inference over bounded HTTPS instead of launching a provider CLI.

Contract (identical to the accepted adapters):

1. Read exactly one protocol-1.3 JSON request object from stdin.
2. Validate the request context and the logical-call envelope.
3. Build the model-facing prompt pair through the provider-neutral
   protocol-1.3 shaping authority (``scripts/protocol_prompt_shaper.py``),
   then shape it per protocol family using that family's dedicated
   system/instruction channel.
4. Resolve the credential inside this boundary only: the app-injected
   session credential, the app-supported provider environment source,
   or (OpenCode Go) the CLI auth store read in place.  The value never
   appears in argv, diagnostics, evidence, or this process's output.
5. Shape ONE provider request for the model's resolved protocol family
   (Chat Completions / Responses / Messages) and execute it with
   explicit timeout, bounded payload, and bounded response capture.
6. Parse the completion strictly and boundedly; usage is copied when
   the provider supplies it and never fabricated.
7. Emit ``{"provider_completion_schema": ..., "directive_content": ...}``
   on stdout and exit 0; typed stderr envelope and exit 1 on failure.

Zero provider retries, zero model fallback: exactly ONE inference per
transport request; the accepted ``LiveModelAdapter`` owns bounded retry
attempts with directive feedback above this boundary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

try:
    import opencode_go_command_adapter as frozen
except ImportError:  # pragma: no cover - defensive import path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import opencode_go_command_adapter as frozen

try:
    import protocol_prompt_shaper as prompt_shaper
except ImportError:  # pragma: no cover - defensive import path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import protocol_prompt_shaper as prompt_shaper

try:
    from agentic_debugger.application.provider_connections import (
        inference_path_for,
        provider_api_model_id,
        resolve_runtime_credential,
    )
    from agentic_debugger.application.provider_http import (
        ProviderHttpError,
        request_json,
        sanitize_text,
    )
except ImportError:  # pragma: no cover - defensive import path (bare child)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agentic_debugger.application.provider_connections import (
        inference_path_for,
        provider_api_model_id,
        resolve_runtime_credential,
    )
    from agentic_debugger.application.provider_http import (
        ProviderHttpError,
        request_json,
        sanitize_text,
    )

PROVIDER_COMPLETION_SCHEMA_VERSION = "provider-completion-v1"
TOOL_VERSION = "provider-direct-api-adapter-v1"

#: Explicit prompt-profile identity for this transport.
#: Direct-API / interactive provider runs use the enhanced profile.
PROVIDER_PROMPT_PROFILE = prompt_shaper.PromptProfile.INTERACTIVE_PROVIDER_V2  # type: ignore[attr-defined]
PROMPT_PROFILE = PROVIDER_PROMPT_PROFILE

DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_LOGICAL_MODEL_CALLS = 64

#: Bounded provider response capture (aligned with the runtime's
#: 1 MiB model-response bound; the completion itself is bounded again).
MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024

#: Anthropic Messages requires an explicit output budget; bounded here
#: so a directive-sized completion always fits with margin.
_MESSAGES_MAX_TOKENS = 16384

class ProviderDirectApiError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "adapter_error") -> None:
        super().__init__(frozen.redact(message)[:400])
        self.kind = kind


def _load_child_auth_mode(provider: str) -> str:
    """Strict child-side authentication authority (never guesses).

    Loads the configured ``auth_mode`` for ``provider`` from the child
    process's own provider configuration view.  Any failure — missing,
    disabled, quarantined, corrupt, or unreadable configuration — raises
    a typed configuration error BEFORE any HTTP request.  There is no
    Bearer default: silently choosing an authentication mode the
    operator never configured would send credentials (or omit them)
    under false semantics.
    """
    try:
        from agentic_debugger.application.provider_connections import (
            provider_auth_mode,
        )
    except ImportError as exc:
        raise ProviderDirectApiError(
            f"provider {provider!r} authentication is not configured",
            kind="configuration",
        ) from exc
    try:
        return provider_auth_mode(provider)
    except Exception as exc:
        raise ProviderDirectApiError(
            f"provider {provider!r} authentication is not configured: {exc}",
            kind="configuration",
        ) from None


def _resolve_credential(provider: str, auth_mode: str) -> Optional[str]:
    """Resolve through the provider-owned runtime credential contract.

    That contract covers the private session hop, supported provider
    environment source, and consumable auth store without duplicating any
    variable names here.  The value is never logged or echoed.  No-auth
    providers resolve to ``None`` and send no credential header.
    """

    if auth_mode == "none":
        return None
    value = resolve_runtime_credential(provider)
    if value and value.strip():
        return value.strip()
    raise ProviderDirectApiError(
        "no usable credential source for the direct API route",
        kind="configuration",
    )


def read_request(stdin_stream: Any) -> Mapping[str, Any]:
    raw = stdin_stream.buffer.readline(frozen.MAX_PUBLIC_REQUEST_BYTES + 1)
    if not raw:
        raise ProviderDirectApiError("no request on stdin", kind="invalid_request")
    if len(raw) > frozen.MAX_PUBLIC_REQUEST_BYTES:
        raise ProviderDirectApiError(
            "request exceeds the public request ceiling", kind="request_too_large"
        )
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderDirectApiError(
            f"request is not valid JSON: {exc}", kind="invalid_request"
        ) from None
    if not isinstance(request, Mapping):
        raise ProviderDirectApiError(
            "request must be a JSON object", kind="invalid_request"
        )
    return request


def validate_logical_call_index(request: Mapping[str, Any], maximum: int) -> None:
    # Same zero-based product envelope as the accepted Ollama/OpenCode
    # provider adapters (first request 0, envelope 0..N-1).
    protocol = request.get("protocol")
    if not isinstance(protocol, Mapping):
        raise ProviderDirectApiError(
            "request is missing the protocol envelope", kind="invalid_request"
        )
    index = protocol.get("logical_model_call_index")
    if type(index) is not int or isinstance(index, bool) or not 0 <= index < maximum:
        if type(index) is int and not isinstance(index, bool) and index >= maximum:
            raise ProviderDirectApiError(
                f"logical model call {index} is outside the session envelope "
                f"(0..{maximum - 1})",
                kind="logical_call_limit",
            )
        raise ProviderDirectApiError(
            "logical_model_call_index must be an integer within the session envelope",
            kind="invalid_request",
        )


# -- protocol request shaping -------------------------------------------------


def build_provider_payload(
    protocol: str,
    model_id: str,
    *,
    system_prompt: str,
    user_prompt: str,
    chat_delivery: prompt_shaper.ChatCompletionsSystemDelivery = (
        prompt_shaper.ChatCompletionsSystemDelivery.INLINE_USER_V1
    ),
) -> Mapping[str, Any]:
    """The bounded provider request for one protocol family.

    Chat Completions uses the conservative OpenAI-compatible common denominator:
    ONE user-role message containing the system instruction + request-specific
    guidance + canonical public request, via prompt_shaper authority.
    Responses and Anthropic Messages retain their dedicated system channels.
    """

    if protocol == "chat_completions":
        return {
            "model": model_id,
            "messages": prompt_shaper.build_chat_completions_messages(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                delivery=chat_delivery,
            ),
            "stream": False,
        }
    if protocol == "responses":
        return {
            "model": model_id,
            "instructions": system_prompt,
            "input": user_prompt,
            "stream": False,
        }
    if protocol == "messages":
        return {
            "model": model_id,
            "max_tokens": _MESSAGES_MAX_TOKENS,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
    raise ProviderDirectApiError(
        f"unsupported protocol family: {protocol!r}", kind="configuration"
    )


# -- strict bounded response parsing ------------------------------------------


def _bounded_text(value: Any) -> Optional[str]:
    if type(value) is not str:
        return None
    return value


def _extract_chat_completions(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if type(choices) is not list or not choices:
        raise ProviderDirectApiError(
            "completion response has no choices", kind="invalid_completion"
        )
    first = choices[0]
    if not isinstance(first, Mapping):
        raise ProviderDirectApiError(
            "completion choice is not an object", kind="invalid_completion"
        )
    message = first.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        # Supported common shapes: plain string, or a list of text parts
        # (e.g. [{"type": "text", "text": "..."}]).  Non-text thinking /
        # metadata blocks are ignored; refusal/empty content fails closed.
        if type(content) is str and content.strip():
            return content
        if type(content) is list:
            parts: list[str] = []
            for piece in content:
                if not isinstance(piece, Mapping):
                    continue
                if piece.get("type") not in ("text", "output_text"):
                    continue
                piece_text = _bounded_text(piece.get("text"))
                if piece_text:
                    parts.append(piece_text)
            if parts:
                return "".join(parts)
    text = _bounded_text(first.get("text"))
    if text and text.strip():
        return text
    raise ProviderDirectApiError(
        "completion response has no message content", kind="invalid_completion"
    )


def _extract_responses(payload: Mapping[str, Any]) -> str:
    text = _bounded_text(payload.get("output_text"))
    if text:
        return text
    output = payload.get("output")
    if type(output) is list:
        parts: list[str] = []
        for item in output:
            if not isinstance(item, Mapping) or item.get("type") != "message":
                continue
            content = item.get("content")
            if type(content) is not list:
                continue
            for piece in content:
                if (
                    isinstance(piece, Mapping)
                    and piece.get("type") in ("output_text", "text")
                ):
                    piece_text = _bounded_text(piece.get("text"))
                    if piece_text:
                        parts.append(piece_text)
        if parts:
            return "".join(parts)
    raise ProviderDirectApiError(
        "responses payload has no message output text", kind="invalid_completion"
    )


def _extract_messages(payload: Mapping[str, Any]) -> str:
    content = payload.get("content")
    if type(content) is list:
        parts: list[str] = []
        for piece in content:
            # Ignore non-text thinking/metadata blocks; only text counts.
            if isinstance(piece, Mapping) and piece.get("type") == "text":
                piece_text = _bounded_text(piece.get("text"))
                if piece_text:
                    parts.append(piece_text)
        if parts:
            return "".join(parts)
    text = _bounded_text(content)
    if text and text.strip():
        return text
    raise ProviderDirectApiError(
        "messages payload has no text content", kind="invalid_completion"
    )


_EXTRACTORS = {
    "chat_completions": _extract_chat_completions,
    "responses": _extract_responses,
    "messages": _extract_messages,
}


def extract_completion(protocol: str, payload: Mapping[str, Any]) -> str:
    extractor = _EXTRACTORS.get(protocol)
    if extractor is None:
        raise ProviderDirectApiError(
            f"unsupported protocol family: {protocol!r}", kind="configuration"
        )
    return extractor(payload)


def extract_usage(protocol: str, payload: Mapping[str, Any]) -> Optional[dict]:
    """Provider-reported usage copied verbatim when present.

    Absence of usage is normal for some routes; nothing is fabricated.
    """

    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    total = usage.get("total_tokens")
    result: dict[str, Any] = {}
    for name, value in (
        ("prompt_tokens", prompt),
        ("completion_tokens", completion),
        ("total_tokens", total),
    ):
        if type(value) is int and not isinstance(value, bool) and value >= 0:
            result[name] = value
    return result or None


# -- one inference ------------------------------------------------------------


def perform_inference(
    provider: str,
    model_id: str,
    protocol: str,
    *,
    system_prompt: str,
    user_prompt: str,
    timeout_seconds: float,
    engine: Optional[str] = None,
    base_url: Optional[str] = None,
    auth_mode: Optional[str] = None,
) -> tuple[str, Optional[dict]]:
    """Exactly ONE provider inference for one transport request.

    ``auth_mode`` carries the parent-verified authentication contract
    (``run_adapter`` agreement-checks it against child configuration
    before calling here).  When omitted, the child resolves its own
    configured mode strictly — a missing/unreadable configuration fails
    closed instead of guessing Bearer.
    """

    from agentic_debugger.application.provider_connections import (
        provider_base_url,
        provider_tls_signature_blocked,
    )

    verified_auth = auth_mode if auth_mode is not None else _load_child_auth_mode(provider)
    credential = _resolve_credential(provider, verified_auth)
    path = inference_path_for(provider, protocol)
    payload = build_provider_payload(
        protocol,
        provider_api_model_id(provider, model_id),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    # Canonical endpoint authority: the explicit --base-url passed by the
    # live config (which equals the configured provider Base URL) wins;
    # otherwise the configured Base URL is authoritative.  Credentials
    # travel only via headers/environment, never argv.
    endpoint = (base_url or provider_base_url(provider)).rstrip("/")
    tls_blocked = False
    try:
        tls_blocked = provider_tls_signature_blocked(provider)
    except Exception:
        pass
    try:
        response = request_json(
            "POST",
            endpoint + path,
            credential=credential,
            json_payload=payload,
            timeout_seconds=timeout_seconds,
            max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
            engine=engine,
            tls_signature_blocked=tls_blocked,
            auth_mode=verified_auth,
        )
    except ProviderHttpError as exc:
        kind = {
            "timeout": "timeout",
            "response_too_large": "response_too_large",
            "invalid_response": "invalid_response",
            "http_status": "http_error",
            "tls_blocked": "http_error",
            "connection_error": "http_error",
            "engine_unavailable": "http_error",
            "invalid_url": "configuration",
            "invalid_request": "invalid_request",
        }.get(exc.kind, "http_error")
        sanitized_msg = sanitize_text(str(exc), active_credential=credential)
        raise ProviderDirectApiError(sanitized_msg, kind=kind) from None
    text = extract_completion(protocol, response)
    if not text or not text.strip():
        raise ProviderDirectApiError(
            "provider returned an empty completion", kind="invalid_completion"
        )
    if len(text.encode("utf-8")) > frozen.MAX_RAW_RESPONSE_BYTES:
        raise ProviderDirectApiError(
            "provider completion exceeds the response bound", kind="response_too_large"
        )
    # Bounded sanity check only: the completion must contain at least one
    # JSON object so the app-side resolver has directive material to
    # normalize (same contract as the accepted CLI adapters).
    try:
        candidate_found = bool(frozen._json_objects(text))
    except Exception:
        candidate_found = False
    if not candidate_found:
        raise ProviderDirectApiError(
            "completion does not contain a JSON directive object",
            kind="invalid_directive",
        )
    return text, extract_usage(protocol, response)


def run_adapter(
    stdin_stream: Any = sys.stdin,
    stdout_stream: Any = sys.stdout,
    *,
    provider: str,
    model: str,
    protocol: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_logical_calls: int = DEFAULT_MAX_LOGICAL_MODEL_CALLS,
    engine: Optional[str] = None,
    base_url: Optional[str] = None,
    auth_mode: Optional[str] = None,
) -> int:
    """Verify parent/child configuration identity, then run one inference.

    Authentication authority is explicit and fail-closed across the
    parent → worker → transport → child boundary: the parent passes the
    non-secret ``auth_mode`` (and endpoint) it resolved, and the child
    agreement-checks both against its own configuration view BEFORE any
    HTTP request.  Missing/corrupt/mismatched child metadata fails with
    a typed configuration error and zero network attempts.  Credentials
    never travel via argv.
    """
    if protocol not in ("chat_completions", "responses", "messages"):
        raise ProviderDirectApiError(
            f"unknown protocol: {protocol!r}", kind="configuration"
        )
    try:
        from agentic_debugger.application.provider_connections import (
            effective_model_protocol,
            get_provider_config,
        )
    except ImportError as exc:
        raise ProviderDirectApiError(
            "provider configuration is unavailable in this child process",
            kind="configuration",
        ) from exc
    try:
        child_cfg = get_provider_config(provider)
    except Exception as exc:
        raise ProviderDirectApiError(
            f"provider {provider!r} configuration could not be verified: {exc}",
            kind="configuration",
        ) from None
    if child_cfg is None or not child_cfg.enabled:
        raise ProviderDirectApiError(
            f"unknown direct-API provider: {provider!r}", kind="configuration"
        )
    if auth_mode is not None and auth_mode != child_cfg.auth_mode:
        raise ProviderDirectApiError(
            "parent/child authentication contract disagrees for provider "
            f"{provider!r}",
            kind="configuration",
        )
    verified_auth = child_cfg.auth_mode
    if base_url is not None and base_url.rstrip("/") != child_cfg.base_url.rstrip("/"):
        raise ProviderDirectApiError(
            "parent/child endpoint contract disagrees for provider "
            f"{provider!r}",
            kind="configuration",
        )
    verified_endpoint = child_cfg.base_url
    try:
        effective = effective_model_protocol(provider, model)
    except Exception as exc:
        raise ProviderDirectApiError(
            f"provider {provider!r} model {model!r} is not executable: {exc}",
            kind="configuration",
        ) from None
    if protocol != effective:
        raise ProviderDirectApiError(
            "declared protocol does not match the provider-resolved protocol "
            "for this model",
            kind="configuration",
        )
    request = read_request(stdin_stream)
    validate_logical_call_index(request, max_logical_calls)
    # The provider-neutral protocol-1.3 shaping authority: the same mature
    # system-role instruction and request-specific legal-representation
    # guidance every model transport consumes.  The direct-API route uses
    # the interactive enhanced profile (diagnosis guidance, bounded repairs).
    # This is intentionally NOT the frozen scientific treatment.
    try:
        system_message, user_message = prompt_shaper.build_chat_messages(
            request,
            prompt_profile=prompt_shaper.PromptProfile.INTERACTIVE_PROVIDER_V2,
            max_request_bytes=frozen.MAX_PUBLIC_REQUEST_BYTES,
        )
    except prompt_shaper.ProtocolPromptError as exc:
        raise ProviderDirectApiError(str(exc), kind=exc.kind) from None
    text, usage = perform_inference(
        provider,
        model,
        protocol,
        system_prompt=system_message["content"],
        user_prompt=user_message["content"],
        timeout_seconds=timeout_seconds,
        engine=engine,
        base_url=verified_endpoint,
        auth_mode=verified_auth,
    )
    response: dict[str, Any] = {
        "provider_completion_schema_version": PROVIDER_COMPLETION_SCHEMA_VERSION,
        "directive_content": text,
        "tool_version": TOOL_VERSION,
    }
    if isinstance(usage, dict):
        response["usage"] = usage
    stdout_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
    stdout_stream.flush()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provider direct-API protocol-1.3 command adapter"
    )
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--protocol",
        required=True,
        choices=("chat_completions", "responses", "messages"),
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--max-logical-model-calls", type=int, default=DEFAULT_MAX_LOGICAL_MODEL_CALLS
    )
    parser.add_argument("--engine", default=None, choices=("stdlib", "curl"))
    parser.add_argument(
        "--auth-mode",
        default=None,
        choices=("bearer", "anthropic", "none"),
        help=(
            "Parent-verified authentication contract (non-secret); the child "
            "agreement-checks it against its own configuration before any request."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "Canonical configured provider endpoint (passed explicitly by the "
            "live config so parent, worker, and adapter agree byte-for-byte)."
        ),
    )
    args = parser.parse_args()
    try:
        raise SystemExit(
            run_adapter(
                sys.stdin,
                sys.stdout,
                provider=args.provider,
                model=args.model,
                protocol=args.protocol,
                timeout_seconds=args.timeout,
                max_logical_calls=args.max_logical_model_calls,
                engine=args.engine,
                base_url=args.base_url,
                auth_mode=args.auth_mode,
            )
        )
    except ProviderDirectApiError as exc:
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
