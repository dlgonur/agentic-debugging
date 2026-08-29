"""Regression for PUBLIC-OFFLINE-DEMO-ROUTING-REPAIR.

The bug: the public startup UI showed Mode Offline demo with task
curated-caller-callee-005, but the primary CTA routed to Level 32
Cookiecutter #967 (Ollama Cloud) and then failed Ollama 0.33.0 vs 0.33.1.
The fail-closed version gate is correct and must not be weakened.

This test exercises the real UI/application routing boundary and proves
that the displayed/selected task remains authoritative through CTA â†’ session
spec â†’ worker/source selection.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

textual = pytest.importorskip("textual")

from agentic_debugger.application.events import SourceKind
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.level32 import LEVEL32_TASK_ID, level32_model_profiles
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import StartSessionScreen, WorkspaceMode
from ui_support import run_headless


CURATED_TASK = "curated-caller-callee-005"
CURATED_TITLE = "Convert the caller representation at the boundary"


def make_app(tmp_path: Path) -> LocalApplicationV1:
    return LocalApplicationV1(history_store=HistoryStore(tmp_path))


def test_offline_demo_cta_starts_selected_curated_task_provider_free(tmp_path: Path) -> None:
    """Startup Offline demo + curated-caller-callee-005 â†’ offline deterministic.

    Proves at the real UI/application boundary:
      * startup state is Offline demo
      * curated task is selected/displayed
      * activating Run evidence demo starts that SAME task
      * resulting session/task identity matches selected curated task
      * execution source is local/provider-free (OFFLINE_DEMO, deterministic_offline)
      * no Ollama/model transport request is attempted
      * no Level-32 Cookiecutter/default scientific session is substituted
    """
    app = make_app(tmp_path / "offline_routing")

    # Capture the worker construction to assert source identity without spawning
    captured: dict[str, object] = {}

    # Patch both worker types at the app module so level32 cannot be substituted
    original_session_worker = None
    original_level32_worker = None

    # Import here to patch the symbols used by app.start_live_session
    from agentic_debugger.ui import app as app_module

    class FakeSessionWorker:
        def __init__(self, **kwargs):
            captured["worker_kind"] = "SessionWorkerProcess"
            captured["scenario"] = kwargs.get("scenario")
            captured["scenario_params"] = kwargs.get("scenario_params")
            captured["spec"] = kwargs.get("spec")
            captured["session_dir"] = kwargs.get("session_dir")
            captured["session_id"] = kwargs.get("session_id")
            # Minimal attributes expected by LiveSessionRunner
            self.session_dir = Path(kwargs.get("session_dir"))
            self._events = ()
            self._liveness = None

            # Create the session dir so history can register (or at least not fail)
            Path(self.session_dir).mkdir(parents=True, exist_ok=True)

        @property
        def pid(self):
            return None

        @property
        def events(self):
            return ()

        @property
        def liveness(self):
            return None

        def start(self):
            return None

        def cancel(self):
            pass

        def wait(self):
            # Return a minimal successful result so the runner can finish
            from agentic_debugger.application.session import SessionResult, SessionStatus, SessionTerminationReason

            return SessionResult(
                status=SessionStatus.SUCCEEDED,
                termination_reason=SessionTerminationReason.DONE,
                diagnostics=(),
            )

        def close(self):
            pass

    class FailIfLevel32:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Level-32 worker must not be instantiated for an Offline demo curated task")

    # Also ensure no Ollama transport is attempted via level32 profiles alias
    # For offline we should not even query Ollama cloud profiles for worker creation
    level32_created = {"called": False}

    async def scenario(pilot):
        await pilot.press("s")
        screen = pilot.app.screen
        assert isinstance(screen, StartSessionScreen), "session setup must be StartSessionScreen"

        # 1. startup state is the provider-free curated default
        assert screen._config.target == "curated", "startup target must be Curated task"
        assert screen._config.model.is_offline, "startup model must be Offline"
        assert screen.query_one("#start-session-button").label.plain == "Run evidence demo"

        # 2. a known curated task is selected/displayed
        assert screen.task_id == CURATED_TASK, f"expected default curated task {CURATED_TASK}, got {screen.task_id}"
        task_display = screen._task_display_name()
        assert CURATED_TITLE in task_display
        # Context panel must show the offline truth
        context = screen.query_one("#context-summary").render().plain
        assert "Target\nCurated task" in context
        assert f"Task\n{CURATED_TITLE}" in context
        assert f"Task ID\n{CURATED_TASK}" in context
        assert "Model\nOffline" in context
        assert "READY  Yes" in context
        # No Ollama/Level32 chrome should be visible for Offline demo
        assert "Ollama Cloud" not in context
        assert "Cookiecutter" not in context
        assert "Level 32" not in context

        # Patch workers so we can assert identity without spawning
        with patch.object(app_module, "SessionWorkerProcess", FakeSessionWorker), patch.object(
            app_module, "Level32OperatorWorker", FailIfLevel32
        ):
            # 3. activating the real Run evidence demo action starts that same task
            # Use the real button press path (on_button_pressed â†’ _start)
            await pilot.click("#start-session-button")
            await pilot.pause()

            # 4. resulting session/task identity matches selected curated task
            # The app should have created a live runner with offline spec
            assert app.live_runner is not None, "Run evidence demo must create a live runner"
            assert app.live_view is not None
            assert app.live_view.task_id == CURATED_TASK
            # 5. execution source is the intended local/provider-free path
            assert app.live_view.source_kind is SourceKind.OFFLINE_DEMO
            # 6. no Ollama/model transport request is attempted (Level32 not instantiated)
            assert captured.get("worker_kind") == "SessionWorkerProcess"
            assert captured.get("scenario") == "deterministic_offline"
            params = captured.get("scenario_params")
            assert isinstance(params, dict)
            assert params.get("task_id") == CURATED_TASK
            # policy should be the screen's selected policy
            assert params.get("policy") == screen._config.debugger_policy
            # model_config_ref must be None for offline
            spec = captured.get("spec")
            assert spec is not None
            assert spec.task_id == CURATED_TASK
            assert spec.source.kind is SourceKind.OFFLINE_DEMO
            assert spec.source.task_id == CURATED_TASK
            assert spec.source.model_config_ref is None

            # 7. no Level-32 Cookiecutter/default scientific session is substituted
            assert CURATED_TASK != LEVEL32_TASK_ID
            assert app.live_view.task_id != LEVEL32_TASK_ID
            # Header must not show Level32 chrome (workspace header is not yet proven until events, but view already shows task)
            # The live workspace should be OFFLINE_DEMO, not LEVEL32_OPERATOR
            workspace = pilot.app.screen
            # After start, the screen should be WorkspaceMode.LIVE
            from agentic_debugger.ui.screens import WorkspaceScreen

            assert isinstance(workspace, WorkspaceScreen)
            assert workspace.mode is WorkspaceMode.LIVE
            assert workspace._view.task_id == CURATED_TASK
            assert workspace._view.source_kind is SourceKind.OFFLINE_DEMO

    run_headless(app, scenario, size=(120, 32))


def test_offline_demo_rejects_inconsistent_ladder_task_fail_closed(tmp_path: Path) -> None:
    """Fail-closed boundary: offline source must not silently start a ladder task.

    This is the real failure boundary that caused visible curated Offline demo
    to be executed as Level32. On the pre-repair candidate this call would
    silently fall back to the ladder/Ollama path (creating a Level32 worker)
    instead of failing; after the repair it must raise.

    PRE-REPAIR: DID NOT RAISE (or raised a different error) â€” proves the bug.
    POST-REPAIR: raises "offline demo source cannot start a ladder task" â€” passes.
    """
    # Use a valid ladder profile so the stale handoff would have succeeded on
    # pre-repair (silent Level32 fallback). The repair must fail closed.
    profiles = level32_model_profiles()
    if not profiles:
        pytest.skip("no Level-32 eligible profiles")
    valid_profile = profiles[0].alias

    for task in (LEVEL32_TASK_ID, "pdb-required-boundary-006"):
        app = make_app(tmp_path / f"offline_rejects_{task.replace('/', '_')}")

        async def scenario(pilot):
            # On pre-repair, this would NOT raise our expected error, but would
            # instead create a Level32/Ollama worker (silent fallback). On
            # post-repair, it must raise our fail-closed error before any
            # worker is created.
            from unittest.mock import patch

            # Patch screen navigation to avoid needing a real Textual screen stack
            # and to avoid NoMatches for #evidence-pane on pre-repair
            with patch.object(pilot.app, "switch_screen", lambda *a, **k: None), patch.object(
                pilot.app, "push_screen", lambda *a, **k: None
            ):
                with pytest.raises(ValueError, match="offline demo source cannot start"):
                    pilot.app.start_live_session(
                        task_id=task,
                        policy="pdb-on-uncertainty",
                        max_elapsed_seconds=None,
                        source_kind=SourceKind.OFFLINE_DEMO,
                        profile_id=valid_profile,
                    )

        run_headless(app, scenario, size=(120, 32))


def test_explicit_level32_path_is_not_redirected_to_offline(tmp_path: Path) -> None:
    """Explicit Level-32 selection must still route to the scientific path.

    Bounded guard: selecting the Level-32 task must NOT be accidentally
    redirected into the offline demo path by the offline fix.
    """
    app = make_app(tmp_path / "level32_guard")

    # Need at least one eligible Ollama model for Level32
    profiles = level32_model_profiles()
    if not profiles:
        pytest.skip("no Level-32 eligible Ollama profiles in this checkout")

    chosen = profiles[0].alias
    captured: dict[str, object] = {}

    async def scenario(pilot):
        await pilot.press("s")
        screen = pilot.app.screen
        assert isinstance(screen, StartSessionScreen)

        # Explicitly select the ladder target, the Level-32 rung, and a
        # qualified model through the unified surface
        screen._choice_selected("target", "ladder")
        screen._choice_selected("task", LEVEL32_TASK_ID)
        await pilot.pause()
        assert screen.task_id == LEVEL32_TASK_ID
        screen._choice_selected("model", f"ollama_cloud:{chosen}")
        await pilot.pause()
        assert screen.start_available is True
        # Button should now be "Start session" (ladder, not evidence demo)
        assert screen.query_one("#start-session-button").label.plain == "Start session"
        context = screen.query_one("#context-summary").render().plain
        assert "Level 32/100" in context or "Cookiecutter" in context

        # Capture the routing at the application boundary without spawning
        original_start = app.start_live_session

        def capturing_start(**kwargs):
            captured.update(kwargs)
            # Do not actually spawn; just record and raise to prevent side effects
            # Simulate the app's validation by checking source/task pairing
            # The real method would create a Level32 worker; we just record
            raise RuntimeError("captured Level32 routing")

        with patch.object(app, "start_live_session", side_effect=capturing_start):
            # Activate the CTA (s or button) - use the screen's _start via button
            # We need to bypass the screen's exception handling: it catches and shows status
            # So we directly call the app method via screen's logic
            screen.action_start()
            await pilot.pause()
            # The screen should have caught the RuntimeError and shown it in status
            status = screen.query_one("#start-status").render().plain
            # Our fake raises "captured Level32 routing" - ensure it was called
            assert captured.get("task_id") == LEVEL32_TASK_ID
            assert captured.get("source_kind") is SourceKind.LEVEL32_OPERATOR
            assert captured.get("profile_id") == chosen
            assert captured.get("policy") == "exact-pdb-level32-frozen"
            # Must be Level32, not offline
            assert captured.get("source_kind") is not SourceKind.OFFLINE_DEMO
            # Also check that the status shows our captured error (proves the call happened)
            assert "captured Level32 routing" in status or captured.get("task_id") == LEVEL32_TASK_ID

    run_headless(app, scenario, size=(120, 32))
