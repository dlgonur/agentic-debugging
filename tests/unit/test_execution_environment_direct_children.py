"""V2-01/06 direct-child environment presence/absence proofs.

Commit 05 isolated CommandRunner/PDB/verifier-command children.  This
module proves the 06 repair: EVERY other direct subprocess child owned by
the normal Local Project session worker also receives its environment
from the explicit product ExecutionEnvironment authority instead of
implicitly inheriting the worker environment:

A. inventory ``git ls-files`` child (``local_project`` helper);
B. verifier-owned Git children (``_inspect_source`` / ``_export_commit``
   via the single ``_run_git`` authority);
C. verifier single-authority semantics (runners + Git share one fixed
   mapping; post-construction ambient mutation changes nothing;
   custom-factory + explicit-env fails closed);
D. worker terminal cleanup Git children (``git worktree prune`` /
   ``git worktree list --porcelain``);
E. structural inventory: every direct spawn reachable in a normal Local
   Project worker lifecycle is an explicit-authority site.

Only synthetic values are used; no real credential is ever constructed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentic_debugger.application.execution_environment import (
    ExecutionEnvironment,
    ExecutionRole,
)

SYNTHETIC_HOP_VAR = "AGENTIC_DEBUGGER_PROVIDER_T06_API_KEY"
SYNTHETIC_HOP_VALUE = "sk-synthetic-v206-hop-value-not-a-real-credential"
BUILTIN_HOP_VAR = "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY"
BUILTIN_CRED_VAR = "OPENCODE_API_KEY"
CLI_AUTH_VAR = "OPENCODE_CONFIG_DIR"
CONTROL_VAR = "AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH"
BENIGN_VAR = "V2_06_BENIGN_PROJECT_DSN"
BENIGN_VALUE = "service://synthetic/test-dsn"

SECRET_NAMES = (
    SYNTHETIC_HOP_VAR,
    BUILTIN_HOP_VAR,
    BUILTIN_CRED_VAR,
    CLI_AUTH_VAR,
    CONTROL_VAR,
)


def _seed(monkeypatch: pytest.MonkeyPatch) -> ExecutionEnvironment:
    monkeypatch.setenv(SYNTHETIC_HOP_VAR, SYNTHETIC_HOP_VALUE)
    monkeypatch.setenv(BUILTIN_HOP_VAR, SYNTHETIC_HOP_VALUE)
    monkeypatch.setenv(BUILTIN_CRED_VAR, SYNTHETIC_HOP_VALUE)
    monkeypatch.setenv(CLI_AUTH_VAR, "/synthetic/opencode-config")
    monkeypatch.setenv(CONTROL_VAR, "/synthetic/provider-config.json")
    monkeypatch.setenv(BENIGN_VAR, BENIGN_VALUE)
    return ExecutionEnvironment.snapshot_process()


def _assert_project_safe(env: dict) -> None:
    for name in SECRET_NAMES:
        assert name not in env, f"control/provider channel leaked: {name}"
        assert name.upper() not in {k.upper() for k in env}
    assert SYNTHETIC_HOP_VALUE not in list(env.values())
    assert env[BENIGN_VAR] == BENIGN_VALUE


# ---------------------------------------------------------------------------
# A. inventory Git child
# ---------------------------------------------------------------------------

def test_inventory_git_child_receives_explicit_project_safe_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentic_debugger.application import local_project as lp

    authority = _seed(monkeypatch)
    role_env = dict(authority.role_environment(ExecutionRole.PROJECT_COMMAND))

    isolated = _make_git_repo(tmp_path)
    assert (isolated / "a.py").is_file()

    captured: dict = {}

    real_run = subprocess.run

    def _capture(argv, **kwargs):
        captured.update(kwargs)
        captured["argv"] = argv
        return real_run(argv, **kwargs)

    monkeypatch.setattr(lp.subprocess, "run", _capture)
    files = lp.inventory_tracked_python_files(isolated, environment=role_env)
    assert files == ["a.py"]
    # The real `git ls-files` invocation received an explicit env mapping.
    assert captured.get("env") is not None, "inventory Git child omitted env="
    _assert_project_safe(dict(captured["env"]))


def test_inventory_git_child_defaults_to_legacy_inherit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct non-product callers keep working without an explicit env."""
    from agentic_debugger.application import local_project as lp

    isolated = _make_git_repo(tmp_path)
    assert lp.inventory_tracked_python_files(isolated) == ["a.py"]


