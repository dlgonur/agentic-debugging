"""Model-revisable test commands (REVISE_TEST_COMMAND).

The agent may narrow a hanging or suite-wide repro/verify command
mid-session without any user configuration: validation owns safety
(exact shapes, tracked files, bounds, credential-shape), the handler
bounds revisions (2 per command) and resets the invalidated evidence,
readers and the verifier bind the revised commands, and the journal
carries the revision lineage.
"""

from __future__ import annotations

from pathlib import Path


def _context(isolated: Path, tracked=("calc.py", "tests/test_calc.py")):
    from agentic_debugger.agent.controller_policy import PdbPolicy
    from agentic_debugger.application.events import SourceKind
    from agentic_debugger.application.local_project_helpers import LocalProjectTask
    from agentic_debugger.application.local_project_tools import (
        _LocalToolContext,
        _build_local_registry,
    )
    from agentic_debugger.application.observability import (
        ObservabilityContext,
        SessionObservability,
    )
    from agentic_debugger.evaluation.task_schema import Constraints

    task = LocalProjectTask(
        task_id="local-project-debug",
        title="t",
        description="b",
        language="python",
        fixture_path="isolated",
        constraints=Constraints(
            allowed_write_paths=list(tracked),
            denied_write_paths=["task.json"],
            network_allowed=False,
            external_services_allowed=False,
            max_patch_attempts=2,
            max_test_runs=10,
            max_pdb_observations=5,
        ),
        tracked_files=tuple(tracked),
        bug_description="b",
        reproduction_command="python -m pytest -q",
        verification_command="python -m pytest -q",
    )
    ctx = _LocalToolContext(
        isolated=isolated,
        tracked=list(tracked),
        task=task,
        probe=None,
        observability=SessionObservability(
            ObservabilityContext(
                session_id="sess-revise-01",
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
    registry = _build_local_registry(ctx, pdb_policy=PdbPolicy.DISABLED)
    return ctx, registry


def _dispatch(registry, name: str, state: str, arguments: dict, seq: int):
    from agentic_debugger.agent.state_machine import ControllerState
    from agentic_debugger.events.schema import Action

    action = Action(
        action_id=f"action-{seq:09d}",
        run_id="run-1",
        task_id="local-project-debug",
        state=ControllerState[state],
        name=name,
        arguments=arguments,
    )
    return registry.dispatch(action, observation_id=f"observation-{seq:09d}")


def _workspace(tmp_path: Path) -> Path:
    isolated = tmp_path / "iso"
    (isolated / "tests").mkdir(parents=True)
    (isolated / "calc.py").write_text("def add(a,b):\n    return a - b\n", encoding="utf-8")
    (isolated / "tests" / "test_calc.py").write_text(
        "from calc import add\ndef test_add():\n    assert add(1,2)==3\n",
        encoding="utf-8",
    )
    return isolated


def test_policy_allows_revise_in_reproduce_and_validate_only() -> None:
    from agentic_debugger.agent.controller_policy import ActionName, is_action_allowed
    from agentic_debugger.agent.state_machine import ControllerState

    assert is_action_allowed(ControllerState.REPRODUCE, ActionName.REVISE_TEST_COMMAND)
    assert is_action_allowed(ControllerState.VALIDATE, ActionName.REVISE_TEST_COMMAND)
    for state in (
        ControllerState.UNDERSTAND,
        ControllerState.RUNTIME_EVIDENCE,
        ControllerState.PATCH,
        ControllerState.DONE,
        ControllerState.FAILED,
    ):
        assert not is_action_allowed(state, ActionName.REVISE_TEST_COMMAND)


def test_validator_accepts_exact_shapes(tmp_path: Path) -> None:
    from agentic_debugger.application.local_project_helpers import (
        validate_revised_test_command,
    )

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("x=1\n", encoding="utf-8")
    tracked = ("calc.py", "tests/test_calc.py", "repro.py")
    assert (
        validate_revised_test_command(
            "python -m pytest tests/test_calc.py -q",
            current="python -m pytest -q",
            tracked_files=tracked,
            workspace_root=tmp_path,
        )
        == "python -m pytest tests/test_calc.py -q"
    )
    assert (
        validate_revised_test_command(
            "python -m pytest -q",
            current="python -m pytest tests/test_calc.py -q",
            tracked_files=tracked,
            workspace_root=tmp_path,
        )
        == "python -m pytest -q"
    )


def test_validator_rejects_unsafe_shapes() -> None:
    import pytest

    from agentic_debugger.application.local_project_helpers import (
        validate_revised_test_command,
    )

    tracked = ("calc.py", "tests/test_calc.py")
    base = {
        "current": "python -m pytest -q",
        "tracked_files": tracked,
        "workspace_root": ".",
    }
    with pytest.raises(ValueError, match="identical"):
        validate_revised_test_command("python -m pytest -q", **base)
    with pytest.raises(ValueError, match="single line"):
        validate_revised_test_command("python -m pytest -q\nrm -rf /", **base)
    with pytest.raises(ValueError, match="2 KiB"):
        validate_revised_test_command("python -m pytest -q" + " # " + "x" * 2048, **base)
    with pytest.raises(ValueError, match="credential"):
        validate_revised_test_command("python repro.py --password=hunter2", **base)
    with pytest.raises(ValueError, match="unsupported command shape"):
        validate_revised_test_command("pytest tests/test_calc.py", **base)
    with pytest.raises(ValueError, match="unsupported command shape"):
        validate_revised_test_command("python -m pytest tests/test_calc.py -q -x", **base)
    with pytest.raises(ValueError, match="not a tracked file"):
        validate_revised_test_command("python -m pytest tests/test_evil.py -q", **base)
    with pytest.raises(ValueError, match="escapes|tracked"):
        validate_revised_test_command("python -m pytest ../evil.py -q", **base)
    with pytest.raises(ValueError, match="non-empty"):
        validate_revised_test_command("   ", **base)


def test_revise_success_sets_context_and_resets_evidence(tmp_path: Path) -> None:
    from agentic_debugger.agent.controller_policy import ActionName
    from agentic_debugger.events.schema import ObservationStatus

    isolated = _workspace(tmp_path)
    ctx, registry = _context(isolated)
    ctx.baseline_failure_reproduced = True
    ctx.post_patch_f2p_passed = True

    obs = _dispatch(
        registry,
        ActionName.REVISE_TEST_COMMAND.value,
        "REPRODUCE",
        {"which": "repro", "command": "python -m pytest tests/test_calc.py -q"},
        1,
    )
    assert obs.status is ObservationStatus.OK
    assert obs.payload["which"] == "repro"
    assert obs.payload["previous"] == "python -m pytest -q"
    assert obs.payload["command"] == "python -m pytest tests/test_calc.py -q"
    assert obs.payload["revision"] == 1
    assert ctx.revised_reproduction_command == "python -m pytest tests/test_calc.py -q"
    assert ctx.baseline_failure_reproduced is None
    assert ctx.post_patch_f2p_passed is None
    # Revision lineage is journal truth (existing event kind).
    texts = [e.payload.get("text", "") for e in ctx.observability.events()]
    assert any(
        "repro command revised" in t
        and "python -m pytest tests/test_calc.py -q" in t
        for t in texts
    ), texts


def test_revision_budget_is_two_per_command(tmp_path: Path) -> None:
    from agentic_debugger.agent.controller_policy import ActionName
    from agentic_debugger.agent.tool_registry import ToolRejectedError
    from agentic_debugger.events.schema import ObservationStatus

    isolated = _workspace(tmp_path)
    ctx, registry = _context(isolated)
    first = "python -m pytest tests/test_calc.py -q"
    _dispatch(
        registry, ActionName.REVISE_TEST_COMMAND.value, "REPRODUCE",
        {"which": "repro", "command": first}, 1,
    )
    # Second revision must differ from the first.
    second = _dispatch(
        registry, ActionName.REVISE_TEST_COMMAND.value, "REPRODUCE",
        {"which": "repro", "command": "python -m pytest -q"}, 2,
    )
    assert second.status is ObservationStatus.OK
    assert second.payload["revision"] == 2
    # Third revision hits the per-command budget: rejected at dispatch but
    # recoverable, so the session proceeds with the current command.
    third = _dispatch(
        registry, ActionName.REVISE_TEST_COMMAND.value, "REPRODUCE",
        {"which": "repro", "command": first}, 3,
    )
    assert third.status is ObservationStatus.REJECTED
    assert third.payload.get("recoverable") is True
    assert third.payload.get("revision_budget_spent") is True
    assert ctx.repro_revisions == 2


def test_wrong_state_dispatch_is_rejected(tmp_path: Path) -> None:
    from agentic_debugger.agent.controller_policy import ActionName
    from agentic_debugger.events.schema import ObservationStatus

    isolated = _workspace(tmp_path)
    _, registry = _context(isolated)
    obs = _dispatch(
        registry, ActionName.REVISE_TEST_COMMAND.value, "UNDERSTAND",
        {"which": "repro", "command": "python -m pytest tests/test_calc.py -q"}, 1,
    )
    assert obs.status is ObservationStatus.REJECTED


def test_run_reproduction_executes_revised_command(tmp_path: Path) -> None:
    from agentic_debugger.agent.controller_policy import ActionName
    from agentic_debugger.events.schema import ObservationStatus

    isolated = _workspace(tmp_path)
    ctx, registry = _context(isolated)
    seen: list[str] = []
    orig = ctx.run_project_command

    def _capture(cmd, cwd, timeout=30.0, cancel_check=None):
        seen.append(cmd)
        return orig(cmd, cwd, timeout, cancel_check=cancel_check)

    ctx.run_project_command = _capture  # type: ignore[method-assign]
    try:
        _dispatch(
            registry, ActionName.REVISE_TEST_COMMAND.value, "REPRODUCE",
            {"which": "repro", "command": "python -m pytest tests/test_calc.py -q"}, 1,
        )
        obs = _dispatch(
            registry, ActionName.RUN_REPRODUCTION.value, "REPRODUCE",
            {"phase": "baseline"}, 2,
        )
    finally:
        ctx.run_project_command = orig
    assert obs.status is ObservationStatus.OK
    assert seen == ["python -m pytest tests/test_calc.py -q"]
    assert obs.payload["failure_reproduced"] is True


def test_registry_contract_exposes_revise_action(tmp_path: Path) -> None:
    from agentic_debugger.agent.controller_policy import ActionName

    isolated = _workspace(tmp_path)
    _, registry = _context(isolated)
    contracts = registry.argument_contracts()
    assert ActionName.REVISE_TEST_COMMAND.value in registry.names()
    props = contracts[ActionName.REVISE_TEST_COMMAND.value]["properties"]
    assert props["which"]["enum"] == ["repro", "verify"]
    assert props["command"]["type"] == "string"
