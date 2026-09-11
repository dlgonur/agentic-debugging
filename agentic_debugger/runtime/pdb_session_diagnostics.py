"""Bounded diagnostics authority for the PDB session.

Owns the head/marker/tail truncation policy for worker stderr capture.
The session remains the sole owner of the diagnostics *state* (the
accumulator instance and its lock); this module owns the truncation
*behavior*.

Dependency direction: depends only on ``pdb_session_limits``.
"""

from __future__ import annotations

from typing import List

from agentic_debugger.runtime.pdb_session_limits import (
    _DEFAULT_MAX_DIAGNOSTICS,
    _TRUNCATION_MARKER,
)


class _BoundedDiagnostics:
    def __init__(self, max_chars: int = _DEFAULT_MAX_DIAGNOSTICS) -> None:
        self._max = max_chars
        self._half = max_chars // 2 if max_chars > 1 else 0
        self._parts: List[str] = []
        self._total = 0
        self._overflowed = False
        self._head: List[str] = []
        self._head_len = 0
        self._tail: List[str] = []
        self._tail_len = 0

    def add(self, text: str) -> None:
        if not text:
            return
        n = len(text)
        if not self._overflowed:
            self._parts.append(text)
            self._total += n
            if self._total > self._max:
                self._flush_overflow()
        else:
            self._tail.append(text)
            self._tail_len += n
            self._trim_tail()

    def _flush_overflow(self) -> None:
        full = "".join(self._parts)
        self._parts = []
        self._head.append(full[: self._half])
        self._head_len = self._half
        rest = full[self._half:]
        if rest:
            if len(rest) > self._half:
                rest = rest[-self._half:]
            self._tail.append(rest)
            self._tail_len = len(rest)
        self._overflowed = True

    def _trim_tail(self) -> None:
        while self._tail_len > self._half:
            oldest = self._tail[0]
            excess = self._tail_len - self._half
            if len(oldest) <= excess:
                self._tail.pop(0)
                self._tail_len -= len(oldest)
            else:
                self._tail[0] = oldest[excess:]
                self._tail_len -= excess

    def getvalue(self) -> str:
        return self._build()

    def _build(self) -> str:
        if not self._overflowed:
            text = "".join(self._parts)
            if len(text) > self._max:
                text = text[: self._max]
            return text

        head = "".join(self._head)
        tail = "".join(self._tail)
        marker = _TRUNCATION_MARKER
        mlen = len(marker)

        if mlen >= self._max:
            return head[: self._max]

        half = self._half
        if half <= 0:
            budget = self._max - mlen
            if budget <= 0:
                return head[: self._max]
            return head[:budget] + marker

        max_tail = self._max - half - mlen
        if max_tail <= 0:
            budget = self._max - mlen
            if budget <= 0:
                return head[: self._max]
            return head[:budget] + marker

        if len(head) > half:
            head = head[:half]
        if len(tail) > max_tail:
            tail_start = len(tail) - max_tail
            if tail_start > 0:
                tail = tail[tail_start:]
            else:
                tail = tail[:max_tail]
        return head + marker + tail
