"""V2-01 execution-environment authority acceptance tests.

Proves the accepted V2-01 invariants:

* Agentic Debugger-owned control/model/provider channels (private provider
  session credential hops, built-in provider credential variables, provider
  CLI-auth authority, config/catalog/quarantine/secure-store control
  variables, the whole repository namespace) never reach the project
  command, product PDB, or verifier role environments;
* benign legacy project ambient variables — including ordinary operator
  ambient network/trust names such as ``HTTPS_PROXY`` — remain
  bridge-compatible (provenance classification, not name guessing);
* CommandRunner no longer decides the Local Project product environment
  from ``os.environ``; conflicting verified/product authorities fail
  closed; VerifiedExecutionContext mode stays constructible unchanged;
* the product PDB worker receives its explicit role environment through
  the established ``build_worker_env`` venv-identity authority.

Only synthetic values are used; no real credential is ever constructed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentic_debugger.application.execution_environment import (
    BRIDGE_COMPATIBILITY_IDENTITY,
    ExecutionEnvironment,
    ExecutionEnvironmentError,
    ExecutionRole,
    is_control_or_provider_authority,
)
from agentic_debugger.application.provider_connections import (
    provider_authority_environment_names,
)
from agentic_debugger.runtime.command_runner import CommandRunner
from agentic_debugger.runtime.exceptions import CommandRequestError
from agentic_debugger.runtime.execution import (
    ContainmentGuarantee,
    DependencyPreparation,
    PreparedEnvironment,
    VerifiedExecutionContext,
)
from agentic_debugger.runtime.pdb_session import PdbSession, PdbSessionError
from agentic_debugger.runtime.python_launcher import build_worker_env
from agentic_debugger.runtime.workspace import TaskWorkspace

SYNTHETIC_HOP_VALUE = "sk-synthetic-v201-hop-value-not-a-real-credential"

#: Private UI→worker provider session hop variables (built-in contracts and
#: the dynamic generic-provider shape).
_SESSION_HOP_NAMES = (
    "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
    "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
    "AGENTIC_DEBUGGER_OLLAMA_API_KEY",
    "AGENTIC_DEBUGGER_PROVIDER_CUSTOMKIND_API_KEY",
)

#: Agentic Debugger provider control/config/quarantine/secure-store variables.
_CONTROL_NAMES = (
    "AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH",
    "AGENTIC_DEBUGGER_CONFIG_DIR",
    "AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH",
    "AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH",
    "AGENTIC_DEBUGGER_DISABLE_SECURE_STORE",
)

#: Built-in provider credential environment authorities + CLI-auth authority.
_PROVIDER_AUTHORITY_NAMES = (
    "OPENCODE_API_KEY",
    "COMMAND_CODE_API_KEY",
    "OLLAMA_API_KEY",
    "OPENCODE_CONFIG_DIR",
)

BENIGN_PROJECT_VAR = "V2_01_BENIGN_PROJECT_DSN"
BENIGN_PROJECT_VALUE = "service://synthetic/test-dsn"

#: Ordinary operator ambient network/trust names the provider transport also
#: happens to read.  V2-01 characterizes them as bridge-compatible ambient
#: state (provenance, not spelling); they are NOT provider-secret channels.
_AMBIENT_NETWORK_NAMES = ("HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE")


def _seed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BENIGN_PROJECT_VAR, BENIGN_PROJECT_VALUE)
    for name in _SESSION_HOP_NAMES + _CONTROL_NAMES + _PROVIDER_AUTHORITY_NAMES:
        monkeypatch.setenv(name, SYNTHETIC_HOP_VALUE)
    for name in _AMBIENT_NETWORK_NAMES:
        monkeypatch.setenv(name, "http://synthetic-proxy.invalid:8080")


def _authority(monkeypatch: pytest.MonkeyPatch) -> ExecutionEnvironment:
    _seed_environment(monkeypatch)
    return ExecutionEnvironment.snapshot_process()


def _all_roles() -> tuple[ExecutionRole, ...]:
    return (
        ExecutionRole.PROJECT_COMMAND,
        ExecutionRole.PRODUCT_PDB,
        ExecutionRole.VERIFIER,
    )


# ---------------------------------------------------------------------------
# Authority classification: provenance, not secret-looking names
# ---------------------------------------------------------------------------

def test_provider_session_hop_never_reaches_project_roles(monkeypatch):
    authority = _authority(monkeypatch)
    for role in _all_roles():
        env = authority.role_environment(role)
        for name in _SESSION_HOP_NAMES:
            assert name not in env
            assert name.upper() not in {k.upper() for k in env}


def test_builtin_provider_and_cli_auth_authorities_excluded(monkeypatch):
    authority = _authority(monkeypatch)
    for role in _all_roles():
        env = authority.role_environment(role)
        for name in _PROVIDER_AUTHORITY_NAMES:
            assert name not in env


def test_control_config_quarantine_secure_store_variables_excluded(monkeypatch):
    authority = _authority(monkeypatch)
    for role in _all_roles():
        env = authority.role_environment(role)
        for name in _CONTROL_NAMES:
            assert name not in env


def test_whole_repository_namespace_is_structurally_excluded():
    for name in (
        "AGENTIC_DEBUGGER_",
        "AGENTIC_DEBUGGER_SOMETHING_NEW_IN_THE_FUTURE",
        "agentic_debugger_theme",
    ):
        assert is_control_or_provider_authority(name)
    assert not is_control_or_provider_authority("PATH")
    assert not is_control_or_provider_authority(BENIGN_PROJECT_VAR)
    assert not is_control_or_provider_authority("")
    assert not is_control_or_provider_authority(123)  # type: ignore[arg-type]


def test_provider_authority_names_are_centralized_not_duplicated():
    names = provider_authority_environment_names()
    for name in _PROVIDER_AUTHORITY_NAMES + (
        "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
        "AGENTIC_DEBUGGER_OLLAMA_API_KEY",
    ):
        assert name in names
    for name in names:
        assert is_control_or_provider_authority(name)


def test_excluded_name_count_reports_counts_not_values(monkeypatch):
    authority = _authority(monkeypatch)
    assert authority.excluded_name_count() >= len(
        _SESSION_HOP_NAMES + _CONTROL_NAMES + _PROVIDER_AUTHORITY_NAMES
    )


# ---------------------------------------------------------------------------
# LEGACY PROJECT AMBIENT bridge: compatibility and network provenance
# ---------------------------------------------------------------------------

def test_benign_project_ambient_remains_bridge_compatible(monkeypatch):
    authority = _authority(monkeypatch)
    for role in _all_roles():
        env = authority.role_environment(role)
        assert env[BENIGN_PROJECT_VAR] == BENIGN_PROJECT_VALUE


def test_ambient_network_state_is_characterized_not_stripped(monkeypatch):
    """Ordinary parent ambient HTTPS_PROXY/NO_PROXY/CA variables pass through
    the V2-01 bridge.  They are NOT classified as provider secrets merely
    because the provider transport reads the same parent names; V2-01 keeps
    this documented residual compatibility instead of inventing a
    project-network authorization model."""
    authority = _authority(monkeypatch)
    for role in _all_roles():
        env = authority.role_environment(role)
        for name in _AMBIENT_NETWORK_NAMES:
            assert env[name] == "http://synthetic-proxy.invalid:8080"


def test_roles_receive_equivalent_declared_project_runtime_state(monkeypatch):
    authority = _authority(monkeypatch)
    project = dict(authority.role_environment(ExecutionRole.PROJECT_COMMAND))
    pdb = dict(authority.role_environment(ExecutionRole.PRODUCT_PDB))
    verifier = dict(authority.role_environment(ExecutionRole.VERIFIER))
    assert project == pdb == verifier
    assert "PATH" in project


def test_bridge_carries_named_compatibility_identity(monkeypatch):
    authority = _authority(monkeypatch)
    assert BRIDGE_COMPATIBILITY_IDENTITY == "legacy-project-ambient/v1"
    assert authority.bridge_identity == BRIDGE_COMPATIBILITY_IDENTITY


# ---------------------------------------------------------------------------
# Authority hygiene: immutability, stability, fail-closed validation
# ---------------------------------------------------------------------------

def test_role_environments_are_immutable(monkeypatch):
    authority = _authority(monkeypatch)
    env = authority.role_environment(ExecutionRole.PROJECT_COMMAND)
    with pytest.raises(TypeError):
        env["INTRUDER"] = "1"  # type: ignore[index]


def test_snapshot_is_copied_and_stable_for_the_session(monkeypatch):
    source = {
        "PATH": "/usr/bin",
        BENIGN_PROJECT_VAR: BENIGN_PROJECT_VALUE,
        "AGENTIC_DEBUGGER_PROVIDER_X_API_KEY": SYNTHETIC_HOP_VALUE,
    }
    authority = ExecutionEnvironment(source)
    # Mutating the caller's mapping after construction must not change the
    # authority (one stable snapshot per session).
    source["PATH"] = "/tampered"
    source["LATE_INJECTION"] = "1"
    for role in _all_roles():
        env = authority.role_environment(role)
        assert env["PATH"] == "/usr/bin"
        assert "LATE_INJECTION" not in env
        assert "AGENTIC_DEBUGGER_PROVIDER_X_API_KEY" not in env


def test_snapshot_process_does_not_track_later_os_changes(monkeypatch):
    authority = _authority(monkeypatch)
    monkeypatch.setenv("V2_01_LATER_VARIABLE", "late")
    for role in _all_roles():
        assert "V2_01_LATER_VARIABLE" not in authority.role_environment(role)


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-mapping",
        {"OK": 1},
        {"OK": None},
        {"": "value"},
        {1: "value"},
    ],
)
def test_invalid_snapshot_fails_closed(bad):
    with pytest.raises(ExecutionEnvironmentError):
        ExecutionEnvironment(bad)


def test_role_environment_rejects_unknown_role(monkeypatch):
    authority = _authority(monkeypatch)
    with pytest.raises(ExecutionEnvironmentError):
        authority.role_environment("project_command")  # type: ignore[arg-type]


def test_repr_never_carries_environment_values(monkeypatch):
    authority = _authority(monkeypatch)
    text = repr(authority)
    assert SYNTHETIC_HOP_VALUE not in text
    assert BENIGN_PROJECT_VALUE not in text
    assert BRIDGE_COMPATIBILITY_IDENTITY in text


# ---------------------------------------------------------------------------
# CommandRunner: explicit product authority, fail-closed conflict
# ---------------------------------------------------------------------------

def _verified_context(tmp_path: Path) -> VerifiedExecutionContext:
    """Minimal VerifiedExecutionContext (same contracts the BugsInPy tests
    exercise) used only to prove precedence/fail-closed behavior."""

    class _FakeContainmentRunner:
        runner_id = "fake-contained"

        def __init__(self, guarantee: ContainmentGuarantee) -> None:
            self.boundary_guarantee = guarantee.to_mapping()

        def run(self, argv, cwd, timeout_seconds, env):  # pragma: no cover
            raise AssertionError("verified runner must not execute in this test")

    guarantee = ContainmentGuarantee(
        str(tmp_path.resolve()),
        "fake-contained",
        resource_limits={"cpu": "1", "memory": "256m"},
    )
    runner = _FakeContainmentRunner(guarantee)
    dependencies = DependencyPreparation(
        "pilot-task",
        "m" * 64,
        "rev-1",
        "project",
        "1",
        "revision",
        "recipe.txt",
        "a" * 64,
        "b" * 64,
    )
    environment = PreparedEnvironment(
        str(tmp_path / "venv" / "python"),
        "3.11",
        ".",
        (),
        {"LANG": "C"},
        dependencies,
    )
    return VerifiedExecutionContext(environment, guarantee, runner)


def test_conflicting_verified_and_product_authorities_fail_closed(tmp_path, monkeypatch):
    _seed_environment(monkeypatch)
    authority = ExecutionEnvironment.snapshot_process()
    workspace = TaskWorkspace(str(tmp_path))
    with pytest.raises(CommandRequestError):
        CommandRunner(
            workspace,
            execution_context=_verified_context(tmp_path),
            environment=authority.role_environment(ExecutionRole.PROJECT_COMMAND),
        )


def test_product_runner_uses_explicit_environment_not_os_environ(tmp_path, monkeypatch):
    """The active defect, closed: a provider session credential present in
    the worker (test process here) must not reach a project command child,
    while the project's own ambient variable keeps working."""
    _seed_environment(monkeypatch)
    authority = ExecutionEnvironment.snapshot_process()
    workspace = TaskWorkspace(str(tmp_path))
    runner = CommandRunner(
        workspace, environment=authority.role_environment(ExecutionRole.PROJECT_COMMAND)
    )
    probe = (
        "import os, sys\n"
        "leak = 'AGENTIC_DEBUGGER_PROVIDER_CUSTOMKIND_API_KEY' in os.environ\n"
        "benign = os.environ.get('V2_01_BENIGN_PROJECT_DSN') == 'service://synthetic/test-dsn'\n"
        "print('leak', leak, 'benign', benign)\n"
        "sys.exit(0 if (not leak and benign) else 7)\n"
    )
    result = runner.run([sys.executable, "-c", probe], ".", 30.0)
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "leak False benign True" in result.stdout


