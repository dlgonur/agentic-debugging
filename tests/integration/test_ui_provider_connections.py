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
    AddProviderDialogScreen,
    ChoicePickerScreen,
    ConfirmDeleteProviderDialogScreen,
    EditProviderDialogScreen,
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


def test_provider_screen_keyboard_refresh_and_removed_k_shortcut(
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

        # Pressing 'k' must no longer open any key-editor modal; screen remains ProviderConnectionsScreen
        await pilot.press("k")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ProviderConnectionsScreen)

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
        assert "Edit provider" in rendered_svg
        assert "Connect API key" not in rendered_svg

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
    """Action buttons render full labels and footer hint adapts to geometry without obsolete key shortcut."""
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
        assert str(hint.render().plain) == "↑/↓ select   r refresh models   a add provider   e edit provider   d delete provider   esc back"

        # Test compact resize
        screen._update_hint(80)
        assert str(hint.render().plain) == "↑/↓ select   r refresh   a add   e edit   d delete   esc back"
        assert "k key" not in str(hint.render().plain)
        assert "Connect API key" not in str(hint.render().plain)

    run_headless(app, actions, size=(100, 30))


def test_add_provider_save_and_discover_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Add Provider dialog supports real pilot typing into API Key field and auto-refreshes on credentialed save."""
    from textual.widgets import Input, Button

    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.provider_configurations_path",
        lambda: config_file,
    )
    fake_key = "test-fast-pilot-key-88"
    received_keys: dict[str, str] = {}
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_secure_credential",
        lambda kind, key: received_keys.__setitem__(kind, key) or True,
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
        from agentic_debugger.ui.screens import AddProviderDialogScreen
        add_dlg = pilot.app.screen
        assert isinstance(add_dlg, AddProviderDialogScreen)

        # Verify button label is "Save & discover"
        save_btn = add_dlg.query_one("#btn-save-dialog", Button)
        assert str(save_btn.label) == "Save & discover"

        # Fill details
        name_inp = add_dlg.query_one("#input-name", Input)
        name_inp.value = "Fast Inference Corp"
        url_inp = add_dlg.query_one("#input-url", Input)
        url_inp.value = "https://api.fastinference.corp/v1"

        # Type fake key into password field via real Pilot typing
        key_inp = add_dlg.query_one("#input-key", Input)
        assert key_inp.password is True
        key_inp.focus()
        await pilot.pause()
        await pilot.press(*list(fake_key + "123"))
        await pilot.pause()
        await pilot.press("backspace", "backspace", "backspace")
        await pilot.pause()
        assert key_inp.value == fake_key

        # Plaintext fake key never rendered in UI/screenshot
        rendered_svg = unescape(pilot.app.export_screenshot()).replace("\xa0", " ")
        assert fake_key not in rendered_svg

        await pilot.click("#btn-save-dialog")
        await pilot.pause()

        # Check that provider was added and credential saved
        cfg = pc.get_provider_config("fast_inference_corp")
        assert cfg is not None
        assert cfg.name == "Fast Inference Corp"
        assert cfg.base_url == "https://api.fastinference.corp/v1"

    run_headless(app, actions, size=(100, 30))


def test_edit_provider_commandcode_goat_pilot_typing_and_credential_preservation_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance regression: CommandCode GOAT -> Edit provider exposes API key input, real typing works,

    saving reaches secure storage, secret is never rendered, reopening displays status without secret,
    and editing unrelated fields with blank API key preserves the saved credential.
    """
    from textual.widgets import Input

    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.provider_configurations_path",
        lambda: config_file,
    )
    fake_key = "fake-sk-goat-edit-key-42"
    secure_store: dict[str, str] = {}
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_secure_credential",
        lambda kind, key: secure_store.__setitem__(kind, key) or True,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.load_secure_credential",
        lambda kind: secure_store.get(kind),
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.has_secure_credential",
        lambda kind: kind in secure_store,
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

        # 3. Open Edit provider via 'e' key
        await pilot.press("e")
        await pilot.pause()
        edit_dlg = pilot.app.screen
        assert isinstance(edit_dlg, EditProviderDialogScreen)

        # 4. Confirm Edit provider exposes API Key input with password=True
        inp_key = edit_dlg.query_one("#input-key", Input)
        assert inp_key.password is True
        assert inp_key.value == ""  # Never prepopulates existing secret

        # 5. Type fake API key via real Textual Pilot keyboard events with editing (typing extra + backspacing)
        inp_key.focus()
        await pilot.pause()
        await pilot.press(*list(fake_key + "xyz99"))
        await pilot.pause()
        await pilot.press("backspace", "backspace", "backspace", "backspace", "backspace")
        await pilot.pause()
        assert inp_key.value == fake_key

        # 6. Prove plaintext fake key never appears in rendered UI or screenshot
        rendered_svg = unescape(pilot.app.export_screenshot()).replace("\xa0", " ")
        assert fake_key not in rendered_svg
        for node in edit_dlg.query("*"):
            try:
                plain = node.render().plain if hasattr(node, "render") else ""
            except Exception:
                plain = ""
            assert fake_key not in str(plain)

        # 7. Submit via Enter
        await pilot.press("enter")
        await pilot.pause()

        # 8. Prove the newly entered fake API key reached secure credential storage & session
        assert pc.has_session_key("commandcode_goat") is True
        assert pc.peek_session_key("commandcode_goat") == fake_key
        assert secure_store.get("commandcode_goat") == fake_key
        assert isinstance(pilot.app.screen, ProviderConnectionsScreen)

        # 9. Reopen Edit provider
        await pilot.press("e")
        await pilot.pause()
        edit_dlg_2 = pilot.app.screen
        assert isinstance(edit_dlg_2, EditProviderDialogScreen)

        # 10. Confirm credential status without exposing the secret
        status_static = edit_dlg_2.query_one("#dialog-credential-status")
        status_text = str(status_static.render().plain)
        assert "Credential: saved securely" in status_text
        assert fake_key not in status_text

        inp_key_2 = edit_dlg_2.query_one("#input-key", Input)
        assert inp_key_2.value == ""  # Never prepopulates existing secret

        # 11. Edit unrelated field (Base URL) while leaving API key field blank
        inp_url_2 = edit_dlg_2.query_one("#input-url", Input)
        inp_url_2.value = "https://api.commandcode.ai/provider/v2"

        # 12. Save changes
        await pilot.click("#btn-save-dialog")
        await pilot.pause()

        # 13. Prove existing credential was PRESERVED after editing with blank API Key
        assert pc.has_session_key("commandcode_goat") is True
        assert pc.peek_session_key("commandcode_goat") == fake_key
        assert secure_store.get("commandcode_goat") == fake_key
        cfg = pc.get_provider_config("commandcode_goat")
        assert cfg is not None
        assert cfg.base_url == "https://api.commandcode.ai/provider/v2"

    run_headless(app, actions, size=(100, 30))


