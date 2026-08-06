"""Strict imported-generation artifact schema and verifier-backed execution.

``generation-artifact-v1`` is the single self-contained JSON file per attempt
that carries externally produced (base or fine-tuned) generation output into
the comparison harness:

* identity (experiment, attempt, condition, task);
* model repository/revision and optional adapter identity;
* prompt-contract identity and generation configuration;
* bounded raw output (preserved exactly);
* a strict ``patch_extraction`` contract that binds the candidate patch to
  the raw output;
* optional runtime/memory/cost/token fields and external generation
  provider/network telemetry;
* provenance.

**Raw-output-to-patch binding (repair 1).** A model comparison must never
credit a patch that was not produced in the recorded raw output.  The
candidate patch is therefore always *derived from* ``raw_output`` through a
strict extraction contract:

* ``{"mode": "exact"}`` — for patch-only prompt contracts, the entire raw
  output IS the patch (``patch == raw_output``);
* ``{"mode": "substring", "start": s, "end": e}`` — for prose-plus-patch
  contracts, the patch is the exact UTF-8 byte substring
  ``raw_output[s:e]``.

At load time the importer reconstructs the derived patch and requires exact
substring equality, recomputed SHA-256 equality, and in-bounds offsets.  Any
mismatch — including a valid diff supplied in a separate ``patch`` field
that the raw output never contained — is rejected.  Malformed model output is
never heuristically repaired.

Rules enforced here:

* unknown and missing fields are rejected;
* NaN/Infinity is rejected (recursively, at load);
* oversized raw output is rejected;
* duplicate attempt identity is rejected at experiment level
  (:mod:`agentic_debugger.comparison.schema`);
* malformed patches are never normalized into valid patches — the existing
  strict patch parser and the existing :class:`EvaluationVerifier` decide;
* raw output is preserved exactly (stored byte-for-byte);
* patches are applied only inside a disposable workspace;
* verification happens only through :class:`EvaluationVerifier`;
* canonical fixture immutability is preserved and reported;
* workspaces are always cleaned.

Synthetic fixtures MUST be labeled with provenance generator
``offline-deterministic-demo``; they are infrastructure evidence, not real
model performance.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from agentic_debugger.comparison.json_bounds import validate_json_bounds
from agentic_debugger.comparison.schema import (
    MAX_RESPONSE_TEXT_BYTES,
    ComparisonInputError,
    ComparisonInvariantError,
    bound_response_text,
    canonical_json,
)
from agentic_debugger.evaluation.runner import EvaluationInputError
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.evaluation.verifier import EvaluationVerifier

GENERATION_ARTIFACT_SCHEMA_VERSION = "generation-artifact-v1"
#: Hard cap on the raw model output carried by one artifact.
MAX_RAW_OUTPUT_BYTES = 256 * 1024
#: Hard cap on the patch text carried by one artifact.
MAX_PATCH_BYTES = 128 * 1024

_EXPERIMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_ATTEMPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CONDITION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")

#: Synthetic generator identity; infrastructure evidence only.
SYNTHETIC_GENERATOR = "offline-deterministic-demo"

#: Closed vocabulary of patch-extraction modes.
PATCH_EXTRACTION_MODES = ("exact", "substring")


class ImportError(ComparisonInputError):
    """Raised when a generation artifact cannot be imported."""


def _ensure_str(v: Any, label: str) -> str:
    if type(v) is not str or not v:
        raise ImportError(f"{label} must be a non-empty string")
    return v


def _ensure_optional_str(v: Any, label: str) -> Optional[str]:
    if v is None:
        return None
    return _ensure_str(v, label)


def _ensure_bounded_str(v: Any, label: str, cap: int) -> str:
    value = _ensure_str(v, label)
    if len(value.encode("utf-8")) > cap:
        raise ImportError(f"{label} exceeds the {cap}-byte cap")
    return value


def _ensure_int_or_none(v: Any, label: str) -> Optional[int]:
    if v is None:
        return None
    if type(v) is not int or isinstance(v, bool) or v < 0:
        raise ImportError(f"{label} must be a non-negative integer or null")
    return v


def _ensure_finite_or_none(v: Any, label: str) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool) or type(v) not in (int, float):
        raise ImportError(f"{label} must be a finite number or null")
    value = float(v)
    if not math.isfinite(value):
        raise ImportError(f"{label} must be finite")
    return value


def _check_required_fields(m: Mapping[str, Any], required: set, label: str) -> None:
    missing = required - set(m.keys())
    if missing:
        raise ImportError(f"Missing required fields in {label}: {sorted(missing)}")


def _check_no_unknown_fields(m: Mapping[str, Any], known: set, label: str) -> None:
    extra = set(m.keys()) - known
    if extra:
        raise ImportError(f"Unknown fields in {label}: {sorted(extra)}")


def derive_patch(
    raw_output: str,
    patch: Optional[str],
    patch_extraction: Optional[Dict[str, Any]],
) -> str:
    """Strictly reconstruct the candidate patch from the raw output.

    Raises :class:`ImportError` on any mismatch: offsets outside the raw
    output, substring inequality, SHA-256 inequality, an extraction contract
    for a missing patch, or a patch without an extraction contract.
    """

    if type(raw_output) is not str:
        raise ImportError("raw_output must be a string")
    if patch is None:
        if patch_extraction is not None:
            raise ImportError("patch_extraction requires a patch")
        return ""
    if patch_extraction is None:
        raise ImportError("a patch requires a patch_extraction contract")
    if type(patch_extraction) is not dict:
        raise ImportError("patch_extraction must be a mapping")
    if set(patch_extraction) != {"mode"} and set(patch_extraction) != {"mode", "start", "end"}:
        raise ImportError(f"unknown patch_extraction fields: {sorted(patch_extraction)}")
    mode = patch_extraction.get("mode")
    if mode not in PATCH_EXTRACTION_MODES:
        raise ImportError(f"unknown patch extraction mode: {mode!r}")
    encoded = raw_output.encode("utf-8")
    if mode == "exact":
        if set(patch_extraction) != {"mode"}:
            raise ImportError("exact extraction accepts only the mode field")
        derived = raw_output
    else:
        start = patch_extraction.get("start")
        end = patch_extraction.get("end")
        if type(start) is not int or isinstance(start, bool):
            raise ImportError("substring extraction requires integer start")
        if type(end) is not int or isinstance(end, bool):
            raise ImportError("substring extraction requires integer end")
        if start < 0 or end < start or end > len(encoded):
            raise ImportError(
                f"extraction offsets [{start}, {end}) are outside the raw output"
            )
        segment = encoded[start:end]
        try:
            derived = segment.decode("utf-8")
        except UnicodeDecodeError:
            raise ImportError(
                "extraction offsets do not delimit a valid UTF-8 substring"
            ) from None
    if derived != patch:
        raise ImportError("patch does not equal the extracted raw-output substring")
    if hashlib.sha256(derived.encode("utf-8")).hexdigest() != hashlib.sha256(
        patch.encode("utf-8")
    ).hexdigest():
        raise ImportError("patch SHA-256 does not match the extracted substring")
    return derived


def extraction_for_substring(raw_output: str, patch: str) -> Dict[str, Any]:
    """Build the strict substring extraction contract for an embedded patch.

    Deterministic: the byte offsets of the first exact occurrence of ``patch``
    inside ``raw_output``.  Raises when the patch is not embedded exactly.
    """

    if type(raw_output) is not str or type(patch) is not str or not patch:
        raise ImportError("extraction_for_substring requires raw_output and patch")
    start = raw_output.encode("utf-8").find(patch.encode("utf-8"))
    if start < 0:
        raise ImportError("patch is not embedded in the raw output")
    end = start + len(patch.encode("utf-8"))
    return {"mode": "substring", "start": start, "end": end}


@dataclass(frozen=True)
class GenerationArtifact:
    """One strict imported generation artifact."""

    schema_version: str
    experiment_id: str
    attempt_id: str
    condition_id: str
    task_id: str
    model_repository: str
    model_revision: str
    adapter_identity: Optional[str]
    prompt_contract: str
    generation_config: Dict[str, Any]
    raw_output: str
    patch_extraction: Optional[Dict[str, Any]]
    patch: Optional[str]
    runtime_ms: Optional[int]
    memory_bytes: Optional[int]
    cost: Optional[float]
    tokens: Optional[int]
    external_provider_attempts: Optional[int]
    external_network_attempts: Optional[int]
    provenance: Dict[str, Any]

    _KNOWN_FIELDS = {
        "schema_version", "experiment_id", "attempt_id", "condition_id", "task_id",
        "model_repository", "model_revision", "adapter_identity", "prompt_contract",
        "generation_config", "raw_output", "patch_extraction", "patch", "runtime_ms",
        "memory_bytes", "cost", "tokens", "external_provider_attempts",
        "external_network_attempts", "provenance",
    }

    def __post_init__(self) -> None:
        if self.schema_version != GENERATION_ARTIFACT_SCHEMA_VERSION:
            raise ImportError(
                f"unsupported artifact schema version: {self.schema_version!r}"
            )
        _ensure_str(self.experiment_id, "experiment_id")
        if _EXPERIMENT_ID_PATTERN.match(self.experiment_id) is None:
            raise ImportError(f"invalid experiment_id: {self.experiment_id!r}")
        _ensure_str(self.attempt_id, "attempt_id")
        if _ATTEMPT_ID_PATTERN.match(self.attempt_id) is None:
            raise ImportError(f"invalid attempt_id: {self.attempt_id!r}")
        _ensure_str(self.condition_id, "condition_id")
        if _CONDITION_ID_PATTERN.match(self.condition_id) is None:
            raise ImportError(f"invalid condition_id: {self.condition_id!r}")
        _ensure_str(self.task_id, "task_id")
        if _TASK_ID_PATTERN.match(self.task_id) is None:
            raise ImportError(f"invalid task_id: {self.task_id!r}")
        _ensure_str(self.model_repository, "model_repository")
        _ensure_str(self.model_revision, "model_revision")
        _ensure_optional_str(self.adapter_identity, "adapter_identity")
        _ensure_str(self.prompt_contract, "prompt_contract")
        if type(self.generation_config) is not dict:
            raise ImportError("generation_config must be a mapping")
        try:
            validate_json_bounds(self.generation_config)
        except ComparisonInvariantError as exc:
            raise ImportError(f"generation_config {exc}") from exc
        _ensure_bounded_str(self.raw_output, "raw_output", MAX_RAW_OUTPUT_BYTES)
        if self.patch is not None:
            _ensure_bounded_str(self.patch, "patch", MAX_PATCH_BYTES)
        if type(self.provenance) is not dict:
            raise ImportError("provenance must be a mapping")
        try:
            validate_json_bounds(self.provenance)
        except ComparisonInvariantError as exc:
            raise ImportError(f"provenance {exc}") from exc
        _ensure_int_or_none(self.runtime_ms, "runtime_ms")
        _ensure_int_or_none(self.memory_bytes, "memory_bytes")
        _ensure_finite_or_none(self.cost, "cost")
        _ensure_int_or_none(self.tokens, "tokens")
        _ensure_int_or_none(
            self.external_provider_attempts, "external_provider_attempts"
        )
        _ensure_int_or_none(
            self.external_network_attempts, "external_network_attempts"
        )
        # Strict raw-output-to-patch binding: the patch must be derived from
        # the raw output through the declared extraction contract.
        derive_patch(self.raw_output, self.patch, self.patch_extraction)

    def identity(self) -> str:
        """Stable artifact identity over the complete canonical payload.

        Covers the raw output byte-for-byte, so the identity is the full
        generation response binding.
        """

        payload = self.to_mapping()
        payload.pop("runtime_ms", None)
        payload.pop("memory_bytes", None)
        payload.pop("cost", None)
        payload.pop("tokens", None)
        payload.pop("external_provider_attempts", None)
        payload.pop("external_network_attempts", None)
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "attempt_id": self.attempt_id,
            "condition_id": self.condition_id,
            "task_id": self.task_id,
            "model_repository": self.model_repository,
            "model_revision": self.model_revision,
            "adapter_identity": self.adapter_identity,
            "prompt_contract": self.prompt_contract,
            "generation_config": dict(self.generation_config),
            "raw_output": self.raw_output,
            "patch_extraction": (
                None if self.patch_extraction is None else dict(self.patch_extraction)
            ),
            "patch": self.patch,
            "runtime_ms": self.runtime_ms,
            "memory_bytes": self.memory_bytes,
            "cost": self.cost,
            "tokens": self.tokens,
            "external_provider_attempts": self.external_provider_attempts,
            "external_network_attempts": self.external_network_attempts,
            "provenance": dict(self.provenance),
        }

    @staticmethod
    def from_mapping(m: Any) -> "GenerationArtifact":
        if not isinstance(m, Mapping):
            raise ImportError("generation artifact must be a mapping")
        _check_required_fields(m, GenerationArtifact._KNOWN_FIELDS, "generation-artifact-v1")
        _check_no_unknown_fields(m, GenerationArtifact._KNOWN_FIELDS, "generation-artifact-v1")
        raw_output = m["raw_output"]
        if type(raw_output) is not str:
            raise ImportError("raw_output must be a string")
        patch = m["patch"]
        if patch is not None and type(patch) is not str:
            raise ImportError("patch must be a string or null")
        extraction = m["patch_extraction"]
        if extraction is not None and type(extraction) is not dict:
            raise ImportError("patch_extraction must be a mapping or null")
        return GenerationArtifact(
            schema_version=_ensure_str(m["schema_version"], "schema_version"),
            experiment_id=_ensure_str(m["experiment_id"], "experiment_id"),
            attempt_id=_ensure_str(m["attempt_id"], "attempt_id"),
            condition_id=_ensure_str(m["condition_id"], "condition_id"),
            task_id=_ensure_str(m["task_id"], "task_id"),
            model_repository=_ensure_str(m["model_repository"], "model_repository"),
            model_revision=_ensure_str(m["model_revision"], "model_revision"),
            adapter_identity=_ensure_optional_str(m["adapter_identity"], "adapter_identity"),
            prompt_contract=_ensure_str(m["prompt_contract"], "prompt_contract"),
            generation_config=dict(m["generation_config"]),
            raw_output=raw_output,
            patch_extraction=(
                None if extraction is None else dict(extraction)
            ),
            patch=patch,
            runtime_ms=_ensure_int_or_none(m["runtime_ms"], "runtime_ms"),
            memory_bytes=_ensure_int_or_none(m["memory_bytes"], "memory_bytes"),
            cost=_ensure_finite_or_none(m["cost"], "cost"),
            tokens=_ensure_int_or_none(m["tokens"], "tokens"),
            external_provider_attempts=_ensure_int_or_none(
                m["external_provider_attempts"], "external_provider_attempts"
            ),
            external_network_attempts=_ensure_int_or_none(
                m["external_network_attempts"], "external_network_attempts"
            ),
            provenance=dict(m["provenance"]),
        )

    @staticmethod
    def from_text(text: str) -> "GenerationArtifact":
        if type(text) is not str or not text:
            raise ImportError("artifact text must be non-empty")
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise ImportError(f"artifact is not valid JSON: {exc}") from None
        if not isinstance(parsed, dict):
            raise ImportError("artifact must be a JSON object")
        return GenerationArtifact.from_mapping(parsed)

    @staticmethod
    def from_file(path: str) -> "GenerationArtifact":
        if not isinstance(path, str) or not path:
            raise ImportError("path must be non-empty")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise ImportError(f"artifact file could not be read: {exc}") from None
        return GenerationArtifact.from_text(text)

    def to_text(self) -> str:
        return canonical_json(self.to_mapping()) + "\n"


# ---------------------------------------------------------------------------
# Verifier-backed execution of an imported artifact
# ---------------------------------------------------------------------------


def verifier_evidence_mapping(evaluation: Any) -> Dict[str, Any]:
    """Compact verifier evidence mapping for an :class:`EvaluationResult`.

    Mirrors the accepted ``demo.runner._verifier_record`` key set so that
    imported and native attempt records carry the same verifier evidence
    shape; the verification itself is always performed by the existing
    :class:`EvaluationVerifier`.
    """

    if evaluation is None:
        return {
            "executed": False,
            "status": None,
            "stop_reason": None,
            "outcome": None,
            "baseline_valid": None,
            "patch_applied": None,
            "patch_changed_files": [],
            "syntax_passed": None,
            "f2p_total": None,
            "f2p_passed": None,
            "p2p_total": None,
            "p2p_passed": None,
            "full_suite_status": None,
            "workspace_lifecycle": None,
            "workspace_cleaned": None,
            "canonical_fixture_unchanged": None,
            "diagnostic": None,
        }
    full_suite = evaluation.full_suite
    return {
        "executed": True,
        "status": evaluation.status.value,
        "stop_reason": evaluation.stop_reason,
        "outcome": evaluation.outcome.value if evaluation.outcome else None,
        "baseline_valid": evaluation.baseline.valid,
        "patch_applied": evaluation.patch_application.success,
        "patch_changed_files": list(evaluation.patch_application.changed_files),
        "syntax_passed": evaluation.syntax.passed,
        "f2p_total": evaluation.f2p_total,
        "f2p_passed": evaluation.f2p_passed,
        "p2p_total": evaluation.p2p_total,
        "p2p_passed": evaluation.p2p_passed,
        "full_suite_status": full_suite.status.value if full_suite else None,
        "workspace_lifecycle": evaluation.workspace.lifecycle.value,
        "workspace_cleaned": evaluation.workspace.cleaned,
        "canonical_fixture_unchanged": evaluation.workspace.canonical_fixture_unchanged,
        "diagnostic": evaluation.diagnostic,
    }


def run_imported_attempt(
    artifact: GenerationArtifact,
    *,
    task: DebugTask,
    repository_root: str,
    workspace_parent: str,
    runtime_ms: Optional[int] = None,
    role: str = "evaluation",
) -> Dict[str, Any]:
    """Verify one imported artifact and return a normalized attempt record.

    The candidate patch is the patch derived from the artifact's raw output
    (already enforced at load).  It goes through the existing strict parser
    and :class:`EvaluationVerifier` inside a disposable workspace.  A
    malformed patch is a typed rejection with ``valid_patch=false`` — it is
    never normalized into a valid patch.  No patch means no verifier
    execution and ``valid_patch=false``.

    Local verification telemetry (provider/network attempts) is separate
    from external generation telemetry: the latter is taken from the
    artifact when supplied and is never fabricated as zero.
    """

    if not isinstance(artifact, GenerationArtifact):
        raise ImportError("run_imported_attempt requires a GenerationArtifact")
    if not isinstance(task, DebugTask):
        raise ImportError("run_imported_attempt requires a DebugTask")
    if artifact.task_id != task.task_id:
        raise ImportError(
            f"artifact task {artifact.task_id!r} does not match task {task.task_id!r}"
        )
    if role not in ("evaluation", "preference-fixture"):
        raise ImportError(f"unknown attempt role: {role!r}")
    if not os.path.isdir(repository_root):
        raise ImportError(f"repository_root is not a directory: {repository_root!r}")
    if not os.path.isdir(workspace_parent):
        raise ImportError(f"workspace_parent is not a directory: {workspace_parent!r}")

    generation_produced = bool(artifact.raw_output.strip())
    if generation_produced:
        # Bounded storage copy: marker-inclusive, never exceeds the cap; the
        # full raw output stays byte-for-byte in the artifact and its hash is
        # bound by the artifact identity (source_identity).
        response_text = bound_response_text(
            artifact.raw_output, cap=MAX_RESPONSE_TEXT_BYTES
        )
        response_sha256 = hashlib.sha256(response_text.encode("utf-8")).hexdigest()
    else:
        response_text = None
        response_sha256 = None
    patch = artifact.patch
    patch_present = patch is not None
    valid_patch = False
    patch_sha256 = None
    evidence: Optional[Dict[str, Any]] = None
    verifier_outcome = None
    verifier_status = None
    f2p_passed = f2p_total = p2p_passed = p2p_total = None
    changed_file_count = None
    correct_target_file = None
    cleanup_status = None
    canonical_fixture_unchanged = None
    diagnostic: Optional[str] = None
    evaluation = None

    if patch is not None:
        patch_sha256 = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        try:
            evaluation = EvaluationVerifier(
                repository_root, workspace_parent=workspace_parent
            ).evaluate(task, patch)
        except EvaluationInputError as exc:
            diagnostic = str(exc)[:400]
        except Exception as exc:  # noqa: BLE001 - typed boundary
            diagnostic = str(exc)[:400]
            evaluation = None
        if evaluation is not None:
            evidence = verifier_evidence_mapping(evaluation)
            # Strict-parse gate: only a patch the verifier actually applied
            # counts as valid; malformed diffs are rejected, never normalized.
            valid_patch = bool(evidence.get("patch_applied"))
            verifier_status = evidence.get("status")
            verifier_outcome = evidence.get("outcome")
            f2p_passed = evidence.get("f2p_passed")
            f2p_total = evidence.get("f2p_total")
            p2p_passed = evidence.get("p2p_passed")
            p2p_total = evidence.get("p2p_total")
            changed_files = evidence.get("patch_changed_files") or []
            changed_file_count = len(changed_files)
            if evidence.get("status") == "COMPLETED" and evidence.get(
                "workspace_cleaned"
            ) is True:
                cleanup_status = "cleaned"
            else:
                cleanup_status = "failed"
            canonical_fixture_unchanged = evidence.get("canonical_fixture_unchanged")
            if evidence.get("patch_applied") and changed_files:
                oracle_files = set(task.oracle.target_files)
                correct_target_file = set(changed_files).issubset(oracle_files)
    else:
        diagnostic = "no patch in artifact"

    from agentic_debugger.comparison.metrics import normalize_failure_category

    facts = {
        "generation_produced": generation_produced,
        "valid_patch": valid_patch,
        "patch_present": patch_present,
        "patch_applied": evidence.get("patch_applied") if evidence else None,
        "syntax_passed": evidence.get("syntax_passed") if evidence else None,
        "verifier_outcome": verifier_outcome,
        "verifier_status": verifier_status,
        "f2p_passed": f2p_passed,
        "f2p_total": f2p_total,
        "p2p_passed": p2p_passed,
        "p2p_total": p2p_total,
    }
    return {
        "attempt_id": artifact.attempt_id,
        "condition_id": artifact.condition_id,
        "task_id": artifact.task_id,
        "mode": "imported",
        "role": role,
        "source_identity": f"generation-artifact-v1:{artifact.identity()}",
        "generation_produced": generation_produced,
        "valid_patch": valid_patch,
        "patch_sha256": patch_sha256,
        "changed_file_count": changed_file_count,
        "correct_target_file": correct_target_file,
        "localization_outcome": None,
        "f2p_passed": f2p_passed,
        "f2p_total": f2p_total,
        "p2p_passed": p2p_passed,
        "p2p_total": p2p_total,
        "verifier_outcome": verifier_outcome,
        "verifier_status": verifier_status,
        "failure_category": normalize_failure_category(facts),
        "runtime_ms": runtime_ms,
        "memory_bytes": artifact.memory_bytes,
        "cost": artifact.cost,
        "tokens": artifact.tokens,
        "retrieval_count": None,
        "retrieval_bytes": None,
        "retrieval_latency_ms": None,
        "replay_valid": None,
        "cleanup_status": cleanup_status,
        "canonical_fixture_unchanged": canonical_fixture_unchanged,
        "provider_attempts": 0,
        "network_attempts": 0,
        "external_provider_attempts": artifact.external_provider_attempts,
        "external_network_attempts": artifact.external_network_attempts,
        "response_text": response_text,
        "response_sha256": response_sha256,
        "verifier_evidence": evidence,
        "provenance": {
            "source": "generation-artifact-v1",
            "generator": artifact.provenance.get("generator"),
            "note": artifact.provenance.get("note"),
            "model_identity": artifact.model_repository,
            "model_revision": artifact.model_revision,
            "adapter_identity": artifact.adapter_identity,
            "prompt_contract": artifact.prompt_contract,
            "generation_config": dict(artifact.generation_config),
            "diagnostic": diagnostic,
        },
    }


__all__ = [
    "GENERATION_ARTIFACT_SCHEMA_VERSION",
    "MAX_RAW_OUTPUT_BYTES",
    "MAX_PATCH_BYTES",
    "PATCH_EXTRACTION_MODES",
    "SYNTHETIC_GENERATOR",
    "ImportError",
    "derive_patch",
    "extraction_for_substring",
    "GenerationArtifact",
    "run_imported_attempt",
    "verifier_evidence_mapping",
]
