"""V2-02 Session/AgentDefinition/EffectiveSessionCapabilities acceptance tests.

Proves the explicit session authority:

A. requested + available produces the expected intersection;
B. requested-but-unavailable required capability fails closed;
C. available-but-not-requested is never silently enabled;
D. the result is computed once and session-stable;
E. a real Local Project behavior consumes the result (PDB gate);
F. capability/provenance identity serializes safely without secrets.
"""

from __future__ import annotations

import json
import os

import pytest

from agentic_debugger.application.execution_environment import ExecutionEnvironment
from agentic_debugger.application.session_runtime import (
    AgentDefinition,
    CapabilityUnavailableError,
    EffectiveSessionCapabilities,
    ProjectRuntimeEnvironmentSpec,
    SessionCapability,
    SessionLaunch,
    build_local_project_launch,
    compute_effective_capabilities,
    task_allowed_capabilities,
)


def _agent(**overrides) -> AgentDefinition:
    fields = {
        "controller_policy": "pdb-on-uncertainty",
        "provider_id": None,
        "model_id": None,
    }
    fields.update(overrides)
    return AgentDefinition(**fields)


def _available(*names: str) -> frozenset:
    return frozenset(SessionCapability(name) for name in names)


# ---------------------------------------------------------------------------
# A. intersection semantics
# ---------------------------------------------------------------------------


def test_full_request_and_availability_grants_everything():
    capabilities = compute_effective_capabilities(
        requested=set(SessionCapability),
        available=set(SessionCapability),
        task_allowed=set(SessionCapability),
    )
    for capability in SessionCapability:
        assert capabilities.has(capability)


def test_launch_computes_all_capabilities_for_default_local_project():
    launch = build_local_project_launch(
        session_id="sess-v202-cap-default",
        task_id="local-project-debug",
        policy="pdb-on-uncertainty",
        provider_id=None,
        model_id=None,
        profile_id="dummy-profile",
        launch_snapshot={"PATH": "/usr/bin"},
        project_spec=ProjectRuntimeEnvironmentSpec(),
    )
    assert type(launch) is SessionLaunch
    for capability in SessionCapability:
        assert launch.capabilities.has(capability)
    # The launch authority is declarative: the normal product path never
    # took the retired bridge.
    assert not launch.execution_environment.uses_legacy_bridge
    # SessionSpec remains the serialized compatibility representation;
    # SessionLaunch is the authoritative in-process binding.
    assert launch.task_id == "local-project-debug"
    assert launch.profile_id == "dummy-profile"


def test_task_policy_denies_pdb_for_static_baseline():
    allowed = task_allowed_capabilities("static-baseline")
    assert SessionCapability.PDB not in allowed
    assert SessionCapability.PROJECT_COMMAND in allowed
    launch = build_local_project_launch(
        session_id="sess-v202-cap-static",
        task_id="local-project-debug",
        policy="static-baseline",
        provider_id=None,
        model_id=None,
        profile_id="dummy-profile",
        launch_snapshot={"PATH": "/usr/bin"},
        project_spec=ProjectRuntimeEnvironmentSpec(),
    )
    assert not launch.capabilities.has(SessionCapability.PDB)
    assert launch.capabilities.has(SessionCapability.PROJECT_COMMAND)
    assert launch.capabilities.has(SessionCapability.PATCH)
    assert launch.capabilities.has(SessionCapability.VERIFIER)


# ---------------------------------------------------------------------------
# B. requested-but-unavailable fails closed
# ---------------------------------------------------------------------------


def test_requested_but_unavailable_capability_is_denied():
    capabilities = compute_effective_capabilities(
        requested={SessionCapability.PDB},
        available={SessionCapability.PROJECT_COMMAND},
        task_allowed=set(SessionCapability),
    )
    assert not capabilities.has(SessionCapability.PDB)
    with pytest.raises(CapabilityUnavailableError) as excinfo:
        capabilities.require(SessionCapability.PDB)
    assert "pdb" in str(excinfo.value)
    assert "unavailable" in str(excinfo.value)


def test_require_rejects_unknown_capability_type():
    capabilities = EffectiveSessionCapabilities(
        capabilities=frozenset({SessionCapability.PDB})
    )
    with pytest.raises(Exception):
        capabilities.has("pdb")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# C. available-but-not-requested is never silently enabled
# ---------------------------------------------------------------------------


