"""Domain presentation panes of the Local Application V1 workspace.

Every pane renders exclusively from the immutable
:class:`~agentic_debugger.application.presentation.SessionViewState` produced
by the shared pure reducer.  Widgets never import PDB, PatchManager,
controller, or verifier implementation internals, and never execute domain
logic: missing recorded data is rendered as an explicit ``NOT RECORDED`` /
empty state, never fabricated.

Panes keep their own scroll/focus state (UI-owned); all presentation facts
come from the view.
"""

from __future__ import annotations

from typing import Any, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from agentic_debugger.application.presentation import (
    DebuggerViewState,
    PatchStage,
    SessionViewState,
    VerifierSummaryView,
    current_source,
)

_NOT_RECORDED = "NOT RECORDED"

_ACTIVITY_FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "all"),
    ("lifecycle", "lifecycle (session/cleanup)"),
    ("controller", "controller"),
    ("model", "model requests"),
    ("tools", "tools"),
    ("debugger", "debugger"),
    ("patch", "patch/source"),
    ("verifier", "verifier"),
)

_ACTIVITY_FILTER_KINDS: dict[str, frozenset[str]] = {
    "all": frozenset(),
    "lifecycle": frozenset(
        {
            "session.created",
            "session.started",
            "session.status_changed",
            "session.cancel_requested",
            "session.completed",
            "session.failed",
            "session.cancelled",
            "cleanup.started",
            "cleanup.completed",
        }
    ),
    "controller": frozenset({"controller.step", "controller.transition"}),
    "model": frozenset(
        {
            "model.request_started",
            "model.request_completed",
            "model.directive_accepted",
            "model.directive_rejected",
            "model.configured",
        }
    ),
    "tools": frozenset({"tool.started", "tool.completed"}),
    "debugger": frozenset(
        {
            "debugger.started",
            "debugger.location_changed",
            "debugger.stack_observed",
            "debugger.locals_observed",
        }
    ),
    "patch": frozenset(
        {
            "patch.proposed",
            "patch.rejected",
            "patch.apply_failed",
            "patch.applied",
            "patch.reverted",
            "source.snapshot",
            "diagnosis.recorded",
        }
    ),
    "verifier": frozenset(
        {
            "verifier.started",
            "verifier.stage_started",
            "verifier.stage_completed",
            "verifier.completed",
        }
    ),
}

_KIND_STYLE: dict[str, str] = {
    "session.created": "bold",
    "session.started": "bold",
    "session.status_changed": "bold",
    "session.cancel_requested": "bold yellow",
    "session.completed": "bold green",
    "session.failed": "bold red",
    "session.cancelled": "bold yellow",
    "controller.step": "dim",
    "controller.transition": "cyan",
    "model.request_started": "blue",
    "model.request_completed": "blue",
    "model.directive_accepted": "green",
    "model.directive_rejected": "yellow",
    "tool.started": "magenta",
    "tool.completed": "magenta",
    "debugger.started": "deep_sky_blue1",
    "debugger.location_changed": "deep_sky_blue1",
    "debugger.stack_observed": "deep_sky_blue1",
    "debugger.locals_observed": "deep_sky_blue1",
    "patch.proposed": "yellow",
    "patch.rejected": "red",
    "patch.apply_failed": "red",
    "patch.applied": "orange1",
    "patch.reverted": "orange1",
    "source.snapshot": "steel_blue1",
    "diagnosis.recorded": "steel_blue1",
    "verifier.started": "green",
    "verifier.stage_started": "green",
    "verifier.stage_completed": "green",
    "verifier.completed": "bold green",
    "cleanup.started": "dim",
    "cleanup.completed": "dim",
    "artifact.written": "dim",
}


def _append_section(text: Text, title: str) -> None:
    """Append one section heading as plain text with its own style.

    Recorded/derived text is always appended as plain ``rich.text.Text``;
    styling is supplied separately and markup is never embedded in a
    preconstructed ``Text`` object.
    """
    text.append("\n")
    text.append(title, style="bold #8fb7d9")
    text.append("\n")


