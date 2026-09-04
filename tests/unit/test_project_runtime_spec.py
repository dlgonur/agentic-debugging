"""V2-02 ProjectRuntimeEnvironmentSpec acceptance tests.

Proves the declarative ingress and the retirement of arbitrary ambient
inheritance on the normal Local Project path:

A. empty spec: essentials work, undeclared parent variables invisible;
B. declared inherit: visible to project/PDB/verifier roles, unrelated absent;
C. stable snapshot: post-start parent mutation cannot alter the session;
D. missing required: fail closed, safe name-only error, no ambient fallback;
E. project secret: ephemeral by-name binding, never serialized/shown, never
   in the model channel, never in cleanup;
F. provider secret: absent from every declarative role;
G. network provenance: proxy names not inherited by default, available when
   declared, provider transport unchanged.

Only synthetic values are used; no real credential is ever constructed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from agentic_debugger.application.command_transport import (
    CancellableJsonlCommandTransport,
)
from agentic_debugger.application.execution_environment import (
    BRIDGE_COMPATIBILITY_IDENTITY,
    ExecutionEnvironment,
    ExecutionEnvironmentError,
    ExecutionRole,
)
from agentic_debugger.application.session_runtime import (
    PROJECT_RUNTIME_SPEC_VERSION,
    ProjectEnvDeclaration,
    ProjectExplicitValue,
    ProjectRuntimeEnvironmentSpec,
    materialize_project_runtime,
    spec_from_param,
    spec_to_param,
)
from agentic_debugger.runtime.command_runner import CommandRunner
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.python_launcher import build_worker_env
from agentic_debugger.runtime.workspace import TaskWorkspace

SYNTHETIC_SECRET = "synthetic-v202-project-secret-not-a-real-credential"
SYNTHETIC_HOP_VALUE = "sk-synthetic-v202-hop-value-not-a-real-credential"

FLAG = "V2_02_PROJECT_FLAG"
FLAG_VALUE = "flag-value-A"
UNDECLARED = "V2_02_UNDECLARED_AMBIENT"
SECRET_NAME = "V2_02_PROJECT_DB_URL"
HOP_NAME = "AGENTIC_DEBUGGER_PROVIDER_V202_API_KEY"


def _project_roles():
    return (
        ExecutionRole.PROJECT_COMMAND,
        ExecutionRole.PRODUCT_PDB,
        ExecutionRole.VERIFIER,
    )


def _authority(extra: dict | None = None) -> ExecutionEnvironment:
    """One declarative authority over the live parent snapshot."""
    snapshot = dict(os.environ)
    if extra:
        snapshot.update(extra)
    return ExecutionEnvironment.for_local_project(
        snapshot, ProjectRuntimeEnvironmentSpec()
    )


def _authority_with_spec(
    spec: ProjectRuntimeEnvironmentSpec, extra: dict | None = None
) -> ExecutionEnvironment:
    snapshot = dict(os.environ)
    if extra:
        snapshot.update(extra)
    return ExecutionEnvironment.for_local_project(snapshot, spec)


def _inherit_spec(*names: str, required: bool = True) -> ProjectRuntimeEnvironmentSpec:
    return ProjectRuntimeEnvironmentSpec(
        inherit=tuple(ProjectEnvDeclaration(name, required) for name in names)
    )


def _secret_spec(*names: str, required: bool = True) -> ProjectRuntimeEnvironmentSpec:
    return ProjectRuntimeEnvironmentSpec(
        secrets=tuple(ProjectEnvDeclaration(name, required) for name in names)
    )


# ---------------------------------------------------------------------------
# A. no custom declaration
# ---------------------------------------------------------------------------


def test_empty_spec_keeps_platform_essentials(monkeypatch):
    monkeypatch.setenv(UNDECLARED, "should-never-appear")
    authority = _authority()
    assert not authority.uses_legacy_bridge
    for role in _project_roles():
        env = authority.role_environment(role)
        assert "PATH" in env
        assert UNDECLARED not in env


def test_empty_spec_child_sees_no_undeclared_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(UNDECLARED, "should-never-appear")
    authority = _authority()
    workspace = TaskWorkspace(str(tmp_path))
    runner = CommandRunner(
        workspace,
        environment=authority.role_environment(ExecutionRole.PROJECT_COMMAND),
    )
    result = runner.run(
        [sys.executable, "-c", "import os; print('seen' if os.environ.get(%r) else 'absent')" % UNDECLARED],
        ".",
        30.0,
    )
    assert result.exit_code == 0
    assert "absent" in result.stdout


def test_declarative_path_does_not_use_the_bridge(monkeypatch):
    authority = _authority()
    assert authority.bridge_identity == PROJECT_RUNTIME_SPEC_VERSION
    assert BRIDGE_COMPATIBILITY_IDENTITY == "legacy-project-ambient/v1"


# ---------------------------------------------------------------------------
# B. explicitly inherited benign variable
# ---------------------------------------------------------------------------


def test_declared_inherit_visible_to_all_project_roles(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG, FLAG_VALUE)
    monkeypatch.setenv(UNDECLARED, "should-never-appear")
    authority = _authority_with_spec(_inherit_spec(FLAG))
    for role in _project_roles():
        env = authority.role_environment(role)
        assert env[FLAG] == FLAG_VALUE
        assert UNDECLARED not in env


def test_declared_inherit_reaches_command_child(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG, FLAG_VALUE)
    monkeypatch.setenv(UNDECLARED, "should-never-appear")
    authority = _authority_with_spec(_inherit_spec(FLAG))
    workspace = TaskWorkspace(str(tmp_path))
    runner = CommandRunner(
        workspace,
        environment=authority.role_environment(ExecutionRole.PROJECT_COMMAND),
    )
    probe = (
        "import os, sys; "
        "ok = os.environ.get(%r) == %r and %r not in os.environ; "
        "print('ok' if ok else 'bad'); sys.exit(0 if ok else 7)"
        % (FLAG, FLAG_VALUE, UNDECLARED)
    )
    result = runner.run([sys.executable, "-c", probe], ".", 30.0)
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_declared_inherit_reaches_pdb_worker_env(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG, FLAG_VALUE)
    authority = _authority_with_spec(_inherit_spec(FLAG))
    role_env = dict(authority.role_environment(ExecutionRole.PRODUCT_PDB))
    workspace = TaskWorkspace(str(tmp_path))
    session = PdbSession(workspace, worker_environment=role_env)
    worker_env = session._worker_env()
    assert worker_env is not None
    assert worker_env[FLAG] == FLAG_VALUE
    # The explicit mapping still flows through the established venv-identity
    # authority (no reimplementation, Windows PID behavior preserved).
    assert worker_env == build_worker_env(role_env)


def test_declared_inherit_reaches_verifier_commands(tmp_path, monkeypatch):
    """Verifier parity: the verifier role carries the declared input."""
    from agentic_debugger.evaluation.local_project_verifier import LocalProjectVerifier

    monkeypatch.setenv(FLAG, FLAG_VALUE)
    authority = _authority_with_spec(_inherit_spec(FLAG))
    verifier = LocalProjectVerifier(
        product_environment=dict(
            authority.role_environment(ExecutionRole.VERIFIER)
        )
    )
    workspace = TaskWorkspace(str(tmp_path))
    runner = verifier._make_runner(workspace)
    result = runner.run(
        [
            sys.executable,
            "-c",
            "import os, sys; sys.exit(0 if os.environ.get(%r) == %r else 7)"
            % (FLAG, FLAG_VALUE),
        ],
        ".",
        30.0,
    )
    assert result.exit_code == 0, result.stdout + result.stderr


def test_full_verifier_run_sees_declared_variable(tmp_path, monkeypatch):
    """End-to-end verifier parity through the independent verifier.

    RESOLVED is only reachable when every verifier workspace (baseline
    repro, baseline regression, post-patch repro, regression) observes
    the declared flag: the baseline repro exits 1 on the real bug, and
    the post-patch repro exits 0 only with the flag present.
    """
    from agentic_debugger.evaluation.local_project_verifier import (
        LocalProjectEvaluationPlan,
        LocalProjectVerifier,
    )

    monkeypatch.setenv(FLAG, FLAG_VALUE)
    authority = _authority_with_spec(_inherit_spec(FLAG))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "from calc import add\ndef test_add():\n    assert add(0, 0) == 0\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), check=True,
                          stdout=subprocess.PIPE, text=True).stdout.strip()
    repro = [
        sys.executable, "-c",
        "import os, sys; "
        "assert os.environ.get(%r) == %r, 'declared flag missing'; "
        "from calc import add; sys.exit(0 if add(1, 2) == 3 else 1)" % (FLAG, FLAG_VALUE),
    ]
    regression = [sys.executable, "-m", "pytest", "test_calc.py", "-q"]
    plan = LocalProjectEvaluationPlan(
        source_repo_path=str(repo),
        source_head_commit=head,
        candidate_patch=(
            "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n-    return a - b\n+    return a + b\n"
        ),
        reproduction_argv=tuple(repro),
        regression_argv=tuple(regression),
        allowed_paths=("calc.py", "test_calc.py"),
        denied_paths=("tests", "task.json"),
        timeout_seconds=60.0,
    )
    verifier = LocalProjectVerifier(
        product_environment=dict(
            authority.role_environment(ExecutionRole.VERIFIER)
        )
    )
    result = verifier.evaluate(plan)
    assert result.resolved, result.diagnostic


# ---------------------------------------------------------------------------
# C. stable snapshot
# ---------------------------------------------------------------------------


def test_session_materialization_is_stable_after_parent_mutation(monkeypatch):
    monkeypatch.setenv(FLAG, "value-A")
    authority = _authority_with_spec(_inherit_spec(FLAG))
    monkeypatch.setenv(FLAG, "value-B")
    monkeypatch.setenv("V2_02_LATE_INJECTION", "late")
    for role in _project_roles():
        env = authority.role_environment(role)
        assert env[FLAG] == "value-A"
        assert "V2_02_LATE_INJECTION" not in env


def test_materialization_copies_do_not_alias(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG, FLAG_VALUE)
    authority = _authority_with_spec(_inherit_spec(FLAG))
    first = authority.role_environment(ExecutionRole.PROJECT_COMMAND)
    with pytest.raises(TypeError):
        first["INTRUDER"] = "1"  # type: ignore[index]


# ---------------------------------------------------------------------------
# D. missing required variable
# ---------------------------------------------------------------------------


def test_missing_required_inherit_fails_closed(monkeypatch):
    monkeypatch.delenv("V2_02_MISSING_REQUIRED", raising=False)
    spec = _inherit_spec("V2_02_MISSING_REQUIRED")
    with pytest.raises(ExecutionEnvironmentError) as excinfo:
        _authority_with_spec(spec)
    message = str(excinfo.value)
    assert "V2_02_MISSING_REQUIRED" in message
    assert "unavailable" in message


def test_missing_required_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("V2_02_MISSING_SECRET", raising=False)
    spec = _secret_spec("V2_02_MISSING_SECRET")
    with pytest.raises(ExecutionEnvironmentError) as excinfo:
        _authority_with_spec(spec)
    assert "V2_02_MISSING_SECRET" in str(excinfo.value)


def test_missing_optional_declaration_is_skipped(monkeypatch):
    monkeypatch.delenv("V2_02_OPTIONAL_ABSENT", raising=False)
    spec = ProjectRuntimeEnvironmentSpec(
        inherit=(ProjectEnvDeclaration("V2_02_OPTIONAL_ABSENT", False),)
    )
    authority = _authority_with_spec(spec)
    for role in _project_roles():
        assert "V2_02_OPTIONAL_ABSENT" not in authority.role_environment(role)


def test_materialize_error_names_variable_without_value():
    spec = _inherit_spec("V2_02_MISSING_DIRECT")
    with pytest.raises(Exception) as excinfo:
        materialize_project_runtime(spec, {"PATH": "/usr/bin"})
    assert "V2_02_MISSING_DIRECT" in str(excinfo.value)


# ---------------------------------------------------------------------------
# E. project secret declaration
# ---------------------------------------------------------------------------


def test_project_secret_reaches_project_roles_not_cleanup(monkeypatch):
    monkeypatch.setenv(SECRET_NAME, SYNTHETIC_SECRET)
    authority = _authority_with_spec(_secret_spec(SECRET_NAME))
    for role in _project_roles():
        assert authority.role_environment(role)[SECRET_NAME] == SYNTHETIC_SECRET
    cleanup = authority.role_environment(ExecutionRole.CLEANUP)
    assert SECRET_NAME not in cleanup


def test_project_secret_never_serialized_or_repr(monkeypatch):
    import json as _json

    monkeypatch.setenv(SECRET_NAME, SYNTHETIC_SECRET)
    spec = _secret_spec(SECRET_NAME)
    authority = _authority_with_spec(spec)
    assert SYNTHETIC_SECRET not in repr(spec)
    assert SYNTHETIC_SECRET not in repr(authority)
    assert SYNTHETIC_SECRET not in _json.dumps(spec.to_mapping(), sort_keys=True)
    assert SYNTHETIC_SECRET not in spec_to_param(spec)
    materialization = materialize_project_runtime(spec, dict(os.environ))
    assert SYNTHETIC_SECRET not in repr(materialization)
    # The materialization deliberately has no serialization surface.
    assert not hasattr(materialization, "to_mapping")
    assert not hasattr(materialization, "to_dict")


def test_project_secret_never_reaches_model_channel(monkeypatch):
    monkeypatch.setenv(SECRET_NAME, SYNTHETIC_SECRET)
    adapter_env = CancellableJsonlCommandTransport.subprocess_environment()
    assert SECRET_NAME not in adapter_env
    assert SYNTHETIC_SECRET not in " ".join(adapter_env.values())


def test_project_secret_never_in_launch_provenance(monkeypatch):
    import json as _json

    from agentic_debugger.application.session_runtime import (
        build_local_project_launch,
    )

    monkeypatch.setenv(SECRET_NAME, SYNTHETIC_SECRET)
    launch = build_local_project_launch(
        session_id="sess-v202-secret-prov",
        task_id="local-project-debug",
        policy="pdb-on-uncertainty",
        provider_id=None,
        model_id=None,
        profile_id="dummy-profile",
        launch_snapshot=dict(os.environ),
        project_spec=_secret_spec(SECRET_NAME),
    )
    assert SYNTHETIC_SECRET not in _json.dumps(launch.to_mapping(), sort_keys=True)
    assert SYNTHETIC_SECRET not in repr(launch)
    assert SYNTHETIC_SECRET not in launch.fingerprint()


# ---------------------------------------------------------------------------
# F. provider secret stays absent
# ---------------------------------------------------------------------------


def test_provider_authorities_absent_from_declarative_roles(monkeypatch):
    monkeypatch.setenv(HOP_NAME, SYNTHETIC_HOP_VALUE)
    monkeypatch.setenv("OPENCODE_API_KEY", SYNTHETIC_HOP_VALUE)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/synthetic/auth")
    monkeypatch.setenv(FLAG, FLAG_VALUE)
    authority = _authority_with_spec(_inherit_spec(FLAG))
    for role in (
        ExecutionRole.PROJECT_COMMAND,
        ExecutionRole.PRODUCT_PDB,
        ExecutionRole.VERIFIER,
        ExecutionRole.CLEANUP,
    ):
        env = authority.role_environment(role)
        assert HOP_NAME not in env
        assert "OPENCODE_API_KEY" not in env
        assert "OPENCODE_CONFIG_DIR" not in env
        assert SYNTHETIC_HOP_VALUE not in " ".join(env.values())


# ---------------------------------------------------------------------------
# G. network variable provenance
# ---------------------------------------------------------------------------


def test_proxy_not_inherited_by_default(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-proxy.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "localhost")
    authority = _authority()
    for role in _project_roles():
        env = authority.role_environment(role)
        assert "HTTPS_PROXY" not in env
        assert "NO_PROXY" not in env


def test_declared_proxy_available_to_project_roles_not_cleanup(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-proxy.invalid:8080")
    authority = _authority_with_spec(_inherit_spec("HTTPS_PROXY"))
    for role in _project_roles():
        assert (
            authority.role_environment(role)["HTTPS_PROXY"]
            == "http://synthetic-proxy.invalid:8080"
        )
    assert "HTTPS_PROXY" not in authority.role_environment(ExecutionRole.CLEANUP)


def test_provider_transport_behavior_unchanged(monkeypatch):
    """The V2-02 ingress does not alter model transport environment rules."""
    from agentic_debugger.application.model_providers import (
        provider_transport_environment,
    )

    monkeypatch.setenv(FLAG, FLAG_VALUE)
    assert provider_transport_environment("configured") is None


# ---------------------------------------------------------------------------
# Declaration validation (fail-closed ingress)
# ---------------------------------------------------------------------------


def test_control_authority_names_cannot_be_declared():
    with pytest.raises(Exception):
        _inherit_spec("AGENTIC_DEBUGGER_PROVIDER_X_API_KEY")
    with pytest.raises(Exception):
        _inherit_spec("OPENCODE_API_KEY")
    with pytest.raises(Exception):
        _secret_spec("OPENCODE_CONFIG_DIR")
    with pytest.raises(Exception):
        ProjectRuntimeEnvironmentSpec(
            values={"AGENTIC_DEBUGGER_CUSTOM": "value"}
        )


def test_credential_shaped_explicit_value_rejected():
    with pytest.raises(Exception):
        ProjectRuntimeEnvironmentSpec(
            values={"V2_02_SOME_FLAG": "api_key=synthetic-secret-value"}
        )


def test_duplicate_declaration_across_categories_rejected():
    with pytest.raises(Exception):
        ProjectRuntimeEnvironmentSpec(
            inherit=(ProjectEnvDeclaration("V2_02_DUP"),),
            secrets=(ProjectEnvDeclaration("V2_02_DUP"),),
        )


def test_invalid_names_rejected():
    for bad in ("", "HAS SPACE", "HAS-DASH", "HAS=EQUALS", "9STARTS_DIGIT"):
        with pytest.raises(Exception):
            ProjectEnvDeclaration(bad)


def test_platform_essential_cannot_be_declared(monkeypatch):
    spec = _inherit_spec("PATH")
    with pytest.raises(ExecutionEnvironmentError):
        _authority_with_spec(spec)


def test_explicit_nonsecret_value_materializes(monkeypatch):
    spec = ProjectRuntimeEnvironmentSpec(
        values={"V2_02_EXPLICIT_FLAG": "plain-configuration"}
    )
    authority = _authority_with_spec(spec)
    for role in _project_roles():
        assert (
            authority.role_environment(role)["V2_02_EXPLICIT_FLAG"]
            == "plain-configuration"
        )


def test_spec_transport_roundtrip_and_bound():
    spec = ProjectRuntimeEnvironmentSpec(
        values={"V2_02_EXPLICIT_FLAG": "plain"},
        inherit=(ProjectEnvDeclaration("V2_02_A", False),),
        secrets=(ProjectEnvDeclaration("V2_02_S", True),),
    )
    param = spec_to_param(spec)
    assert isinstance(param, str)
    restored = spec_from_param(param)
    assert restored == spec
    assert spec_from_param(None) == ProjectRuntimeEnvironmentSpec()
    assert spec_from_param("") == ProjectRuntimeEnvironmentSpec()
    with pytest.raises(Exception):
        spec_from_param("{not-json")
