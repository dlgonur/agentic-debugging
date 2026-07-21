from __future__ import annotations

import io
import os
import pdb
import sys
import traceback

from agentic_debugger.runtime.pdb_protocol import (
    PROTOCOL_VERSION,
    MAX_LINE_LENGTH,
    PdbRequest,
    PdbResponse,
    serialize_response,
    deserialize_request,
)
from agentic_debugger.runtime.exceptions import PdbProtocolError


class PdbWorker:
    def __init__(self) -> None:
        self._pdb_stdin = io.StringIO()
        self._pdb_stdout = io.StringIO()
        self._pdb = pdb.Pdb(
            readrc=False,
            stdin=self._pdb_stdin,
            stdout=self._pdb_stdout,
        )
        self._running = True

    def run(self) -> None:
        while self._running:
            try:
                data = sys.stdin.buffer.readline(MAX_LINE_LENGTH + 1)
            except OSError:
                self._diag("stdin read error")
                break

            if not data:
                break

            if len(data) > MAX_LINE_LENGTH:
                self._send_error(
                    request_id=0,
                    error=(
                        f"Input line exceeds maximum length "
                        f"({len(data)} > {MAX_LINE_LENGTH} bytes)"
                    ),
                )
                self._running = False
                break

            try:
                request = deserialize_request(data)
            except PdbProtocolError as e:
                self._send_error(
                    request_id=0,
                    error=str(e),
                )
                continue

            try:
                self._handle(request)
            except Exception as e:
                self._diag(f"Unhandled error: {traceback.format_exc()}")
                self._send_error(
                    request_id=request.request_id,
                    error=f"Internal worker error: {e}",
                )

    def _handle(self, request: PdbRequest) -> None:
        if request.protocol_version != PROTOCOL_VERSION:
            self._send_error(
                request_id=request.request_id,
                error=(
                    f"Unsupported protocol version: "
                    f"{request.protocol_version}, "
                    f"expected {PROTOCOL_VERSION}"
                ),
            )
            return
        op = request.operation
        if op == "hello":
            self._handle_hello(request)
        elif op == "ping":
            self._handle_ping(request)
        elif op == "shutdown":
            self._handle_shutdown(request)
        else:
            self._send_error(
                request_id=request.request_id,
                error=f"Unsupported operation: {op!r}",
            )

    def _handle_hello(self, request: PdbRequest) -> None:
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            success=True,
            result={
                "pid": os.getpid(),
                "protocol_version": PROTOCOL_VERSION,
            },
            error="",
        )
        self._send_response(response)

    def _handle_ping(self, request: PdbRequest) -> None:
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            success=True,
            result={"status": "ok", "pdb_created": True},
            error="",
        )
        self._send_response(response)

    def _handle_shutdown(self, request: PdbRequest) -> None:
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            success=True,
            result={"shutdown": True},
            error="",
        )
        self._send_response(response)
        self._running = False

    def _send_response(self, response: PdbResponse) -> None:
        data = serialize_response(response)
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except OSError:
            self._running = False

    def _send_error(
        self, request_id: int, error: str
    ) -> None:
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request_id,
            success=False,
            result={},
            error=error,
        )
        self._send_response(response)

    @staticmethod
    def _diag(message: str) -> None:
        try:
            print(f"[pdb_worker] {message}", file=sys.stderr)
        except OSError:
            pass


def main() -> None:
    worker = PdbWorker()
    worker.run()


if __name__ == "__main__":
    main()
