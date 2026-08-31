"""Product-surface coverage for Provider Connections and the dynamic
general model picker.

Uses fake connection statuses and fake catalogs only: no real provider
is contacted, no credential value is ever rendered.
"""

from __future__ import annotations

from html import unescape
from pathlib import Path
from types import SimpleNamespace

import pytest

textual = pytest.importorskip("textual")

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui_support import run_headless  # noqa: E402

from agentic_debugger.application.history import HistoryStore  # noqa: E402
from agentic_debugger.application.model_providers import ProviderModel  # noqa: E402
from agentic_debugger.application import provider_connections as pc  # noqa: E402
from agentic_debugger.ui.app import LocalApplicationV1  # noqa: E402
from agentic_debugger.ui.screens import (  # noqa: E402
    ChoicePickerScreen,
    ProviderConnectionsScreen,
    StartSessionScreen,
)

SECRET = "ui-test-key-not-a-real-credential"


def make_app(tmp_path: Path) -> LocalApplicationV1:
    return LocalApplicationV1(history_store=HistoryStore(tmp_path))


def fake_statuses(connected_opencode: bool = True, connected_goat: bool = False):
    from agentic_debugger.application.provider_connections import (
        ProviderConnectionStatus,
    )

    def _status(kind: str, connected: bool, model_count: int) -> ProviderConnectionStatus:
        return ProviderConnectionStatus(
            kind=kind,
            label="OpenCode Go" if kind == "opencode_go" else "CommandCode GOAT",
            base_url=(
                "https://opencode.ai/zen/go/v1/models"
                if kind == "opencode_go"
                else "https://api.commandcode.ai/provider/v1/models"
            ),
            connected=connected,
            credential_source="session_key" if connected else None,
            model_count=model_count,
            last_refresh_utc="2026-08-30T12:00:00Z" if model_count else None,
            last_refresh_source="live" if model_count else None,
            stale=False,
            status_message=None if connected else "Not connected — no credential",
            cached_models=(),
        )

    return [
        _status("opencode_go", connected_opencode, 33 if connected_opencode else 0),
        _status("commandcode_goat", connected_goat, 27 if connected_goat else 0),
    ]


@pytest.fixture(autouse=True)
def _clean_session_keys():
    pc.clear_all_session_keys()
    yield
    pc.clear_all_session_keys()


def test_provider_connections_screen_renders_both_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path)
    statuses = fake_statuses()
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.connection_statuses", lambda: statuses
    )

    def scenario(pilot):
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)
        for status in statuses:
            refresh_line = str(
                screen.query_one(f"#provider-refresh-{status.kind}").render().plain
            )
            if status.model_count:
                assert f"{status.model_count} models" in refresh_line
                assert "2026-08-30" in refresh_line
            else:
                assert "Not connected" in refresh_line

    async def actions(pilot):
        await pilot.app.push_screen(ProviderConnectionsScreen())
        await pilot.pause()
        scenario(pilot)

    run_headless(app, actions, size=(100, 30))


def test_provider_screen_keyboard_refresh_and_key_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path)
    statuses = fake_statuses()
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.connection_statuses", lambda: statuses
    )
    refreshed: list[str] = []
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.refresh_provider_catalog",
        lambda kind, **kwargs: (
            refreshed.append(kind),
            SimpleNamespace(models=[SimpleNamespace(model_id="m")] * 33),
        )[1],
    )

    async def actions(pilot):
        await pilot.app.push_screen(ProviderConnectionsScreen())
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)
        await pilot.press("r")
        await pilot.pause()
        for _ in range(30):
            await pilot.pause()
        assert refreshed == ["opencode_go"]
        status_line = str(screen.query_one("#providers-status").render().plain)
        assert "33" in status_line

        # k opens the masked key editor; a submitted value is typed
        # via real pilot keyboard events and never rendered back.
        await pilot.press("k")
        await pilot.pause()
        from agentic_debugger.ui.screens import MaskedKeyEditorScreen

        editor = pilot.app.screen
        assert isinstance(editor, MaskedKeyEditorScreen)
        from textual.widgets import Input

        inp = editor.query_one("#masked-key-editor", Input)
        assert pilot.app.focused is inp
        assert inp.password is True
        await pilot.press(*list(SECRET))
        await pilot.pause()
        assert inp.value == SECRET
        await pilot.press("enter")
        await pilot.pause()
        assert pc.has_session_key("opencode_go") is True
        for node in screen.query("*"):
            try:
                plain = node.render().plain if hasattr(node, "render") else ""
            except Exception:
                plain = ""
            assert SECRET not in str(plain)

    run_headless(app, actions, size=(100, 30))