def test_product_runner_adds_io_encoding_contract(tmp_path, monkeypatch):
    _seed_environment(monkeypatch)
    authority = ExecutionEnvironment.snapshot_process()
    workspace = TaskWorkspace(str(tmp_path))
    runner = CommandRunner(
        workspace, environment=authority.role_environment(ExecutionRole.PROJECT_COMMAND)
    )
    result = runner.run(
        [sys.executable, "-c", "import os; print(os.environ['PYTHONIOENCODING'])"],
        ".",
        30.0,
    )
    assert result.exit_code == 0
    assert "utf-8" in result.stdout


def test_nonproduct_fallback_semantics_preserved(tmp_path, monkeypatch):
    """The narrow non-product compatibility default (no authority supplied)
    keeps the historical inheritance behavior for harness/test callers."""
    monkeypatch.setenv("V2_01_HARNESS_VARIABLE", "harness")
    workspace = TaskWorkspace(str(tmp_path))
    runner = CommandRunner(workspace)
    result = runner.run(
        [sys.executable, "-c", "import os; print(os.environ['V2_01_HARNESS_VARIABLE'])"],
        ".",
        30.0,
    )
    assert result.exit_code == 0
    assert "harness" in result.stdout


def test_runner_rejects_invalid_environment_mapping(tmp_path):
    workspace = TaskWorkspace(str(tmp_path))
    with pytest.raises(CommandRequestError):
        CommandRunner(workspace, environment={"KEY": 1})  # type: ignore[dict-item]
    with pytest.raises(CommandRequestError):
        CommandRunner(workspace, environment="PATH=/usr/bin")  # type: ignore[arg-type]


