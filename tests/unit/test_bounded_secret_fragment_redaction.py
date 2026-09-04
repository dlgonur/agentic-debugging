"""V2-02 repair 11 — application-owned pre-redaction bounding fragment seal.

FirstMate regressions: repair 10 redacts COMPLETE raw secret values at the
product boundaries, but several lower layers bound/truncate/preview project
output BEFORE the one session redaction authority sees it.  When such a cut
goes through the middle of a secret, the complete value no longer exists in
the text and exact replacement cannot match — Agentic Debugger itself would
manufacture the leaking fragment.  These tests prove repair 11 seals every
application-owned cut:

- A/B/C(+spanning): ProductExecutor command output across CommandRunner
  head/tail truncation boundaries (the direct FirstMate reproduction, tail
  boundary, stdout and stderr, and a long secret spanning a boundary
  substantially);
- D: product LocalProjectVerifier command evidence across the same
  truncation class;
- E: verifier Git diagnostic ordering (redact FIRST, then the 1000-char
  public bound);
- F/G/H: product PDB long-local frame-locals preview, safe-eval preview,
  and a nested container summary (the worker's own 2048-byte preview);
- I: product PDB exception diagnostics (redact BEFORE the 400-char
  diagnostic bound, including the worker's marked bounded-tail fragments);
- J: non-truncated exact redaction from repair 10 stays green;
- K/L: ordinary non-secret and no-secret sessions stay byte/behavior
  compatible, and the neutral CommandRunner seam preserves generic
  behavior.

Distinct from the documented non-goal: a project deliberately transforming
its own secret (``secret[:5]``, ``base64(secret)`` ...) is still out of
scope; here Agentic Debugger's OWN bounding creates the fragment.

No provider network, no real credentials, no frozen experiment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_debugger.agent.controller_policy import ActionName
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application.events import SourceKind
from agentic_debugger.application.execution_environment import (
    PDB_BOUNDED_TEXT_TRUNCATION_MARKER,
    ExecutionRole,
    ProjectSecretRedactor,
)
from agentic_debugger.application.executor import ProductExecutor
from agentic_debugger.application.local_project_source import (
    LocalProjectTask,
    _LocalToolContext,
    _build_local_registry,
)
from agentic_debugger.application.observability import (
    ObservabilityContext,
    SessionObservability,
)
from agentic_debugger.application.session_runtime import (
    ProjectEnvDeclaration,
    ProjectRuntimeEnvironmentSpec,
    build_local_project_launch,
)
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.local_project_verifier import (
    LocalProjectEvaluationPlan,
    LocalProjectVerifier,
    _run_git,
)
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome
from agentic_debugger.evaluation.runner import (
    EvaluationInputError,
    EvaluationStatus,
)
from agentic_debugger.events.schema import Action
from agentic_debugger.runtime.command_runner import CommandRunner
from agentic_debugger.runtime.exceptions import (
    CommandRequestError,
    PdbSessionError,
)
from agentic_debugger.runtime.workspace import TaskWorkspace

# The exact FirstMate reproduction secret and paddings.
SECRET_NAME = "V2_02_PROJECT_SECRET"
SECRET_VALUE = "UNIQUE_SECRET_ABCDEFGHIJ"
SECRET_MARKER = f"<PROJECT_SECRET:{SECRET_NAME}>"
SECOND_NAME = "V2_02_PROJECT_SECOND_SECRET"
SECOND_VALUE = "SECOND_UNIQUE_SECRET_XYZ"
SECOND_MARKER = f"<PROJECT_SECRET:{SECOND_NAME}>"
HEAD_BOUNDARY_FRAGMENT = "A" * 9995 + "UNIQU"
TAIL_BOUNDARY_FRAGMENT = SECRET_VALUE[-5:] + "BBB"

# A declared secret long enough to span a CommandRunner truncation
# boundary substantially (the raw prefix survives the downstream
# evidence bound on the repair-10 ordering).
SPAN_SECRET_NAME = "V2_02_PROJECT_SPAN_SECRET"
SPAN_SECRET = "SP" + "m" * 6998
SPAN_SECRET_MARKER = f"<PROJECT_SECRET:{SPAN_SECRET_NAME}>"

# A declared secret longer than the PDB worker's string-preview bound.
LONG_SECRET_NAME = "V2_02_PROJECT_LONG_SECRET"
LONG_SECRET = "LS" + "x" * 2998
LONG_SECRET_MARKER = f"<PROJECT_SECRET:{LONG_SECRET_NAME}>"
LONG_SECRET_PREFIX = LONG_SECRET[:2048]

GOOD_PATCH = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(a,b):
-    return a - b
+    return a + b
"""


