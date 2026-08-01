"""Fail-closed BugsInPy adapter for the bounded no-model smoke.

The adapter consumes the accepted metadata manifest but does not treat metadata
as execution authorization.  External source, environments, logs, caches, and
benchmark outputs are owned by :class:`ExternalWorkspace` and remain outside
the repository.  Once a task is materialized, it is represented by the
repository's existing ``DebugTask`` contract and evaluated by the existing
``EvaluationVerifier``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform as platform_module
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from agentic_debugger.evaluation.task_schema import DebugTask, TaskSource
from agentic_debugger.runtime.execution import PdbLaunchPlan, VerifiedExecutionContext
from agentic_debugger.evaluation.verifier import EvaluationResult, EvaluationVerifier
from agentic_debugger.bugsinpy.metadata_preflight import (
    AuthoritySnapshot,
    DEFAULT_GATE_PATH,
    DEFAULT_MANIFEST_PATH,
    BugsInPyMetadataPreflight,
    BugsInPyOperationPermit,
    MetadataPreflightDecision,
    PreflightAuthorizationError,
)


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


class BugsInPyAdapter:
    """Manifest loader, command normalizer, preflight, and DebugTask bridge."""

    def __init__(
        self,
        manifest: BugsInPyManifest,
        *,
        manifest_path: Optional[str | os.PathLike[str]] = None,
        gate_path: str | os.PathLike[str] = DEFAULT_GATE_PATH,
    ) -> None:
        self.manifest = manifest
        self.metadata_boundary = BugsInPyMetadataPreflight(
            manifest_path=manifest_path or DEFAULT_MANIFEST_PATH,
            gate_path=gate_path,
        )

    @classmethod
    def from_manifest(cls, path: str | os.PathLike[str]) -> "BugsInPyAdapter":
        return cls(BugsInPyManifest.load(path), manifest_path=path)

    def metadata_preflight(
        self,
        pilot_task_id: Optional[str],
        operation: Optional[str],
        **kwargs: Any,
    ) -> MetadataPreflightDecision:
        """Run the shared authority-backed boundary without touching source."""
        return self.metadata_boundary.decide(pilot_task_id, operation, **kwargs)

    def select(self, pilot_task_id: str) -> Mapping[str, Any]:
        return self.manifest.select(pilot_task_id)

    def source_provenance(self, entry: Mapping[str, Any]) -> dict[str, str]:
        project = entry["bugsinpy"]
        return {
            "dataset": _DATASET,
            "manifest_id": self.manifest.manifest_id,
            "manifest_fingerprint": self.manifest.fingerprint,
            "upstream_repository": self.manifest.to_mapping()["authority"]["official_repository"],
            "upstream_revision": self.manifest.authority_revision,
            "project": project["project"],
            "bug_id": str(project["bug_id"]),
            "buggy_revision": project["buggy_revision"],
            "fixed_revision": project["fixed_revision"],
        }

    def normalize(self, entry: Mapping[str, Any]) -> NormalizedCommands:
        reproduction = entry["reproduction"]
        bugsinpy = entry["bugsinpy"]
        regression = entry["regression_oracle"]
        f2p = tuple(regression["fail_to_pass"])
        p2p = tuple(regression["pass_to_pass_candidates"])
        baseline = _normalize_pytest_command(reproduction["argv"], f2p[0])
        all_nodes = f2p + p2p
        selected = _command_with_nodes(baseline, all_nodes)
        official = regression["full_project_suite"]
        official_argv = None if _is_unknown(official) else tuple(_normalize_pytest_command(official, None))
        reviewed_pythonpath = _reviewed_pythonpath(entry)
        return NormalizedCommands(
            baseline_argv=tuple(baseline),
            fail_to_pass=f2p,
            pass_to_pass=p2p,
            selected_suite_argv=tuple(selected),
            official_full_suite_argv=official_argv,
            cwd=_reviewed_cwd(entry) or ".",
            timeout_seconds=_DEFAULT_TIMEOUT,
            environment={
                "platform": "linux-reference-required",
                "python_version": entry["environment"]["python_version"],
                "network": "denied-during-execution",
                "project": bugsinpy["project"],
            },
            pythonpath=reviewed_pythonpath or (),
        )

    def preflight(
        self,
        pilot_task_id: str,
        facts: Optional[PreflightFacts] = None,
        *,
        target_symbols: Optional[Sequence[str]] = None,
        repository_root: Optional[str] = None,
    ) -> PreflightReport:
        facts = facts or PreflightFacts()
        entry = self.select(pilot_task_id)
        commands = self.normalize(entry)
        gates = (
            GateResult(GateName.MANIFEST_VALID, GateStatus.PASS, "manifest loaded and schema-validated"),
            _platform_gate(facts),
            _source_gate(self.manifest, entry, facts),
            _license_gate(entry, facts),
            _python_gate(entry, facts),
            _dependency_gate(self.manifest, pilot_task_id, entry, facts),
            _test_command_gate(commands, facts),
            _containment_gate(facts, repository_root),
            _cleanup_gate(facts),
            _target_gate(entry, facts, target_symbols),
            _pdb_gate(entry, commands, facts),
        )
        return PreflightReport(pilot_task_id, self.manifest.fingerprint, gates)

    def to_debug_task(
        self,
        entry: Mapping[str, Any],
        source: TaskSource,
        *,
        target_symbols: Optional[Sequence[str]] = None,
    ) -> DebugTask:
        if not isinstance(source, TaskSource) or source.kind != "external":
            raise TaskMappingError("BugsInPy tasks require an external source binding")
        symbols = _reviewed_symbols(entry, target_symbols)
        commands = self.normalize(entry)
        changed_files = tuple(entry["debugger_relevance"]["candidate_changed_files"])
        project = entry["bugsinpy"]["project"]
        bug_id = entry["bugsinpy"]["bug_id"]
        family = entry["bug_family"]["label"]
        mapping = {
            "schema_version": "1.0",
            "task_id": entry["pilot_task_id"],
            "title": f"BugsInPy {project} bug {bug_id}",
            "description": (
                f"Repair the regression exposed by {commands.fail_to_pass[0]} "
                f"in the pinned {project} buggy revision. Use the existing "
                "test, patch, and verifier workflow."
            ),
            "language": "python",
            "fixture_path": source.path,
            "source": source.to_mapping(),
            "reproduction": {
                "argv": list(commands.baseline_argv),
                "cwd": commands.cwd,
                "timeout_seconds": commands.timeout_seconds,
                "expected_exit_code": 1,
            },
            "tests": {
                "fail_to_pass": list(commands.fail_to_pass),
                "pass_to_pass": list(commands.pass_to_pass),
                # The manifest has no verified broader suite.  This is the
                # exact selected regression suite accepted by schema v1.
                "full_suite_argv": list(commands.selected_suite_argv),
                "timeout_seconds": commands.timeout_seconds,
            },
            "constraints": {
                "allowed_write_paths": list(changed_files),
                "denied_write_paths": [
                    "tests",
                    "task.json",
                    "requirements.txt",
                    "setup.py",
                    "pyproject.toml",
                    "tox.ini",
                    ".git",
                ],
                "network_allowed": False,
                "external_services_allowed": False,
                "max_patch_attempts": 1,
                "max_test_runs": 10,
                "max_pdb_observations": 10,
            },
            "oracle": {
                "bug_category": family,
                "target_files": list(changed_files),
                "target_symbols": list(symbols),
                "root_cause_summary": entry["bug_family"]["basis"],
                "runtime_evidence_hint": entry["debugger_relevance"]["expected_relevance"],
            },
            "tags": ["bugsinpy", project, "no-model-smoke", "selected-suite-only"],
        }
        return DebugTask.from_mapping(mapping)


class ExternalWorkspace:
    """Owned disposable root for source, environment, logs, and outputs."""

    def __init__(self, root: Path, owner_token: str) -> None:
        self.root = root
        self.owner_token = owner_token
        self._cleaned = False

    @classmethod
    def create(cls, parent_dir: str | os.PathLike[str], *, repository_root: Optional[str] = None, containment_root: Optional[str] = None) -> "ExternalWorkspace":
        parent = Path(parent_dir).resolve()
        if not parent.is_dir():
            raise OSError(f"external workspace parent does not exist: {parent}")
        if repository_root is not None:
            repo = Path(repository_root).resolve()
            if _is_within(parent, repo):
                raise OSError("external workspace parent is inside the tracked repository")
        containment = Path(containment_root).resolve() if containment_root else None
        if containment is not None and (_is_filesystem_root(containment) or not _is_within(parent, containment)):
            raise OSError("external workspace parent is outside the declared containment root")
        for _ in range(64):
            root = parent / f"bugsinpy_case_{uuid.uuid4().hex}"
            try:
                root.mkdir()
                if containment is not None and not _is_within(root, containment):
                    shutil.rmtree(root)
                    raise OSError("created external workspace escaped containment root")
                token = uuid.uuid4().hex
                (root / _EXTERNAL_ROOT_MARKER).write_text(token + "\n", encoding="utf-8")
                return cls(root, token)
            except FileExistsError:
                continue
        raise OSError("external workspace collision limit reached")

    @property
    def source_dir(self) -> Path:
        return self.root / "sources"

    @property
    def verifier_workspace_parent(self) -> Path:
        return self.root / "verifier-workspaces"

    def assert_contained(self, path: Path) -> None:
        if not _is_within(path.resolve(), self.root.resolve()):
            raise OSError("external workspace path escapes owned root")

    def materialize_project(self, source: Path, project_name: str, provenance: Mapping[str, str]) -> TaskSource:
        if not source.is_dir() or source.is_symlink():
            raise OSError("project source must be a real directory")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", project_name):
            raise OSError("invalid external project name")
        destination = source.resolve()
        if not _is_within(destination, self.source_dir.resolve()):
            raise OSError("project source must be inside the owned source directory")
        if destination.name != project_name:
            raise OSError("project source name does not match manifest project")
        self.verifier_workspace_parent.mkdir(parents=True, exist_ok=True)
        return TaskSource("external", "sources/" + project_name, dict(provenance))

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        marker = self.root / _EXTERNAL_ROOT_MARKER
        try:
            token = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if token != self.owner_token:
            return
        if self.root.is_symlink() or not self.root.is_dir():
            return
        shutil.rmtree(self.root)

    def __enter__(self) -> "ExternalWorkspace":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.cleanup()


class GitSourceAcquirer:
    """Pinned Git acquisition.  It is only callable after preflight auth."""

    def __init__(self, *, preflight_boundary: Optional[BugsInPyMetadataPreflight] = None) -> None:
        self.preflight_boundary = preflight_boundary or BugsInPyMetadataPreflight()

    def _require_permit(
        self,
        *,
        task_id: str,
        operation: str,
        preflight_decision: MetadataPreflightDecision,
        permit: BugsInPyOperationPermit,
    ) -> None:
        if not isinstance(preflight_decision, MetadataPreflightDecision) or not preflight_decision.allowed:
            raise PreflightAuthorizationError("BugsInPy operation requires an ALLOW metadata decision")
        if preflight_decision.task_id != task_id or preflight_decision.requested_operation != operation:
            raise PreflightAuthorizationError("BugsInPy operation permit has the wrong task or operation")
        if preflight_decision.permit is not permit or not isinstance(permit, BugsInPyOperationPermit):
            raise PreflightAuthorizationError("BugsInPy operation permit is not issuer-bound to the decision")
        try:
            fresh = self.preflight_boundary.decide(
                task_id,
                operation,
                operator_authorization_state="approved",
                containment_readiness=True,
                dependency_readiness=True,
                evidence_handling="unspecified",
            )
        except Exception as exc:
            raise PreflightAuthorizationError("BugsInPy operation permit authority is unavailable") from exc
        if not fresh.allowed or fresh.authority_snapshot is None:
            raise PreflightAuthorizationError("BugsInPy operation permit authority is revoked")
        if preflight_decision.authority_snapshot != fresh.authority_snapshot:
            raise PreflightAuthorizationError("BugsInPy operation decision authority snapshot is stale or mismatched")
        if not fresh.authority_snapshot.canonical_paths:
            raise PreflightAuthorizationError("production acquisition requires canonical tracked authority paths")
        if not permit._valid_for(
            task_id=task_id,
            operation=operation,
            authority_snapshot=fresh.authority_snapshot,
            run_id=preflight_decision.run_id,
        ):
            raise PreflightAuthorizationError("BugsInPy operation permit is stale or mismatched")

    def acquire(
        self,
        url: str,
        revision: str,
        destination: Path,
        *,
        task_id: Optional[str] = None,
        preflight_decision: Optional[MetadataPreflightDecision] = None,
        permit: Optional[BugsInPyOperationPermit] = None,
    ) -> AcquiredSourceReceipt:
        if not _SHA1.fullmatch(revision):
            raise ValueError("source revision must be a full lowercase Git SHA-1")
        if not isinstance(url, str) or not url.startswith("https://github.com/") or url.endswith(".git"):
            raise ValueError("source URL is not an approved public HTTPS BugsInPy repository")
        self._require_permit(
            task_id=task_id or "",
            operation="acquire_source",
            preflight_decision=preflight_decision,
            permit=permit,
        )
        if (url.rstrip("/"), revision) not in permit.allowed_source_pairs:
            raise PreflightAuthorizationError("source URL and revision are outside the task permit scope")
        if not _owned_external_root(destination.parent):
            raise PermissionError("Git acquisition requires an owned external workspace")
        if not _is_within(destination.resolve(), destination.parent.resolve()):
            raise ValueError("acquisition destination escapes its owned parent")
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _run_git(["-c", "credential.helper=", "clone", "--no-checkout", "--no-tags", url, str(destination)], destination.parent)
        _run_git(["checkout", "--detach", revision], destination)
        actual = _run_git(["rev-parse", "HEAD"], destination).stdout.strip()
        if actual != revision:
            raise RuntimeError(f"pinned revision mismatch: expected {revision}, got {actual}")
        if not isinstance(preflight_decision, MetadataPreflightDecision) or not isinstance(permit, BugsInPyOperationPermit) or preflight_decision.authority_snapshot is None:
            raise PreflightAuthorizationError("successful acquisition has no issuer-bound authority context")
        return AcquiredSourceReceipt(
            task_id,
            url,
            revision,
            destination.resolve(),
            preflight_decision.authority_snapshot,
            preflight_decision.run_id,
            permit,
            _issuer=_RECEIPT_ISSUER,
        )

    def read_gold_patch(
        self,
        framework_source: AcquiredSourceReceipt,
        metadata_path: str,
        *,
        task_id: Optional[str] = None,
        preflight_decision: Optional[MetadataPreflightDecision] = None,
        permit: Optional[BugsInPyOperationPermit] = None,
    ) -> str:
        self._require_permit(
            task_id=task_id or "",
            operation="verify_patch",
            preflight_decision=preflight_decision,
            permit=permit,
        )
        if not isinstance(preflight_decision, MetadataPreflightDecision) or not isinstance(permit, BugsInPyOperationPermit):
            raise PreflightAuthorizationError("patch reading requires issuer-bound authorization")
        snapshot = preflight_decision.authority_snapshot
        expected_revision = snapshot.authority_revisions.get("manifest") if snapshot is not None else None
        if (
            snapshot is None
            or not isinstance(framework_source, AcquiredSourceReceipt)
            or not framework_source._valid_for(
                task_id=task_id or "",
                url="https://github.com/soarsmu/BugsInPy",
                revision=expected_revision or "",
                authority_snapshot=snapshot,
                acquisition_permit=framework_source._acquisition_permit,
            )
            or framework_source.url != "https://github.com/soarsmu/BugsInPy"
            or framework_source.revision != expected_revision
            or framework_source.run_id != framework_source._acquisition_permit.run_id
        ):
            raise PreflightAuthorizationError("patch reading requires the exact issuer-bound BugsInPy framework receipt")
        normalized_metadata_path = metadata_path.replace("\\", "/")
        if normalized_metadata_path not in permit.allowed_metadata_paths:
            raise PreflightAuthorizationError("patch metadata path is outside the task permit scope")
        if not normalized_metadata_path.startswith("projects/") or ".." in Path(normalized_metadata_path).parts:
            raise ValueError("gold patch path escapes the framework snapshot")
        path = (framework_source.root / normalized_metadata_path).resolve()
        if not _is_within(path, framework_source.root.resolve()) or not path.is_file():
            raise FileNotFoundError(path)
        return path.read_text(encoding="utf-8")


class NoModelSmokeRunner:
    """Run one official gold-patch smoke through the existing verifier."""

    def __init__(self, adapter: BugsInPyAdapter, acquirer: SourceAcquirer, verifier_factory: Callable[..., EvaluationVerifier] = EvaluationVerifier) -> None:
        self.adapter = adapter
        self.acquirer = acquirer
        self.verifier_factory = verifier_factory

    def run(
        self,
        pilot_task_id: str,
        *,
        facts: PreflightFacts,
        external_parent: str,
        repository_root: Optional[str] = None,
        target_symbols: Optional[Sequence[str]] = None,
    ) -> SmokeEvidence:
        decision = self.adapter.metadata_preflight(
            pilot_task_id,
            "reproduce_bug",
            operator_authorization_state=facts.operator_authorization_state,
            containment_readiness=facts.containment_ready,
            dependency_readiness=facts.dependency_install_boundary_ready,
        )
        if not decision.allowed:
            return SmokeEvidence(pilot_task_id or "", "REAL_SMOKE_BLOCKED", decision, None, None, False, True, None)

        acquisition_decision = self.adapter.metadata_preflight(
            pilot_task_id,
            "acquire_source",
            operator_authorization_state=facts.operator_authorization_state,
            containment_readiness=facts.containment_ready,
            dependency_readiness=facts.dependency_install_boundary_ready,
        )
        if not acquisition_decision.allowed or acquisition_decision.permit is None:
            return SmokeEvidence(pilot_task_id or "", "REAL_SMOKE_BLOCKED", acquisition_decision, None, None, False, True, None)

        patch_decision = self.adapter.metadata_preflight(
            pilot_task_id,
            "verify_patch",
            operator_authorization_state=facts.operator_authorization_state,
            containment_readiness=facts.containment_ready,
            dependency_readiness=facts.dependency_install_boundary_ready,
        )
        if not patch_decision.allowed or patch_decision.permit is None:
            return SmokeEvidence(pilot_task_id or "", "REAL_SMOKE_BLOCKED", patch_decision, None, None, False, True, None)

        entry = self.adapter.select(pilot_task_id)
        commands = self.adapter.normalize(entry)
        report = self.adapter.preflight(pilot_task_id, facts, target_symbols=target_symbols, repository_root=repository_root)
        if not report.authorized:
            return SmokeEvidence(pilot_task_id, "REAL_SMOKE_BLOCKED", report, commands, None, False, True, None)

        external: Optional[ExternalWorkspace] = None
        cleanup_error: Optional[str] = None
        execution_error: Optional[str] = None
        failure_kind: Optional[str] = None
        result: Optional[EvaluationResult] = None
        phase = "acquisition"
        try:
            if facts.execution_context is None:
                raise RuntimeError("authorized smoke requires a verified execution context")
            external = ExternalWorkspace.create(
                external_parent,
                repository_root=repository_root,
                containment_root=facts.execution_context.containment.root,
            )
            external.assert_contained(external.source_dir)
            external.assert_contained(external.verifier_workspace_parent)
            framework = self.acquirer.acquire(
                "https://github.com/soarsmu/BugsInPy",
                self.adapter.manifest.authority_revision,
                external.source_dir / "bugsinpy-framework",
                task_id=pilot_task_id,
                preflight_decision=acquisition_decision,
                permit=acquisition_decision.permit,
            )
            project = entry["bugsinpy"]
            project_root = self.acquirer.acquire(
                project["project_url"],
                project["buggy_revision"],
                external.source_dir / project["project"],
                task_id=pilot_task_id,
                preflight_decision=acquisition_decision,
                permit=acquisition_decision.permit,
            )
            source = external.materialize_project(project_root.root, project["project"], self.adapter.source_provenance(entry))
            external.assert_contained(project_root.root)
            debug_task = self.adapter.to_debug_task(entry, source, target_symbols=target_symbols)
            gold_paths = [path for path in project["metadata_paths"] if path.replace("\\", "/").endswith("bug_patch.txt")]
            if len(gold_paths) != 1:
                raise TaskMappingError("exactly one gold patch metadata path is required")
            gold_patch = self.acquirer.read_gold_patch(
                framework,
                gold_paths[0],
                task_id=pilot_task_id,
                preflight_decision=patch_decision,
                permit=patch_decision.permit,
            )
            phase = "verifier"
            verifier = self.verifier_factory(
                str(external.root),
                workspace_parent=str(external.verifier_workspace_parent),
                execution_context=facts.execution_context,
            )
            result = verifier.evaluate(debug_task, gold_patch)
            verdict = "REAL_SMOKE_PASSED" if result.status.value == "COMPLETED" else "REAL_SMOKE_FAILED"
        except Exception as exc:
            execution_error = f"{type(exc).__name__}: {exc}"
            failure_kind = f"{phase}_failure"
            verdict = "REAL_SMOKE_FAILED"
        finally:
            cleanup_attempted = external is not None
            cleanup_succeeded = True
            if external is not None:
                root = external.root
                try:
                    external.cleanup()
                    cleanup_succeeded = not root.exists()
                    if not cleanup_succeeded:
                        cleanup_error = cleanup_error or "owned external workspace remains"
                except Exception as exc:
                    cleanup_succeeded = False
                    cleanup_error = cleanup_error or f"{type(exc).__name__}: {exc}"
        return SmokeEvidence(pilot_task_id, verdict, report, commands, result, external is not None, cleanup_succeeded, cleanup_error, failure_kind, execution_error)


def normalize_pytest_commands(entry: Mapping[str, Any]) -> NormalizedCommands:
    """Public functional wrapper used by tests and operator tooling."""
    return BugsInPyAdapter.__new__(BugsInPyAdapter).normalize(entry)  # type: ignore[misc]


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


def _pdb_argv_compatible(argv: Sequence[str], f2p: str, context: VerifiedExecutionContext) -> bool:
    try:
        bound = context.bind_argv(argv)
    except Exception:
        return False
    return bound[:3] == [context.environment.python_executable, "-m", "pytest"] and f2p in bound[3:]


def _platform_gate(facts: PreflightFacts) -> GateResult:
    actual = (facts.platform or platform_module.system()).lower()
    return _boolean_gate(GateName.SUPPORTED_PLATFORM, actual == "linux", f"reference platform is Linux; observed {actual}")


def _source_gate(manifest: BugsInPyManifest, entry: Mapping[str, Any], facts: PreflightFacts) -> GateResult:
    pinned = _SHA1.fullmatch(manifest.authority_revision) and _SHA1.fullmatch(entry["bugsinpy"]["buggy_revision"])
    if not pinned:
        return GateResult(GateName.PINNED_UPSTREAM_SOURCE, GateStatus.BLOCKED, "manifest revision is not a full pin")
    return _boolean_gate(GateName.PINNED_UPSTREAM_SOURCE, facts.pinned_source_verified, "source revision and identity must be verified before checkout")


def _license_gate(entry: Mapping[str, Any], facts: PreflightFacts) -> GateResult:
    license_data = entry["licensing"]
    cleared = facts.license_reviewed and license_data.get("status") not in {"gated", "unknown", "unverified"} and not _is_unknown(license_data.get("underlying_project_license"))
    return _boolean_gate(GateName.PROJECT_LICENSE_REVIEW, cleared, "project and BugsInPy license/notice review is not cleared")


def _python_gate(entry: Mapping[str, Any], facts: PreflightFacts) -> GateResult:
    expected = entry["environment"]["python_version"]
    context = facts.execution_context
    actual = context.environment.python_version if context else "unknown"
    reviewed_cwd = _reviewed_cwd(entry)
    reviewed_pythonpath = _reviewed_pythonpath(entry)
    reviewed_environment = _reviewed_environment(entry)
    ok = (
        context is not None
        and actual == expected
        and Path(context.environment.python_executable).is_file()
        and reviewed_cwd is not None
        and context.environment.project_cwd == reviewed_cwd
        and reviewed_pythonpath is not None
        and context.environment.pythonpath == reviewed_pythonpath
        and reviewed_environment is not None
        and dict(context.environment.environment) == reviewed_environment
    )
    return _boolean_gate(GateName.PYTHON_RUNTIME_AVAILABLE, ok, f"task requires Python {expected}; verified runtime is {actual}")


def _dependency_gate(manifest: BugsInPyManifest, pilot_task_id: str, entry: Mapping[str, Any], facts: PreflightFacts) -> GateResult:
    context = facts.execution_context
    prepared = context.environment.dependencies if context else None
    environment = entry["environment"]
    recipe = environment.get("official_dependency_recipe")
    recipe_sha256 = environment.get("official_dependency_recipe_sha256")
    expected = {
        "pilot_task_id": pilot_task_id,
        "manifest_fingerprint": manifest.fingerprint,
        "authority_revision": manifest.authority_revision,
        "project": str(entry["bugsinpy"]["project"]),
        "bug_id": str(entry["bugsinpy"]["bug_id"]),
        "buggy_revision": entry["bugsinpy"]["buggy_revision"],
        "recipe_path": recipe,
        "recipe_sha256": recipe_sha256,
    }
    ok = (
        prepared is not None
        and all(_nonempty_equal(getattr(prepared, name, None), value) for name, value in expected.items())
        and _SHA256.fullmatch(str(recipe_sha256 or "")) is not None
        and _SHA256.fullmatch(prepared.installed_fingerprint) is not None
        and prepared.status == "prepared"
        and prepared.network_disabled is True
    )
    return _boolean_gate(GateName.DEPENDENCY_INSTALL_BOUNDARY, ok, "dependency preparation is not bound to the selected task and manifest")


def _test_command_gate(commands: NormalizedCommands, facts: PreflightFacts) -> GateResult:
    ok = bool(commands.baseline_argv and commands.fail_to_pass and commands.pass_to_pass and facts.test_command_available and facts.execution_context is not None)
    return _boolean_gate(GateName.TEST_COMMAND_AVAILABILITY, ok, "normalized pytest command is not verified available in the task environment")


def _containment_gate(facts: PreflightFacts, repository_root: Optional[str]) -> GateResult:
    context = facts.execution_context
    parent = Path(facts.external_parent).resolve() if facts.external_parent else None
    root = Path(context.containment.root).resolve() if context else None
    repository = Path(repository_root).resolve() if repository_root else None
    ok = (
        context is not None
        and parent is not None
        and root is not None
        and not _is_filesystem_root(root)
        and _is_within(parent, root)
        and repository is not None
        and not _is_within(repository, root)
        and context.runner is not None
        and context.runner.runner_id == context.containment.runner_id
        and dict(getattr(context.runner, "boundary_guarantee", {})) == context.containment.to_mapping()
    )
    return _boolean_gate(GateName.CONTAINMENT_READY, ok, "external parent, repository, and containment runner relationships are not verified")


def _pdb_gate(entry: Mapping[str, Any], commands: NormalizedCommands, facts: PreflightFacts) -> GateResult:
    context = facts.execution_context
    plan = facts.pdb_launch_plan
    review = entry["debugger_relevance"]
    candidates = {str(item).replace("\\", "/") for item in review.get("candidate_changed_files", []) if isinstance(item, str)}
    reviewed_driver = review.get("pdb_driver")
    reviewed_breakpoints = review.get("reviewed_breakpoints")
    f2p = commands.fail_to_pass[0] if commands.fail_to_pass else None
    plan_nodes = [item for item in plan.argv if isinstance(item, str) and "::" in item] if plan else []
    ok = (
        context is not None
        and plan is not None
        and isinstance(reviewed_driver, str)
        and _safe_relative(reviewed_driver) == plan.driver
        and isinstance(reviewed_breakpoints, list)
        and tuple(reviewed_breakpoints) == plan.breakpoints
        and plan.target in candidates
        and plan.python_executable == context.environment.python_executable
        and plan.cwd == context.environment.project_cwd
        and dict(plan.environment) == dict(context.environment.environment)
        and f2p is not None
        and plan_nodes == [f2p]
        and _pdb_argv_compatible(plan.argv, f2p, context)
    )
    return _boolean_gate(GateName.PDB_PLANNING, ok, "PDB launch plan is not bound to the selected task and reviewed F2P plan")


def _cleanup_gate(facts: PreflightFacts) -> GateResult:
    parent = Path(facts.external_parent).resolve() if facts.external_parent else None
    ok = facts.workspace_cleanup_ready and parent is not None and parent.is_dir()
    return _boolean_gate(GateName.WORKSPACE_CLEANUP_READY, ok, "owned external workspace parent and cleanup proof are not ready")


def _target_gate(entry: Mapping[str, Any], facts: PreflightFacts, target_symbols: Optional[Sequence[str]]) -> GateResult:
    symbols = target_symbols or ()
    known = isinstance(entry["debugger_relevance"].get("target_symbols"), str) and not entry["debugger_relevance"]["target_symbols"].lower().startswith("unknown")
    ok = facts.target_annotation_reviewed and (known or bool(symbols))
    return _boolean_gate(GateName.TARGET_ANNOTATION_REVIEW, ok, "target symbols/breakpoint plan require review before execution")


def _reviewed_symbols(entry: Mapping[str, Any], target_symbols: Optional[Sequence[str]]) -> tuple[str, ...]:
    supplied = tuple(target_symbols or ())
    if supplied and all(isinstance(item, str) and item for item in supplied):
        return supplied
    raw = entry["debugger_relevance"].get("target_symbols", "")
    if isinstance(raw, str) and not raw.lower().startswith("unknown"):
        return (raw,)
    raise TaskMappingError("reviewed target_symbols are required to map this entry into DebugTask")


def _boolean_gate(name: GateName, ok: bool, reason: str) -> GateResult:
    return GateResult(name, GateStatus.PASS if ok else GateStatus.BLOCKED, reason if not ok else "explicitly cleared")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_filesystem_root(path: Path) -> bool:
    return path == path.parent


def _owned_external_root(path: Path) -> bool:
    current = path.resolve()
    for candidate in (current, *current.parents):
        marker = candidate / _EXTERNAL_ROOT_MARKER
        if marker.is_file():
            return True
    return False


def _copy_tree_without_symlinks(source: Path, destination: Path) -> None:
    for item in source.iterdir():
        if item.is_symlink():
            raise OSError(f"source contains symlink: {item}")
        target = destination / item.name
        if item.is_dir():
            target.mkdir()
            _copy_tree_without_symlinks(item, target)
        else:
            shutil.copy2(item, target)


def _run_git(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "PATHEXT", "SystemRoot", "TEMP", "TMP")
        if key in os.environ
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return subprocess.run(
        argv,
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        shell=False,
        env=environment,
    )
