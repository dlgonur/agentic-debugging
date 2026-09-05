"""V2-02/09 single session-start authority acceptance tests.

Proves the repair finding: once a ``SessionLaunch`` exists, product
runtime consumers use it for every session-start fact it owns.

1. ``SessionLaunch`` cannot represent contradictory agent vs
   provider/model/policy identities (single copy, read-only views).
2-6. A supplied ``ctx.session_launch`` contradicting a mirrored scenario
   param (provider, model_id, policy, profile_id, project spec) fails
   closed before model execution.
7. With matching params, downstream selection comes from the launch and
   behavior is unchanged.
8. The source never rebuilds capabilities/environment when a launch is
   supplied.

No live provider, no real credentials — synthetic values only.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from agentic_debugger.application.session_runtime import (
    AgentDefinition,
    ProjectEnvDeclaration,
    ProjectRuntimeEnvironmentSpec,
    SessionCapability,
    SessionLaunch,
    build_local_project_launch,
    check_launch_matches_params,
    spec_to_param,
)
from agentic_debugger.application.worker_scenarios import (
    ScenarioContext,
    ScenarioInputError,
)
from agentic_debugger.application.model_gateway import ModelBinding
from agentic_debugger.cancellation import CancellationToken
from agentic_debugger.application.events import SourceKind

HEAD = "a" * 40


def _base_params(**overrides):
    params = {
        "project_repo_path": "C:/repo",
        "project_head": HEAD,
        "isolated_workspace": "C:/iso",
        "bug_description": "a bug",
        "config_root": "C:/cfg",
        "profile_id": "test-profile",
    }
    params.update(overrides)
    return params


def _launch(**overrides):
    fields = {
        "session_id": "sess-authority-001",
        "task_id": "local-project-debug",
        "policy": "pdb-on-uncertainty",
        "provider_id": None,
        "model_id": None,
        "profile_id": "test-profile",
        "launch_snapshot": {"PATH": "/usr/bin"},
        "project_spec": ProjectRuntimeEnvironmentSpec(),
    }
    fields.update(overrides)
    return build_local_project_launch(**fields)


def _mismatch_case(tmp_path, monkeypatch, *, launch_kwargs, param_overrides):
    from agentic_debugger.application.local_project_source import (
        run_local_project_session,
    )

    iso = tmp_path / "iso"
    iso.mkdir(exist_ok=True)
    launch = _launch(**launch_kwargs)
    ctx = ScenarioContext(
        work_dir=tmp_path,
        token=CancellationToken(),
        emitter=_Emitter(),
        run_id="run-authority",
        session_launch=launch,
    )
    params = _base_params(
        project_repo_path=str(tmp_path),
        isolated_workspace=str(iso),
        **param_overrides,
    )
    calls = _bomb_monkeypatch(monkeypatch)
    with pytest.raises(ScenarioInputError) as excinfo:
        run_local_project_session(ctx, params)
    assert "does not match" in str(excinfo.value)
    assert calls["count"] == 0
    return excinfo


class _Emitter:
    session_id = "sess-authority-001"
    task_id = "local-project-debug"
    source_kind = SourceKind.LOCAL_PROJECT

    def emit(self, kind, payload):
        return None


def _bomb_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Fail the test if model/provider resolution is ever attempted."""
    from agentic_debugger.application import model_providers
    from agentic_debugger.application import command_config

    calls: dict = {"count": 0}

    def _bomb(*args, **kwargs):
        calls["count"] += 1
        raise AssertionError("model/provider resolution must not run")

    monkeypatch.setattr(
        model_providers, "resolve_provider_live_config", _bomb
    )
    monkeypatch.setattr(command_config.CommandModelConfigStore, "get", _bomb)
    return calls


# ---------------------------------------------------------------------------
# 1. contradictory identities are unrepresentable
# ---------------------------------------------------------------------------


def test_launch_has_single_copy_of_request_identities():
    binding = ModelBinding(
        provider_id="commandcode_goat",
        model_id="goat-1",
        provider_model_id="goat-1",
        display_name="goat-1",
        route="direct_api",
        effective_protocol="chat_completions",
        endpoint_contract="generic",
        endpoint="http://127.0.0.1:8000",
        auth_mode=None,
        config_fingerprint=None,
        tool_version="1.0",
    )
    launch = _launch(provider_id="commandcode_goat", model_id="goat-1", model_binding=binding)
    assert launch.provider_id == "commandcode_goat" == launch.agent.provider_id
    assert launch.model_id == "goat-1" == launch.agent.model_id
    assert launch.policy == "pdb-on-uncertainty" == launch.agent.controller_policy
    # The duplicates are gone from the constructor: passing them is a
    # TypeError, so no public construction state can contradict the agent.
    params = set(inspect.signature(SessionLaunch).parameters)
    assert "provider_id" not in params
    assert "model_id" not in params
    assert "policy" not in params
    with pytest.raises(TypeError):
        SessionLaunch(
            session_id="sess-authority-001",
            task_id="local-project-debug",
            agent=launch.agent,
            execution_environment=launch.execution_environment,
            project_spec=launch.project_spec,
            capabilities=launch.capabilities,
            profile_id="test-profile",
            budgets=launch.budgets,
            provider_id="other",
        )


