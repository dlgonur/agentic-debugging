"""Deterministic, metadata-only BugsInPy licensing and operation preflight.

This module deliberately reads only the tracked pilot manifest and canonical
licensing record.  It never resolves an upstream path, starts a process, opens
a socket, creates a workspace, or imports an execution backend.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional


SCHEMA_VERSION = "1.0"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_MANIFEST_PATH = (REPOSITORY_ROOT / "research/bugsinpy/PILOT_ELIGIBILITY_MANIFEST_V1.json").resolve()
CANONICAL_GATE_PATH = (REPOSITORY_ROOT / "research/bugsinpy/BUGSINPY_LICENSE_GATE_V1.json").resolve()
CANONICAL_VALIDATOR_PATH = (REPOSITORY_ROOT / "scripts/validate_bugsinpy_license_gate.py").resolve()
DEFAULT_MANIFEST_PATH = CANONICAL_MANIFEST_PATH
DEFAULT_GATE_PATH = CANONICAL_GATE_PATH
DEFAULT_VALIDATOR_PATH = CANONICAL_VALIDATOR_PATH


class BugsInPyOperation(str, Enum):
    INSPECT_METADATA = "inspect_metadata"
    ACQUIRE_SOURCE = "acquire_source"
    CHECKOUT_REVISION = "checkout_revision"
    PREPARE_DEPENDENCIES = "prepare_dependencies"
    START_CONTAINMENT = "start_containment"
    REPRODUCE_BUG = "reproduce_bug"
    RUN_DEBUG_POLICY = "run_debug_policy"
    VERIFY_PATCH = "verify_patch"
    PACKAGE_EVIDENCE = "package_evidence"
    PACKAGE_METADATA_EVIDENCE = "package_metadata_evidence"


KNOWN_OPERATIONS = frozenset(item.value for item in BugsInPyOperation)
SIDE_EFFECTING_OPERATIONS = frozenset(
    item.value
    for item in BugsInPyOperation
    if item not in {BugsInPyOperation.INSPECT_METADATA, BugsInPyOperation.PACKAGE_METADATA_EVIDENCE}
)
AFFIRMATIVE_OPERATIONS = SIDE_EFFECTING_OPERATIONS - {BugsInPyOperation.PACKAGE_EVIDENCE.value}
DEPENDENCY_OPERATIONS = frozenset({
    BugsInPyOperation.PREPARE_DEPENDENCIES.value,
    BugsInPyOperation.REPRODUCE_BUG.value,
    BugsInPyOperation.RUN_DEBUG_POLICY.value,
    BugsInPyOperation.VERIFY_PATCH.value,
})
CONTAINMENT_OPERATIONS = AFFIRMATIVE_OPERATIONS - {BugsInPyOperation.PREPARE_DEPENDENCIES.value}
VERDICT_RANK = {"BLOCKED": 0, "UNKNOWN": 1, "CLEAR_WITH_CONDITIONS": 2, "CLEAR": 3}
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class PreflightReasonCode(str, Enum):
    ALLOWED_METADATA_INSPECTION = "ALLOWED_METADATA_INSPECTION"
    ALLOWED_SANITIZED_METADATA_EVIDENCE = "ALLOWED_SANITIZED_METADATA_EVIDENCE"
    OPERATION_ALLOWED = "OPERATION_ALLOWED"
    TASK_ID_REQUIRED = "TASK_ID_REQUIRED"
    UNKNOWN_TASK = "UNKNOWN_TASK"
    UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
    AUTHORITY_MISSING = "AUTHORITY_MISSING"
    AUTHORITY_JSON_INVALID = "AUTHORITY_JSON_INVALID"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    LICENSE_VALIDATOR_FAILED = "LICENSE_VALIDATOR_FAILED"
    AUTHORITY_REVISION_MISMATCH = "AUTHORITY_REVISION_MISMATCH"
    TASK_VERDICT_EXCEEDS_AUTHORITY = "TASK_VERDICT_EXCEEDS_AUTHORITY"
    DATASET_VERDICT_BLOCKED = "DATASET_VERDICT_BLOCKED"
    TASK_VERDICT_BLOCKED = "TASK_VERDICT_BLOCKED"
    OPERATIONAL_GATE_BLOCKED = "OPERATIONAL_GATE_BLOCKED"
    PROJECT_PERMISSION_REQUIRED = "PROJECT_PERMISSION_REQUIRED"
    DATASET_PERMISSION_REQUIRED = "DATASET_PERMISSION_REQUIRED"
    TASK_PERMISSION_REQUIRED = "TASK_PERMISSION_REQUIRED"
    OPERATIONAL_PERMISSION_REQUIRED = "OPERATIONAL_PERMISSION_REQUIRED"
    PRIVATE_USE_PERMISSION_REQUIRED = "PRIVATE_USE_PERMISSION_REQUIRED"
    FORMAL_LICENSE_PERMISSION_REQUIRED = "FORMAL_LICENSE_PERMISSION_REQUIRED"
    AFFIRMATIVE_VERDICT_REQUIRED = "AFFIRMATIVE_VERDICT_REQUIRED"
    OPERATOR_AUTHORIZATION_REQUIRED = "OPERATOR_AUTHORIZATION_REQUIRED"
    CONTAINMENT_NOT_READY = "CONTAINMENT_NOT_READY"
    DEPENDENCY_NOT_READY = "DEPENDENCY_NOT_READY"
    SOURCE_BEARING_EVIDENCE_PROHIBITED = "SOURCE_BEARING_EVIDENCE_PROHIBITED"
    EVIDENCE_HANDLING_REQUIRED = "EVIDENCE_HANDLING_REQUIRED"
    RESOURCE_SCOPE_INVALID = "RESOURCE_SCOPE_INVALID"
    AUTHORITY_SNAPSHOT_MISMATCH = "AUTHORITY_SNAPSHOT_MISMATCH"
    NONCANONICAL_AUTHORITY = "NONCANONICAL_AUTHORITY"
    UNSUPPORTED_OVERRIDE = "UNSUPPORTED_OVERRIDE"


_PERMIT_ISSUER = object()


class PreflightAuthorizationError(PermissionError):
    """A BugsInPy side-effect operation lacks an issuer-bound permit."""


@dataclass(frozen=True)
class AuthoritySnapshot:
    """Immutable content and verdict identity used to revoke old permits."""

    manifest_path: str
    gate_path: str
    validator_path: str
    manifest_sha256: str
    gate_sha256: str
    validator_sha256: str
    authority_revisions: Mapping[str, Optional[str]]
    dataset_verdict: Optional[str]
    task_verdict: Optional[str]
    operational_execution_gate: Optional[str]
    validation_status: str
    canonical_paths: bool

    def to_mapping(self) -> dict[str, Any]:
        return {
            "manifest_path": self.manifest_path,
            "gate_path": self.gate_path,
            "validator_path": self.validator_path,
            "manifest_sha256": self.manifest_sha256,
            "gate_sha256": self.gate_sha256,
            "validator_sha256": self.validator_sha256,
            "authority_revisions": dict(self.authority_revisions),
            "dataset_verdict": self.dataset_verdict,
            "task_verdict": self.task_verdict,
            "operational_execution_gate": self.operational_execution_gate,
            "validation_status": self.validation_status,
            "canonical_paths": self.canonical_paths,
        }


class BugsInPyOperationPermit:
    """Opaque capability issued only by an ALLOWing metadata preflight.

    The constructor requires a module-private issuer sentinel.  Callers can
    hold and pass a permit, but cannot manufacture one from public decision
    fields or a copied ``MetadataPreflightDecision``.
    """

    __slots__ = ("_task_id", "_operation", "_authority_snapshot", "_run_id", "_allowed_source_pairs", "_allowed_metadata_paths", "_issuer", "_initialized")

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_initialized", False):
            raise AttributeError("operation permits are immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        task_id: str,
        operation: str,
        authority_snapshot: AuthoritySnapshot,
        run_id: str,
        allowed_source_pairs: tuple[tuple[str, str], ...],
        allowed_metadata_paths: tuple[str, ...],
        *,
        _issuer: object,
    ) -> None:
        if _issuer is not _PERMIT_ISSUER:
            raise TypeError("operation permits are issued by metadata preflight only")
        object.__setattr__(self, "_task_id", task_id)
        object.__setattr__(self, "_operation", operation)
        object.__setattr__(self, "_authority_snapshot", authority_snapshot)
        object.__setattr__(self, "_run_id", run_id)
        object.__setattr__(self, "_allowed_source_pairs", tuple(allowed_source_pairs))
        object.__setattr__(self, "_allowed_metadata_paths", tuple(allowed_metadata_paths))
        object.__setattr__(self, "_issuer", _issuer)
        object.__setattr__(self, "_initialized", True)

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def operation(self) -> str:
        return self._operation

    @property
    def authority_revisions(self) -> Mapping[str, Optional[str]]:
        return dict(self._authority_snapshot.authority_revisions)

    @property
    def authority_snapshot(self) -> AuthoritySnapshot:
        return self._authority_snapshot

    @property
    def allowed_source_pairs(self) -> tuple[tuple[str, str], ...]:
        return self._allowed_source_pairs

    @property
    def allowed_metadata_paths(self) -> tuple[str, ...]:
        return self._allowed_metadata_paths

    @property
    def run_id(self) -> str:
        return self._run_id

    def _valid_for(
        self,
        *,
        task_id: str,
        operation: str,
        authority_snapshot: AuthoritySnapshot,
        run_id: str,
    ) -> bool:
        return (
            self._issuer is _PERMIT_ISSUER
            and self._task_id == task_id
            and self._operation == operation
            and self._run_id == run_id
            and self._authority_snapshot == authority_snapshot
        )


def _issue_permit(
    *,
    task_id: str,
    operation: str,
    authority_snapshot: AuthoritySnapshot,
    run_id: str,
    allowed_source_pairs: tuple[tuple[str, str], ...],
    allowed_metadata_paths: tuple[str, ...],
) -> BugsInPyOperationPermit:
    return BugsInPyOperationPermit(
        task_id,
        operation,
        authority_snapshot,
        run_id,
        allowed_source_pairs,
        allowed_metadata_paths,
        _issuer=_PERMIT_ISSUER,
    )


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _bounded_path(value: str | Path) -> str:
    return str(Path(value))


def _resolved_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_url(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return value.rstrip("/")


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("authority JSON root must be an object")
    return value


def _validator_function() -> Any:
    """Load only the tracked canonical validator, never a caller path."""
    spec = importlib.util.spec_from_file_location("bugsinpy_license_gate_validator", CANONICAL_VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise ValueError("tracked validator cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, "validate", None)
    if not callable(function):
        raise ValueError("tracked validator has no validate function")
    return function


def _readiness(value: object, *, not_required: bool) -> str:
    if not_required and value in (None, "not_required"):
        return "not_required"
    if value is True:
        return "ready"
    if isinstance(value, Mapping) and value.get("ready") is True:
        return "ready"
    if value is False:
        return "not_ready"
    return "not_affirmatively_clear"


def _affirmative(value: Optional[str]) -> bool:
    return value in {"CLEAR", "CLEAR_WITH_CONDITIONS"}


@dataclass(frozen=True)
class MetadataPreflightDecision:
    """Stable machine-readable decision returned by the metadata boundary."""

    schema_version: str
    task_id: str
    requested_operation: str
    decision: str
    reason_code: str
    reason: str
    manifest_verdict: Optional[str]
    dataset_verdict: Optional[str]
    project_verdict: Optional[str]
    task_verdict: Optional[str]
    formal_license_status: Optional[str]
    redistribution_verdict: Optional[str]
    private_use_verdict: Optional[str]
    operational_execution_gate: Optional[str]
    operator_authorization_state: str
    containment_readiness: str
    dependency_readiness: str
    authority_revisions: Mapping[str, Optional[str]]
    authority_paths: Mapping[str, str]
    validation: Mapping[str, str]
    run_id: str
    authority_snapshot: Optional[AuthoritySnapshot] = None
    authorization_scope: Mapping[str, Any] = None  # type: ignore[assignment]
    permit: Optional[BugsInPyOperationPermit] = None

    @property
    def allowed(self) -> bool:
        return self.decision == "ALLOW"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "requested_operation": self.requested_operation,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "manifest_verdict": self.manifest_verdict,
            "dataset_verdict": self.dataset_verdict,
            "project_verdict": self.project_verdict,
            "task_verdict": self.task_verdict,
            "formal_license_status": self.formal_license_status,
            "redistribution_verdict": self.redistribution_verdict,
            "private_use_verdict": self.private_use_verdict,
            "operational_execution_gate": self.operational_execution_gate,
            "operator_authorization_state": self.operator_authorization_state,
            "containment_readiness": self.containment_readiness,
            "dependency_readiness": self.dependency_readiness,
            "authority_revisions": dict(self.authority_revisions),
            "authority_paths": dict(self.authority_paths),
            "validation": dict(self.validation),
            "run_id": self.run_id,
            "authority_snapshot": self.authority_snapshot.to_mapping() if self.authority_snapshot else None,
            "authorization_scope": dict(self.authorization_scope or {}),
        }


def _run_id(values: Mapping[str, Any]) -> str:
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "bugsinpy-preflight-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


class BugsInPyMetadataPreflight:
    """Validate tracked authorities and decide only bounded operations."""

    def __init__(
        self,
        *,
        manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
        gate_path: str | Path = DEFAULT_GATE_PATH,
    ) -> None:
        self.manifest_path = Path(manifest_path).expanduser().resolve()
        self.gate_path = Path(gate_path).expanduser().resolve()
        self.validator_path = CANONICAL_VALIDATOR_PATH

    def authority_revisions(self) -> dict[str, Optional[str]]:
        """Read the current bounded authority revisions for permit freshness."""
        manifest = _load_json(self.manifest_path)
        gate = _load_json(self.gate_path)
        authority = manifest.get("authority")
        records = gate.get("dataset_records")
        if not isinstance(authority, Mapping) or not isinstance(records, list) or not records:
            raise ValueError("authority revisions are unavailable")
        manifest_revision = _text(authority.get("official_repository_revision"))
        dataset_revision = _text(records[0].get("revision")) if isinstance(records[0], Mapping) else None
        if not manifest_revision or not dataset_revision or manifest_revision != dataset_revision:
            raise ValueError("authority revisions do not match")
        return {"manifest": manifest_revision, "gate_dataset": dataset_revision}

    def _scope_for(self, manifest: Mapping[str, Any], task: Mapping[str, Any], operation: str, authority_revision: str) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
        bugsinpy = task.get("bugsinpy")
        if not isinstance(bugsinpy, Mapping):
            raise ValueError("task BugsInPy metadata is invalid")
        project_url = _normalize_url(bugsinpy.get("project_url"))
        buggy = _text(bugsinpy.get("buggy_revision"))
        fixed = _text(bugsinpy.get("fixed_revision"))
        official = _normalize_url((manifest.get("authority") or {}).get("official_repository"))
        metadata_paths = bugsinpy.get("metadata_paths")
        if not project_url or not official or not _REVISION.fullmatch(buggy or "") or not _REVISION.fullmatch(fixed or "") or not isinstance(metadata_paths, list):
            raise ValueError("task resource metadata is invalid")
        normalized_paths = tuple(str(path).replace("\\", "/") for path in metadata_paths if isinstance(path, str))
        patch_paths = tuple(path for path in normalized_paths if path.endswith("/bug_patch.txt"))
        if len(normalized_paths) != len(metadata_paths) or len(patch_paths) != 1 or len(set(normalized_paths)) != len(normalized_paths):
            raise ValueError("task patch metadata path is ambiguous")
        if operation == BugsInPyOperation.ACQUIRE_SOURCE.value:
            pairs = ((official, authority_revision), (project_url, buggy))
        elif operation == BugsInPyOperation.CHECKOUT_REVISION.value:
            pairs = ((official, authority_revision), (project_url, buggy), (project_url, fixed))
        else:
            pairs = ()
        return pairs, patch_paths

    def _snapshot(self, task: Mapping[str, Any], dataset_verdict: Optional[str], task_verdict: Optional[str], operational: Optional[str], *, validation_status: str) -> AuthoritySnapshot:
        revisions = self.authority_revisions()
        return AuthoritySnapshot(
            str(self.manifest_path),
            str(self.gate_path),
            str(CANONICAL_VALIDATOR_PATH),
            _sha256_file(self.manifest_path),
            _sha256_file(self.gate_path),
            _sha256_file(CANONICAL_VALIDATOR_PATH),
            revisions,
            dataset_verdict,
            task_verdict,
            operational,
            validation_status,
            self.manifest_path == CANONICAL_MANIFEST_PATH and self.gate_path == CANONICAL_GATE_PATH,
        )

    def decide(
        self,
        task_id: Optional[str],
        operation: Optional[str],
        *,
        operator_authorization_state: str = "absent",
        containment_readiness: object = None,
        dependency_readiness: object = None,
        evidence_handling: str = "unspecified",
        override_flags: Optional[Mapping[str, object]] = None,
    ) -> MetadataPreflightDecision:
        task_value = task_id if isinstance(task_id, str) else ""
        operation_value = operation if isinstance(operation, str) else ""
        operator_value = operator_authorization_state if isinstance(operator_authorization_state, str) else "invalid"
        containment = _readiness(containment_readiness, not_required=operation_value not in AFFIRMATIVE_OPERATIONS)
        dependency = _readiness(dependency_readiness, not_required=operation_value not in {BugsInPyOperation.PREPARE_DEPENDENCIES.value, BugsInPyOperation.REPRODUCE_BUG.value, BugsInPyOperation.RUN_DEBUG_POLICY.value, BugsInPyOperation.VERIFY_PATCH.value})
        paths = {
            "manifest": _bounded_path(self.manifest_path),
            "canonical_gate": _bounded_path(self.gate_path),
            "validator": _bounded_path(CANONICAL_VALIDATOR_PATH),
        }
        revisions: dict[str, Optional[str]] = {"manifest": None, "gate_dataset": None}
        authority_snapshot: Optional[AuthoritySnapshot] = None
        allowed_source_pairs: tuple[tuple[str, str], ...] = ()
        allowed_metadata_paths: tuple[str, ...] = ()
        values: dict[str, Any] = {
            "task_id": task_value,
            "operation": operation_value,
            "manifest_path": paths["manifest"],
            "gate_path": paths["canonical_gate"],
            "operator": operator_value,
            "containment": containment,
            "dependency": dependency,
            "evidence": evidence_handling,
        }

        def finish(
            code: PreflightReasonCode,
            reason: str,
            *,
            decision: str = "BLOCK",
            manifest_verdict: Optional[str] = None,
            dataset_verdict: Optional[str] = None,
            project_verdict: Optional[str] = None,
            task_verdict: Optional[str] = None,
            formal_license_status: Optional[str] = None,
            redistribution_verdict: Optional[str] = None,
            private_use_verdict: Optional[str] = None,
            operational_execution_gate: Optional[str] = None,
            validation: Optional[Mapping[str, str]] = None,
        ) -> MetadataPreflightDecision:
            scope = {
                "allowed_source_pairs": [list(pair) for pair in allowed_source_pairs],
                "allowed_metadata_paths": list(allowed_metadata_paths),
            }
            full_values = {**values, "code": code.value, "decision": decision, "revisions": revisions, "snapshot": authority_snapshot.to_mapping() if authority_snapshot else None, "scope": scope}
            result = MetadataPreflightDecision(
                SCHEMA_VERSION,
                task_value,
                operation_value,
                decision,
                code.value,
                reason,
                manifest_verdict,
                dataset_verdict,
                project_verdict,
                task_verdict,
                formal_license_status,
                redistribution_verdict,
                private_use_verdict,
                operational_execution_gate,
                operator_value,
                containment,
                dependency,
                revisions,
                paths,
                validation or {"status": "FAIL"},
                _run_id(full_values),
                authority_snapshot,
                scope,
            )
            if result.allowed and operation_value in SIDE_EFFECTING_OPERATIONS:
                if authority_snapshot is None:
                    raise PreflightAuthorizationError("allowed side-effect operation has no authority snapshot")
                object.__setattr__(
                    result,
                    "permit",
                    _issue_permit(
                        task_id=task_value,
                        operation=operation_value,
                        authority_snapshot=authority_snapshot,
                        run_id=result.run_id,
                        allowed_source_pairs=allowed_source_pairs,
                        allowed_metadata_paths=allowed_metadata_paths,
                    ),
                )
            return result

        if not task_value:
            return finish(PreflightReasonCode.TASK_ID_REQUIRED, "task ID is required")
        if operation_value not in KNOWN_OPERATIONS:
            return finish(PreflightReasonCode.UNKNOWN_OPERATION, "operation is not in the supported vocabulary")
        if override_flags is not None and (not isinstance(override_flags, Mapping) or override_flags):
            return finish(PreflightReasonCode.UNSUPPORTED_OVERRIDE, "override and bypass flags are not accepted")
        if operator_value not in {"absent", "approved", "declined", "not_required"}:
            return finish(PreflightReasonCode.UNSUPPORTED_OVERRIDE, "operator authorization state is unsupported")
        if operation_value == BugsInPyOperation.PACKAGE_EVIDENCE.value:
            return finish(PreflightReasonCode.SOURCE_BEARING_EVIDENCE_PROHIBITED, "package_evidence is prohibited for upstream-bearing evidence")
        if operation_value == BugsInPyOperation.PACKAGE_METADATA_EVIDENCE.value and evidence_handling != "sanitized_metadata_only":
            if evidence_handling in {"source_bearing", "raw_upstream"}:
                return finish(PreflightReasonCode.SOURCE_BEARING_EVIDENCE_PROHIBITED, "source-bearing metadata evidence is prohibited")
            return finish(PreflightReasonCode.EVIDENCE_HANDLING_REQUIRED, "package_metadata_evidence requires sanitized_metadata_only")
        if operation_value not in {BugsInPyOperation.PACKAGE_EVIDENCE.value, BugsInPyOperation.PACKAGE_METADATA_EVIDENCE.value} and evidence_handling != "unspecified":
            if evidence_handling in {"source_bearing", "raw_upstream"}:
                return finish(PreflightReasonCode.SOURCE_BEARING_EVIDENCE_PROHIBITED, "source-bearing evidence handling is prohibited for this operation")
            return finish(PreflightReasonCode.EVIDENCE_HANDLING_REQUIRED, "evidence handling value is unsupported")

        try:
            manifest = _load_json(self.manifest_path)
        except FileNotFoundError:
            return finish(PreflightReasonCode.AUTHORITY_MISSING, "tracked manifest is missing")
        except (OSError, json.JSONDecodeError, ValueError):
            return finish(PreflightReasonCode.AUTHORITY_JSON_INVALID, "tracked manifest JSON is invalid")
        try:
            gate = _load_json(self.gate_path)
        except FileNotFoundError:
            return finish(PreflightReasonCode.AUTHORITY_MISSING, "canonical licensing gate is missing")
        except (OSError, json.JSONDecodeError, ValueError):
            return finish(PreflightReasonCode.AUTHORITY_JSON_INVALID, "canonical licensing gate JSON is invalid")

        if (
            manifest.get("manifest_schema_version") != "1.0"
            or manifest.get("manifest_id") != "bugsinpy-pilot-eligibility-v1"
            or manifest.get("dataset") != "BugsInPy"
            or not isinstance(manifest.get("authority"), Mapping)
            or not _REVISION.fullmatch(str((manifest.get("authority") or {}).get("official_repository_revision", "")))
            or not isinstance(manifest.get("tasks"), list)
        ):
            return finish(PreflightReasonCode.MANIFEST_INVALID, "tracked manifest identity or schema is invalid")
        task_ids = [item.get("pilot_task_id") for item in manifest["tasks"] if isinstance(item, Mapping)]
        if len(task_ids) != len(manifest["tasks"]) or len(task_ids) != len(set(task_ids)):
            return finish(PreflightReasonCode.MANIFEST_INVALID, "tracked manifest task IDs are invalid or duplicated")

        authority = manifest.get("authority")
        if isinstance(authority, Mapping):
            revisions["manifest"] = _text(authority.get("official_repository_revision"))
        dataset_records = gate.get("dataset_records")
        if isinstance(dataset_records, list) and dataset_records:
            dataset_revisions = [_text(item.get("revision")) for item in dataset_records if isinstance(item, Mapping)]
            revisions["gate_dataset"] = dataset_revisions[0] if dataset_revisions else None
            if revisions["manifest"] and any(revision != revisions["manifest"] for revision in dataset_revisions):
                return finish(PreflightReasonCode.AUTHORITY_REVISION_MISMATCH, "manifest and canonical gate authority revisions differ")
        if revisions["manifest"] and revisions["gate_dataset"] and revisions["manifest"] != revisions["gate_dataset"]:
            return finish(PreflightReasonCode.AUTHORITY_REVISION_MISMATCH, "manifest and canonical gate authority revisions differ")

        manifest_tasks = manifest.get("tasks")
        if not isinstance(manifest_tasks, list):
            return finish(PreflightReasonCode.MANIFEST_INVALID, "manifest task list is invalid")
        task_matches = [item for item in manifest_tasks if isinstance(item, Mapping) and item.get("pilot_task_id") == task_value]
        if len(task_matches) == 0:
            return finish(PreflightReasonCode.UNKNOWN_TASK, "task ID is not present in the tracked manifest")
        if len(task_matches) != 1:
            return finish(PreflightReasonCode.MANIFEST_INVALID, "task ID is duplicated in the tracked manifest")
        task = task_matches[0]
        licensing = task.get("licensing") if isinstance(task, Mapping) else None
        if not isinstance(licensing, Mapping):
            return finish(PreflightReasonCode.MANIFEST_INVALID, "task licensing metadata is invalid")
        project_name = ((task.get("bugsinpy") or {}).get("project") if isinstance(task.get("bugsinpy"), Mapping) else None)
        project_records = gate.get("project_records")
        project_record = next((item for item in project_records or [] if isinstance(item, Mapping) and item.get("project") == project_name), None)
        dataset_record = next((item for item in dataset_records or [] if isinstance(item, Mapping) and item.get("id") == licensing.get("dataset_record_id")), None)
        manifest_verdict = _text(licensing.get("dataset_verdict"))
        dataset_verdict = _text((dataset_record or {}).get("verdict"))
        project_verdict = _text((project_record or {}).get("verdict")) or _text(licensing.get("project_verdict"))
        task_verdict = _text(licensing.get("task_verdict"))
        formal = _text(licensing.get("formal_license_status")) or _text((dataset_record or {}).get("formal_license_status"))
        redistribution = _text(licensing.get("redistribution_verdict")) or _text((dataset_record or {}).get("redistribution_verdict"))
        private_use = _text(licensing.get("private_local_research_use_verdict")) or _text((dataset_record or {}).get("private_local_research_use_verdict"))
        operational = _text(licensing.get("operational_execution_gate")) or _text((dataset_record or {}).get("operational_execution_gate"))
        common = dict(
            manifest_verdict=manifest_verdict,
            dataset_verdict=dataset_verdict,
            project_verdict=project_verdict,
            task_verdict=task_verdict,
            formal_license_status=formal,
            redistribution_verdict=redistribution,
            private_use_verdict=private_use,
            operational_execution_gate=operational,
        )

        try:
            validator = _validator_function()
            validator(manifest, gate)
        except Exception as exc:
            message = str(exc)
            if (
                isinstance(task_verdict, str)
                and isinstance(dataset_verdict, str)
                and task_verdict in VERDICT_RANK
                and dataset_verdict in VERDICT_RANK
                and VERDICT_RANK[task_verdict] > VERDICT_RANK[dataset_verdict]
            ):
                return finish(PreflightReasonCode.TASK_VERDICT_EXCEEDS_AUTHORITY, "task verdict exceeds a controlling authority", **common)
            return finish(PreflightReasonCode.LICENSE_VALIDATOR_FAILED, "tracked licensing validator failed", **common, validation={"status": "FAIL", "validator": "FAIL"})

        validation = {"status": "PASS", "manifest": "PASS", "canonical_gate": "PASS", "license_validator": "PASS"}
        try:
            authority_snapshot = self._snapshot(task, dataset_verdict, task_verdict, operational, validation_status="PASS")
            allowed_source_pairs, allowed_metadata_paths = self._scope_for(manifest, task, operation_value, revisions["manifest"] or "")
        except (OSError, ValueError):
            return finish(PreflightReasonCode.RESOURCE_SCOPE_INVALID, "validated task resource scope is invalid", validation=validation, **common)
        if operation_value in {BugsInPyOperation.INSPECT_METADATA.value, BugsInPyOperation.PACKAGE_METADATA_EVIDENCE.value}:
            code = PreflightReasonCode.ALLOWED_METADATA_INSPECTION if operation_value == BugsInPyOperation.INSPECT_METADATA.value else PreflightReasonCode.ALLOWED_SANITIZED_METADATA_EVIDENCE
            return finish(code, "tracked authorities validate; metadata-only operation is allowed", decision="ALLOW", validation=validation, **common)
        if dataset_verdict == "BLOCKED":
            return finish(PreflightReasonCode.DATASET_VERDICT_BLOCKED, "dataset verdict is BLOCKED", validation=validation, **common)
        if project_verdict == "BLOCKED":
            return finish(PreflightReasonCode.PROJECT_PERMISSION_REQUIRED, "project permission verdict is BLOCKED", validation=validation, **common)
        if task_verdict == "BLOCKED":
            return finish(PreflightReasonCode.TASK_VERDICT_BLOCKED, "task verdict is BLOCKED", validation=validation, **common)
        if operational == "BLOCKED":
            return finish(PreflightReasonCode.OPERATIONAL_GATE_BLOCKED, "operational execution gate is BLOCKED", validation=validation, **common)
        if operation_value in AFFIRMATIVE_OPERATIONS:
            requirements = (
                (dataset_verdict, PreflightReasonCode.DATASET_PERMISSION_REQUIRED, "dataset permission verdict"),
                (project_verdict, PreflightReasonCode.PROJECT_PERMISSION_REQUIRED, "project permission verdict"),
                (task_verdict, PreflightReasonCode.TASK_PERMISSION_REQUIRED, "task permission verdict"),
                (operational, PreflightReasonCode.OPERATIONAL_PERMISSION_REQUIRED, "operational execution gate"),
                (formal, PreflightReasonCode.FORMAL_LICENSE_PERMISSION_REQUIRED, "formal license status"),
                (private_use, PreflightReasonCode.PRIVATE_USE_PERMISSION_REQUIRED, "private local research-use verdict"),
            )
            for value, code, label in requirements:
                if not _affirmative(value):
                    return finish(code, f"{label} is not affirmatively clear", validation=validation, **common)
        if operator_value != "approved":
            return finish(PreflightReasonCode.OPERATOR_AUTHORIZATION_REQUIRED, "explicit operator authorization is required", validation=validation, **common)
        if operation_value in DEPENDENCY_OPERATIONS and dependency != "ready":
            return finish(PreflightReasonCode.DEPENDENCY_NOT_READY, "dependency readiness is not affirmatively clear", validation=validation, **common)
        if operation_value in CONTAINMENT_OPERATIONS and containment != "ready":
            return finish(PreflightReasonCode.CONTAINMENT_NOT_READY, "containment readiness is not affirmatively clear", validation=validation, **common)
        return finish(PreflightReasonCode.OPERATION_ALLOWED, "affirmative authorities and explicit readiness allow the bounded operation", decision="ALLOW", validation=validation, **common)


def default_preflight(**kwargs: Any) -> MetadataPreflightDecision:
    """Convenience wrapper using the tracked authority defaults."""
    task_id = kwargs.pop("task_id", None)
    operation = kwargs.pop("operation", None)
    return BugsInPyMetadataPreflight().decide(task_id, operation, **kwargs)