def _append_kv(text: Text, key: str, value: str) -> None:
    """Append one ``key: value`` line; both parts stay plain text."""
    text.append(f"{key}: ", style="dim")
    text.append(f"{value}\n")


def _stage_style(stage: PatchStage) -> str:
    return {
        PatchStage.PROPOSED: "yellow",
        PatchStage.REJECTED: "red",
        PatchStage.APPLY_FAILED: "red",
        PatchStage.APPLIED: "orange1",
        PatchStage.REVERTED: "orange1",
        PatchStage.VERIFIED: "green",
    }[stage]


class SourcePanel(VerticalScroll):
    """Recorded source with the current execution line highlighted."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(self, view: SessionViewState) -> None:
        self._view = view
        self._text.update(self._render_view(view))

    @staticmethod
    def _render_view(view: SessionViewState) -> Text:
        source = current_source(view)
        debugger = view.debugger
        if source is None:
            if not view.sources:
                text = Text("No source snapshot recorded for this session.\n\n")
                text.append("NOT RECORDED", style="bold dim")
                return text
            text = Text(
                "The debugger's current file has no recorded source snapshot "
                "(source for the current execution location is not recorded).\n\n",
                style="yellow",
            )
            text.append("NOT RECORDED", style="bold dim")
            return text
        text = Text()
        text.append(
            f"{source.path}  ·  {source.stage.value}  ·  sha256 "
            f"{source.sha256[:12]}…"
        )
        if source.truncated:
            text.append("  ·  [truncated]", style="yellow")
        text.append(f"  ·  {source.line_count} lines", style="dim")
        text.append("\n")
        text.append("─" * 40, style="dim")
        text.append("\n")
        highlight_line = (
            debugger.line
            if debugger.script == source.path and debugger.line is not None
            else None
        )
        lines = source.text.splitlines() or [""]
        highlighted = highlight_line is not None and 1 <= highlight_line <= len(lines)
        gutter_width = max(3, len(str(len(lines))))
        for index, line in enumerate(lines, start=1):
            gutter = Text(f"{index:>{gutter_width}} ", style="dim")
            if index == highlight_line:
                gutter.stylize("bold yellow")
            text.append_text(gutter)
            content = Text(line, no_wrap=True)
            if index == highlight_line:
                content.stylize("bold yellow on #3a2f00")
                text.append("▶ ", style="bold yellow")
            else:
                text.append("  ")
            text.append_text(content)
            text.append("\n")
        if not highlighted and debugger.script is not None:
            text.append(
                f"\n(no execution-line marker: debugger is at "
                f"{debugger.script}:{debugger.line or '?'})",
                style="dim",
            )
        return text


class DebuggerPanel(VerticalScroll):
    """Recorded debugger state: location, breakpoints, stack, locals."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(self, view: SessionViewState) -> None:
        self._view = view
        self._text.update(self._render_view(view))

    @staticmethod
    def _render_view(view: SessionViewState) -> Text:
        debugger: DebuggerViewState = view.debugger
        text = Text()
        if not debugger.session_started:
            text.append("No debugger session was recorded.\n\n")
            text.append(_NOT_RECORDED, style="dim italic")
            return text
        _append_section(text, "Current location")
        _append_kv(text, "script", debugger.script if debugger.script else "—")
        _append_kv(text, "line", str(debugger.line) if debugger.line is not None else "—")
        _append_kv(
            text,
            "function",
            debugger.function if debugger.function else "—",
        )
        _append_kv(
            text,
            "pause generation",
            str(debugger.pause_generation)
            if debugger.pause_generation is not None
            else "not recorded",
        )
        _append_section(text, "Breakpoints")
        if debugger.breakpoints:
            for item in debugger.breakpoints:
                text.append(f"  • {item}\n")
        else:
            text.append("  none recorded\n")
        _append_section(text, "Stack")
        if debugger.frames:
            for frame in debugger.frames:
                marker = "▶ " if frame.is_current else "  "
                line = (
                    f"{marker}#{frame.index} {frame.function} — "
                    f"{frame.file}:{frame.line}"
                )
                text.append(line, style="bold" if frame.is_current else "default")
                text.append("\n")
        else:
            text.append("  no stack recorded\n")
        _append_section(text, "Locals (current recorded frame)")
        if debugger.locals:
            for local in debugger.locals:
                if "redacted" in local.summary:
                    text.append(f"  {local.name} = ", style="yellow")
                    text.append(local.summary, style="bold red")
                    text.append("\n")
                else:
                    text.append(f"  {local.name} = {local.summary}\n")
        else:
            text.append("  no locals recorded\n")
        if debugger.pause_generation is None:
            text.append(
                "\npause generation: not recorded (stale guard "
                "applies in stream order)\n",
                style="dim",
            )
        return text


