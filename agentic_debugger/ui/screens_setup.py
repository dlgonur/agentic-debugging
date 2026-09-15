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
    ProjectEnvBuilderScreen,
    SessionSettingRow,
    SingleLineFieldEditorScreen,
    TimeLimitEditorScreen,
)
from agentic_debugger.ui.screens_providers import ProviderConnectionsScreen
from agentic_debugger.ui.screens_shared import (
    LOCAL_SETUP_SUBTITLE,
    START_FOOTER,
    START_FOOTER_COMPACT,
    _markup_escape,
    manager_group_header,
)
from agentic_debugger.ui.session_config import (
    AUTO_RETRY_MAX,
    GROUP_BOUNDS,
    GROUP_HOW,
    GROUP_WHAT,
    GROUP_WHERE,
    LOCAL_SETUP_FOCUS_ORDER,
    LOCAL_SETUP_GROUPS,
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
    APPLY_ELIGIBILITY_CAPTION,
    _clip_cells,
    _fit_row_cells,
    _provider_label,
    _short_unavailable_reason,
    bug_display_tag,
    bug_preview,
    debugger_display,
    discovery_bar_text,
    discovery_card_text,
    ladder_presentation,
    local_context_notes_text,
    local_group_error_counts,
    local_group_status_label,
    model_display,
    repro_auto_tag,
    repro_display_tag,
    task_display_name,
    verify_auto_tag,
    verify_display_tag,
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
    its reason — except Local Project setup information architecture
    (A1): when the target is Local, Task/Debugger become one-line
    context notes (not focusable rows) and Target renders as the Mode
    switch, with the eight remaining rows grouped into Where / What /
    How / Bounds.  One selection change never silently rewrites another.
    Every readiness presentation (Run button, status checklist,
    pre-flight rail) renders from the single ``SessionReadiness``
    object derived by :func:`derive_readiness`.
    """

    BINDINGS = [
        Binding("up", "move_up", "Previous setting", show=False, priority=True),
        Binding("down", "move_down", "Next setting", show=False, priority=True),
        Binding("s", "start", "Run"),
        Binding("p", "focus_local_project", "Local project"),
        Binding("t", "switch_mode", "Mode"),
        Binding("c", "open_providers", "Providers"),
        Binding("h", "history", "History"),
        Binding("d", "discover", "Discover", show=False),
        Binding("a", "accept_proposal", "Accept", show=False),
        Binding("e", "edit_proposal", "Edit", show=False),
        Binding("r", "rerun_discovery", "Re-run", show=False),
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
        # D2 discovery confirm UX (proposal §2): screen-memory-only state.
        # Only the three ACCEPTED strings ever cross into SessionConfig;
        # confidence/evidence/not-determined/recon summary are never
        # persisted, hashed, or journaled (no such code path exists).
        self._bug_user_edited = False
        self._bug_is_proposed = False
        self._repro_is_proposed = False
        self._verify_is_proposed = False
        self._discovery_proposal = None
        self._discovery_running = False
        self._discovery_generation = 0
        self._discovery_visible_reruns = 0
        self._discovery_consecutive_low = 0
        self._discovery_manual_only = False
        self._discovery_error: Optional[str] = None
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
                # R1 panel language: the form lives in a bordered card with
                # a fixed title row (title left + faint subtitle right)
                # above the scroll.  The footer stays outside the card on
                # the last row so the single-line vocabulary always paints
                # in full (A3 contract).
                with Vertical(id="start-card"):
                    with Horizontal(id="start-header"):
                        yield Static("SESSION SETUP", id="start-section-label")
                        yield Static(LOCAL_SETUP_SUBTITLE, id="start-subtitle")
                    with VerticalScroll(id="start-config"):
                        yield SessionSettingRow("Target", row_key=ROW_TARGET, id="target-row")
                        yield Static("", id="local-context-notes")
                        yield Static("", id="group-where")
                        yield SessionSettingRow("Task", row_key=ROW_TASK, id="task-row")
                        yield SessionSettingRow("Project", row_key=ROW_PROJECT, id="project-row")
                        with Horizontal(id="group-what-row"):
                            yield Static("", id="group-what")
                            yield Button(
                                "Discover (d)", id="discovery-button", classes="secondary-action"
                            )
                        yield Static("", id="discovery-card")
                        yield SessionSettingRow("Bug", row_key=ROW_BUG, id="bug-row")
                        yield SessionSettingRow("Repro", row_key=ROW_REPRO, id="repro-row")
                        yield SessionSettingRow("Verify (P2P)", row_key=ROW_VERIFY, id="verify-row")
                        yield Static("", id="group-how")
                        yield SessionSettingRow("ProjEnv", row_key=ROW_PROJECT_ENV, id="project-env-row")
                        yield SessionSettingRow("Model", row_key=ROW_MODEL, id="model-row")
                        yield SessionSettingRow("Debugger", row_key=ROW_DEBUGGER, id="debugger-row")
                        yield Static("", id="group-bounds")
                        yield SessionSettingRow("Time limit", row_key=ROW_TIME_LIMIT, id="time-limit-row")
                        yield SessionSettingRow("Auto-retry", row_key=ROW_AUTO_RETRY, id="auto-retry-row")
                        yield Static("", id="start-status")
                        yield Static("", id="start-notes")
                        yield Static("", id="start-context-summary")
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
        # Narrow terminals collapse the pre-flight rail into the in-column
        # summary line (rendered by render_state); wide keeps the rail.
        try:
            self.query_one("#start-context-summary", Static).display = width < 100
        except Exception:
            pass

    def _update_footer(self, width: int) -> None:
        # The variant follows what fits, not the width alone: the 36-cell
        # pre-flight rail steals width at >= 100 cols and the footer keeps
        # 2 cells of padding per side, so the full vocabulary only fits on
        # very wide terminals. Below 100 cols this always selects compact.
        footer = self.query_one("#start-footer", Static)
        rail = 36 if width >= 100 else 0
        content = width - rail - 4
        footer.update(START_FOOTER if content >= len(START_FOOTER) else START_FOOTER_COMPACT)
        # R1 compact contract: below 100 cols the card tightens (the header
        # rule drops) so the whole form keeps fitting 80x24; wide keeps the
        # ruled header. Same gates, same rows — density only.
        try:
            card = self.query_one("#start-card")
            if width and width < 100:
                card.add_class("compact")
            else:
                card.remove_class("compact")
        except Exception:
            pass

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

    # -- local-project discovery confirm UX (D2, proposal §2) -------------------
    #
    # D1 `discover_local_project` is called unchanged (no allowlist/cap/rubric
    # edits). Recon runs in a worker thread (read-only, bounded ≤60 s); the UI
    # stays responsive with progress + Esc-cancel (zero mutation on cancel).
    # Proposal metadata lives in screen memory only (`_discovery_proposal`);
    # only the three ACCEPTED strings ever cross into SessionConfig.

    _DISCOVERY_MAX_VISIBLE_RERUNS = 2

    def _clear_discovery_for_new_project(self) -> None:
        """Drop stale recon when Where changes (manual values survive)."""
        try:
            self._discovery_generation += 1
        except Exception:
            pass
        self._discovery_running = False
        self._discovery_proposal = None
        self._discovery_error = None
        self._discovery_visible_reruns = 0
        self._discovery_consecutive_low = 0
        self._discovery_manual_only = False
        try:
            if getattr(self, "_bug_is_proposed", False) and not getattr(self, "_bug_user_edited", False):
                self._config = replace(self._config, bug_description="")
            self._bug_is_proposed = False
            if getattr(self, "_repro_is_proposed", False) and not getattr(self, "_repro_user_edited", False):
                self._config = replace(self._config, reproduction_command=None)
            self._repro_is_proposed = False
            if getattr(self, "_verify_is_proposed", False) and not getattr(self, "_verify_user_edited", False):
                self._config = replace(self._config, verification_command=None)
            self._verify_is_proposed = False
        except Exception:
            pass

    def _discovery_gate(self) -> tuple[bool, str]:
        """Whether Discover may run (Where-clean + Model-live, same inputs)."""
        if not self._is_local_setup():
            return False, "Local Project setup only"
        if getattr(self, "_discovery_running", False):
            return False, "Discovery already running"
        if getattr(self, "_discovery_manual_only", False):
            return False, "Manual form is the path"
        readiness = getattr(self, "_readiness", None)
        if readiness is not None:
            for issue in getattr(readiness, "issues", ()):
                if getattr(issue, "severity", "") == SEVERITY_ERROR and getattr(issue, "field", "") in (ROW_PROJECT, ROW_MODEL):
                    return False, getattr(issue, "message", "Where-clean + live model required")
        if not (self._project_status.ok and self._project_status.state == "clean"):
            message = self._project_status.message or "Choose a clean Git repository to debug."
            return False, message
        if self._config.model.is_offline:
            return False, "Select a live model."
        return True, ""

    def _discovery_proposal_visible(self) -> bool:
        """Whether a/e/r have a proposal to act on (scoped keys)."""
        if not self._is_local_setup():
            return False
        if getattr(self, "_discovery_running", False):
            return False
        if getattr(self, "_discovery_proposal", None) is None:
            return False
        return bool(
            getattr(self, "_bug_is_proposed", False)
            or getattr(self, "_repro_is_proposed", False)
            or getattr(self, "_verify_is_proposed", False)
        )

    def action_discover(self) -> None:
        """D Discover (explicit affordance, no auto-run)."""
        if not self._is_local_setup():
            return
        # Pressing Discover with an existing proposal counts toward the
        # visible budget (prevents bypassing `r` via `d`).
        is_rerun = self._discovery_proposal is not None or self._discovery_visible_reruns > 0
        self._discovery_start(is_rerun=is_rerun)

    def action_rerun_discovery(self) -> None:
        """R Re-run discovery (bounded: 2 visible, then manual-only)."""
        if not self._is_local_setup():
            return
        self._discovery_start(is_rerun=True)

    def action_accept_proposal(self) -> None:
        """A Accept proposal (sets user_edited, clears transients)."""
        if not self._discovery_proposal_visible():
            return
        if getattr(self, "_bug_is_proposed", False):
            self._bug_user_edited = True
            self._bug_is_proposed = False
        if getattr(self, "_repro_is_proposed", False):
            self._repro_user_edited = True
            self._repro_is_proposed = False
            self._repro_is_auto = False
        if getattr(self, "_verify_is_proposed", False):
            self._verify_user_edited = True
            self._verify_is_proposed = False
            self._verify_is_auto = False
        self.render_state()

    def action_edit_proposal(self) -> None:
        """E Edit proposal (opens existing editors prefilled)."""
        if not self._is_local_setup():
            return
        if getattr(self, "_discovery_running", False):
            return
        if getattr(self, "_discovery_proposal", None) is None and getattr(self, "_discovery_error", None) is None:
            return
        focused = self._focused_row_key()
        if focused in (ROW_BUG, ROW_REPRO, ROW_VERIFY):
            self._activate_row(focused)
        else:
            self._activate_row(ROW_BUG)

    def _discovery_start(self, *, is_rerun: bool) -> None:
        if not self._is_local_setup():
            return
        if getattr(self, "_discovery_running", False):
            try:
                self.notify("Discovery already running — Esc cancels", severity="information", timeout=3.0)
            except Exception:
                pass
            return
        if getattr(self, "_discovery_manual_only", False):
            try:
                self.notify(
                    "Manual form is the path — discovery is the default, not the only one",
                    severity="information",
                    timeout=4.0,
                )
            except Exception:
                pass
            self.render_state()
            return
        if is_rerun:
            if self._discovery_proposal is None and self._discovery_error is None and self._discovery_visible_reruns <= 0:
                try:
                    self.notify("Nothing to re-run — press d to Discover first", severity="information", timeout=3.0)
                except Exception:
                    pass
                return
            if self._discovery_visible_reruns >= self._DISCOVERY_MAX_VISIBLE_RERUNS:
                self._discovery_manual_only = True
                try:
                    self.notify(
                        "Manual form is the path — discovery is the default, not the only one",
                        severity="information",
                        timeout=4.0,
                    )
                except Exception:
                    pass
                self.render_state()
                return
        allowed, reason = self._discovery_gate()
        if not allowed:
            try:
                self.notify(f"Discover unavailable — {reason}", severity="information", timeout=4.0)
            except Exception:
                pass
            self.render_state()
            return
        self._discovery_running = True
        self._discovery_error = None
        self._discovery_generation += 1
        generation = self._discovery_generation
        try:
            project_path = self._config.project_path
            launch_cwd = self._launch_cwd
        except Exception:
            project_path = self._config.project_path
            launch_cwd = None
        self.render_state()
        try:
            from functools import partial

            self.run_worker(
                partial(
                    self._discovery_worker,
                    generation,
                    str(project_path),
                    str(launch_cwd) if launch_cwd is not None else None,
                    bool(is_rerun),
                ),
                name=f"discovery-{generation}",
                exclusive=False,
                thread=True,
            )
        except Exception:
            # Worker spawn failed: fail closed to the manual form, zero mutation.
            if generation == self._discovery_generation:
                self._discovery_running = False
                self._discovery_error = "Discovery worker failed to start"
                self.render_state()

    def _discovery_worker(self, generation: int, project_path: str, launch_cwd: Optional[str], is_rerun: bool) -> None:
        """Read-only recon off the UI thread (D1 unchanged, 1 internal retry)."""
        proposal = None
        error: Optional[str] = None
        try:
            from agentic_debugger.application.local_project_discovery import (
                DiscoveryRefusalError,
                discover_local_project,
            )
        except Exception as exc:
            error = f"Discovery unavailable ({str(exc)[:120]})"
            try:
                self.app.call_from_thread(self._discovery_finished, generation, None, error, is_rerun)
            except Exception:
                pass
            return
        try:
            proposal = discover_local_project(project_path, launch_cwd)
            # 1 internal retry: a budget-stop Low gets one second chance
            # (deterministic recon rarely changes, but the retry is bounded).
            try:
                could = " ".join(list(getattr(proposal, "could_not_determine", ()) or []))
                if getattr(proposal, "confidence_overall", "") == "low" and "budget exceeded" in could:
                    proposal = discover_local_project(project_path, launch_cwd)
            except Exception:
                pass
        except Exception as exc:
            # DiscoveryRefusalError and any unexpected failure stay fail-closed:
            # zero mutation, manual form is the path. Message is safe (existing
            # validation copy, never paths beyond the message itself).
            try:
                message = str(exc)[:220] or "Discovery refused"
            except Exception:
                message = "Discovery refused"
            error = message
            proposal = None
        try:
            self.app.call_from_thread(self._discovery_finished, generation, proposal, error, is_rerun)
        except Exception:
            pass

    def _discovery_finished(self, generation: int, proposal: Any, error: Optional[str], is_rerun: bool) -> None:
        """Apply recon on the UI thread (zero mutation on cancel/stale)."""
        if generation != getattr(self, "_discovery_generation", -1):
            return
        if not getattr(self, "_discovery_running", False):
            return
        self._discovery_running = False
        if error is not None or proposal is None:
            message = (error or "Discovery refused")[:220]
            self._discovery_error = message
            self._discovery_proposal = None
            if bool(is_rerun):
                self._discovery_visible_reruns += 1
            self._discovery_consecutive_low += 1
            # Q8 budget: 1 internal + 2 visible, then manual-only. Consecutive
            # Low alone does not exhaust the visible budget (that would allow
            # only 1 re-run, contradicting Q8); manual-only needs 2 visibles.
            if self._discovery_visible_reruns >= self._DISCOVERY_MAX_VISIBLE_RERUNS:
                self._discovery_manual_only = True
            self.render_state()
            try:
                self._focus_row(ROW_BUG)
            except Exception:
                pass
            return
        self._discovery_error = None
        self._discovery_proposal = proposal
        if bool(is_rerun):
            self._discovery_visible_reruns += 1
        confidence = getattr(proposal, "confidence_overall", "low")
        if confidence == "low":
            self._discovery_consecutive_low += 1
        else:
            self._discovery_consecutive_low = 0
        if self._discovery_visible_reruns >= self._DISCOVERY_MAX_VISIBLE_RERUNS:
            # Budget spent with no confident bug → manual-only message.
            # A confident proposal always stays actionable even at budget.
            if confidence == "low":
                self._discovery_manual_only = True
        self._apply_discovery_proposal(proposal)
        self.render_state()
        if confidence == "low":
            try:
                self._focus_row(ROW_BUG)
            except Exception:
                pass

    def _apply_discovery_proposal(self, proposal: Any) -> None:
        """Land proposals as `proposed`-tagged values (never overwrite manual)."""
        try:
            hypotheses = list(getattr(proposal, "hypotheses", ()) or [])
            repro_candidate = getattr(proposal, "repro_candidate", None)
            verify_candidate = getattr(proposal, "verify_candidate", None)
        except Exception:
            return
        # Bug: Medium/High prefill with `proposed` tag (Q6); Low stays empty.
        try:
            if hypotheses and not getattr(self, "_bug_user_edited", False):
                statement = str(getattr(hypotheses[0], "statement", "") or "")
                if statement.strip():
                    self._config = replace(self._config, bug_description=statement)
                    self._bug_is_proposed = True
                else:
                    if getattr(self, "_bug_is_proposed", False):
                        self._config = replace(self._config, bug_description="")
                    self._bug_is_proposed = False
            else:
                if not getattr(self, "_bug_user_edited", False) and getattr(self, "_bug_is_proposed", False) and not hypotheses:
                    # Stale proposed bug with no replacement → clear (never a guess).
                    self._config = replace(self._config, bug_description="")
                    self._bug_is_proposed = False
        except Exception:
            pass
        # Repro/Verify: deterministic candidates still propose on Low.
        try:
            if not getattr(self, "_repro_user_edited", False):
                if isinstance(repro_candidate, str) and repro_candidate:
                    self._config = replace(self._config, reproduction_command=repro_candidate)
                    self._repro_is_proposed = True
                    self._repro_is_auto = False
                elif getattr(self, "_repro_is_proposed", False):
                    self._config = replace(self._config, reproduction_command=None)
                    self._repro_is_proposed = False
        except Exception:
            pass
        try:
            if not getattr(self, "_verify_user_edited", False):
                if isinstance(verify_candidate, str) and verify_candidate:
                    self._config = replace(self._config, verification_command=verify_candidate)
                    self._verify_is_proposed = True
                    self._verify_is_auto = False
                elif getattr(self, "_verify_is_proposed", False):
                    self._config = replace(self._config, verification_command=None)
                    self._verify_is_proposed = False
        except Exception:
            pass

    # -- navigation ------------------------------------------------------------

    def _is_local_setup(self) -> bool:
        return self._config.target == TARGET_LOCAL_PROJECT

    def _focusable_row_ids(self) -> list[str]:
        # Curated/Ladder keep the fixed ROW_ORDER stack.  Local setup
        # groups eight rows plus the Mode switch; Task/Debugger are
        # context notes and never focusable when Local.
        if self._is_local_setup():
            return list(LOCAL_SETUP_FOCUS_ORDER)
        return list(ROW_ORDER)

    def _focus_row(self, row_key: str) -> None:
        # Hidden context-note rows can never take focus when Local;
        # redirect them to the first focusable row instead of focusing
        # a hidden widget.
        if self._is_local_setup() and row_key in (ROW_TASK, ROW_DEBUGGER):
            row_key = ROW_PROJECT
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
        # Local setup never shows foreign-row toasts: Task/Debugger are
        # context notes, not rows, so activating them is a no-op.
        if self._is_local_setup() and row_key in (ROW_TASK, ROW_DEBUGGER):
            return
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
                caption=APPLY_ELIGIBILITY_CAPTION,
            )
        elif row_key == ROW_VERIFY:
            self._open_text_editor(
                "Regression check command (optional; must pass BEFORE and after the fix)",
                self._config.verification_command or "",
                self._on_verify_saved,
                caption=APPLY_ELIGIBILITY_CAPTION,
            )
        elif row_key == ROW_PROJECT_ENV:
            self._open_project_env_builder()
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
        # A new Where invalidates any recon: the proposal pointed at a
        # different repo. Manual values survive; proposed transients clear.
        self._clear_discovery_for_new_project()
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
            # Manual typing supersedes any proposed value (manual > proposed).
            self._bug_user_edited = True
            self._bug_is_proposed = False
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
        self._repro_is_proposed = False
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
        self._verify_is_proposed = False
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

    def _open_project_env_builder(self) -> None:
        """ProjEnv opens the inline builder by default (A2).

        The single-line DSL remains as the advanced toggle inside the
        builder (Edit as text), with round-trip parity both ways.  Every
        builder state serializes to the DSL string; the existing
        ValueError → Issue readiness path stays the gate.
        """
        self.app.push_screen(
            ProjectEnvBuilderScreen(
                initial_text=self._config.project_env_text or "",
                on_save=self._on_project_env_saved,
            )
        )

    def _open_text_editor(
        self,
        title: str,
        current: str,
        on_save: Any,
        multiline: bool = False,
        caption: Optional[str] = None,
    ) -> None:
        self.app.push_screen(
            SingleLineFieldEditorScreen(
                title=title,
                current=current or "",
                on_save=on_save,
                placeholder=title,
                caption=caption,
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
            # Mode switch invalidates recon (different surface); manual survives.
            try:
                self._clear_discovery_for_new_project()
            except Exception:
                pass
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

        # -- Local IA: Target becomes the Mode switch ----------------------
        try:
            target_row = self._row(ROW_TARGET)
            target_row.label = "Mode" if local else "Target"
        except Exception:
            pass
        if local:
            fitted(ROW_TARGET, TARGET_LABELS[config.target], "T to switch")
        else:
            fitted(ROW_TARGET, TARGET_LABELS[config.target])
        fitted(
            ROW_TASK,
            "" if local else self._task_display_name(),
            "" if local else (config.task_id or ""),
        )
        fitted(ROW_PROJECT, (config.project_path or "") if local else "")
        fitted(ROW_BUG, self._bug_preview() if local else "", bug_display_tag(self) if local else "")
        if local:
            fitted(
                ROW_REPRO,
                config.reproduction_command or "Not set (optional)",
                repro_display_tag(self),
            )
            fitted(
                ROW_VERIFY,
                config.verification_command or "Not set (optional)",
                verify_display_tag(self),
            )
        else:
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

        # -- Local IA visibility: groups + notes vs fixed stack ------------
        try:
            self.query_one("#task-row", SessionSettingRow).display = not local
            self.query_one("#debugger-row", SessionSettingRow).display = not local
        except Exception:
            pass
        group_counts = local_group_error_counts(readiness) if local else {}
        try:
            self.query_one("#local-context-notes", Static).display = local
            self.query_one("#group-where", Static).display = local
            self.query_one("#group-what", Static).display = local
            self.query_one("#group-what-row", Horizontal).display = local
            self.query_one("#group-how", Static).display = local
            self.query_one("#group-bounds", Static).display = local
        except Exception:
            pass
        if local:
            try:
                self.query_one("#local-context-notes", Static).update(
                    f"[{FAINT}]{_markup_escape(local_context_notes_text(self))}[/]"
                )
            except Exception:
                pass
            titles = {gid: title for gid, title, _fields in LOCAL_SETUP_GROUPS}
            numbers = {
                GROUP_WHERE: "1",
                GROUP_WHAT: "2",
                GROUP_HOW: "3",
                GROUP_BOUNDS: "4",
            }
            for group_id in (GROUP_WHERE, GROUP_WHAT, GROUP_HOW, GROUP_BOUNDS):
                try:
                    count = group_counts.get(group_id, 0)
                    label = local_group_status_label(group_id, count)
                    self.query_one(f"#group-{group_id}", Static).update(
                        manager_group_header(
                            numbers[group_id],
                            titles[group_id],
                            label,
                            ok=(count <= 0),
                        )
                    )
                except Exception:
                    pass
            # -- D2 Discover affordance + proposal/fallback card (What group) --
            # The What header and Discover button share one Horizontal row
            # (still 1 line total, preserving the A3 80x24 fit); the card below
            # shows only for active states (progress/proposal/fallback/manual).
            try:
                self.query_one("#group-what-row", Horizontal).display = True
            except Exception:
                pass
            try:
                button = self.query_one("#discovery-button", Button)
                button.display = True
                allowed, _reason = self._discovery_gate()
                # Enabled iff Where-clean + Model-live (same readiness inputs);
                # running and manual-only also disable (stop rule, no new gate).
                button.disabled = (not allowed) or bool(getattr(self, "_discovery_running", False))
                try:
                    button.label = (
                        "Discovering…" if getattr(self, "_discovery_running", False)
                        else "Discover (d)"
                    )
                except Exception:
                    pass
            except Exception:
                pass
            try:
                card = self.query_one("#discovery-card", Static)
                # Idle (no recon yet, no error, not running, not manual-only)
                # hides the card to preserve the A3 80x24 fit (Run stays in
                # the initial viewport, clickable without scroll). The button
                # label alone carries the affordance; blockers already explain
                # gate reasons. Active states show the card (progress, proposal,
                # fallback, manual-only).
                show_card = bool(
                    getattr(self, "_discovery_running", False)
                    or getattr(self, "_discovery_proposal", None) is not None
                    or getattr(self, "_discovery_error", None)
                    or getattr(self, "_discovery_manual_only", False)
                )
                card.display = show_card
                if show_card:
                    bar = discovery_bar_text(self, readiness)
                    body = discovery_card_text(self)
                    if body and body != bar:
                        card.update(
                            f"[{FAINT}]{_markup_escape(bar)}[/]\n"
                            f"[{MUTED}]{_markup_escape(body)}[/]"
                        )
                    elif bar:
                        card.update(f"[{FAINT}]{_markup_escape(bar)}[/]")
                    else:
                        card.update("")
                else:
                    try:
                        card.update("")
                    except Exception:
                        pass
            except Exception:
                pass
        else:
            try:
                self.query_one("#group-what-row", Horizontal).display = False
            except Exception:
                pass
            try:
                self.query_one("#discovery-button", Button).display = False
            except Exception:
                pass
            try:
                self.query_one("#discovery-card", Static).display = False
            except Exception:
                pass

        # -- blockers / status (every blocker as a checklist) --------------
        status = self.query_one("#start-status", Static)
        if self._start_error is not None:
            status.update(f"[{ERROR}]! Start failed — {_markup_escape(self._start_error)}[/]")
        elif not readiness.ready:
            errors = [item for item in readiness.issues if item.severity == SEVERITY_ERROR]
            lines = [f"[bold {ERROR}]Blockers ({len(errors)}):[/]"]
            lines.extend(
                f"[{ERROR}]! {_markup_escape(item.message)}[/]" for item in errors
            )
            status.update("\n".join(lines))
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

        # -- narrow pre-flight summary (rail collapsed below 100 cols) -----
        try:
            summary = self.query_one("#start-context-summary", Static)
            if local:
                if readiness.ready:
                    summary.update(
                        f"[{SUCCESS}]Pre-flight: ready — "
                        f"{_markup_escape(readiness.run_label.lower())}[/]"
                    )
                else:
                    parts: list[str] = []
                    for group_id, title, _fields in LOCAL_SETUP_GROUPS:
                        count = group_counts.get(group_id, 0)
                        mark = "\u2713" if count <= 0 else f"!{count}"
                        parts.append(f"{title} {mark}")
                    errors = [
                        item for item in readiness.issues if item.severity == SEVERITY_ERROR
                    ]
                    summary.update(
                        f"[{ERROR}]Pre-flight: {len(errors)} blocker(s)[/]"
                        f"  [{FAINT}]{' · '.join(parts)}[/]"
                    )
            else:
                if readiness.ready:
                    summary.update(
                        f"[{SUCCESS}]Pre-flight: ready — "
                        f"{_markup_escape(readiness.run_label.lower())}[/]"
                    )
                else:
                    errors = [
                        item for item in readiness.issues if item.severity == SEVERITY_ERROR
                    ]
                    summary.update(
                        f"[{ERROR}]Pre-flight: {len(errors)} blocker(s)[/]"
                    )
            # Visibility follows the rail: summary only when rail hidden.
            try:
                summary.display = self.size.width < 100
            except Exception:
                pass
        except Exception:
            pass

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
        # D2: Esc during recon cancels discovery with zero mutation (stays on
        # the form); otherwise Esc is Back. The worker thread is read-only and
        # bounded — its late result is ignored via the generation guard.
        if getattr(self, "_discovery_running", False):
            try:
                self._discovery_generation += 1
            except Exception:
                pass
            self._discovery_running = False
            try:
                self.render_state()
            except Exception:
                pass
            return
        self.app.pop_screen()

    def action_history(self) -> None:
        self.app.pop_screen()

    def action_focus_local_project(self) -> None:
        """P: jump straight to the Local Project controls (same screen)."""
        if self._config.target != TARGET_LOCAL_PROJECT:
            self._choice_selected(ROW_TARGET, TARGET_LOCAL_PROJECT)
        self._focus_row(ROW_PROJECT)

    def action_switch_mode(self) -> None:
        """T: open the Target mode switch (same screen, all targets)."""
        self._open_target_picker()

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
        elif event.button.id == "discovery-button":
            self.action_discover()
            event.stop()
