"""The BugsInPy owned external workspace and gold-patch smoke runner.

This module owns :class:`ExternalWorkspace` — the disposable
owned-by-marker external root for source, environments, logs, and
outputs — with its containment/filesystem helpers, and the
:class:`NoModelSmokeRunner` official gold-patch smoke over the existing
independent verifier.

Source acquisition (:class:`GitSourceAcquirer`) stays with the adapter
module because its Git helper is a load-bearing module-attribute test
seam there.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from agentic_debugger.bugsinpy.contracts import (
    _EXTERNAL_ROOT_MARKER,
    AcquiredSourceReceipt,
    PreflightFacts,
    SmokeEvidence,
    SourceAcquirer,
    TaskMappingError,
)
from agentic_debugger.evaluation.task_schema import TaskSource
from agentic_debugger.evaluation.verifier import EvaluationResult, EvaluationVerifier

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