def test_launch_identity_views_are_read_only():
    launch = _launch()
    with pytest.raises(Exception):
        launch.agent = AgentDefinition(controller_policy="static-baseline")  # type: ignore[misc]
    # Serialization still carries the same provenance keys, derived from
    # the canonical agent — never an independent copy.
    mapping = launch.to_mapping()
    assert mapping["provider_id"] == launch.agent.provider_id
    assert mapping["model_id"] == launch.agent.model_id
    assert mapping["policy"] == launch.agent.controller_policy


def test_corrobation_accepts_matching_params():
    launch = _launch()
    check_launch_matches_params(
        launch,
        policy="pdb-on-uncertainty",
        provider_id=None,
        model_id=None,
        profile_id="test-profile",
        project_spec=ProjectRuntimeEnvironmentSpec(),
    )


# ---------------------------------------------------------------------------
# 2-6. mirrored-param contradictions fail closed before model execution
# ---------------------------------------------------------------------------


def test_mismatched_provider_fails_closed(tmp_path, monkeypatch):
    _mismatch_case(
        tmp_path,
        monkeypatch,
        launch_kwargs={"provider_id": "configured", "model_id": None},
        param_overrides={"provider": "commandcode_goat", "model_id": "goat-1"},
    )


def test_mismatched_model_fails_closed(tmp_path, monkeypatch):
    _mismatch_case(
        tmp_path,
        monkeypatch,
        launch_kwargs={},
        param_overrides={"model_id": "some-model"},
    )


def test_mismatched_policy_fails_closed(tmp_path, monkeypatch):
    _mismatch_case(
        tmp_path,
        monkeypatch,
        launch_kwargs={},
        param_overrides={"policy": "static-baseline"},
    )


def test_mismatched_profile_fails_closed(tmp_path, monkeypatch):
    _mismatch_case(
        tmp_path,
        monkeypatch,
        launch_kwargs={},
        param_overrides={"profile_id": "other-profile"},
    )


def test_mismatched_project_spec_fails_closed(tmp_path, monkeypatch):
    spec = ProjectRuntimeEnvironmentSpec(
        inherit=(ProjectEnvDeclaration("V2_09_DECLARED_FLAG"),)
    )
    excinfo = _mismatch_case(
        tmp_path,
        monkeypatch,
        launch_kwargs={},
        param_overrides={"project_runtime_spec": spec_to_param(spec)},
    )
    assert "project runtime spec" in str(excinfo.value)


def test_mismatch_error_carries_no_values(tmp_path, monkeypatch):
    secret_value = "synthetic-v209-secret-not-a-real-credential"
    spec = ProjectRuntimeEnvironmentSpec(
        secrets=(ProjectEnvDeclaration("V2_09_SECRET_NAME"),)
    )
    launch = _launch(
        project_spec=spec,
        launch_snapshot={"PATH": "/usr/bin", "V2_09_SECRET_NAME": secret_value},
    )
    # Materialized secret values live only in the launch memory; the
    # mismatch path must never render them.
    excinfo = _mismatch_case(
        tmp_path,
        monkeypatch,
        launch_kwargs={"project_spec": ProjectRuntimeEnvironmentSpec()},
        param_overrides={"project_runtime_spec": spec_to_param(spec)},
    )
    assert secret_value not in str(excinfo.value)
    assert secret_value not in json.dumps(launch.to_mapping(), sort_keys=True)


# ---------------------------------------------------------------------------
# 7. matching params: launch-sourced selection, unchanged behavior
# ---------------------------------------------------------------------------