def _spec(**overrides) -> ProjectRuntimeEnvironmentSpec:
    fields = {
        "secrets": (ProjectEnvDeclaration(SECRET_NAME),),
    }
    fields.update(overrides)
    return ProjectRuntimeEnvironmentSpec(**fields)


def _launch(monkeypatch, **overrides):
    fields = {
        "session_id": "sess-v202-bounded",
        "task_id": "local-project-debug",
        "policy": "pdb-on-uncertainty",
        "provider_id": None,
        "model_id": None,
        "profile_id": "dummy-profile",
        "launch_snapshot": dict(os.environ),
        "project_spec": _spec(),
    }
    fields.update(overrides)
    return build_local_project_launch(**fields)


def _executor(launch) -> ProductExecutor:
    return ProductExecutor(
        execution_environment=launch.execution_environment,
        capabilities=launch.capabilities,
    )


def _child_code(python: str) -> list:
    return [sys.executable, "-c", python]


def _raw_runner(launch, workspace):
    return CommandRunner(
        workspace,
        environment=dict(
            launch.execution_environment.role_environment(
                ExecutionRole.PROJECT_COMMAND
            )
        ),
    )


# ---------------------------------------------------------------------------
# A/B/C. ProductExecutor command output across truncation boundaries
# ---------------------------------------------------------------------------


def _first_mate_head_argv() -> list:
    return _child_code(
        "print('A' * 9995 + '{s}' + 'B' * 20000, end='')".format(
            s=SECRET_VALUE
        )
    )


def test_product_executor_head_boundary_split_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    launch = _launch(monkeypatch, session_id="sess-bounded-head")
    executor = _executor(launch)
    workspace = TaskWorkspace(str(tmp_path))
    # The child really receives and emits the complete raw secret, and the
    # runner's own head boundary really cuts through it (FirstMate repro).
    raw = _raw_runner(launch, workspace).run(
        _first_mate_head_argv(), ".", 30.0
    )
    assert raw.exit_code == 0
    assert raw.stdout_truncated is True
    assert HEAD_BOUNDARY_FRAGMENT in raw.stdout
    assert SECRET_VALUE not in raw.stdout
    # The executor boundary must not expose the system-created fragment.
    result = executor.run_project_command(
        _first_mate_head_argv(), workspace, 30.0
    )
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.stdout_truncated is True
    assert SECRET_VALUE not in result.stdout
    assert SECRET_VALUE not in result.stderr
    assert HEAD_BOUNDARY_FRAGMENT not in result.stdout
    assert "UNIQU" not in result.stdout
    # The cut fragment was replaced by the redaction marker BEFORE the
    # boundary was applied (the marker prefix sits exactly where the raw
    # secret prefix used to be; the full marker straddles the bound).
    assert result.stdout.startswith("A" * 9995 + SECRET_MARKER[:5])
    # The retained tail region is unchanged, and execution facts match.
    assert "B" * 200 in result.stdout
    assert result.argv == raw.argv
    assert result.cwd == raw.cwd
    assert result.exit_code == raw.exit_code


def test_product_executor_tail_boundary_split_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    launch = _launch(monkeypatch, session_id="sess-bounded-tail")
    executor = _executor(launch)
    workspace = TaskWorkspace(str(tmp_path))
    argv = _child_code(
        "print('A' * 20000 + '{s}' + 'B' * 9995, end='')".format(
            s=SECRET_VALUE
        )
    )
    raw = _raw_runner(launch, workspace).run(argv, ".", 30.0)
    assert raw.stdout_truncated is True
    # The omitted-region -> tail boundary cut leaves a raw secret suffix.
    assert TAIL_BOUNDARY_FRAGMENT in raw.stdout
    assert SECRET_VALUE not in raw.stdout
    result = executor.run_project_command(argv, workspace, 30.0)
    assert result.exit_code == 0
    assert result.stdout_truncated is True
    assert SECRET_VALUE not in result.stdout
    assert SECRET_VALUE[-5:] not in result.stdout
    assert TAIL_BOUNDARY_FRAGMENT not in result.stdout
    assert "A" * 200 in result.stdout
    assert "B" * 200 in result.stdout


