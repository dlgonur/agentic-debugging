"""Task 46 — UI screens decomposition import/lifecycle compatibility.

Proves the screens.py facade preserves the public import surface after the
responsibility-owned split (screens_shared/home/editors/providers/setup/
workspace), without duplicating existing UI behavior tests:

1. every previously public screen import from agentic_debugger.ui.screens
   still resolves to the implementation object;
2. the Textual app can instantiate the relevant screens;
3. representative compose/render paths still execute headless;
4. screen navigation contracts (bindings/actions, push/pop) remain;
5. provider/model picker and Level-32 setup surfaces remain;
6. no circular import is introduced (impl modules never import the facade,
   import order is irrelevant).

No provider calls: all provider access is monkeypatched or avoided.
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("textual")

UI_DIR = Path("agentic_debugger/ui")
IMPL_MODULES = [
    "screens_shared.py",
    "screens_home.py",
    "screens_editors.py",
    "screens_providers.py",
    "screens_setup.py",
    "screens_workspace.py",
]

# Every screen class the baseline screens.py exposed (its __all__ plus the
# editor/provider screens the test suite imports from the facade).
FACADE_SCREENS = [
    "AddManualModelDialogScreen",
    "AddProviderDialogScreen",
    "BugDescriptionEditorScreen",
    "BrowseScreen",
    "ChoicePickerScreen",
    "ConfirmDeleteProviderDialogScreen",
    "EditProviderDialogScreen",
    "EffortModalScreen",
    "HelpModalScreen",
    "HistoryScreen",
    "HomeScreen",
    "JumpToSequenceScreen",
    "ModelCatalogBrowserScreen",
    "ModelProvidersScreen",
    "ProviderConnectionsScreen",
    "SingleLineFieldEditorScreen",
    "StartSessionScreen",
    "TimeLimitEditorScreen",
    "WorkspaceScreen",
]

BASELINE_ALL = [
    "HelpModalScreen",
    "HistoryScreen",
    "HomeActionRow",
    "HomeScreen",
    "JumpToSequenceScreen",
    "StartSessionScreen",
    "WorkspaceMode",
    "WorkspaceScreen",
    "render_view_header",
]


def test_facade_reexports_every_previously_public_screen():
    import agentic_debugger.ui.screens as facade

    for name in FACADE_SCREENS + ["HomeActionRow", "WorkspaceMode", "ChoiceOption", "SessionSettingRow"]:
        assert hasattr(facade, name), name
        assert getattr(facade, name) is not None
    # Helpers/constants previously importable from the facade.
    for name in [
        "render_view_header",
        "verifier_cell",
        "START_FOOTER",
        "START_FOOTER_COMPACT",
        "WORKSPACE_FOOTER_ACTIVE",
        "WORKSPACE_FOOTER_IDLE",
        "REPLAY_FOOTER",
        "BANNER_3LINE",
        "BANNER_WIDE_SLANT",
        "list_provider_models",
        "format_model_display_name",
    ]:
        assert hasattr(facade, name), name


def test_facade_reexports_are_implementation_objects():
    import agentic_debugger.ui.screens as facade
    import agentic_debugger.ui.screens_editors as editors
    import agentic_debugger.ui.screens_home as home
    import agentic_debugger.ui.screens_providers as providers
    import agentic_debugger.ui.screens_setup as setup
    import agentic_debugger.ui.screens_shared as shared
    import agentic_debugger.ui.screens_workspace as workspace

    assert facade.HomeScreen is home.HomeScreen
    assert facade.HistoryScreen is home.HistoryScreen
    assert facade.StartSessionScreen is setup.StartSessionScreen
    assert facade.ModelProvidersScreen is providers.ModelProvidersScreen
    assert facade.ProviderConnectionsScreen is providers.ModelProvidersScreen
    assert facade.WorkspaceScreen is workspace.WorkspaceScreen
    assert facade.HelpModalScreen is shared.HelpModalScreen
    assert facade.render_view_header is shared.render_view_header
    assert facade.ChoicePickerScreen is editors.ChoicePickerScreen
    assert facade.SingleLineFieldEditorScreen is editors.SingleLineFieldEditorScreen
    # The provider-catalog lookup keeps one canonical binding: the facade
    # alias IS the setup module's global, which StartSessionScreen reads.
    assert facade.list_provider_models is setup.list_provider_models


def test_facade_all_matches_baseline_exactly():
    import agentic_debugger.ui.screens as facade

    # Behavior-preserving contract: wildcard/public API is exactly the
    # baseline 9-name sequence — ordered equality, not subset/superset.
    assert facade.__all__ == BASELINE_ALL


def test_star_import_matches_baseline():
    # `from ... import *` without an explicit list exposes exactly __all__.
    namespace: dict = {}
    exec("from agentic_debugger.ui.screens import *", namespace)
    for name in BASELINE_ALL:
        assert name in namespace, name
    assert [n for n in namespace if not n.startswith("__")] == BASELINE_ALL


def test_no_impl_module_imports_facade():
    for mod in IMPL_MODULES:
        tree = ast.parse((UI_DIR / mod).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "agentic_debugger.ui.screens":
                raise AssertionError(f"{mod}:{node.lineno} imports the facade")
            if isinstance(node, ast.Import) and any(
                a.name == "agentic_debugger.ui.screens" for a in node.names
            ):
                raise AssertionError(f"{mod}:{node.lineno} imports the facade")


def test_facade_defines_no_implementation():
    tree = ast.parse((UI_DIR / "screens.py").read_text(encoding="utf-8"))
    impl = [
        n.name
        for n in tree.body
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert impl == [], impl


def test_import_order_independent_no_cycle():
    code = (
        "import agentic_debugger.ui.screens_workspace as w; "
        "import agentic_debugger.ui.screens_home as h; "
        "import agentic_debugger.ui.screens as f; "
        "import agentic_debugger.ui.app as a; "
        "assert f.WorkspaceScreen is w.WorkspaceScreen; "
        "assert f.HomeScreen is h.HomeScreen; "
        "print('order-ok')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path.cwd()),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "order-ok" in proc.stdout


def _run_async(coro):
    return asyncio.run(coro)


def _make_app(tmp_path):
    from agentic_debugger.ui.app import LocalApplicationV1

    return LocalApplicationV1(history_root=tmp_path / "hist-facade")


def test_home_and_setup_compose_headless(tmp_path):
    async def _inner():
        from agentic_debugger.ui.screens import HomeScreen, StartSessionScreen
        from agentic_debugger.ui.screens_home import HomeActionRow

        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(HomeScreen())
            await pilot.pause()
            rows = app.screen.query(HomeActionRow)
            assert len(rows) == 5
            app.push_screen(StartSessionScreen(task_options=[]))
            await pilot.pause()
            from agentic_debugger.ui.screens import StartSessionScreen as FacadeStart

            assert isinstance(app.screen, FacadeStart)
            assert app.screen.query_one("#start-session-button") is not None
            assert app.screen.query_one("#start-footer") is not None
            app.pop_screen()
            await pilot.pause()
            assert isinstance(app.screen, HomeScreen)

    _run_async(_inner())


def test_provider_manager_and_pickers_compose_headless(tmp_path):
    async def _inner():
        from agentic_debugger.ui.screens import (
            ChoiceOption,
            ChoicePickerScreen,
            ModelProvidersScreen,
            SingleLineFieldEditorScreen,
            TimeLimitEditorScreen,
        )

        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(ModelProvidersScreen())
            await pilot.pause()
            assert app.screen.query_one("#providers-status") is not None
            app.pop_screen()
            await pilot.pause()
            picked = []
            app.push_screen(
                ChoicePickerScreen(
                    title="Model",
                    choices=[
                        ChoiceOption("a", "Alpha", group="LADDER", group_note="ok"),
                        ChoiceOption("b", "Beta", group="LADDER"),
                    ],
                    current="a",
                    on_select=picked.append,
                )
            )
            await pilot.pause()
            assert app.screen.query_one("#choice-picker-list") is not None
            app.pop_screen()
            await pilot.pause()
            app.push_screen(
                TimeLimitEditorScreen(current=60, on_save=lambda v: None, on_cancel=lambda: None)
            )
            await pilot.pause()
            assert app.screen.query_one("#time-limit-editor") is not None
            app.pop_screen()
            await pilot.pause()
            app.push_screen(
                SingleLineFieldEditorScreen(title="Repro", current="", on_save=lambda v: None)
            )
            await pilot.pause()
            assert app.screen.query_one("#single-line-editor") is not None
            app.pop_screen()
            await pilot.pause()

    _run_async(_inner())


def test_workspace_live_compose_and_help_headless(tmp_path):
    async def _inner():
        from agentic_debugger.application.events import SourceKind
        from agentic_debugger.application.presentation import (
            PresentationIdentity,
            initial_session_view,
        )
        from agentic_debugger.ui.screens import HelpModalScreen, WorkspaceMode, WorkspaceScreen

        identity = PresentationIdentity(
            task_id="curated-off-by-one-002",
            source_kind=SourceKind.OFFLINE_DEMO,
            session_id="sess-facade",
        )
        view = initial_session_view(identity)
        app = _make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            workspace = WorkspaceScreen(mode=WorkspaceMode.LIVE, identity=identity, view=view)
            app.push_screen(workspace)
            await pilot.pause()
            await pilot.pause()
            assert workspace.query_one("#pane-tabs") is not None
            assert workspace.query_one("#copy-live") is not None
            app.push_screen(HelpModalScreen())
            await pilot.pause()
            assert isinstance(app.screen, HelpModalScreen)
            app.pop_screen()
            await pilot.pause()
            assert isinstance(app.screen, WorkspaceScreen)

    _run_async(_inner())


def test_navigation_bindings_preserved():
    from agentic_debugger.ui.screens import (
        HistoryScreen,
        HomeScreen,
        StartSessionScreen,
        WorkspaceScreen,
    )

    def actions(screen_cls):
        return {b.action for b in screen_cls.BINDINGS}

    home = actions(HomeScreen)
    assert "start_session" in home and "open_history" in home and "open_providers" in home
    setup = actions(StartSessionScreen)
    assert "confirm" in setup and "cancel" in setup and "history" in setup
    hist = actions(HistoryScreen)
    assert "go_back" in hist and "open_selected" in hist
    ws = actions(WorkspaceScreen)
    for expected in ("select_tab_1", "replay_next", "replay_previous", "history", "new_session"):
        assert expected in ws, expected
    # Bound actions resolve to real handlers (lifecycle boundary unchanged).
    for cls, names in [
        (HomeScreen, ("action_start_session", "action_open_history")),
        (StartSessionScreen, ("action_confirm", "action_cancel", "action_start")),
        (WorkspaceScreen, ("action_replay_next", "action_new_session", "action_back_home")),
    ]:
        for name in names:
            assert callable(getattr(cls, name)), (cls.__name__, name)


def test_provider_picker_and_ladder_surfaces_remain():
    from agentic_debugger.ui.session_config import TARGET_LADDER, TARGET_LOCAL_PROJECT
    from agentic_debugger.ui.screens import ChoiceOption, StartSessionScreen
    from agentic_debugger.ui.screens_setup import (
        _fit_row_cells,
        _provider_label,
        _short_unavailable_reason,
    )

    # The setup surface still owns the ladder/local targets and the provider
    # display helpers it moved with.
    assert TARGET_LADDER and TARGET_LOCAL_PROJECT
    assert "local" in _provider_label("local_project").lower() or _provider_label("x-y") == "x-y"
    assert isinstance(_fit_row_cells("a", "b", "c", 20), tuple)
    assert isinstance(_short_unavailable_reason("x (y)"), str)
    opt = ChoiceOption("m", "Model M", group="OLLAMA CLOUD")
    assert opt.group == "OLLAMA CLOUD"
    assert hasattr(StartSessionScreen, "action_open_providers")


def test_render_view_header_request_display():
    # Task-44 STEP-display contract at the facade: the shared header still
    # surfaces the cumulative request count for a running session.
    from agentic_debugger.application.events import SessionStatus, SourceKind
    from agentic_debugger.application.presentation import SessionViewState
    from agentic_debugger.ui.screens import render_view_header

    view = SessionViewState(
        task_id="curated-off-by-one-002",
        source_kind=SourceKind.OFFLINE_DEMO,
        status=SessionStatus.RUNNING,
        latest_model_request_index=0,
    )
    header = render_view_header(view, mode="LIVE", mode_style="bold")
    assert "Request 1" in header.plain
