from __future__ import annotations

import json
from typing import Any, Dict, Optional, TextIO

from agentic_debugger import SchemaValidationError
from agentic_debugger.events.schema import RunEvent, validate_json_compatible


class JsonlEventLogger:
    def __init__(
        self,
        run_id: str,
        task_id: str,
        path: Optional[str] = None,
        stream: Optional[TextIO] = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise SchemaValidationError("run_id must be a non-empty string")
        if not isinstance(task_id, str) or not task_id:
            raise SchemaValidationError("task_id must be a non-empty string")
        if path is None and stream is None:
            raise SchemaValidationError(
                "either path or stream must be provided"
            )
        if path is not None and stream is not None:
            raise SchemaValidationError(
                "provide either path or stream, not both"
            )

        self._run_id = run_id
        self._task_id = task_id
        self._closed = False
        self._last_sequence: Optional[int] = None

        if path is not None:
            self._stream: TextIO = open(path, "w", encoding="utf-8")
            self._owns_stream = True
        else:
            self._stream = stream  # type: ignore[assignment]
            self._owns_stream = False

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def closed(self) -> bool:
        return self._closed

    def append(self, event: RunEvent) -> None:
        if self._closed:
            raise SchemaValidationError("logger is closed")

        if event.run_id != self._run_id:
            raise SchemaValidationError(
                f"event run_id {event.run_id!r} does not match "
                f"logger run_id {self._run_id!r}"
            )
        if event.task_id != self._task_id:
            raise SchemaValidationError(
                f"event task_id {event.task_id!r} does not match "
                f"logger task_id {self._task_id!r}"
            )

        if event.sequence < 0:
            raise SchemaValidationError(
                f"sequence must be non-negative, got {event.sequence}"
            )

        if self._last_sequence is None:
            if event.sequence != 0:
                raise SchemaValidationError(
                    f"first event must have sequence 0, got {event.sequence}"
                )
        else:
            expected = self._last_sequence + 1
            if event.sequence <= self._last_sequence:
                raise SchemaValidationError(
                    f"duplicate or out-of-order sequence: "
                    f"last={self._last_sequence}, got={event.sequence}"
                )
            if event.sequence != expected:
                raise SchemaValidationError(
                    f"non-contiguous sequence: expected {expected}, "
                    f"got {event.sequence}"
                )

        mapping = event.to_mapping()
        validate_json_compatible(mapping, "event")

        line = json.dumps(
            mapping,
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        self._stream.write(line + "\n")
        self._last_sequence = event.sequence

    def flush(self) -> None:
        if self._closed:
            return
        self._stream.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_stream:
            self._stream.close()
