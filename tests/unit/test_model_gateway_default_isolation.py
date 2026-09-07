"""Task 29 regression tests: ModelGateway.default() config_root singleton isolation.

Proves:
1. Canonical isolation:
   - ModelGateway.default() returns stable canonical process-level gateway.
   - Contextual acquisition ModelGateway.default(config_root=X) does NOT mutate
     the canonical default gateway's config_root or probe state.
   - Later calls to ModelGateway.default() still return canonical state with
     config_root=None.
2. A/B context isolation & sequential interleaving:
   - Profile resolution for store A uses profile A.
   - Subsequent acquisition and resolution of store B in between does NOT redirect A to B.
   - A preflight and transport materialization after B remains coherent with store A.
   - Store B resolves and preflights against store B.
3. Immutability:
   - config_root on gateway instances is read-only and cannot be mutated.
4. Local Project session launch isolation:
   - Consecutive / interleaved Local Project launches with distinct config roots
     do not contaminate canonical gateway or each other.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from agentic_debugger.application.command_config import (
    COMMAND_CONFIG_SCHEMA_VERSION,
    CommandModelConfigStore,
)
from agentic_debugger.application.model_gateway import (
    ModelGateway,
    ROUTE_CONFIGURED_PROFILE,
)
from agentic_debugger.application.model_providers import PROVIDER_KIND_CONFIGURED
from agentic_debugger.application.session import SessionBudgets
from agentic_debugger.application.session_runtime import (
    ProjectRuntimeEnvironmentSpec,
    build_local_project_launch,
)


def _create_command_profile_store(
    root: Path,
    *,
    profile_id: str = "test-profile",
    display_name: str = "Profile Name",
    argv: list[str] | None = None,
    tool_version: str = "live-v1",
) -> Path:
    """Create a valid command-models.json inside root/config."""
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "schema_version": COMMAND_CONFIG_SCHEMA_VERSION,
        "profiles": [
            {
                "profile_id": profile_id,
                "display_name": display_name,
                "executable": "python",
                "argv": argv or ["-c", f"print({display_name!r})"],
                "tool_version": tool_version,
                "protocol_version": "1.3",
            }
        ],
    }
    (config_dir / "command-models.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    return root


@pytest.fixture(autouse=True)
def _clean_default_gateway():
    """Ensure clean default instance before and after each test."""
    ModelGateway._default_instance = None
    yield
    ModelGateway._default_instance = None


def test_canonical_isolation_and_probe_stability(tmp_path: Path):
    """Canonical ModelGateway.default() must not inherit contextual roots or lose probe state."""
    root_a = _create_command_profile_store(
        tmp_path / "root_a", display_name="Store A Profile", tool_version="v1"
    )
    root_b = _create_command_profile_store(
        tmp_path / "root_b", display_name="Store B Profile", tool_version="v2"
    )

    # 1. Acquire canonical default gateway
    default_gateway = ModelGateway.default()
    assert default_gateway.config_root is None

    # Record probe state on canonical gateway
    default_gateway._live_probe_results["prov_test"] = {
        "verified": True,
        "timestamp": "2026-09-07T00:00:00Z",
        "error": None,
        "runtime_identity": "test-identity-hash",
    }

    # 2. Acquire contextual gateway for root A
    gw_a = ModelGateway.default(config_root=root_a)
    assert gw_a.config_root == root_a
    assert gw_a is not default_gateway

    # 3. Acquire contextual gateway for root B
    gw_b = ModelGateway.default(config_root=root_b)
    assert gw_b.config_root == root_b
    assert gw_b is not default_gateway
    assert gw_b is not gw_a

    # 4. Call ModelGateway.default() again
    canonical_after = ModelGateway.default()
    assert canonical_after is default_gateway
    # Canonical default state has NOT inherited A or B
    assert canonical_after.config_root is None

    # Probe state on canonical gateway remains intact
    assert "prov_test" in canonical_after._live_probe_results
    assert canonical_after._live_probe_results["prov_test"]["verified"] is True


def test_ab_context_isolation_and_sequential_interleaving(tmp_path: Path):
    """Sequential interleaving A resolve -> B resolve/use -> A preflight/transport.

    Proves:
    - Resolving A uses profile A.
    - Acquiring/using B in between does NOT redirect A to B.
    - Subsequent preflight and transport corroboration for A uses store A.
    - Neither A nor B pollutes canonical ModelGateway.default().
    """
    root_a = _create_command_profile_store(
        tmp_path / "store_a",
        profile_id="shared-profile-id",
        display_name="Profile in Store A",
        argv=["-c", "print('output from A')"],
        tool_version="tool-v1",
    )
    root_b = _create_command_profile_store(
        tmp_path / "store_b",
        profile_id="shared-profile-id",
        display_name="Profile in Store B",
        argv=["-c", "print('output from B')"],
        tool_version="tool-v2",
    )

    store_a = CommandModelConfigStore(root_a)
    store_b = CommandModelConfigStore(root_b)
    prof_a = store_a.get("shared-profile-id")
    prof_b = store_b.get("shared-profile-id")
    assert prof_a.configuration_fingerprint != prof_b.configuration_fingerprint
    assert prof_a.tool_version != prof_b.tool_version

    # Ensure canonical default is initialized
    g_canonical = ModelGateway.default()
    assert g_canonical.config_root is None

    # Step 1: A resolve
    gw_a = ModelGateway.default(config_root=root_a)
    binding_a = gw_a.resolve(PROVIDER_KIND_CONFIGURED, "shared-profile-id")
    assert binding_a.route == ROUTE_CONFIGURED_PROFILE
    assert binding_a.config_fingerprint == prof_a.configuration_fingerprint
    assert binding_a.tool_version == "tool-v1"

    # Step 2: B resolve and use
    gw_b = ModelGateway.default(config_root=root_b)
    binding_b = gw_b.resolve(PROVIDER_KIND_CONFIGURED, "shared-profile-id")
    assert binding_b.route == ROUTE_CONFIGURED_PROFILE
    assert binding_b.config_fingerprint == prof_b.configuration_fingerprint
    assert binding_b.tool_version == "tool-v2"

    pf_b = gw_b.static_preflight(binding_b)
    assert pf_b.is_runnable is True
    assert pf_b.blocker_reason is None
    transport_b, live_cfg_b = gw_b.create_transport(binding_b)
    assert live_cfg_b.model_name == prof_b.display_name
    assert live_cfg_b.tool_version == "tool-v2"

    # Verify canonical default was not polluted by B
    assert ModelGateway.default().config_root is None

    # Step 3: A preflight and transport materialization (after B was resolved and used!)
    # On baseline, gw_a was the mutated singleton, so it re-checked against root B
    # and failed with 'configuration drifted (stale binding)'.
    pf_a = gw_a.static_preflight(binding_a)
    assert pf_a.is_runnable is True, f"Preflight failed: {pf_a.blocker_reason}"
    assert pf_a.blocker_reason is None

    transport_a, live_cfg_a = gw_a.create_transport(binding_a)
    assert live_cfg_a.model_name == prof_a.display_name
    assert live_cfg_a.tool_version == "tool-v1"

    # Prove fingerprints and configurations remain distinct
    assert live_cfg_a.configuration_fingerprint != live_cfg_b.configuration_fingerprint


def test_ab_interleaving_corroboration_fails_on_shared_root(tmp_path: Path):
    """Direct proof: acquiring B between A's resolve and preflight must not make A stale."""
    root_a = _create_command_profile_store(
        tmp_path / "store_a",
        profile_id="p1",
        display_name="Profile A",
        tool_version="tool-v1",
    )
    root_b = _create_command_profile_store(
        tmp_path / "store_b",
        profile_id="p1",
        display_name="Profile B",
        tool_version="tool-v2",
    )

    # 1. Resolve on A
    gw_a = ModelGateway.default(config_root=root_a)
    binding_a = gw_a.resolve(PROVIDER_KIND_CONFIGURED, "p1")

    # 2. Acquire and resolve on B
    gw_b = ModelGateway.default(config_root=root_b)
    binding_b = gw_b.resolve(PROVIDER_KIND_CONFIGURED, "p1")

    # 3. Preflight and transport on gw_a: must not fail as stale against root_b
    pf_a = gw_a.static_preflight(binding_a)
    assert pf_a.is_runnable is True, f"Preflight failed spuriously: {pf_a.blocker_reason}"
    transport_a, live_cfg_a = gw_a.create_transport(binding_a)
    assert live_cfg_a.model_name == "Profile A"
    assert live_cfg_a.tool_version == "tool-v1"