def test_available_but_not_requested_stays_disabled():
    agent = _agent(
        allowed_capabilities=frozenset({SessionCapability.PROJECT_COMMAND})
    )
    environment = ExecutionEnvironment.for_local_project(
        {"PATH": "/usr/bin"}, ProjectRuntimeEnvironmentSpec()
    )
    capabilities = compute_effective_capabilities(
        requested=agent.allowed_capabilities,
        available=environment.available_capabilities,
        task_allowed=task_allowed_capabilities(agent.controller_policy),
    )
    assert capabilities.has(SessionCapability.PROJECT_COMMAND)
    assert not capabilities.has(SessionCapability.PDB)
    assert not capabilities.has(SessionCapability.PATCH)
    assert not capabilities.has(SessionCapability.VERIFIER)


def test_machine_support_alone_enables_nothing():
    capabilities = compute_effective_capabilities(
        requested=frozenset(),
        available=set(SessionCapability),
        task_allowed=set(SessionCapability),
    )
    # Empty request is itself invalid input (fail closed at the definition),
    # and an empty intersection grants nothing either way.
    assert not capabilities.has(SessionCapability.PDB)


def test_empty_allowed_capabilities_rejected():
    with pytest.raises(Exception):
        _agent(allowed_capabilities=frozenset())


# ---------------------------------------------------------------------------
# D. computed once, session-stable
# ---------------------------------------------------------------------------


def test_capabilities_are_frozen_and_stable():
    capabilities = compute_effective_capabilities(
        requested=set(SessionCapability),
        available=set(SessionCapability),
        task_allowed=set(SessionCapability),
    )
    with pytest.raises(Exception):
        capabilities.capabilities = frozenset()  # type: ignore[misc]
    first = capabilities.to_mapping()
    assert capabilities.to_mapping() == first
    assert capabilities.fingerprint() == capabilities.fingerprint()


def test_launch_authority_is_immutable():
    launch = build_local_project_launch(
        session_id="sess-v202-cap-frozen",
        task_id="local-project-debug",
        policy="pdb-on-uncertainty",
        provider_id=None,
        model_id=None,
        profile_id="dummy-profile",
        launch_snapshot={"PATH": "/usr/bin"},
        project_spec=ProjectRuntimeEnvironmentSpec(),
    )
    with pytest.raises(Exception):
        launch.policy = "static-baseline"  # type: ignore[misc]
    with pytest.raises(Exception):
        launch.agent = _agent()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# E. real behavior consumes the result (PDB gate)
# ---------------------------------------------------------------------------


def test_pdb_denied_without_capability_through_executor(tmp_path):
    from agentic_debugger.agent.tool_registry import ToolRejectedError
    from agentic_debugger.application.executor import ProductExecutor
    from agentic_debugger.runtime.workspace import TaskWorkspace

    launch = build_local_project_launch(
        session_id="sess-v202-cap-pdb-denied",
        task_id="local-project-debug",
        policy="static-baseline",
        provider_id=None,
        model_id=None,
        profile_id="dummy-profile",
        launch_snapshot=dict(os.environ),
        project_spec=ProjectRuntimeEnvironmentSpec(),
    )
    executor = ProductExecutor(
        execution_environment=launch.execution_environment,
        capabilities=launch.capabilities,
    )
    workspace = TaskWorkspace(str(tmp_path))
    with pytest.raises(ToolRejectedError) as excinfo:
        executor.open_product_pdb(workspace)
    assert "pdb" in str(excinfo.value).lower()


def test_pdb_granted_with_capability_builds_explicit_session(tmp_path):
    from agentic_debugger.application.executor import ProductExecutor
    from agentic_debugger.runtime.pdb_session import PdbSession
    from agentic_debugger.runtime.workspace import TaskWorkspace

    launch = build_local_project_launch(
        session_id="sess-v202-cap-pdb-ok",
        task_id="local-project-debug",
        policy="pdb-on-uncertainty",
        provider_id=None,
        model_id=None,
        profile_id="dummy-profile",
        launch_snapshot=dict(os.environ),
        project_spec=ProjectRuntimeEnvironmentSpec(),
    )
    executor = ProductExecutor(
        execution_environment=launch.execution_environment,
        capabilities=launch.capabilities,
    )
    workspace = TaskWorkspace(str(tmp_path))
    session = executor.open_product_pdb(workspace)
    assert isinstance(session, PdbSession)
    # Unstarted: no process created by the seam itself.
    assert session._proc is None


