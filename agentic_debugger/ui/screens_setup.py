"""Session-setup surface for the Agentic Debugger TUI.

StartSessionScreen is the single place a bounded new deterministic session
may be requested.  It owns the fixed ROW_ORDER form stack, the read-only
environment catalog, project validation, and readiness derivation, and it
opens the editor family and the provider-management surface for edits.

Row-fitting helpers and the provider display label live here because the
setup screen is their only consumer.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Static

from agentic_debugger.application.level32 import (
    LEVEL32_TASK_ID,
    is_ladder_task,
    ladder_task_metadata,
)
from agentic_debugger.application.model_providers import (
    format_model_display_name,
    list_provider_models,
)
from agentic_debugger.ui.screens_editors import (
    BrowseScreen,
    BugDescriptionEditorScreen,
    ChoiceOption,
    ChoicePickerScreen,
    SessionSettingRow,
    SingleLineFieldEditorScreen,
    TimeLimitEditorScreen,
)
from agentic_debugger.ui.screens_providers import ProviderConnectionsScreen
from agentic_debugger.ui.screens_shared import (
    START_FOOTER,
    START_FOOTER_COMPACT,
    _markup_escape,
)
from agentic_debugger.ui.session_config import (
    AUTO_RETRY_MAX,
    OFFLINE_CHOICE,
    POLICY_LABELS,
    POLICY_ON_UNCERTAINTY,
    POLICY_STATIC_BASELINE,
    PROVIDER_CONFIGURED,
    PROVIDER_LABELS,
    PROVIDER_OFFLINE,
    PROVIDER_OLLAMA,
    ROW_AUTO_RETRY,
    ROW_BUG,
    ROW_DEBUGGER,
    ROW_MODEL,
    ROW_ORDER,
    ROW_PROJECT,
    ROW_PROJECT_ENV,
    ROW_REPRO,
    ROW_TARGET,
    ROW_TASK,
    ROW_TIME_LIMIT,
    ROW_VERIFY,
    SEVERITY_ERROR,
    ModelChoice,
    ModelOption,
    ProjectStatus,
    SessionCatalog,
    SessionConfig,
    SessionReadiness,
    TARGET_CURATED,
    TARGET_LABELS,
    TARGET_LADDER,
    TARGET_LOCAL_PROJECT,
    TaskOption,
    derive_readiness,
    model_compatibility,
    summarize_project_env_declarations,
)
from agentic_debugger.ui.setup_display import (
    _clip_cells,
    _fit_row_cells,
    _provider_label,
    _short_unavailable_reason,
    bug_preview,
    debugger_display,
    ladder_presentation,
    model_display,
    task_display_name,
)
from agentic_debugger.ui.setup_pickers import (
    gather_catalog,
    model_choice_key,
    open_auto_retry_picker,
    open_debugger_picker,
    open_model_picker,
    open_target_picker,
    open_task_picker,
)
from agentic_debugger.ui.theme import (
    ERROR,
    EVIDENCE,
    FAINT,
    FOREGROUND,
    MUTED,
    SUCCESS,
    WARNING,
)


class StartSessionScreen(Screen):
    """The ONE session-setup surface for every target and provider.

    One fixed stack of controls — Target, Task, Project, Bug, Repro,
    Verify, Model, Debugger, Time limit, Auto-retry — serves curated
    tasks, Local Project debugging, and the scientific capability ladder
    alike.  Rows never disappear: an inapplicable row is disabled with
    its reason.  One selection change never silently rewrites another.
    Every readiness presentation (Run button, status line, hero chips,
    pre-flight rail) renders from the single ``SessionReadiness``
    object derived by :func:`derive_readiness`.
    """

    BINDINGS = [
        Binding("up", "move_up", "Previous setting", show=False, priority=True),
        Binding("down", "move_down", "Next setting", show=False, priority=True),
        Binding("s", "start", "Run"),
        Binding("p", "focus_local_project", "Local project"),
        Binding("c", "open_providers", "Providers"),
        Binding("h", "history", "History"),
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("escape", "cancel", "Back"),
    ]

    def __init__(
        self,
        task_options: Optional[list[tuple[str, str]]] = None,
        *,
        initial_target: Optional[str] = None,
        initial_project: Optional[str] = None,
    ) -> None:
        super().__init__()
        from agentic_debugger.ui.app import task_display_option

        self._task_options: list[tuple[str, str]] = []
        for item in list(task_options or []):
            if isinstance(item, tuple) and len(item) == 2:
                label, value = item
                self._task_options.append(
                    task_display_option(value) if label == value else (label, value)
                )
            elif isinstance(item, str):
                self._task_options.append(task_display_option(item))
        self._config = SessionConfig()
        if initial_target in TARGET_LABELS:
            self._config = self._config.with_target(initial_target)
        self._catalog = SessionCatalog()
        self._project_status = ProjectStatus.unchecked("")
        self._readiness: Optional[SessionReadiness] = None
        self._start_error: Optional[str] = None
        # Manual-edit guards: automatic, project-derived defaults never
        # overwrite a value the user chose.
        self._repro_user_edited = False
        self._verify_user_edited = False
        self._repro_is_auto = False
        self._verify_is_auto = False
        # Launch-cwd capture for project resolution (never a cwd change).
        try:
            from agentic_debugger.application.local_project import (
                get_launch_cwd,
                resolve_project_path,
            )

            self._launch_cwd = get_launch_cwd()
        except Exception:
            self._launch_cwd = Path.cwd().resolve()
        initial_path = initial_project or str(self._launch_cwd)
        try:
            from agentic_debugger.application.local_project import (
                resolve_project_path,
            )

            self._config = replace(
                self._config, project_path=str(resolve_project_path(initial_path, self._launch_cwd))
            )
        except Exception:
            self._config = replace(self._config, project_path=initial_path)
        try:
            self._apply_tracked_repro_defaults()
        except Exception:
            pass

    # -- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="start-workspace"):
            with Vertical(id="start-main"):
                with VerticalScroll(id="start-config"):
                    yield Static("SESSION SETUP", id="start-section-label")
                    yield SessionSettingRow("Target", row_key=ROW_TARGET, id="target-row")
                    yield SessionSettingRow("Task", row_key=ROW_TASK, id="task-row")
                    yield SessionSettingRow("Project", row_key=ROW_PROJECT, id="project-row")
                    yield SessionSettingRow("Bug", row_key=ROW_BUG, id="bug-row")
                    yield SessionSettingRow("Repro", row_key=ROW_REPRO, id="repro-row")
                    yield SessionSettingRow("Verify (P2P)", row_key=ROW_VERIFY, id="verify-row")
                    yield SessionSettingRow("ProjEnv", row_key=ROW_PROJECT_ENV, id="project-env-row")
                    yield SessionSettingRow("Model", row_key=ROW_MODEL, id="model-row")
                    yield SessionSettingRow("Debugger", row_key=ROW_DEBUGGER, id="debugger-row")
                    yield SessionSettingRow("Time limit", row_key=ROW_TIME_LIMIT, id="time-limit-row")
                    yield SessionSettingRow("Auto-retry", row_key=ROW_AUTO_RETRY, id="auto-retry-row")
                    yield Static("", id="start-status")
                    yield Static("", id="start-notes")
                    with Horizontal(id="start-actions"):
                        yield Button(
                            "Run", id="start-session-button", classes="primary-action"
                        )
                yield Static(START_FOOTER, id="start-footer")
            with VerticalScroll(id="start-context"):
                yield Static("[bold $primary]PRE-FLIGHT[/]", id="context-title")
                yield Static("", id="context-summary")

    def on_mount(self) -> None:
        if not self._task_options:
            self._task_options = list(self.app.curated_task_options())
        self._gather_catalog()
        first_curated = next(
            (task_id for _, task_id in self._task_options if not is_ladder_task(task_id)),
            None,
        )
        self._config = replace(self._config, task_id=first_curated)
        if self._config.target == TARGET_LOCAL_PROJECT:
            self._validate_project()
        self.render_state()
        self._focus_row(ROW_PROJECT if self._config.target == TARGET_LOCAL_PROJECT else ROW_TARGET)
        self._update_context_visibility(self.size.width)
        self._update_footer(self.size.width)

    def on_resize(self, event: Any) -> None:
        self._update_context_visibility(event.size.width)
        self._update_footer(event.size.width)
        if self.is_mounted:
            self.render_state()

    def _update_context_visibility(self, width: int) -> None:
        self.query_one("#start-context", VerticalScroll).display = width >= 100

    def _update_footer(self, width: int) -> None:
        footer = self.query_one("#start-footer", Static)
        footer.update(START_FOOTER_COMPACT if width < 100 else START_FOOTER)

    # -- catalog -------------------------------------------------------------

    def _gather_catalog(self) -> None:
        gather_catalog(self)

    def _validate_project(self) -> None:
        try:
            from agentic_debugger.application.local_project import (
                validate_local_project,
            )

            validated = validate_local_project(
                self._config.project_path, launch_cwd=self._launch_cwd
            )
            if validated.dirty:
                self._project_status = ProjectStatus(
                    path=str(validated.repo_root),
                    ok=False,
                    state="dirty",
                    message=(
                        "Project has uncommitted changes — commit or stash "
                        "them first."
                    ),
                )
            else:
                self._project_status = ProjectStatus(
                    path=str(validated.repo_root),
                    ok=True,
                    state="clean",
                    message=f"Git: {validated.repo_root.name} @ {validated.head_commit[:7]}",
                )
        except Exception as exc:
            message = self._project_error_message(exc)
            self._project_status = ProjectStatus(
                path=self._config.project_path, ok=False, state="invalid", message=message
            )

    @staticmethod
    def _project_error_message(exc: Exception) -> str:
        msg = str(exc)
        if "not a Git repository" in msg:
            return "Not a Git repository."
        if "not found" in msg:
            return "Project path not found."
        if "not a directory" in msg:
            return "Project path is not a directory."
        bounded = msg[:96]
        return bounded

    def _apply_tracked_repro_defaults(self) -> None:
        """Prefill Repro/Verify from a tracked ``repro.py`` (safe checks only).

        Never overwrites a manual value; never invents commands beyond the
        documented convention.
        """
        if self._repro_user_edited and self._verify_user_edited:
            return
        has_repro = False
        try:
            from agentic_debugger.application.local_project import (
                has_tracked_root_repro,
                validate_local_project,
            )

            validated = validate_local_project(
                self._config.project_path, launch_cwd=self._launch_cwd
            )
            has_repro = has_tracked_root_repro(validated.repo_root)
        except Exception:
            has_repro = False
        if not self._repro_user_edited:
            if has_repro:
                if (
                    self._config.reproduction_command is None
                    or self._repro_is_auto
                ):
                    self._config = replace(
                        self._config, reproduction_command="python repro.py"
                    )
                    self._repro_is_auto = True
            elif self._repro_is_auto and self._config.reproduction_command == "python repro.py":
                self._config = replace(self._config, reproduction_command=None)
                self._repro_is_auto = False
        if not self._verify_user_edited:
            if has_repro:
                if self._config.verification_command is None or self._verify_is_auto:
                    self._config = replace(
                        self._config, verification_command="python repro.py"
                    )
                    self._verify_is_auto = True
            elif self._verify_is_auto and self._config.verification_command == "python repro.py":
                self._config = replace(self._config, verification_command=None)
                self._verify_is_auto = False

    # -- navigation ------------------------------------------------------------

    def _focusable_row_ids(self) -> list[str]:
        # The stack is fixed: every row stays reachable for every target.
        return list(ROW_ORDER)

    def _focus_row(self, row_key: str) -> None:
        try:
            self.query_one(f"#{row_key.replace('_', '-')}-row", SessionSettingRow).focus()
        except Exception:
            pass

    def _focused_row_key(self) -> str:
        return getattr(self.app.focused, "row_key", ROW_TARGET)

    def action_move_down(self) -> None:
        rows, current = self._focusable_row_ids(), self._focused_row_key()
        self._focus_row(
            rows[(rows.index(current) + 1) % len(rows)] if current in rows else rows[0]
        )

    def action_move_up(self) -> None:
        rows, current = self._focusable_row_ids(), self._focused_row_key()
        self._focus_row(
            rows[(rows.index(current) - 1) % len(rows)] if current in rows else rows[0]
        )

    def _activate_row(self, row_key: str) -> None:
        readiness = self._readiness
        if readiness is not None:
            state = readiness.rows.get(row_key)
            if state is not None and not state.enabled:
                self.notify(
                    f"Not used for {TARGET_LABELS[self._config.target]} sessions — "
                    f"{state.reason}",
                    severity="information",
                    timeout=4.0,
                )
                return
        if row_key == ROW_TARGET:
            self._open_target_picker()
        elif row_key == ROW_TASK:
            self._open_task_picker()
        elif row_key == ROW_PROJECT:
            self._open_project_picker()
        elif row_key == ROW_BUG:
            self._open_bug_editor()
        elif row_key == ROW_REPRO:
            self._open_text_editor(
                "Reproduction command (optional)",
                self._config.reproduction_command or "",
                self._on_repro_saved,
            )
        elif row_key == ROW_VERIFY:
            self._open_text_editor(
                "Regression check command (optional; must pass BEFORE and after the fix)",
                self._config.verification_command or "",
                self._on_verify_saved,
            )
        elif row_key == ROW_PROJECT_ENV:
            self._open_text_editor(
                "Project env names (optional; e.g. FOO, BAR?, secret:DB_URL — names only, values never stored)",
                self._config.project_env_text or "",
                self._on_project_env_saved,
            )
        elif row_key == ROW_MODEL:
            self._open_model_picker()
        elif row_key == ROW_DEBUGGER:
            self._open_debugger_picker()
        elif row_key == ROW_TIME_LIMIT:
            self._open_time_limit_editor()
        elif row_key == ROW_AUTO_RETRY:
            self._open_auto_retry_picker()

    # -- pickers ----------------------------------------------------------------

    def _open_target_picker(self) -> None:
        open_target_picker(self)

    def _open_task_picker(self) -> None:
        open_task_picker(self)

    def _model_choice_key(self, choice: ModelChoice) -> str:
        return model_choice_key(self, choice)

    def _open_model_picker(self) -> None:
        open_model_picker(self)

    def _open_debugger_picker(self) -> None:
        open_debugger_picker(self)

    def _open_auto_retry_picker(self) -> None:
        open_auto_retry_picker(self)

    def _open_project_picker(self) -> None:
        self.app.push_screen(
            ChoicePickerScreen(
                title="Project input",
                choices=[
                    ChoiceOption("use_cwd", "Use current directory", f"{self._launch_cwd}"),
                    ChoiceOption("browse", "Browse…", "Pick via directory list"),
                    ChoiceOption("type", "Type/paste path…", "Enter absolute or relative path"),
                ],
                current=None,
                on_select=self._project_choice_selected,
            )
        )

    def _project_choice_selected(self, value: str) -> None:
        if value == "use_cwd":
            self._set_project(str(self._launch_cwd))
        elif value == "browse":
            self.app.push_screen(
                BrowseScreen(
                    start_path=self._config.project_path, on_select=self._on_browse_selected
                )
            )
        elif value == "type":
            self._open_text_editor(
                "Project path", self._config.project_path, self._on_project_saved
            )

    def _set_project(self, path: str) -> None:
        try:
            from agentic_debugger.application.local_project import (
                resolve_project_path,
            )

            resolved = str(resolve_project_path(path, self._launch_cwd))
        except Exception:
            resolved = path
        self._config = replace(self._config, project_path=resolved)
        try:
            self._apply_tracked_repro_defaults()
        except Exception:
            pass
        if self._config.target == TARGET_LOCAL_PROJECT:
            self._validate_project()
        self.render_state()
        self._focus_row(ROW_PROJECT)

    def _on_browse_selected(self, path: str) -> None:
        self._set_project(path)

    def _on_project_saved(self, value: Optional[str]) -> None:
        if value is not None:
            self._set_project(value)
        else:
            self.render_state()
            self._focus_row(ROW_PROJECT)

    def _open_bug_editor(self) -> None:
        self.app.push_screen(
            BugDescriptionEditorScreen(
                current=self._config.bug_description or "",
                on_save=self._on_bug_saved,
            )
        )

    def _on_bug_saved(self, value: Optional[str]) -> None:
        if value is not None:
            self._config = replace(self._config, bug_description=value)
        self.render_state()
        self._focus_row(ROW_BUG)

    def _on_repro_saved(self, value: Optional[str]) -> None:
        if value is None:
            self.render_state()
            self._focus_row(ROW_REPRO)
            return
        self._repro_user_edited = True
        self._repro_is_auto = False
        self._config = replace(
            self._config,
            reproduction_command=value.strip() if value.strip() else None,
        )
        self.render_state()
        self._focus_row(ROW_REPRO)

    def _on_verify_saved(self, value: Optional[str]) -> None:
        if value is None:
            self.render_state()
            self._focus_row(ROW_VERIFY)
            return
        self._verify_user_edited = True
        self._verify_is_auto = False
        self._config = replace(
            self._config,
            verification_command=value.strip() if value.strip() else None,
        )
        self.render_state()
        self._focus_row(ROW_VERIFY)

    def _on_project_env_saved(self, value: Optional[str]) -> None:
        if value is None:
            self.render_state()
            self._focus_row(ROW_PROJECT_ENV)
            return
        self._config = replace(self._config, project_env_text=value.strip())
        self.render_state()
        self._focus_row(ROW_PROJECT_ENV)

    def _open_text_editor(
        self, title: str, current: str, on_save: Any, multiline: bool = False
    ) -> None:
        self.app.push_screen(
            SingleLineFieldEditorScreen(
                title=title,
                current=current or "",
                on_save=on_save,
                placeholder=title,
            )
        )

    def _open_time_limit_editor(self) -> None:
        self.app.push_screen(
            TimeLimitEditorScreen(
                current=self._config.time_limit_seconds,
                on_save=self._time_limit_saved,
                on_cancel=lambda: self._focus_row(ROW_TIME_LIMIT),
            )
        )

    def _time_limit_saved(self, value: Optional[int]) -> None:
        self._config = replace(self._config, time_limit_seconds=value)
        self.render_state()
        self._focus_row(ROW_TIME_LIMIT)

    # -- selection change (the single mutation entry point) ---------------------

    def _choice_selected(self, row_key: str, value: str) -> None:
        self._start_error = None
        if row_key == ROW_TARGET:
            self._config = self._config.with_target(value)
            if value == TARGET_LOCAL_PROJECT:
                self._validate_project()
                try:
                    self._apply_tracked_repro_defaults()
                except Exception:
                    pass
        elif row_key == ROW_TASK:
            self._config = replace(self._config, task_id=value)
        elif row_key == ROW_MODEL:
            if value == "providers:manage":
                # The picker's management entry never mutates the model
                # selection; it opens the provider-connections surface.
                self.app.push_screen(ProviderConnectionsScreen())
                return
            provider, _, model_id = value.partition(":")
            if provider == PROVIDER_OFFLINE:
                self._config = replace(self._config, model=OFFLINE_CHOICE)
            else:
                option = next(
                    (
                        m
                        for m in self._catalog.models + self._catalog.ladder_models
                        if m.provider == provider and m.model_id == model_id
                    ),
                    None,
                )
                if provider == PROVIDER_CONFIGURED:
                    display = option.display if option else model_id
                else:
                    display = format_model_display_name(option.display if option else model_id)
                self._config = replace(
                    self._config,
                    model=ModelChoice(provider, model_id, display),
                )
        elif row_key == ROW_DEBUGGER:
            self._config = replace(self._config, debugger_policy=value)
        elif row_key == ROW_AUTO_RETRY:
            try:
                self._config = replace(
                    self._config, auto_retries=max(0, min(int(value), AUTO_RETRY_MAX))
                )
            except ValueError:
                return
        self.render_state()
        self._focus_row(row_key)

    # -- rendering (single derivation, many surfaces) -----------------------------

    def _task_display_name(self) -> str:
        return task_display_name(self)

    def _model_display(self) -> tuple[str, str]:
        return model_display(self)

    def _ladder_presentation(self) -> tuple[str, str, str]:
        return ladder_presentation(self)

    def _debugger_display(self) -> str:
        return debugger_display(self)

    def _bug_preview(self) -> str:
        return bug_preview(self)

    def _config_content_width(self) -> int:
        """Usable width of the configuration column (rail steals 36 cells at 100+)."""
        rail = 36 if self.size.width >= 100 else 0
        return max(30, self.size.width - rail - 6)

    _hero_content_width = _config_content_width

    def render_state(self) -> None:
        """Derive readiness once and render every surface from it."""
        self._readiness = derive_readiness(
            self._config, self._catalog, self._project_status
        )
        readiness = self._readiness
        config = self._config
        local = config.target == TARGET_LOCAL_PROJECT

        # -- rows (width-aware: the whole line fits or ellipsizes) ---------
        # Row chrome is 2 prefix + 14 label; one cell of scrollbar slack.
        width = self._config_content_width()
        row_budget = max(12, width - 17)

        def fitted(row_key: str, value: str, secondary: str = "") -> None:
            state = readiness.rows.get(row_key)
            reason = state.reason if state is not None and not state.enabled else ""
            value, secondary, reason = _fit_row_cells(
                value, secondary, reason, row_budget
            )
            row = self._row(row_key)
            row.set_value(value, secondary=secondary)
            if state is not None and not state.enabled:
                row.set_disabled(reason)
            else:
                row.set_enabled()

        fitted(ROW_TARGET, TARGET_LABELS[config.target])
        fitted(
            ROW_TASK,
            "" if local else self._task_display_name(),
            "" if local else (config.task_id or ""),
        )
        fitted(ROW_PROJECT, (config.project_path or "") if local else "")
        fitted(ROW_BUG, self._bug_preview() if local else "")
        fitted(
            ROW_REPRO,
            (config.reproduction_command or "Not set (optional)") if local else "",
        )
        fitted(
            ROW_VERIFY,
            (config.verification_command or "Not set (optional)") if local else "",
        )
        fitted(
            ROW_PROJECT_ENV,
            (summarize_project_env_declarations(config.project_env_text or "") if local else ""),
        )
        model_value, model_secondary = self._model_display()
        fitted(ROW_MODEL, model_value, model_secondary)
        fitted(ROW_DEBUGGER, self._debugger_display())
        fitted(
            ROW_TIME_LIMIT,
            "No limit"
            if config.time_limit_seconds is None
            else str(config.time_limit_seconds),
        )
        if config.target == TARGET_LADDER:
            fitted(ROW_AUTO_RETRY, "0 automatic retries")
        else:
            fitted(ROW_AUTO_RETRY, f"{config.auto_retries} on retryable failure")

        # -- blockers / status (concise actionable blocker when necessary) --
        status = self.query_one("#start-status", Static)
        if self._start_error is not None:
            status.update(f"[{ERROR}]! Start failed — {_markup_escape(self._start_error)}[/]")
        elif not readiness.ready:
            errors = [item for item in readiness.issues if item.severity == SEVERITY_ERROR]
            status.update("\n".join(f"[{ERROR}]! {_markup_escape(item.message)}[/]" for item in errors))
        else:
            status.update("")

        # Trust notices stay visible at every width (not only in the rail)
        notes = self.query_one("#start-notes", Static)
        if readiness.notes:
            notes.update(
                f"[{FAINT}]{'   '.join(_markup_escape(note) for note in readiness.notes)}[/]"
            )
        else:
            notes.update("")

        # -- run button -----------------------------------------------------
        button = self.query_one("#start-session-button", Button)
        button.label = readiness.run_label
        button.disabled = not readiness.ready

        self._update_context(readiness)

    def _row(self, row_key: str) -> SessionSettingRow:
        return self.query_one(f"#{row_key.replace('_', '-')}-row", SessionSettingRow)

    def _update_context(self, readiness: SessionReadiness) -> None:
        config = self._config
        lines: list[str] = []

        def kv(label: str, value: str) -> None:
            lines.append(f"[{MUTED}]{label}[/]\n[{FOREGROUND}]{_markup_escape(value)}[/]")

        kv("Target", TARGET_LABELS[config.target])
        if config.target != TARGET_LOCAL_PROJECT:
            kv("Task", self._task_display_name())
            kv("Task ID", config.task_id or "Not selected")
        else:
            kv("Project", config.project_path or "—")
            kv("Repo", self._project_status.message if self._project_status.state != "unchecked" else "—")
            kv("Bug", self._bug_preview())
            kv("Repro", config.reproduction_command or "Not set")
            kv("Verify", config.verification_command or "Not set")
            kv("ProjEnv", summarize_project_env_declarations(config.project_env_text or ""))
        model_value, model_secondary = self._model_display()
        kv("Model", model_value)
        kv("Provider", model_secondary)
        kv("Debugger", self._debugger_display())
        kv("Time limit", "No limit" if config.time_limit_seconds is None else str(config.time_limit_seconds))
        if config.target == TARGET_LADDER:
            _, treatment, evaluation = self._ladder_presentation()
            kv("Treatment", treatment)
            kv("Evaluation", evaluation)

        if readiness.issues:
            lines.append("")
            lines.append(f"[bold {ERROR}]CHECKS[/]")
            for issue in readiness.issues:
                marker = "!" if issue.severity == SEVERITY_ERROR else "?"
                style = ERROR if issue.severity == SEVERITY_ERROR else WARNING
                lines.append(f"[{style}]{marker} {_markup_escape(issue.message)}[/]")
        if readiness.notes:
            lines.append("")
            lines.append(f"[bold {EVIDENCE}]NOTICES[/]")
            for note in readiness.notes:
                lines.append(f"[{MUTED}]· {_markup_escape(note)}[/]")

        lines.append("")
        if readiness.ready:
            lines.append(f"[bold {SUCCESS}]READY  Yes[/]  [{MUTED}]→ {readiness.run_label}[/]")
        else:
            errors = sum(1 for i in readiness.issues if i.severity == SEVERITY_ERROR)
            lines.append(f"[bold {ERROR}]READY  No[/]  [{MUTED}]· {errors} blocking issue(s)[/]")
        self.query_one("#context-summary", Static).update("\n".join(lines))

    # -- run ----------------------------------------------------------------------

    @property
    def start_available(self) -> bool:
        return self._readiness.ready if self._readiness is not None else False

    @property
    def task_id(self) -> Optional[str]:
        return self._config.task_id

    @property
    def profile_id(self) -> Optional[str]:
        return None if self._config.model.is_offline else self._config.model.model_id

    def action_edit(self) -> None:
        self._activate_row(self._focused_row_key())

    def action_confirm(self) -> None:
        self.action_edit()

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def action_history(self) -> None:
        self.app.pop_screen()

    def action_focus_local_project(self) -> None:
        """P: jump straight to the Local Project controls (same screen)."""
        if self._config.target != TARGET_LOCAL_PROJECT:
            self._choice_selected(ROW_TARGET, TARGET_LOCAL_PROJECT)
        self._focus_row(ROW_PROJECT)

    def action_open_providers(self) -> None:
        """C: open the provider-connections management surface."""
        self.app.push_screen(ProviderConnectionsScreen())

    def action_quit_app(self) -> None:
        self.app.action_quit()

    def _refresh_for_start(self) -> None:
        """Re-gather truth immediately before starting (fail closed on a
        changed environment rather than launching a stale selection)."""
        self._gather_catalog()
        if self._config.target == TARGET_LOCAL_PROJECT:
            self._validate_project()
        self.render_state()

    def _start(self) -> None:
        from agentic_debugger.application.events import SourceKind

        self._start_error = None
        self._refresh_for_start()
        readiness = self._readiness
        if readiness is None or not readiness.ready:
            return  # the status line already states the first blocker
        config = self._config
        try:
            if config.target == TARGET_LADDER:
                task_id = str(config.task_id)
                is_level32 = task_id == LEVEL32_TASK_ID
                # Distinguish executable vs qualified: qualified Ollama
                # models use the frozen/canonical ladder operator paths;
                # all other executable providers use the shared
                # configured-provider runtime (direct API) so the
                # provider_id/model_id survive to the worker.
                ladder_entry = self._catalog.ladder_model(config.model)
                if is_level32:
                    if ladder_entry is not None:
                        # Qualified Ollama model: frozen official Level-32 operator
                        self.app.start_live_session(
                            task_id=task_id,
                            policy="exact-pdb-level32-frozen",
                            max_elapsed_seconds=None,
                            source_kind=SourceKind.LEVEL32_OPERATOR,
                            profile_id=config.model.model_id,
                        )
                        return
                    # Non-qualified executable model on Level 32: routes to CONFIGURED_MODEL
                    if config.model.provider == PROVIDER_CONFIGURED:
                        self.app.start_live_session(
                            task_id=task_id,
                            policy="pdb-on-uncertainty",
                            max_elapsed_seconds=None,
                            source_kind=SourceKind.CONFIGURED_MODEL,
                            profile_id=config.model.model_id,
                        )
                    else:
                        self.app.start_live_session(
                            task_id=task_id,
                            policy="pdb-on-uncertainty",
                            max_elapsed_seconds=None,
                            source_kind=SourceKind.CONFIGURED_MODEL,
                            profile_id=config.model.model_id,
                            model_provider=config.model.provider,
                        )
                    return
                # Lower ladder rungs (6, 12, 18)
                if ladder_entry is not None:
                    # Qualified Ollama retains canonical ladder path
                    self.app.start_live_session(
                        task_id=task_id,
                        policy="pdb-on-uncertainty",
                        max_elapsed_seconds=None,
                        source_kind=SourceKind.OLLAMA_CLOUD_LADDER,
                        profile_id=config.model.model_id,
                    )
                elif config.model.provider == PROVIDER_CONFIGURED:
                    self.app.start_live_session(
                        task_id=task_id,
                        policy="pdb-on-uncertainty",
                        max_elapsed_seconds=None,
                        source_kind=SourceKind.CONFIGURED_MODEL,
                        profile_id=config.model.model_id,
                    )
                else:
                    self.app.start_live_session(
                        task_id=task_id,
                        policy="pdb-on-uncertainty",
                        max_elapsed_seconds=None,
                        source_kind=SourceKind.CONFIGURED_MODEL,
                        profile_id=config.model.model_id,
                        model_provider=config.model.provider,
                    )
                return
            if config.target == TARGET_LOCAL_PROJECT:
                provider = (
                    None
                    if config.model.provider == PROVIDER_CONFIGURED
                    else config.model.provider
                )
                self.app.start_local_project_session(
                    project_path=config.project_path,
                    bug_description=config.bug_description.strip(),
                    reproduction_command=config.reproduction_command,
                    verification_command=config.verification_command,
                    profile_id=config.model.model_id,
                    model_provider=provider,
                    max_elapsed_seconds=config.time_limit_seconds,
                    auto_retries=config.auto_retries,
                    project_env_text=config.project_env_text or "",
                )
                return
            # Curated target: the model selection routes the source.
            if config.model.is_offline:
                self.app.start_live_session(
                    task_id=str(config.task_id),
                    policy=config.debugger_policy,
                    max_elapsed_seconds=config.time_limit_seconds,
                    source_kind=SourceKind.OFFLINE_DEMO,
                    profile_id=None,
                )
            elif config.model.provider == PROVIDER_CONFIGURED:
                self.app.start_live_session(
                    task_id=str(config.task_id),
                    policy=config.debugger_policy,
                    max_elapsed_seconds=config.time_limit_seconds,
                    source_kind=SourceKind.CONFIGURED_MODEL,
                    profile_id=config.model.model_id,
                )
            else:
                self.app.start_live_session(
                    task_id=str(config.task_id),
                    policy=config.debugger_policy,
                    max_elapsed_seconds=config.time_limit_seconds,
                    source_kind=SourceKind.CONFIGURED_MODEL,
                    profile_id=config.model.model_id,
                    model_provider=config.model.provider,
                )
        except Exception as exc:
            self._start_error = str(exc)
            self.render_state()

    def action_start(self) -> None:
        self._start()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-session-button":
            self._start()
            event.stop()