@pytest.mark.parametrize("geometry", [(120, 32), (100, 30)])
def test_edit_provider_dialog_is_centered_across_geometries(
    tmp_path: Path, geometry: tuple[int, int]
) -> None:
    """Edit provider dialog is horizontally centered at standard window sizes."""
    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.press("m")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()

        modal = pilot.app.screen
        assert isinstance(modal, EditProviderDialogScreen)
        assert modal.styles.align == ("center", "middle")

        dialog = modal.query_one("#provider-dialog-card")
        w, h = geometry
        assert dialog.region.width <= min(70, w)
        assert dialog.region.x >= 0

        # Check horizontal centering (margins approximately equal within 2 cols)
        left_margin = dialog.region.x
        right_margin = w - (dialog.region.x + dialog.region.width)
        assert abs(left_margin - right_margin) <= 2

    run_headless(app, actions, size=geometry)


def test_no_standalone_credential_modal_reachable(tmp_path: Path) -> None:
    """Acceptance regression: Provider Manager no longer renders Connect API key and no credential modal remains."""
    import agentic_debugger.ui.screens as screens_module
    assert not hasattr(screens_module, "MaskedKeyEditorScreen")

    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.press("m")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)

        # No button with 'Connect API key' exists
        rendered_svg = unescape(pilot.app.export_screenshot()).replace("\xa0", " ")
        assert "Connect API key" not in rendered_svg

        # No provider-key-button-* id exists
        for btn in screen.query("Button"):
            assert not (btn.id or "").startswith("provider-key-button-")

        # Pressing 'k' does not open any modal
        await pilot.press("k")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ProviderConnectionsScreen)

    run_headless(app, actions, size=(100, 30))


