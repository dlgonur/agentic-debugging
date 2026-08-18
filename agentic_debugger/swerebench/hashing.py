"""Canonical SHA-256 helpers for frozen SWE-rebench artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def sha256_canonical_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def require_sha256(path: Path, expected: str, *, label: str) -> str:
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(
            f"{label} SHA-256 mismatch for {path.name}: "
            f"expected {expected}, got {actual}"
        )
    return actual
