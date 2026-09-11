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
from agentic_debugger.ui.theme import (
    ERROR,
    EVIDENCE,
    FAINT,
    FOREGROUND,
    MUTED,
    SUCCESS,
    WARNING,
)


def _clip_cells(value: str, room: int) -> str:
    """Ellipsize to a cell budget so content never hard-clips at a border."""
    if room <= 1:
        return "…"
    if len(value) <= room:
        return value
    return value[: room - 1].rstrip() + "…"


def _fit_row_cells(
    value: str,
    secondary: str,
    reason: str,
    budget: int,
) -> tuple[str, str, str]:
    """Fit one setting row's value, secondary, and reason into ``budget``
    cells (everything after the 16-cell prefix+label chrome).

    Priority keeps the value intact longest: the reason clips first,
    then the secondary, then the value itself.
    """
    def used(v: str, s: str, r: str) -> int:
        total = len(v)
        if s:
            total += 2 + len(s)
        if r:
            total += 4 + len(r)  # gap + parentheses
        return total

    if used(value, secondary, reason) <= budget:
        return value, secondary, reason
    room = budget - len(value) - (4 if reason else 0)
    if reason and room >= 4:
        reason = _clip_cells(reason, budget - len(value) - 4)
        if used(value, secondary, reason) <= budget:
            return value, secondary, reason
    if secondary:
        secondary = _clip_cells(secondary, max(1, budget - len(value) - (4 + len(reason) if reason else 0)))
        if used(value, secondary, reason) <= budget:
            return value, secondary, reason
    keep = budget - (4 + len(reason) if reason else 0)
    return _clip_cells(value, max(1, keep)), "", reason


def _short_unavailable_reason(reason: Optional[str]) -> str:
    """One bounded picker line for a provider unavailability reason."""
    if not reason:
        return "unavailable"
    text = reason.split("(", 1)[0].strip().rstrip(".")
    if len(text) > 60:
        text = text[:60].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text or "unavailable"