def test_local_project_session_launch_isolation(tmp_path: Path):
    """Local Project session launches with distinct roots remain isolated."""
    root_a = _create_command_profile_store(
        tmp_path / "proj_a",
        profile_id="my-model",
        display_name="Project A Model",
        tool_version="v-alpha",
    )
    root_b = _create_command_profile_store(
        tmp_path / "proj_b",
        profile_id="my-model",
        display_name="Project B Model",
        tool_version="v-beta",
    )

    # Initial canonical gateway
    canonical = ModelGateway.default()
    assert canonical.config_root is None

    # Build launch for session A
    launch_a = build_local_project_launch(
        session_id="session-a",
        task_id="task-a",
        policy="pdb-on-uncertainty",
        provider_id=PROVIDER_KIND_CONFIGURED,
        model_id="my-model",
        profile_id="my-model",
        launch_snapshot={"PATH": "/usr/bin"},
        project_spec=ProjectRuntimeEnvironmentSpec(),
        budgets=SessionBudgets(max_model_calls=10),
        config_root=root_a,
    )
    assert launch_a.model_binding is not None
    assert launch_a.model_binding.tool_version == "v-alpha"

    # Canonical gateway still not polluted
    assert ModelGateway.default().config_root is None

    # Build launch for session B
    launch_b = build_local_project_launch(
        session_id="session-b",
        task_id="task-b",
        policy="pdb-on-uncertainty",
        provider_id=PROVIDER_KIND_CONFIGURED,
        model_id="my-model",
        profile_id="my-model",
        launch_snapshot={"PATH": "/usr/bin"},
        project_spec=ProjectRuntimeEnvironmentSpec(),
        budgets=SessionBudgets(max_model_calls=20),
        config_root=root_b,
    )
    assert launch_b.model_binding is not None
    assert launch_b.model_binding.tool_version == "v-beta"

    # Canonical gateway still not polluted
    assert ModelGateway.default().config_root is None

    # Transport materialization for A in a contextual gateway for A
    gw_a = ModelGateway.default(config_root=root_a)
    transport_a, cfg_a = gw_a.create_transport(launch_a.model_binding)
    assert cfg_a.tool_version == "v-alpha"

    # Transport materialization for B in a contextual gateway for B
    gw_b = ModelGateway.default(config_root=root_b)
    transport_b, cfg_b = gw_b.create_transport(launch_b.model_binding)
    assert cfg_b.tool_version == "v-beta"

    # Final check: canonical gateway remains completely unpolluted
    assert ModelGateway.default().config_root is None


def test_config_root_is_immutable_property(tmp_path: Path):
    """config_root must not be reassignable on default or contextual gateways."""
    g_def = ModelGateway.default()
    with pytest.raises(AttributeError):
        g_def.config_root = tmp_path  # type: ignore[misc]

    g_ctx = ModelGateway.default(config_root=tmp_path)
    with pytest.raises(AttributeError):
        g_ctx.config_root = None  # type: ignore[misc]
