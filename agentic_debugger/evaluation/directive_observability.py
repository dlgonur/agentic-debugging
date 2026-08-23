"""Provider-safe evidence for rejected final directive content.

This module deliberately does not parse or validate directives.  The live
adapter owns that decision; this module only serializes already-typed parser
outcomes for bounded observability and verifies persisted representation
identity.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

DIRECTIVE_OBSERVABILITY_SCHEMA_VERSION = "directive-observability-v1"
MAX_RECORDED_CONTENT_BYTES = 4096
MAX_RECORDED_REASON_CHARS = 200

_SECRET = re.compile(
    r"(?i)(?:bearer\s+\S+|basic\s+\S+|(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|token|private[_-]?key)\s*[:=]\s*\S+)"
)


def _utf8_prefix(value: str, limit: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value, False
    clipped = raw[: max(0, limit - 3)]
    while True:
        try:
            return clipped.decode("utf-8") + "...", True
        except UnicodeDecodeError:
            clipped = clipped[:-1]


def _safe_reason(value: Any) -> str:
    text = str(value or "")
    text, _ = _utf8_prefix(text, MAX_RECORDED_REASON_CHARS)
    return text


def serialize_rejection_evidence(
    *,
    stage: str,
    category: str,
    reason_code: str,
    reason: str,
    content: str | None,
    classification_sufficient: bool | None = None,
) -> dict[str, Any]:
    """Serialize typed rejection data without retaining reasoning text."""

    valid_content = content if type(content) is str else None
    secret_shaped = bool(valid_content is not None and _SECRET.search(valid_content))
    content_hash = (
        hashlib.sha256(valid_content.encode("utf-8")).hexdigest()
        if valid_content is not None and not secret_shaped
        else None
    )
    if valid_content is None:
        representation = None
        content_bytes = 0
        sufficient = False
    else:
        sanitized = _SECRET.sub("<redacted>", valid_content)
        representation_text, truncated = _utf8_prefix(sanitized, MAX_RECORDED_CONTENT_BYTES)
        representation_bytes = representation_text.encode("utf-8")
        representation = {
            "encoding": "utf-8",
            "text": representation_text,
            "byte_length": len(representation_bytes),
            "sha256": hashlib.sha256(representation_bytes).hexdigest(),
            "redacted": secret_shaped,
            "truncated": truncated,
        }
        content_bytes = len(valid_content.encode("utf-8"))
        sufficient = (
            classification_sufficient
            if type(classification_sufficient) is bool
            else not secret_shaped and not truncated
        )
    return {
        "schema_version": DIRECTIVE_OBSERVABILITY_SCHEMA_VERSION,
        "stage": str(stage),
        "category": str(category),
        "reason_code": str(reason_code),
        "reason": _safe_reason(reason),
        "content_available": valid_content is not None,
        "content_byte_length": content_bytes,
        "content_sha256": content_hash,
        "raw_hash_withheld": secret_shaped,
        "content_representation": representation,
        "evidence_sufficiency": "sufficient" if sufficient else "insufficient",
    }


def validate_rejection_evidence(value: Any) -> bool:
    """Validate shape and persisted identity, never directive semantics."""

    if not isinstance(value, Mapping):
        return False
    required = {
        "schema_version", "stage", "category", "reason_code", "reason",
        "content_available", "content_byte_length", "content_sha256",
        "raw_hash_withheld", "content_representation", "evidence_sufficiency",
    }
    if set(value) != required or value.get("schema_version") != DIRECTIVE_OBSERVABILITY_SCHEMA_VERSION:
        return False
    if any(type(value.get(key)) is not str for key in ("stage", "category", "reason_code", "reason")):
        return False
    if type(value.get("content_available")) is not bool or type(value.get("raw_hash_withheld")) is not bool:
        return False
    if type(value.get("content_byte_length")) is not int or value["content_byte_length"] < 0:
        return False
    if value.get("content_sha256") is not None and (
        type(value.get("content_sha256")) is not str or len(value["content_sha256"]) != 64
    ):
        return False
    if value.get("evidence_sufficiency") not in {"sufficient", "insufficient"}:
        return False
    representation = value.get("content_representation")
    if value["content_available"] != (representation is not None):
        return False
    if representation is not None:
        required_representation = {"encoding", "text", "sha256", "redacted", "truncated"}
        allowed_representation = required_representation | {"byte_length"}
        if not isinstance(representation, Mapping) or not set(representation).issubset(allowed_representation) or not required_representation.issubset(set(representation)):
            return False
        if representation.get("encoding") != "utf-8" or type(representation.get("text")) is not str:
            return False
        if type(representation.get("redacted")) is not bool or type(representation.get("truncated")) is not bool:
            return False
        if type(representation.get("sha256")) is not str or len(representation["sha256"]) != 64:
            return False
        if representation["redacted"]:
            if value["raw_hash_withheld"] is not True or value["content_sha256"] is not None:
                return False
        elif value["raw_hash_withheld"] or type(value["content_sha256"]) is not str or len(value["content_sha256"]) != 64:
            return False
        representation_bytes = representation["text"].encode("utf-8")
        if representation["sha256"] != hashlib.sha256(representation_bytes).hexdigest():
            return False
        if "byte_length" in representation and (
            type(representation["byte_length"]) is not int
            or representation["byte_length"] != len(representation_bytes)
        ):
            return False
        if not representation["redacted"] and not representation["truncated"]:
            if value["content_byte_length"] != len(representation_bytes):
                return False
            if value["content_sha256"] != representation["sha256"]:
                return False
            if value["evidence_sufficiency"] != "sufficient":
                return False
        elif value["evidence_sufficiency"] == "sufficient":
            if not value["content_available"] or not representation["text"]:
                return False
    elif value["evidence_sufficiency"] == "sufficient":
        return False
    return True


def export_rejection_evidence(source_path: str | Path, destination_path: str | Path, *, index: int = 0) -> dict[str, Any]:
    """Export one structured rejection record without manual transcription."""

    source = Path(source_path)
    destination = Path(destination_path)
    if source.resolve() == destination.resolve():
        raise ValueError("source and destination must differ")
    if destination.exists():
        raise FileExistsError(str(destination))
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        evidence = payload["evidence"]["observable_model_rejection_evidence"]
        selected = evidence[index]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ValueError("canonical rejection evidence could not be selected") from exc
    if not validate_rejection_evidence(selected):
        raise ValueError("canonical rejection evidence failed integrity validation")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(selected, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return selected