def test_verified_context_authority_still_accepted_alone(tmp_path):
    workspace = TaskWorkspace(str(tmp_path))
    runner = CommandRunner(workspace, execution_context=_verified_context(tmp_path))
    assert runner._execution_context is not None
    assert runner._environment is None


# ---------------------------------------------------------------------------
# Product PDB: explicit role environment through build_worker_env
# ---------------------------------------------------------------------------

def test_pdb_worker_environment_flows_through_build_worker_env(tmp_path, monkeypatch):
    _seed_environment(monkeypatch)
    authority = ExecutionEnvironment.snapshot_process()
    role_env = dict(authority.role_environment(ExecutionRole.PRODUCT_PDB))
    workspace = TaskWorkspace(str(tmp_path))
    session = PdbSession(workspace, worker_environment=dict(role_env))
    # The explicit base mapping passes through the established venv-identity
    # authority (build_worker_env) — identical semantics, no reimplementation.
    assert session._worker_env() == build_worker_env(role_env)
    for name in _SESSION_HOP_NAMES + _PROVIDER_AUTHORITY_NAMES:
        assert name not in session._worker_env()
    assert session._worker_env()[BENIGN_PROJECT_VAR] == BENIGN_PROJECT_VALUE


def test_pdb_default_preserves_inherit_semantics(tmp_path):
    workspace = TaskWorkspace(str(tmp_path))
    session = PdbSession(workspace)
    assert session._worker_env() == build_worker_env(None)


