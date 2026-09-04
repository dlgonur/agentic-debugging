"""V2-02 logical Executor seam acceptance tests.

Proves the adopted Executor operations (the only two in V2-02):

- use the fixed session ExecutionEnvironment (post-construction parent
  mutation is invisible);
- respect EffectiveSessionCapabilities (denied capability fails closed as
  tool-unavailable);
- preserve existing CommandRunner/PDB behavior (exit codes, timeouts,
  venv-identity authority);
- create no new process (in-process facade over the existing machinery);
- never bypass verifier authority (the seam has no verifier operation);
- never touch provider credentials.

No speculative API is tested: every assertion exercises an operation the
Local Project source actually calls.
"""

from __future__ import annotations

import os
import sys

import pytest

from agentic_debugger.agent.tool_registry import ToolRejectedError
from agentic_debugger.application.executor import ProductExecutor
from agentic_debugger.application.execution_environment import (
    ExecutionEnvironment,
    ExecutionRole,
)
from agentic_debugger.application.session_runtime import (
    EffectiveSessionCapabilities,
    ProjectEnvDeclaration,
    ProjectRuntimeEnvironmentSpec,
    SessionCapability,
    build_local_project_launch,
)
from agentic_debugger.runtime.command_runner import CommandResult
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.python_launcher import build_worker_env
from agentic_debugger.runtime.workspace import TaskWorkspace

SYNTHETIC_HOP = "AGENTIC_DEBUGGER_PROVIDER_V202_API_KEY"
SYNTHETIC_HOP_VALUE = "sk-synthetic-v202-hop-value-not-a-real-credential"
FLAG = "V2_02_EXECUTOR_FLAG"


def _launch(**overrides):
    fields = {
        "session_id": "sess-v202-executor",
        "task_id": "local-project-debug",
        "policy": "pdb-on-uncertainty",
        "provider_id": None,
        "model_id": None,
        "profile_id": "dummy-profile",
        "launch_snapshot": dict(os.environ),
        "project_spec": ProjectRuntimeEnvironmentSpec(),
    }
    fields.update(overrides)
    return build_local_project_launch(**fields)


def _executor(launch=None) -> ProductExecutor:
    launch = launch or _launch()
    return ProductExecutor(
        execution_environment=launch.execution_environment,
        capabilities=launch.capabilities,
    )


# ---------------------------------------------------------------------------
# Fixed session environment
# ---------------------------------------------------------------------------


def test_run_project_command_uses_fixed_session_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG, "value-A")
    launch = _launch(
        project_spec=ProjectRuntimeEnvironmentSpec(
            inherit=(ProjectEnvDeclaration(FLAG),)
        )
    )
    executor = _executor(launch)
    monkeypatch.setenv(FLAG, "value-B")
    workspace = TaskWorkspace(str(tmp_path))
    result = executor.run_project_command(
        [sys.executable, "-c", "import os; print(os.environ.get(%r))" % FLAG],
        workspace,
        30.0,
    )
    assert isinstance(result, CommandResult)
    assert result.exit_code == 0
    assert "value-A" in result.stdout


def test_run_project_command_excludes_provider_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv(SYNTHETIC_HOP, SYNTHETIC_HOP_VALUE)
    executor = _executor()
    workspace = TaskWorkspace(str(tmp_path))
    result = executor.run_project_command(
        [sys.executable, "-c", "import os; print(%r in os.environ)" % SYNTHETIC_HOP],
        workspace,
        30.0,
    )
    assert result.exit_code == 0
    assert "False" in result.stdout


def test_run_project_command_preserves_runner_behavior(tmp_path):
    executor = _executor()
    workspace = TaskWorkspace(str(tmp_path))
    ok = executor.run_project_command(
        [sys.executable, "-c", "print('hello')"], workspace, 30.0
    )
    assert ok.exit_code == 0
    assert "hello" in ok.stdout
    failing = executor.run_project_command(
        [sys.executable, "-c", "import sys; sys.exit(3)"], workspace, 30.0
    )
    assert failing.exit_code == 3
    timed = executor.run_project_command(
        [sys.executable, "-c", "import time; time.sleep(30)"], workspace, 1.0
    )
    assert timed.timed_out


def test_run_project_command_denied_without_capability(tmp_path):
    capabilities = EffectiveSessionCapabilities(
        capabilities=frozenset({SessionCapability.PDB})
    )
    environment = ExecutionEnvironment.for_local_project(
        {"PATH": "/usr/bin"}, ProjectRuntimeEnvironmentSpec()
    )
    executor = ProductExecutor(
        execution_environment=environment, capabilities=capabilities
    )
    workspace = TaskWorkspace(str(tmp_path))
    with pytest.raises(ToolRejectedError) as excinfo:
        executor.run_project_command(
            [sys.executable, "-c", "print('x')"], workspace, 30.0
        )
    assert "project_command" in str(excinfo.value)


