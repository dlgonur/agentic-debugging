"""Provider-management workflow screens for the Agentic Debugger TUI.

The provider manager (sidebar + detail/catalog summary with refresh,
browse, add, edit, delete dispatch), the read-only model-catalog browser,
and the add/edit/manual-model/delete-confirmation dialogs, which are only
ever opened by the manager itself.

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
                        yield Button("+ Add provider", id="provider-add-button", classes="primary-action")
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
                yield Button("Cancel", id="btn-cancel-dialog")

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
                yield Button("Cancel", id="btn-cancel-dialog")

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
                yield Button("Cancel", id="btn-cancel-dialog")

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
                yield Button("Cancel", id="btn-cancel-dialog")

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
ProviderConnectionsScreen = ModelProvidersScreen
