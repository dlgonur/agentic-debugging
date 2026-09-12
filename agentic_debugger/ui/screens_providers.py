"""Provider-management workflow screens for the Agentic Debugger TUI.

The provider manager (sidebar + detail/catalog summary with refresh,
browse, add, edit, delete dispatch) stays in this module as the one
provider-screen orchestration authority.  The read-only model-catalog
browser and the add/edit/manual-model/delete-confirmation dialogs live
in :mod:`agentic_debugger.ui.provider_dialogs`.

Screens are presentation-only: provider registry and vault access goes
through the application boundary and deferred application-layer imports;
no screen holds credentials.
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

from agentic_debugger.ui.provider_dialogs import (
    AddManualModelDialogScreen,
    AddProviderDialogScreen,
    ConfirmDeleteProviderDialogScreen,
    EditProviderDialogScreen,
    ModelCatalogBrowserScreen,
)
from agentic_debugger.ui.theme import FAINT, FOREGROUND, MUTED, PRIMARY


_PROVIDER_CREDENTIAL_SOURCE_LABELS = {
    "saved": "saved",
    "session_key": "session only",
    "environment": "environment",
    "cli_auth_store": "CLI auth",
}

class ModelProvidersScreen(Screen):
    """First-class Model Provider Manager: endpoints, credentials, and models.

    Operational surface:
    - Left column: provider sidebar with statuses + '+ Add provider'
    - Right column: active provider details, credential status, concise model catalog
      summary with dedicated Browse models browser, manual model fallback, edit/delete.
    - Fully centered, substantial desktop geometry (width ~98 cols) and responsive
      adaptation on compact screens.
    """

    PROVIDERS_HINT = (
        "↑/↓ select   b browse models   r refresh models   a add provider   e edit provider   d delete provider   esc back"
    )
    PROVIDERS_HINT_COMPACT = (
        "↑/↓ select   b browse   r refresh   a add   e edit   d delete   esc back"
    )

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("up", "select_previous", "Previous provider", show=False),
        Binding("down", "select_next", "Next provider", show=False),
        Binding("b", "browse_models", "Browse models"),
        Binding("r", "refresh", "Refresh models"),
        Binding("a", "add_provider", "Add provider"),
        Binding("e", "edit_provider", "Edit provider"),
        Binding("d", "delete_provider", "Delete provider"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._selected_index = 0
        self._mode = "details"  # "details", "add", "edit"
        self._refreshing: set[str] = set()
        self._message = ""
        self._form_name = ""
        self._form_url = ""
        self._form_key = ""
        self._form_format = "chat_completions"
        self._editing_provider_id: Optional[str] = None
        self._discovery_results: Optional[str] = None
        self._statuses_cache: Optional[list[Any]] = None
        self._config_error: Optional[str] = None
        # Deferred auto-refresh consumed in on_mount: a worker started on
        # an unmounted screen never runs and would mark the provider as
        # refreshing forever.
        self._pending_refresh_provider: Optional[str] = None

    def _current_statuses(self, force_reload: bool = False):
        if self._statuses_cache is None or force_reload:
            from agentic_debugger.application.provider_connections import (
                ProviderConnectionError,
            )
            from agentic_debugger.application.model_gateway import ModelGateway

            try:
                gateway = ModelGateway.default()
                history_root = None
                if hasattr(self, "app") and hasattr(self.app, "history_store") and self.app.history_store is not None:
                    history_root = getattr(self.app.history_store, "root_dir", None)
                self._statuses_cache = gateway.list_provider_statuses(history_root=history_root)
                self._config_error = None
            except ProviderConnectionError as exc:
                self._statuses_cache = []
                self._config_error = str(exc)
            except Exception as exc:
                self._statuses_cache = []
                self._config_error = str(exc)
        return self._statuses_cache

    def compose(self) -> ComposeResult:
        statuses = self._current_statuses()
        config_error = self._config_error
        with Vertical(id="providers-wrap"):
            with Vertical(id="providers-manager-card"):
                with Horizontal(id="providers-manager-header"):
                    yield Static("MODEL PROVIDER MANAGER", id="providers-title")
                    yield Static("Endpoints · OS Secure Store · Direct API", id="providers-subtitle")
                with Horizontal(id="providers-manager-body"):
                    with Vertical(id="providers-sidebar"):
                        yield Static("PROVIDERS", id="providers-sidebar-title")
                        with VerticalScroll(id="providers-sidebar-list"):
                            if config_error:
                                yield Static("Configuration Error", id="providers-empty-label", classes="providers-empty-text")
                            elif not statuses:
                                yield Static("No providers configured.", id="providers-empty-label", classes="providers-empty-text")
                            else:
                                for index, st in enumerate(statuses):
                                    is_live = bool(getattr(st, "connected", False))
                                    dot = "● " if is_live else "○ "
                                    label = f"{dot}{st.label}"
                                    yield Button(
                                        label,
                                        id=f"provider-select-{st.kind}",
                                        classes=f"provider-item-button{' -selected' if index == self._selected_index else ''}",
                                    )
                        yield Button("+ Add provider", id="provider-add-button", classes="secondary-action")
                    with VerticalScroll(id="provider-main-view"):
                        if config_error:
                            with Vertical(classes="provider-empty-panel", id="provider-empty-panel"):
                                yield Static("Configuration Error", id="provider-empty-message", classes="provider-empty-title")
                                yield Static(
                                    f"Provider configuration error: {config_error}\n"
                                    "Please check or restore provider-configurations.json.",
                                    id="provider-empty-detail",
                                    classes="provider-empty-detail",
                                )
                        elif not statuses:
                            with Vertical(classes="provider-empty-panel", id="provider-empty-panel"):
                                yield Static("No providers configured.", id="provider-empty-message", classes="provider-empty-title")
                                yield Static(
                                    "Use '+ Add provider' on the left to configure a model provider endpoint.",
                                    id="provider-empty-detail",
                                    classes="provider-empty-detail",
                                )
                        else:
                            for st in statuses:
                                with Vertical(classes="provider-panel", id=f"provider-panel-{st.kind}"):
                                    yield Static("", id=f"provider-summary-{st.kind}", classes="provider-summary")
                                    yield Static("", id=f"provider-refresh-{st.kind}", classes="provider-refresh")
                                    with Horizontal(classes="provider-actions", id=f"provider-actions-{st.kind}"):
                                        yield Button(
                                            "Refresh models",
                                            id=f"provider-refresh-button-{st.kind}",
                                            classes="provider-action-button",
                                        )
                                        yield Button(
                                            "Edit provider",
                                            id=f"provider-edit-button-{st.kind}",
                                            classes="provider-action-button",
                                        )
                                        yield Button(
                                            "Delete provider",
                                            id=f"provider-delete-button-{st.kind}",
                                            classes="provider-action-button danger-action",
                                        )
                                    yield Static("MODELS", classes="models-header", id=f"provider-models-title-{st.kind}")
                                    yield Static("", classes="models-helper-text", id=f"provider-models-helper-{st.kind}")
                                    with Horizontal(classes="provider-models-actions", id=f"provider-models-actions-{st.kind}"):
                                        yield Button(
                                            "Browse models",
                                            id=f"provider-browse-models-button-{st.kind}",
                                            classes="provider-action-button primary-action",
                                        )
                                        yield Button(
                                            "+ Add model (manual)",
                                            id=f"provider-add-model-button-{st.kind}",
                                            classes="provider-action-button",
                                        )
                yield Static("", id="providers-status")
                yield Static(
                    self.PROVIDERS_HINT,
                    id="providers-hint",
                )

    def on_mount(self) -> None:
        self._update_hint(self.size.width)
        self.render_state()
        if self._pending_refresh_provider is not None:
            provider_id = self._pending_refresh_provider
            self._pending_refresh_provider = None
            if self._selected_kind() == provider_id:
                self.action_refresh()

    def on_resize(self, event: Any) -> None:
        width = getattr(event, "size", None).width if hasattr(event, "size") and event.size else self.size.width
        self._update_hint(width)

    def _update_hint(self, width: int) -> None:
        try:
            hint = self.query_one("#providers-hint", Static)
            hint.update(self.PROVIDERS_HINT_COMPACT if width < 95 else self.PROVIDERS_HINT)
        except Exception:
            pass

    # -- state ---------------------------------------------------------------

    def _selected_kind(self) -> str:
        statuses = self._current_statuses()
        if not statuses:
            return ""
        idx = max(0, min(self._selected_index, len(statuses) - 1))
        return statuses[idx].kind

    def _selected_label(self) -> str:
        statuses = self._current_statuses()
        if not statuses:
            return ""
        idx = max(0, min(self._selected_index, len(statuses) - 1))
        return statuses[idx].label

    def render_state(self) -> None:
        statuses = self._current_statuses()
        if not statuses:
            try:
                status_widget = self.query_one("#providers-status", Static)
                msg = self._message or (f"Configuration error: {self._config_error}" if self._config_error else "")
                status_widget.update(msg)
            except Exception:
                pass
            return

        selected_kind = self._selected_kind()

        # Update sidebar buttons
        for index, status in enumerate(statuses):
            btn_id = f"#provider-select-{status.kind}"
            try:
                btn = self.query_one(btn_id, Button)
                is_live = bool(getattr(status, "connected", False))
                dot = "● " if is_live else "○ "
                btn.label = f"{dot}{status.label}"
                if index == self._selected_index:
                    btn.add_class("-selected")
                else:
                    btn.remove_class("-selected")
            except Exception:
                pass

        # Update main view panels
        for index, status in enumerate(statuses):
            panel_id = f"#provider-panel-{status.kind}"
            try:
                panel = self.query_one(panel_id, Vertical)
                panel.display = (index == self._selected_index)
            except Exception:
                continue

            summary = self.query_one(f"#provider-summary-{status.kind}", Static)
            refresh_line = self.query_one(f"#provider-refresh-{status.kind}", Static)
            models_helper = self.query_one(f"#provider-models-helper-{status.kind}", Static)

            from agentic_debugger.application.provider_connections import (
                AUTH_DISPLAY_LABELS,
                ENDPOINT_CONTRACT_DISPLAY_LABELS,
                PROTOCOL_DISPLAY_LABELS,
                TRANSPORT_DISPLAY_LABELS,
            )

            proto_raw = PROTOCOL_DISPLAY_LABELS.get(status.api_format, status.api_format)
            proto_label = proto_raw.split(" (", 1)[0] if " (" in proto_raw else proto_raw
            auth_label = AUTH_DISPLAY_LABELS.get(
                getattr(status, "auth_mode", "bearer"), getattr(status, "auth_mode", "")
            )
            profile_label = ENDPOINT_CONTRACT_DISPLAY_LABELS.get(
                getattr(status, "transport_profile", "generic"),
                TRANSPORT_DISPLAY_LABELS.get(
                    getattr(status, "transport_profile", "generic"),
                    getattr(status, "transport_profile", ""),
                ),
            )

            source = _PROVIDER_CREDENTIAL_SOURCE_LABELS.get(
                status.credential_source, status.credential_source or ""
            )

            # Truthful status facts:
            # "Connected" is never displayed based purely on static configuration or credential availability.
            # "Live verified" is reserved for explicit live verification.
            if hasattr(status, "summary_headline"):
                status_disp = status.summary_headline
            elif getattr(status, "live_verified", False):
                status_disp = "Live verified"
            elif getattr(status, "runtime_succeeded_at_utc", None):
                status_disp = "Runtime succeeded"
            elif getattr(status, "credential_ready", False) or getattr(status, "connected", False):
                if source:
                    status_disp = f"Configured · {source}"
                elif getattr(status, "auth_mode", "") == "none":
                    from agentic_debugger.application.model_gateway import is_loopback_url
                    if is_loopback_url(status.base_url):
                        status_disp = "Configured · loopback"
                    else:
                        status_disp = "Configured · no auth"
                else:
                    status_disp = "Configured · credential ready"
            elif getattr(status, "is_configured", True) and getattr(status, "enabled", True) and not getattr(status, "is_quarantined", False):
                status_disp = "Configured · no credential"
            else:
                status_disp = "Not configured"

            if status_disp in ("Live verified", "Runtime succeeded"):
                status_style = PRIMARY
            elif status_disp.startswith("Configured") and "no credential" not in status_disp:
                status_style = FOREGROUND
            elif status_disp in ("Disabled", "Quarantined · recovery required", "Configured · no credential"):
                status_style = MUTED
            else:
                status_style = FAINT

            summary.update(
                Text()
                .append(f"{status.label}", style=f"bold {FOREGROUND}")
                .append(f"   {status_disp}", style=status_style)
                .append(f"\nBase URL    {status.base_url}", style=f"{MUTED}")
                .append(f"\nProtocol    {proto_label}", style=f"{MUTED}")
                .append(f"\nAuth        {auth_label}", style=f"{MUTED}")
                .append(f"\nEndpoint    {profile_label}", style=f"{MUTED}")
            )

            lines = []
            if status.model_count:
                when = (status.last_refresh_utc or "").replace("T", " ").split(".", 1)[0]
                when = when.removesuffix("Z").removesuffix("+00:00").strip()
                suffix = " · stale/unverified" if status.stale else ""
                lines.append(f"Catalog     {status.model_count} models")
                lines.append(f"Updated     {when} UTC{suffix}" if when else "Updated     —")
            elif getattr(status, "live_verified", False):
                lines.append("Catalog     No catalog yet — refresh models to discover the live catalog")
            elif getattr(status, "credential_ready", False) or getattr(status, "connected", False):
                lines.append("Catalog     No catalog yet — refresh models to discover the live catalog")
            else:
                lines.append("Catalog     Not connected")

            def _fmt_ts(ts: Optional[str]) -> Optional[str]:
                if not ts:
                    return None
                w = ts.replace("T", " ").split(".", 1)[0]
                return w.removesuffix("Z").removesuffix("+00:00").strip() + " UTC"

            if getattr(status, "live_verified_at_utc", None):
                lines.append(f"Live verified  {_fmt_ts(status.live_verified_at_utc)}")
            if getattr(status, "runtime_succeeded_at_utc", None):
                lines.append(f"Runtime success {_fmt_ts(status.runtime_succeeded_at_utc)}")

            if status.status_message:
                lines.append(status.status_message)
            refresh_line.update("\n".join(lines) if lines else "")

            # Models summary
            if status.model_count:
                models_helper.update(f"{status.model_count} models available")
            elif status.connected:
                models_helper.update("No models discovered yet. Click 'Refresh models' or '+ Add model'.")
            else:
                models_helper.update("No usable credential — edit provider to add an API key and refresh models.")

        status_widget = self.query_one("#providers-status", Static)
        status_widget.update(self._message)

    # -- actions ---------------------------------------------------------------

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_select_previous(self) -> None:
        if self._selected_index > 0:
            self._selected_index -= 1
            self.render_state()

    def action_select_next(self) -> None:
        statuses = self._current_statuses()
        if self._selected_index < len(statuses) - 1:
            self._selected_index += 1
            self.render_state()

    def _set_message(self, text: str) -> None:
        self._message = text
        try:
            self.query_one("#providers-status", Static).update(text)
        except Exception:
            pass

    def action_browse_models(self) -> None:
        statuses = self._current_statuses()
        if not statuses:
            return
        idx = max(0, min(self._selected_index, len(statuses) - 1))
        current_status = statuses[idx]
        self.app.push_screen(
            ModelCatalogBrowserScreen(
                provider_id=current_status.kind,
                provider_name=current_status.label,
                models=current_status.cached_models,
            )
        )

    def action_refresh(self) -> None:
        kind = self._selected_kind()
        if not kind:
            return
        if kind in self._refreshing:
            return
        self._refreshing.add(kind)
        self._set_message("")
        self.render_state()
        self.run_worker(
            partial(self._refresh_catalog, kind),
            name=f"refresh-{kind}",
            exclusive=False,
            thread=True,
        )

    def _refresh_catalog(self, kind: str):
        from agentic_debugger.application.provider_connections import (
            ProviderConnectionError,
        )
        from agentic_debugger.application.model_gateway import (
            CatalogProbeError,
            ModelGateway,
        )

        try:
            gateway = ModelGateway.default()
            snapshot = gateway.refresh_catalog(kind)
        except (ProviderConnectionError, CatalogProbeError) as exc:
            self.app.call_from_thread(self._refresh_finished, kind, False, str(exc))
            return
        except Exception:
            self.app.call_from_thread(
                self._refresh_finished,
                kind,
                False,
                "catalog refresh failed unexpectedly; retry or check the connection",
            )
            return
        self.app.call_from_thread(
            self._refresh_finished, kind, True, f"{len(snapshot.models)} models"
        )

    def _refresh_finished(self, kind: str, ok: bool, detail: str) -> None:
        self._refreshing.discard(kind)
        self._current_statuses(force_reload=True)
        detail = detail if len(detail) <= 160 else detail[:160] + "…"
        if ok:
            self._set_message(f"Catalog refreshed — {detail}")
        else:
            self._set_message(f"Refresh failed — {detail}")
        self.render_state()

    def action_add_provider(self) -> None:
        def on_saved(new_cfg):
            if new_cfg:
                from agentic_debugger.application.model_gateway import ModelGateway
                ModelGateway.default().invalidate_provider(new_cfg.provider_id)
                self._set_message(f"Added provider '{new_cfg.name}'")
                # Reload screen to refresh widget tree
                self.app.pop_screen()
                new_screen = ModelProvidersScreen()
                new_screen._selected_index = new_screen._index_of(new_cfg.provider_id)
                if ModelGateway.default().credential_readiness(
                    new_cfg.provider_id
                ).has_credential_source:
                    # Consumed by the mounted screen: starting the refresh
                    # worker before the screen mounts would never run it.
                    new_screen._pending_refresh_provider = new_cfg.provider_id
                self.app.push_screen(new_screen)

        self.app.push_screen(AddProviderDialogScreen(on_save=on_saved))

    def action_edit_provider(self) -> None:
        kind = self._selected_kind()
        label = self._selected_label()
        if not kind:
            return
        from agentic_debugger.application.provider_connections import get_provider_config
        cfg = get_provider_config(kind)
        if not cfg:
            self._set_message(f"Provider '{label}' cannot be edited")
            return

        def on_saved(updated_cfg):
            if updated_cfg:
                from agentic_debugger.application.model_gateway import ModelGateway
                ModelGateway.default().invalidate_provider(updated_cfg.provider_id)
                self._set_message(f"Updated provider '{updated_cfg.name}'")
                self._current_statuses(force_reload=True)
                self.render_state()

        self.app.push_screen(EditProviderDialogScreen(config=cfg, on_save=on_saved))

    def action_delete_provider(self) -> None:
        kind = self._selected_kind()
        label = self._selected_label()
        if not kind:
            return

        def on_confirmed(confirmed: bool) -> None:
            if not confirmed:
                return
            from agentic_debugger.application.provider_connections import (
                ProviderConnectionError,
                delete_provider_config,
            )

            try:
                deleted = delete_provider_config(kind)
            except ProviderConnectionError:
                self._set_message(
                    f"Failed to delete provider '{label}': provider credential cleanup could not be completed"
                )
                return
            except Exception:
                self._set_message(
                    f"Failed to delete provider '{label}': provider credential cleanup could not be completed"
                )
                return
            if deleted:
                from agentic_debugger.application.model_gateway import ModelGateway
                ModelGateway.default().invalidate_provider(kind)
                self.app.pop_screen()
                new_screen = ModelProvidersScreen()
                statuses = new_screen._current_statuses(force_reload=True)
                new_screen._selected_index = max(0, min(self._selected_index, len(statuses) - 1))
                self.app.push_screen(new_screen)
                new_screen._set_message(f"Deleted provider '{label}'")
            else:
                self._set_message(f"Failed to delete provider '{label}'")

        self.app.push_screen(
            ConfirmDeleteProviderDialogScreen(
                provider_id=kind,
                provider_name=label,
                on_confirm=on_confirmed,
            )
        )

    def _action_add_manual_model(self, kind: str) -> None:
        def on_added(model_id: Optional[str], display_name: Optional[str], protocol: Optional[str]):
            if model_id:
                from agentic_debugger.application.provider_connections import add_manual_model
                try:
                    add_manual_model(kind, model_id, display_name, protocol)
                    from agentic_debugger.application.model_gateway import ModelGateway
                    ModelGateway.default().invalidate_provider(kind)
                    self._set_message(f"Added model {model_id}")
                    self._current_statuses(force_reload=True)
                    self.render_state()
                except Exception as exc:
                    self._set_message(f"Failed to add model: {exc}")

        self.app.push_screen(AddManualModelDialogScreen(provider_id=kind, on_save=on_added))

    def on_button_pressed(self, event: Any) -> None:
        button_id = getattr(event.button, "id", "") or ""
        if button_id.startswith("provider-select-"):
            kind = button_id.removeprefix("provider-select-")
            self._selected_index = self._index_of(kind)
            self.render_state()
            event.stop()
        elif button_id == "provider-add-button":
            self.action_add_provider()
            event.stop()
        elif button_id.startswith("provider-refresh-button-"):
            kind = button_id.removeprefix("provider-refresh-button-")
            self._selected_index = self._index_of(kind)
            self.action_refresh()
            event.stop()
        elif button_id.startswith("provider-edit-button-"):
            kind = button_id.removeprefix("provider-edit-button-")
            self._selected_index = self._index_of(kind)
            self.action_edit_provider()
            event.stop()
        elif button_id.startswith("provider-delete-button-"):
            kind = button_id.removeprefix("provider-delete-button-")
            self._selected_index = self._index_of(kind)
            self.action_delete_provider()
            event.stop()
        elif button_id.startswith("provider-browse-models-button-"):
            kind = button_id.removeprefix("provider-browse-models-button-")
            self._selected_index = self._index_of(kind)
            self.action_browse_models()
            event.stop()
        elif button_id.startswith("provider-add-model-button-"):
            kind = button_id.removeprefix("provider-add-model-button-")
            self._selected_index = self._index_of(kind)
            self._action_add_manual_model(kind)
            event.stop()

    def _index_of(self, kind: str) -> int:
        statuses = self._current_statuses()
        for idx, st in enumerate(statuses):
            if st.kind == kind:
                return idx
        return 0


# Backward compatibility alias
ProviderConnectionsScreen = ModelProvidersScreen