def _provider_label(provider: str) -> str:
    if provider in PROVIDER_LABELS:
        return PROVIDER_LABELS[provider]
    try:
        from agentic_debugger.application.provider_connections import get_provider_config
        cfg = get_provider_config(provider)
        if cfg is not None:
            return cfg.name
    except Exception:
        pass
    return provider


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
        """Rebuild the read-only environment catalog (offline, no provider
        contact, no mutation)."""
        tasks: list[TaskOption] = []
        for label, task_id in self._task_options:
            title = label.split("·", 1)[0].strip() or task_id
            ladder = is_ladder_task(task_id)
            detail = ""
            if ladder:
                meta = ladder_task_metadata(task_id)
                detail = f"{meta.treatment} · {meta.evaluation}"
            tasks.append(TaskOption(task_id, title, ladder=ladder, detail=detail))

        models: list[ModelOption] = [
            ModelOption(
                PROVIDER_OFFLINE,
                "",
                "Offline",
                detail="",
            )
        ]
        provider_reasons: dict[str, Optional[str]] = {}
        provider_registry_error: Optional[str] = None
        try:
            for item in list_provider_models(include_ollama=True):
                if not item.available and item.provider_label not in provider_reasons:
                    provider_reasons.setdefault(
                        item.kind, item.unavailable_reason or "provider unavailable"
                    )
                models.append(
                    ModelOption(
                        item.kind,
                        item.model_id,
                        item.display_name,
                        detail=item.note or "",
                        available=item.available,
                        unavailable_reason=item.unavailable_reason,
                    )
                )
        except Exception as exc:
            # Fail-closed: a corrupt provider registry must never look like
            # a healthy fresh install.  Surface a disabled, credential-safe
            # configuration-error entry instead of an empty state.
            from agentic_debugger.application.model_providers import ProviderRegistryError

            if isinstance(exc, ProviderRegistryError):
                provider_registry_error = str(exc)[:160]
            else:
                provider_registry_error = "provider configuration error"
            models.append(
                ModelOption(
                    "__provider_registry_error__",
                    "",
                    "Configuration Error",
                    detail="",
                    available=False,
                    unavailable_reason=provider_registry_error,
                )
            )

        configured_error: Optional[str] = None
        try:
            summaries, configured_error = self.app.configured_profiles()
        except Exception as exc:  # pragma: no cover - bounded diagnostics
            summaries, configured_error = (), str(exc)
        for profile in summaries:
            models.append(
                ModelOption(
                    PROVIDER_CONFIGURED,
                    profile.profile_id,
                    profile.display_name,
                    detail="",
                )
            )

        ladder_models: list[ModelOption] = []
        try:
            for item in self.app.ollama_cloud_model_profiles():
                ladder_models.append(
                    ModelOption(
                        PROVIDER_OLLAMA,
                        item.alias,
                        item.display_name,
                        detail="",
                    )
                )
        except Exception:
            pass

        self._catalog = SessionCatalog(
            tasks=tuple(tasks),
            models=tuple(models),
            ladder_models=tuple(ladder_models),
            configured_error=configured_error,
        )

    # -- project validation ---------------------------------------------------

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
        descriptions = {
            TARGET_CURATED: "Reproducible in-repo fixture; offline or any provider.",
            TARGET_LOCAL_PROJECT: "Your clean Git repository; describe the bug.",
            TARGET_LADDER: "Levels 6/12/18: configured provider models allowed; Level 32: frozen qualified treatment.",
        }
        choices = [
            ChoiceOption(
                target,
                TARGET_LABELS[target],
                descriptions[target],
            )
            for target in (TARGET_CURATED, TARGET_LOCAL_PROJECT, TARGET_LADDER)
        ]
        self.app.push_screen(
            ChoicePickerScreen(
                title="Debug what?",
                choices=choices,
                current=self._config.target,
                on_select=lambda value: self._choice_selected(ROW_TARGET, value),
            )
        )

    def _open_task_picker(self) -> None:
        target = self._config.target
        choices: list[ChoiceOption] = []
        for task in self._catalog.tasks:
            if task.ladder:
                disabled = target != TARGET_LADDER
                reason = (
                    "" if not disabled else "runs under the Capability ladder target"
                )
                group = "CAPABILITY LADDER"
            else:
                disabled = target == TARGET_LADDER
                reason = "" if not disabled else "ladder runs use Level rungs"
                group = "CURATED TASKS"
            choices.append(
                ChoiceOption(
                    task.task_id,
                    task.title,
                    task.detail,
                    secondary=task.task_id,
                    group=group,
                    disabled=disabled,
                    disabled_reason=reason,
                )
            )
        self.app.push_screen(
            ChoicePickerScreen(
                title="Select task",
                choices=choices,
                current=self._config.task_id,
                on_select=lambda value: self._choice_selected(ROW_TASK, value),
            )
        )

    def _model_choice_key(self, choice: ModelChoice) -> str:
        return f"{choice.provider}:{choice.model_id}"

    def _open_model_picker(self) -> None:
        self._gather_catalog()
        target = self._config.target
        choices: list[ChoiceOption] = []
        offline_ok, offline_reason = model_compatibility(
            target, ModelOption(PROVIDER_OFFLINE, "", "Offline")
        )
        offline_group_note = "unavailable for Capability Ladder" if target == TARGET_LADDER else ""
        choices.append(
            ChoiceOption(
                self._model_choice_key(OFFLINE_CHOICE),
                "Offline",
                "",
                group="OFFLINE",
                group_note=offline_group_note,
                disabled=not offline_ok,
                disabled_reason=offline_reason,
            )
        )
        provider_groups: list[tuple[str, str]] = []
        try:
            from agentic_debugger.application.provider_connections import list_configured_providers
            for cfg in list_configured_providers():
                if cfg.provider_id in (
                    PROVIDER_CONFIGURED,
                    PROVIDER_OFFLINE,
                ):
                    continue
                if not cfg.enabled:
                    continue
                provider_groups.append((cfg.provider_id, cfg.name.upper()))
        except Exception:
            pass
        configured_group = (PROVIDER_CONFIGURED, "CUSTOM COMMAND PROFILES")
        groups = tuple(provider_groups + [configured_group])

        # One stable provider world for every target.  The qualified roster
        # annotates Ollama entries; it never replaces the general catalog or
        # hides the other provider groups.  Missing qualified aliases are
        # merged into the one Ollama group, not duplicated in a second island.
        options_by_provider: dict[str, list[ModelOption]] = {
            provider: [] for provider, _ in groups
        }
        seen_keys: set[tuple[str, str]] = set()
        for option in self._catalog.models:
            if option.provider == PROVIDER_OFFLINE:
                continue
            key = (option.provider, option.model_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            options_by_provider.setdefault(option.provider, []).append(option)
        for option in self._catalog.ladder_models:
            key = (option.provider, option.model_id)
            if key not in seen_keys:
                seen_keys.add(key)
                options_by_provider.setdefault(PROVIDER_OLLAMA, []).append(option)

        for provider, group in groups:
            provider_options = options_by_provider.get(provider, [])
            group_note = ""
            if provider == PROVIDER_CONFIGURED and self._catalog.configured_error:
                group_note = "configuration error"
            elif provider == PROVIDER_CONFIGURED and not provider_options:
                group_note = "none configured"
            elif provider_options and not any(opt.available for opt in provider_options):
                first_reason = provider_options[0].unavailable_reason or ""
                if (
                    "auth store not found" in first_reason.lower()
                    or "cli not found" in first_reason.lower()
                    or "no direct api credential" in first_reason.lower()
                ):
                    group_note = "not configured"
                else:
                    group_note = _short_unavailable_reason(first_reason)

            if not provider_options:
                reason = (
                    _short_unavailable_reason(self._catalog.configured_error)
                    if provider == PROVIDER_CONFIGURED and self._catalog.configured_error
                    else f"No {_provider_label(provider)} models configured"
                )
                title = (
                    "Configuration error"
                    if provider == PROVIDER_CONFIGURED and self._catalog.configured_error
                    else "None configured"
                )
                choices.append(
                    ChoiceOption(
                        f"unavailable:{provider}",
                        title,
                        "",
                        group=group,
                        group_note=group_note,
                        disabled=True,
                        disabled_reason=reason,
                    )
                )
                continue

            for index, option in enumerate(provider_options):
                qualified = self._catalog.ladder_model(option.choice)
                effective = qualified or option
                is_level32 = target == TARGET_LADDER and self._config.task_id == LEVEL32_TASK_ID
                compatible, compat_reason = model_compatibility(
                    target,
                    effective,
                    ladder_qualified=qualified is not None,
                )
                disabled = not effective.available or not compatible
                if not compatible:
                    reason = compat_reason
                elif not effective.available:
                    reason = _short_unavailable_reason(effective.unavailable_reason)
                else:
                    reason = ""
                if provider == PROVIDER_CONFIGURED:
                    display_name = effective.display or effective.model_id
                else:
                    display_name = format_model_display_name(effective.display or effective.model_id)
                # Discovered-catalog detail (direct-API protocol family or
                # the bounded unresolved-protocol note) stays secondary.
                if is_level32 and qualified is None and effective.available and provider != PROVIDER_OFFLINE:
                    if effective.detail:
                        secondary = f"{effective.detail} · not qualified for frozen Level-32 comparison"
                    else:
                        secondary = "not qualified for frozen Level-32 comparison"
                else:
                    secondary = effective.detail if effective.available else ""
                choices.append(
                    ChoiceOption(
                        self._model_choice_key(effective.choice),
                        display_name,
                        "",
                        secondary=secondary,
                        group=group if index == 0 else "",
                        group_note=group_note if index == 0 or group_note else "",
                        disabled=disabled,
                        disabled_reason=reason,
                    )
                )
        choices.append(
            ChoiceOption(
                "providers:manage",
                "Manage model providers…",
                "status, model refresh, API key (press m anytime)",
                group="",
            )
        )
        self.app.push_screen(
            ChoicePickerScreen(
                title="Select model",
                choices=choices,
                current=self._model_choice_key(self._config.model),
                on_select=lambda value: self._choice_selected(ROW_MODEL, value),
            )
        )

    def _open_debugger_picker(self) -> None:
        choices = [
            ChoiceOption(
                POLICY_ON_UNCERTAINTY,
                POLICY_LABELS[POLICY_ON_UNCERTAINTY],
                "Attach PDB when runtime evidence is useful.",
            ),
            ChoiceOption(
                POLICY_STATIC_BASELINE,
                POLICY_LABELS[POLICY_STATIC_BASELINE],
                "Static reasoning only; no debugger session.",
            ),
        ]
        self.app.push_screen(
            ChoicePickerScreen(
                title="Select debugger policy",
                choices=choices,
                current=self._config.debugger_policy,
                on_select=lambda value: self._choice_selected(ROW_DEBUGGER, value),
            )
        )

    def _open_auto_retry_picker(self) -> None:
        choices = [
            ChoiceOption("0", "No auto-retry", "fail fast; retry manually with r"),
            ChoiceOption("1", "1 auto-retry", "one fresh attempt on retryable failure"),
            ChoiceOption("2", "2 auto-retries", "two fresh attempts"),
            ChoiceOption("3", "3 auto-retries", "maximum"),
        ]
        self.app.push_screen(
            ChoicePickerScreen(
                title="Auto-retry on failure",
                choices=choices,
                current=str(self._config.auto_retries),
                on_select=lambda value: self._choice_selected(ROW_AUTO_RETRY, value),
            )
        )

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
        task = self._catalog.find_task(self._config.task_id)
        if task is not None:
            title = task.title
        elif self._config.task_id:
            title = next(
                (
                    label.split("·", 1)[0].strip()
                    for label, task_id in self._task_options
                    if task_id == self._config.task_id
                ),
                self._config.task_id,
            )
        else:
            title = "Not selected"
        if self.size.width and self.size.width < 70:
            available = max(18, self.size.width - 20)
            if len(title) > available:
                return f"{title[: available - 1]}…"
        return title

    def _model_display(self) -> tuple[str, str]:
        choice = self._config.model
        if choice.is_offline:
            return "Offline", "Offline"
        label = _provider_label(choice.provider)
        if choice.provider == PROVIDER_CONFIGURED:
            display = choice.display or choice.model_id
        else:
            display = format_model_display_name(choice.display or choice.model_id)
        return display, label

    def _ladder_presentation(self) -> tuple[str, str, str]:
        """Derive (debugger, treatment, evaluation) presentation for the current ladder selection."""
        task = self._catalog.find_task(self._config.task_id)
        if task is None or not task.ladder:
            return "Frozen contract", "—", "—"
        meta = ladder_task_metadata(task.task_id)
        if task.task_id == LEVEL32_TASK_ID:
            ladder_entry = self._catalog.ladder_model(self._config.model)
            if ladder_entry is not None:
                # Qualified official Level-32 route (dispatches to LEVEL32_OPERATOR)
                return meta.debugger, meta.treatment, meta.evaluation
            if not self._config.model.is_offline:
                # Executable non-qualified Level-32 route (dispatches to CONFIGURED_MODEL)
                return (
                    POLICY_LABELS.get(POLICY_ON_UNCERTAINTY, "On uncertainty"),
                    "Interactive Level-32 · non-official",
                    "Independent verifier",
                )
            if not self._catalog.ladder_models:
                return (
                    POLICY_LABELS.get(POLICY_ON_UNCERTAINTY, "On uncertainty"),
                    "Interactive Level-32 · non-official",
                    "Independent verifier",
                )
            return meta.debugger, meta.treatment, meta.evaluation
        # Lower ladder rungs (Level 6, 12, 18)
        return meta.debugger, meta.treatment, meta.evaluation

    def _debugger_display(self) -> str:
        if self._config.target == TARGET_LADDER:
            debugger, _, _ = self._ladder_presentation()
            return debugger
        if self._config.target == TARGET_LOCAL_PROJECT:
            return POLICY_LABELS[POLICY_ON_UNCERTAINTY]
        return POLICY_LABELS.get(self._config.debugger_policy, self._config.debugger_policy)

    def _bug_preview(self) -> str:
        text = self._config.bug_description.strip()
        if not text:
            return "—"
        first = text.splitlines()[0][:48] + ("…" if len(text.splitlines()[0]) > 48 else "")
        if "\n" in text:
            first = f"{first} [+]" if first else "Described [+]"
        return first or "Described"

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
