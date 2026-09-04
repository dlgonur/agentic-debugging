"""V2-02 repair 10 — project-secret egress seal acceptance tests.

FirstMate regression: a declared project secret, echoed by a normal project
command or held in a debugged local, crossed back RAW into the Agentic
Debugger control/model/evidence domain.  These tests prove the one
per-session redaction authority seals every product egress boundary:

- A: ProductExecutor command output (the direct FirstMate reproduction);
- B: a full deterministic Local Project worker session journal;
- C: the real run_reproduction tool observation (controller/model boundary);
- D/E: real product PDB frame-locals / safe-eval responses and their
  observability events;
- F: product LocalProjectVerifier evidence (TestRecords, to_mapping, Git
  error diagnostics);
- G: serialization/repr surfaces never contain the raw value;
- H: benign declared values are NOT redacted;
- I: overlapping and empty secret values behave deterministically.

No provider network, no real credentials, no frozen experiment.
"""

from __future__ import annotations

import copy
import json
import os
import pickle
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agentic_debugger.agent.controller_policy import ActionName
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application.events import SourceKind
from agentic_debugger.application.execution_environment import (
    ExecutionEnvironment,
    ExecutionRole,
    ProjectSecretRedactor,
)
from agentic_debugger.application.executor import ProductExecutor
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.local_project import (
    LocalProjectTaskSpec,
    cleanup_parent_tmpdir,
    create_isolated_worktree,
    validate_local_project,
)
from agentic_debugger.application.local_project_source import (
    LocalProjectTask,
    _LocalToolContext,
    _build_local_registry,
)
from agentic_debugger.application.observability import (
    ObservabilityContext,
    SessionObservability,
)
from agentic_debugger.application.session import SessionBudgets, SessionSpec
from agentic_debugger.application.session_runtime import (
    ProjectEnvDeclaration,
    ProjectRuntimeEnvironmentSpec,
    build_local_project_launch,
    spec_to_param,
)
from agentic_debugger.application.sources import ExecutionSourceSpec
from agentic_debugger.application.worker_process import SessionWorkerProcess
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
from agentic_debugger.runtime.workspace import TaskWorkspace

SECRET_NAME = "V2_02_PROJECT_SECRET"
SECRET_VALUE = "synthetic-project-secret-value-xyz"
SECRET_MARKER = f"<PROJECT_SECRET:{SECRET_NAME}>"
BENIGN_NAME = "V2_02_PROJECT_BENIGN_FLAG"
BENIGN_VALUE = "benign-project-value"