def test_refresh_without_credential_error_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance regression: refresh failure guidance points to Edit provider, not obsolete Connect API key."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.provider_configurations_path",
        lambda: config_file,
    )
    monkeypatch.delenv("COMMAND_CODE_API_KEY", raising=False)
    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.press("m")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)

        # Select CommandCode GOAT
        await pilot.press("down")
        await pilot.pause()
        assert screen._selected_kind() == "commandcode_goat"

        # Refresh without credential
        await pilot.press("r")
        await pilot.pause()
        for _ in range(30):
            await pilot.pause()

        status_text = str(screen.query_one("#providers-status").render().plain)
        assert "edit provider to add an api key" in status_text.lower()
        assert "connect api key" not in status_text.lower()
        assert "connect an api key" not in status_text.lower()

        # Add an unconnected provider without models to verify empty models list copy
        await pilot.press("a")
        await pilot.pause()
        from textual.widgets import Input
        add_dlg = pilot.app.screen
        add_dlg.query_one("#input-name", Input).value = "Unconnected Provider"
        add_dlg.query_one("#input-url", Input).value = "https://api.unconnected.test/v1"
        await pilot.click("#btn-save-dialog")
        await pilot.pause()

        new_screen = pilot.app.screen
        models_text = str(new_screen.query_one("#provider-models-list-unconnected_provider").render().plain)
        assert "edit provider to add an api key" in models_text.lower()
        assert "connect api key" not in models_text.lower()

    run_headless(app, actions, size=(100, 30))


