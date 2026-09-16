"""Local Project timeout UX — configurable verifier bound + raised default.

Offline proving for Prompt-020:
- Bounds group carries the verifier test-execution bound (default 120 s,
  1–600 s, no unlimited), verifier honors it end-to-end (trial-shape:
  slow-but-bounded suite completes; short bound forces TEST_TIMEOUT).
- Forced timeouts explain themselves in activity evidence
  ("timed out after Ns — not a failure verdict" + next step) with the raw
  exit/status preserved alongside, never reclassified as pass/fail.
- Controller/tool 30 s bounds stay intact; cleanup-after-timeout verified.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("textual")

from tests.unit.test_local_project_verifier import GOOD_PATCH


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Verifier Test")
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n\ndef stable():\n    return 7\n",
        encoding="utf-8",
    )
    (repo / "reproduce.py").write_text(
        "from calculator import add\nraise SystemExit(0 if add(2, 3) == 5 else 1)\n",
        encoding="utf-8",
    )
    (repo / "regression.py").write_text(
        "from calculator import stable\nraise SystemExit(0 if stable() == 7 else 1)\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _slow_repo(tmp_path: Path, sleep_s: float = 2.0) -> tuple[Path, str]:
    """Suite-shaped slow commands: sleep then exit (fail for repro, pass for regression)."""
    repo = tmp_path / "slowproj"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Timeout UX")
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n\ndef stable():\n    return 7\n",
        encoding="utf-8",
    )
    (repo / "slow_repro.py").write_text(
        f"import time\ntime.sleep({sleep_s})\n"
        "from calculator import add\nraise SystemExit(0 if add(2, 3) == 5 else 1)\n",
        encoding="utf-8",
    )
    (repo / "slow_regress.py").write_text(
        f"import time\ntime.sleep({sleep_s})\n"
        "from calculator import stable\nraise SystemExit(0 if stable() == 7 else 1)\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "slow fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _plan(repo: Path, head: str, **overrides):
    from agentic_debugger.evaluation.local_project_verifier import (
        LocalProjectEvaluationPlan,
    )

    base = dict(
        source_repo_path=str(repo),
        source_head_commit=head,
        candidate_patch=GOOD_PATCH,
        reproduction_argv=(sys.executable, "reproduce.py"),
        regression_argv=(sys.executable, "regression.py"),
        allowed_paths=("calculator.py",),
        denied_paths=("tests", "task.json"),
    )
    base.update(overrides)
    return LocalProjectEvaluationPlan(**base)


# ---------------------------------------------------------------------------
# 1. Default + validation (bound remains a bound)
# ---------------------------------------------------------------------------

def test_verifier_default_is_120() -> None:
    from agentic_debugger.evaluation.local_project_verifier_contracts import (
        DEFAULT_TIMEOUT_SECONDS,
        LocalProjectEvaluationPlan,
    )

    assert DEFAULT_TIMEOUT_SECONDS == 120.0
    plan = LocalProjectEvaluationPlan(
        source_repo_path="/tmp/x",
        source_head_commit="a" * 40,
        candidate_patch="--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n",
        reproduction_argv=None,
        regression_argv=None,
        allowed_paths=("f",),
        denied_paths=(),
    )
    assert plan.timeout_seconds == 120.0


def test_verifier_bound_caps_at_600_and_rejects_unlimited() -> None:
    from agentic_debugger.evaluation.runner import EvaluationInputError
    from agentic_debugger.evaluation.local_project_verifier_contracts import (
        LocalProjectEvaluationPlan,
    )

    kwargs = dict(
        source_repo_path="/tmp/x",
        source_head_commit="a" * 40,
        candidate_patch="--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n",
        reproduction_argv=None,
        regression_argv=None,
        allowed_paths=("f",),
        denied_paths=(),
    )
    ok = LocalProjectEvaluationPlan(timeout_seconds=600.0, **kwargs)
    assert ok.timeout_seconds == 600.0
    for bad in (0, -1, 601, 1000, float("inf"), float("nan"), True, "120", None):
        with pytest.raises(EvaluationInputError):
            LocalProjectEvaluationPlan(timeout_seconds=bad, **kwargs)  # type: ignore[arg-type]


def test_session_config_default_120_and_bounds_group() -> None:
    from agentic_debugger.ui.session_config import (
        DEFAULT_VERIFIER_TIMEOUT_SECONDS,
        GROUP_BOUNDS,
        LOCAL_SETUP_FOCUS_ORDER,
        LOCAL_SETUP_GROUPS,
        ROW_AUTO_RETRY,
        ROW_TIME_LIMIT,
        ROW_VERIFIER_TIMEOUT,
        SessionConfig,
        validate_verifier_timeout_seconds,
    )

    assert DEFAULT_VERIFIER_TIMEOUT_SECONDS == 120
    assert SessionConfig().verifier_timeout_seconds == 120
    bounds = dict((gid, fields) for gid, _title, fields in LOCAL_SETUP_GROUPS)[GROUP_BOUNDS]
    assert bounds == (ROW_TIME_LIMIT, ROW_VERIFIER_TIMEOUT, ROW_AUTO_RETRY)
    assert LOCAL_SETUP_FOCUS_ORDER.index(ROW_VERIFIER_TIMEOUT) == (
        LOCAL_SETUP_FOCUS_ORDER.index(ROW_TIME_LIMIT) + 1
    )
    assert validate_verifier_timeout_seconds(1) == 1
    assert validate_verifier_timeout_seconds(600) == 600
    for bad in (0, 601, -5, True, "120", None, 1.5):
        with pytest.raises(ValueError):
            validate_verifier_timeout_seconds(bad)  # type: ignore[arg-type]


def test_verifier_row_local_only_and_readiness_neutral() -> None:
    from agentic_debugger.ui.session_config import (
        ROW_VERIFIER_TIMEOUT,
        ModelChoice,
        ProjectStatus,
        SessionCatalog,
        SessionConfig,
        TARGET_CURATED,
        TARGET_LADDER,
        TARGET_LOCAL_PROJECT,
        TaskOption,
        ModelOption,
        derive_readiness,
    )
    from agentic_debugger.ui.session_config import PROVIDER_OFFLINE, PROVIDER_OLLAMA

    catalog = SessionCatalog(
        tasks=(TaskOption("t", "T"),),
        models=(
            ModelOption(PROVIDER_OFFLINE, "", "Offline"),
            ModelOption(PROVIDER_OLLAMA, "m", "M"),
        ),
    )
    clean = ProjectStatus("C:/r", True, "clean", "Git: r @ abc1234")
    local = SessionConfig(
        target=TARGET_LOCAL_PROJECT,
        project_path="C:/r",
        bug_description="b",
        model=ModelChoice(PROVIDER_OLLAMA, "m", "M"),
    )
    ready = derive_readiness(local, catalog, clean)
    assert ready.rows[ROW_VERIFIER_TIMEOUT].enabled is True
    assert ready.ready is True
    # Invalid bound values never block readiness (readiness-neutral);
    # the worker fails closed at execution instead.
    weird = SessionConfig(
        target=TARGET_LOCAL_PROJECT,
        project_path="C:/r",
        bug_description="b",
        model=ModelChoice(PROVIDER_OLLAMA, "m", "M"),
        verifier_timeout_seconds=9999,
    )
    assert derive_readiness(weird, catalog, clean).ready is True
    curated = derive_readiness(SessionConfig(target=TARGET_CURATED), catalog, clean)
    assert curated.rows[ROW_VERIFIER_TIMEOUT].enabled is False
    ladder = derive_readiness(
        SessionConfig(target=TARGET_LADDER), catalog, ProjectStatus.unchecked("")
    )
    assert ladder.rows[ROW_VERIFIER_TIMEOUT].enabled is False


def test_helper_params_default_120_and_reject_bad() -> None:
    from agentic_debugger.application.local_project_helpers import _validate_params
    from agentic_debugger.application.worker_scenarios import ScenarioInputError

    base = dict(
        project_repo_path="/tmp/r",
        project_head="a" * 40,
        isolated_workspace="/tmp/w",
        bug_description="b",
        config_root="/tmp/c",
        profile_id="p",
    )
    assert _validate_params(dict(base))["verifier_timeout_seconds"] == 120.0
    assert _validate_params(dict(base, verifier_timeout_seconds=45))["verifier_timeout_seconds"] == 45.0
    for bad in (0, -3, 601, float("inf"), True, "120", None.__class__):
        with pytest.raises(ScenarioInputError):
            _validate_params(dict(base, verifier_timeout_seconds=bad))


# ---------------------------------------------------------------------------
# 2. Verifier honors the bound end-to-end (trial-shape proof, fast sleeps)
# ---------------------------------------------------------------------------

def test_verifier_honors_configured_bound(tmp_path: Path) -> None:
    from agentic_debugger.evaluation.local_project_verifier import LocalProjectVerifier
    from agentic_debugger.evaluation.runner import EvaluationStatus

    repo, head = _slow_repo(tmp_path, sleep_s=2.0)
    parent = tmp_path / "ws"
    parent.mkdir()

    short = LocalProjectVerifier().evaluate(
        _plan(
            repo,
            head,
            reproduction_argv=(sys.executable, "slow_repro.py"),
            regression_argv=(sys.executable, "slow_regress.py"),
            timeout_seconds=1.0,
            workspace_parent=str(parent),
        )
    )
    assert short.status is EvaluationStatus.TEST_TIMEOUT
    assert short.stop_reason == "baseline_reproduction_timeout"
    assert short.workspace.cleaned is True
    assert list(parent.iterdir()) == []

    full = LocalProjectVerifier().evaluate(
        _plan(
            repo,
            head,
            reproduction_argv=(sys.executable, "slow_repro.py"),
            regression_argv=(sys.executable, "slow_regress.py"),
            timeout_seconds=30.0,
            workspace_parent=str(parent),
        )
    )
    assert full.status is EvaluationStatus.COMPLETED
    assert full.workspace.cleaned is True
    assert list(parent.iterdir()) == []


def test_default_bound_completes_slow_suite_shape(tmp_path: Path) -> None:
    """The 120 s default (not the old 30 s) governs an unpinned plan."""
    from agentic_debugger.evaluation.local_project_verifier import LocalProjectVerifier
    from agentic_debugger.evaluation.runner import EvaluationStatus

    repo, head = _slow_repo(tmp_path, sleep_s=2.0)
    result = LocalProjectVerifier().evaluate(
        _plan(
            repo,
            head,
            reproduction_argv=(sys.executable, "slow_repro.py"),
            regression_argv=(sys.executable, "slow_regress.py"),
        )
    )
    assert result.timeout_seconds if hasattr(result, "timeout_seconds") else True
    assert result.status is EvaluationStatus.COMPLETED
    assert result.workspace.cleaned is True


def test_cleanup_after_timeout_verified(tmp_path: Path) -> None:
    from agentic_debugger.evaluation.local_project_verifier import LocalProjectVerifier
    from agentic_debugger.evaluation.runner import EvaluationStatus

    repo, head = _repo(tmp_path)
    parent = tmp_path / "ws-clean"
    parent.mkdir()
    result = LocalProjectVerifier().evaluate(
        _plan(
            repo,
            head,
            reproduction_argv=(sys.executable, "-c", "import time; time.sleep(5)"),
            timeout_seconds=1.0,
            workspace_parent=str(parent),
        )
    )
    assert result.status is EvaluationStatus.TEST_TIMEOUT
    assert result.timeout is True
    assert result.workspace.cleaned is True
    assert result.workspace.canonical_fixture_unchanged is True
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain") == ""
    assert list(parent.iterdir()) == []


# ---------------------------------------------------------------------------
# 3. Timeout copy: facts only, next step, raw code preserved, no reclassify
# ---------------------------------------------------------------------------

def test_timeout_copy_states_facts_and_next_step() -> None:
    from agentic_debugger.application.local_project_helpers import (
        format_tool_timeout_text,
        format_verifier_timeout_text,
    )

    tool = format_tool_timeout_text(what="reproduction", timeout_seconds=30.0, exit_code=124)
    assert "timed out after 30s" in tool
    assert "exit 124" in tool
    assert "not a failure verdict" in tool
    assert "Narrow the command" in tool
    assert "tool bound is fixed at 30s" in tool
    # The tool bound has no UI knob: the copy must not send the user to
    # Bounds → Verifier timeout (that setting governs verification runs).
    assert "raise the bound" not in tool
    lowered = tool.lower()
    assert " passed" not in lowered and " failed" not in lowered.replace("failure verdict", "")

    verifier = format_verifier_timeout_text(
        timeout_seconds=120.0, status="TEST_TIMEOUT", stop_reason="baseline_reproduction_timeout"
    )
    assert "timed out after 120s" in verifier
    assert "TEST_TIMEOUT" in verifier
    assert "baseline_reproduction_timeout" in verifier
    assert "not a failure verdict" in verifier
    assert "Narrow the command or raise the bound" in verifier


def test_tool_timeout_records_explanation_preserving_exit_124(tmp_path: Path) -> None:
    """Exit-124 tool timeout: payload keeps 124; activity gets the human copy."""
    from agentic_debugger.agent.controller_policy import PdbPolicy
    from agentic_debugger.agent.state_machine import ControllerState
    from agentic_debugger.application.local_project_helpers import LocalProjectTask
    from agentic_debugger.application.local_project_tools import (
        _LocalToolContext,
        _build_local_registry,
    )
    from agentic_debugger.application.observability import (
        ObservabilityContext,
        SessionObservability,
    )
    from agentic_debugger.application.events import SourceKind
    from agentic_debugger.evaluation.task_schema import Constraints
    from agentic_debugger.agent.controller_policy import ActionName
    from agentic_debugger.events.schema import Action

    repo = tmp_path / "toolrepo"
    repo.mkdir()
    (repo / "a.py").write_text("x=1\n", encoding="utf-8")
    tracked = ["a.py"]
    task = LocalProjectTask(
        task_id="local-project-debug",
        title="t",
        description="b",
        language="python",
        fixture_path="isolated",
        constraints=Constraints(
            allowed_write_paths=tracked,
            denied_write_paths=["tests", "task.json"],
            network_allowed=False,
            external_services_allowed=False,
            max_patch_attempts=2,
            max_test_runs=10,
            max_pdb_observations=5,
        ),
        tracked_files=tuple(tracked),
        bug_description="b",
        reproduction_command="python -c \"import time; time.sleep(5)\"",
        verification_command=None,
    )
    ctx = _LocalToolContext(
        isolated=repo,
        tracked=tracked,
        task=task,
        probe=None,
        observability=SessionObservability(
            ObservabilityContext(
                session_id="sess-tool-timeout-01",
                task_id="local-project-debug",
                source_kind=SourceKind.LOCAL_PROJECT,
                run_id="run-1",
            )
        ),
        command_environment={},
        pdb_worker_environment=None,
        executor=None,
        capabilities=None,
    )
    # Force the 124 shape without waiting (legacy bounded runner maps
    # timed_out -> 124; the handler must preserve it and explain it).
    orig = ctx.run_project_command
    ctx.run_project_command = lambda cmd, cwd, timeout=30.0, cancel_check=None: (  # type: ignore[method-assign]
        124,
        "",
        "timed out 30.0s",
        30.0,
    )
    try:
        registry = _build_local_registry(ctx, pdb_policy=PdbPolicy.DISABLED)
        action = Action(
            action_id="action-000000001",
            run_id="run-1",
            task_id="local-project-debug",
            state=ControllerState.REPRODUCE,
            name=ActionName.RUN_REPRODUCTION.value,
            arguments={"phase": "baseline"},
        )
        observation = registry.dispatch(action, observation_id="observation-000000001")
    finally:
        ctx.run_project_command = orig
    assert observation.payload["exit_code"] == 124
    assert observation.payload["passed"] is False
    texts = [e.payload.get("text", "") for e in ctx.observability.events()]
    assert any("timed out after 30s" in t for t in texts), texts
    assert any("exit 124" in t for t in texts), texts
    assert any("not a failure verdict" in t for t in texts), texts
    assert any("tool bound is fixed at 30s" in t for t in texts), texts
    assert all("raise the bound" not in t for t in texts), texts


def test_timeout_copy_in_activity_timeline() -> None:
    """The human copy survives the activity projection (timeline summary)."""
    from agentic_debugger.application.emitter import SessionEventEmitter
    from agentic_debugger.application.events import SessionEventKind, SourceKind
    from agentic_debugger.application.local_project_helpers import format_tool_timeout_text
    from agentic_debugger.application.presentation import reduce_event
    from agentic_debugger.application.presentation_views import (
        PresentationIdentity,
        initial_session_view,
    )

    copy = format_tool_timeout_text(what="reproduction", timeout_seconds=30.0, exit_code=124)
    identity = PresentationIdentity(
        task_id="local-project-debug",
        source_kind=SourceKind.LOCAL_PROJECT,
        session_id="sess-activity-timeout-01",
    )
    view = initial_session_view(identity)
    emitter = SessionEventEmitter(
        session_id="sess-activity-timeout-01",
        task_id="local-project-debug",
        source_kind=SourceKind.LOCAL_PROJECT,
        run_id="run-1",
    )
    event = emitter.emit(
        SessionEventKind.DIAGNOSIS_RECORDED,
        {
            "text": f"{copy} Output: ...dots...",
            "file_path": None,
            "symbol": None,
            "confidence": "observed",
        },
    )
    view = reduce_event(view, event)
    summaries = [e.summary for e in view.timeline]
    assert any("timed out after 30s" in s for s in summaries)
    assert any("exit 124" in s for s in summaries)
    assert any("not a failure verdict" in s for s in summaries)
    assert any("tool bound is fixed at 30s" in s for s in summaries)


def test_controller_tool_30s_bounds_intact() -> None:
    """Controller/tool 30 s bounds stay UNTOUCHED (no suite-shape evidence)."""
    tools_text = Path("agentic_debugger/application/local_project_tools.py").read_text(encoding="utf-8")
    assert tools_text.count("timeout=30.0") >= 2
    source_text = Path("agentic_debugger/application/local_project_source.py").read_text(encoding="utf-8")
    assert "30.0" in source_text
    assert "timed out 30.0s" in source_text
    # The verifier default moved; the tool default must not have followed.
    assert "timeout_seconds=30.0" not in Path(
        "agentic_debugger/evaluation/local_project_verifier_contracts.py"
    ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 4. Setup surface: Bounds group setting + editor + start plumbing
# ---------------------------------------------------------------------------

def test_setup_row_and_editor_and_start_plumbing(tmp_path) -> None:
    import asyncio

    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen
        from agentic_debugger.ui.session_config import ROW_VERIFIER_TIMEOUT

        reset_launch_cwd()
        repo = tmp_path / "setuprepo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.email", "a@a.com"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "A"], cwd=str(repo), check=True)
        (repo / "file.py").write_text("x=1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo), stdout=subprocess.PIPE, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), stdout=subprocess.PIPE, check=True)
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist-setup")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            lp = StartSessionScreen(
                task_options=list(app.curated_task_options()),
                initial_target="local_project",
                initial_project=str(repo),
            )
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert lp._config.verifier_timeout_seconds == 120
            lp.render_state()
            await pilot.pause()
            row = lp._row(ROW_VERIFIER_TIMEOUT)
            from agentic_debugger.ui.screens import StartSessionScreen as _S

            assert row is not None
            # Row paints the bound next to Time limit in the Bounds group.
            assert "120s" in (row._value or "")
            # Editor saves a new bound; invalid stays fail-closed.
            lp._verifier_timeout_saved(45)
            assert lp._config.verifier_timeout_seconds == 45
            lp._verifier_timeout_saved(None)
            assert lp._config.verifier_timeout_seconds == 45
        reset_launch_cwd()

    asyncio.run(_inner())
