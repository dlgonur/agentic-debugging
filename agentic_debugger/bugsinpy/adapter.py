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



from agentic_debugger.bugsinpy.contracts import (
    _DATASET,
    _EXTERNAL_ROOT_MARKER,
    _MANIFEST_ID,
    _MANIFEST_SCHEMA,
    _DEFAULT_TIMEOUT,
    _RECEIPT_ISSUER,
    _SHA1,
    _SHA256,
    _SHELL_TOKENS,
    _TASK_ID,
    _UNKNOWN,
    _VERSION,
    AcquiredSourceReceipt,
    GateName,
    GateResult,
    GateStatus,
    ManifestValidationError,
    NormalizedCommands,
    PreflightFacts,
    PreflightReport,
    SourceAcquirer,
    SmokeEvidence,
    TaskMappingError,
    BugsInPyManifest,
)
from agentic_debugger.bugsinpy.preflight_gates import (
    _cleanup_gate,
    _command_with_nodes,
    _containment_gate,
    _dependency_gate,
    _is_unknown,
    _license_gate,
    _normalize_pytest_command,
    _pdb_argv_compatible,
    _pdb_gate,
    _platform_gate,
    _python_gate,
    _reviewed_cwd,
    _reviewed_environment,
    _reviewed_pythonpath,
    _reviewed_symbols,
    _safe_relative,
    _source_gate,
    _target_gate,
    _test_command_gate,
)
from agentic_debugger.bugsinpy.workspace_source import (
    ExternalWorkspace,
    NoModelSmokeRunner,
    _copy_tree_without_symlinks,
    _is_filesystem_root,
    _is_within,
    _owned_external_root,
)

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


def normalize_pytest_commands(entry: Mapping[str, Any]) -> NormalizedCommands:
    """Public functional wrapper used by tests and operator tooling."""
    return BugsInPyAdapter.__new__(BugsInPyAdapter).normalize(entry)  # type: ignore[misc]


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
