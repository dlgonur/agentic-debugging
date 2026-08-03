"""Tests for the protocol-1.3 QuixBugs live-case integration.

Covers the fail-closed boundaries required for the QuixBugs gcd live
feasibility case: no silent fallback to curated fixtures, exactly one task
and one static repetition, no oracle/gold-patch/corrected-source leakage
into model requests, PDB unavailability under the (only available)
static-baseline policy, fail-closed manifest/source/context/authorization
gates, authoritative verifier grading, and cleanup/provenance enforcement.

Discovery is exercised through a fake ``ContainmentRunner`` (no WSL, no
network), mirroring the accepted offline pattern in
``tests/unit/test_quixbugs_adapter.py``. The verifier step is exercised
through a fake ``EvaluationVerifier`` (also mirroring that file's
``_fake_verifier_factory`` pattern) so this suite proves the integration's
own wiring -- not a second copy of QuixBugs's own already-tested pytest
execution semantics.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agentic_debugger.evaluation.live_quixbugs as live_quixbugs
from agentic_debugger.demo.catalog import RuntimeProbe
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import (
    LiveCaseStatus,
    LiveConfigurationError,
    LiveExecutionAuthorization,
    LiveModelConfig,
    LiveOptInError,
    LiveRunLimits,
    validate_live_report,
)
from agentic_debugger.quixbugs.adapter import (
    QuixBugsAdapter,
    QuixBugsPreflightFacts,
    build_gold_patch,
)
from agentic_debugger.runtime.command_runner import CommandResult
from agentic_debugger.runtime.execution import (
    ContainmentGuarantee,
    DependencyPreparation,
    PreparedEnvironment,
    VerifiedExecutionContext,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "research" / "quixbugs" / "GCD_SMOKE_MANIFEST_V1.json"

BUGGY_GCD = "def gcd(a, b):\n    if b == 0:\n        return a\n    else:\n        return gcd(a % b, b)\n"
CORRECT_GCD = "def gcd(a, b):\n    if b == 0:\n        return a\n    else:\n        return gcd(b, a % b)\n"
FIX_PATCH = build_gold_patch(BUGGY_GCD, CORRECT_GCD, "python_programs/gcd.py")

_MANIFEST_ORACLE = json.loads(MANIFEST.read_text(encoding="utf-8"))["oracle"]
_ORACLE_SECRETS = (_MANIFEST_ORACLE["root_cause_summary"], _MANIFEST_ORACLE["runtime_evidence_hint"])


def adapter() -> QuixBugsAdapter:
    return QuixBugsAdapter.from_manifest(MANIFEST)


def config() -> LiveModelConfig:
    return LiveModelConfig("test-model", ("test-model-command",))


def pdb_config() -> LiveModelConfig:
    return LiveModelConfig(
        "opencode/deepseek-v4-flash-free",
        ("transport-wrapper", "--model", "opencode/deepseek-v4-flash-free", "--variant", "max"),
    )


def limits() -> LiveRunLimits:
    return LiveRunLimits(max_model_requests=32, max_controller_steps=32)


COLLECT_STDOUT = "\n".join(
    f"python_testcases/test_gcd.py::test_gcd[input_data{i}-expected{i}]" for i in range(6)
)
NODE_EXIT_CODES = {
    f"python_testcases/test_gcd.py::test_gcd[input_data{i}-expected{i}]": (0 if i == 0 else 1) for i in range(6)
}


class FakeContainmentRunner:
    """Discovery-only fake: handles collection, oracle-correct, and node runs.

    Deliberately identical in shape to ``tests/unit/test_quixbugs_adapter.py``'s
    fixture of the same name -- this module intentionally does not invent a
    second, divergent fake execution boundary.
    """

    runner_id = "fake-quixbugs-live-contained"

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str, float, dict]] = []
        self.boundary_guarantee: dict = {}
        self.resource_isolation_ready = True
        self.process = SimpleNamespace(distro="Ubuntu-22.04")

    def run(self, argv, cwd, timeout_seconds, env):
        self.calls.append((argv, cwd, timeout_seconds, env))
        if "--collect-only" in argv:
            return CommandResult(list(argv), cwd, 0, False, 1, COLLECT_STDOUT, "", False, False)
        if "--correct" in argv:
            return CommandResult(list(argv), cwd, 0, False, 1, "1 passed\n", "", False, False)
        for node, code in NODE_EXIT_CODES.items():
            if node in argv:
                return CommandResult(list(argv), cwd, code, False, 1, f"{node} {'PASSED' if code == 0 else 'FAILED'}\n", "", False, False)
        raise AssertionError(f"unexpected argv reached the discovery-only fake runner: {argv}")


def _dependencies(current: QuixBugsAdapter) -> DependencyPreparation:
    recipe_path = f"pytest=={current.manifest.environment['pinned_packages']['pytest']}"
    import hashlib

    return DependencyPreparation(
        current.manifest.task_id, current.manifest.fingerprint, current.manifest.authority_revision,
        "quixbugs", current.manifest.algorithm, current.manifest.authority_revision,
        recipe_path, hashlib.sha256(recipe_path.encode("utf-8")).hexdigest(), current.manifest.environment["expected_fingerprint"],
    )


def _fake_facts(tmp_path: Path, *, authorized: bool, task_adapter: QuixBugsAdapter | None = None) -> QuixBugsPreflightFacts:
    current = task_adapter if task_adapter is not None else adapter()
    runner = FakeContainmentRunner()
    runner.resource_isolation_ready = authorized
    python_executable = tmp_path / "venv" / "bin" / "python"
    python_executable.parent.mkdir(parents=True, exist_ok=True)
    python_executable.write_text("fake interpreter", encoding="utf-8")
    environment = PreparedEnvironment(str(python_executable), "3.10.12", ".", (), {}, _dependencies(current))
    containment = ContainmentGuarantee(str(tmp_path.resolve()), runner.runner_id, resource_limits={"cpu_seconds": "prlimit-enforced:5"})
    runner.boundary_guarantee = containment.to_mapping()
    context = VerifiedExecutionContext(environment, containment, runner)
    external_parent = tmp_path / "external"
    external_parent.mkdir(exist_ok=True)
    return QuixBugsPreflightFacts(
        platform="linux",
        pinned_source_verified=authorized,
        license_reviewed=authorized,
        dependency_install_boundary_ready=authorized,
        test_command_available=authorized,
        workspace_cleanup_ready=authorized,
        target_annotation_reviewed=authorized,
        external_parent=str(external_parent),
        execution_context=context,
    )


def _prepare_pinned_source(
    tmp_path: Path,
    *,
    algorithm: str = "gcd",
    buggy: str = BUGGY_GCD,
    corrected: str = CORRECT_GCD,
) -> Path:
    """Populate every path PatchManager's denied-write policy must resolve.

    ``to_debug_task`` denies writes to ``tests``, ``task.json``, the pytest
    file, the corrected-source file, ``conftest.py``, ``json_testcases``,
    ``.git``, and every declared support path -- ``PatchManager`` requires
    each denied entry to already exist, so the offline fake source tree
    must contain all of them even though none is ever actually executed
    (test execution is fully intercepted by the fake containment runner).
    """
    sources_parent = tmp_path / "sources"
    project_root = sources_parent / "quixbugs"
    (project_root / "python_programs").mkdir(parents=True)
    (project_root / "python_programs" / f"{algorithm}.py").write_text(buggy, encoding="utf-8")
    (project_root / "correct_python_programs").mkdir(parents=True)
    (project_root / "correct_python_programs" / f"{algorithm}.py").write_text(corrected, encoding="utf-8")
    (project_root / "python_testcases").mkdir(parents=True)
    (project_root / "python_testcases" / f"test_{algorithm}.py").write_text("# fake test file, never executed\n", encoding="utf-8")
    (project_root / "python_testcases" / "load_testdata.py").write_text("# fake loader, never executed\n", encoding="utf-8")
    (project_root / "json_testcases").mkdir(parents=True)
    (project_root / "json_testcases" / f"{algorithm}.json").write_text("[]", encoding="utf-8")
    (project_root / "conftest.py").write_text("# fake conftest, never executed\n", encoding="utf-8")
    (project_root / "tests").mkdir(parents=True)
    (project_root / "task.json").write_text("{}", encoding="utf-8")
    (project_root / ".git").mkdir(parents=True)
    return sources_parent


class FakeSourceAcquirer:
    """verify_pinned is a no-op (the real pin check is exercised elsewhere,
    against the real pinned SHA-1, in test_quixbugs_adapter.py); acquire()
    asserts it is never called -- a live case must never clone."""

    def verify_pinned(self, destination: Path, revision: str) -> None:
        return None

    def acquire(self, url: str, revision: str, destination: Path) -> Path:
        raise AssertionError("a live QuixBugs case must never acquire/clone a source")


class FakeEvaluationResult:
    def __init__(self, *, status: str, outcome: str | None):
        from agentic_debugger.evaluation.runner import EvaluationStatus

        class _Enum:
            def __init__(self, value):
                self.value = value

        self.status = getattr(EvaluationStatus, status)
        self.outcome = _Enum(outcome) if outcome else None
        self.baseline = _Enum(True)
        self.baseline.valid = True
        self.patch_application = type("PA", (), {"to_mapping": staticmethod(lambda: {"applied": True})})()
        self.f2p_passed = 5
        self.f2p_total = 5
        self.p2p_passed = 1
        self.p2p_total = 1
        workspace = type("WS", (), {})()
        workspace.cleaned = True
        workspace.canonical_fixture_unchanged = True
        self.workspace = workspace


class FakeVerifier:
    """Mirrors ``test_quixbugs_adapter.py``'s ``_fake_verifier_factory`` pattern:
    a fake whole-verifier substitute proves this integration's own wiring
    (construction args, exact task/patch handed over) without re-testing
    QuixBugs's already-covered pytest execution semantics."""

    calls: list[dict[str, Any]] = []
    outcome: str | None = "RESOLVED"
    status: str = "COMPLETED"

    def __init__(self, repository_root: str, *, workspace_parent=None, execution_context=None, **_kwargs):
        FakeVerifier.calls.append(
            {"repository_root": repository_root, "workspace_parent": workspace_parent, "execution_context": execution_context}
        )

    def evaluate(self, task, candidate_patch):
        assert "gcd(b, a % b)" in candidate_patch
        assert task.task_id == adapter().manifest.task_id
        return FakeEvaluationResult(status=FakeVerifier.status, outcome=FakeVerifier.outcome)