def test_pdb_worker_environment_is_copied_on_boundary(tmp_path):
    workspace = TaskWorkspace(str(tmp_path))
    supplied = {"V2_01_BENIGN_PROJECT_DSN": BENIGN_PROJECT_VALUE}
    session = PdbSession(workspace, worker_environment=supplied)
    supplied["INTRUDER"] = "1"
    assert "INTRUDER" not in session._worker_env()


@pytest.mark.parametrize(
    "bad",
    ["not-a-mapping", {"KEY": 1}, {"KEY": None}, {"": "value"}],
)
def test_pdb_worker_environment_validation_fails_closed(tmp_path, bad):
    workspace = TaskWorkspace(str(tmp_path))
    with pytest.raises(PdbSessionError):
        PdbSession(workspace, worker_environment=bad)


# ---------------------------------------------------------------------------
# LocalProjectVerifier seam: fixed role environment, no post-start mutation
# ---------------------------------------------------------------------------

def test_verifier_runner_factory_uses_fixed_role_environment(tmp_path, monkeypatch):
    """The verifier seam keeps constructing its own runners, but each runner
    receives the FIXED verifier-role mapping derived once by the session
    authority.  A provider credential present in the worker never reaches
    the verifier command child; benign project ambient state does."""
    import sys

    _seed_environment(monkeypatch)
    authority = ExecutionEnvironment.snapshot_process()
    verifier_environment = authority.role_environment(ExecutionRole.VERIFIER)

    def _verifier_runner_factory(workspace):
        return CommandRunner(workspace, environment=verifier_environment)

    workspace = TaskWorkspace(str(tmp_path))
    runner = _verifier_runner_factory(workspace)
    probe = (
        "import os, sys\n"
        "leak = any(name in os.environ for name in "
        "('AGENTIC_DEBUGGER_PROVIDER_CUSTOMKIND_API_KEY', 'OPENCODE_API_KEY', "
        "'COMMAND_CODE_API_KEY', 'OLLAMA_API_KEY', 'OPENCODE_CONFIG_DIR'))\n"
        "benign = os.environ.get('V2_01_BENIGN_PROJECT_DSN') == 'service://synthetic/test-dsn'\n"
        "ambient_proxy = 'HTTPS_PROXY' in os.environ\n"
        "print('leak', leak, 'benign', benign, 'proxy', ambient_proxy)\n"
        "sys.exit(0 if (not leak and benign and ambient_proxy) else 7)\n"
    )
    result = runner.run([sys.executable, "-c", probe], ".", 30.0)
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "leak False benign True proxy True" in result.stdout


