"""Local fake provider HTTP server for direct-API transport tests.

Deterministic, bounded, loopback-only replacement for the real
subscription endpoints.  No provider contact and no generation spend
happens in automated tests: the fake server records requests (method,
path, Authorization header, body bytes) and replays scripted responses.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple


class FakeProviderServer:
    """One bounded loopback HTTP server with scripted responses."""

    def __init__(
        self,
        responder: Optional[Callable[[Dict[str, Any]], Tuple[int, Any]]] = None,
    ) -> None:
        """``responder`` receives one request record and returns
        ``(status, payload)`` where payload is a Mapping (JSON body),
        a str (raw body), or bytes."""

        self.requests: List[Dict[str, Any]] = []
        self._responder = responder or self._default_responder
        self._lock = threading.Lock()

    # -- request recording ----------------------------------------------------

    @staticmethod
    def _default_responder(request: Dict[str, Any]) -> Tuple[int, Any]:
        return 404, {"error": "not scripted"}

    def record(self, request: Dict[str, Any]) -> Tuple[int, bytes, str]:
        with self._lock:
            self.requests.append(request)
        status, payload = self._responder(request)
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
            content_type = "text/plain"
        else:
            body = payload
            content_type = "application/json"
        return status, body, content_type

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self.requests)

    # -- lifecycle ------------------------------------------------------------

    def __enter__(self) -> "FakeProviderServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                status, payload, content_type = outer.record(
                    {
                        "method": self.command,
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                        "body": body,
                    }
                )
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _handle
            do_POST = _handle

            def log_message(self, format: str, *args: Any) -> None:  # silence
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        self._server = server
        self.port = server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def scripted_chat_completion(content: str) -> Dict[str, Any]:
    """One OpenAI chat-completions response body."""

    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "fake-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def scripted_responses_output(content: str) -> Dict[str, Any]:
    """One OpenAI Responses response body."""

    return {
        "id": "resp-fake",
        "object": "response",
        "output": [
            {
                "type": "message",
                "id": "msg-fake",
                "content": [{"type": "output_text", "text": content}],
            }
        ],
        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    }


def scripted_messages_output(content: str) -> Dict[str, Any]:
    """One Anthropic Messages response body."""

    return {
        "id": "msg-fake",
        "model": "fake-model",
        "content": [{"type": "text", "text": content}],
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def catalog_payload(model_ids: List[str]) -> Dict[str, Any]:
    """One OpenAI-shaped /models list body."""

    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "created": 1788120760, "owned_by": "fake"}
            for model_id in model_ids
        ],
    }