def test_start_session_c_binding_opens_provider_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path)
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.connection_statuses", lambda: fake_statuses()
    )

    async def actions(pilot):
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(pilot.app.screen, StartSessionScreen)
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ProviderConnectionsScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, StartSessionScreen)

    run_headless(app, actions, size=(120, 32))


def test_model_picker_shows_discovered_notes_and_management_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path)
    models = (
        ProviderModel(
            "opencode_go",
            "opencode-go/kimi-k3",
            "Kimi K3",
            "OpenCode Go",
            True,
            note=f"direct API · {pc.PROTOCOL_CHAT_COMPLETIONS}",
        ),
        ProviderModel(
            "opencode_go",
            "opencode-go/glm-5",
            "GLM 5",
            "OpenCode Go",
            True,
            note="direct API: protocol not yet resolved",
        ),
        ProviderModel(
            "commandcode_goat",
            "deepseek/deepseek-v4-flash",
            "DeepSeek V4 Flash",
            "CommandCode GOAT",
            False,
            unavailable_reason="no direct API credential — connect in Provider Connections (press c)",
        ),
    )
    monkeypatch.setattr(
        "agentic_debugger.ui.screens.list_provider_models",
        lambda **kwargs: models,
    )
    monkeypatch.setattr(
        app, "ollama_cloud_model_profiles", lambda: ()
    )
    monkeypatch.setattr(
        app,
        "configured_profiles",
        lambda: ((), None),
    )

    async def actions(pilot):
        await pilot.press("s")
        await pilot.pause()
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)
        start._open_model_picker()
        await pilot.pause()
        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)
        values = [choice.value for choice in picker.choices]
        assert "opencode_go:opencode-go/kimi-k3" in values
        assert "opencode_go:opencode-go/glm-5" in values
        assert "commandcode_goat:deepseek/deepseek-v4-flash" in values
        assert "providers:manage" in values
        # Provider identity preserved: same display text never collapses
        # distinct provider routes.
        titles = [choice.title for choice in picker.choices]
        assert "Kimi K3" in titles
        goat = next(
            c for c in picker.choices
            if c.value == "commandcode_goat:deepseek/deepseek-v4-flash"
        )
        assert goat.disabled is True

    run_headless(app, actions, size=(120, 32))


def test_model_picker_management_entry_opens_provider_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path)
    monkeypatch.setattr(
        "agentic_debugger.ui.screens.list_provider_models",
        lambda **kwargs: (),
    )
    monkeypatch.setattr(app, "ollama_cloud_model_profiles", lambda: ())
    monkeypatch.setattr(app, "configured_profiles", lambda: ((), None))
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.connection_statuses", lambda: fake_statuses()
    )

    async def actions(pilot):
        await pilot.press("s")
        await pilot.pause()
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)
        start._open_model_picker()
        await pilot.pause()
        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)
        picker._on_select("providers:manage")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ProviderConnectionsScreen)

    run_headless(app, actions, size=(120, 32))


def test_provider_screen_usable_at_compact_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path)
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.connection_statuses", lambda: fake_statuses()
    )

    async def actions(pilot):
        await pilot.app.push_screen(ProviderConnectionsScreen())
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)
        title = screen.query_one("#providers-title")
        assert title.visible
        region = screen.query_one("#providers-wrap").region
        assert region.width <= 80

    run_headless(app, actions, size=(80, 24))


@pytest.mark.parametrize("size", [(120, 32), (80, 24)])
def test_provider_action_labels_are_visible_in_rendered_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    app = make_app(tmp_path)
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.connection_statuses",
        lambda: fake_statuses(connected_opencode=True, connected_goat=True),
    )

    async def actions(pilot):
        await pilot.app.push_screen(ProviderConnectionsScreen())
        await pilot.pause()
        rendered_svg = unescape(pilot.app.export_screenshot()).replace("\xa0", " ")
        assert "Refresh models" in rendered_svg
        assert "Connect API key" in rendered_svg

    run_headless(app, actions, size=size)



def test_home_screen_m_binding_and_row_opens_providers(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.pause()
        from agentic_debugger.ui.screens import HomeScreen
        assert isinstance(pilot.app.screen, HomeScreen)
        await pilot.press("m")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ProviderConnectionsScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, HomeScreen)

    run_headless(app, actions, size=(100, 30))