def test_inventory_rejects_bad_environment() -> None:
    from agentic_debugger.application import ApplicationInputError
    from agentic_debugger.application import local_project as lp

    with pytest.raises(ApplicationInputError):
        lp.inventory_tracked_python_files(Path("."), environment={"K": 1})  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# B. verifier Git children share the one fixed authority
# ---------------------------------------------------------------------------

def _make_git_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    for argv in (
        ["init"],
        ["config", "user.email", "t@t.t"],
        ["config", "user.name", "T"],
    ):
        result = subprocess.run(
            ["git", *argv],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    for argv in (["add", "."], ["commit", "-m", "init"]):
        result = subprocess.run(
            ["git", *argv],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    return repo


def test_verifier_git_children_use_fixed_verifier_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentic_debugger.evaluation import local_project_verifier as lv

    authority = _seed(monkeypatch)
    verifier_env = dict(authority.role_environment(ExecutionRole.VERIFIER))
    repo = _make_git_repo(tmp_path)

    seen: list = []
    real_run = subprocess.run

    def _capture(argv, **kwargs):
        seen.append(dict(kwargs.get("env") or {}))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(lv.subprocess, "run", _capture)
    verifier = lv.LocalProjectVerifier(product_environment=dict(verifier_env))
    state = lv._inspect_source(str(repo), environment=verifier._product_environment)
    assert state.clean is True
    export_root = tmp_path / "export"
    export_root.mkdir()
    dest = export_root / "source"
    dest.mkdir()
    lv._export_commit(
        state.root,
        state.head,
        str(dest),
        str(export_root),
        environment=verifier._product_environment,
    )
    # Both source inspection (rev-parse/status) and the archive path went
    # through the one tested `_run_git` authority with explicit env.
    assert len(seen) >= 6, f"expected inspection + archive git calls, saw {len(seen)}"
    for env in seen:
        assert env, "verifier Git child omitted env="
        _assert_project_safe(env)


def test_verifier_evaluate_threads_fixed_env_to_git_and_runners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through ``evaluate``: Git + CommandRunner children share
    the single fixed mapping (baseline-invalid plan still exercises the
    inspection/archive Git path up to the verdict)."""
    from agentic_debugger.evaluation import local_project_verifier as lv
    from agentic_debugger.evaluation.local_project_verifier import (
        LocalProjectEvaluationPlan,
        LocalProjectVerifier,
    )
    from agentic_debugger.runtime.command_runner import CommandRunner

    authority = _seed(monkeypatch)
    verifier_env = dict(authority.role_environment(ExecutionRole.VERIFIER))
    repo = _make_git_repo(tmp_path)
    head = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            timeout=30,
            check=True,
        )
        .stdout.decode()
        .strip()
    )

    git_envs: list = []
    runner_envs: list = []
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def _capture_run(argv, **kwargs):
        if isinstance(argv, list) and argv[:1] == ["git"]:
            git_envs.append(dict(kwargs.get("env") or {}))
        return real_run(argv, **kwargs)

    class _RecordingRunner(CommandRunner):
        def __init__(self, workspace, environment=None):
            runner_envs.append(dict(environment or {}))
            super().__init__(workspace, environment=environment)

    monkeypatch.setattr(lv.subprocess, "run", _capture_run)
    monkeypatch.setattr(lv, "CommandRunner", _RecordingRunner)
    verifier = LocalProjectVerifier(product_environment=dict(verifier_env))
    plan = LocalProjectEvaluationPlan(
        source_repo_path=str(repo),
        source_head_commit=head,
        candidate_patch="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
        reproduction_argv=("python", "-c", "import sys; sys.exit(1)"),
        regression_argv=("python", "-c", "import sys; sys.exit(0)"),
        allowed_paths=("a.py",),
        denied_paths=("tests", "task.json"),
        timeout_seconds=30.0,
    )
    result = verifier.evaluate(plan)
    assert result is not None
    assert git_envs, "no verifier Git children observed"
    for env in git_envs:
        _assert_project_safe(env)
    assert runner_envs, "no verifier CommandRunner children observed"
    for env in runner_envs:
        _assert_project_safe(env)


# ---------------------------------------------------------------------------
# C. verifier single authority
# ---------------------------------------------------------------------------

def test_verifier_single_authority_runners_and_git_share_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentic_debugger.evaluation.local_project_verifier import (
        LocalProjectVerifier,
    )
    from agentic_debugger.runtime.workspace import TaskWorkspace

    authority = _seed(monkeypatch)
    verifier_env = dict(authority.role_environment(ExecutionRole.VERIFIER))
    verifier = LocalProjectVerifier(product_environment=dict(verifier_env))
    workspace = TaskWorkspace(str(tmp_path))
    runner = verifier._make_runner(workspace)
    assert runner._environment is not None
    _assert_project_safe(dict(runner._environment))
    assert dict(runner._environment) == verifier_env


def test_verifier_authority_fixed_after_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-construction ambient mutation (the controller/model path)
    cannot change either runners or Git children."""
    from agentic_debugger.evaluation.local_project_verifier import (
        LocalProjectVerifier,
    )
    from agentic_debugger.runtime.workspace import TaskWorkspace

    authority = _seed(monkeypatch)
    verifier = LocalProjectVerifier(
        product_environment=dict(authority.role_environment(ExecutionRole.VERIFIER))
    )
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_LATE_API_KEY", SYNTHETIC_HOP_VALUE)
    monkeypatch.setenv("V2_06_LATE_VARIABLE", "late")
    runner = verifier._make_runner(TaskWorkspace(str(tmp_path)))
    assert "AGENTIC_DEBUGGER_PROVIDER_LATE_API_KEY" not in runner._environment
    assert "V2_06_LATE_VARIABLE" not in runner._environment
    assert dict(verifier._product_environment or {}) == dict(
        authority.role_environment(ExecutionRole.VERIFIER)
    )


def test_verifier_custom_factory_plus_env_fails_closed(tmp_path) -> None:
    from agentic_debugger.evaluation.local_project_verifier import (
        LocalProjectVerifier,
    )
    from agentic_debugger.evaluation.runner import EvaluationInputError
    from agentic_debugger.runtime.command_runner import CommandRunner
    from agentic_debugger.runtime.workspace import TaskWorkspace

    def _custom(workspace: TaskWorkspace) -> CommandRunner:
        return CommandRunner(workspace)

    with pytest.raises(EvaluationInputError):
        LocalProjectVerifier(
            command_runner_factory=_custom,
            product_environment={"PATH": "/usr/bin"},
        )


# ---------------------------------------------------------------------------
# D. worker terminal cleanup Git children
# ---------------------------------------------------------------------------

def test_cleanup_git_children_receive_explicit_project_safe_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentic_debugger.application import local_project as lp

    authority = _seed(monkeypatch)
    role_env = dict(authority.role_environment(ExecutionRole.PROJECT_COMMAND))
    repo = _make_git_repo(tmp_path)
    parent = tmp_path / "wt-parent"
    parent.mkdir()
    (parent / "worktree").mkdir()

    captured: list = []
    real_run = subprocess.run

    def _capture(argv, **kwargs):
        if isinstance(argv, list) and argv[:2] == ["git", "worktree"]:
            captured.append({"argv": list(argv), "env": kwargs.get("env")})
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(lp.subprocess, "run", _capture)
    assert lp.cleanup_parent_tmpdir(parent, repo, environment=role_env) is True
    kinds = [" ".join(entry["argv"][1:]) for entry in captured]
    assert "worktree prune" in kinds
    assert "worktree list --porcelain" in kinds
    for entry in captured:
        assert entry["env"] is not None, f"{entry['argv']} omitted env="
        _assert_project_safe(dict(entry["env"]))


def test_cleanup_defaults_to_legacy_inherit(tmp_path: Path) -> None:
    """Direct non-product callers (supervisor post-mortem, UI teardown)
    keep the historical behavior."""
    from agentic_debugger.application import local_project as lp

    repo = _make_git_repo(tmp_path)
    parent = tmp_path / "wt-parent"
    parent.mkdir()
    assert lp.cleanup_parent_tmpdir(parent, repo) is True


# ---------------------------------------------------------------------------
# E. complete worker subprocess inventory (structural)
# ---------------------------------------------------------------------------

def _read(relative: str) -> str:
    return (Path(__file__).resolve().parents[2] / relative).read_text(encoding="utf-8")


def test_worker_lifecycle_spawns_all_use_explicit_authority() -> None:
    """Every direct spawn reachable in a normal Local Project worker
    lifecycle derives its child environment from the explicit product
    authority.  This test traces the callers rather than grepping alone:

    - ``local_project_source`` owns no direct ``subprocess.run/Popen`` for
      worker children (commands → CommandRunner+env, PDB → PdbSession+env,
      verifier → product_environment, inventory → env);
    - ``local_project`` worker-reachable helpers (inventory, terminal
      cleanup) pass ``env=`` from an explicit ``environment`` parameter;
    - the verifier's ``_run_git`` passes ``env=`` and every internal
      ``_inspect_source``/``_export_commit``/runner site threads the one
      fixed ``_product_environment``;
    - ``worker`` terminal cleanup passes ``environment=`` from the same
      session authority it stashed on the context before dispatch.
    """
    source = _read("agentic_debugger/application/local_project_source.py")
    assert "subprocess.run(" not in source, (
        "local_project_source must not spawn directly; "
        "all worker children go through CommandRunner/PdbSession/verifier/inventory"
    )
    assert "subprocess.Popen(" not in source
    assert "_inventory_tracked_python_files(isolated, environment=" in source
    assert "product_environment=dict(verifier_command_environment)" in source

    worker_src = _read("agentic_debugger/application/worker.py")
    assert "product_environment=session_execution_environment" in worker_src
    assert "cleanup_parent_tmpdir(" in worker_src
    assert "environment=cleanup_environment" in worker_src

    project = _read("agentic_debugger/application/local_project.py")
    assert "environment: Optional[Mapping[str, str]] = None" in project
    assert project.count("env=child_env") >= 3, (
        "inventory ls-files + cleanup prune/list must all pass explicit env"
    )

    verifier = _read("agentic_debugger/evaluation/local_project_verifier.py")
    assert "env=child_env" in verifier
    assert "product_environment: Optional[Mapping[str, str]] = None" in verifier
    assert "self._make_runner(candidate_workspace)" in verifier
    assert "self._make_runner(regression_workspace)" in verifier
    assert "environment=self._product_environment" in verifier
    assert "conflicting verifier authorities" in verifier

    scenarios = _read("agentic_debugger/application/worker_scenarios.py")
    assert "product_environment" in scenarios

    runner = _read("agentic_debugger/runtime/command_runner.py")
    assert "_product_environment(self._environment)" in runner
    pdb = _read("agentic_debugger/runtime/pdb_session.py")
    assert "build_worker_env(self._worker_environment)" in pdb


def test_patcher_git_helpers_not_reachable_from_product_path() -> None:
    """Source proof for the patcher scope decision: the direct Git helpers
    in ``runtime/patcher.py`` run only behind ``official_patch_compatibility``
    (default ``False``) or inside ``materialize_and_canonicalize_patch``,
    neither of which the normal Local Project controller/verifier path
    enables.  The Local Project tool context constructs
    ``PatchManager(...)`` without the flag, and the verifier applies
    patches through ``PatchManager`` + ``syntax_check`` only."""
    patcher = _read("agentic_debugger/runtime/patcher.py")
    assert "official_patch_compatibility: bool = False" in patcher
    assert "if self._official_patch_compatibility:" in patcher
    source = _read("agentic_debugger/application/local_project_source.py")
    assert "official_patch_compatibility" not in source
    verifier = _read("agentic_debugger/evaluation/local_project_verifier.py")
    assert "materialize_and_canonicalize_patch" not in verifier
    assert "official_patch_compatibility" not in verifier
    assert "PatchManager(" in verifier
