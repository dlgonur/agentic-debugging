"""Provider catalog browser and the provider-management dialogs.

This module owns the read-only model-catalog browser and the
add/edit/manual-model/delete-confirmation dialog screens.  The dialogs
are only ever opened by the provider manager screen
(:mod:`agentic_debugger.ui.screens_providers`); they are
presentation-only and never hold credentials.
"""

from __future__ import annotations

from functools import partial
from typing import Any, Callable, Optional, Tuple

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, OptionList, Static

from agentic_debugger.ui.theme import FAINT, FOREGROUND, MUTED, PRIMARY

from agentic_debugger.application.provider_connections import DiscoveredProviderModel


_PROVIDER_CREDENTIAL_SOURCE_LABELS = {
    "saved": "saved",
    "session_key": "session only",
    "environment": "environment",
    "cli_auth_store": "CLI auth",
}

class ModelCatalogBrowserScreen(Screen):
    """Dedicated searchable catalog browser for a selected model provider."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
    ]

    def __init__(
        self,
        provider_id: str,
        provider_name: str,
        models: Tuple[DiscoveredProviderModel, ...] = (),
    ) -> None:
        super().__init__()
        self._provider_id = provider_id
        self._provider_name = provider_name
        self._all_models = list(models)
        self._filtered_models = list(models)
        self._filter_text = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="catalog-browser-card"):
            with Horizontal(id="catalog-browser-header"):
                yield Static(f"MODEL CATALOG — {self._provider_name.upper()}", id="catalog-title")
                yield Static(f"{len(self._all_models)} models", id="catalog-count")
            yield Input(placeholder="Search / filter models (type name or ID)...", id="catalog-filter-input")
            yield OptionList(id="catalog-models-list")
            with Horizontal(id="catalog-browser-footer"):
                yield Static("↑/↓ navigate   pgup/pgdn page   esc close", id="catalog-browser-hint")
                yield Button("Close", id="btn-close-catalog", classes="catalog-close-btn")

    def on_mount(self) -> None:
        self._populate_list()
        self.query_one("#catalog-filter-input", Input).focus()

    def _populate_list(self) -> None:
        from textual.widgets.option_list import Option

        opt_list = self.query_one("#catalog-models-list", OptionList)
        opt_list.clear_options()

        count_widget = self.query_one("#catalog-count", Static)
        if self._filter_text:
            count_widget.update(f"{len(self._filtered_models)} of {len(self._all_models)} models")
        else:
            count_widget.update(f"{len(self._all_models)} models available")

        if not self._filtered_models:
            if self._all_models:
                opt_list.add_option(
                    Option(Text(f"No models match '{self._filter_text}'", style=FAINT), disabled=True)
                )
            else:
                opt_list.add_option(
                    Option(Text("No models discovered in catalog yet.", style=FAINT), disabled=True)
                )
            return

        for m in self._filtered_models:
            t = Text()
            t.append(f"{m.display_name:<34}", style=f"bold {FOREGROUND}")
            t.append(f"  {m.model_id}", style=f"{MUTED}")
            if m.protocol:
                t.append(f" [{m.protocol}]", style=FAINT)
            opt_list.add_option(Option(t, id=f"model::{m.model_id}"))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "catalog-filter-input":
            self._filter_text = event.value.strip().lower()
            if self._filter_text:
                self._filtered_models = [
                    m
                    for m in self._all_models
                    if self._filter_text in m.model_id.lower()
                    or self._filter_text in m.display_name.lower()
                ]
            else:
                self._filtered_models = list(self._all_models)
            self._populate_list()
            event.stop()

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_button_pressed(self, event: Any) -> None:
        btn_id = getattr(event.button, "id", "")
        if btn_id == "btn-close-catalog":
            self.action_back()
            event.stop()


class AddProviderDialogScreen(Screen):
    """Dialog for adding a new generic model provider."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, on_save: Callable[[Any], None]) -> None:
        super().__init__()
        self._on_save = on_save
        self._format = "chat_completions"
        self._auth = "bearer"
        self._catalog = "openai"
        self._profile = "generic"

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-dialog-card"):
            yield Static("ADD MODEL PROVIDER", id="dialog-title")
            yield Static("Provider Name", classes="dialog-label")
            yield Input(placeholder="e.g. Groq Direct or DeepSeek V3", id="input-name")
            yield Static("Base URL", classes="dialog-label")
            yield Input(placeholder="https://api.groq.com/openai/v1", id="input-url")
            yield Static("API Key (optional for no-auth loopback; stored securely)", classes="dialog-label")
            yield Input(password=True, placeholder="API key", id="input-key")
            yield Static("API Protocol Format", classes="dialog-label")
            with Horizontal(id="format-buttons-row"):
                yield Button("Chat Completions", id="fmt-chat", classes="fmt-btn -selected")
                yield Button("Responses", id="fmt-resp", classes="fmt-btn")
                yield Button("Messages", id="fmt-msg", classes="fmt-btn")
            yield Static("Authentication", classes="dialog-label")
            with Horizontal(id="auth-buttons-row"):
                yield Button("Bearer", id="auth-bearer", classes="fmt-btn -selected")
                yield Button("Anthropic", id="auth-anthropic", classes="fmt-btn")
                yield Button("None (loopback)", id="auth-none", classes="fmt-btn")
            yield Static("Catalog", classes="dialog-label")
            with Horizontal(id="catalog-buttons-row"):
                yield Button("Auto (/models)", id="cat-auto", classes="fmt-btn -selected")
                yield Button("Manual only", id="cat-manual", classes="fmt-btn")
            yield Static("Endpoint contract", classes="dialog-label")
            with Horizontal(id="profile-buttons-row"):
                yield Button("Generic / OpenAI-compatible", id="prof-generic", classes="fmt-btn -selected")
                yield Button("CommandCode", id="prof-commandcode", classes="fmt-btn")
                yield Button("OpenCode", id="prof-opencode", classes="fmt-btn")
                yield Button("Ollama", id="prof-ollama", classes="fmt-btn")
            yield Static("", id="dialog-feedback")
            with Horizontal(id="dialog-actions-row"):
                yield Button("Save & discover", id="btn-save-dialog", classes="primary-action")
                yield Button("Cancel", id="btn-cancel-dialog", classes="secondary-action")

    def on_mount(self) -> None:
        self.query_one("#input-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._do_save()
        event.stop()

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_save(None)

    def on_button_pressed(self, event: Any) -> None:
        btn_id = getattr(event.button, "id", "")
        if btn_id == "fmt-chat":
            self._format = "chat_completions"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "fmt-resp":
            self._format = "responses"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "fmt-msg":
            self._format = "messages"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "auth-bearer":
            self._auth = "bearer"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "auth-anthropic":
            self._auth = "anthropic"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "auth-none":
            self._auth = "none"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "cat-auto":
            self._catalog = "openai"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "cat-manual":
            self._catalog = "disabled"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "prof-generic":
            self._profile = "generic"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "prof-ollama":
            self._profile = "ollama_cloud"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "prof-opencode":
            self._profile = "opencode_go"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "prof-commandcode":
            self._profile = "commandcode_goat"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "btn-cancel-dialog":
            self.action_cancel()
            event.stop()
        elif btn_id == "btn-save-dialog":
            self._do_save()
            event.stop()

    def _update_format_buttons(self) -> None:
        for fid, val in [("fmt-chat", "chat_completions"), ("fmt-resp", "responses"), ("fmt-msg", "messages")]:
            try:
                b = self.query_one(f"#{fid}", Button)
                if self._format == val:
                    b.add_class("-selected")
                else:
                    b.remove_class("-selected")
            except Exception:
                pass
        for fid, val in [("prof-generic", "generic"), ("prof-ollama", "ollama_cloud"), ("prof-opencode", "opencode_go"), ("prof-commandcode", "commandcode_goat")]:
            try:
                b = self.query_one(f"#{fid}", Button)
                if self._profile == val:
                    b.add_class("-selected")
                else:
                    b.remove_class("-selected")
            except Exception:
                pass
        for fid, val in [("auth-bearer", "bearer"), ("auth-anthropic", "anthropic"), ("auth-none", "none")]:
            try:
                b = self.query_one(f"#{fid}", Button)
                if self._auth == val:
                    b.add_class("-selected")
                else:
                    b.remove_class("-selected")
            except Exception:
                pass
        for fid, val in [("cat-auto", "openai"), ("cat-manual", "disabled")]:
            try:
                b = self.query_one(f"#{fid}", Button)
                if self._catalog == val:
                    b.add_class("-selected")
                else:
                    b.remove_class("-selected")
            except Exception:
                pass

    def _do_save(self) -> None:
        name = self.query_one("#input-name", Input).value.strip()
        url = self.query_one("#input-url", Input).value.strip()
        key = self.query_one("#input-key", Input).value.strip()
        feedback = self.query_one("#dialog-feedback", Static)

        if not name:
            feedback.update("[red]Provider name is required[/]")
            return
        if not url:
            feedback.update("[red]Base URL is required[/]")
            return
        from agentic_debugger.application.provider_connections import add_provider_config
        try:
            cfg = add_provider_config(
                name=name,
                base_url=url,
                api_format=self._format,
                api_key=key or None,
                auth_mode=self._auth,
                catalog_mode=self._catalog,
                transport_profile=self._profile,
            )
            # V2-04: the raw key lived only in this widget; drop it as soon
            # as the vault-owned save succeeded (never echoed back, never
            # pre-filled into an edit dialog).
            self.query_one("#input-key", Input).value = ""
            self.app.pop_screen()
            self._on_save(cfg)
        except Exception as exc:
            feedback.update(f"[red]Error: {exc}[/]")


class EditProviderDialogScreen(Screen):
    """Dialog for editing an existing model provider."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, config: Any, on_save: Callable[[Any], None]) -> None:
        super().__init__()
        self._config = config
        self._on_save = on_save
        self._format = config.api_format
        self._auth = getattr(config, "auth_mode", "bearer")
        self._catalog = getattr(config, "catalog_mode", "openai")
        self._profile = getattr(config, "transport_profile", "generic")

    def compose(self) -> ComposeResult:
        from agentic_debugger.application.model_gateway import ModelGateway
        from agentic_debugger.application.provider_connections import (
            CREDENTIAL_SOURCE_SAVED,
            CREDENTIAL_SOURCE_SESSION_KEY,
            CREDENTIAL_SOURCE_ENVIRONMENT,
            CREDENTIAL_SOURCE_CLI_AUTH_STORE,
        )
        from agentic_debugger.application.credential_vault import (
            CREDENTIAL_SOURCE_EXTERNAL_CLI,
        )

        try:
            readiness = ModelGateway.default().credential_readiness(
                self._config.provider_id
            )
            source = readiness.source_kind
            recovery_required = readiness.recovery_required
        except Exception:
            source = None
            recovery_required = False
        if recovery_required:
            cred_status = "Credential: recovery required (re-enter the API key)"
        elif source == CREDENTIAL_SOURCE_SAVED:
            cred_status = "Credential: saved securely"
        elif source == CREDENTIAL_SOURCE_SESSION_KEY:
            cred_status = "Credential: session only"
        elif source == CREDENTIAL_SOURCE_ENVIRONMENT:
            cred_status = "Credential: environment variable"
        elif source == CREDENTIAL_SOURCE_CLI_AUTH_STORE:
            cred_status = "Credential: CLI auth (read in place)"
        elif source == CREDENTIAL_SOURCE_EXTERNAL_CLI:
            cred_status = "Credential: CLI auth (external)"
        else:
            if getattr(self._config, "auth_mode", "bearer") == "none":
                cred_status = "Credential: none required (loopback)"
            else:
                cred_status = "Credential: none configured"

        with Vertical(id="provider-dialog-card"):
            yield Static(f"EDIT PROVIDER: {self._config.name}", id="dialog-title")
            yield Static("Provider Name", classes="dialog-label")
            yield Input(value=self._config.name, id="input-name")
            yield Static("Base URL", classes="dialog-label")
            yield Input(value=self._config.base_url, id="input-url")
            yield Static("API Key (blank keeps current; re-enter when Base URL changes)", classes="dialog-label")
            yield Input(password=True, placeholder="new API key", id="input-key")
            yield Static(cred_status, id="dialog-credential-status", classes="dialog-cred-status")
            yield Static("API Protocol Format", classes="dialog-label")
            with Horizontal(id="format-buttons-row"):
                yield Button("Chat Completions", id="fmt-chat", classes=f"fmt-btn {'-selected' if self._format == 'chat_completions' else ''}")
                yield Button("Responses", id="fmt-resp", classes=f"fmt-btn {'-selected' if self._format == 'responses' else ''}")
                yield Button("Messages", id="fmt-msg", classes=f"fmt-btn {'-selected' if self._format == 'messages' else ''}")
            yield Static("Authentication", classes="dialog-label")
            with Horizontal(id="auth-buttons-row"):
                yield Button("Bearer", id="auth-bearer", classes=f"fmt-btn {'-selected' if self._auth == 'bearer' else ''}")
                yield Button("Anthropic", id="auth-anthropic", classes=f"fmt-btn {'-selected' if self._auth == 'anthropic' else ''}")
                yield Button("None (loopback)", id="auth-none", classes=f"fmt-btn {'-selected' if self._auth == 'none' else ''}")
            yield Static("Catalog", classes="dialog-label")
            with Horizontal(id="catalog-buttons-row"):
                yield Button("Auto (/models)", id="cat-auto", classes=f"fmt-btn {'-selected' if self._catalog == 'openai' else ''}")
                yield Button("Manual only", id="cat-manual", classes=f"fmt-btn {'-selected' if self._catalog == 'disabled' else ''}")
            yield Static("Endpoint contract", classes="dialog-label")
            with Horizontal(id="profile-buttons-row"):
                yield Button("Generic / OpenAI-compatible", id="prof-generic", classes=f"fmt-btn {'-selected' if self._profile == 'generic' else ''}")
                yield Button("CommandCode", id="prof-commandcode", classes=f"fmt-btn {'-selected' if self._profile == 'commandcode_goat' else ''}")
                yield Button("OpenCode", id="prof-opencode", classes=f"fmt-btn {'-selected' if self._profile == 'opencode_go' else ''}")
                yield Button("Ollama", id="prof-ollama", classes=f"fmt-btn {'-selected' if self._profile == 'ollama_cloud' else ''}")
            yield Static("", id="dialog-feedback")
            with Horizontal(id="dialog-actions-row"):
                yield Button("Save changes", id="btn-save-dialog", classes="primary-action")
                yield Button("Cancel", id="btn-cancel-dialog", classes="secondary-action")

    def on_mount(self) -> None:
        self.query_one("#input-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._do_save()
        event.stop()

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_save(None)

    def on_button_pressed(self, event: Any) -> None:
        btn_id = getattr(event.button, "id", "")
        if btn_id == "fmt-chat":
            self._format = "chat_completions"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "fmt-resp":
            self._format = "responses"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "fmt-msg":
            self._format = "messages"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "auth-bearer":
            self._auth = "bearer"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "auth-anthropic":
            self._auth = "anthropic"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "auth-none":
            self._auth = "none"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "cat-auto":
            self._catalog = "openai"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "cat-manual":
            self._catalog = "disabled"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "prof-generic":
            self._profile = "generic"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "prof-ollama":
            self._profile = "ollama_cloud"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "prof-opencode":
            self._profile = "opencode_go"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "prof-commandcode":
            self._profile = "commandcode_goat"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "btn-cancel-dialog":
            self.action_cancel()
            event.stop()
        elif btn_id == "btn-save-dialog":
            self._do_save()
            event.stop()

    def _update_format_buttons(self) -> None:
        for fid, val in [("fmt-chat", "chat_completions"), ("fmt-resp", "responses"), ("fmt-msg", "messages")]:
            try:
                b = self.query_one(f"#{fid}", Button)
                if self._format == val:
                    b.add_class("-selected")
                else:
                    b.remove_class("-selected")
            except Exception:
                pass
        for fid, val in [("prof-generic", "generic"), ("prof-ollama", "ollama_cloud"), ("prof-opencode", "opencode_go"), ("prof-commandcode", "commandcode_goat")]:
            try:
                b = self.query_one(f"#{fid}", Button)
                if self._profile == val:
                    b.add_class("-selected")
                else:
                    b.remove_class("-selected")
            except Exception:
                pass
        for fid, val in [("auth-bearer", "bearer"), ("auth-anthropic", "anthropic"), ("auth-none", "none")]:
            try:
                b = self.query_one(f"#{fid}", Button)
                if self._auth == val:
                    b.add_class("-selected")
                else:
                    b.remove_class("-selected")
            except Exception:
                pass
        for fid, val in [("cat-auto", "openai"), ("cat-manual", "disabled")]:
            try:
                b = self.query_one(f"#{fid}", Button)
                if self._catalog == val:
                    b.add_class("-selected")
                else:
                    b.remove_class("-selected")
            except Exception:
                pass

    def _do_save(self) -> None:
        name = self.query_one("#input-name", Input).value.strip()
        url = self.query_one("#input-url", Input).value.strip()
        key = self.query_one("#input-key", Input).value.strip()
        feedback = self.query_one("#dialog-feedback", Static)

        if not name:
            feedback.update("[red]Provider name is required[/]")
            return
        if not url:
            feedback.update("[red]Base URL is required[/]")
            return
        from agentic_debugger.application.provider_connections import update_provider_config
        try:
            cfg = update_provider_config(
                provider_id=self._config.provider_id,
                name=name,
                base_url=url,
                api_format=self._format,
                api_key=key or None,
                auth_mode=self._auth,
                catalog_mode=self._catalog,
                transport_profile=self._profile,
            )
            # V2-04: the raw key lived only in this widget; drop it as soon
            # as the vault-owned save succeeded (never echoed back, never
            # pre-filled into an edit dialog).
            self.query_one("#input-key", Input).value = ""
            self.app.pop_screen()
            self._on_save(cfg)
        except Exception as exc:
            feedback.update(f"[red]Error: {exc}[/]")


class AddManualModelDialogScreen(Screen):
    """Dialog for manually adding a model identifier to a provider."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, provider_id: str, on_save: Callable[[Optional[str], Optional[str], Optional[str]], None]) -> None:
        super().__init__()
        self._provider_id = provider_id
        self._on_save = on_save

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-dialog-card"):
            yield Static(f"ADD MANUAL MODEL ({self._provider_id})", id="dialog-title")
            yield Static("Model ID (sent to API)", classes="dialog-label")
            yield Input(placeholder="e.g. llama-3.3-70b-versatile or claude-3-7-sonnet", id="input-model-id")
            yield Static("Display Name (optional)", classes="dialog-label")
            yield Input(placeholder="e.g. Llama 3.3 70B", id="input-model-disp")
            yield Static("", id="dialog-feedback")
            with Horizontal(id="dialog-actions-row"):
                yield Button("Add model", id="btn-save-dialog", classes="primary-action")
                yield Button("Cancel", id="btn-cancel-dialog", classes="secondary-action")

    def on_mount(self) -> None:
        self.query_one("#input-model-id", Input).focus()

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_save(None, None, None)

    def on_button_pressed(self, event: Any) -> None:
        btn_id = getattr(event.button, "id", "")
        if btn_id == "btn-cancel-dialog":
            self.action_cancel()
            event.stop()
        elif btn_id == "btn-save-dialog":
            mid = self.query_one("#input-model-id", Input).value.strip()
            disp = self.query_one("#input-model-disp", Input).value.strip()
            feedback = self.query_one("#dialog-feedback", Static)
            if not mid:
                feedback.update("[red]Model ID is required[/]")
                return
            self.app.pop_screen()
            self._on_save(mid, disp or None, None)
            event.stop()


class ConfirmDeleteProviderDialogScreen(Screen):
    """Dialog for confirming deletion of a custom model provider."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        provider_id: str,
        provider_name: str,
        on_confirm: Callable[[bool], None],
    ) -> None:
        super().__init__()
        self._provider_id = provider_id
        self._provider_name = provider_name
        self._on_confirm = on_confirm

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-dialog-card"):
            yield Static(f"DELETE PROVIDER: {self._provider_name}", id="dialog-title")
            yield Static(
                f"Are you sure you want to delete '{self._provider_name}' ({self._provider_id})?\n\n"
                "This will remove its persisted configuration and any securely stored credentials.",
                classes="dialog-label",
            )
            yield Static("", id="dialog-feedback")
            with Horizontal(id="dialog-actions-row"):
                yield Button("Delete provider", id="btn-confirm-delete", classes="danger-action")
                yield Button("Cancel", id="btn-cancel-dialog", classes="secondary-action")

    def on_mount(self) -> None:
        self.query_one("#btn-cancel-dialog", Button).focus()

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_confirm(False)

    def on_button_pressed(self, event: Any) -> None:
        btn_id = getattr(event.button, "id", "")
        if btn_id == "btn-confirm-delete":
            self.app.pop_screen()
            self._on_confirm(True)
            event.stop()
        elif btn_id == "btn-cancel-dialog":
            self.action_cancel()
            event.stop()


# Backward compatibility alias