# ---------------------------------------------------------------------------
# PDB operation
# ---------------------------------------------------------------------------


def test_open_product_pdb_uses_fixed_role_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG, "executor-pdb-A")
    launch = _launch(
        project_spec=ProjectRuntimeEnvironmentSpec(
            inherit=(ProjectEnvDeclaration(FLAG),)
        )
    )
    executor = _executor(launch)
    workspace = TaskWorkspace(str(tmp_path))
    session = executor.open_product_pdb(workspace)
    assert isinstance(session, PdbSession)
    worker_env = session._worker_env()
    assert worker_env is not None
    assert worker_env[FLAG] == "executor-pdb-A"
    assert worker_env == build_worker_env(
        dict(
            launch.execution_environment.role_environment(ExecutionRole.PRODUCT_PDB)
        )
    )


def test_open_product_pdb_denied_without_capability(tmp_path):
    capabilities = EffectiveSessionCapabilities(
        capabilities=frozenset({SessionCapability.PROJECT_COMMAND})
    )
    environment = ExecutionEnvironment.for_local_project(
        dict(os.environ), ProjectRuntimeEnvironmentSpec()
    )
    executor = ProductExecutor(
        execution_environment=environment, capabilities=capabilities
    )
    workspace = TaskWorkspace(str(tmp_path))
    with pytest.raises(ToolRejectedError):
        executor.open_product_pdb(workspace)


def test_open_product_pdb_excludes_provider_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv(SYNTHETIC_HOP, SYNTHETIC_HOP_VALUE)
    executor = _executor()
    workspace = TaskWorkspace(str(tmp_path))
    session = executor.open_product_pdb(workspace)
    worker_env = session._worker_env()
    assert worker_env is not None
    assert SYNTHETIC_HOP not in worker_env


# ---------------------------------------------------------------------------
# Seam shape: no process, no verifier bypass, no ambient reads
# ---------------------------------------------------------------------------


def test_executor_creates_no_process_of_its_own():
    executor = _executor()
    assert getattr(executor, "_proc", None) is None
    assert not hasattr(executor, "run_forever")
    # The seam exposes exactly the adopted operations plus fixed role
    # mappings — notably NO verifier operation (the verifier keeps its
    # own independent authority and call path).
    public = {name for name in dir(executor) if not name.startswith("_")}
    assert "run_project_command" in public
    assert "open_product_pdb" in public
    assert "verifier_environment" in public
    assert not any("verif" in name and "evaluate" in name for name in public)
    assert not hasattr(executor, "evaluate")
    assert not hasattr(executor, "verify")


def test_executor_never_reads_parent_environment(tmp_path, monkeypatch):
    """Construction fixes the authority; later ambient state is invisible."""
    executor = _executor()
    monkeypatch.setenv("V2_02_EXECUTOR_LATE", "late")
    workspace = TaskWorkspace(str(tmp_path))
    result = executor.run_project_command(
        [
            sys.executable,
            "-c",
            "import os; print('late' if 'V2_02_EXECUTOR_LATE' in os.environ else 'clean')",
        ],
        workspace,
        30.0,
    )
    assert result.exit_code == 0
    assert "clean" in result.stdout


def test_executor_rejects_invalid_authorities():
    environment = ExecutionEnvironment.for_local_project(
        {"PATH": "/usr/bin"}, ProjectRuntimeEnvironmentSpec()
    )
    capabilities = EffectiveSessionCapabilities(
        capabilities=frozenset({SessionCapability.PROJECT_COMMAND})
    )
    with pytest.raises(Exception):
        ProductExecutor(execution_environment="env", capabilities=capabilities)  # type: ignore[arg-type]
    with pytest.raises(Exception):
        ProductExecutor(execution_environment=environment, capabilities="caps")  # type: ignore[arg-type]


def test_verifier_mapping_comes_from_the_same_fixed_authority(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG, "verifier-parity")
    launch = _launch(
        project_spec=ProjectRuntimeEnvironmentSpec(
            inherit=(ProjectEnvDeclaration(FLAG),)
        )
    )
    executor = _executor(launch)
    verifier_env = dict(executor.verifier_environment())
    assert verifier_env[FLAG] == "verifier-parity"
    assert verifier_env == dict(
        launch.execution_environment.role_environment(ExecutionRole.VERIFIER)
    )
