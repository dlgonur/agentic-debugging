"""V2-04 product-path integration: the vault-resolved provider credential
reaches the authorized provider adapter child and NOTHING else.

Deterministic product chain, loopback only (no real provider contact):

    provider config setup (product write path)
      → CredentialVault binding
      → SessionLaunch (build_local_project_launch)
      → ModelBinding (ModelGateway)
      → credential lease resolved ONCE (create_transport)
      → real adapter child subprocess against a loopback fake endpoint

Proofs:
- the authorized provider adapter child receives the synthetic credential
  (Authorization header at the fake endpoint), and keeps receiving the
  LEASE-RESOLVED value even after the durable slot is overwritten
  (session-stable secret authority);
- a Local Project child execution derived from the same session authority
  never sees the credential channel variable;
- the durable journal and the ``model.configured`` payload never contain
  either credential value;
- Provider Manager status facts (via ModelGateway) report ``Credential
  ready`` truthfully through the vault.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))

from fake_provider_server import FakeProviderServer, scripted_chat_completion  # noqa: E402

from agentic_debugger.application import provider_connections as pc  # noqa: E402
from agentic_debugger.application.credential_vault import (  # noqa: E402
    CredentialVault,
)
from agentic_debugger.application.events import (  # noqa: E402
    SessionEventKind,
    SourceKind,
)
from agentic_debugger.application.journal import SessionEventJournal  # noqa: E402
from agentic_debugger.application.emitter import SessionEventEmitter  # noqa: E402
from agentic_debugger.application.model_gateway import (  # noqa: E402
    ModelGateway,
    provider_runtime_identity,
)
from agentic_debugger.application.session import SourceKind  # noqa: E402
from agentic_debugger.application.execution_environment import (  # noqa: E402
    ExecutionEnvironment,
    ExecutionRole,
)
from agentic_debugger.application.session_runtime import (  # noqa: E402
    ProjectRuntimeEnvironmentSpec,
    build_local_project_launch,
)

SECRET_A = "v204-product-credential-alpha-not-real"
SECRET_B = "v204-product-credential-beta-not-real"
DIRECTIVE = '{"kind": "transition", "target_state": "Understand", "reason": "r"}'


@pytest.fixture(autouse=True)
def _hermetic_product_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(tmp_path / "c.json")
    )
    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(tmp_path / "q.json")
    )
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path / "config-dir"))
    monkeypatch.setattr(pc, "catalog_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: tmp_path / "missing-auth.json")
    store: dict[str, str] = {}
    monkeypatch.setattr(pc, "save_secure_credential", lambda k, v: store.__setitem__(k, v) or True)
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in store)
    monkeypatch.setattr(pc, "delete_secure_credential", lambda k: store.pop(k, None) is not None)
    for var in (
        "COMMAND_CODE_API_KEY",
        "OPENCODE_API_KEY",
        "OLLAMA_API_KEY",
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    pc.clear_all_session_keys()
    yield store
    pc.clear_all_session_keys()


def test_vault_credential_reaches_provider_child_and_not_project_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_product_vault: dict,
) -> None:
    # 1. Product write path: provider config setup stores the credential
    #    through the vault-owned atomic save; durable config stays clean.
    provider_id = "v204_product_gateway"
    with FakeProviderServer(lambda request: (200, scripted_chat_completion(DIRECTIVE))) as server:
        pc.add_provider_config(
            name="V2-04 Product Gateway",
            base_url=server.base_url,
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id=provider_id,
            api_key=SECRET_A,
        )
        pc.add_manual_model(provider_id, "v204-model-x", "V2-04 Model X")
        config_file = Path(os.environ["AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH"])
        assert SECRET_A not in config_file.read_text(encoding="utf-8")
        assert SECRET_B not in config_file.read_text(encoding="utf-8")

        # 2. Status facts: Credential ready reported truthfully via the vault.
        gateway = ModelGateway()
        assert gateway.credential_readiness(provider_id).credential_ready is True
        status = gateway.get_provider_status(provider_id)
        assert status.credential_ready is True
        assert status.credential_source == pc.CREDENTIAL_SOURCE_SAVED

        # 3. SessionLaunch → ModelBinding (safe provenance only).
        launch = build_local_project_launch(
            session_id="sess-v204-product",
            task_id="local-project-debug",
            policy="static-baseline",
            provider_id=provider_id,
            model_id="v204-model-x",
            profile_id="v204-model-x",
            launch_snapshot=dict(os.environ),
            project_spec=ProjectRuntimeEnvironmentSpec(),
        )
        binding = launch.model_binding
        assert binding.route == "direct_api"
        payload = json.dumps(binding.model_configured_payload())
        assert SECRET_A not in payload and SECRET_B not in payload

        # 4. The transport resolves the vault lease ONCE and fixes the
        #    model-channel child environment for the session.
        transport, _live_config = gateway.create_transport(binding)
        transport_env = getattr(transport, "_environment")
        channel = "AGENTIC_DEBUGGER_PROVIDER_V204_PRODUCT_GATEWAY_API_KEY"
        assert transport_env == {channel: SECRET_A}

        # 5. The durable slot is overwritten after lease resolution: the
        #    authorized provider child keeps receiving the LEASE value.
        store = _hermetic_product_vault
        store[provider_id] = SECRET_B
        request = {
            "protocol": {"version": "1.3", "logical_model_call_index": 0},
            "context": {"task_id": "t", "state": "UNDERSTAND"},
        }
        result = transport.request(request, timeout_seconds=30.0)
        assert result["directive_content"] == DIRECTIVE
        assert len(server.requests) == 1
        assert server.requests[0]["authorization"] == f"Bearer {SECRET_A}"
        # A new session resolves the new durable value (secret authority
        # does not drift beneath NEW bindings either).
        assert CredentialVault.default().resolve_lease(provider_id).reveal() == SECRET_B

    # 6. Local Project child execution never sees the credential channel.
    #    The worker's launch snapshot legitimately contains the private hop
    #    variable (the UI→worker hop), yet the project role derivation
    #    structurally excludes every provider credential channel.
    hop = CredentialVault.default().session_forwarding_environment(provider_id)
    assert hop == {
        channel: SECRET_B,
        pc.provider_session_credential_authority_variable(
            provider_id
        ): provider_runtime_identity(pc.get_provider_config(provider_id)),
    }
    snapshot = dict(os.environ)
    snapshot.update(hop)
    environment = ExecutionEnvironment.for_local_project(
        snapshot, ProjectRuntimeEnvironmentSpec()
    )
    authority_var = pc.provider_session_credential_authority_variable(provider_id)
    for role in (ExecutionRole.PROJECT_COMMAND, ExecutionRole.PRODUCT_PDB, ExecutionRole.VERIFIER):
        assert channel not in environment.role_environment(role)
        assert authority_var not in environment.role_environment(role)

    # 7. The durable journal never contains either credential value.
    journal_path = tmp_path / "session" / "session.events.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal = SessionEventJournal(
        journal_path,
        session_id="sess-v204-product",
        task_id="local-project-debug",
        source_kind=SourceKind.LOCAL_PROJECT,
    )
    emitter = SessionEventEmitter(
        session_id="sess-v204-product",
        task_id="local-project-debug",
        source_kind=SourceKind.LOCAL_PROJECT,
        sink=journal,
    )
    emitter.bind_run_id("run-v204")
    # The journal schema's known model.configured fields (the accepted
    # provenance subset).  NOTE: ModelBinding.model_configured_payload()
    # additionally emits fields the current event schema rejects -- a
    # pre-existing V2-03 inconsistency on the baseline (reported, not
    # widened into V2-04).
    configured_payload = {
        key: value
        for key, value in binding.model_configured_payload().items()
        if key
        in {
            "profile_id",
            "config_fingerprint",
            "display_name",
            "protocol_version",
            "tool_version",
            "provider",
            "route",
            "api_protocol",
            "auth_mode",
            "provider_model_id",
            "endpoint",
        }
    }
    emitter.emit(SessionEventKind.MODEL_CONFIGURED, configured_payload)
    emitter.emit(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"})
    journal_text = journal_path.read_text(encoding="utf-8")
    assert SECRET_A not in journal_text
    assert SECRET_B not in journal_text
    assert channel not in journal_text

def test_session_credential_authority_fixed_at_launch_survives_slot_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_product_vault: dict,
) -> None:
    """F2 (repair 21): the SESSION credential authority is fixed at the
    authoritative SessionLaunch boundary.

    Reproduces the Candidate-20 product gap end-to-end: with the
    UI-issued session channel committed to SECRET_A at launch, replacing
    the durable slot with SECRET_B BEFORE transport creation must NOT
    switch the running session — the fake endpoint still receives
    SECRET_A.  A NEW session (no issued channel) resolves SECRET_B.
    """

    provider_id = "v204_lifecycle_gateway"
    channel = "AGENTIC_DEBUGGER_PROVIDER_V204_LIFECYCLE_GATEWAY_API_KEY"
    with FakeProviderServer(lambda request: (200, scripted_chat_completion(DIRECTIVE))) as server:
        pc.add_provider_config(
            name="V2-04 Lifecycle Gateway",
            base_url=server.base_url,
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id=provider_id,
            api_key=SECRET_A,
        )
        pc.add_manual_model(provider_id, "v204-lifecycle-x", "V2-04 Lifecycle X")

        # The real worker receives the UI-issued hop (channel VALUE plus
        # the safe issuance-authority companion) at spawn; simulate it.
        from agentic_debugger.application.model_gateway import (
            provider_runtime_identity,
        )

        monkeypatch.setenv(
            pc.provider_session_credential_authority_variable(provider_id),
            provider_runtime_identity(pc.get_provider_config(provider_id)),
        )
        monkeypatch.setenv(channel, SECRET_A)

        # Authoritative session-start boundary: the launch fixes the
        # session credential authority (SAFE metadata only).
        launch = build_local_project_launch(
            session_id="sess-v204-lifecycle",
            task_id="local-project-debug",
            policy="static-baseline",
            provider_id=provider_id,
            model_id="v204-lifecycle-x",
            profile_id="v204-lifecycle-x",
            launch_snapshot=dict(os.environ),
            project_spec=ProjectRuntimeEnvironmentSpec(),
        )
        authority = launch.credential_binding
        assert authority is not None
        assert authority.source_kind == "forwarded_session"
        assert SECRET_A not in json.dumps(authority.to_mapping())

        # Replace the durable slot BEFORE create_transport / first request.
        _hermetic_product_vault[provider_id] = SECRET_B

        gateway = ModelGateway()
        transport, _live_config = gateway.create_transport(
            launch.model_binding, credential_binding=authority
        )
        request = {
            "protocol": {"version": "1.3", "logical_model_call_index": 0},
            "context": {"task_id": "t", "state": "UNDERSTAND"},
        }
        result = transport.request(request, timeout_seconds=30.0)
        assert result["directive_content"] == DIRECTIVE
        # The SAME session still authenticates with the ISSUED SECRET_A.
        assert server.requests[0]["authorization"] == f"Bearer {SECRET_A}"

        # A NEW session (fresh process semantics: no issued channel)
        # resolves the new durable state.
        monkeypatch.delenv(channel, raising=False)
        new_launch = build_local_project_launch(
            session_id="sess-v204-lifecycle-2",
            task_id="local-project-debug",
            policy="static-baseline",
            provider_id=provider_id,
            model_id="v204-lifecycle-x",
            profile_id="v204-lifecycle-x",
            launch_snapshot=dict(os.environ),
            project_spec=ProjectRuntimeEnvironmentSpec(),
        )
        assert new_launch.credential_binding is not None
        assert new_launch.credential_binding.source_kind == "saved"
        new_env = gateway.transport_environment(
            new_launch.model_binding,
            credential_binding=new_launch.credential_binding,
        )
        assert new_env == {channel: SECRET_B}

def test_legacy_route_launch_materializes_no_direct_api_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_product_vault: dict,
) -> None:
    """F2 (repair 22): a LEGACY_CLI session never carries or materializes
    an Agentic-Debugger-held API credential — the external CLI credential
    authority owns that route — and an incompatible direct binding
    supplied to the legacy transport boundary fails closed."""

    from agentic_debugger.application.model_gateway import ModelBinding

    provider_id = "v204_legacy_gateway"
    pc.add_provider_config(
        name="V2-04 Legacy Gateway",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id=provider_id,
        api_key=SECRET_A,  # a direct credential ALSO exists
        transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT,
    )
    identity = provider_runtime_identity(pc.get_provider_config(provider_id))
    legacy_model_binding = ModelBinding(
        provider_id=provider_id,
        model_id="deepseek/deepseek-v4-flash",
        provider_model_id="deepseek/deepseek-v4-flash",
        display_name="deepseek/deepseek-v4-flash",
        route="legacy_cli",
        effective_protocol=None,
        endpoint_contract=pc.TRANSPORT_COMMANDCODE_GOAT,
        endpoint=None,
        auth_mode="bearer",
        config_fingerprint=None,
        tool_version="legacy-command-v1",
        provider_runtime_identity=identity,
    )
    channel = pc.provider_session_credential_variable(provider_id)

    # The launch is ROUTE-AWARE: the legacy session authority is the safe
    # external CLI authority (or none) — never a materializable direct
    # credential authority.
    launch = build_local_project_launch(
        session_id="sess-v204-legacy",
        task_id="local-project-debug",
        policy="static-baseline",
        provider_id=provider_id,
        model_id="deepseek/deepseek-v4-flash",
        profile_id="deepseek/deepseek-v4-flash",
        launch_snapshot=dict(os.environ),
        project_spec=ProjectRuntimeEnvironmentSpec(),
        model_binding=legacy_model_binding,
    )
    assert launch.credential_binding is None or (
        launch.credential_binding.source_kind == "external_cli"
    )
    # No secret can be pinned for this route: no ticket exists and the
    # transport materialization produces NO Agentic-Debugger-held env.
    assert launch.credential_ticket is None
    gateway = ModelGateway()
    env = gateway.transport_environment(
        launch.model_binding, credential_binding=launch.credential_binding
    )
    assert env is None

    # An explicit DIRECT credential binding supplied to the legacy
    # boundary fails closed before any child construction.
    direct_binding = CredentialVault.default().safe_binding(provider_id)
    assert direct_binding is not None
    assert direct_binding.source_kind == "saved"
    from agentic_debugger.application.credential_vault import CredentialRouteError
    from agentic_debugger.application.model_gateway import (
        IncoherentCredentialBindingError,
    )

    with pytest.raises((CredentialRouteError, IncoherentCredentialBindingError)):
        gateway.transport_environment(
            legacy_model_binding, credential_binding=direct_binding
        )
    # Even the retained-secret path cannot inject it: no ticket exists.
    assert launch.credential_ticket is None