DUMMY_MODEL = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "command_models"
    / "local_project_dummy.py"
)

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
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    fields = {
        "session_id": "sess-v202-redact",
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


def _echo_secret_argv() -> list:
    return [
        sys.executable,
        "-c",
        f"import os; print(os.environ['{SECRET_NAME}'])",
    ]


# ---------------------------------------------------------------------------
# A. ProductExecutor raw echo (the direct FirstMate reproduction)
# ---------------------------------------------------------------------------


def test_product_executor_redacts_raw_secret_echo(tmp_path, monkeypatch):
    launch = _launch(monkeypatch)
    executor = _executor(launch)
    workspace = TaskWorkspace(str(tmp_path))
    # The child project role really receives the exact raw secret.
    raw_runner = CommandRunner(
        workspace,
        environment=dict(
            launch.execution_environment.role_environment(
                ExecutionRole.PROJECT_COMMAND
            )
        ),
    )
    raw = raw_runner.run(_echo_secret_argv(), ".", 30.0)
    assert raw.exit_code == 0
    assert SECRET_VALUE in raw.stdout
    # The executor boundary redacts it before the control plane sees it.
    result = executor.run_project_command(_echo_secret_argv(), workspace, 30.0)
    assert result.exit_code == 0
    assert result.timed_out is False
    assert SECRET_VALUE not in result.stdout
    assert SECRET_VALUE not in result.stderr
    assert SECRET_MARKER in result.stdout
    # Execution facts are preserved untouched.
    assert result.argv == raw.argv
    assert result.stdout_truncated == raw.stdout_truncated
    failing = executor.run_project_command(
        [sys.executable, "-c", "import sys; sys.exit(3)"], workspace, 30.0
    )
    assert failing.exit_code == 3


# ---------------------------------------------------------------------------
# B. Full deterministic worker session: raw secret never reaches the journal
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


def _git_fixture(tmp_path: Path, name: str) -> tuple[Path, str]:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "calculator.py").write_text(
        "def add(a,b):\n    return a - b\n", encoding="utf-8"
    )
    (repo / "test_calculator.py").write_text(
        "from calculator import add\ndef test_add():\n    assert add(1,2)==3\n",
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
    return repo, head


def _write_dummy_profile(root: Path, profile_id: str, patch_path: Path) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir = root / f"state-{profile_id}"
    data_file = root / f"data-{profile_id}.json"
    data_file.write_text(
        json.dumps(
            {
                "symbol": "add",
                "file": "calculator.py",
                "hypothesis_id": "h1",
                "statement": "add returns a - b instead of a + b",
                "patch_file": str(patch_path),
                "expressions": [],
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "command-models.json").write_text(
        json.dumps(
            {
                "schema_version": "command-models-v1",
                "profiles": [
                    {
                        "profile_id": profile_id,
                        "display_name": "Dummy redact",
                        "executable": sys.executable,
                        "argv": [
                            str(DUMMY_MODEL),
                            "--state-dir",
                            str(state_dir),
                            "--data",
                            str(data_file),
                        ],
                        "request_timeout_seconds": 10,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


def test_worker_session_journal_never_contains_raw_secret(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    repo, head = _git_fixture(tmp_path, "redact_repo")
    patch_path = tmp_path / "good.patch"
    patch_path.write_text(GOOD_PATCH, encoding="utf-8")
    store_root = _write_dummy_profile(tmp_path / "cfg", "dummy-redact", patch_path)
    validated = validate_local_project(str(repo), launch_cwd=tmp_path)
    wt = create_isolated_worktree(validated.repo_root, validated.head_commit)
    session_id = f"sess-redact-b-{uuid.uuid4().hex[:6]}"
    try:
        store = HistoryStore(tmp_path / "hist")
        # The reproduction command prints the declared secret and its exit
        # truthfully depends on the buggy calculator (fails baseline,
        # passes post-patch): the session keeps real result semantics.
        repro = (
            "python -c \"import os; "
            f"print(os.environ['{SECRET_NAME}']); "
            "from calculator import add; "
            'raise SystemExit(0 if add(1,2)==3 else 1)"'
        )
        worker = SessionWorkerProcess(
            session_dir=store.session_dir(session_id),
            session_id=session_id,
            spec=SessionSpec(
                task_id="local-project-debug",
                source=ExecutionSourceSpec(
                    kind=SourceKind.LOCAL_PROJECT,
                    task_id="local-project-debug",
                    model_config_ref="dummy-redact",
                ),
            ),
            run_id=f"run-{session_id}",
            scenario="local_project",
            scenario_params={
                "project_repo_path": str(repo),
                "project_head": validated.head_commit,
                "isolated_workspace": str(wt.isolated_path),
                "bug_description": "add returns a - b",
                "reproduction_command": repro,
                "verification_command": 'python -c "from calculator import add; raise SystemExit(0 if add(0,0)==0 else 1)"',
                "config_root": str(store_root),
                "profile_id": "dummy-redact",
                "expected_fingerprint": None,
                "parent_tmpdir": str(wt.parent_tmpdir),
                "policy": "pdb-on-uncertainty",
                "project_runtime_spec": spec_to_param(_spec()),
            },
            cooperative_grace_seconds=5.0,
            ready_timeout_seconds=30.0,
            max_elapsed_seconds=180,
        )
        worker.session_dir.mkdir(parents=True, exist_ok=True)
        local_spec = LocalProjectTaskSpec(
            session_id=session_id,
            source_repo_path=str(repo),
            source_head_commit=validated.head_commit,
            isolated_workspace_path=str(wt.isolated_path),
            bug_description="add returns a - b",
            reproduction_command=repro,
            verification_command='python -c "from calculator import add; raise SystemExit(0 if add(0,0)==0 else 1)"',
            model_runtime="dummy-redact",
            budgets=SessionBudgets(max_elapsed_seconds=180),
            created_at_utc="2026-09-04T00:00:00Z",
            project_runtime=_spec().to_mapping(),
        )
        (worker.session_dir / "local_project_task.json").write_text(
            json.dumps(local_spec.to_mapping(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        assert worker.start() is None
        result = worker.wait()
        # Truthful session semantics retained end-to-end.
        assert result.status.value == "succeeded"
        journal_path = worker.session_dir / "session.events.jsonl"
        raw_journal = journal_path.read_bytes()
        assert SECRET_VALUE.encode("utf-8") not in raw_journal
        assert SECRET_MARKER.encode("utf-8") in raw_journal
        from agentic_debugger.application.journal import read_session_journal

        journal = read_session_journal(journal_path)
        diagnosis = [
            event
            for event in journal.events
            if "reproduction result exit" in str(
                event.payload.get("text", "")
            )
        ]
        assert diagnosis
        assert SECRET_VALUE not in str(diagnosis[0].payload)
        assert SECRET_MARKER in str(diagnosis[0].payload)
        disposition = json.loads(
            (worker.session_dir / "local_project_disposition.json").read_text(
                encoding="utf-8"
            )
        )
        assert disposition["disposition"] == "FIXED"
    finally:
        worker.close()
        cleanup_parent_tmpdir(wt.parent_tmpdir, validated.repo_root)


# ---------------------------------------------------------------------------
# C. Real registry run_reproduction tool observation (model boundary)
# ---------------------------------------------------------------------------


def test_run_reproduction_tool_observation_redacts_secret(tmp_path, monkeypatch):
    launch = _launch(monkeypatch)
    executor = _executor(launch)
    task = LocalProjectTask(
        reproduction_command=(
            "python -c \"import os; "
            f"print(os.environ['{SECRET_NAME}']); "
            'raise SystemExit(3)"'
        )
    )
    context = _LocalToolContext(
        isolated=tmp_path,
        tracked=[],
        task=task,
        probe=None,
        observability=None,
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
    action = Action(
        action_id="action-1",
        run_id="run-1",
        task_id="local-project-debug",
        state=ControllerState.REPRODUCE,
        name=ActionName.RUN_REPRODUCTION.value,
        arguments={"phase": "baseline"},
    )
    observation = registry.dispatch(action, observation_id="observation-1")
    assert observation.status.value == "ok"
    payload = observation.payload
    assert payload["exit_code"] == 3
    assert payload["failure_reproduced"] is True
    assert SECRET_VALUE not in json.dumps(payload)
    assert SECRET_MARKER in payload["failure_output"]


# ---------------------------------------------------------------------------
# D/E. Real product PDB boundaries (frame locals and safe eval)
# ---------------------------------------------------------------------------


PDB_SCRIPT_LINES = [
    "import os",
    "import sys",
    "",
    "def main():",
    "    LEAK = os.environ['%s']" % SECRET_NAME,
    "    print(len(LEAK))",
    "",
    "main()",
]


def _pdb_redaction_context(tmp_path, monkeypatch, launch):
    """Start a real product PDB session paused inside main() with LEAK bound."""
    executor = _executor(launch)
    ws_root = tmp_path / "pdbws"
    ws_root.mkdir()
    (ws_root / "repro_secret.py").write_text(
        "\n".join(PDB_SCRIPT_LINES), encoding="utf-8"
    )
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
    # Break at the print inside main(): the local LEAK already holds the
    # raw secret inside the debugged function frame.
    breakpoint_line = PDB_SCRIPT_LINES.index("    print(len(LEAK))") + 1
    started = session.start_paused_target("repro_secret.py", [breakpoint_line])
    assert started.get("state") == "paused"
    stack = session.get_stack_summary()
    generation = stack["pause_generation"]
    return context, registry, generation


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


def test_pdb_frame_locals_redact_secret(tmp_path, monkeypatch):
    launch = _launch(monkeypatch, session_id="sess-redact-pdb")
    context, registry, generation = _pdb_redaction_context(
        tmp_path, monkeypatch, launch
    )
    session = context.pdb_session
    try:
        # The debugged process itself sees the exact raw secret.
        raw_locals = session.get_frame_locals(0, generation)
        raw_values = json.dumps(raw_locals, default=str)
        assert SECRET_VALUE in raw_values
        # The product tool payload is redacted.
        observation = _dispatch(
            registry,
            ActionName.GET_FRAME_LOCALS,
            {"frame_id": 0, "pause_generation": generation},
        )
        assert observation.status.value == "ok"
        assert SECRET_VALUE not in json.dumps(observation.payload)
        assert SECRET_MARKER in json.dumps(observation.payload)
        # The same sanitized object fed the observability event.
        locals_events = [
            event
            for event in context.observability.events()
            if event.event_kind.value == "debugger.locals_observed"
        ]
        assert locals_events
        event_text = json.dumps(
            locals_events[-1].to_mapping(), default=str
        )
        assert SECRET_VALUE not in event_text
        assert SECRET_MARKER in event_text
    finally:
        context.release_pdb()


def test_pdb_safe_eval_redact_secret(tmp_path, monkeypatch):
    launch = _launch(monkeypatch, session_id="sess-redact-eval")
    context, registry, generation = _pdb_redaction_context(
        tmp_path, monkeypatch, launch
    )
    session = context.pdb_session
    try:
        # The restricted grammar legally reads the local holding the secret.
        raw_eval = session.safe_eval_expression(0, generation, "LEAK")
        assert SECRET_VALUE in json.dumps(raw_eval, default=str)
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
        assert SECRET_VALUE not in json.dumps(observation.payload)
        assert SECRET_MARKER in json.dumps(observation.payload)
    finally:
        context.release_pdb()


# ---------------------------------------------------------------------------
# F. Product LocalProjectVerifier evidence
# ---------------------------------------------------------------------------


def _verifier_repo(tmp_path: Path, name: str) -> tuple[Path, str]:
    repo, head = _git_fixture(tmp_path, name)
    (repo / "reproduce.py").write_text(
        "import os\n"
        f"print(os.environ['{SECRET_NAME}'])\n"
        "from calculator import add\n"
        "raise SystemExit(0 if add(2, 3) == 5 else 1)\n",
        encoding="utf-8",
    )
    (repo / "regression.py").write_text(
        "from calculator import add\n"
        "raise SystemExit(0 if add(0, 0) == 0 else 1)\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "commands")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout.strip()
    return repo, head


def _product_verifier(launch, product_environment):
    return LocalProjectVerifier(
        product_environment=product_environment,
        product_secret_redactor=(
            launch.execution_environment.project_secret_redactor()
        ),
    )


def test_product_verifier_evidence_redacts_secret(tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    launch = _launch(monkeypatch, session_id="sess-redact-verifier")
    repo, head = _verifier_repo(tmp_path, "verifier_repo")
    # GOOD_PATCH fixes add: baseline reproduction prints the secret and
    # FAILS, post-patch reproduction PASSES -> RESOLVED, proving the
    # classification is unchanged by redaction.
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
    product_environment = dict(
        launch.execution_environment.role_environment(ExecutionRole.VERIFIER)
    )
    # The verifier children really receive the exact raw secret.
    raw_runner = CommandRunner(
        TaskWorkspace(str(tmp_path)), environment=dict(product_environment)
    )
    raw = raw_runner.run(
        [sys.executable, "-c", f"import os; print(os.environ['{SECRET_NAME}'])"],
        ".",
        30.0,
    )
    assert SECRET_VALUE in raw.stdout
    verifier = _product_verifier(launch, product_environment)
    result = verifier.evaluate(plan)
    try:
        assert result.status is EvaluationStatus.COMPLETED
        assert result.outcome is SemanticOutcome.RESOLVED
        for record in (
            result.baseline_reproduction,
            result.baseline_regression,
            result.post_patch_reproduction,
            result.regression,
        ):
            assert record is not None
            assert SECRET_VALUE not in record.stdout
            assert SECRET_VALUE not in record.stderr
        assert SECRET_VALUE not in json.dumps(result.to_mapping(), default=str)
        assert SECRET_MARKER in result.baseline_reproduction.stdout
        assert result.workspace.cleaned
    finally:
        pass


def test_verifier_git_error_text_redacted_by_injected_subprocess(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    launch = _launch(monkeypatch, session_id="sess-redact-git")
    redactor = launch.execution_environment.project_secret_redactor()
    repo, _head = _git_fixture(tmp_path, "git_repo")

    def _failing_git(*args, **kwargs):
        from subprocess import CompletedProcess

        return CompletedProcess(
            args, returncode=1, stdout=b"", stderr=SECRET_VALUE.encode("utf-8")
        )

    from agentic_debugger.evaluation import local_project_verifier as module

    monkeypatch.setattr(module.subprocess, "run", _failing_git)
    with pytest.raises(EvaluationInputError) as excinfo:
        _run_git(
            str(repo),
            ["status", "--porcelain"],
            environment={"PATH": os.environ.get("PATH", "")},
            redactor=redactor,
        )
    assert SECRET_VALUE not in str(excinfo.value)
    assert SECRET_MARKER in str(excinfo.value)


def test_verifier_rejects_incompatible_redaction_authorities():
    redactor = ProjectSecretRedactor(((SECRET_NAME, SECRET_VALUE),))
    with pytest.raises(Exception) as without_env:
        LocalProjectVerifier(product_secret_redactor=redactor)
    assert "product_environment" in str(without_env.value)
    from agentic_debugger.runtime.workspace import TaskWorkspace as _WS

    with pytest.raises(Exception):
        LocalProjectVerifier(
            product_environment={"PATH": os.environ.get("PATH", "")},
            product_secret_redactor=redactor,
            command_runner_factory=lambda workspace: CommandRunner(workspace),
        )
    with pytest.raises(Exception):
        LocalProjectVerifier(
            product_environment={"PATH": os.environ.get("PATH", "")},
            product_secret_redactor="not-the-authority",
        )


# ---------------------------------------------------------------------------
# G. Serialization / repr surfaces
# ---------------------------------------------------------------------------


def test_raw_secret_absent_from_safe_structures(tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    monkeypatch.setenv(BENIGN_NAME, BENIGN_VALUE)
    launch = _launch(
        monkeypatch,
        session_id="sess-redact-serial",
        project_spec=_spec(
            values=(),
            inherit=(ProjectEnvDeclaration(BENIGN_NAME),),
        ),
    )
    environment = launch.execution_environment
    executor = _executor(launch)
    redactor = environment.project_secret_redactor()
    assert redactor is not None

    def _clean(value) -> bool:
        return SECRET_VALUE not in json.dumps(value, default=str)

    assert _clean(launch.project_spec.to_mapping())
    assert _clean(launch.to_mapping())
    assert SECRET_VALUE not in launch.fingerprint()
    task_spec = LocalProjectTaskSpec(
        session_id="sess-redact-serial",
        source_repo_path=str(tmp_path),
        source_head_commit="a" * 40,
        isolated_workspace_path=str(tmp_path),
        bug_description="bug",
        reproduction_command=None,
        verification_command=None,
        model_runtime=None,
        budgets=SessionBudgets(),
        created_at_utc="2026-09-04T00:00:00Z",
        project_runtime=launch.project_spec.to_mapping(),
    )
    assert _clean(task_spec.to_mapping())
    assert SECRET_VALUE not in repr(environment)
    assert SECRET_VALUE not in repr(executor)
    assert SECRET_VALUE not in repr(redactor)
    assert SECRET_VALUE not in repr(launch)
    # The redaction authority itself is non-serializable, fail-closed.
    with pytest.raises(Exception):
        pickle.dumps(redactor)
    with pytest.raises(Exception):
        copy.copy(redactor)
    assert not hasattr(redactor, "to_mapping")
    assert not hasattr(redactor, "secret_values")


# ---------------------------------------------------------------------------
# H. Benign declared values are NOT redacted
# ---------------------------------------------------------------------------


def test_non_secret_project_values_are_not_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    monkeypatch.setenv(BENIGN_NAME, BENIGN_VALUE)
    launch = _launch(
        monkeypatch,
        session_id="sess-redact-benign",
        project_spec=_spec(
            inherit=(ProjectEnvDeclaration(BENIGN_NAME),),
        ),
    )
    executor = _executor(launch)
    result = executor.run_project_command(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['%s']); print(os.environ['%s'])"
            % (BENIGN_NAME, SECRET_NAME),
        ],
        TaskWorkspace(str(tmp_path)),
        30.0,
    )
    assert result.exit_code == 0
    assert BENIGN_VALUE in result.stdout
    assert SECRET_VALUE not in result.stdout
    assert SECRET_MARKER in result.stdout
    redactor = launch.execution_environment.project_secret_redactor()
    assert redactor.redact(BENIGN_VALUE) == BENIGN_VALUE
    structure = {"flag": BENIGN_VALUE, "count": 7, "none": None}
    assert redactor.redact_structure(structure) == structure


# ---------------------------------------------------------------------------
# I. Overlap and empty secret value semantics
# ---------------------------------------------------------------------------


def test_overlapping_secret_values_redact_longest_first():
    redactor = ProjectSecretRedactor(
        (
            ("SECRET_SHORT", "abcde"),
            ("SECRET_LONG", "abcdefghij"),
            ("ALPHABETICAL", "abcde"),
        )
    )
    text = "x abcdefghij y abcde z"
    redacted = redactor.redact(text)
    # Longest value first; equal-length values resolve deterministically by
    # name order (ALPHABETICAL < SECRET_SHORT).
    assert redacted == (
        "x <PROJECT_SECRET:SECRET_LONG> y <PROJECT_SECRET:ALPHABETICAL> z"
    )
    # Deterministic across repeats.
    assert redactor.redact(text) == redacted


def test_empty_secret_value_is_explicitly_inert():
    redactor = ProjectSecretRedactor((("SECRET_EMPTY", ""),))
    assert redactor.redact("anything at all") == "anything at all"
    assert redactor.redact("") == ""
    structure = {"a": ["b", {"c": ""}], "d": (1, 2)}
    assert redactor.redact_structure(structure) == structure
    mixed = ProjectSecretRedactor(
        (("SECRET_EMPTY", ""), ("SECRET_REAL", "real-value"))
    )
    assert mixed.redact("has real-value here") == (
        "has <PROJECT_SECRET:SECRET_REAL> here"
    )


def test_redactor_rejects_invalid_bindings():
    with pytest.raises(Exception):
        ProjectSecretRedactor("not-a-tuple")
    with pytest.raises(Exception):
        ProjectSecretRedactor((("1BAD_NAME", "value"),))
    with pytest.raises(Exception):
        ProjectSecretRedactor(((SECRET_NAME, "v1"), (SECRET_NAME, "v2")))
    with pytest.raises(Exception):
        ProjectSecretRedactor(((SECRET_NAME, 123),))