def test_matching_launch_drives_model_selection(tmp_path, monkeypatch):
    from agentic_debugger.application import command_config
    from agentic_debugger.application import local_project_source
    from agentic_debugger.application.events import SessionEventKind

    seen: dict = {}

    class _FakeProfile:
        profile_id = "test-profile"
        display_name = "Test Dummy"
        protocol_version = "1.3"
        tool_version = "live-command-v1"
        request_timeout_seconds = 5.0
        cwd = None
        environment = None
        configuration_fingerprint = "f" * 64

        def live_command(self):
            return ("python", "-c", "pass")

    def _fake_get(self, profile_id):
        seen["profile_id"] = profile_id
        return _FakeProfile()

    monkeypatch.setattr(command_config.CommandModelConfigStore, "get", _fake_get)
    monkeypatch.setattr(
        local_project_source,
        "_inventory_tracked_python_files",
        lambda _isolated, **_kwargs: ["sample.py"],
    )

    class _NoopObservability:
        def diagnosis_recorded(self, **_kwargs):
            pass

        def source_snapshot(self, _snapshot):
            pass

    monkeypatch.setattr(
        local_project_source,
        "SessionObservability",
        lambda *_args, **_kwargs: _NoopObservability(),
    )

    class _StopAfterProvenance(RuntimeError):
        pass

    class _ProvenanceEmitter(_Emitter):
        def emit(self, kind, payload):
            if kind is SessionEventKind.MODEL_CONFIGURED:
                seen["provenance"] = dict(payload)
                raise _StopAfterProvenance
            return None

    iso = tmp_path / "iso"
    iso.mkdir(exist_ok=True)
    (iso / "sample.py").write_text("value = 1\n", encoding="utf-8")
    launch = _launch(provider_id="configured")
    ctx = ScenarioContext(
        work_dir=tmp_path,
        token=CancellationToken(),
        emitter=_ProvenanceEmitter(),
        run_id="run-authority",
        session_launch=launch,
    )
    params = _base_params(
        project_repo_path=str(tmp_path),
        isolated_workspace=str(iso),
        provider="configured",
    )
    with pytest.raises(_StopAfterProvenance):
        local_project_source.run_local_project_session(ctx, params)
    # The profile lookup used the launch-owned profile identity, and the
    # durable provenance matches the launch-owned agent identity.
    assert seen["profile_id"] == launch.profile_id == "test-profile"
    assert seen["provenance"]["provider"] == "configured"
    assert seen["provenance"]["profile_id"] == "test-profile"


# ---------------------------------------------------------------------------
# 8. supplied launch is never rebuilt (capabilities not recomputed)
# ---------------------------------------------------------------------------


def test_supplied_launch_is_consumed_without_rebuild(tmp_path, monkeypatch):
    from agentic_debugger.application import local_project_source
    from agentic_debugger.application import session_runtime
    from agentic_debugger.application.events import SessionEventKind

    monkeypatch.setattr(
        local_project_source,
        "_inventory_tracked_python_files",
        lambda _isolated, **_kwargs: ["sample.py"],
    )

    class _NoopObservability:
        def diagnosis_recorded(self, **_kwargs):
            pass

        def source_snapshot(self, _snapshot):
            pass

    monkeypatch.setattr(
        local_project_source,
        "SessionObservability",
        lambda *_args, **_kwargs: _NoopObservability(),
    )

    class _StopAfterProvenance(RuntimeError):
        pass

    class _ProvenanceEmitter(_Emitter):
        def emit(self, kind, payload):
            if kind is SessionEventKind.MODEL_CONFIGURED:
                raise _StopAfterProvenance
            return None

    iso = tmp_path / "iso2"
    iso.mkdir(exist_ok=True)
    (iso / "sample.py").write_text("value = 1\n", encoding="utf-8")
    # The launch is built BEFORE the rebuild bombs are planted: the
    # factory legitimately runs once here (as the worker would), and must
    # never run again once the source consumes the supplied launch.
    # provider "configured" matches the params below (corroboration).
    launch = _launch(provider_id="configured")

    def _rebuild_bomb(*args, **kwargs):
        raise AssertionError("launch must not be rebuilt when supplied")

    monkeypatch.setattr(
        session_runtime, "build_local_project_launch", _rebuild_bomb
    )
    monkeypatch.setattr(
        session_runtime, "compute_effective_capabilities", _rebuild_bomb
    )
    ctx = ScenarioContext(
        work_dir=tmp_path,
        token=CancellationToken(),
        emitter=_ProvenanceEmitter(),
        run_id="run-authority",
        session_launch=launch,
    )
    # Configured-provider path needs a profile; the Transportes stop right
    # after provenance, so a minimal stub store is enough.
    from agentic_debugger.application import command_config

    class _FakeProfile:
        profile_id = "test-profile"
        display_name = "Test Dummy"
        protocol_version = "1.3"
        tool_version = "live-command-v1"
        request_timeout_seconds = 5.0
        cwd = None
        environment = None
        configuration_fingerprint = "f" * 64

        def live_command(self):
            return ("python", "-c", "pass")

    monkeypatch.setattr(
        command_config.CommandModelConfigStore,
        "get",
        lambda self, profile_id: _FakeProfile(),
    )
    params = _base_params(
        project_repo_path=str(tmp_path),
        isolated_workspace=str(iso),
        provider="configured",
    )
    with pytest.raises(_StopAfterProvenance):
        local_project_source.run_local_project_session(ctx, params)
    # Reaching MODEL_CONFIGURED without touching the factory proves the
    # supplied launch — with its one-time capabilities — was authoritative.


def test_capability_result_identity_flows_to_executor(tmp_path):
    """The executor consumes the launch's capability object, not a copy."""
    from agentic_debugger.application.executor import ProductExecutor

    launch = _launch()
    executor = ProductExecutor(
        execution_environment=launch.execution_environment,
        capabilities=launch.capabilities,
    )
    assert executor.capabilities is launch.capabilities
    assert SessionCapability.PROJECT_COMMAND in executor.capabilities.capabilities
