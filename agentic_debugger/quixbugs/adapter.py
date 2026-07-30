"""Fail-closed QuixBugs adapter for the resource-limited no-model baseline.

QuixBugs is the fallback dataset while BugsInPy remains license-gated. This
module supports any single pinned QuixBugs Python task whose manifest
declares a ``target.algorithm`` matching the upstream ``python_programs/
<algorithm>.py`` / ``correct_python_programs/<algorithm>.py`` /
``python_testcases/test_<algorithm>.py`` naming convention (originally
scoped to exactly one task, Python ``gcd``, and generalized for the
eight-task baseline). It does not build a second evaluation framework:
source acquisition follows the same pinned-Git pattern already accepted for
BugsInPy, workspace ownership reuses
:class:`agentic_debugger.bugsinpy.adapter.ExternalWorkspace` directly, and the
official gold-patch smoke is executed by the existing
:class:`agentic_debugger.evaluation.verifier.EvaluationVerifier` through the
existing :class:`agentic_debugger.evaluation.task_schema.DebugTask` contract.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import platform as platform_module
import re
import subprocess
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from agentic_debugger.bugsinpy.adapter import ExternalWorkspace
from agentic_debugger.evaluation.task_schema import DebugTask, TaskSource
from agentic_debugger.evaluation.verifier import EvaluationResult, EvaluationVerifier
from agentic_debugger.runtime.execution import VerifiedExecutionContext
from agentic_debugger.runtime.test_runner import TestRunKind, TestRunner
from agentic_debugger.runtime.workspace import TaskWorkspace

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_SCHEMA = "1.0"
_DATASET = "QuixBugs"
_APPROVED_URL = "https://github.com/jkoppel/QuixBugs"
_ALGORITHM = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _manifest_identity(algorithm: str) -> str:
    """Derive the required manifest_id/task_id from target.algorithm.

    Generalizes the identity that was previously pinned to the single
    literal ``quixbugs-gcd-smoke-v1``: any pinned QuixBugs Python task now
    gets the same deterministic ``quixbugs-<algorithm-with-hyphens>-smoke-v1``
    identity, keeping the shared DebugTask task_id pattern (hyphens only).
    """
    return f"quixbugs-{algorithm.replace('_', '-')}-smoke-v1"


class QuixBugsManifestValidationError(ValueError):
    """The accepted single-task QuixBugs manifest is malformed or unsupported."""


class QuixBugsTaskMappingError(ValueError):
    """The selected entry cannot be represented safely by ``DebugTask``."""


class QuixGateName(str, Enum):
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


class QuixGateStatus(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class QuixGateResult:
    name: QuixGateName
    status: QuixGateStatus
    reason: str

    def to_mapping(self) -> dict[str, str]:
        return {"name": self.name.value, "status": self.status.value, "reason": self.reason}


@dataclass(frozen=True)
class QuixPreflightReport:
    task_id: str
    manifest_fingerprint: str
    gates: tuple[QuixGateResult, ...]

    @property
    def authorized(self) -> bool:
        return all(gate.status is QuixGateStatus.PASS for gate in self.gates)

    @property
    def blocked_gates(self) -> tuple[str, ...]:
        return tuple(gate.name.value for gate in self.gates if gate.status is not QuixGateStatus.PASS)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "manifest_fingerprint": self.manifest_fingerprint,
            "authorized": self.authorized,
            "blocked_gates": list(self.blocked_gates),
            "gates": [gate.to_mapping() for gate in self.gates],
        }


@dataclass(frozen=True)
class QuixBugsPreflightFacts:
    """Explicit operator/environment facts. Missing facts never pass a gate."""

    platform: Optional[str] = None
    pinned_source_verified: bool = False
    license_reviewed: bool = False
    dependency_install_boundary_ready: bool = False
    test_command_available: bool = False
    workspace_cleanup_ready: bool = False
    target_annotation_reviewed: bool = False
    external_parent: Optional[str] = None
    execution_context: Optional[VerifiedExecutionContext] = None


class QuixBugsManifest:
    """Strict, immutable-in-use view of the accepted single-task JSON manifest."""

    def __init__(self, data: Mapping[str, Any], fingerprint: str) -> None:
        self._data = copy.deepcopy(dict(data))
        self.fingerprint = fingerprint

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "QuixBugsManifest":
        try:
            raw = Path(path).read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise QuixBugsManifestValidationError(f"manifest cannot be loaded: {exc}") from exc
        _validate_manifest(data)
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return cls(data, hashlib.sha256(canonical.encode("utf-8")).hexdigest())

    @property
    def manifest_id(self) -> str:
        return self._data["manifest_id"]

    @property
    def task_id(self) -> str:
        return self._data["task_id"]

    @property
    def authority_revision(self) -> str:
        return self._data["authority"]["official_repository_revision"]

    @property
    def official_repository(self) -> str:
        return self._data["authority"]["official_repository"]

    @property
    def algorithm(self) -> str:
        return self._data["target"]["algorithm"]

    @property
    def buggy_path(self) -> str:
        return self._data["target"]["buggy_path"]

    @property
    def corrected_path(self) -> str:
        return self._data["target"]["corrected_path"]

    @property
    def pytest_path(self) -> str:
        return self._data["target"]["pytest_path"]

    @property
    def support_paths(self) -> tuple[str, ...]:
        return tuple(self._data["target"]["support_paths"])

    @property
    def cwd(self) -> str:
        return self._data["target"]["selected_cwd"]

    @property
    def resource_profile(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._data["resource_profile"])

    @property
    def environment(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._data["environment"])

    @property
    def oracle(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._data["oracle"])

    def select(self, task_id: str) -> Mapping[str, Any]:
        if task_id != self.task_id:
            raise QuixBugsTaskMappingError(f"unknown QuixBugs task ID: {task_id!r}")
        return self.to_mapping()

    def to_mapping(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)


@dataclass(frozen=True)
class QuixBugsCommands:
    collection_argv: tuple[str, ...]
    baseline_argv: tuple[str, ...]
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    full_suite_argv: tuple[str, ...]
    oracle_correct_argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int
    max_test_runs: int

    def to_mapping(self) -> dict[str, Any]:
        return {
            "collection_argv": list(self.collection_argv),
            "baseline_argv": list(self.baseline_argv),
            "fail_to_pass": list(self.fail_to_pass),
            "pass_to_pass": list(self.pass_to_pass),
            "full_suite_argv": list(self.full_suite_argv),
            "oracle_correct_argv": list(self.oracle_correct_argv),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "max_test_runs": self.max_test_runs,
        }


def verifier_command_count(f2p_count: int, p2p_count: int) -> int:
    """Exact number of commands EvaluationVerifier.evaluate() issues for one run.

    1 collection + 1 baseline reproduction + (f2p+p2p) baseline node runs +
    1 post-patch reproduction + (f2p+p2p) post-patch node runs + 1 full suite.
    """
    return 4 + 2 * (f2p_count + p2p_count)


@dataclass(frozen=True)
class DiscoveryRecord:
    """Live pytest collection/baseline evidence gathered before task construction."""

    collected_nodes: tuple[str, ...]
    baseline_outcomes: Mapping[str, bool]
    f2p_candidates: tuple[str, ...]
    p2p_candidates: tuple[str, ...]
    oracle_correct_exit_code: Optional[int]
    oracle_correct_summary: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "collected_nodes": list(self.collected_nodes),
            "baseline_outcomes": dict(self.baseline_outcomes),
            "f2p_candidates": list(self.f2p_candidates),
            "p2p_candidates": list(self.p2p_candidates),
            "oracle_correct_exit_code": self.oracle_correct_exit_code,
            "oracle_correct_summary": self.oracle_correct_summary,
        }


class DiscoveryError(RuntimeError):
    """Live test discovery could not establish a valid F2P/P2P baseline."""


class QuixBugsAdapter:
    """Manifest loader, command normalizer, preflight, and DebugTask bridge."""

    def __init__(self, manifest: QuixBugsManifest) -> None:
        self.manifest = manifest

    @classmethod
    def from_manifest(cls, path: str | os.PathLike[str]) -> "QuixBugsAdapter":
        return cls(QuixBugsManifest.load(path))

    def collection_argv(self) -> list[str]:
        return ["python", "-m", "pytest", self.manifest.pytest_path, "--collect-only", "-q", "-p", "no:cacheprovider"]

    def node_run_argv(self, node_id: str) -> list[str]:
        return ["python", "-m", "pytest", node_id, "-q", "-p", "no:cacheprovider"]

    def oracle_correct_argv(self) -> list[str]:
        return ["python", "-m", "pytest", self.manifest.pytest_path, "--correct", "-q", "-p", "no:cacheprovider"]

    def full_suite_argv(self) -> list[str]:
        return ["python", "-m", "pytest", self.manifest.pytest_path, "-q", "-p", "no:cacheprovider"]

    def build_commands(self, *, fail_to_pass: Sequence[str], pass_to_pass: Sequence[str]) -> QuixBugsCommands:
        f2p = tuple(fail_to_pass)
        if not f2p or not all(isinstance(item, str) and item for item in f2p):
            raise QuixBugsTaskMappingError("at least one non-empty fail_to_pass node ID is required")
        if len(f2p) != len(set(f2p)):
            raise QuixBugsTaskMappingError("fail_to_pass contains duplicate node IDs")
        p2p = tuple(pass_to_pass)
        if not p2p or not all(isinstance(item, str) and item for item in p2p):
            raise QuixBugsTaskMappingError("at least one non-empty pass_to_pass node ID is required")
        if len(p2p) != len(set(p2p)):
            raise QuixBugsTaskMappingError("pass_to_pass contains duplicate node IDs")
        overlap = set(f2p) & set(p2p)
        if overlap:
            raise QuixBugsTaskMappingError(f"fail_to_pass and pass_to_pass must not overlap: {sorted(overlap)}")
        max_test_runs = verifier_command_count(len(f2p), len(p2p))
        return QuixBugsCommands(
            collection_argv=tuple(self.collection_argv()),
            baseline_argv=tuple(self.node_run_argv(f2p[0])),
            fail_to_pass=f2p,
            pass_to_pass=p2p,
            full_suite_argv=tuple(self.full_suite_argv()),
            oracle_correct_argv=tuple(self.oracle_correct_argv()),
            cwd=self.manifest.cwd,
            timeout_seconds=int(self.manifest.resource_profile["wall_clock_timeout_seconds"]),
            max_test_runs=max_test_runs,
        )

    def source_provenance(self) -> dict[str, str]:
        return {
            "dataset": _DATASET,
            "manifest_id": self.manifest.manifest_id,
            "manifest_fingerprint": self.manifest.fingerprint,
            "upstream_repository": self.manifest.official_repository,
            "upstream_revision": self.manifest.authority_revision,
            "project": "quixbugs",
            "bug_id": self.manifest.algorithm,
            "buggy_revision": self.manifest.authority_revision,
            "fixed_revision": self.manifest.authority_revision,
        }

    def preflight(self, facts: Optional[QuixBugsPreflightFacts] = None, *, repository_root: Optional[str] = None) -> QuixPreflightReport:
        facts = facts or QuixBugsPreflightFacts()
        gates = (
            QuixGateResult(QuixGateName.MANIFEST_VALID, QuixGateStatus.PASS, "manifest loaded and schema-validated"),
            _platform_gate(facts),
            _source_gate(self.manifest, facts),
            _license_gate(self.manifest, facts),
            _python_gate(self.manifest, facts),
            _dependency_gate(self.manifest, facts),
            _test_command_gate(facts),
            _containment_gate(facts, repository_root),
            _cleanup_gate(facts),
            _target_gate(facts),
        )
        return QuixPreflightReport(self.manifest.task_id, self.manifest.fingerprint, gates)

    def to_debug_task(self, source: TaskSource, commands: QuixBugsCommands) -> DebugTask:
        if not isinstance(source, TaskSource) or source.kind != "external":
            raise QuixBugsTaskMappingError("QuixBugs tasks require an external source binding")
        algorithm = self.manifest.algorithm
        oracle = self.manifest.oracle
        denied_write_paths = sorted(
            {
                "tests",
                "task.json",
                self.manifest.pytest_path,
                self.manifest.corrected_path,
                "conftest.py",
                "json_testcases",
                ".git",
                *self.manifest.support_paths,
            }
        )
        mapping = {
            "schema_version": "1.0",
            "task_id": self.manifest.task_id,
            "title": f"QuixBugs Python {algorithm} resource-limited smoke",
            "description": (
                f"Repair the regression exposed by {len(commands.fail_to_pass)} failing {algorithm} nodes "
                f"(e.g. {commands.fail_to_pass[0]}) in the pinned QuixBugs buggy revision. Uses the "
                "existing test, patch, and verifier workflow."
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
                "full_suite_argv": list(commands.full_suite_argv),
                "timeout_seconds": commands.timeout_seconds,
            },
            "constraints": {
                "allowed_write_paths": [self.manifest.buggy_path],
                "denied_write_paths": denied_write_paths,
                "network_allowed": False,
                "external_services_allowed": False,
                "max_patch_attempts": 1,
                "max_test_runs": commands.max_test_runs,
                "max_pdb_observations": 0,
            },
            "oracle": {
                "bug_category": oracle["bug_category"],
                "target_files": [self.manifest.buggy_path],
                "target_symbols": list(oracle["target_symbols"]),
                "root_cause_summary": oracle["root_cause_summary"],
                "runtime_evidence_hint": oracle["runtime_evidence_hint"],
            },
            "tags": ["quixbugs", algorithm, "no-model-smoke", "resource-limited", "selected-suite-only"],
        }
        return DebugTask.from_mapping(mapping)


class QuixBugsSourceAcquirer:
    """Pinned Git acquisition restricted to the single approved QuixBugs URL.

    Unlike BugsInPy's per-run acquisition into a freshly marked ephemeral
    root, the QuixBugs source is acquired once into a persistent, immutable
    location (see :meth:`QuixBugsSmokeRunner.ensure_source`) and reused
    across runs, so ownership here is proven by the caller's explicit,
    already-existing parent directory rather than a disposable-root marker.
    """

    def acquire(self, url: str, revision: str, destination: Path) -> Path:
        if not _SHA1.fullmatch(revision):
            raise ValueError("source revision must be a full lowercase Git SHA-1")
        if url != _APPROVED_URL:
            raise ValueError("source URL is not the approved public HTTPS QuixBugs repository")
        if not destination.parent.is_dir():
            raise PermissionError("Git acquisition requires an already-existing owned parent directory")
        if not _is_within(destination.resolve(), destination.parent.resolve()):
            raise ValueError("acquisition destination escapes its owned parent")
        if destination.exists():
            raise FileExistsError(destination)
        _run_git(["-c", "credential.helper=", "clone", "--no-checkout", "--no-tags", "--no-recurse-submodules", url, str(destination)], destination.parent)
        _run_git(["checkout", "--detach", revision], destination)
        self.verify_pinned(destination, revision)
        return destination

    def verify_pinned(self, destination: Path, revision: str) -> None:
        """Re-verify an already-acquired checkout is exactly the pinned, immutable state.

        Fails closed on any deviation (wrong revision, attached HEAD, wrong
        origin, any modified/staged/untracked file, or a submodule). Never
        resets, cleans, or deletes the checkout -- a dirty checkout is a
        blocker to report, not something to repair.
        """
        if not _SHA1.fullmatch(revision):
            raise ValueError("source revision must be a full lowercase Git SHA-1")
        actual = _run_git(["rev-parse", "HEAD"], destination).stdout.strip()
        if actual != revision:
            raise RuntimeError(f"pinned revision mismatch: expected {revision}, got {actual}")
        symbolic = _run_git(["symbolic-ref", "-q", "HEAD"], destination, check=False)
        if symbolic.returncode != 1:
            raise RuntimeError(f"expected a detached HEAD (git symbolic-ref exit 1); got exit {symbolic.returncode}: {symbolic.stderr}")
        origin = _run_git(["remote", "get-url", "origin"], destination).stdout.strip()
        if origin != _APPROVED_URL:
            raise RuntimeError(f"pinned checkout origin does not match the approved URL: {origin!r}")
        status = _run_git(["status", "--porcelain"], destination).stdout
        if status.strip():
            raise RuntimeError(
                "pinned checkout is not clean (modified/staged/untracked files present); "
                f"refusing to reset, clean, or delete it:\n{status}"
            )
        submodules = _run_git(["submodule", "status"], destination).stdout
        if submodules.strip():
            raise RuntimeError(f"pinned checkout unexpectedly has submodules:\n{submodules}")


def build_gold_patch(buggy_text: str, corrected_text: str, path: str) -> str:
    """Build the evaluator-only unified diff between one task's buggy and corrected source."""
    if buggy_text == corrected_text:
        raise QuixBugsTaskMappingError("gold patch is empty; buggy and corrected sources are identical")
    diff_lines = list(
        difflib.unified_diff(
            buggy_text.splitlines(keepends=True),
            corrected_text.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    if not diff_lines:
        raise QuixBugsTaskMappingError("gold patch generation produced no hunks")
    diff_text = "".join(diff_lines)
    touched = {line[4:].strip() for line in diff_lines if line.startswith("--- ") or line.startswith("+++ ")}
    normalized = {item[2:] if item.startswith(("a/", "b/")) else item for item in touched}
    if normalized != {path}:
        raise QuixBugsTaskMappingError(f"gold patch touches unexpected paths: {sorted(normalized)}")
    return diff_text


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_real_smoke_passed(result: Optional[EvaluationResult], cleanup_succeeded: bool) -> bool:
    """A real smoke only passes when the verifier COMPLETED with a genuinely
    resolved outcome and the owned external workspace was actually cleaned up."""
    return (
        result is not None
        and result.status.value == "COMPLETED"
        and result.outcome is not None
        and result.outcome.value == "RESOLVED"
        and cleanup_succeeded
    )


class QuixBugsSmokeRunner:
    """Discover live F2P/P2P nodes, then run one official gold-patch smoke."""

    def __init__(self, adapter: QuixBugsAdapter, acquirer: QuixBugsSourceAcquirer, verifier_factory: Callable[..., EvaluationVerifier] = EvaluationVerifier) -> None:
        self.adapter = adapter
        self.acquirer = acquirer
        self.verifier_factory = verifier_factory

    def discover(self, execution_context: VerifiedExecutionContext, workspace: TaskWorkspace) -> DiscoveryRecord:
        runner = TestRunner(workspace, execution_context=execution_context)
        cwd = self.adapter.manifest.cwd
        algorithm = self.adapter.manifest.algorithm
        timeout = int(self.adapter.manifest.resource_profile["wall_clock_timeout_seconds"])
        collection = runner.run_tests(self.adapter.collection_argv(), cwd, timeout, kind=TestRunKind.SELECTED)
        if collection.launch_error or collection.command_result.exit_code != 0:
            raise DiscoveryError(f"pytest collection failed for the pinned {algorithm} test file")
        collected = _parse_collected_nodes(collection.command_result.stdout)
        if not collected:
            raise DiscoveryError(f"pytest collection returned no {algorithm} test nodes")
        outcomes: dict[str, bool] = {}
        for node in collected:
            result = runner.run_tests(self.adapter.node_run_argv(node), cwd, timeout, kind=TestRunKind.SELECTED)
            if result.launch_error or result.command_result.exit_code not in (0, 1):
                raise DiscoveryError(
                    f"baseline run for {node!r} did not produce a clean pass/fail result "
                    f"(exit_code={result.command_result.exit_code!r}, timed_out={result.timed_out!r}, "
                    f"launch_error={result.launch_error!r}) -- likely a non-terminating buggy baseline, "
                    "safely bounded by the resource-limited sandbox's CPU-time/wall-clock limits rather "
                    "than a clean test failure"
                )
            outcomes[node] = result.command_result.exit_code == 0
        f2p_candidates = tuple(node for node in collected if not outcomes[node])
        p2p_candidates = tuple(node for node in collected if outcomes[node])
        if not f2p_candidates:
            raise DiscoveryError(f"no reproducible failing {algorithm} node was found on the buggy baseline")
        oracle = runner.run_tests(self.adapter.oracle_correct_argv(), cwd, timeout, kind=TestRunKind.SELECTED)
        oracle_summary = _last_summary_line(oracle.command_result.stdout)
        return DiscoveryRecord(tuple(collected), outcomes, f2p_candidates, p2p_candidates, oracle.command_result.exit_code, oracle_summary)

    def ensure_source(self, sources_parent: Path) -> Path:
        """Idempotently acquire the pinned source once into an immutable location.

        Unlike the disposable per-run workspace, this directory is never
        deleted by the smoke runner. A second call against an
        already-populated directory only re-verifies the pin; it never
        re-clones or mutates existing bytes.
        """
        sources_parent.mkdir(parents=True, exist_ok=True)
        destination = sources_parent / "quixbugs"
        revision = self.adapter.manifest.authority_revision
        if destination.is_dir():
            self.acquirer.verify_pinned(destination, revision)
            return destination
        return self.acquirer.acquire(self.adapter.manifest.official_repository, revision, destination)

    def run(
        self,
        *,
        facts: QuixBugsPreflightFacts,
        sources_parent: str,
        external_parent: str,
        repository_root: Optional[str] = None,
    ) -> "QuixSmokeEvidence":
        report = self.adapter.preflight(facts, repository_root=repository_root)
        if not report.authorized:
            return QuixSmokeEvidence(self.adapter.manifest.task_id, "REAL_SMOKE_BLOCKED", report, None, None, None, False, True, None)

        sources_dir = Path(sources_parent).resolve()
        external: Optional[ExternalWorkspace] = None
        cleanup_error: Optional[str] = None
        execution_error: Optional[str] = None
        failure_kind: Optional[str] = None
        result: Optional[EvaluationResult] = None
        discovery: Optional[DiscoveryRecord] = None
        gold_hashes: Optional[dict[str, str]] = None
        phase = "acquisition"
        try:
            if facts.execution_context is None:
                raise RuntimeError("authorized smoke requires a verified execution context")
            project_root = self.ensure_source(sources_dir)
            source = TaskSource("external", "quixbugs", self.adapter.source_provenance())

            external = ExternalWorkspace.create(
                external_parent,
                repository_root=repository_root,
                containment_root=facts.execution_context.containment.root,
            )
            external.verifier_workspace_parent.mkdir(parents=True, exist_ok=True)
            external.assert_contained(external.verifier_workspace_parent)

            phase = "discovery"
            discovery_workspace = TaskWorkspace(str(project_root), parent_dir=str(external.verifier_workspace_parent))
            try:
                discovery = self.discover(facts.execution_context, discovery_workspace)
            finally:
                discovery_workspace.cleanup()

            phase = "gold_patch"
            buggy_text = (project_root / self.adapter.manifest.buggy_path).read_text(encoding="utf-8")
            corrected_text = (project_root / self.adapter.manifest.corrected_path).read_text(encoding="utf-8")
            gold_patch = build_gold_patch(buggy_text, corrected_text, self.adapter.manifest.buggy_path)
            gold_hashes = {
                "buggy_sha256": sha256_of(buggy_text),
                "corrected_sha256": sha256_of(corrected_text),
                "gold_patch_sha256": sha256_of(gold_patch),
            }

            commands = self.adapter.build_commands(fail_to_pass=discovery.f2p_candidates, pass_to_pass=discovery.p2p_candidates)
            debug_task = self.adapter.to_debug_task(source, commands)

            phase = "verifier"
            verifier = self.verifier_factory(
                str(sources_dir),
                workspace_parent=str(external.verifier_workspace_parent),
                execution_context=facts.execution_context,
            )
            result = verifier.evaluate(debug_task, gold_patch)
        except Exception as exc:
            execution_error = f"{type(exc).__name__}: {exc}"
            failure_kind = f"{phase}_failure"
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
        # A real smoke only PASSES when the verifier reached a genuinely
        # resolved outcome AND the owned external workspace cleanup
        # succeeded -- COMPLETED-but-unresolved and cleanup-failed cases
        # must never report REAL_SMOKE_PASSED.
        verdict = "REAL_SMOKE_PASSED" if _is_real_smoke_passed(result, cleanup_succeeded) else "REAL_SMOKE_FAILED"
        return QuixSmokeEvidence(
            self.adapter.manifest.task_id, verdict, report, discovery, gold_hashes, result,
            cleanup_attempted, cleanup_succeeded, cleanup_error, failure_kind, execution_error,
        )


@dataclass(frozen=True)
class QuixSmokeEvidence:
    task_id: str
    verdict: str
    preflight: QuixPreflightReport
    discovery: Optional[DiscoveryRecord]
    gold_patch_hashes: Optional[Mapping[str, str]]
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
            "discovery": self.discovery.to_mapping() if self.discovery else None,
            "gold_patch_hashes": dict(self.gold_patch_hashes) if self.gold_patch_hashes else None,
            "evaluation": self.evaluation.semantic_mapping() if self.evaluation else None,
            "cleanup_attempted": self.cleanup_attempted,
            "cleanup_succeeded": self.cleanup_succeeded,
            "cleanup_error": self.cleanup_error,
            "failure_kind": self.failure_kind,
            "execution_error": self.execution_error,
        }


def _validate_manifest(data: Any) -> None:
    if not isinstance(data, dict):
        raise QuixBugsManifestValidationError("manifest root must be an object")
    if data.get("manifest_schema_version") != _MANIFEST_SCHEMA:
        raise QuixBugsManifestValidationError("unsupported manifest schema version")
    if data.get("dataset") != _DATASET:
        raise QuixBugsManifestValidationError("manifest identity is unsupported")
    authority = data.get("authority")
    if not isinstance(authority, dict) or not _SHA1.fullmatch(str(authority.get("official_repository_revision", ""))):
        raise QuixBugsManifestValidationError("authority lacks a pinned official revision")
    if authority.get("official_repository") != _APPROVED_URL:
        raise QuixBugsManifestValidationError("authority official repository is not the approved QuixBugs URL")
    target = data.get("target")
    if not isinstance(target, dict):
        raise QuixBugsManifestValidationError("target section is required")
    for field in ("algorithm", "language", "buggy_path", "corrected_path", "pytest_path", "selected_cwd"):
        if not isinstance(target.get(field), str) or not target[field]:
            raise QuixBugsManifestValidationError(f"target.{field} must be a non-empty string")
    algorithm = target["algorithm"]
    if not _ALGORITHM.fullmatch(algorithm):
        raise QuixBugsManifestValidationError("target.algorithm must be a lowercase snake_case identifier")
    if target["language"] != "python":
        raise QuixBugsManifestValidationError("this smoke only supports QuixBugs Python tasks")
    expected_paths = {
        "buggy_path": f"python_programs/{algorithm}.py",
        "corrected_path": f"correct_python_programs/{algorithm}.py",
        "pytest_path": f"python_testcases/test_{algorithm}.py",
    }
    for field, expected in expected_paths.items():
        if target[field] != expected:
            raise QuixBugsManifestValidationError(f"target.{field} must be {expected!r} for target.algorithm {algorithm!r}, got {target[field]!r}")
    for label in ("buggy_path", "corrected_path", "pytest_path"):
        _safe_relative_or_raise(target[label], f"target.{label}")
    expected_identity = _manifest_identity(algorithm)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", expected_identity):
        raise QuixBugsManifestValidationError(f"derived identity {expected_identity!r} is not a valid task_id (algorithm name too long)")
    if data.get("manifest_id") != expected_identity or data.get("task_id") != expected_identity:
        raise QuixBugsManifestValidationError(f"manifest_id and task_id must both be the derived identity {expected_identity!r}")
    support = target.get("support_paths")
    if not isinstance(support, list) or not all(isinstance(item, str) and item for item in support):
        raise QuixBugsManifestValidationError("target.support_paths must be a list of non-empty strings")
    for item in support:
        _safe_relative_or_raise(item, "target.support_paths entry")
    oracle = data.get("oracle")
    if not isinstance(oracle, dict):
        raise QuixBugsManifestValidationError("oracle section is required")
    if not isinstance(oracle.get("bug_category"), str) or not oracle["bug_category"]:
        raise QuixBugsManifestValidationError("oracle.bug_category must be a non-empty string")
    target_symbols = oracle.get("target_symbols")
    if not isinstance(target_symbols, list) or not target_symbols or not all(isinstance(item, str) and item for item in target_symbols):
        raise QuixBugsManifestValidationError("oracle.target_symbols must be a non-empty list of non-empty strings")
    if not isinstance(oracle.get("root_cause_summary"), str) or not oracle["root_cause_summary"]:
        raise QuixBugsManifestValidationError("oracle.root_cause_summary must be a non-empty string")
    if not isinstance(oracle.get("runtime_evidence_hint"), str) or not oracle["runtime_evidence_hint"]:
        raise QuixBugsManifestValidationError("oracle.runtime_evidence_hint must be a non-empty string")
    environment = data.get("environment")
    if not isinstance(environment, dict) or not isinstance(environment.get("python_version"), str) or not environment["python_version"]:
        raise QuixBugsManifestValidationError("environment.python_version is required")
    pinned = environment.get("pinned_packages")
    if not isinstance(pinned, dict) or "pytest" not in pinned or not isinstance(pinned["pytest"], str):
        raise QuixBugsManifestValidationError("environment.pinned_packages.pytest is required")
    if not _SHA256.fullmatch(str(environment.get("expected_fingerprint", ""))):
        raise QuixBugsManifestValidationError("environment.expected_fingerprint must be a pinned SHA-256")
    resource_profile = data.get("resource_profile")
    if not isinstance(resource_profile, dict):
        raise QuixBugsManifestValidationError("resource_profile is required")
    for field in ("cpu_seconds", "memory_bytes", "max_processes", "wall_clock_timeout_seconds", "retained_output_chars"):
        value = resource_profile.get(field)
        if type(value) is not int or value <= 0:
            raise QuixBugsManifestValidationError(f"resource_profile.{field} must be a positive int")
    if resource_profile.get("network_denied") is not True:
        raise QuixBugsManifestValidationError("resource_profile.network_denied must be true")
    licensing = data.get("licensing")
    if not isinstance(licensing, dict) or licensing.get("license") != "MIT":
        raise QuixBugsManifestValidationError("licensing.license must be reviewed as MIT")
    if not isinstance(licensing.get("authorized_use"), str) or not licensing["authorized_use"]:
        raise QuixBugsManifestValidationError("licensing.authorized_use is required")
    unresolved = data.get("unresolved_assumptions")
    if not isinstance(unresolved, list) or not all(isinstance(item, str) and item for item in unresolved):
        raise QuixBugsManifestValidationError("unresolved_assumptions must be a list of non-empty strings")


def _safe_relative_or_raise(value: str, label: str) -> None:
    if value.startswith(("/", "\\")) or (len(value) >= 2 and value[1] == ":" and value[0].isalpha()):
        raise QuixBugsManifestValidationError(f"{label} must be a relative path")
    if ".." in value.replace("\\", "/").split("/"):
        raise QuixBugsManifestValidationError(f"{label} must not contain path traversal")


def _boolean_gate(name: QuixGateName, ok: bool, reason: str) -> QuixGateResult:
    return QuixGateResult(name, QuixGateStatus.PASS if ok else QuixGateStatus.BLOCKED, reason if not ok else "explicitly cleared")


def _platform_gate(facts: QuixBugsPreflightFacts) -> QuixGateResult:
    actual = (facts.platform or platform_module.system()).lower()
    return _boolean_gate(QuixGateName.SUPPORTED_PLATFORM, actual == "linux", f"reference platform is Linux; observed {actual}")


def _source_gate(manifest: QuixBugsManifest, facts: QuixBugsPreflightFacts) -> QuixGateResult:
    if not _SHA1.fullmatch(manifest.authority_revision):
        return QuixGateResult(QuixGateName.PINNED_UPSTREAM_SOURCE, QuixGateStatus.BLOCKED, "manifest revision is not a full pin")
    return _boolean_gate(QuixGateName.PINNED_UPSTREAM_SOURCE, facts.pinned_source_verified, "source revision and identity must be verified before checkout")


def _license_gate(manifest: QuixBugsManifest, facts: QuixBugsPreflightFacts) -> QuixGateResult:
    return _boolean_gate(QuixGateName.PROJECT_LICENSE_REVIEW, facts.license_reviewed, "QuixBugs license/legal_notes review is not cleared")


def _python_gate(manifest: QuixBugsManifest, facts: QuixBugsPreflightFacts) -> QuixGateResult:
    expected = manifest.environment["python_version"]
    context = facts.execution_context
    actual = context.environment.python_version if context else "unknown"
    ok = context is not None and actual == expected and Path(context.environment.python_executable).is_file() and context.environment.project_cwd == manifest.cwd
    return _boolean_gate(QuixGateName.PYTHON_RUNTIME_AVAILABLE, ok, f"task requires Python {expected}; verified runtime is {actual}")


def _nonempty_equal(actual: Any, expected: Any) -> bool:
    return isinstance(expected, str) and bool(expected) and actual == expected


def _dependency_gate(manifest: QuixBugsManifest, facts: QuixBugsPreflightFacts) -> QuixGateResult:
    context = facts.execution_context
    prepared = context.environment.dependencies if context else None
    expected_pytest = manifest.environment["pinned_packages"]["pytest"]
    expected_recipe_path = f"pytest=={expected_pytest}"
    expected_recipe_sha256 = hashlib.sha256(expected_recipe_path.encode("utf-8")).hexdigest()
    expected = {
        "pilot_task_id": manifest.task_id,
        "manifest_fingerprint": manifest.fingerprint,
        "authority_revision": manifest.authority_revision,
        "project": "quixbugs",
        "bug_id": manifest.algorithm,
        "buggy_revision": manifest.authority_revision,
        "recipe_path": expected_recipe_path,
        "recipe_sha256": expected_recipe_sha256,
        "installed_fingerprint": manifest.environment["expected_fingerprint"],
    }
    ok = (
        prepared is not None
        and all(_nonempty_equal(getattr(prepared, name, None), value) for name, value in expected.items())
        and prepared.status == "prepared"
        and prepared.network_disabled is True
    )
    return _boolean_gate(
        QuixGateName.DEPENDENCY_INSTALL_BOUNDARY, ok,
        "dependency preparation is not exactly bound to the selected task, manifest, revision, and pinned recipe",
    )


def _test_command_gate(facts: QuixBugsPreflightFacts) -> QuixGateResult:
    ok = facts.test_command_available and facts.execution_context is not None
    return _boolean_gate(QuixGateName.TEST_COMMAND_AVAILABILITY, ok, "normalized pytest command is not verified available in the task environment")


def _containment_gate(facts: QuixBugsPreflightFacts, repository_root: Optional[str]) -> QuixGateResult:
    context = facts.execution_context
    parent = Path(facts.external_parent).resolve() if facts.external_parent else None
    root = Path(context.containment.root).resolve() if context else None
    repository = Path(repository_root).resolve() if repository_root else None
    ok = (
        context is not None
        and parent is not None
        and root is not None
        and root != root.parent
        and _is_within(parent, root)
        and repository is not None
        and not _is_within(repository, root)
        and context.runner is not None
        and getattr(context.runner, "resource_isolation_ready", False) is True
        and dict(getattr(context.runner, "boundary_guarantee", {})) == context.containment.to_mapping()
    )
    return _boolean_gate(QuixGateName.CONTAINMENT_READY, ok, "external parent, repository, and resource-isolation-ready containment are not verified")


def _cleanup_gate(facts: QuixBugsPreflightFacts) -> QuixGateResult:
    parent = Path(facts.external_parent).resolve() if facts.external_parent else None
    ok = facts.workspace_cleanup_ready and parent is not None and parent.is_dir()
    return _boolean_gate(QuixGateName.WORKSPACE_CLEANUP_READY, ok, "owned external workspace parent and cleanup proof are not ready")


def _target_gate(facts: QuixBugsPreflightFacts) -> QuixGateResult:
    return _boolean_gate(QuixGateName.TARGET_ANNOTATION_REVIEW, facts.target_annotation_reviewed, "target annotation requires explicit review before execution")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False



def _run_git(argv: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "PATHEXT", "SystemRoot", "TEMP", "TMP")
        if key in os.environ
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    # The external WSL root is reached via the \\wsl.localhost\ UNC bridge, whose
    # file ownership does not match the Windows git process; this per-invocation
    # -c override (not a persistent config file) is git's documented way to
    # accept that boundary without touching any global/system configuration.
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *argv],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
        shell=False,
        env=environment,
    )


def _parse_collected_nodes(output: str) -> list[str]:
    found: list[str] = []
    for line in output.splitlines():
        value = line.strip()
        if "::" not in value or value.startswith("="):
            continue
        found.append(value)
    return found


def _last_summary_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else ""
