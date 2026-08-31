"""Unit gates for the bounded provider HTTP boundary.

Covers URL validation (explicit HTTPS outside loopback), bounded
response capture, typed sanitized failures, credential-safe error text,
and the exactly-one-request (zero hidden retry) contract for both
engines against a local fake provider server.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_provider_server import FakeProviderServer  # noqa: E402
from agentic_debugger.application.provider_http import (  # noqa: E402
    ProviderHttpError,
    curl_executable,
    describe_url,
    request_json,
)


class TestUrlValidation:
    def test_plain_http_outside_loopback_fails_closed(self) -> None:
        with pytest.raises(ProviderHttpError) as excinfo:
            request_json("GET", "http://example.com/models", engine="stdlib")
        assert excinfo.value.kind == "invalid_url"

    def test_embedded_url_credentials_rejected(self) -> None:
        with pytest.raises(ProviderHttpError) as excinfo:
            request_json(
                "GET", "https://user:pass@example.com/models", engine="stdlib"
            )
        assert excinfo.value.kind == "invalid_url"

    def test_unknown_method_rejected(self) -> None:
        with pytest.raises(ProviderHttpError) as excinfo:
            request_json("PUT", "https://example.com/v1/models", engine="stdlib")
        assert excinfo.value.kind == "invalid_request"

    def test_url_control_characters_rejected(self) -> None:
        with pytest.raises(ProviderHttpError) as excinfo:
            request_json(
                "GET", "https://example.com/models\nheader:value", engine="stdlib"
            )
        assert excinfo.value.kind == "invalid_url"

    @pytest.mark.parametrize("bound", [0, -1, True, 32 * 1024 * 1024])
    def test_response_capture_bound_is_validated(self, bound) -> None:
        with pytest.raises(ProviderHttpError) as excinfo:
            request_json(
                "GET",
                "https://example.com/models",
                engine="stdlib",
                max_response_bytes=bound,
            )
        assert excinfo.value.kind == "invalid_request"

    def test_request_payload_is_bounded_before_network(self) -> None:
        with pytest.raises(ProviderHttpError) as excinfo:
            request_json(
                "POST",
                "https://example.com/chat/completions",
                engine="stdlib",
                json_payload={"input": "x" * (4 * 1024 * 1024)},
            )
        assert excinfo.value.kind == "invalid_request"

    def test_credential_control_characters_rejected(self) -> None:
        with pytest.raises(ProviderHttpError) as excinfo:
            request_json(
                "GET",
                "https://example.com/models",
                engine="curl",
                credential="secret\nheader = injected",
            )
        assert excinfo.value.kind == "invalid_request"

    def test_describe_url_is_credential_free(self) -> None:
        identity = describe_url("https://opencode.ai/zen/go/v1/models")
        assert identity == "https://opencode.ai/zen/go/v1/models"

    def test_loopback_http_allowed(self) -> None:
        with FakeProviderServer(
            lambda request: (200, {"object": "list", "data": []})
        ) as server:
            payload = request_json(
                "GET",
                server.base_url + "/models",
                engine="stdlib",
                timeout_seconds=5,
            )
            assert isinstance(payload, dict)


class TestStdlibEngine:
    def test_get_parses_json_and_sends_bearer(self) -> None:
        with FakeProviderServer(
            lambda request: (200, {"object": "list", "data": []})
        ) as server:
            payload = request_json(
                "GET",
                server.base_url + "/models",
                credential="secret-value-123",
                engine="stdlib",
                timeout_seconds=5,
            )
            assert payload["object"] == "list"
            assert server.requests[0]["authorization"] == "Bearer secret-value-123"

    def test_post_serializes_json_payload(self) -> None:
        with FakeProviderServer(
            lambda request: (200, {"ok": True})
        ) as server:
            request_json(
                "POST",
                server.base_url + "/chat/completions",
                json_payload={"model": "m", "stream": False},
                engine="stdlib",
                timeout_seconds=5,
            )
            recorded = server.requests[0]
            assert recorded["method"] == "POST"
            assert b'"stream":false' in recorded["body"].replace(b" ", b"")

    def test_http_error_is_typed_and_bounded(self) -> None:
        with FakeProviderServer(
            lambda request: (500, {"error": "provider exploded " * 50})
        ) as server:
            with pytest.raises(ProviderHttpError) as excinfo:
                request_json(
                    "GET", server.base_url + "/models", engine="stdlib", timeout_seconds=5
                )
        assert excinfo.value.kind == "http_status"
        assert excinfo.value.status == 500
        assert len(str(excinfo.value)) <= 220

    def test_oversized_response_fails_typed(self) -> None:
        big = "x" * 4096
        with FakeProviderServer(lambda request: (200, {"blob": big})) as server:
            with pytest.raises(ProviderHttpError) as excinfo:
                request_json(
                    "GET",
                    server.base_url + "/models",
                    engine="stdlib",
                    timeout_seconds=5,
                    max_response_bytes=1024,
                )
        assert excinfo.value.kind == "response_too_large"

    def test_invalid_json_fails_typed(self) -> None:
        with FakeProviderServer(lambda request: (200, "not json at all")) as server:
            with pytest.raises(ProviderHttpError) as excinfo:
                request_json(
                    "GET", server.base_url + "/models", engine="stdlib", timeout_seconds=5
                )
        assert excinfo.value.kind == "invalid_response"

    def test_non_object_json_fails_typed(self) -> None:
        with FakeProviderServer(lambda request: (200, [1, 2, 3])) as server:
            with pytest.raises(ProviderHttpError) as excinfo:
                request_json(
                    "GET", server.base_url + "/models", engine="stdlib", timeout_seconds=5
                )
        assert excinfo.value.kind == "invalid_response"

    def test_timeout_is_typed_and_bounded(self) -> None:
        release = threading.Event()

        def slow_responder(request):
            release.wait(timeout=5)
            return 200, {}

        with FakeProviderServer(slow_responder) as server:
            with pytest.raises(ProviderHttpError) as excinfo:
                request_json(
                    "GET",
                    server.base_url + "/models",
                    engine="stdlib",
                    timeout_seconds=0.3,
                )
        assert excinfo.value.kind == "timeout"

    def test_exactly_one_request_no_retry(self) -> None:
        with FakeProviderServer(lambda request: (500, {"error": "down"})) as server:
            with pytest.raises(ProviderHttpError):
                request_json(
                    "GET", server.base_url + "/models", engine="stdlib", timeout_seconds=5
                )
            assert server.request_count == 1


class TestCurlEngine:
    @pytest.mark.skipif(curl_executable() is None, reason="OS curl client not installed")
    def test_curl_get_parses_json(self) -> None:
        with FakeProviderServer(
            lambda request: (200, {"object": "list", "data": [{"id": "glm-5.2"}]})
        ) as server:
            payload = request_json(
                "GET",
                server.base_url + "/models",
                credential="curl-secret-value",
                engine="curl",
                timeout_seconds=10,
            )
            assert payload["data"][0]["id"] == "glm-5.2"
            assert server.requests[0]["authorization"] == "Bearer curl-secret-value"

    @pytest.mark.skipif(curl_executable() is None, reason="OS curl client not installed")
    def test_curl_post_body_and_single_request(self) -> None:
        with FakeProviderServer(lambda request: (200, {"ok": True})) as server:
            request_json(
                "POST",
                server.base_url + "/chat/completions",
                json_payload={"model": "m", "prompt": 'quote " and \\ backslash'},
                engine="curl",
                timeout_seconds=10,
            )
            assert server.request_count == 1
            body = json.loads(server.requests[0]["body"].decode("utf-8"))
            assert body["prompt"] == 'quote " and \\ backslash'

    @pytest.mark.skipif(curl_executable() is None, reason="OS curl client not installed")
    def test_curl_http_error_typed(self) -> None:
        with FakeProviderServer(lambda request: (503, {"error": "unavailable"})) as server:
            with pytest.raises(ProviderHttpError) as excinfo:
                request_json(
                    "GET", server.base_url + "/models", engine="curl", timeout_seconds=10
                )
        assert excinfo.value.kind == "http_status"
        assert excinfo.value.status == 503

    @pytest.mark.skipif(curl_executable() is None, reason="OS curl client not installed")
    def test_curl_oversized_response_typed(self) -> None:
        with FakeProviderServer(lambda request: (200, {"blob": "y" * 8192})) as server:
            with pytest.raises(ProviderHttpError) as excinfo:
                request_json(
                    "GET",
                    server.base_url + "/models",
                    engine="curl",
                    timeout_seconds=10,
                    max_response_bytes=1024,
                )
        assert excinfo.value.kind == "response_too_large"


class TestErrorSanitization:
    def test_credential_shaped_text_redacted(self) -> None:
        from agentic_debugger.application.provider_http import sanitize_text

        text = "Bearer sk-abcdefghijklmnop1234 leaked api_key=supersecret123"
        sanitized = sanitize_text(text)
        assert "sk-abcdefghijklmnop1234" not in sanitized
        assert "supersecret123" not in sanitized
        assert "<redacted>" in sanitized

    def test_error_message_bounded(self) -> None:
        from agentic_debugger.application.provider_http import sanitize_text

        assert len(sanitize_text("z" * 10_000)) <= 210

    def test_no_credential_in_http_error(self) -> None:
        with FakeProviderServer(lambda request: (401, {"error": "bad key"})) as server:
            with pytest.raises(ProviderHttpError) as excinfo:
                request_json(
                    "GET",
                    server.base_url + "/models",
                    credential="super-secret-key-value",
                    engine="stdlib",
                    timeout_seconds=5,
                )
        assert "super-secret-key-value" not in str(excinfo.value)
