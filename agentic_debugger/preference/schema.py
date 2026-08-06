"""Strict preference-pair schema (``preference-pair-v1``).

A preference pair is one deterministic, verifier-backed record:

* stable pair identity (SHA-256 over the canonical identity payload), which
  binds the task/prompt identity, both attempt identities, both condition/
  model/adapter identities, both response hashes, both patch hashes, both
  source identities, the verifier-evidence identity, the source comparison
  identity and the schema version — and is **recomputed and verified on
  every load**;
* chosen and rejected responses with full provenance and response/patch
  hashes;
* verifier evidence for both sides;
* the rule id and preference reason;
* the source comparison identity.

The schema is strict: no unknown fields, no missing fields, no NaN/Infinity,
canonical JSON serialization.  Pairs are exported as deterministic JSONL plus
an audit summary; this package performs no DPO/RLHF.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from agentic_debugger.comparison.schema import (
    MAX_RESPONSE_TEXT_BYTES,
    bound_response_text,
    canonical_json,
)

PREFERENCE_PAIR_SCHEMA_VERSION = "preference-pair-v1"

#: Hard cap on one stored response; the explicit truncation marker is
#: included inside this exact budget (see :func:`bound_response`).
MAX_PAIR_RESPONSE_BYTES = MAX_RESPONSE_TEXT_BYTES

_ATTEMPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CONDITION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_HEX64_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")


class PreferenceError(ValueError):
    """Base error for the preference subsystem."""


class PreferenceInputError(PreferenceError):
    """Raised for invalid preference inputs."""


class PreferenceInvariantError(PreferenceError):
    """Raised when preference contracts are violated."""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bound_response(text: str) -> str:
    """Deterministically bound a response for pair storage.

    Marker-inclusive within the exact 64 KiB budget; no code-point split; no
    replacement-character expansion; the exact output byte length never
    exceeds the cap.  The raw response is preserved exactly elsewhere; only
    the pair's stored copy is bounded.
    """

    return bound_response_text(text, cap=MAX_PAIR_RESPONSE_BYTES)


def _ensure_str(v: Any, label: str) -> str:
    if type(v) is not str or not v:
        raise PreferenceInvariantError(f"{label} must be a non-empty string")
    return v


def _ensure_optional_str(v: Any, label: str) -> Optional[str]:
    if v is None:
        return None
    return _ensure_str(v, label)


def _ensure_dict(v: Any, label: str) -> Dict[str, Any]:
    if type(v) is not dict:
        raise PreferenceInvariantError(f"{label} must be a mapping")
    return dict(v)


def _check_required_fields(m: Mapping[str, Any], required: set, label: str) -> None:
    missing = required - set(m.keys())
    if missing:
        raise PreferenceInvariantError(f"Missing required fields in {label}: {sorted(missing)}")


def _check_no_unknown_fields(m: Mapping[str, Any], known: set, label: str) -> None:
    extra = set(m.keys()) - known
    if extra:
        raise PreferenceInvariantError(f"Unknown fields in {label}: {sorted(extra)}")


@dataclass(frozen=True)
class AttemptRef:
    """One side of a preference pair (strict, with hashes)."""

    attempt_id: str
    condition_id: str
    model_identity: Optional[str]
    adapter_identity: Optional[str]
    response: str
    response_sha256: Optional[str]
    patch_sha256: Optional[str]
    source_identity: str
    provenance: Dict[str, Any]

    _KNOWN_FIELDS = {
        "attempt_id", "condition_id", "model_identity", "adapter_identity",
        "response", "response_sha256", "patch_sha256", "source_identity",
        "provenance",
    }

    def __post_init__(self) -> None:
        _ensure_str(self.attempt_id, "attempt_id")
        if _ATTEMPT_ID_PATTERN.match(self.attempt_id) is None:
            raise PreferenceInvariantError(f"invalid attempt_id: {self.attempt_id!r}")
        _ensure_str(self.condition_id, "condition_id")
        if _CONDITION_ID_PATTERN.match(self.condition_id) is None:
            raise PreferenceInvariantError(f"invalid condition_id: {self.condition_id!r}")
        _ensure_optional_str(self.model_identity, "model_identity")
        _ensure_optional_str(self.adapter_identity, "adapter_identity")
        _ensure_str(self.response, "response")
        if len(self.response.encode("utf-8")) > MAX_PAIR_RESPONSE_BYTES:
            raise PreferenceInvariantError(
                f"stored response exceeds the {MAX_PAIR_RESPONSE_BYTES}-byte cap"
            )
        _ensure_optional_str(self.response_sha256, "response_sha256")
        if self.response_sha256 is not None:
            if _HEX64_PATTERN.match(self.response_sha256) is None:
                raise PreferenceInvariantError(
                    f"invalid response_sha256: {self.response_sha256!r}"
                )
            if self.response_sha256 != _sha256_text(self.response):
                raise PreferenceInvariantError(
                    "response_sha256 does not match the stored response"
                )
        _ensure_optional_str(self.patch_sha256, "patch_sha256")
        if self.patch_sha256 is not None and _HEX64_PATTERN.match(self.patch_sha256) is None:
            raise PreferenceInvariantError(f"invalid patch_sha256: {self.patch_sha256!r}")
        _ensure_str(self.source_identity, "source_identity")
        if type(self.provenance) is not dict:
            raise PreferenceInvariantError("provenance must be a mapping")

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "condition_id": self.condition_id,
            "model_identity": self.model_identity,
            "adapter_identity": self.adapter_identity,
            "response": self.response,
            "response_sha256": self.response_sha256,
            "patch_sha256": self.patch_sha256,
            "source_identity": self.source_identity,
            "provenance": dict(self.provenance),
        }

    @staticmethod
    def from_mapping(m: Any) -> "AttemptRef":
        if not isinstance(m, Mapping):
            raise PreferenceInvariantError("attempt ref must be a mapping")
        _check_required_fields(m, AttemptRef._KNOWN_FIELDS, "attempt ref")
        _check_no_unknown_fields(m, AttemptRef._KNOWN_FIELDS, "attempt ref")
        return AttemptRef(
            attempt_id=_ensure_str(m["attempt_id"], "attempt_id"),
            condition_id=_ensure_str(m["condition_id"], "condition_id"),
            model_identity=_ensure_optional_str(m["model_identity"], "model_identity"),
            adapter_identity=_ensure_optional_str(
                m["adapter_identity"], "adapter_identity"
            ),
            response=_ensure_str(m["response"], "response"),
            response_sha256=_ensure_optional_str(
                m["response_sha256"], "response_sha256"
            ),
            patch_sha256=_ensure_optional_str(m["patch_sha256"], "patch_sha256"),
            source_identity=_ensure_str(m["source_identity"], "source_identity"),
            provenance=_ensure_dict(m["provenance"], "provenance"),
        )


@dataclass(frozen=True)
class PreferencePair:
    """One strict preference pair."""

    schema_version: str
    pair_id: str
    task_id: str
    prompt_identity: str
    chosen: AttemptRef
    rejected: AttemptRef
    verifier_evidence: Dict[str, Any]
    rule_id: str
    preference_reason: str
    source_comparison_identity: str

    _KNOWN_FIELDS = {
        "schema_version", "pair_id", "task_id", "prompt_identity", "chosen",
        "rejected", "verifier_evidence", "rule_id", "preference_reason",
        "source_comparison_identity",
    }

    def __post_init__(self) -> None:
        if self.schema_version != PREFERENCE_PAIR_SCHEMA_VERSION:
            raise PreferenceInvariantError(
                f"unsupported preference pair schema: {self.schema_version!r}"
            )
        _ensure_str(self.pair_id, "pair_id")
        _ensure_str(self.task_id, "task_id")
        _ensure_str(self.prompt_identity, "prompt_identity")
        if not isinstance(self.chosen, AttemptRef) or not isinstance(self.rejected, AttemptRef):
            raise PreferenceInvariantError("chosen/rejected must be AttemptRef values")
        if self.chosen.attempt_id == self.rejected.attempt_id:
            raise PreferenceInvariantError("chosen and rejected must differ")
        if self.chosen.response == self.rejected.response:
            raise PreferenceInvariantError("chosen and rejected responses must differ")
        _ensure_str(self.rule_id, "rule_id")
        _ensure_str(self.preference_reason, "preference_reason")
        _ensure_str(self.source_comparison_identity, "source_comparison_identity")
        if type(self.verifier_evidence) is not dict:
            raise PreferenceInvariantError("verifier_evidence must be a mapping")
        expected = self._compute_identity()
        if self.pair_id != expected:
            raise PreferenceInvariantError("pair identity does not match its content")

    def _compute_identity(self) -> str:
        return PreferencePair.identity(
            task_id=self.task_id,
            prompt_identity=self.prompt_identity,
            chosen=self.chosen,
            rejected=self.rejected,
            verifier_evidence=self.verifier_evidence,
            source_comparison_identity=self.source_comparison_identity,
            schema_version=self.schema_version,
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pair_id": self.pair_id,
            "task_id": self.task_id,
            "prompt_identity": self.prompt_identity,
            "chosen": self.chosen.to_mapping(),
            "rejected": self.rejected.to_mapping(),
            "verifier_evidence": dict(self.verifier_evidence),
            "rule_id": self.rule_id,
            "preference_reason": self.preference_reason,
            "source_comparison_identity": self.source_comparison_identity,
        }

    @staticmethod
    def identity(
        *,
        task_id: str,
        prompt_identity: str,
        chosen: AttemptRef,
        rejected: AttemptRef,
        verifier_evidence: Mapping[str, Any],
        source_comparison_identity: str,
        schema_version: str = PREFERENCE_PAIR_SCHEMA_VERSION,
    ) -> str:
        """Stable pair identity binding every pair-relevant identity.

        Binds: schema version; task/prompt identity; chosen and rejected
        attempt identities, condition/model/adapter identities, full response
        hashes, patch hashes and source identities; the verifier-evidence
        identity; and the source comparison identity.
        """

        payload = {
            "schema_version": schema_version,
            "task_id": task_id,
            "prompt_identity": prompt_identity,
            "chosen": {
                "attempt_id": chosen.attempt_id,
                "condition_id": chosen.condition_id,
                "model_identity": chosen.model_identity,
                "adapter_identity": chosen.adapter_identity,
                "response_sha256": chosen.response_sha256,
                "patch_sha256": chosen.patch_sha256,
                "source_identity": chosen.source_identity,
            },
            "rejected": {
                "attempt_id": rejected.attempt_id,
                "condition_id": rejected.condition_id,
                "model_identity": rejected.model_identity,
                "adapter_identity": rejected.adapter_identity,
                "response_sha256": rejected.response_sha256,
                "patch_sha256": rejected.patch_sha256,
                "source_identity": rejected.source_identity,
            },
            "verifier_evidence_identity": hashlib.sha256(
                canonical_json(dict(verifier_evidence)).encode("utf-8")
            ).hexdigest(),
            "source_comparison_identity": source_comparison_identity,
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def from_mapping(m: Any) -> "PreferencePair":
        if not isinstance(m, Mapping):
            raise PreferenceInvariantError("preference pair must be a mapping")
        _check_required_fields(m, PreferencePair._KNOWN_FIELDS, "preference-pair-v1")
        _check_no_unknown_fields(m, PreferencePair._KNOWN_FIELDS, "preference-pair-v1")
        return PreferencePair(
            schema_version=_ensure_str(m["schema_version"], "schema_version"),
            pair_id=_ensure_str(m["pair_id"], "pair_id"),
            task_id=_ensure_str(m["task_id"], "task_id"),
            prompt_identity=_ensure_str(m["prompt_identity"], "prompt_identity"),
            chosen=AttemptRef.from_mapping(m["chosen"]),
            rejected=AttemptRef.from_mapping(m["rejected"]),
            verifier_evidence=_ensure_dict(m["verifier_evidence"], "verifier_evidence"),
            rule_id=_ensure_str(m["rule_id"], "rule_id"),
            preference_reason=_ensure_str(m["preference_reason"], "preference_reason"),
            source_comparison_identity=_ensure_str(
                m["source_comparison_identity"], "source_comparison_identity"
            ),
        )


__all__ = [
    "PREFERENCE_PAIR_SCHEMA_VERSION",
    "MAX_PAIR_RESPONSE_BYTES",
    "PreferenceError",
    "PreferenceInputError",
    "PreferenceInvariantError",
    "canonical_json",
    "bound_response",
    "AttemptRef",
    "PreferencePair",
]