def test_add_custom_provider_and_manual_model_dialogs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.provider_configurations_path",
        lambda: config_file,
    )
    _store = {}
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_secure_credential",
        lambda k, v: _store.__setitem__(k, v) or True,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.load_secure_credential",
        lambda k: _store.get(k),
    )
    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.app.push_screen(ProviderConnectionsScreen())
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)

        # Open Add Provider Dialog
        await pilot.press("a")
        await pilot.pause()
        from agentic_debugger.ui.screens import AddProviderDialogScreen, AddManualModelDialogScreen
        from textual.widgets import Input

        dlg = pilot.app.screen
        assert isinstance(dlg, AddProviderDialogScreen)
        dlg.query_one("#input-name", Input).value = "Local LLM"
        dlg.query_one("#input-url", Input).value = "http://localhost:8000/v1"
        dlg.query_one("#input-key", Input).value = "sk-test"
        await pilot.click("#btn-save-dialog")
        await pilot.pause()

        # Check that new provider is now listed in Model Provider Manager
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)
        assert pc.get_provider_config("local_llm") is not None
        assert pc.get_provider_config("local_llm").name == "Local LLM"

        # Open manual model dialog
        screen._action_add_manual_model("local_llm")
        await pilot.pause()
        model_dlg = pilot.app.screen
        assert isinstance(model_dlg, AddManualModelDialogScreen)
        model_dlg.query_one("#input-model-id", Input).value = "custom-llama-3"
        model_dlg.query_one("#input-model-disp", Input).value = "Custom Llama 3"
        await pilot.click("#btn-save-dialog")
        await pilot.pause()

        cfg = pc.get_provider_config("local_llm")
        assert len(cfg.models) == 1
        assert cfg.models[0].model_id == "custom-llama-3"

    run_headless(app, actions, size=(100, 30))


def test_capability_ladder_isolation_with_custom_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom provider models are never eligible for Capability Ladder targets."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.provider_configurations_path",
        lambda: config_file,
    )
    app = make_app(tmp_path)
    pc.add_provider_config(
        name="Custom AI",
        base_url="https://api.custom.ai/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
    )
    pc.add_manual_model("custom_ai", "custom-model-x", "Custom Model X")

    async def actions(pilot):
        await pilot.press("s")
        await pilot.pause()
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)
        # Select Ladder Target
        start._choice_selected("target", "ladder")
        await pilot.pause()
        start._open_model_picker()
        await pilot.pause()
        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)
        custom_choices = [c for c in picker.choices if "custom_ai" in str(c.value)]
        for c in custom_choices:
            # Custom provider models must be disabled / ineligible for Ladder
            assert c.disabled is True
            assert "ladder" in str(c.disabled_reason).lower() or "unavailable" in str(c.disabled_reason).lower()

    run_headless(app, actions, size=(120, 32))


def test_action_buttons_and_compact_footer_rendering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Action buttons render full labels and footer hint adapts to geometry."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.provider_configurations_path",
        lambda: config_file,
    )
    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.press("m")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)

        # Check Edit Provider button label is complete
        edit_btn = screen.query_one("#provider-edit-button-opencode_go")
        assert str(edit_btn.label) == "Edit provider"

        # Check hint on wide screen
        hint = screen.query_one("#providers-hint")
        assert str(hint.render().plain).startswith("↑/↓ select   r refresh models")

        # Test compact resize
        screen._update_hint(80)
        assert str(hint.render().plain).startswith("↑/↓ select   r refresh   k key")

    run_headless(app, actions, size=(100, 30))