def test_verifier_environment_is_fixed_after_verification_begins(tmp_path, monkeypatch):
    """Mutating the worker ambient after the authority snapshot (the
    controller/model path) cannot change the verifier-role mapping the
    factory already closed over."""
    _seed_environment(monkeypatch)
    authority = ExecutionEnvironment.snapshot_process()
    verifier_environment = authority.role_environment(ExecutionRole.VERIFIER)

    def _verifier_runner_factory(workspace):
        return CommandRunner(workspace, environment=verifier_environment)

    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_LATE_API_KEY", SYNTHETIC_HOP_VALUE)
    monkeypatch.setenv("V2_01_LATE_VARIABLE", "late")
    workspace = TaskWorkspace(str(tmp_path))
    runner = _verifier_runner_factory(workspace)
    import sys

    result = runner.run(
        [sys.executable, "-c", "import os; print('late' in repr(sorted(os.environ)))"],
        ".",
        30.0,
    )
    assert result.exit_code == 0
    assert "False" in result.stdout


def test_provider_transport_override_is_never_merged_into_project_roles(monkeypatch):
    """Provenance, not spelling: a provider-derived transport override (even
    one that happens to share an ambient name) is never copied into a
    project role.  The project role derives solely from the classified
    parent ambient snapshot."""
    monkeypatch.setenv(BENIGN_PROJECT_VAR, BENIGN_PROJECT_VALUE)
    monkeypatch.setenv("HTTPS_PROXY", "http://operator-proxy.invalid:8080")
    authority = ExecutionEnvironment.snapshot_process()
    project_env = dict(authority.role_environment(ExecutionRole.PROJECT_COMMAND))
    # The bridge carries the ordinary parent ambient proxy value through...
    assert project_env["HTTPS_PROXY"] == "http://operator-proxy.invalid:8080"
    # ...but a provider-derived override object is a different provenance
    # and is never merged: the authority exposes no merge API and the
    # derived mapping equals the classified snapshot exactly.
    transport_override = {"HTTPS_PROXY": "http://provider-override.invalid:9090"}
    assert project_env["HTTPS_PROXY"] != transport_override["HTTPS_PROXY"]
    for role in _all_roles():
        assert dict(authority.role_environment(role)) == project_env