def test_commandcode_goat_62_models_presentation_and_scrolling_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truthful model-list presentation communicates all 62 discovered models and supports independent scroll navigation."""
    from textual.containers import VerticalScroll

    models_62 = tuple(
        pc.DiscoveredProviderModel(
            kind="commandcode_goat",
            model_id=f"deepseek/deepseek-v4-model-{i:02d}",
            display_name=f"DeepSeek V4 Model {i:02d}",
            protocol="chat_completions",
            runnable=True,
        )
        for i in range(1, 63)
    )

    def _status_with_62_models():
        return [
            pc.ProviderConnectionStatus(
                kind="commandcode_goat",
                label="CommandCode GOAT",
                base_url="https://api.commandcode.ai/provider/v1",
                connected=True,
                credential_source="saved",
                model_count=62,
                last_refresh_utc="2026-08-31T12:00:00Z",
                last_refresh_source="live",
                stale=False,
                status_message=None,
                cached_models=models_62,
                is_builtin=True,
            )
        ]

    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.connection_statuses",
        _status_with_62_models,
    )
    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.app.push_screen(ProviderConnectionsScreen())
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)

        # 1. Verify truthful helper copy indicates more discovered models than visible rows
        helper = screen.query_one("#provider-models-helper-commandcode_goat")
        helper_text = str(helper.render().plain)
        assert "62 discovered models — scroll to view more" in helper_text

        # 2. Verify all 62 models are in the models list text (not truncated to 24)
        models_widget = screen.query_one("#provider-models-list-commandcode_goat")
        list_text = str(models_widget.render().plain)
        assert "DeepSeek V4 Model 01" in list_text
        assert "DeepSeek V4 Model 24" in list_text
        assert "DeepSeek V4 Model 25" in list_text
        assert "DeepSeek V4 Model 62" in list_text

        # 3. Verify models box is a focusable scrollable widget
        models_box = screen.query_one("#provider-models-box-commandcode_goat", VerticalScroll)
        assert models_box.can_focus is True

        # 4. Focus models box and test keyboard arrow scrolling navigation
        models_box.focus()
        await pilot.pause()
        assert screen.focused == models_box

        # Scroll down through models
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause()
        assert models_box.scroll_y > 0

        # Page down and end navigation
        await pilot.press("pagedown")
        await pilot.pause()
        assert models_box.scroll_y >= 3

        await pilot.press("end")
        await pilot.pause()
        assert models_box.scroll_y > 10

    run_headless(app, actions, size=(100, 30))


def test_custom_provider_delete_confirmation_and_cancel_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a custom provider opens a confirmation dialog that can be cancelled without deleting."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.provider_configurations_path",
        lambda: config_file,
    )
    pc.add_provider_config(
        name="Groq Direct Test",
        base_url="https://api.groq.com/openai/v1",
        api_format="chat_completions",
    )
    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.app.push_screen(ProviderConnectionsScreen())
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)

        # Select custom provider
        screen._selected_index = screen._index_of("groq_direct_test")
        screen.render_state()
        await pilot.pause()

        # Delete button is enabled for custom provider
        del_btn = screen.query_one("#provider-delete-button-groq_direct_test")
        assert del_btn.disabled is False
        assert "Delete provider" in str(del_btn.label)

        # Press 'd' to open confirmation dialog
        await pilot.press("d")
        await pilot.pause()
        confirm_screen = pilot.app.screen
        assert isinstance(confirm_screen, ConfirmDeleteProviderDialogScreen)
        assert "DELETE PROVIDER: Groq Direct Test" in str(confirm_screen.query_one("#dialog-title").render().plain)

        # Cancel deletion
        await pilot.click("#btn-cancel-dialog")
        await pilot.pause()

        # Verify back on provider screen and provider was NOT deleted
        assert isinstance(pilot.app.screen, ProviderConnectionsScreen)
        assert pc.get_provider_config("groq_direct_test") is not None

    run_headless(app, actions, size=(100, 30))


def test_custom_provider_delete_confirmed_removes_config_and_updates_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirming custom provider deletion removes config, secure credential, and safely updates selection."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.provider_configurations_path",
        lambda: config_file,
    )
    store = {}
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_secure_credential",
        lambda k, v: store.__setitem__(k, v) or True,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.load_secure_credential",
        lambda k: store.get(k),
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.delete_secure_credential",
        lambda k: store.pop(k, None) is not None,
    )

    pc.add_provider_config(
        name="Temporary Custom",
        base_url="https://api.temp.test/v1",
        api_format="chat_completions",
        api_key="secret-temp-key-1234",
    )
    assert pc.get_provider_config("temporary_custom") is not None
    assert store.get("temporary_custom") == "secret-temp-key-1234"

    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.app.push_screen(ProviderConnectionsScreen())
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)

        # Select temporary_custom
        screen._selected_index = screen._index_of("temporary_custom")
        screen.render_state()
        await pilot.pause()

        # Click delete button
        await pilot.click("#provider-delete-button-temporary_custom")
        await pilot.pause()
        confirm_screen = pilot.app.screen
        assert isinstance(confirm_screen, ConfirmDeleteProviderDialogScreen)

        # Confirm deletion
        await pilot.click("#btn-confirm-delete")
        await pilot.pause()

        # Verify provider and credentials are completely removed
        assert pc.get_provider_config("temporary_custom") is None
        assert "temporary_custom" not in store
        assert pc.has_session_key("temporary_custom") is False

        # Verify UI remains stable and selection is safely updated
        active_screen = pilot.app.screen
        assert isinstance(active_screen, ProviderConnectionsScreen)
        status_msg = str(active_screen.query_one("#providers-status").render().plain)
        assert "Deleted provider 'Temporary Custom'" in status_msg

    run_headless(app, actions, size=(100, 30))


def test_builtin_provider_delete_is_protected_in_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Built-in providers display protected status on delete action and reject deletion."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.provider_configurations_path",
        lambda: config_file,
    )
    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.app.push_screen(ProviderConnectionsScreen())
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProviderConnectionsScreen)

        # Select built-in OpenCode Go
        screen._selected_index = screen._index_of("opencode_go")
        screen.render_state()
        await pilot.pause()

        # Verify delete button is disabled and marked protected
        del_btn = screen.query_one("#provider-delete-button-opencode_go")
        assert del_btn.disabled is True
        assert "Delete (protected)" in str(del_btn.label)

        # Pressing 'd' produces truthful protected status feedback
        await pilot.press("d")
        await pilot.pause()

        # No confirm dialog opened; status message shows protection feedback
        assert isinstance(pilot.app.screen, ProviderConnectionsScreen)
        status_msg = str(screen.query_one("#providers-status").render().plain)
        assert "protected and cannot be deleted" in status_msg
        assert pc.get_provider_config("opencode_go") is not None

    run_headless(app, actions, size=(100, 30))



