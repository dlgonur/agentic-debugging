"""Secondary workspace screens: jump-to-sequence and effort modals.

This module owns the small secondary screens opened only from the
workspace surface: the sequence jump modal and the effort display
modal.  They are presentation-only and never own session state.
"""

from __future__ import annotations

from typing import Any, Optional

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Static

from agentic_debugger.ui.theme import PRIMARY

class JumpToSequenceScreen(Screen):
    """One compact input modal for jumping to a replay sequence."""

    BINDINGS = [Binding("escape", "cancel", "Back")]

    def __init__(
        self,
        on_submit: Any,
        min_sequence: int = 0,
        max_sequence: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._on_submit = on_submit
        self._min_sequence = min_sequence
        self._max_sequence = max_sequence

    def compose(self) -> ComposeResult:
        with Static(id="jump-dialog"):
            yield Static(f"[bold {PRIMARY}]Jump to sequence[/]", id="jump-title")
            placeholder = (
                f"sequence number ({self._min_sequence}–{self._max_sequence})"
                if self._max_sequence is not None
                else "sequence number"
            )
            yield Input(id="jump-input", placeholder=placeholder)
            hint = (
                f"[dim]enter: jump ({self._min_sequence}–{self._max_sequence}) · escape: cancel[/]"
                if self._max_sequence is not None
                else "[dim]enter: jump · escape: cancel[/]"
            )
            yield Static(hint, id="jump-hint")

    def on_mount(self) -> None:
        self.query_one("#jump-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "jump-input":
            return
        raw = event.input.value.strip()
        sequence: Optional[int] = None
        if raw:
            try:
                sequence = int(raw)
            except ValueError:
                self.notify("Sequence must be a whole number.", severity="warning")
                return
        self._on_submit(sequence)
        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()


class EffortModalScreen(Screen):
    """Read-only 'what the agent tried' projection modal."""

    BINDINGS = [
        Binding("escape", "close_effort", "Close"),
        Binding("enter", "close_effort", "Close"),
        Binding("w", "close_effort", "Close"),
    ]

    def __init__(self, body: str) -> None:
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="effort-dialog"):
            yield Static(self._body, id="effort-body")

    def action_close_effort(self) -> None:
        self.app.pop_screen()