def test_source_registry_enforces_pdb_capability(tmp_path):
    """The real tool registry path rejects PDB when the session denies it."""
    from agentic_debugger.agent.controller_policy import ControllerState
    from agentic_debugger.agent.tool_registry import ToolRejectedError
    from agentic_debugger.application.executor import ProductExecutor
    from agentic_debugger.application.execution_environment import ExecutionRole
    from agentic_debugger.application.local_project_source import (
        LocalProjectTask,
        _build_local_registry,
        _LocalToolContext,
    )
    from agentic_debugger.events.schema import Action

    launch = build_local_project_launch(
        session_id="sess-v202-cap-reg",
        task_id="local-project-debug",
        policy="static-baseline",
        provider_id=None,
        model_id=None,
        profile_id="dummy-profile",
        launch_snapshot=dict(os.environ),
        project_spec=ProjectRuntimeEnvironmentSpec(),
    )
    executor = ProductExecutor(
        execution_environment=launch.execution_environment,
        capabilities=launch.capabilities,
    )
    context = _LocalToolContext(
        isolated=tmp_path,
        tracked=[],
        task=LocalProjectTask(bug_description="bug"),
        probe=None,
        observability=None,
        command_environment=dict(
            launch.execution_environment.role_environment(
                ExecutionRole.PROJECT_COMMAND
            )
        ),
        pdb_worker_environment=None,
        executor=executor,
        capabilities=launch.capabilities,
    )
    from agentic_debugger.agent.controller_policy import PdbPolicy

    # PdbPolicy.ALLOWS (ON_UNCERTAINTY) here isolates the session
    # capability as the denial cause: the computed static-baseline
    # authority denies PDB even though the task policy would allow it.
    registry = _build_local_registry(context, pdb_policy=PdbPolicy.ON_UNCERTAINTY)
    action = Action(
        action_id="action-000000001",
        run_id="run-cap",
        task_id="local-project-debug",
        state=ControllerState.REPRODUCE,
        name="start_pdb_session",
        arguments={},
    )
    from agentic_debugger.events.schema import ObservationStatus

    observation = registry.dispatch(action, observation_id="observation-000000001")
    assert observation.status is ObservationStatus.REJECTED


# ---------------------------------------------------------------------------
# F. safe serialization
# ---------------------------------------------------------------------------


def test_capability_identity_serializes_without_secrets():
    capabilities = EffectiveSessionCapabilities(
        capabilities=frozenset(
            {SessionCapability.PROJECT_COMMAND, SessionCapability.PDB}
        )
    )
    mapping = capabilities.to_mapping()
    assert mapping["version"] == "session-capabilities/v1"
    assert json.dumps(mapping)
    restored = EffectiveSessionCapabilities.from_mapping(mapping)
    assert restored == capabilities


def test_agent_definition_excludes_runtime_facts():
    agent = _agent(provider_id="commandcode_goat", model_id="goat-1")
    mapping = agent.to_mapping()
    for forbidden in (
        "route",
        "protocol",
        "endpoint",
        "transport_profile",
        "credential",
        "catalog",
        "status",
        "treatment",
    ):
        assert forbidden not in json.dumps(mapping)
    assert mapping["provider_id"] == "commandcode_goat"
    assert mapping["model_id"] == "goat-1"
    assert AgentDefinition.from_mapping(mapping) == agent


def test_launch_provenance_is_safe_and_stable():
    from agentic_debugger.application.session_runtime import ProjectEnvDeclaration

    secret_value = "synthetic-v202-launch-secret-not-a-real-credential"
    launch = build_local_project_launch(
        session_id="sess-v202-cap-prov",
        task_id="local-project-debug",
        policy="pdb-on-uncertainty",
        provider_id="commandcode_goat",
        model_id="goat-1",
        profile_id="goat-1",
        launch_snapshot={
            "PATH": "/usr/bin",
            "V2_02_LAUNCH_SECRET": secret_value,
        },
        project_spec=ProjectRuntimeEnvironmentSpec(
            secrets=(ProjectEnvDeclaration("V2_02_LAUNCH_SECRET"),)
        ),
    )
    mapping = launch.to_mapping()
    text = json.dumps(mapping, sort_keys=True)
    # The safe binding NAME is provenance; the VALUE must never appear.
    assert "V2_02_LAUNCH_SECRET" in text
    assert secret_value not in text
    for forbidden in ("transport_profile", "credential"):
        assert forbidden not in text
    assert launch.fingerprint() == launch.fingerprint()
