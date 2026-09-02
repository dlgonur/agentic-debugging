"""Integration regression: configured provider must be usable for Capability Ladder.

Covers the user journey:
1. Configure a fake direct-API provider equivalent to CommandCode.
2. Persist credential through secure-store boundary.
3. Discover at least two models.
4. Open Capability Ladder.
5. Select Level 18.
6. Open model picker.
7. Select configured provider/model.
8. Confirm Start Session is enabled.
9. Start session.
10. Worker receives exact provider_id/model_id.
11. Fake HTTP provider receives real inference request.
12. Authorization uses configured credential.
13. Model request is NOT routed through Ollama.
14. Run/session metadata reports correct provider/model.
15. No silent Offline fallback.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentic_debugger.application import provider_connections as pc
from agentic_debugger.application import model_providers as mp
from agentic_debugger.application.events import (
    SessionEventKind,
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
    validate_session_event_stream,
)
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.journal import JournalReadState, read_session_journal
from agentic_debugger.application.level32 import LEVEL32_TASK_ID
from agentic_debugger.application.worker_scenarios import ScenarioContext, ScenarioInputError
from agentic_debugger.cancellation import CancellationToken
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import ChoicePickerScreen, StartSessionScreen
from test_single_task_live_path import LadderConfiguredTransport
from ui_support import run_headless

SECRET = "ladder-unified-test-credential-not-real"
LADDER_LEVEL_18_TASK = "pdb-required-multistage-units-008"
LADDER_LEVEL_6_TASK = "pdb-required-boundary-006"


@pytest.fixture(autouse=True)
def _isolate_provider_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path / "config-dir"))
    _secure_store: dict[str, str] = {}

    def _save(kind: str, val: str) -> bool:
        _secure_store[kind] = val
        return True

    def _load(kind: str):
        return _secure_store.get(kind)

    def _has(kind: str) -> bool:
        return kind in _secure_store

    def _delete(kind: str) -> bool:
        return bool(_secure_store.pop(kind, None) is not None)

    monkeypatch.setattr(pc, "save_secure_credential", _save)
    monkeypatch.setattr(pc, "load_secure_credential", _load)
    monkeypatch.setattr(pc, "has_secure_credential", _has)
    monkeypatch.setattr(pc, "delete_secure_credential", _delete)
    monkeypatch.setattr(pc, "provider_quarantine_path", lambda: tmp_path / "quarantine.json")
    pc._QUARANTINED_PROVIDERS.clear()
    pc.clear_all_session_keys()
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
    )
    yield
    pc.clear_all_session_keys()
    pc._QUARANTINED_PROVIDERS.clear()


class _ScriptedLadderServer:
    """Fake provider that records requests and serves scripted ladder directives.

    Drives the REAL Level-18 exact-PDB path:
      reproduce -> PDB (start/stack/locals/next/stop) -> hypothesis -> patch -> post-patch -> regression -> classification -> verifier
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self.catalog_models = ["deepseek/deepseek-v4-flash", "zai-org/glm-5.2"]
        self._transport = LadderConfiguredTransport(
            LADDER_LEVEL_18_TASK,
            {"value", "base_delay_ms", "retry_count"},
        )

    def _catalog_payload(self) -> Dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {"id": mid, "object": "model", "created": 1, "owned_by": "fake"}
                for mid in self.catalog_models
            ],
        }

    def _respond_chat(self, body: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
        msg = body["messages"][-1]["content"]
        if "=== BEGIN PUBLIC REQUEST ===" in msg:
            between = msg.split("=== BEGIN PUBLIC REQUEST ===", 1)[1].split("=== END PUBLIC REQUEST ===", 1)[0]
        else:
            between = msg
        payload = json.loads(between.strip())
        logical_index = payload.get("protocol", {}).get("logical_model_call_index")
        if logical_index == 2:
            directive = {
                "kind": "action",
                "name": "get_source_window",
                "arguments": {
                    "path": self._transport.scenario.localization.file_path,
                    "line": self._transport.scenario.runtime_probe.breakpoint_line,
                },
            }
        else:
            directive = self._transport.request(payload, timeout_seconds=30)
        content = json.dumps(directive)
        return 200, {
            "id": "chatcmpl-ladder",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        }

    def handler(self, request: Dict[str, Any]) -> tuple[int, Any]:
        with self._lock:
            self.calls.append(request)
        path = request["path"]
        method = request["method"]
        if method == "GET" and path.startswith("/models"):
            return 200, self._catalog_payload()
        if method == "GET" and path.startswith("/v1/models"):
            return 200, self._catalog_payload()
        if method == "POST" and path.endswith("/chat/completions"):
            body = json.loads(request["body"].decode("utf-8"))
            return self._respond_chat(body)
        if method == "POST" and path.endswith("/messages"):
            body = json.loads(request["body"].decode("utf-8"))
            return self._respond_chat({"messages": [{"role": "user", "content": json.dumps(body)}]})
        return 404, {"error": "not found"}

    def __enter__(self) -> "_ScriptedLadderServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                status, payload = outer.handler(
                    {
                        "method": "GET",
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": body,
                    }
                )
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                status, payload = outer.handler(
                    {
                        "method": "POST",
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": body,
                    }
                )
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format, *args):  # noqa: A003
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server = server
        self.port = server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()