def _no_pdb_scripted_directives(patch: str) -> list[dict[str, Any]]:
    return [
        {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
        {"kind": "transition", "target_state": "Understand", "reason": "reproduced"},
        {"kind": "action", "name": "find_function", "arguments": {"name": "gcd", "path": "python_programs/gcd.py"}},
        {"kind": "action", "name": "get_source_window", "arguments": {"path": "python_programs/gcd.py", "line": 1}},
        {"kind": "add_hypothesis", "hypothesis_id": "h1", "statement": "argument order is swapped in the recursive call", "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": False},
        {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": "h1", "statement": "argument order is swapped in the recursive call", "target_file": "python_programs/gcd.py", "target_symbol": "gcd", "confidence": "low"}},
        {"kind": "transition", "target_state": "Patch", "reason": "static evidence is sufficient"},
        {"kind": "action", "name": "apply_patch", "arguments": {"patch": patch}},
        {"kind": "action", "name": "syntax_check", "arguments": {}},
        {"kind": "transition", "target_state": "Validate", "reason": "syntax checked"},
        {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "post_patch"}},
        {"kind": "action", "name": "run_regression_tests", "arguments": {}},
        {"kind": "action", "name": "classify_outcome", "arguments": {}},
        {"kind": "transition", "target_state": "Done", "reason": "finished"},
    ]


class RecordingScriptedTransport:
    """Drives the deterministic controller while asserting, on every single
    request, that no oracle/gold/PDB material is ever advertised."""

    def __init__(self, patch: str):
        self.directives = _no_pdb_scripted_directives(patch)
        self.index = 0
        self.requests: list[dict[str, Any]] = []

    def request(self, payload, timeout_seconds):
        self.requests.append(payload)
        serialized = json.dumps(payload)
        assert "oracle" not in payload["task"]
        for secret in _ORACLE_SECRETS:
            assert secret not in serialized
        assert "corrected_path" not in serialized
        assert "fixed_revision" not in serialized
        allowed = set(payload["controller"]["allowed_actions"])
        assert not (allowed & {"start_pdb_session", "get_stack_summary", "get_frame_locals", "safe_eval_expression", "inspect_caller_frame", "stop_pdb_session"})
        assert "RuntimeEvidence" not in payload["controller"]["legal_transition_targets"]
        assert not (set(payload["action_contracts"]) & {"start_pdb_session", "get_stack_summary"})
        directive = self.directives[min(self.index, len(self.directives) - 1)]
        self.index += 1
        return {"directive": directive, "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class NeverCalledTransport:
    def request(self, payload, timeout_seconds):
        raise AssertionError("transport must not be contacted")


class FakeContainedPdbSession:
    starts = 0

    def __init__(self, workspace, **_kwargs):
        self.workspace = workspace
        self.started = False

    def start(self):
        type(self).starts += 1

    def start_paused_target(self, script, breakpoints):
        self.started = True
        return {"state": "paused", "script": script, "line": breakpoints[0], "function": "gcd"}

    def get_stack_summary(self):
        return {"pause_generation": 1, "frames": [{"frame_id": 0, "function": "gcd", "script": "python_programs/gcd.py", "line": 5}]}

    def get_frame_locals(self, frame_id, pause_generation):
        return {"frame_id": frame_id, "pause_generation": pause_generation, "locals": [{"name": "a", "value": "35"}, {"name": "b", "value": "21"}]}

    def stop(self):
        self.started = False


class PdbScriptedTransport:
    def __init__(self, *, use_pdb: bool):
        self.requests: list[dict[str, Any]] = []
        self.index = 0
        self.directives = [
            {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
            {"kind": "transition", "target_state": "Understand", "reason": "baseline reproduced"},
        ]
        if use_pdb:
            self.directives.extend([
                {"kind": "add_hypothesis", "hypothesis_id": "runtime-h1", "statement": "The failing recursion needs runtime evidence.", "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": True},
                {"kind": "transition", "target_state": "RuntimeEvidence", "reason": "uncertainty requires runtime evidence"},
                {"kind": "action", "name": "start_pdb_session", "arguments": {}},
                {"kind": "action", "name": "get_stack_summary", "arguments": {}},
                {"kind": "action", "name": "get_frame_locals", "arguments": {"frame_id": 0, "pause_generation": 1}},
                {"kind": "action", "name": "stop_pdb_session", "arguments": {}},
                {"kind": "transition", "target_state": "Failed", "reason": "runtime observations collected; no patch submitted in this probe"},
            ])
        else:
            self.directives.extend(_no_pdb_scripted_directives(FIX_PATCH)[2:])

    def request(self, payload, timeout_seconds):
        self.requests.append(payload)
        serialized = json.dumps(payload)
        assert "oracle" not in payload["task"]
        assert not any(secret in serialized for secret in _ORACLE_SECRETS)
        assert "corrected_path" not in serialized and "fixed_revision" not in serialized
        directive = self.directives[min(self.index, len(self.directives) - 1)]
        self.index += 1
        return {"directive": directive, "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


# ---- No silent fallback to curated fixtures ---------------------------------


def test_module_never_imports_the_curated_fixture_root() -> None:
    import ast

    tree = ast.parse(inspect.getsource(live_quixbugs))
    imported_modules: set[str] = set()
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
            imported_names.update(alias.name for alias in node.names)
    assert not any("demo.runner" in module for module in imported_modules)
    assert "CURATED_RELATIVE_ROOT" not in imported_names
    assert "CURATED_RELATIVE_ROOT" not in live_quixbugs.__dict__


def test_quixbugs_task_id_is_distinct_from_every_curated_fixture_name() -> None:
    curated_root = ROOT / "agentic_debugger" / "datasets" / "curated"
    curated_names = {p.name for p in curated_root.iterdir() if (p / "task.json").is_file()}
    assert adapter().manifest.task_id not in curated_names
    assert adapter().manifest.task_id.startswith("quixbugs-")


# ---- Exactly one task, one static repetition, no PDB substitution ----------


def test_static_is_default_and_pdb_mode_is_explicitly_available() -> None:
    assert live_quixbugs.QUIXBUGS_LIVE_POLICY is DemoPolicy.STATIC_BASELINE
    case_params = inspect.signature(live_quixbugs.run_live_quixbugs_case).parameters
    evaluation_params = inspect.signature(live_quixbugs.run_live_quixbugs_evaluation).parameters
    assert case_params["policy"].default is DemoPolicy.STATIC_BASELINE
    assert evaluation_params["policy"].default is DemoPolicy.STATIC_BASELINE
    assert live_quixbugs.QUIXBUGS_PDB_MODEL_ID == "opencode/deepseek-v4-flash-free"
    assert live_quixbugs.QUIXBUGS_PDB_VARIANT == "max"


def test_case_rejects_any_repetition_other_than_one(tmp_path: Path) -> None:
    facts = _fake_facts(tmp_path, authorized=True)
    with pytest.raises(live_quixbugs.QuixBugsLiveConfigurationError):
        live_quixbugs.run_live_quixbugs_case(
            repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(tmp_path / "sources"),
            facts=facts, config=config(), limits=limits(), transport=NeverCalledTransport(), repetition=2,
        )


def test_evaluation_rejects_any_repetitions_other_than_one(tmp_path: Path) -> None:
    facts = _fake_facts(tmp_path, authorized=True)
    with pytest.raises(LiveConfigurationError):
        live_quixbugs.run_live_quixbugs_evaluation(
            repository_root=str(ROOT), authorization=LiveExecutionAuthorization.authorize(True, True),
            manifest_path=str(MANIFEST), sources_parent=str(tmp_path / "sources"), facts=facts,
            config=config(), limits=limits(), repetitions=3,
        )


def test_evaluation_requires_explicit_authorization(tmp_path: Path) -> None:
    facts = _fake_facts(tmp_path, authorized=True)
    with pytest.raises(LiveOptInError):
        live_quixbugs.run_live_quixbugs_evaluation(
            repository_root=str(ROOT), authorization=None, manifest_path=str(MANIFEST),
            sources_parent=str(tmp_path / "sources"), facts=facts, config=config(), limits=limits(),
        )


def test_pdb_mode_rejects_wrong_model_or_variant_before_transport(tmp_path: Path) -> None:
    facts = _fake_facts(tmp_path, authorized=True)
    with pytest.raises(live_quixbugs.QuixBugsLiveConfigurationError):
        live_quixbugs.run_live_quixbugs_case(
            repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(tmp_path / "sources"),
            facts=facts, config=config(), limits=limits(), transport=NeverCalledTransport(),
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        )


def test_pdb_mode_rejects_missing_task_local_probe(tmp_path: Path) -> None:
    facts = _fake_facts(tmp_path, authorized=True)
    with pytest.raises(live_quixbugs.QuixBugsLiveConfigurationError, match="explicit task-local RuntimeProbe"):
        live_quixbugs.run_live_quixbugs_case(
            repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(tmp_path / "sources"),
            facts=facts, config=pdb_config(), limits=limits(), transport=NeverCalledTransport(),
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        )


def test_static_baseline_rejects_an_explicit_probe(tmp_path: Path) -> None:
    facts = _fake_facts(tmp_path, authorized=True)
    with pytest.raises(live_quixbugs.QuixBugsLiveConfigurationError, match="static-baseline accepts no PDB probe"):
        live_quixbugs.run_live_quixbugs_case(
            repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(tmp_path / "sources"),
            facts=facts, config=config(), limits=limits(), transport=NeverCalledTransport(),
            runtime_probe=live_quixbugs.QUIXBUGS_GCD_RUNTIME_PROBE,
        )


def test_default_gcd_probe_keeps_its_gcd_lock(tmp_path: Path) -> None:
    """A non-GCD task is no longer rejected merely because it is non-GCD, but
    the historical default gcd probe itself stays locked to the gcd task."""
    hanoi_manifest = ROOT / "research" / "quixbugs" / "HANOI_SMOKE_MANIFEST_V1.json"
    hanoi_adapter = QuixBugsAdapter.from_manifest(hanoi_manifest)
    facts = _fake_facts(tmp_path, authorized=True, task_adapter=hanoi_adapter)
    with pytest.raises(live_quixbugs.QuixBugsLiveConfigurationError, match=r"locked to '?quixbugs-gcd-smoke-v1'?"):
        live_quixbugs.run_live_quixbugs_case(
            repository_root=str(ROOT), manifest_path=str(hanoi_manifest), sources_parent=str(tmp_path / "sources"),
            facts=facts, config=pdb_config(), limits=limits(), transport=NeverCalledTransport(),
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            runtime_probe=live_quixbugs.QUIXBUGS_GCD_RUNTIME_PROBE,
        )


# ---- Fail-closed manifest / source / context / authorization gates ---------


def test_case_requires_a_facts_object(tmp_path: Path) -> None:
    with pytest.raises(live_quixbugs.QuixBugsLiveConfigurationError):
        live_quixbugs.run_live_quixbugs_case(
            repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(tmp_path / "sources"),
            facts=None, config=config(), limits=limits(), transport=NeverCalledTransport(),
        )


def test_case_requires_a_verified_execution_context(tmp_path: Path) -> None:
    facts = QuixBugsPreflightFacts(external_parent=str(tmp_path))
    with pytest.raises(live_quixbugs.QuixBugsLiveConfigurationError):
        live_quixbugs.run_live_quixbugs_case(
            repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(tmp_path / "sources"),
            facts=facts, config=config(), limits=limits(), transport=NeverCalledTransport(),
        )


def test_blocked_preflight_never_contacts_transport_or_touches_source(tmp_path: Path) -> None:
    facts = _fake_facts(tmp_path, authorized=False)
    case = live_quixbugs.run_live_quixbugs_case(
        repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(tmp_path / "does-not-exist"),
        facts=facts, config=config(), limits=limits(), transport=NeverCalledTransport(),
    )
    assert case.status is LiveCaseStatus.HARNESS_ERROR
    assert case.reporting["completed"] is False
    assert not case.reporting["case_directory_owned"]


def test_missing_pinned_source_fails_closed_without_cloning(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(live_quixbugs, "QuixBugsSourceAcquirer", FakeSourceAcquirer)
    facts = _fake_facts(tmp_path, authorized=True)
    sources_parent = tmp_path / "sources"
    sources_parent.mkdir()  # exists, but "quixbugs" subdirectory does not
    case = live_quixbugs.run_live_quixbugs_case(
        repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(sources_parent),
        facts=facts, config=config(), limits=limits(), transport=NeverCalledTransport(),
    )
    assert case.status is LiveCaseStatus.HARNESS_ERROR
    assert any("refusing to clone" in d for d in case.diagnostics)


def test_source_pin_mismatch_fails_closed(tmp_path: Path) -> None:
    # A real (non-monkeypatched) verify_pinned against a directory that is not
    # the pinned git checkout must fail closed via ordinary git failure -- no
    # source acquisition, no provider contact.
    facts = _fake_facts(tmp_path, authorized=True)
    sources_parent = _prepare_pinned_source(tmp_path)  # a plain directory, not a git repo
    case = live_quixbugs.run_live_quixbugs_case(
        repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(sources_parent),
        facts=facts, config=config(), limits=limits(), transport=NeverCalledTransport(),
    )
    assert case.status is LiveCaseStatus.HARNESS_ERROR


# ---- Full pipeline: oracle/PDB absence, verifier-authoritative grading -----


def _run_full_pipeline(tmp_path: Path, monkeypatch, *, verifier_status: str, verifier_outcome: str | None):
    monkeypatch.setattr(live_quixbugs, "QuixBugsSourceAcquirer", FakeSourceAcquirer)
    FakeVerifier.calls = []
    FakeVerifier.status = verifier_status
    FakeVerifier.outcome = verifier_outcome
    monkeypatch.setattr(live_quixbugs, "EvaluationVerifier", FakeVerifier)

    facts = _fake_facts(tmp_path, authorized=True)
    sources_parent = _prepare_pinned_source(tmp_path)
    transport = RecordingScriptedTransport(FIX_PATCH)

    case = live_quixbugs.run_live_quixbugs_case(
        repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(sources_parent),
        facts=facts, config=config(), limits=limits(), transport=transport, evaluation_id="quixbugs-gcd-test",
    )
    return case, transport, facts


def test_full_pipeline_resolved_hides_oracle_and_denies_pdb(tmp_path: Path, monkeypatch) -> None:
    case, transport, facts = _run_full_pipeline(tmp_path, monkeypatch, verifier_status="COMPLETED", verifier_outcome="RESOLVED")

    assert case.status is LiveCaseStatus.RESOLVED
    assert len(transport.requests) >= 1
    assert len(FakeVerifier.calls) == 1
    assert FakeVerifier.calls[0]["execution_context"] is facts.execution_context

    # Cleanup: the owned external WSL-side workspace is gone, and the
    # "pinned" source (outside that owned workspace) is untouched.
    assert case.reporting["cleanup"] == "cleaned"
    assert list((tmp_path / "external").iterdir()) == []
    assert (tmp_path / "sources" / "quixbugs" / "python_programs" / "gcd.py").read_text(encoding="utf-8") == BUGGY_GCD

    # No PDB observation was ever recorded, under a policy that never advertised PDB.
    assert case.measurements["successful_pdb_observation_count"] == 0
    assert case.measurements["failed_pdb_observation_count"] == 0
    assert case.policy == "static-baseline"

    # The report's own localization evidence legitimately scores the model's
    # declared localization against oracle target files/symbols (post-hoc,
    # never sent to the model) -- but the oracle's free-text root-cause prose
    # must never appear anywhere in report evidence either.
    payload_text = json.dumps(case.to_mapping())
    for secret in _ORACLE_SECRETS:
        assert secret not in payload_text


def test_full_pipeline_unresolved_reflects_verifier_not_controller_claim(tmp_path: Path, monkeypatch) -> None:
    # The controller reaches Done and submits a patch, but the *verifier*
    # reports the outcome as unresolved. The final status must follow the
    # verifier, not the fact that the controller's own state machine
    # completed successfully.
    case, _transport, _facts = _run_full_pipeline(tmp_path, monkeypatch, verifier_status="COMPLETED", verifier_outcome="NO_OP")

    assert case.controller["completed"] is True
    assert case.controller["final_state"] == "Done"
    assert case.status is LiveCaseStatus.UNRESOLVED
    assert case.verifier["outcome"] == "NO_OP"


def test_live_model_triggered_pdb_uses_contained_session_and_returns_observations(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(live_quixbugs, "QuixBugsSourceAcquirer", FakeSourceAcquirer)
    monkeypatch.setattr(live_quixbugs, "to_wsl_path", lambda path, distro: "/fake/pdb-runtime-bundle")
    monkeypatch.setattr(live_quixbugs, "ContainedPdbSession", FakeContainedPdbSession)
    FakeContainedPdbSession.starts = 0
    facts = _fake_facts(tmp_path, authorized=True)
    sources_parent = _prepare_pinned_source(tmp_path)
    transport = PdbScriptedTransport(use_pdb=True)

    case = live_quixbugs.run_live_quixbugs_case(
        repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(sources_parent),
        facts=facts, config=pdb_config(), limits=limits(), transport=transport,
        evaluation_id="quixbugs-gcd-pdb-test", policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        runtime_probe=live_quixbugs.QUIXBUGS_GCD_RUNTIME_PROBE,
    )

    assert case.status is LiveCaseStatus.CONTROLLER_FAILED
    assert FakeContainedPdbSession.starts == 1
    assert case.policy == "pdb-on-uncertainty"
    assert case.measurements["successful_pdb_observation_count"] == 2
    assert case.measurements["failed_pdb_observation_count"] == 0
    assert case.evidence is not None
    assert case.evidence["contained_preflight"]["authorized"] is True
    assert any(item["allowed"] is True for item in case.evidence["pdb_gate_decisions"])
    event_names = {step.get("name") for step in json.loads("[" + ",".join(line for line in case.events_jsonl.splitlines()) + "]")}
    assert event_names >= {"start_pdb_session", "get_stack_summary", "get_frame_locals", "stop_pdb_session"}
    assert list((tmp_path / "external").iterdir()) == []


def test_pdb_policy_without_model_triggered_observation_is_not_reachability_success(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(live_quixbugs, "QuixBugsSourceAcquirer", FakeSourceAcquirer)
    monkeypatch.setattr(live_quixbugs, "to_wsl_path", lambda path, distro: "/fake/pdb-runtime-bundle")
    monkeypatch.setattr(live_quixbugs, "ContainedPdbSession", FakeContainedPdbSession)
    monkeypatch.setattr(live_quixbugs, "EvaluationVerifier", FakeVerifier)
    FakeVerifier.status = "COMPLETED"
    FakeVerifier.outcome = "RESOLVED"
    facts = _fake_facts(tmp_path, authorized=True)
    sources_parent = _prepare_pinned_source(tmp_path)

    case = live_quixbugs.run_live_quixbugs_case(
        repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(sources_parent),
        facts=facts, config=pdb_config(), limits=limits(), transport=PdbScriptedTransport(use_pdb=False),
        evaluation_id="quixbugs-gcd-pdb-no-reach", policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        runtime_probe=live_quixbugs.QUIXBUGS_GCD_RUNTIME_PROBE,
    )

    assert case.status is LiveCaseStatus.PDB_NOT_REACHED
    assert case.measurements["successful_pdb_observation_count"] == 0
    assert case.verifier["outcome"] == "RESOLVED"



def test_evaluation_report_is_schema_valid_and_single_case(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(live_quixbugs, "QuixBugsSourceAcquirer", FakeSourceAcquirer)
    FakeVerifier.calls = []
    FakeVerifier.status = "COMPLETED"
    FakeVerifier.outcome = "RESOLVED"
    monkeypatch.setattr(live_quixbugs, "EvaluationVerifier", FakeVerifier)

    facts = _fake_facts(tmp_path, authorized=True)
    sources_parent = _prepare_pinned_source(tmp_path)
    transport = RecordingScriptedTransport(FIX_PATCH)

    report = live_quixbugs.run_live_quixbugs_evaluation(
        repository_root=str(ROOT), authorization=LiveExecutionAuthorization.authorize(True, True),
        manifest_path=str(MANIFEST), sources_parent=str(sources_parent), facts=facts, config=config(), limits=limits(),
        transport_factory=lambda: transport,
    )
    validated = validate_live_report(report)
    assert validated["selected_tasks"] == [adapter().manifest.task_id]
    assert validated["selected_policies"] == ["static-baseline"]
    assert validated["repetitions"] == 1
    assert validated["expected_case_count"] == 1
    assert len(validated["cases"]) == 1
    assert validated["completion"] == "complete"


# ---- task-local PDB probe ---------------------------------------------------

FIND_IN_SORTED_MANIFEST = ROOT / "research" / "quixbugs" / "FIND_IN_SORTED_SMOKE_MANIFEST_V1.json"

#: Minimal fake buggy module for find_in_sorted whose own ``mid = ...``
#: statement inside ``binsearch`` is the reviewed probe anchor.  Never
#: executed: discovery is fully intercepted by the fake containment runner.
FIND_IN_SORTED_BUGGY = (
    "def find_in_sorted(arr, x):\n"
    "    return binsearch(0, len(arr), arr, x)\n"
    "\n"
    "def binsearch(low, high, arr, x):\n"
    "    mid = (low + high) // 2\n"
    "    return mid\n"
)
FIND_IN_SORTED_CORRECTED = FIND_IN_SORTED_BUGGY.replace("return mid\n", "return arr[mid]\n")


def _find_in_sorted_probe() -> RuntimeProbe:
    return RuntimeProbe(
        module_path="python_programs/find_in_sorted.py",
        focus_function="binsearch",
        call_source="find_in_sorted([1, 2], 3)",
        anchor="mid =",
        inspect_expressions=("arr", "x"),
    )


def test_non_gcd_pdb_case_accepts_its_reviewed_task_local_probe(tmp_path: Path, monkeypatch) -> None:
    """A non-GCD PDB case is no longer rejected merely because it is
    non-GCD: with the explicit task-local probe reviewed for
    quixbugs-find-in-sorted-smoke-v1 the full contained-PDB pipeline runs."""
    monkeypatch.setattr(live_quixbugs, "QuixBugsSourceAcquirer", FakeSourceAcquirer)
    monkeypatch.setattr(live_quixbugs, "to_wsl_path", lambda path, distro: "/fake/pdb-runtime-bundle")
    monkeypatch.setattr(live_quixbugs, "ContainedPdbSession", FakeContainedPdbSession)
    FakeContainedPdbSession.starts = 0
    task_adapter = QuixBugsAdapter.from_manifest(FIND_IN_SORTED_MANIFEST)
    facts = _fake_facts(tmp_path, authorized=True, task_adapter=task_adapter)
    sources_parent = _prepare_pinned_source(
        tmp_path, algorithm="find_in_sorted", buggy=FIND_IN_SORTED_BUGGY, corrected=FIND_IN_SORTED_CORRECTED,
    )
    transport = PdbScriptedTransport(use_pdb=True)

    case = live_quixbugs.run_live_quixbugs_case(
        repository_root=str(ROOT), manifest_path=str(FIND_IN_SORTED_MANIFEST), sources_parent=str(sources_parent),
        facts=facts, config=pdb_config(), limits=limits(), transport=transport,
        evaluation_id="quixbugs-find-in-sorted-pdb-test", policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        runtime_probe=_find_in_sorted_probe(),
    )

    assert case.status is LiveCaseStatus.CONTROLLER_FAILED
    assert case.policy == "pdb-on-uncertainty"
    assert case.task_id == "quixbugs-find-in-sorted-smoke-v1"
    assert FakeContainedPdbSession.starts == 1
    assert case.measurements["successful_pdb_observation_count"] == 2
    assert case.evidence is not None
    assert case.evidence["contained_preflight"]["authorized"] is True
    assert case.evidence["launch_plan"]["target"] == "python_programs/find_in_sorted.py"
    assert case.evidence["launch_plan"]["breakpoints"] == [5]
    assert any(item["allowed"] is True for item in case.evidence["pdb_gate_decisions"])
    assert list((tmp_path / "external").iterdir()) == []


def test_pdb_probe_validation_rejects_mismatched_module(tmp_path: Path) -> None:
    facts = _fake_facts(tmp_path, authorized=True)
    wrong_probe = RuntimeProbe(
        module_path="python_programs/hanoi.py", focus_function="hanoi",
        call_source="hanoi(2)", anchor="helper =", inspect_expressions=("height",),
    )
    with pytest.raises(live_quixbugs.QuixBugsLiveConfigurationError, match="not the selected task buggy path"):
        live_quixbugs.run_live_quixbugs_case(
            repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(tmp_path / "sources"),
            facts=facts, config=pdb_config(), limits=limits(), transport=NeverCalledTransport(),
            policy=DemoPolicy.PDB_ON_UNCERTAINTY, runtime_probe=wrong_probe,
        )


def test_pdb_probe_validation_rejects_corrected_path_probe(tmp_path: Path) -> None:
    facts = _fake_facts(tmp_path, authorized=True)
    corrected_probe = RuntimeProbe(
        module_path="correct_python_programs/gcd.py", focus_function="gcd",
        call_source="gcd(35, 21)", anchor="return gcd(", inspect_expressions=("a", "b"),
    )
    with pytest.raises(live_quixbugs.QuixBugsLiveConfigurationError, match="corrected, test, or support material"):
        live_quixbugs.run_live_quixbugs_case(
            repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(tmp_path / "sources"),
            facts=facts, config=pdb_config(), limits=limits(), transport=NeverCalledTransport(),
            policy=DemoPolicy.PDB_ON_UNCERTAINTY, runtime_probe=corrected_probe,
        )


def test_pdb_probe_validation_rejects_unreviewed_focus_symbol(tmp_path: Path) -> None:
    facts = _fake_facts(tmp_path, authorized=True)
    unreviewed_probe = RuntimeProbe(
        module_path="python_programs/gcd.py", focus_function="not_a_reviewed_symbol",
        call_source="gcd(35, 21)", anchor="return gcd(", inspect_expressions=("a", "b"),
    )
    with pytest.raises(live_quixbugs.QuixBugsLiveConfigurationError, match="not a reviewed target symbol"):
        live_quixbugs.run_live_quixbugs_case(
            repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(tmp_path / "sources"),
            facts=facts, config=pdb_config(), limits=limits(), transport=NeverCalledTransport(),
            policy=DemoPolicy.PDB_ON_UNCERTAINTY, runtime_probe=unreviewed_probe,
        )


def test_pdb_probe_validation_rejects_non_runtime_probe_type(tmp_path: Path) -> None:
    facts = _fake_facts(tmp_path, authorized=True)
    with pytest.raises(live_quixbugs.QuixBugsLiveConfigurationError, match="exact RuntimeProbe"):
        live_quixbugs.run_live_quixbugs_case(
            repository_root=str(ROOT), manifest_path=str(MANIFEST), sources_parent=str(tmp_path / "sources"),
            facts=facts, config=pdb_config(), limits=limits(), transport=NeverCalledTransport(),
            policy=DemoPolicy.PDB_ON_UNCERTAINTY, runtime_probe={"module_path": "python_programs/gcd.py"},
        )


def test_pdb_probe_validation_rejects_unresolvable_anchor(tmp_path: Path) -> None:
    task_adapter = QuixBugsAdapter.from_manifest(FIND_IN_SORTED_MANIFEST)
    facts = _fake_facts(tmp_path, authorized=True, task_adapter=task_adapter)
    sources_parent = _prepare_pinned_source(
        tmp_path, algorithm="find_in_sorted", buggy=FIND_IN_SORTED_BUGGY, corrected=FIND_IN_SORTED_CORRECTED,
    )
    bad_probe = RuntimeProbe(
        module_path="python_programs/find_in_sorted.py", focus_function="binsearch",
        call_source="find_in_sorted([1, 2], 3)", anchor="low = 0", inspect_expressions=("arr", "x"),
    )
    with pytest.raises(live_quixbugs.QuixBugsLiveConfigurationError, match="exactly one match is required"):
        live_quixbugs.run_live_quixbugs_case(
            repository_root=str(ROOT), manifest_path=str(FIND_IN_SORTED_MANIFEST), sources_parent=str(sources_parent),
            facts=facts, config=pdb_config(), limits=limits(), transport=NeverCalledTransport(),
            policy=DemoPolicy.PDB_ON_UNCERTAINTY, runtime_probe=bad_probe,
        )


def test_gcd_legacy_default_apis_remain_unchanged(tmp_path: Path) -> None:
    """The historical standalone GCD APIs and their default GCD lock are
    preserved: the gcd probe wrapper still prepares the default gcd probe,
    the live evaluation entry point stays gcd-locked for PDB, and the gcd
    task constant is unchanged."""
    from agentic_debugger.quixbugs.contained_pdb import (
        QUIXBUGS_PDB_TASK_ID,
        prepare_quixbugs_gcd_pdb_probe,
        prepare_quixbugs_pdb_probe,
    )

    assert QUIXBUGS_PDB_TASK_ID == "quixbugs-gcd-smoke-v1"
    assert live_quixbugs.QUIXBUGS_PDB_TASK_ID == "quixbugs-gcd-smoke-v1"
    assert live_quixbugs.QUIXBUGS_PDB_MODEL_ID == "opencode/deepseek-v4-flash-free"
    assert live_quixbugs.QUIXBUGS_PDB_VARIANT == "max"

    sources_parent = _prepare_pinned_source(tmp_path)
    project_root = sources_parent / "quixbugs"
    legacy_parent = tmp_path / "legacy-parent"
    explicit_parent = tmp_path / "explicit-parent"
    legacy_parent.mkdir()
    explicit_parent.mkdir()
    legacy = prepare_quixbugs_gcd_pdb_probe(project_root, legacy_parent)
    explicit = prepare_quixbugs_pdb_probe(project_root, explicit_parent, live_quixbugs.QUIXBUGS_GCD_RUNTIME_PROBE)
    assert legacy.script == explicit.script == "python_programs/gcd.py"
    assert legacy.breakpoint_line == explicit.breakpoint_line
    assert legacy.focus_function == explicit.focus_function == "gcd"

    facts = _fake_facts(tmp_path, authorized=True)
    with pytest.raises(LiveConfigurationError, match=r"locked to '?quixbugs-gcd-smoke-v1'?"):
        live_quixbugs.run_live_quixbugs_evaluation(
            repository_root=str(ROOT), authorization=LiveExecutionAuthorization.authorize(True, True),
            manifest_path=str(ROOT / "research" / "quixbugs" / "HANOI_SMOKE_MANIFEST_V1.json"),
            sources_parent=str(tmp_path / "sources"), facts=facts, config=pdb_config(), limits=limits(),
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        )
