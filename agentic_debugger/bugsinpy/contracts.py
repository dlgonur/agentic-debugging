"""BugsInPy contract records: gates, preflight facts, manifest, receipts.

This module owns the immutable BugsInPy pilot vocabulary beneath the
adapter: the manifest/mapping error classes, the gate vocabulary
(:class:`GateName`/:class:`GateStatus`/:class:`GateResult`), the
preflight facts/report records, normalized command/smoke/receipt
records, the :class:`SourceAcquirer` protocol, and the
:class:`BugsInPyManifest` record.

The adapter/mapping authority (:class:`BugsInPyAdapter`), the external
workspace/source acquisition, and the preflight gates live in their own
modules and import these contracts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from agentic_debugger.bugsinpy.metadata_preflight import (
    AuthoritySnapshot,
    BugsInPyOperationPermit,
    MetadataPreflightDecision,
)
from agentic_debugger.evaluation.task_schema import DebugTask, TaskSource
from agentic_debugger.evaluation.verifier import EvaluationResult
from agentic_debugger.runtime.execution import PdbLaunchPlan, VerifiedExecutionContext

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_MANIFEST_SCHEMA = "1.0"
_MANIFEST_ID = "bugsinpy-pilot-eligibility-v1"
_DATASET = "BugsInPy"
_DEFAULT_TIMEOUT = 60
_EXTERNAL_ROOT_MARKER = ".bugsinpy-external-workspace-owner"
_SHELL_TOKENS = {"&&", "||", ";", "|", ">", ">>", "<", "`"}
_UNKNOWN = {"unknown", "unverified", "gated", "possible"}
_RECEIPT_ISSUER = object()


class ManifestValidationError(ValueError):
    """The accepted manifest is malformed or unsupported."""


class TaskMappingError(ValueError):
    """A selected entry cannot be represented safely by ``DebugTask``."""


class GateName(str, Enum):
    MANIFEST_VALID = "manifest_valid"
    SUPPORTED_PLATFORM = "supported_platform"
    PINNED_UPSTREAM_SOURCE = "pinned_upstream_source"
    PROJECT_LICENSE_REVIEW = "project_license_review"
    PYTHON_RUNTIME_AVAILABLE = "python_runtime_available"
    DEPENDENCY_INSTALL_BOUNDARY = "dependency_install_boundary"
    TEST_COMMAND_AVAILABILITY = "test_command_availability"
    CONTAINMENT_READY = "containment_ready"
    WORKSPACE_CLEANUP_READY = "workspace_cleanup_ready"
    TARGET_ANNOTATION_REVIEW = "target_annotation_review"
    PDB_PLANNING = "pdb_planning"


class GateStatus(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class GateResult:
    name: GateName
    status: GateStatus
    reason: str

    def to_mapping(self) -> dict[str, str]:
        return {"name": self.name.value, "status": self.status.value, "reason": self.reason}


@dataclass(frozen=True)
class PreflightFacts:
    """Explicit operator/environment facts.  Missing facts never pass a gate."""

    platform: Optional[str] = None
    operator_authorization_state: str = "absent"
    python_executable: Optional[str] = None
    python_version: Optional[str] = None
    python_available: bool = False
    pinned_source_verified: bool = False
    license_reviewed: bool = False
    dependency_install_boundary_ready: bool = False
    test_command_available: bool = False
    containment_ready: bool = False
    workspace_cleanup_ready: bool = False
    target_annotation_reviewed: bool = False
    external_parent: Optional[str] = None
    execution_context: Optional[VerifiedExecutionContext] = None
    pdb_launch_plan: Optional[PdbLaunchPlan] = None


@dataclass(frozen=True)
class PreflightReport:
    task_id: str
    manifest_fingerprint: str
    gates: tuple[GateResult, ...]

    @property
    def authorized(self) -> bool:
        return all(gate.status is GateStatus.PASS for gate in self.gates)

    @property
    def blocked_gates(self) -> tuple[str, ...]:
        return tuple(gate.name.value for gate in self.gates if gate.status is not GateStatus.PASS)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "manifest_fingerprint": self.manifest_fingerprint,
            "authorized": self.authorized,
            "blocked_gates": list(self.blocked_gates),
            "gates": [gate.to_mapping() for gate in self.gates],
        }


@dataclass(frozen=True)
class NormalizedCommands:
    baseline_argv: tuple[str, ...]
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    selected_suite_argv: tuple[str, ...]
    official_full_suite_argv: Optional[tuple[str, ...]]
    cwd: str
    timeout_seconds: int
    environment: Mapping[str, str]
    pythonpath: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "baseline_argv": list(self.baseline_argv),
            "fail_to_pass": list(self.fail_to_pass),
            "pass_to_pass": list(self.pass_to_pass),
            "selected_suite_argv": list(self.selected_suite_argv),
            "official_full_suite_argv": list(self.official_full_suite_argv) if self.official_full_suite_argv else None,
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "environment": dict(sorted(self.environment.items())),
            "pythonpath": list(self.pythonpath),
        }


@dataclass(frozen=True)
class SmokeEvidence:
    task_id: str
    verdict: str
    preflight: PreflightReport | MetadataPreflightDecision
    commands: Optional[NormalizedCommands]
    evaluation: Optional[EvaluationResult]
    cleanup_attempted: bool
    cleanup_succeeded: bool
    cleanup_error: Optional[str]
    failure_kind: Optional[str] = None
    execution_error: Optional[str] = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "verdict": self.verdict,
            "preflight": self.preflight.to_mapping(),
            "commands": self.commands.to_mapping() if self.commands else None,
            "evaluation": self.evaluation.semantic_mapping() if self.evaluation else None,
            "cleanup_attempted": self.cleanup_attempted,
            "cleanup_succeeded": self.cleanup_succeeded,
            "cleanup_error": self.cleanup_error,
            "failure_kind": self.failure_kind,
            "execution_error": self.execution_error,
        }


class AcquiredSourceReceipt:
    """Issuer-bound receipt for one successfully materialized source root."""

    __slots__ = ("_task_id", "_url", "_revision", "_root", "_authority_snapshot", "_run_id", "_acquisition_permit", "_issuer", "_initialized")

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_initialized", False):
            raise AttributeError("acquired source receipts are immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        task_id: str,
        url: str,
        revision: str,
        root: Path,
        authority_snapshot: AuthoritySnapshot,
        run_id: str,
        acquisition_permit: BugsInPyOperationPermit,
        *,
        _issuer: object,
    ) -> None:
        if _issuer is not _RECEIPT_ISSUER:
            raise TypeError("acquired source receipts are issued by GitSourceAcquirer only")
        object.__setattr__(self, "_task_id", task_id)
        object.__setattr__(self, "_url", url.rstrip("/"))
        object.__setattr__(self, "_revision", revision)
        object.__setattr__(self, "_root", root)
        object.__setattr__(self, "_authority_snapshot", authority_snapshot)
        object.__setattr__(self, "_run_id", run_id)
        object.__setattr__(self, "_acquisition_permit", acquisition_permit)
        object.__setattr__(self, "_issuer", _issuer)
        object.__setattr__(self, "_initialized", True)

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def url(self) -> str:
        return self._url

    @property
    def revision(self) -> str:
        return self._revision

    @property
    def root(self) -> Path:
        return self._root

    @property
    def authority_snapshot(self) -> AuthoritySnapshot:
        return self._authority_snapshot

    @property
    def run_id(self) -> str:
        return self._run_id

    def _valid_for(self, *, task_id: str, url: str, revision: str, authority_snapshot: AuthoritySnapshot, acquisition_permit: BugsInPyOperationPermit) -> bool:
        return (
            self._issuer is _RECEIPT_ISSUER
            and self._task_id == task_id
            and self._url == url.rstrip("/")
            and self._revision == revision
            and self._authority_snapshot == authority_snapshot
            and self._acquisition_permit is acquisition_permit
            and acquisition_permit.operation == "acquire_source"
            and acquisition_permit.task_id == task_id
            and acquisition_permit.run_id == self._run_id
        )


class SourceAcquirer(Protocol):
    def acquire(
        self,
        url: str,
        revision: str,
        destination: Path,
        *,
        task_id: str,
        preflight_decision: MetadataPreflightDecision,
        permit: BugsInPyOperationPermit,
    ) -> AcquiredSourceReceipt:
        """Acquire exactly one pinned Git revision into ``destination``."""

    def read_gold_patch(
        self,
        framework_source: AcquiredSourceReceipt,
        metadata_path: str,
        *,
        task_id: str,
        preflight_decision: MetadataPreflightDecision,
        permit: BugsInPyOperationPermit,
    ) -> str:
        """Read the evaluator-only official patch from the framework snapshot."""


class BugsInPyManifest:
    """Strict, immutable-in-use view of the accepted JSON manifest."""

    def __init__(self, data: Mapping[str, Any], fingerprint: str) -> None:
        self._data = copy.deepcopy(dict(data))
        self.fingerprint = fingerprint
        self._tasks = {entry["pilot_task_id"]: entry for entry in self._data["tasks"]}

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "BugsInPyManifest":
        try:
            raw = Path(path).read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestValidationError(f"manifest cannot be loaded: {exc}") from exc
        _validate_manifest(data)
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return cls(data, hashlib.sha256(canonical.encode("utf-8")).hexdigest())

    @property
    def manifest_id(self) -> str:
        return self._data["manifest_id"]

    @property
    def authority_revision(self) -> str:
        return self._data["authority"]["official_repository_revision"]

    @property
    def tasks(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(copy.deepcopy(self._tasks[key]) for key in sorted(self._tasks))

    def select(self, pilot_task_id: str) -> Mapping[str, Any]:
        if not isinstance(pilot_task_id, str) or pilot_task_id not in self._tasks:
            raise TaskMappingError(f"unknown BugsInPy pilot task ID: {pilot_task_id!r}")
        return copy.deepcopy(self._tasks[pilot_task_id])

    def to_mapping(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)


def _validate_manifest(data: Any) -> None:
    if not isinstance(data, dict):
        raise ManifestValidationError("manifest root must be an object")
    if data.get("manifest_schema_version") != _MANIFEST_SCHEMA:
        raise ManifestValidationError("unsupported manifest schema version")
    if data.get("manifest_id") != _MANIFEST_ID or data.get("dataset") != _DATASET:
        raise ManifestValidationError("manifest identity is unsupported")
    authority = data.get("authority")
    if not isinstance(authority, dict) or not _SHA1.fullmatch(str(authority.get("official_repository_revision", ""))):
        raise ManifestValidationError("authority lacks a pinned official revision")
    if not isinstance(authority.get("official_repository"), str) or not authority["official_repository"].startswith("https://"):
        raise ManifestValidationError("authority official repository is invalid")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 8:
        raise ManifestValidationError("accepted pilot manifest must contain exactly eight tasks")
    ids: set[str] = set()
    for index, entry in enumerate(tasks):
        try:
            _validate_entry(entry)
        except ManifestValidationError as exc:
            raise ManifestValidationError(f"tasks[{index}]: {exc}") from exc
        task_id = entry["pilot_task_id"]
        if task_id in ids:
            raise ManifestValidationError(f"duplicate pilot task ID: {task_id}")
        ids.add(task_id)


def _validate_entry(entry: Any) -> None:
    if not isinstance(entry, dict):
        raise ManifestValidationError("task entry must be an object")
    required = {"pilot_task_id", "bugsinpy", "environment", "reproduction", "regression_oracle", "bug_family", "debugger_relevance", "licensing", "eligibility"}
    if not required.issubset(entry):
        raise ManifestValidationError(f"missing required task fields: {sorted(required - set(entry))}")
    if not isinstance(entry["pilot_task_id"], str) or not _TASK_ID.fullmatch(entry["pilot_task_id"]):
        raise ManifestValidationError("invalid pilot_task_id")
    b = entry["bugsinpy"]
    if not isinstance(b, dict) or not isinstance(b.get("project"), str) or not isinstance(b.get("project_url"), str):
        raise ManifestValidationError("bugsinpy project metadata is malformed")
    for field in ("buggy_revision", "fixed_revision"):
        if not _SHA1.fullmatch(str(b.get(field, ""))):
            raise ManifestValidationError(f"{field} is not a full pinned revision")
    if not isinstance(b.get("metadata_paths"), list) or not all(isinstance(item, str) and item.startswith("projects/") for item in b["metadata_paths"]):
        raise ManifestValidationError("metadata_paths must stay inside projects/")
    gold_paths = [item for item in b["metadata_paths"] if item.replace("\\", "/").endswith("bug_patch.txt")]
    if len(gold_paths) != 1:
        raise ManifestValidationError("metadata_paths must identify exactly one official bug_patch.txt")
    env = entry["environment"]
    if not isinstance(env, dict) or not _VERSION.fullmatch(str(env.get("python_version", ""))):
        raise ManifestValidationError("environment.python_version is malformed")
    reproduction = entry["reproduction"]
    if not isinstance(reproduction, dict) or not isinstance(reproduction.get("argv"), list) or not reproduction["argv"]:
        raise ManifestValidationError("reproduction.argv is malformed")
    regression = entry["regression_oracle"]
    if not isinstance(regression, dict) or len(regression.get("fail_to_pass", [])) != 1 or len(regression.get("pass_to_pass_candidates", [])) < 2:
        raise ManifestValidationError("the v1 test contract requires one F2P and at least two P2P candidates")
    if not all(isinstance(item, str) and item for item in regression["fail_to_pass"] + regression["pass_to_pass_candidates"]):
        raise ManifestValidationError("test node IDs must be non-empty strings")
    debug = entry["debugger_relevance"]
    if not isinstance(debug, dict) or not isinstance(debug.get("candidate_changed_files"), list) or not debug["candidate_changed_files"]:
        raise ManifestValidationError("debugger candidate changed files are required")
    if not isinstance(entry["licensing"], dict) or not isinstance(entry["eligibility"], dict):
        raise ManifestValidationError("licensing and eligibility metadata are malformed")


def _normalize_pytest_command(command: Any, node: Optional[str]) -> list[str]:
    if isinstance(command, str):
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as exc:
            raise TaskMappingError(f"malformed command: {exc}") from exc
    elif isinstance(command, list) and all(isinstance(item, str) and item for item in command):
        argv = list(command)
    else:
        raise TaskMappingError("pytest command must be a non-empty argv list or shell-free string")
    if not argv or any(token in _SHELL_TOKENS for token in argv) or any("$" in token or "\x00" in token for token in argv):
        raise TaskMappingError("shell expansion/operators are not allowed in benchmark commands")
    if "pytest" not in argv and not any(token.endswith("pytest") for token in argv):
        raise TaskMappingError("only pytest-compatible BugsInPy commands are supported")
    if node is not None:
        node_positions = [index for index, token in enumerate(argv) if "::" in token]
        if node_positions:
            argv[node_positions[0]] = node
            for index in reversed(node_positions[1:]):
                argv.pop(index)
        else:
            argv.append(node)
    if "-p" not in argv:
        argv.extend(["-p", "no:cacheprovider"])
    if "-q" not in argv and "--quiet" not in argv:
        argv.append("-q")
    return argv


def _command_with_nodes(base: Sequence[str], nodes: Sequence[str]) -> list[str]:
    argv = [token for token in base if "::" not in token]
    insert_at = len(argv)
    for flag in ("-p", "-q", "--quiet"):
        if flag in argv:
            insert_at = min(insert_at, argv.index(flag))
    return argv[:insert_at] + list(nodes) + argv[insert_at:]


def _is_unknown(value: Any) -> bool:
    return not isinstance(value, str) or any(word in value.lower() for word in _UNKNOWN)


def _reviewed_cwd(entry: Mapping[str, Any]) -> Optional[str]:
    value = entry["reproduction"].get("cwd")
    if not isinstance(value, str) or _is_unknown(value) or "unverified" in value.lower() or "project root" in value.lower():
        return None
    return _safe_relative(value)


def _reviewed_pythonpath(entry: Mapping[str, Any]) -> Optional[tuple[str, ...]]:
    value = entry["environment"].get("pythonpath")
    if not isinstance(value, str) or _is_unknown(value):
        return None
    values = tuple(item.strip().replace("\\", "/").strip("/") for item in value.split(os.pathsep) if item.strip())
    if not values or any(_safe_relative(item) is None for item in values):
        return None
    return values


def _reviewed_environment(entry: Mapping[str, Any]) -> Optional[dict[str, str]]:
    value = entry["environment"].get("reviewed_environment")
    if not isinstance(value, Mapping) or not value:
        return None
    if not all(isinstance(key, str) and isinstance(item, str) and key and item for key, item in value.items()):
        return None
    if any(any(secret in key.upper() for secret in ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "CREDENTIAL")) for key in value):
        return None
    return dict(value)


def _safe_relative(value: str) -> Optional[str]:
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")):
        return None
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return None
    if ".." in value.replace("\\", "/").split("/"):
        return None
    return value.replace("\\", "/")


def _nonempty_equal(actual: Any, expected: Any) -> bool:
    return isinstance(expected, str) and bool(expected) and actual == expected
