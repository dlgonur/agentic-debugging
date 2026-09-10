"""Local Project model-call ceiling coherence regression (Task-26, Task-44).

Task-26 established ONE effective ceiling authority from session launch
through model execution.  Task-44 (Unbounded Session Progress v1)
supersedes the finite values for generic execution while PRESERVING the
coherence invariant: launch-time ``ModelBinding`` resolution, transport
materialization, the ``LiveModelAdapter`` request limit, and the
``DeterministicController`` model-call limit must all derive from the
SAME authority — now unbounded (logical ceiling 0 / ``None``) for
generic Local Project sessions.

``SessionBudgets.max_model_calls`` and the historical Local Project
default (32) remain as compatibility/provenance metadata only: they must
not affect generic execution or executable fingerprints.  The
fingerprint/stale-binding validation itself is preserved — the mismatch
check still fails closed; it simply never fires for coherent generic
sessions because both sides resolve at 0.

The regression exercises the real Local Product seam end to end — the
same ``run_local_project_session`` flow the worker runs (worker-owned
``ctx.session_launch`` and the direct non-worker fallback), through the
real ``ModelGateway.resolve``/``create_transport`` — with a synthetic
AUTH_NONE loopback registry provider.  No network, no real credentials,
no model execution (the run stops deterministically right after the
controller limits are constructed).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agentic_debugger.application.model_providers import (  # noqa: E402
    resolve_provider_live_config,
)
from agentic_debugger.application.provider_connections import (  # noqa: E402
    AUTH_NONE,
    PROTOCOL_CHAT_COMPLETIONS,
    TRANSPORT_GENERIC,
    add_provider_config,
)
from agentic_debugger.application.session import SessionBudgets  # noqa: E402
from agentic_debugger.application.session_runtime import (  # noqa: E402
    ProjectRuntimeEnvironmentSpec,
    build_local_project_launch,
)

PROVIDER_ID = "lp_ceiling_p"
MODEL_ID = "ceiling-model-x"
HEAD = "b" * 40


class _StopAfterControllerConfig(RuntimeError):
    """Deterministic stop: the source built the controller limits."""


def _isolate_provider_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH",
        str(tmp_path / "provider-configurations.json"),
    )
    add_provider_config(
        name="LP Ceiling Provider",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id=PROVIDER_ID,
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )


def _command_ceiling(command: tuple[str, ...]) -> str:
    flag = "--max-logical-model-calls"
    assert flag in command, f"ceiling flag missing from command: {command!r}"
    return command[command.index(flag) + 1]


def _fingerprint_at(ceiling: int) -> str:
    live, _ = resolve_provider_live_config(
        PROVIDER_ID, MODEL_ID, logical_call_ceiling=ceiling
    )
    return live.configuration_fingerprint


def _ceiling_is_fingerprint_relevant(tmp_path: Path) -> None:
    """Ground truth: the ceiling enters the executable command and its
    fingerprint, so fingerprint equality proves the resolved ceiling."""
    live32, _ = resolve_provider_live_config(
        PROVIDER_ID, MODEL_ID, logical_call_ceiling=32
    )
    live64, _ = resolve_provider_live_config(
        PROVIDER_ID, MODEL_ID, logical_call_ceiling=64
    )
    assert _command_ceiling(live32.command) == "32"
    assert _command_ceiling(live64.command) == "64"
    assert live32.configuration_fingerprint != live64.configuration_fingerprint


def _params(tmp_path: Path, iso: Path) -> dict:
    return {
        "project_repo_path": str(tmp_path / "repo"),
        "project_head": HEAD,
        "isolated_workspace": str(iso),
        "bug_description": "a bug",
        "config_root": str(tmp_path / "cfg"),
        "profile_id": MODEL_ID,
        "provider": PROVIDER_ID,
        "model_id": MODEL_ID,
    }


def _install_source_harness(
    monkeypatch: pytest.MonkeyPatch, captured: dict
) -> None:
    """No-op the source's observability/inventory and deterministically
    stop the run right after the controller limits are constructed,
    recording the limits the source handed to the live adapter and the
    controller.  No model/transport execution happens.

    The Task-26 flow runs through the normal strict event validator: the
    complete ``ModelBinding.model_configured_payload()`` (including the
    Task-28 safe runtime provenance) is emitted through the real
    ``SessionEventEmitter``/``SessionEvent`` validation path."""
    from agentic_debugger.application import local_project_source

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
    monkeypatch.setattr(
        local_project_source,
        "_inventory_tracked_python_files",
        lambda _isolated, **_kwargs: ["sample.py"],
    )

    real_limits = local_project_source.LiveRunLimits
    real_controller_config = local_project_source.ControllerRunConfig

    def _recording_limits(*args: Any, **kwargs: Any):
        captured["limits_max_model_requests"] = kwargs.get("max_model_requests")
        return real_limits(*args, **kwargs)

    def _capturing_controller_config(*args: Any, **kwargs: Any):
        captured["controller_max_model_calls"] = kwargs.get("max_model_calls")
        raise _StopAfterControllerConfig

    monkeypatch.setattr(
        local_project_source, "LiveRunLimits", _recording_limits
    )
    monkeypatch.setattr(
        local_project_source,
        "ControllerRunConfig",
        _capturing_controller_config,
    )


class _RecordingSink:
    """Minimal event sink: records SessionEvents validated by the real
    ``SessionEventEmitter``/``SessionEvent`` strict schema path.

    The emitter constructs each ``SessionEvent`` through the strict
    payload validators before appending, so reaching the controller-config
    stop proves the complete ``model.configured`` provenance (including
    the safe ``ModelBinding`` runtime fields) passed the durable schema.
    """

    def __init__(self) -> None:
        self.events: list[Any] = []

    def append(self, event):  # type: ignore[no-untyped-def]
        self.events.append(event)
        return event


def _model_configured_payload(sink: _RecordingSink) -> dict:
    from agentic_debugger.application.events import SessionEventKind

    payloads = [
        dict(event.payload)
        for event in sink.events
        if event.event_kind is SessionEventKind.MODEL_CONFIGURED
    ]
    assert len(payloads) == 1, f"expected one model.configured event: {len(payloads)}"
    return payloads[0]


def _run_worker_style_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    budgets: SessionBudgets,
    session_id: str,
) -> tuple[Any, dict]:
    """The normal worker-owned path: the SessionLaunch (with its session
    budgets and resolved ModelBinding) is built once outside the source
    and carried on the ScenarioContext, exactly like ``worker.run_worker``."""
    from agentic_debugger.application.emitter import SessionEventEmitter
    from agentic_debugger.application.events import SourceKind
    from agentic_debugger.application.local_project_source import (
        run_local_project_session,
    )
    from agentic_debugger.application.worker_scenarios import ScenarioContext
    from agentic_debugger.cancellation import CancellationToken

    iso = tmp_path / "iso"
    iso.mkdir(exist_ok=True)
    (iso / "sample.py").write_text("value = 1\n", encoding="utf-8")

    launch = build_local_project_launch(
        session_id=session_id,
        task_id="local-project-debug",
        policy="pdb-on-uncertainty",
        provider_id=PROVIDER_ID,
        model_id=MODEL_ID,
        profile_id=MODEL_ID,
        launch_snapshot={"PATH": "/usr/bin"},
        project_spec=ProjectRuntimeEnvironmentSpec(),
        budgets=budgets,
        config_root=str(tmp_path / "cfg"),
    )
    captured: dict = {"launch": launch}
    _install_source_harness(monkeypatch, captured)
    sink = _RecordingSink()
    emitter = SessionEventEmitter(
        sink=sink,
        session_id=session_id,
        task_id="local-project-debug",
        source_kind=SourceKind.LOCAL_PROJECT,
    )
    ctx = ScenarioContext(
        work_dir=tmp_path,
        token=CancellationToken(),
        emitter=emitter,
        run_id="run-lp-ceiling",
        session_launch=launch,
    )
    with pytest.raises(_StopAfterControllerConfig):
        run_local_project_session(ctx, _params(tmp_path, iso))
    captured["provenance"] = _model_configured_payload(sink)
    return launch, captured


def _run_fallback_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict:
    """The direct non-worker fallback: no context launch, so the source
    builds one itself through the same factory."""
    from agentic_debugger.application.emitter import SessionEventEmitter
    from agentic_debugger.application.events import SourceKind
    from agentic_debugger.application.local_project_source import (
        run_local_project_session,
    )
    from agentic_debugger.application.worker_scenarios import ScenarioContext
    from agentic_debugger.cancellation import CancellationToken

    iso = tmp_path / "iso"
    iso.mkdir(exist_ok=True)
    (iso / "sample.py").write_text("value = 1\n", encoding="utf-8")

    captured: dict = {}
    _install_source_harness(monkeypatch, captured)
    session_id = "sess-lp-ceiling-fallback"
    sink = _RecordingSink()
    emitter = SessionEventEmitter(
        sink=sink,
        session_id=session_id,
        task_id="local-project-debug",
        source_kind=SourceKind.LOCAL_PROJECT,
    )
    ctx = ScenarioContext(
        work_dir=tmp_path,
        token=CancellationToken(),
        emitter=emitter,
        run_id="run-lp-ceiling-fallback",
    )
    with pytest.raises(_StopAfterControllerConfig):
        run_local_project_session(ctx, _params(tmp_path, iso))
    captured["provenance"] = _model_configured_payload(sink)
    return captured


# ---------------------------------------------------------------------------
# generic Local Project execution is unbounded end to end (Task-44)
# ---------------------------------------------------------------------------


def test_default_local_project_ceiling_is_32_worker_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_provider_config(tmp_path, monkeypatch)
    _ceiling_is_fingerprint_relevant(tmp_path)

    launch, captured = _run_worker_style_session(
        tmp_path, monkeypatch, budgets=SessionBudgets(), session_id="sess-lp-ceil-w"
    )

    # Task-44: SessionBudgets counts are provenance metadata only — the
    # historical helper still reports 32, but generic execution never
    # consults it.
    from agentic_debugger.application.session_runtime import (
        LOCAL_PROJECT_DEFAULT_MAX_MODEL_CALLS,
        local_project_model_call_ceiling,
    )

    assert launch.budgets.max_model_calls is None
    assert local_project_model_call_ceiling(launch.budgets) == 32
    assert LOCAL_PROJECT_DEFAULT_MAX_MODEL_CALLS == 32

    # Launch-time ModelBinding was resolved under the unbounded
    # authority (0) — and the session did not fail at transport
    # materialization with a fingerprint drift (reaching the
    # controller-config stop proves create_transport corroborated at 0).
    binding = launch.model_binding
    assert binding.config_fingerprint is not None
    assert binding.config_fingerprint == _fingerprint_at(0)
    assert binding.config_fingerprint != _fingerprint_at(32)
    assert binding.config_fingerprint != _fingerprint_at(64)

    # The adapter request limit and the controller model-call limit are
    # unbounded (None) — counters are telemetry only.
    assert captured["limits_max_model_requests"] is None
    assert captured["controller_max_model_calls"] is None

    # The emitted model.configured provenance carries the launch
    # binding's (0-resolved) configuration fingerprint.
    assert captured["provenance"] is not None
    assert captured["provenance"]["config_fingerprint"] == binding.config_fingerprint


def test_default_local_project_ceiling_is_32_fallback_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_provider_config(tmp_path, monkeypatch)
    _ceiling_is_fingerprint_relevant(tmp_path)

    captured = _run_fallback_session(tmp_path, monkeypatch)

    # Same invariant on the direct/non-worker fallback path: the source
    # built its launch through the factory and stayed at the unbounded
    # authority through binding resolution, transport, adapter, and
    # controller.
    assert captured["limits_max_model_requests"] is None
    assert captured["controller_max_model_calls"] is None
    assert captured["provenance"] is not None
    assert captured["provenance"]["config_fingerprint"] == _fingerprint_at(0)
    assert captured["provenance"]["config_fingerprint"] != _fingerprint_at(32)
    assert captured["provenance"]["config_fingerprint"] != _fingerprint_at(64)


# ---------------------------------------------------------------------------
# explicit Local Project model-call budget: coherent at every seam
# ---------------------------------------------------------------------------


def test_explicit_session_budget_is_authoritative_worker_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_provider_config(tmp_path, monkeypatch)
    _ceiling_is_fingerprint_relevant(tmp_path)

    launch, captured = _run_worker_style_session(
        tmp_path,
        monkeypatch,
        budgets=SessionBudgets(max_model_calls=7),
        session_id="sess-lp-ceil-x",
    )

    # Task-44: an explicit SessionBudgets count is provenance metadata
    # only — it must NOT restore a finite execution ceiling.  The pure
    # helper still reports 7, but the launch binding resolves at the
    # unbounded authority (0), not under 7, 32, or 64.
    from agentic_debugger.application.session_runtime import (
        local_project_model_call_ceiling,
    )

    assert local_project_model_call_ceiling(launch.budgets) == 7

    # Launch-time binding resolved under 0 (unbounded).
    binding = launch.model_binding
    assert binding.config_fingerprint is not None
    assert binding.config_fingerprint == _fingerprint_at(0)
    assert binding.config_fingerprint != _fingerprint_at(7)
    assert binding.config_fingerprint != _fingerprint_at(32)
    assert binding.config_fingerprint != _fingerprint_at(64)

    # Transport materialization corroborated under the SAME unbounded
    # authority (reaching the controller-config stop proves
    # create_transport passed), and the adapter/controller limits are
    # unbounded (None).
    assert captured["limits_max_model_requests"] is None
    assert captured["controller_max_model_calls"] is None
    assert captured["provenance"]["config_fingerprint"] == binding.config_fingerprint


# ---------------------------------------------------------------------------
# ceiling authority contract
# ---------------------------------------------------------------------------


def test_ceiling_helper_contract() -> None:
    from agentic_debugger.application.session_runtime import (
        LOCAL_PROJECT_DEFAULT_MAX_MODEL_CALLS,
        local_project_model_call_ceiling,
    )

    assert LOCAL_PROJECT_DEFAULT_MAX_MODEL_CALLS == 32
    assert local_project_model_call_ceiling(SessionBudgets()) == 32
    assert local_project_model_call_ceiling(SessionBudgets(max_model_calls=7)) == 7
    assert (
        local_project_model_call_ceiling(SessionBudgets(max_model_calls=1)) == 1
    )
    with pytest.raises(Exception, match="budgets must be a SessionBudgets"):
        local_project_model_call_ceiling({"max_model_calls": 7})
    with pytest.raises(Exception, match="budgets must be a SessionBudgets"):
        local_project_model_call_ceiling(None)


# ---------------------------------------------------------------------------
# unrelated authorities unchanged
# ---------------------------------------------------------------------------


def test_gateway_generic_default_is_unbounded_and_explicit_finite_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task-44 (repair F3): a gateway resolve WITHOUT an explicit ceiling
    equals the 0-resolution (unbounded generic operation) — never a
    historical finite default.  An explicitly supplied finite ceiling is
    still honored (explicit-caller semantics for frozen/harness use)."""
    from agentic_debugger.application.model_gateway import ModelGateway

    _isolate_provider_config(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path / "cfg"))
    gateway = ModelGateway(config_root=tmp_path / "cfg")

    binding = gateway.resolve(PROVIDER_ID, MODEL_ID)
    assert binding.config_fingerprint is not None
    assert binding.config_fingerprint == _fingerprint_at(0)
    assert binding.config_fingerprint != _fingerprint_at(32)
    assert binding.config_fingerprint != _fingerprint_at(64)

    explicit = gateway.resolve(PROVIDER_ID, MODEL_ID, logical_call_ceiling=64)
    assert explicit.config_fingerprint == _fingerprint_at(64)