class PatchPanel(VerticalScroll):
    """Recorded patch-attempt lifecycle (never a correctness verdict)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(self, view: SessionViewState) -> None:
        self._view = view
        self._text.update(self._render_view(view))

    @staticmethod
    def _render_view(view: SessionViewState) -> Text:
        text = Text()
        if not view.patch_attempts:
            text.append("No patch attempts were recorded.\n\n")
            text.append(_NOT_RECORDED, style="dim italic")
            return text
        for attempt in view.patch_attempts:
            stage = attempt.stage
            text.append(
                f"Attempt {attempt.attempt_index} — {stage.value.upper()}",
                style=_stage_style(stage),
            )
            text.append("\n")
            if attempt.patch_sha256:
                text.append(f"  sha256: {attempt.patch_sha256[:16]}…\n", style="dim")
            if attempt.changed_files:
                text.append(
                    f"  changed files: {', '.join(attempt.changed_files)}\n"
                )
            if attempt.syntax_passed is not None:
                text.append(f"  syntax passed: {attempt.syntax_passed}\n")
            if attempt.rejection_reason:
                text.append(f"  rejection: {attempt.rejection_reason}\n", style="yellow")
            if attempt.apply_failure_reason:
                text.append(
                    f"  apply failure: {attempt.apply_failure_reason}\n",
                    style="red",
                )
            if attempt.patch_text:
                text.append("\n", style="dim")
                for line in attempt.patch_text.splitlines():
                    if line.startswith("+") and not line.startswith("+++"):
                        style = "green"
                    elif line.startswith("-") and not line.startswith("---"):
                        style = "red"
                    elif line.startswith("@@"):
                        style = "cyan"
                    else:
                        style = "dim"
                    text.append(line, style=style)
                    text.append("\n")
                text.append("\n")
        text.append(
            "\nPatch application only mutates the recorded workspace. "
            "APPLIED does not mean FIXED — correctness is decided by the "
            "independent verifier only.\n",
            style="dim",
        )
        return text


class VerifierPanel(VerticalScroll):
    """Verifier progress and final result (the correctness authority)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(self, view: SessionViewState) -> None:
        self._view = view
        self._text.update(self._render_view(view))

    @staticmethod
    def _render_view(view: SessionViewState) -> Text:
        text = Text()
        if view.verifier_stages:
            _append_section(text, "Stages (progress only)")
            for stage in view.verifier_stages:
                style = {
                    "running": "blue",
                    "completed": "green",
                    "failed": "red",
                    "skipped": "dim",
                    "cancelled": "yellow",
                }.get(stage.status.value, "default")
                text.append(
                    f"  {stage.stage.value:<32} {stage.status.value}", style=style
                )
                text.append("\n")
        summary: Optional[VerifierSummaryView] = view.verifier_summary
        if summary is None:
            text.append("\nNo verifier result recorded.\n\n")
            if view.verifier_stages:
                text.append("Verifier is in progress or was interrupted.", style="yellow")
            else:
                text.append(_NOT_RECORDED, style="dim italic")
            return text
        _append_section(text, "Final verifier result (authoritative)")
        _append_kv(text, "status", summary.status if summary.status else "—")
        _append_kv(
            text,
            "outcome",
            summary.outcome.value if summary.outcome else "—",
        )
        _append_kv(text, "fail-to-pass", _counts(summary.f2p_passed, summary.f2p_total))
        _append_kv(text, "pass-to-pass", _counts(summary.p2p_passed, summary.p2p_total))
        if summary.workspace_cleaned is not None:
            _append_kv(text, "workspace cleaned", str(summary.workspace_cleaned))
        text.append(
            "\nThe verifier result is the correctness authority. "
            "Application completion is operational only.\n",
            style="dim",
        )
        return text