def test_add_provider_save_and_discover_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Add Provider dialog has Save & discover button and auto-refreshes on credentialed save."""
    from textual.widgets import Input, Button
    from agentic_debugger.ui.screens import AddProviderDialogScreen

    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.provider_configurations_path",
        lambda: config_file,
    )
    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.press("m")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)

        # Click + Add provider
        await pilot.click("#provider-add-button")
        await pilot.pause()
        add_dlg = pilot.app.screen
        assert isinstance(add_dlg, AddProviderDialogScreen)

        # Verify button label is "Save & discover"
        save_btn = add_dlg.query_one("#btn-save-dialog", Button)
        assert str(save_btn.label) == "Save & discover"

        # Fill valid details
        add_dlg.query_one("#input-name", Input).value = "Fast Inference Corp"
        add_dlg.query_one("#input-url", Input).value = "https://api.fastinference.corp/v1"
        add_dlg.query_one("#input-key", Input).value = "test-fast-key"

        await pilot.click("#btn-save-dialog")
        await pilot.pause()

        # Check that provider was added
        cfg = pc.get_provider_config("fast_inference_corp")
        assert cfg is not None
        assert cfg.name == "Fast Inference Corp"
        assert cfg.base_url == "https://api.fastinference.corp/v1"

    run_headless(app, actions, size=(100, 30))


def test_connect_api_key_modal_displays_user_facing_display_name_and_concise_note(tmp_path: Path) -> None:
    """Connect API key modal displays configured display name and concise security note."""
    from agentic_debugger.ui.screens import MaskedKeyEditorScreen, Static

    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.press("m")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)

        # Select CommandCode GOAT (index 1 in standard list)
        screen._selected_index = 1
        screen.render_state()
        await pilot.press("k")
        await pilot.pause()

        modal = pilot.app.screen
        assert isinstance(modal, MaskedKeyEditorScreen)
        title_static = modal.query_one("#single-line-title", Static)
        assert str(title_static.render().plain) == "Connect API key for CommandCode GOAT"

        note_static = modal.query_one("#single-line-note", Static)
        note_plain = str(note_static.render().plain)
        assert "Saved securely in Windows Credential Manager." in note_plain
        assert "Falls back to session-only memory if unavailable." in note_plain

        await pilot.press("escape")
        await pilot.pause()

    run_headless(app, actions, size=(100, 30))


@pytest.mark.parametrize("geometry", [(120, 32), (100, 30), (80, 24)])
def test_connect_api_key_modal_is_centered_across_geometries(
    tmp_path: Path, geometry: tuple[int, int]
) -> None:
    """Connect API key modal is horizontally and vertically centered at various window sizes."""
    from agentic_debugger.ui.screens import MaskedKeyEditorScreen

    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.press("m")
        await pilot.pause()
        await pilot.press("k")
        await pilot.pause()

        modal = pilot.app.screen
        assert isinstance(modal, MaskedKeyEditorScreen)
        assert modal.styles.align == ("center", "middle")

        dialog = modal.query_one("#single-line-dialog")
        w, h = geometry
        # Dialog region must be bounded and strictly within the viewport
        assert dialog.region.width <= min(70, w)
        assert dialog.region.x >= 0
        assert dialog.region.y >= 0
        assert dialog.region.x + dialog.region.width <= w
        assert dialog.region.y + dialog.region.height <= h

        # Check horizontal centering (margins approximately equal within 1 col)
        left_margin = dialog.region.x
        right_margin = w - (dialog.region.x + dialog.region.width)
        assert abs(left_margin - right_margin) <= 2

        # Check vertical centering (margins approximately equal within 1 row)
        top_margin = dialog.region.y
        bottom_margin = h - (dialog.region.y + dialog.region.height)
        assert abs(top_margin - bottom_margin) <= 2

    run_headless(app, actions, size=geometry)


def test_connect_api_key_commandcode_goat_pilot_typing_and_editing_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production regression: real keyboard typing, backspace editing, and Enter connection for CommandCode GOAT."""
    fake_key = "fake-sk-goat-connect-99"
    received_keys: dict[str, str] = {}
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_secure_credential",
        lambda kind, key: received_keys.__setitem__(kind, key) or True,
    )

    app = make_app(tmp_path)

    async def actions(pilot):
        # 1. Open Model Provider Manager
        await pilot.press("m")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)

        # 2. Select CommandCode GOAT
        await pilot.press("down")
        await pilot.pause()
        assert screen._selected_kind() == "commandcode_goat"

        # 3. Open Connect API key
        await pilot.press("k")
        await pilot.pause()
        from agentic_debugger.ui.screens import MaskedKeyEditorScreen
        from textual.widgets import Input

        modal = pilot.app.screen
        assert isinstance(modal, MaskedKeyEditorScreen)

        # 4. Confirm masked Input has focus
        inp = modal.query_one("#masked-key-editor", Input)
        assert pilot.app.focused is inp
        assert inp.password is True

        # 5. Enter fake API key through actual Textual Pilot keyboard events, NOT by assigning .value,
        # with at least one editing operation (typing extra characters and backspacing them).
        await pilot.press(*list(fake_key + "xyz"))
        await pilot.pause()
        await pilot.press("backspace", "backspace", "backspace")
        await pilot.pause()

        # 6. Prove the Input's internal value became the exact fake key
        assert inp.value == fake_key

        # 7. Prove the rendered UI never exposes the plaintext key
        rendered_svg = unescape(pilot.app.export_screenshot()).replace("\xa0", " ")
        assert fake_key not in rendered_svg
        for node in modal.query("*"):
            try:
                plain = node.render().plain if hasattr(node, "render") else ""
            except Exception:
                plain = ""
            assert fake_key not in str(plain)

        # 8. Press Enter
        await pilot.press("enter")
        await pilot.pause()

        # 9. Prove the credential callback/storage path receives it
        assert pc.has_session_key("commandcode_goat") is True
        assert pc.peek_session_key("commandcode_goat") == fake_key
        assert received_keys.get("commandcode_goat") == fake_key

        # 10. Prove the modal closes normally
        assert isinstance(pilot.app.screen, ProviderConnectionsScreen)
        for node in pilot.app.screen.query("*"):
            try:
                plain = node.render().plain if hasattr(node, "render") else ""
            except Exception:
                plain = ""
            assert fake_key not in str(plain)

    run_headless(app, actions, size=(100, 30))