def test_product_executor_stdout_and_stderr_bounded_split_redacted(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    monkeypatch.setenv(SECOND_NAME, SECOND_VALUE)
    launch = _launch(
        monkeypatch,
        session_id="sess-bounded-streams",
        project_spec=_spec(
            secrets=(
                ProjectEnvDeclaration(SECRET_NAME),
                ProjectEnvDeclaration(SECOND_NAME),
            )
        ),
    )
    executor = _executor(launch)
    workspace = TaskWorkspace(str(tmp_path))
    argv = _child_code(
        "import sys\n"
        "sys.stdout.write('A' * 9995 + '{s1}' + 'B' * 12000)\n"
        "sys.stderr.write('C' * 20000 + '{s2}' + 'D' * 5000)\n".format(
            s1=SECRET_VALUE, s2=SECOND_VALUE
        )
    )
    result = executor.run_project_command(argv, workspace, 30.0)
    assert result.exit_code == 0
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert HEAD_BOUNDARY_FRAGMENT not in result.stdout
    assert "UNIQU" not in result.stdout
    assert "C" * 20000 + "SECOND_UNIQ" not in result.stderr
    assert SECOND_VALUE not in result.stdout
    assert SECOND_VALUE not in result.stderr
    assert SECRET_VALUE not in result.stdout
    assert SECRET_VALUE not in result.stderr
    # The stderr marker lands fully inside the retained tail region.
    assert SECOND_MARKER in result.stderr


def test_product_executor_long_secret_spanning_boundary_redacted(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(SPAN_SECRET_NAME, SPAN_SECRET)
    launch = _launch(
        monkeypatch,
        session_id="sess-bounded-span",
        project_spec=_spec(
            secrets=(ProjectEnvDeclaration(SPAN_SECRET_NAME),)
        ),
    )
    executor = _executor(launch)
    workspace = TaskWorkspace(str(tmp_path))
    argv = _child_code(
        "print('A' * 5000 + '{s}' + 'B' * 20000, end='')".format(
            s=SPAN_SECRET
        )
    )
    raw = _raw_runner(launch, workspace).run(argv, ".", 30.0)
    assert raw.stdout_truncated is True
    # The cut leaves a multi-thousand-char raw secret prefix in the head.
    assert SPAN_SECRET[:5000] in raw.stdout
    result = executor.run_project_command(argv, workspace, 30.0)
    assert result.stdout_truncated is True
    assert SPAN_SECRET not in result.stdout
    assert SPAN_SECRET[:100] not in result.stdout
    assert SPAN_SECRET[:10] not in result.stdout
    # The full marker survives inside the retained head region.
    assert SPAN_SECRET_MARKER in result.stdout
    assert "A" * 200 in result.stdout
    assert "B" * 200 in result.stdout


# ---------------------------------------------------------------------------
# D. Product LocalProjectVerifier command evidence across truncation
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_verifier_command_truncation_split_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv(SPAN_SECRET_NAME, SPAN_SECRET)
    launch = _launch(
        monkeypatch,
        session_id="sess-bounded-verifier",
        project_spec=_spec(
            secrets=(ProjectEnvDeclaration(SPAN_SECRET_NAME),)
        ),
    )
    repo = tmp_path / "bounded_verifier_repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "calculator.py").write_text(
        "def add(a,b):\n    return a - b\n", encoding="utf-8"
    )
    (repo / "reproduce.py").write_text(
        "import os\n"
        "print('A' * 5000 + os.environ['{n}'] + 'B' * 20000, end='')\n"
        "from calculator import add\n"
        "raise SystemExit(0 if add(2, 3) == 5 else 1)\n".format(
            n=SPAN_SECRET_NAME
        ),
        encoding="utf-8",
    )
    (repo / "regression.py").write_text(
        "from calculator import add\n"
        "raise SystemExit(0 if add(0, 0) == 0 else 1)\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout.strip()
    plan = LocalProjectEvaluationPlan(
        source_repo_path=str(repo),
        source_head_commit=head,
        candidate_patch=GOOD_PATCH,
        reproduction_argv=(sys.executable, "reproduce.py"),
        regression_argv=(sys.executable, "regression.py"),
        allowed_paths=("calculator.py",),
        denied_paths=("tests",),
        timeout_seconds=30.0,
        workspace_parent=str(tmp_path),
    )
    verifier = LocalProjectVerifier(
        product_environment=dict(
            launch.execution_environment.role_environment(ExecutionRole.VERIFIER)
        ),
        product_secret_redactor=(
            launch.execution_environment.project_secret_redactor()
        ),
    )
    result = verifier.evaluate(plan)
    assert result.status is EvaluationStatus.COMPLETED
    # Classification/exit-code behavior is unchanged by the seam.
    assert result.outcome is SemanticOutcome.RESOLVED
    baseline = result.baseline_reproduction
    assert baseline is not None
    assert baseline.status.value == "FAIL"
    assert baseline.stdout_truncated is True
    assert SPAN_SECRET not in baseline.stdout
    assert SPAN_SECRET[:100] not in baseline.stdout
    assert SPAN_SECRET[:10] not in baseline.stdout
    assert SPAN_SECRET_MARKER in baseline.stdout
    assert SPAN_SECRET not in json.dumps(result.to_mapping(), default=str)
    assert SPAN_SECRET[:10] not in json.dumps(result.to_mapping(), default=str)
    assert result.workspace.cleaned


# ---------------------------------------------------------------------------
# E. Verifier Git diagnostic ordering (redact first, then the 1000 bound)
# ---------------------------------------------------------------------------


def test_verifier_git_diagnostic_redacts_before_bound(tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    launch = _launch(monkeypatch, session_id="sess-bounded-git")
    redactor = launch.execution_environment.project_secret_redactor()
    repo = tmp_path / "git_order_repo"
    repo.mkdir()
    # 990 padding chars place the old bound-then-redact cut ([:1000]) in
    # the middle of the complete secret: the old ordering leaks the
    # 10-character raw prefix SECRET[:10].
    payload = "X" * 990 + SECRET_VALUE + "Y" * 1000

    def _failing_git(*args, **kwargs):
        from subprocess import CompletedProcess

        return CompletedProcess(
            args, returncode=1, stdout=b"", stderr=payload.encode("utf-8")
        )

    from agentic_debugger.evaluation import local_project_verifier as module

    monkeypatch.setattr(module.subprocess, "run", _failing_git)
    with pytest.raises(EvaluationInputError) as redacted_exc:
        _run_git(
            str(repo),
            ["status", "--porcelain"],
            environment={"PATH": os.environ.get("PATH", "")},
            redactor=redactor,
        )
    message = str(redacted_exc.value)
    assert SECRET_VALUE not in message
    assert SECRET_VALUE[:10] not in message
    # The redaction happened before the 1000-char public bound: the
    # inserted marker prefix sits where the raw secret prefix used to be
    # (the full marker straddles the 1000-char cut).
    assert "<PROJECT_S" in message
    detail = message.split("failed: ", 1)[1]
    assert len(detail) <= 1000
    # The legacy no-redactor path keeps its historical (unredacted,
    # bounded) behavior — which is exactly the fragment the repair removes.
    with pytest.raises(EvaluationInputError) as legacy_exc:
        _run_git(
            str(repo),
            ["status", "--porcelain"],
            environment={"PATH": os.environ.get("PATH", "")},
            redactor=None,
        )
    assert SECRET_VALUE[:10] in str(legacy_exc.value)


# ---------------------------------------------------------------------------
# F/G/H. Product PDB long-string previews (worker 2048-byte bound)
# ---------------------------------------------------------------------------


def _pdb_context(tmp_path, monkeypatch, launch, extra_local_line=None):
    executor = _executor(launch)
    ws_root = tmp_path / "pdbws"
    ws_root.mkdir()
    lines = [
        "import os",
        "",
        "def main():",
        "    LEAK = os.environ['%s']" % LONG_SECRET_NAME,
    ]
    if extra_local_line is not None:
        lines.append("    " + extra_local_line)
    lines += [
        "    print(len(LEAK))",
        "",
        "main()",
    ]
    (ws_root / "repro_long.py").write_text("\n".join(lines), encoding="utf-8")
    observability = SessionObservability(
        ObservabilityContext(
            session_id=launch.session_id,
            task_id=launch.task_id,
            source_kind=SourceKind.LOCAL_PROJECT,
        )
    )
    context = _LocalToolContext(
        isolated=ws_root,
        tracked=[],
        task=LocalProjectTask(),
        probe=None,
        observability=observability,
        command_environment=dict(
            launch.execution_environment.role_environment(
                ExecutionRole.PROJECT_COMMAND
            )
        ),
        pdb_worker_environment=dict(
            launch.execution_environment.role_environment(
                ExecutionRole.PRODUCT_PDB
            )
        ),
        executor=executor,
        capabilities=launch.capabilities,
    )
    registry = _build_local_registry(
        context, pdb_policy=DemoPolicy("pdb-on-uncertainty")
    )
    workspace = TaskWorkspace(str(ws_root))
    context.pdb_workspace = workspace
    session = context.open_product_pdb(workspace)
    context.pdb_session = session
    session.start()
    breakpoint_line = lines.index("    print(len(LEAK))") + 1
    started = session.start_paused_target("repro_long.py", [breakpoint_line])
    assert started.get("state") == "paused"
    stack = session.get_stack_summary()
    generation = stack["pause_generation"]
    return context, registry, generation


def _launch_long_secret(monkeypatch, session_id):
    monkeypatch.setenv(LONG_SECRET_NAME, LONG_SECRET)
    return _launch(
        monkeypatch,
        session_id=session_id,
        project_spec=_spec(secrets=(ProjectEnvDeclaration(LONG_SECRET_NAME),)),
    )


def _dispatch(registry, name, arguments):
    action = Action(
        action_id=f"action-{name.value}",
        run_id="run-1",
        task_id="local-project-debug",
        state=ControllerState.RUNTIME_EVIDENCE,
        name=name.value,
        arguments=arguments,
    )
    return registry.dispatch(action, observation_id=f"observation-{name.value}")


def _leak_entry(locals_payload):
    entries = [
        item
        for item in locals_payload["locals"]
        if item["name"] == "LEAK"
    ]
    assert entries
    return entries[0]["value"]


def test_pdb_long_local_frame_locals_preview_redacted(tmp_path, monkeypatch):
    launch = _launch_long_secret(monkeypatch, "sess-bounded-locals")
    context, registry, generation = _pdb_context(
        tmp_path, monkeypatch, launch
    )
    session = context.pdb_session
    try:
        # The PDB worker's OWN bounded representation is unchanged: a
        # truncated 2048-char prefix of the complete declared secret.
        raw_locals = session.get_frame_locals(0, generation)
        raw_summary = _leak_entry(raw_locals)
        assert raw_summary["kind"] == "str"
        assert raw_summary["value"] == LONG_SECRET_PREFIX
        assert raw_summary["truncated"] is True
        assert raw_summary["size"] == len(LONG_SECRET)
        # The product tool payload carries no worker-created raw prefix,
        # while the truthful structural metadata is preserved.
        observation = _dispatch(
            registry,
            ActionName.GET_FRAME_LOCALS,
            {"frame_id": 0, "pause_generation": generation},
        )
        assert observation.status.value == "ok"
        payload_text = json.dumps(observation.payload)
        assert LONG_SECRET not in payload_text
        assert LONG_SECRET_PREFIX not in payload_text
        assert LONG_SECRET[:64] not in payload_text
        assert LONG_SECRET_MARKER in payload_text
        summary = _leak_entry(observation.payload)
        assert summary["value"] == LONG_SECRET_MARKER
        assert summary["kind"] == "str"
        assert summary["truncated"] is True
        assert summary["size"] == len(LONG_SECRET)
        # The observability event saw the same sanitized object.
        locals_events = [
            event
            for event in context.observability.events()
            if event.event_kind.value == "debugger.locals_observed"
        ]
        assert locals_events
        event_text = json.dumps(
            locals_events[-1].to_mapping(), default=str
        )
        assert LONG_SECRET_PREFIX not in event_text
        assert LONG_SECRET_MARKER in event_text
    finally:
        context.release_pdb()


def test_pdb_long_local_safe_eval_preview_redacted(tmp_path, monkeypatch):
    launch = _launch_long_secret(monkeypatch, "sess-bounded-eval")
    context, registry, generation = _pdb_context(
        tmp_path, monkeypatch, launch
    )
    session = context.pdb_session
    try:
        raw_eval = session.safe_eval_expression(0, generation, "LEAK")
        assert raw_eval["value"]["value"] == LONG_SECRET_PREFIX
        assert raw_eval["value"]["truncated"] is True
        observation = _dispatch(
            registry,
            ActionName.SAFE_EVAL_EXPRESSION,
            {
                "frame_id": 0,
                "pause_generation": generation,
                "expression": "LEAK",
            },
        )
        assert observation.status.value == "ok"
        payload_text = json.dumps(observation.payload)
        assert LONG_SECRET_PREFIX not in payload_text
        assert LONG_SECRET_MARKER in payload_text
        assert observation.payload["value"]["truncated"] is True
        assert observation.payload["value"]["size"] == len(LONG_SECRET)
    finally:
        context.release_pdb()


def test_pdb_nested_truncated_summary_redacted(tmp_path, monkeypatch):
    launch = _launch_long_secret(monkeypatch, "sess-bounded-nested")
    context, registry, generation = _pdb_context(
        tmp_path, monkeypatch, launch, extra_local_line="PAIR = ('lit', LEAK)"
    )
    session = context.pdb_session
    try:
        # The safe-eval grammar names the container local; the worker
        # recursively summarizes it, nesting a truncated string summary.
        raw_eval = session.safe_eval_expression(0, generation, "PAIR")
        nested = raw_eval["value"]["items"][1]
        assert nested["kind"] == "str"
        assert nested["value"] == LONG_SECRET_PREFIX
        assert nested["truncated"] is True
        observation = _dispatch(
            registry,
            ActionName.SAFE_EVAL_EXPRESSION,
            {
                "frame_id": 0,
                "pause_generation": generation,
                "expression": "PAIR",
            },
        )
        assert observation.status.value == "ok"
        payload_text = json.dumps(observation.payload)
        assert LONG_SECRET_PREFIX not in payload_text
        assert LONG_SECRET_MARKER in payload_text
        nested_out = observation.payload["value"]["items"][1]
        assert nested_out["value"] == LONG_SECRET_MARKER
        assert nested_out["truncated"] is True
        assert nested_out["size"] == len(LONG_SECRET)
        assert observation.payload["value"]["kind"] == "tuple"
    finally:
        context.release_pdb()


# ---------------------------------------------------------------------------
# I. Product PDB exception diagnostics (redact before the 400-char bound)
# ---------------------------------------------------------------------------


class _FailingSession:
    """Product-boundary stub: PdbSession raising a worker-style diagnostic."""

    def __init__(self, message: str) -> None:
        self._message = message

    def get_stack_summary(self):  # type: ignore[no-untyped-def]
        raise PdbSessionError(self._message)


def _diagnostic_context(tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    launch = _launch(monkeypatch, session_id="sess-bounded-exc")
    executor = _executor(launch)
    observability = SessionObservability(
        ObservabilityContext(
            session_id=launch.session_id,
            task_id=launch.task_id,
            source_kind=SourceKind.LOCAL_PROJECT,
        )
    )
    context = _LocalToolContext(
        isolated=tmp_path,
        tracked=[],
        task=LocalProjectTask(),
        probe=None,
        observability=observability,
        command_environment=dict(
            launch.execution_environment.role_environment(
                ExecutionRole.PROJECT_COMMAND
            )
        ),
        pdb_worker_environment=dict(
            launch.execution_environment.role_environment(
                ExecutionRole.PRODUCT_PDB
            )
        ),
        executor=executor,
        capabilities=launch.capabilities,
    )
    registry = _build_local_registry(
        context, pdb_policy=DemoPolicy("pdb-on-uncertainty")
    )
    return context, registry


def test_pdb_exception_diagnostic_complete_secret_redacted_before_bound(
    tmp_path, monkeypatch
):
    context, registry = _diagnostic_context(tmp_path, monkeypatch)
    # A diagnostic carrying the COMPLETE secret straddling the 400-char
    # diagnostic cut: bounding first (the repair-10 ordering) slices the
    # secret and exact redaction can no longer match the fragment.
    # "PdbSessionError: get_stack_summary failed: RuntimeError: " is 57
    # chars; 328 padding chars start the secret at 385, so the 397-char
    # cut keeps a 12-char raw prefix under the old ordering, while the
    # repair leaves the first 12 chars of the redaction marker there.
    context.pdb_session = _FailingSession(
        "get_stack_summary failed: RuntimeError: "
        + "q" * 328
        + SECRET_VALUE
        + " end"
    )
    observation = _dispatch(registry, ActionName.GET_STACK_SUMMARY, {})
    assert observation.status.value == "error"
    payload_text = json.dumps(observation.payload, default=str)
    assert SECRET_VALUE not in payload_text
    assert SECRET_VALUE[:12] not in payload_text
    assert "<PROJECT_SEC" in payload_text
    # record_error (tool_errors) crosses the same boundary; the
    # re-formatted ToolExecutionError prefix shifts the geometry, but no
    # secret material may surface there either way.
    assert context.tool_errors
    diagnostic = context.tool_errors[-1]["diagnostic"]
    assert SECRET_VALUE not in diagnostic
    assert SECRET_VALUE[:12] not in diagnostic
    assert len(diagnostic) <= 400


def test_pdb_exception_marked_truncated_tail_redacted(tmp_path, monkeypatch):
    context, registry = _diagnostic_context(tmp_path, monkeypatch)
    # A worker-style bounded diagnostic: the application cut a long secret
    # and marked the cut with its truncation marker.  The product egress
    # must remove the application-created raw fragment.
    context.pdb_session = _FailingSession(
        "get_stack_summary failed: RuntimeError: "
        + "q" * 100
        + SECRET_VALUE[:80]
        + PDB_BOUNDED_TEXT_TRUNCATION_MARKER
    )
    observation = _dispatch(registry, ActionName.GET_STACK_SUMMARY, {})
    assert observation.status.value == "error"
    payload_text = json.dumps(observation.payload, default=str)
    assert SECRET_VALUE[:80] not in payload_text
    assert SECRET_MARKER[:10] in payload_text
    diagnostic = context.tool_errors[-1]["diagnostic"]
    assert SECRET_VALUE[:80] not in diagnostic
    assert SECRET_MARKER[:10] in diagnostic


# ---------------------------------------------------------------------------
# J/K/L. Repair-10 behavior, neutrality, and no-secret compatibility
# ---------------------------------------------------------------------------


def test_non_truncated_secret_redaction_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    launch = _launch(monkeypatch, session_id="sess-bounded-exact")
    executor = _executor(launch)
    result = executor.run_project_command(
        _child_code(f"import os; print(os.environ['{SECRET_NAME}'])"),
        TaskWorkspace(str(tmp_path)),
        30.0,
    )
    assert result.exit_code == 0
    assert result.stdout_truncated is False
    assert SECRET_VALUE not in result.stdout
    assert SECRET_MARKER in result.stdout


def test_ordinary_truncated_output_stays_byte_compatible(tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    launch = _launch(monkeypatch, session_id="sess-bounded-plain")
    executor = _executor(launch)
    workspace = TaskWorkspace(str(tmp_path))
    argv = _child_code("print('E' * 30018, end='')")
    raw = _raw_runner(launch, workspace).run(argv, ".", 30.0)
    result = executor.run_project_command(argv, workspace, 30.0)
    # Secret-free output passes the sanitizer through byte for byte; the
    # head/tail bounding and flags are identical with and without it.
    assert result.stdout == raw.stdout
    assert result.stderr == raw.stderr
    assert result.stdout_truncated is True
    assert raw.stdout_truncated is True
    assert result.exit_code == raw.exit_code


def test_no_secret_session_stays_byte_compatible(tmp_path, monkeypatch):
    launch = _launch(
        monkeypatch,
        session_id="sess-bounded-nosecret",
        project_spec=ProjectRuntimeEnvironmentSpec(),
    )
    redactor = launch.execution_environment.project_secret_redactor()
    assert redactor is not None
    sanitizer = redactor.stream_sanitizer_factory()()
    assert sanitizer.feed("anything at all") == "anything at all"
    assert sanitizer.flush() == ""
    executor = _executor(launch)
    workspace = TaskWorkspace(str(tmp_path))
    # An undeclared secret-looking string must pass through untouched even
    # across the truncation boundary (no guessing without an authority).
    argv = _first_mate_head_argv()
    raw = _raw_runner(launch, workspace).run(argv, ".", 30.0)
    result = executor.run_project_command(argv, workspace, 30.0)
    assert result.stdout == raw.stdout
    assert result.stdout_truncated is True
    assert HEAD_BOUNDARY_FRAGMENT in result.stdout  # cut by the bound, both runs
    assert SECRET_VALUE not in result.stdout


def test_command_runner_seam_neutrality_and_conflicts(tmp_path):
    workspace = TaskWorkspace(str(tmp_path))

    # A neutral non-secret sanitizer is applied to the complete text
    # before bounding (demonstrating the seam contract).
    class _UpperSanitizer:
        def feed(self, text: str) -> str:
            return text.upper()

        def flush(self) -> str:
            return ""

    runner = CommandRunner(
        workspace,
        environment={"PATH": os.environ.get("PATH", "")},
        output_sanitizer_factory=_UpperSanitizer,
    )
    result = runner.run(
        _child_code("print('abc' * 15000, end='')"), ".", 30.0
    )
    assert result.stdout_truncated is True
    # The sanitizer transformed the complete stream; only the runner's
    # own truncation marker stays untouched (it is applied after
    # sanitization).
    from agentic_debugger.runtime.command_runner import _TRUNCATION_MARKER

    sanitized_view = result.stdout.replace(_TRUNCATION_MARKER, "")
    assert sanitized_view == sanitized_view.upper()
    assert "abc" not in sanitized_view

    # The seam is refused together with a verified execution context, and
    # a non-callable factory is rejected.
    with pytest.raises(CommandRequestError):
        CommandRunner(
            workspace,
            execution_context=object(),  # type: ignore[arg-type]
            output_sanitizer_factory=_UpperSanitizer,
        )
    with pytest.raises(CommandRequestError):
        CommandRunner(
            workspace,
            environment={"PATH": os.environ.get("PATH", "")},
            output_sanitizer_factory="not-callable",
        )


def test_stream_sanitizer_matches_secret_split_across_chunks():
    redactor = ProjectSecretRedactor(((SECRET_NAME, SECRET_VALUE),))
    stream = "A" * 4090 + SECRET_VALUE + "B" * 3000
    expected = "A" * 4090 + f"<PROJECT_SECRET:{SECRET_NAME}>" + "B" * 3000
    # Deterministic chunk sweep: every split granularity (including
    # single-character feeds that cut the secret at every offset) must
    # still match the complete value exactly once.
    for chunk in (1, 2, 3, 7, 4096, 8192):
        instance = redactor.stream_sanitizer_factory()()
        emitted = []
        for start in range(0, len(stream), chunk):
            emitted.append(instance.feed(stream[start : start + chunk]))
        emitted.append(instance.flush())
        assert "".join(emitted) == expected


def test_redactor_bounded_text_and_preview_operations():
    redactor = ProjectSecretRedactor(((SECRET_NAME, SECRET_VALUE),))
    secret_marker = f"<PROJECT_SECRET:{SECRET_NAME}>"
    marker = PDB_BOUNDED_TEXT_TRUNCATION_MARKER
    # Marked bounded text: fragment at the cut is removed, marker kept.
    bounded = "ValueError: " + "q" * 500 + SECRET_VALUE[:100] + marker
    out = redactor.redact_bounded_text(bounded)
    assert SECRET_VALUE[:100] not in out
    assert secret_marker in out
    assert out.endswith(marker)
    # Unmarked text is never boundary-scanned (no guessing).
    unmarked = "plain text ending with " + SECRET_VALUE[:5]
    assert redactor.redact_bounded_text(unmarked) == unmarked
    # Complete secrets inside marked text are still exactly replaced.
    complete = "RuntimeError: " + SECRET_VALUE + " tail " + marker
    out_complete = redactor.redact_bounded_text(complete)
    assert SECRET_VALUE not in out_complete
    assert secret_marker in out_complete
    # Marked preview: whole preview is a proper prefix of the secret.
    assert (
        redactor.redact_truncated_string_preview(SECRET_VALUE[:20])
        == secret_marker
    )
    # Marked preview: the cut lands inside an embedded secret.
    embedded = "x" * 50 + SECRET_VALUE[:7]
    assert (
        redactor.redact_truncated_string_preview(embedded)
        == "x" * 50 + secret_marker
    )
    # Empty values stay inert; determinism holds.
    inert = ProjectSecretRedactor((("EMPTY", ""),))
    assert (
        inert.redact_bounded_text("tail " + SECRET_VALUE[:3] + marker)
        == "tail " + SECRET_VALUE[:3] + marker
    )
    assert redactor.redact_bounded_text(bounded) == out


def test_redactor_structure_preview_requires_explicit_marking():
    redactor = ProjectSecretRedactor(((SECRET_NAME, SECRET_VALUE),))
    secret_marker = f"<PROJECT_SECRET:{SECRET_NAME}>"
    marked = {
        "kind": "str",
        "type": "str",
        "value": "y" * 40 + SECRET_VALUE[:9],
        "special": None,
        "size": 9999,
        "items": [],
        "entries": [],
        "truncated": True,
    }
    out = redactor.redact_structure(marked)
    assert out["value"] == "y" * 40 + secret_marker
    # Truthful structural metadata is preserved untouched.
    assert out["kind"] == "str"
    assert out["type"] == "str"
    assert out["special"] is None
    assert out["size"] == 9999
    assert out["items"] == []
    assert out["entries"] == []
    assert out["truncated"] is True
    # Without the explicit truncated marking there is no fragment
    # guessing: only exact-value replacement applies.
    unmarked = dict(marked, truncated=False)
    out_unmarked = redactor.redact_structure(unmarked)
    assert out_unmarked["value"] == "y" * 40 + SECRET_VALUE[:9]
    # The original structure is not mutated.
    assert marked["value"] == "y" * 40 + SECRET_VALUE[:9]


def test_worker_truncation_marker_constant_matches_pdb_worker():
    from agentic_debugger.runtime.pdb_worker import (
        _POST_MORTEM_TRUNCATION_MARKER,
    )

    assert PDB_BOUNDED_TEXT_TRUNCATION_MARKER == (
        _POST_MORTEM_TRUNCATION_MARKER
    )