def _counts(passed: Optional[int], total: Optional[int]) -> str:
    if passed is None and total is None:
        return "not recorded"
    return f"{passed}/{total}"


class ActivityPanel(VerticalScroll):
    """Filterable activity timeline (controller/model/tool/debugger/…)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None
        self.filter: str = "all"

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(self, view: SessionViewState) -> None:
        self._view = view
        self._text.update(self._render_view(view))

    def cycle_filter(self, step: int = 1) -> str:
        names = [name for name, _ in _ACTIVITY_FILTERS]
        current = names.index(self.filter) if self.filter in names else 0
        self.filter = names[(current + step) % len(names)]
        if self._view is not None:
            self._text.update(self._render_view(self._view))
        return self.filter

    def _render_view(self, view: SessionViewState) -> Text:
        text = Text()
        text.append("Filter: ", style="dim")
        text.append(self.filter, style="bold")
        text.append(
            f" ({dict(_ACTIVITY_FILTERS)[self.filter]})  —  keys: 1..7 "
            "filter (1 = all)\n",
            style="dim",
        )
        text.append("─" * 40, style="dim")
        text.append("\n")
        allowed = _ACTIVITY_FILTER_KINDS[self.filter]
        entries = [
            entry
            for entry in view.timeline
            if not allowed or entry.event_kind.value in allowed
        ]
        if not entries:
            text.append("No activity recorded for this filter.", style="dim")
            return text
        for entry in reversed(entries):
            style = _KIND_STYLE.get(entry.event_kind.value, "default")
            text.append(f"#{entry.sequence:<5} ", style="dim")
            text.append(entry.summary, style=style)
            text.append("\n")
        return text


class TimelinePanel(VerticalScroll):
    """Concise event timeline with effective phase-boundary markers.

    ``phase_boundary_sequences`` is derived by the screen from the recorded
    event stream (the same ``phase_boundaries`` derivation the replay cursor
    uses); the pane itself only renders it.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None
        self._boundaries: frozenset[int] = frozenset()

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(
        self,
        view: SessionViewState,
        phase_boundary_sequences: Optional[frozenset[int]] = None,
    ) -> None:
        self._view = view
        if phase_boundary_sequences is not None:
            self._boundaries = phase_boundary_sequences
        self._text.update(self._render_view(view))

    def _render_view(self, view: SessionViewState) -> Text:
        text = Text()
        if not view.timeline:
            text.append("No events recorded.", style="dim")
            return text
        for entry in view.timeline:
            marker = "» " if entry.sequence in self._boundaries else "  "
            style = _KIND_STYLE.get(entry.event_kind.value, "default")
            text.append(f"{marker}#{entry.sequence:<5} ", style="dim")
            text.append(entry.summary, style=style)
            text.append("\n")
        return text


class StatusHeader(Static):
    """One compact status header line derived from the presentation view.

    The header is a widget boundary: it renders the same ``SessionViewState``
    as every pane and never touches domain state directly.
    """


class ReplayBar(Static):
    """Replay-control footer (position + key hints)."""


class LiveBar(Static):
    """Live-session footer (operational status + cancel hint)."""


__all__ = [
    "ActivityPanel",
    "DebuggerPanel",
    "LiveBar",
    "PatchPanel",
    "ReplayBar",
    "SourcePanel",
    "StatusHeader",
    "TimelinePanel",
    "VerifierPanel",
]