def test_ladder_with_configured_provider_executes_through_direct_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full 15-step regression for Capability Ladder + generic provider."""

    with _ScriptedLadderServer() as server:
        # 1. Configure fake provider equivalent to CommandCode GOAT
        from agentic_debugger.application.provider_connections import (
            DiscoveredProviderModel,
            add_provider_config,
            update_provider_config,
            get_provider_config,
        )

        fake_provider_id = "fake_ladder_provider"
        fake_models = (
            DiscoveredProviderModel.create(fake_provider_id, "deepseek/deepseek-v4-flash", "DeepSeek V4 Flash", protocol="chat_completions"),
            DiscoveredProviderModel.create(fake_provider_id, "zai-org/glm-5.2", "GLM 5.2", protocol="chat_completions"),
        )
        builtin_models = (
            DiscoveredProviderModel.create("commandcode_goat", "deepseek/deepseek-v4-flash", "DeepSeek V4 Flash", protocol="chat_completions"),
            DiscoveredProviderModel.create("commandcode_goat", "zai-org/glm-5.2", "GLM 5.2", protocol="chat_completions"),
        )
        add_provider_config(
            name="CommandCode GOAT",
            base_url=server.base_url,
            api_format="chat_completions",
            provider_id="commandcode_goat",
            models=builtin_models,
        )
        try:
            pc.delete_provider_config(fake_provider_id)
        except Exception:
            pass
        add_provider_config(
            name="Fake Ladder Provider",
            base_url=server.base_url,
            api_format="chat_completions",
            provider_id=fake_provider_id,
            models=fake_models,
        )
        # 2. Persist credential through secure-store boundary for both
        assert pc.save_secure_credential("commandcode_goat", SECRET) is True
        assert pc.save_secure_credential(fake_provider_id, SECRET) is True
        pc.clear_all_session_keys()
        assert pc.credential_source_for("commandcode_goat") == pc.CREDENTIAL_SOURCE_SAVED
        assert pc.credential_source_for(fake_provider_id) == pc.CREDENTIAL_SOURCE_SAVED
        assert pc.load_secure_credential(fake_provider_id) == SECRET

        # 3. Discover models
        pc.refresh_provider_catalog("commandcode_goat")
        pc.refresh_provider_catalog(fake_provider_id)
        cfg2 = get_provider_config(fake_provider_id)
        assert cfg2 is not None and len(cfg2.models) >= 2
        models = [m for m in mp.list_provider_models(include_ollama=False) if m.kind == fake_provider_id]
        assert len(models) >= 2

        # 4-8. UI: Open Capability Ladder, select Level 18, pick provider model, confirm Start enabled
        app = LocalApplicationV1(history_store=HistoryStore(tmp_path / "history"))
        start_calls: list[dict[str, Any]] = []

        def record_start(**kwargs: Any) -> None:
            start_calls.append(kwargs)

        monkeypatch.setattr(app, "start_live_session", record_start)

        async def ui_scenario(pilot):
            await pilot.press("s")
            start = pilot.app.screen
            assert isinstance(start, StartSessionScreen)
            # 4. Open Capability Ladder (target ladder)
            start._choice_selected("target", "ladder")
            # 5. Select Level 18 (pdb-required-multistage-units-008)
            start._choice_selected("task", LADDER_LEVEL_18_TASK)
            # Before model selection, Start should be blocked
            assert start.start_available is False
            # 6. Open model picker
            start._open_model_picker()
            await pilot.pause()
            picker = pilot.app.screen
            assert isinstance(picker, ChoicePickerScreen)
            # 7. Select configured provider/model
            target_key = "commandcode_goat:deepseek/deepseek-v4-flash"
            values = [c.value for c in picker.choices]
            assert target_key in values, f"expected {target_key} in picker, got {values}"
            by_val = {c.value: c for c in picker.choices}
            assert by_val[target_key].disabled is False, f"picker disabled reason: {by_val[target_key].disabled_reason}"
            for c in picker.choices:
                if c.group == "COMMANDCODE GOAT":
                    assert "unavailable for Capability Ladder" not in (c.group_note or "")
                    break
            picker._on_select(target_key)
            pilot.app.pop_screen()
            await pilot.pause()
            # 8. Confirm Start Session is enabled and shows provider identity
            assert start._config.model.provider == "commandcode_goat"
            assert start._config.model.model_id == "deepseek/deepseek-v4-flash"
            assert start.start_available is True, f"status: {start.query_one('#start-status').render().plain}"
            model_value, provider_label = start._model_display()
            assert "DeepSeek" in model_value
            assert "CommandCode" in provider_label
            context_plain = start.query_one("#context-summary").render().plain.lower()
            assert "not a qualified" in context_plain or "executable provider model" in context_plain
            start.action_start()
            assert len(start_calls) == 1
            assert start_calls[0]["task_id"] == LADDER_LEVEL_18_TASK
            assert start_calls[0]["profile_id"] == "deepseek/deepseek-v4-flash"
            assert start_calls[0].get("model_provider") == "commandcode_goat"
            assert start_calls[0]["source_kind"] is SourceKind.CONFIGURED_MODEL

        run_headless(app, ui_scenario, size=(120, 32))

        # 9-15: Real execution through fake HTTP provider via headless app
        pc.set_session_key(fake_provider_id, SECRET)
        pc.set_session_key("commandcode_goat", SECRET)
        server.calls.clear()
        real_app = LocalApplicationV1(history_store=HistoryStore(tmp_path / "history2"))

        captured: dict[str, Any] = {}

        async def real_scenario(pilot):
            pilot.app.start_live_session(
                task_id=LADDER_LEVEL_18_TASK,
                policy="pdb-on-uncertainty",
                max_elapsed_seconds=90,
                source_kind=SourceKind.CONFIGURED_MODEL,
                profile_id="deepseek/deepseek-v4-flash",
                model_provider=fake_provider_id,
            )
            runner = pilot.app.live_runner
            assert runner is not None
            captured["runner"] = runner
            # 10. Worker receives exact provider_id/model_id
            assert runner.worker._scenario_params["provider"] == fake_provider_id
            assert runner.worker._scenario_params["model_id"] == "deepseek/deepseek-v4-flash"
            assert runner.worker._child_environment is not None
            assert SECRET in list(runner.worker._child_environment.values())
            assert SECRET not in " ".join(runner.worker._worker_argv())
            deadline = time.monotonic() + 90
            while runner.terminal is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert runner.terminal is not None, "worker did not reach terminal"
            captured["terminal"] = runner.terminal
            captured["session_dir"] = runner.worker.session_dir

        run_headless(real_app, real_scenario, size=(120, 32))

        runner = captured["runner"]
        history_dir = captured["session_dir"]
        journal_path = history_dir / "session.events.jsonl"
        time.sleep(0.3)
        read = read_session_journal(journal_path)
        assert read.state is JournalReadState.COMPLETE
        validate_session_event_stream(read.events)
        provenance = next(
            (e.payload for e in read.events if e.event_kind is SessionEventKind.MODEL_CONFIGURED),
            None,
        )
        assert provenance is not None
        assert provenance["provider"] == fake_provider_id
        assert provenance["profile_id"] == "deepseek/deepseek-v4-flash"
        assert provenance["route"] == "direct_api"
        assert provenance["api_protocol"] == "chat_completions"
        assert provenance["provider_model_id"] == "deepseek/deepseek-v4-flash"
        assert "127.0.0.1" in provenance.get("endpoint", "") or server.base_url in provenance.get("endpoint", "")

        # 1. Model requests and controller steps must respect the rung contract (24/24)
        model_requests = [e for e in read.events if e.event_kind == SessionEventKind.MODEL_REQUEST_STARTED]
        controller_steps = [e for e in read.events if e.event_kind == SessionEventKind.CONTROLLER_STEP]
        assert len(model_requests) <= 24, f"Level 18 contract allows 24 model requests, got {len(model_requests)}"
        assert len(controller_steps) <= 24, f"Level 18 contract allows 24 controller steps, got {len(controller_steps)}"
        # Retry count must be 0 (no auto-retry for ladder)
        model_configured = [e for e in read.events if e.event_kind == SessionEventKind.MODEL_CONFIGURED]
        assert len(model_configured) == 1
        assert runner.worker._retry_of_session_id is None

        # 2. Patch was actually applied and candidate.patch written
        patch_applied = [e for e in read.events if e.event_kind == SessionEventKind.PATCH_APPLIED]
        assert len(patch_applied) == 1, "expected exactly 1 PATCH_APPLIED event"
        candidate_patch_path = history_dir / "candidate.patch"
        assert candidate_patch_path.is_file(), "candidate.patch not written"
        assert "base_delay_ms" in candidate_patch_path.read_text(encoding="utf-8"), "patch does not contain expected fix"

        # 3. Verifier was run and reached RESOLVED
        verifier_completed = [e for e in read.events if e.event_kind == SessionEventKind.VERIFIER_COMPLETED]
        assert len(verifier_completed) == 1, "expected exactly 1 VERIFIER_COMPLETED event"
        assert verifier_completed[0].payload.get("status") == "COMPLETED"
        assert verifier_completed[0].payload.get("outcome") == "RESOLVED"

        # 4. Final session terminal state: COMPLETED with outcome RESOLVED
        assert captured["terminal"] is not None
        assert captured["terminal"].status == SessionStatus.SUCCEEDED
        assert captured["terminal"].termination_reason == SessionTerminationReason.DONE
        assert captured["terminal"].cleanup_verified is True

        # 5. Exact PDB lifecycle evidence exists
        pdb_events = [
            e for e in read.events
            if e.event_kind in (
                SessionEventKind.DEBUGGER_STARTED,
                SessionEventKind.DEBUGGER_LOCATION_CHANGED,
                SessionEventKind.DEBUGGER_STACK_OBSERVED,
                SessionEventKind.DEBUGGER_LOCALS_OBSERVED,
            )
        ]
        assert len(pdb_events) >= 1, "expected PDB lifecycle events"

        # 6. Observed locals present with required variables
        locals_observed = [e for e in read.events if e.event_kind == SessionEventKind.DEBUGGER_LOCALS_OBSERVED]
        assert len(locals_observed) >= 1
        observed_local_names: set[str] = set()
        for ev in locals_observed:
            payload_dict = dict(ev.payload)
            for loc in payload_dict.get("locals", ()):
                loc_dict = dict(loc)
                if "name" in loc_dict:
                    observed_local_names.add(loc_dict["name"])
        assert "base_delay_ms" in observed_local_names
        assert "value" in observed_local_names
        assert "retry_count" in observed_local_names

        express_directives = [
            e for e in read.events
            if e.event_kind == SessionEventKind.MODEL_DIRECTIVE_ACCEPTED
            and e.payload.get("action_name") == "express_root_cause_hypothesis"
        ]
        assert len(express_directives) == 1

        chat_calls = [c for c in server.calls if c["path"].endswith("/chat/completions")]
        assert len(chat_calls) >= 1, f"expected >=1 chat calls, got {len(server.calls)}"
        for call in chat_calls:
            assert call["authorization"].startswith("Bearer ")
        for call in server.calls:
            assert "ollama" not in call["path"].lower()
            assert call["path"] == "/chat/completions"
        assert read.events[0].source_kind is SourceKind.CONFIGURED_MODEL
        assert provenance["provider"] != "offline"
        journal_text = journal_path.read_text(encoding="utf-8")
        assert SECRET not in journal_text

        # 7. Canonical fixture unchanged
        fixture_path = REPO_ROOT / "agentic_debugger" / "datasets" / "curated" / LADDER_LEVEL_18_TASK / "deadline_pipeline.py"
        fixture_text = fixture_path.read_text(encoding="utf-8")
        assert "retry_window_ms = _expand_retry_window(value, retry_count)" in fixture_text
        for diag in captured["terminal"].diagnostics or []:
            assert SECRET not in diag

        # 7. Cleanup verified and fixture unchanged
        cleanup_completed = [e for e in read.events if e.event_kind == SessionEventKind.CLEANUP_COMPLETED]
        assert len(cleanup_completed) >= 1
        assert cleanup_completed[0].payload.get("verified") is True
        fixture = REPO_ROOT / "agentic_debugger/datasets/curated/pdb-required-multistage-units-008/deadline_pipeline.py"
        assert fixture.is_file()
        assert "_expand_retry_window(value, retry_count)" in fixture.read_text(encoding="utf-8"), "canonical fixture was mutated"

        real_app.live_runner.close() if real_app.live_runner else None


def test_configured_direct_api_uses_ladder_contract_budgets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct budget regression: Level 18 and Level 6 via configured source must use 24/24/3600|600/0."""
    from agentic_debugger.application.ollama_cloud_source import ladder_runtime_contract
    from agentic_debugger.application import model_providers as mp
    from agentic_debugger.application.configured_source import (
        run_configured_session,
        _DEFAULT_MAX_MODEL_REQUESTS,
        _DEFAULT_MAX_CONTROLLER_STEPS,
        _DEFAULT_MAX_RETRIES,
    )
    from agentic_debugger.application.emitter import SessionEventEmitter
    from agentic_debugger.application.journal import SessionEventJournal
    from agentic_debugger.evaluation.live import LiveRunLimits

    # Verify the canonical contracts themselves
    c18 = ladder_runtime_contract(LADDER_LEVEL_18_TASK)
    assert (c18.max_model_requests, c18.max_controller_steps, c18.max_model_phase_seconds, c18.max_retries) == (24, 24, 3600, 0)
    c6 = ladder_runtime_contract(LADDER_LEVEL_6_TASK)
    assert (c6.max_model_requests, c6.max_controller_steps, c6.max_model_phase_seconds, c6.max_retries) == (24, 24, 600, 0)
    assert _DEFAULT_MAX_MODEL_REQUESTS == 64
    assert _DEFAULT_MAX_CONTROLLER_STEPS == 64
    assert _DEFAULT_MAX_RETRIES == 2

    captured_runs: list[dict[str, Any]] = []
    captured_limits: list[LiveRunLimits] = []
    real_limits_init = LiveRunLimits.__init__

    def wrapped_limits_init(self: Any, *args: Any, **kwargs: Any) -> None:
        real_limits_init(self, *args, **kwargs)
        captured_limits.append(self)

    monkeypatch.setattr(LiveRunLimits, "__init__", wrapped_limits_init)

    def fake_run_local(ctx: Any, **kwargs: Any) -> None:
        captured_runs.append({
            "task_id": ctx.emitter.task_id,
            "max_model_calls": kwargs["max_model_calls"],
        })

    import agentic_debugger.application.configured_source as cs
    monkeypatch.setattr(cs, "run_local_session", fake_run_local)

    resolved_ceilings: dict[str, Any] = {}

    def wrapped_resolve(provider: str, model_id: str, **kwargs: Any):
        resolved_ceilings[f"{provider}:{model_id}"] = kwargs.get("logical_call_ceiling")
        from agentic_debugger.evaluation.live import LiveModelConfig
        cfg = LiveModelConfig(model_name=model_id, command=("echo", "hi"), request_timeout_seconds=30, tool_version="test")
        return cfg, {"display_name": model_id, "route": "direct_api", "api_protocol": "chat_completions", "provider_model_id": model_id, "endpoint": "http://fake"}

    monkeypatch.setattr(mp, "resolve_provider_live_config", wrapped_resolve)

    # 1. Level 18 task: configured source chooses 24 ceiling, 24/24/3600/0 limits
    j18 = SessionEventJournal(
        tmp_path / "j18.events.jsonl",
        session_id="sess-18",
        task_id=LADDER_LEVEL_18_TASK,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    e18 = SessionEventEmitter(
        sink=j18,
        session_id="sess-18",
        task_id=LADDER_LEVEL_18_TASK,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    ctx18 = ScenarioContext(work_dir=tmp_path / "w18", emitter=e18, token=CancellationToken())
    run_configured_session(ctx18, {"provider": "commandcode_goat", "model_id": "deepseek/deepseek-v4-flash", "policy": "pdb-on-uncertainty"})

    assert len(captured_runs) == 1
    assert captured_runs[0]["task_id"] == LADDER_LEVEL_18_TASK
    assert captured_runs[0]["max_model_calls"] == 24
    assert len(captured_limits) == 1
    assert captured_limits[0].max_model_requests == 24
    assert captured_limits[0].max_controller_steps == 24
    assert captured_limits[0].max_model_phase_seconds == 3600
    assert captured_limits[0].max_retries == 0
    assert resolved_ceilings["commandcode_goat:deepseek/deepseek-v4-flash"] == 24

    # 2. Ordinary curated task: configured source chooses 64 ceiling, 64/64/None/2 limits
    j_curated = SessionEventJournal(
        tmp_path / "j_curated.events.jsonl",
        session_id="sess-curated",
        task_id="curated-off-by-one-002",
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    e_curated = SessionEventEmitter(
        sink=j_curated,
        session_id="sess-curated",
        task_id="curated-off-by-one-002",
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    ctx_curated = ScenarioContext(work_dir=tmp_path / "w_curated", emitter=e_curated, token=CancellationToken())
    run_configured_session(ctx_curated, {"provider": "commandcode_goat", "model_id": "zai-org/glm-5.2", "policy": "pdb-on-uncertainty"})

    assert len(captured_runs) == 2
    assert captured_runs[1]["task_id"] == "curated-off-by-one-002"
    assert captured_runs[1]["max_model_calls"] == 64
    assert len(captured_limits) == 2
    assert captured_limits[1].max_model_requests == 64
    assert captured_limits[1].max_controller_steps == 64
    assert captured_limits[1].max_retries == 2
    assert resolved_ceilings["commandcode_goat:zai-org/glm-5.2"] == 64


def test_level32_configured_model_rejected_at_app_boundary(tmp_path: Path) -> None:
    """Level 32 must reject CONFIGURED_MODEL and non-LEVEL32_OPERATOR sources at the application boundary."""
    app = LocalApplicationV1(history_store=HistoryStore(tmp_path / "history"))

    with pytest.raises(ValueError, match="Level-32 task requires the Level-32 operator source"):
        app.start_live_session(
            task_id=LEVEL32_TASK_ID,
            policy="pdb-on-uncertainty",
            max_elapsed_seconds=None,
            source_kind=SourceKind.CONFIGURED_MODEL,
            profile_id="deepseek/deepseek-v4-flash",
            model_provider="commandcode_goat",
        )

    with pytest.raises(ValueError, match="Level-32 task requires the Level-32 operator source"):
        app.start_live_session(
            task_id=LEVEL32_TASK_ID,
            policy="pdb-on-uncertainty",
            max_elapsed_seconds=None,
            source_kind=SourceKind.CONFIGURED_MODEL,
            profile_id="my-custom-profile",
        )

    with pytest.raises(ValueError, match="Level-32 task requires the Level-32 operator source"):
        app.start_live_session(
            task_id=LEVEL32_TASK_ID,
            policy="exact-pdb-level32-frozen",
            max_elapsed_seconds=None,
            source_kind=SourceKind.OFFLINE_DEMO,
        )


def test_configured_source_rejects_level32_task(tmp_path: Path) -> None:
    """Defense in depth: run_configured_session must reject LEVEL32_TASK_ID directly."""
    from agentic_debugger.application.configured_source import run_configured_session
    from agentic_debugger.application.emitter import SessionEventEmitter
    from agentic_debugger.application.journal import SessionEventJournal

    journal = SessionEventJournal(
        tmp_path / "journal.events.jsonl",
        session_id="sess-test-level32",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    emitter = SessionEventEmitter(
        sink=journal,
        session_id="sess-test-level32",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    ctx = ScenarioContext(
        work_dir=tmp_path / "work",
        emitter=emitter,
        token=CancellationToken(),
    )
    with pytest.raises(ScenarioInputError, match="Level-32 task"):
        run_configured_session(
            ctx,
            {"provider": "commandcode_goat", "model_id": "deepseek/deepseek-v4-flash", "policy": "pdb-on-uncertainty"},
        )


def test_lower_ladder_contract_load_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract loading errors for lower ladder tasks must fail closed without fallback."""
    from agentic_debugger.application.configured_source import run_configured_session
    import agentic_debugger.application.configured_source as cs
    from agentic_debugger.application.emitter import SessionEventEmitter
    from agentic_debugger.application.journal import SessionEventJournal
    import agentic_debugger.demo.catalog as demo_catalog

    journal = SessionEventJournal(
        tmp_path / "journal.events.jsonl",
        session_id="sess-test-ladder-failclosed",
        task_id=LADDER_LEVEL_18_TASK,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    emitter = SessionEventEmitter(
        sink=journal,
        session_id="sess-test-ladder-failclosed",
        task_id=LADDER_LEVEL_18_TASK,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    ctx = ScenarioContext(
        work_dir=tmp_path / "work",
        emitter=emitter,
        token=CancellationToken(),
    )

    def failing_scenario_for(task_id: str):
        raise RuntimeError("simulated scenario catalog corruption")

    monkeypatch.setattr(demo_catalog, "scenario_for", failing_scenario_for)
    monkeypatch.setattr(cs, "scenario_for", failing_scenario_for)

    resolved_models: list[str] = []
    monkeypatch.setattr(mp, "resolve_provider_live_config", lambda *a, **k: resolved_models.append(a[1]))

    with pytest.raises(RuntimeError, match="simulated scenario catalog corruption"):
        run_configured_session(
            ctx,
            {"provider": "commandcode_goat", "model_id": "deepseek/deepseek-v4-flash", "policy": "pdb-on-uncertainty"},
        )

    assert len(resolved_models) == 0, "model resolution occurred despite contract loading failure"


def test_arbitrary_custom_provider_visible_in_picker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom direct-API providers configured in Model Providers appear in all pickers."""
    config_file = tmp_path / "custom-provider-config.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    store: dict[str, str] = {}
    monkeypatch.setattr(pc, "save_secure_credential", lambda k, v: store.__setitem__(k, v) or True)
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in store)
    monkeypatch.setattr(pc, "delete_secure_credential", lambda k: bool(store.pop(k, None) is not None))
    monkeypatch.setattr(pc, "provider_quarantine_path", lambda: tmp_path / "q.json")
    pc._QUARANTINED_PROVIDERS.clear()
    pc.clear_all_session_keys()

    custom_provider_id = "custom_ladder_provider"
    from agentic_debugger.application.provider_connections import (
        DiscoveredProviderModel,
        add_provider_config,
    )
    custom_models = (
        DiscoveredProviderModel.create(custom_provider_id, "custom/model-a", "Model A", protocol="chat_completions"),
        DiscoveredProviderModel.create(custom_provider_id, "custom/model-b", "Model B", protocol="chat_completions"),
    )
    add_provider_config(
        name="Custom Ladder Provider",
        base_url="http://127.0.0.1:9999",
        api_format="chat_completions",
        provider_id=custom_provider_id,
        models=custom_models,
    )
    pc.save_secure_credential(custom_provider_id, "custom-secret")

    app = LocalApplicationV1(history_store=HistoryStore(tmp_path / "history"))
    start_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(app, "start_live_session", lambda **kwargs: start_calls.append(kwargs))

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)

        # 1. Curated target model picker
        start._choice_selected("target", "curated")
        start._open_model_picker()
        await pilot.pause()
        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)
        values = [c.value for c in picker.choices]
        assert f"{custom_provider_id}:custom/model-a" in values
        assert any(c.group == "CUSTOM LADDER PROVIDER" for c in picker.choices)
        pilot.app.pop_screen()
        await pilot.pause()

        # 2. Local Project target model picker
        start._choice_selected("target", "local_project")
        start._open_model_picker()
        await pilot.pause()
        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)
        values = [c.value for c in picker.choices]
        assert f"{custom_provider_id}:custom/model-a" in values
        assert any(c.group == "CUSTOM LADDER PROVIDER" for c in picker.choices)
        pilot.app.pop_screen()
        await pilot.pause()

        # 3. Level-18 target model picker
        start._choice_selected("target", "ladder")
        start._choice_selected("task", LADDER_LEVEL_18_TASK)
        start._open_model_picker()
        await pilot.pause()
        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)
        values = [c.value for c in picker.choices]
        assert f"{custom_provider_id}:custom/model-a" in values
        assert any(c.group == "CUSTOM LADDER PROVIDER" for c in picker.choices)

        # 4. Select the custom model for Level 18
        picker._on_select(f"{custom_provider_id}:custom/model-a")
        pilot.app.pop_screen()
        await pilot.pause()

        assert start._config.model.provider == custom_provider_id
        assert start._config.model.model_id == "custom/model-a"
        assert start.start_available is True

        start.action_start()
        assert len(start_calls) == 1
        assert start_calls[0]["task_id"] == LADDER_LEVEL_18_TASK
        assert start_calls[0]["model_provider"] == custom_provider_id
        assert start_calls[0]["profile_id"] == "custom/model-a"
        assert start_calls[0]["source_kind"] is SourceKind.CONFIGURED_MODEL

    run_headless(app, scenario, size=(120, 32))


def test_level18_ui_shows_executable_not_qualified_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UI truthfulness: Level 18 + CommandCode shows 0 auto-retries and executable-but-not-qualified notice."""
    from agentic_debugger.application import provider_connections as pc
    from agentic_debugger.application.provider_connections import DiscoveredProviderModel, update_provider_config
    from agentic_debugger.application.ollama_cloud_source import ladder_runtime_contract

    config_file = tmp_path / "provider-config-ui.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    store: dict[str, str] = {}
    monkeypatch.setattr(pc, "save_secure_credential", lambda k, v: store.__setitem__(k, v) or True)
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in store)
    monkeypatch.setattr(pc, "delete_secure_credential", lambda k: bool(store.pop(k, None) is not None))
    monkeypatch.setattr(pc, "provider_quarantine_path", lambda: tmp_path / "q.json")
    pc._QUARANTINED_PROVIDERS.clear()
    pc.clear_all_session_keys()

    with _ScriptedLadderServer() as server:
        base = server.base_url
        pc.add_provider_config(
            name="CommandCode GOAT",
            base_url=base,
            api_format="chat_completions",
            provider_id="commandcode_goat",
            models=(DiscoveredProviderModel.create("commandcode_goat", "deepseek/deepseek-v4-flash", "DeepSeek V4 Flash", protocol="chat_completions"),),
        )
        pc.save_secure_credential("commandcode_goat", SECRET)
        app = LocalApplicationV1(history_store=HistoryStore(tmp_path / "hist"))

        async def scenario(pilot):
            await pilot.press("s")
            start = pilot.app.screen
            assert isinstance(start, StartSessionScreen)
            start._choice_selected("target", "ladder")
            start._choice_selected("task", LADDER_LEVEL_18_TASK)
            start._choice_selected("model", "commandcode_goat:deepseek/deepseek-v4-flash")

            assert start.start_available is True
            # Verify auto-retry row is truthfully displayed as 0 automatic retries
            auto_retry_row = start.query_one("#auto-retry-row")
            assert "0 automatic retries" in auto_retry_row.render().plain

            # Verify runtime max_retries is 0
            contract = ladder_runtime_contract(LADDER_LEVEL_18_TASK)
            assert contract.max_retries == 0

            # Verify notice in start-notes and context-summary
            start_notes_plain = start.query_one("#start-notes").render().plain
            context_plain = start.query_one("#context-summary").render().plain
            assert "executable provider model — not a qualified scientific treatment" in start_notes_plain.lower()
            assert "not a qualified" in context_plain.lower() or "executable provider model" in context_plain.lower()

            # Generate real Textual screenshot
            svg_content = pilot.app.export_screenshot()
            assert svg_content, "export_screenshot returned empty output"
            out = tmp_path / "level18-commandcode.svg"
            out.write_text(svg_content, encoding="utf-8")
            assert out.is_file() and out.stat().st_size > 0

            # Verify expected elements in real SVG (normalize NBSP)
            svg_text = svg_content.replace("\u00a0", " ").replace("&#160;", " ")
            assert "Level 18" in svg_text or "pdb-required-multistage" in svg_text
            assert "DeepSeek V4 Flash" in svg_text or "deepseek" in svg_text.lower()
            assert "CommandCode GOAT" in svg_text or "commandcode" in svg_text.lower()
            assert "0 automatic retries" in svg_text
            assert "Executable provider model — not a qualified scientific treatment" in svg_text or "not a qualified" in svg_text

            # Persist real SVG evidence to _ai-review directory
            review_dir = REPO_ROOT / "_ai-review" / "unified-provider-runtime"
            review_dir.mkdir(parents=True, exist_ok=True)
            (review_dir / "level18-commandcode.svg").write_text(svg_content, encoding="utf-8")

        run_headless(app, scenario, size=(120, 36))
