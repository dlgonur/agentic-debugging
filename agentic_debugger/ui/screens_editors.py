"""Session-setup editor family for the Agentic Debugger TUI.

Small single-purpose screens opened by the session-setup surface: the
shared choice picker (+ its option DTO), the focusable setting row, the
time-limit and single-line field editors, the directory browser, and the
multiline bug-description editor.

Editors never reference the setup screen by name; they return through
bounded on_save/on_select/on_cancel callbacks, so this module depends only
on the shared vocabulary, never on its caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, OptionList, Static, TextArea

from agentic_debugger.ui.screens_shared import _markup_escape
from agentic_debugger.ui.theme import ERROR, FAINT, FOREGROUND, MUTED, PRIMARY, SUCCESS


@dataclass(frozen=True)
class ChoiceOption:
    """One option shown by ChoicePickerScreen.

    ``disabled`` options stay visible with their ``disabled_reason`` —
    incompatibilities are explained, never hidden.  A ``group`` label is
    rendered once as a section header when the group changes.  ``group_note``
    communicates provider-level status once at the group header.
    """

    value: str
    title: str
    description: str = ""
    secondary: str = ""
    group: str = ""
    group_note: str = ""
    disabled: bool = False
    disabled_reason: str = ""


class SessionSettingRow(Static):
    """A compact, keyboard-focusable terminal setting row.

    A row always occupies its place in the stack.  When the current
    target makes it inapplicable it renders dimmed with its reason
    (``set_disabled``) instead of disappearing, and activation explains
    why instead of silently changing anything.
    """

    can_focus = True

    def __init__(self, label: str, *, row_key: str, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.label = label
        self.row_key = row_key
        self._value = ""
        self._secondary = ""
        self._focused = False
        self._disabled = False
        self._disabled_reason = ""

    def set_value(self, value: str, *, secondary: str = "") -> None:
        self._value, self._secondary = value, secondary
        self._render_row()

    def set_disabled(self, reason: str) -> None:
        self._disabled = True
        self._disabled_reason = reason
        self._render_row()

    def set_enabled(self) -> None:
        self._disabled = False
        self._disabled_reason = ""
        self._render_row()

    @property
    def is_disabled(self) -> bool:
        return self._disabled

    @property
    def disabled_reason(self) -> str:
        return self._disabled_reason

    def _render_row(self) -> None:
        text = Text()
        if self._disabled:
            text.append("  ", style=FAINT)
            text.append(f"{self.label:<14}", style=FAINT)
            if self._value:
                text.append(self._value, style=FAINT)
            if self._disabled_reason:
                text.append(f"  ({self._disabled_reason})", style=f"dim {FAINT}")
            self.update(text)
            return
        focused = self._focused
        text.append("› " if focused else "  ", style=f"bold {PRIMARY}" if focused else FAINT)
        text.append(f"{self.label:<14}", style=MUTED)
        text.append(self._value, style=f"bold {FOREGROUND}" if focused else FOREGROUND)
        if self._secondary:
            text.append(f"  {self._secondary}", style=MUTED)
        self.update(text)

    def on_focus(self) -> None:
        self._focused = True
        self._render_row()

    def on_blur(self) -> None:
        self._focused = False
        self._render_row()

    def on_click(self, event: Any) -> None:
        self.focus()
        self.screen._activate_row(self.row_key)  # type: ignore[attr-defined]
        event.stop()

    def on_key(self, event: Any) -> None:
        if getattr(event, "key", None) in ("enter", "space"):
            self.screen._activate_row(self.row_key)  # type: ignore[attr-defined]
            event.prevent_default()
            event.stop()


class TimeLimitEditorInput(Input):
    """Modal time editor input with reliable Enter submission."""

    def on_key(self, event: Any) -> None:
        if getattr(event, "key", None) == "enter":
            self.screen.action_save()  # type: ignore[attr-defined]
            event.prevent_default()
            event.stop()
            return


class TimeLimitEditorScreen(Screen):
    """Small flat modal for editing the optional elapsed-time limit."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "save", "Save", show=False),
    ]

    def __init__(
        self,
        *,
        current: Optional[int],
        on_save: Callable[[Optional[int]], None],
        on_cancel: Callable[[], None],
    ) -> None:
        super().__init__()
        self.current = current
        self._on_save = on_save
        self._on_cancel = on_cancel

    def compose(self) -> ComposeResult:
        with Vertical(id="time-limit-dialog"):
            yield Static("Set time limit", id="time-limit-title")
            yield Static("Seconds", id="time-limit-label")
            yield TimeLimitEditorInput(
                value="" if self.current is None else str(self.current),
                type="integer",
                id="time-limit-editor",
            )
            yield Static("Empty value means no limit.", id="time-limit-help")
            yield Static("enter save   esc cancel", id="time-limit-hint")
            yield Static("", id="time-limit-error")

    def on_mount(self) -> None:
        self.query_one("#time-limit-editor", Input).focus()

    def action_save(self) -> None:
        raw = self.query_one("#time-limit-editor", Input).value.strip()
        error = self.query_one("#time-limit-error", Static)
        if not raw:
            self._close(None)
            return
        try:
            value = int(raw)
        except ValueError:
            error.update("time limit must be a whole number of seconds")
            return
        if value < 1:
            error.update("time limit must be at least 1 second")
            return
        self._close(value)

    def _close(self, value: Optional[int]) -> None:
        self.app.pop_screen()
        self._on_save(value)

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_cancel()


class SingleLineEditorInput(Input):
    """Single-line input with reliable Enter -> save for focused editor."""

    def on_key(self, event: Any) -> None:
        if getattr(event, "key", None) == "enter":
            # Single-line editors save on Enter; multiline Bug editor does not.
            try:
                self.screen.action_save()  # type: ignore[attr-defined]
            except Exception:
                pass
            event.prevent_default()
            event.stop()
            return


class SingleLineFieldEditorScreen(Screen):
    """Reusable centered single-line editor in the same family as Bug picker.

    Visual: centered semantic dialog, dark input with a rounded focus border,
    primary Save action, and footer "Enter save    Esc cancel".

    Keyboard contract (single-line):
        Enter => save
        Esc   => cancel (no mutation)
        Save click => save
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "save", "Save", show=False),
    ]

    def __init__(
        self,
        *,
        title: str,
        current: str,
        on_save: Callable[[Optional[str]], None],
        placeholder: str = "",
        max_length: Optional[int] = None,
        caption: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.current = current or ""
        self.placeholder = placeholder or title
        self.max_length = max_length
        self.caption_text = caption or ""
        self._on_save = on_save

    def compose(self) -> ComposeResult:
        with Vertical(id="single-line-dialog"):
            yield Static(self.title_text, id="single-line-title")
            if self.caption_text:
                yield Static(self.caption_text, id="single-line-note")
            yield SingleLineEditorInput(
                value=self.current,
                placeholder=self.placeholder,
                id="single-line-editor",
            )
            with Horizontal(id="single-line-actions"):
                yield Button("Save", id="single-line-save-button", classes="primary-action")
            yield Static("Enter save    Esc cancel", id="single-line-hint")
            yield Static("", id="single-line-error")

    def on_mount(self) -> None:
        inp = self.query_one("#single-line-editor", Input)
        inp.focus()
        try:
            inp.cursor_position = len(inp.value)
        except Exception:
            pass

    def action_save(self) -> None:
        raw = self.query_one("#single-line-editor", Input).value
        if self.max_length is not None and len(raw.encode("utf-8")) > self.max_length:
            self.query_one("#single-line-error", Static).update(
                f"value exceeds {self.max_length} bytes — shorten it before saving"
            )
            return
        self.app.pop_screen()
        self._on_save(raw)

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_save(None)

    def on_button_pressed(self, event: Any) -> None:
        if getattr(event.button, "id", None) == "single-line-save-button":
            self.action_save()
            event.stop()


class ChoicePickerScreen(Screen):
    """One shared flat picker for mode, task, debugger, and model choices."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        title: str,
        choices: list[ChoiceOption],
        current: Optional[str],
        on_select: Callable[[str], None],
        subtitle: Optional[str] = None,
        empty_text: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.title, self.choices, self.current = title, list(choices), current
        self.subtitle = subtitle
        self.empty_text = empty_text
        self._on_select = on_select

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-picker-dialog"):
            yield Static(self.title, id="choice-picker-title")
            if self.subtitle:
                yield Static(self.subtitle, id="choice-picker-subtitle")
            yield OptionList(id="choice-picker-list")
            yield Static("up/down navigate   enter select   esc cancel", id="choice-picker-hint")

    def on_mount(self) -> None:
        option_list = self.query_one("#choice-picker-list", OptionList)
        from textual.widgets.option_list import Option

        last_group = None
        for choice in self.choices:
            if choice.group and choice.group != last_group:
                last_group = choice.group
                header = Text()
                header.append(f"{choice.group.upper()}", style=f"bold {PRIMARY}")
                if choice.group_note:
                    header.append(f"   {choice.group_note}", style=f"dim italic {MUTED}")
                option_list.add_option(
                    Option(header, disabled=True)
                )
            option_list.add_option(
                Option(
                    self._option_prompt(choice, None),
                    disabled=choice.disabled,
                    id=self._option_id(choice),
                )
            )
        selectable = [index for index, choice in enumerate(self.choices) if not choice.disabled]
        if self.choices:
            current = next(
                (i for i, choice in enumerate(self.choices) if choice.value == self.current and not choice.disabled),
                None,
            )
            if current is None and selectable:
                current = min(selectable)
            if current is not None:
                # Map the choice index onto its OptionList slot (group
                # headers occupy option slots of their own).
                option_list.highlighted = self._option_slots[current]
            option_list.focus()
        else:
            option_list.display = False
            msg = self.empty_text or "No eligible choices available."
            self.mount(Static(msg, id="choice-picker-empty"),
                       before=self.query_one("#choice-picker-hint"))

    def _option_id(self, choice: ChoiceOption) -> str:
        return f"choice::{choice.value}"

    @property
    def _option_slots(self) -> dict[int, int]:
        """choice index -> OptionList option index (headers add slots)."""
        slots: dict[int, int] = {}
        slot = 0
        last_group = None
        for index, choice in enumerate(self.choices):
            if choice.group and choice.group != last_group:
                last_group = choice.group
                slot += 1
            slots[index] = slot
            slot += 1
        return slots

    def _option_prompt(self, choice: ChoiceOption, highlighted_index: Optional[int]) -> Text:
        selected = choice.value == self.current
        focused = (
            highlighted_index is not None
            and highlighted_index == self.query_one("#choice-picker-list", OptionList).highlighted
        )
        active = selected or focused
        if choice.disabled:
            text = Text()
            text.append("  ", style=FAINT)
            text.append(choice.title, style=FAINT)
            if choice.secondary:
                text.append(f"  {choice.secondary}", style=FAINT)
            return text
        text = Text()
        text.append("› " if active else "  ", style=f"bold {PRIMARY}" if active else MUTED)
        text.append(choice.title, style=f"bold {FOREGROUND}" if active else FOREGROUND)
        if choice.secondary:
            text.append(f"  {choice.secondary}", style=MUTED)
        if choice.description:
            text.append(f"  {choice.description}", style=MUTED)
        return text

    @property
    def _slot_choices(self) -> dict[int, int]:
        """OptionList option index -> choice index (header slots excluded)."""
        return {slot: index for index, slot in self._option_slots.items()}

    def _refresh_option_markers(self) -> None:
        option_list = self.query_one("#choice-picker-list", OptionList)
        highlighted = option_list.highlighted
        for slot, index in self._slot_choices.items():
            option_list.replace_option_prompt_at_index(
                slot, self._option_prompt(self.choices[index], highlighted)
            )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._refresh_option_markers()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = getattr(event, "option_index", None)
        if index is None:
            index = self.query_one("#choice-picker-list", OptionList).highlighted
        if index is None:
            return
        choice_index = self._slot_choices.get(index)
        if choice_index is None or not 0 <= choice_index < len(self.choices):
            return
        choice = self.choices[choice_index]
        if choice.disabled:
            return
        self.app.pop_screen()
        self._on_select(choice.value)

    def action_cancel(self) -> None:
        self.app.pop_screen()


class BrowseScreen(Screen):
    """Minimal terminal-native directory picker (no OS dialog).

    Shows parent directory, child directories, and Select-current action.
    Uses ``pathlib``/``os.scandir`` only (no native file-dialog dependency).
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Select"),
        Binding("backspace", "parent", "Parent"),
    ]

    def __init__(
        self,
        *,
        start_path: str | os.PathLike[str],
        on_select: Any,
    ) -> None:
        super().__init__()
        self._current = Path(start_path).resolve() if start_path else Path.cwd().resolve()
        self._on_select = on_select

    def compose(self) -> ComposeResult:
        with Vertical(id="browse-dialog"):
            yield Static("Browse — select project directory", id="browse-title")
            yield Static("", id="browse-current")
            yield Static("[dim]parent: .. (backspace)   select current: enter on first row[/]", id="browse-hint")
            yield OptionList(id="browse-list")
            yield Static("up/down navigate   enter select   backspace parent   esc cancel", id="browse-footer")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        try:
            cur_text = str(self._current)
        except Exception:
            cur_text = "—"
        self.query_one("#browse-current", Static).update(
            f"[bold {PRIMARY}]Current:[/] {_markup_escape(cur_text)}"
        )
        option_list = self.query_one("#browse-list", OptionList)
        option_list.clear_options()
        # First option is "Select current directory"
        option_list.add_option(
            Text(
                f"▶ Use current directory: {self._current.name or str(self._current)}",
                style=f"bold {SUCCESS}",
            )
        )
        # Parent
        parent = self._current.parent
        if parent != self._current:
            option_list.add_option(Text(f"↑ Parent: {parent}", style=MUTED))
        # Children
        try:
            from agentic_debugger.application.local_project import list_child_directories
            children = list_child_directories(self._current)
            for child in children[:64]:
                option_list.add_option(Text(f"  {child.name}/", style=FOREGROUND))
            if len(children) > 64:
                option_list.add_option(Text(f"  … +{len(children)-64} more", style="dim"))
        except Exception as exc:
            option_list.add_option(Text(f"(cannot list: {exc})", style=ERROR))
        option_list.highlighted = 0
        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = getattr(event, "option_index", None)
        if idx is None:
            idx = self.query_one("#browse-list", OptionList).highlighted
        if idx is None:
            return
        if idx == 0:
            self.app.pop_screen()
            self._on_select(str(self._current))
            return
        # Second row is parent when not root
        parent = self._current.parent
        is_parent_row = 1 if parent != self._current else -1
        if idx == 1 and parent != self._current:
            self._current = parent.resolve()
            self._refresh()
            return
        # Child rows
        offset = 2 if parent != self._current else 1
        child_index = idx - offset
        try:
            from agentic_debugger.application.local_project import list_child_directories
            children = list_child_directories(self._current)
            if 0 <= child_index < len(children):
                self._current = children[child_index]
                self._refresh()
        except Exception:
            pass

    def action_parent(self) -> None:
        parent = self._current.parent
        if parent != self._current:
            self._current = parent.resolve()
            self._refresh()

    def action_select(self) -> None:
        # Treat highlighted select as activation
        lst = self.query_one("#browse-list", OptionList)
        idx = lst.highlighted or 0
        if idx == 0:
            self.app.pop_screen()
            self._on_select(str(self._current))
        elif idx == 1 and self._current.parent != self._current:
            self.action_parent()
        else:
            self.on_option_list_option_selected(type("E", (), {"option_index": idx})())

    def action_cancel(self) -> None:
        self.app.pop_screen()


class BugDescriptionEditorScreen(Screen):
    """Dedicated terminal-native multiline editor for Bug Description.

    Visual sibling of the ``Select model`` picker: a focused modal with a
    meaningfully larger editing surface, dark restrained palette, and an
    explicit Save affordance.

    Keyboard contract (multiline):
        Enter      => newline (handled by TextArea, not a save)
        Ctrl+Enter => save and return to the Local Project form
        Esc        => cancel and return without modifying the previous value
        Save click => save and return
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "save", "Save", show=False),
    ]

    def __init__(self, *, current: str, on_save: Any) -> None:
        super().__init__()
        self._current = current or ""
        self._on_save = on_save

    def compose(self) -> ComposeResult:
        with Vertical(id="bug-editor-dialog"):
            yield Static("Bug description", id="bug-editor-title")
            yield TextArea(
                text=self._current,
                id="bug-editor",
                soft_wrap=True,
                show_line_numbers=False,
            )
            with Horizontal(id="bug-editor-actions"):
                yield Button("Save", id="bug-save-button", classes="primary-action")
            yield Static("Ctrl+Enter save    Esc cancel", id="bug-editor-hint")
            yield Static("", id="bug-editor-error")

    def on_mount(self) -> None:
        editor = self.query_one("#bug-editor", TextArea)
        editor.focus()
        # Move cursor to end so appending edits is natural; preserve existing value.
        try:
            editor.cursor_location = (len(editor.text.splitlines()), len(editor.text.splitlines()[-1]) if editor.text else 0)
        except Exception:
            pass

    def action_save(self) -> None:
        editor = self.query_one("#bug-editor", TextArea)
        raw: str = editor.text
        # Keep the task spec size bound (4 KiB) but allow multiline content fully.
        if len(raw.encode("utf-8")) > 4096:
            self.query_one("#bug-editor-error", Static).update(
                "bug description exceeds 4 KiB — shorten it before saving"
            )
            return
        self.app.pop_screen()
        self._on_save(raw)

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_save(None)

    def on_button_pressed(self, event: Any) -> None:
        if getattr(event.button, "id", None) == "bug-save-button":
            self.action_save()
            event.stop()


class ProjectEnvBuilderScreen(Screen):
    """Inline multi-field ProjEnv builder (primary) + DSL advanced toggle.

    One row per declaration: name Input + required toggle + secret toggle.
    Every builder state serializes to the DSL string and passes through the
    existing ``parse_project_env_declarations`` validator — no second
    validation implementation, no spec change, names-only model unchanged
    (no values entered, displayed, or persisted).

    Keyboard contract:
        Tab / Shift+Tab => move between name inputs and toggles
        Enter on a toggle/delete/add/save button => activate it
        (Enter inside a name Input moves focus forward, never saves)
        Ctrl+Enter => save (even when a name Input is focused)
        Esc => cancel (no mutation)
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "save", "Save", show=False),
    ]

    def __init__(
        self,
        *,
        initial_text: str,
        on_save: Callable[[Optional[str]], None],
    ) -> None:
        super().__init__()
        from agentic_debugger.ui.session_config import dsl_to_projenv_entries

        try:
            initial = dsl_to_projenv_entries(initial_text or "")
        except ValueError:
            initial = ()
        # Mutable working copy: list of {uid, name, required, secret}.
        # Uids are stable widget identities so Add/Delete never reuse an
        # id still pending removal (avoids duplicate-id races).
        self._next_uid = 0
        self._rows: list[dict[str, Any]] = []
        for entry in initial:
            self._rows.append(
                {
                    "uid": self._next_uid,
                    "name": entry.name,
                    "required": entry.required,
                    "secret": entry.secret,
                }
            )
            self._next_uid += 1
        self._on_save = on_save
        self._title_text = "Project environment"
        try:
            from agentic_debugger.ui.setup_display import PROJENV_BUILDER_CAPTION

            self._caption_text = PROJENV_BUILDER_CAPTION
        except Exception:
            self._caption_text = (
                "Declare variable NAMES to import — names only, values never stored."
            )

    def compose(self) -> ComposeResult:
        with Vertical(id="projenv-dialog"):
            yield Static(self._title_text, id="projenv-title")
            yield Static(self._caption_text, id="projenv-caption")
            with VerticalScroll(id="projenv-rows"):
                pass
            yield Static("", id="projenv-error")
            yield Static("", id="projenv-count")
            with Horizontal(id="projenv-actions"):
                yield Button("Add row", id="projenv-add", classes="secondary-action")
                yield Button("Edit as text", id="projenv-dsl", classes="secondary-action")
                yield Button("Save", id="projenv-save", classes="primary-action")
                yield Button("Cancel", id="projenv-cancel", classes="secondary-action")
            yield Static(
                "Tab move · Enter toggle · Ctrl+Enter save · Esc cancel",
                id="projenv-hint",
            )

    def on_mount(self) -> None:
        self._rebuild_rows_full()
        self._refresh_status()
        # Focus first name input, else Add.
        try:
            if self._rows:
                self.query_one(f"#projenv-name-{self._rows[0]['uid']}", Input).focus()
            else:
                self.query_one("#projenv-add", Button).focus()
        except Exception:
            pass

    # -- row model ------------------------------------------------------

    def _sync_names_from_inputs(self) -> None:
        """Copy typed Input values into ``self._rows`` (no widget churn)."""
        for row in self._rows:
            try:
                inp = self.query_one(f"#projenv-name-{row['uid']}", Input)
                row["name"] = inp.value
            except Exception:
                pass

    def _current_entries(self) -> list[Any]:
        """Read current UI state (Inputs + toggle flags) as entries."""
        from agentic_debugger.ui.session_config import ProjEnvEntry

        self._sync_names_from_inputs()
        return [
            ProjEnvEntry(
                name=str(row.get("name", "")),
                required=bool(row.get("required", True)),
                secret=bool(row.get("secret", False)),
            )
            for row in self._rows
        ]

    def _mount_one_row(self, row: dict[str, Any], position: int) -> None:
        """Mount a single row widget (incremental; no rebuild)."""
        try:
            container = self.query_one("#projenv-rows", VerticalScroll)
        except Exception:
            return
        # Drop the empty-state hint once the first row arrives.
        try:
            empty = container.query_one("#projenv-empty", Static)
            empty.remove()
        except Exception:
            pass
        uid = row["uid"]
        req_label = "required" if row.get("required", True) else "optional"
        sec_label = "secret" if row.get("secret", False) else "inherit"
        try:
            container.mount(
                Horizontal(
                    Static(f"{position}.", id=f"projenv-num-{uid}"),
                    Input(
                        value=str(row.get("name", "")),
                        placeholder="NAME (e.g. FOO)",
                        id=f"projenv-name-{uid}",
                    ),
                    Button(req_label, id=f"projenv-req-{uid}"),
                    Button(sec_label, id=f"projenv-sec-{uid}"),
                    Button("Del", id=f"projenv-del-{uid}"),
                    id=f"projenv-row-{uid}",
                    classes="projenv-row",
                )
            )
        except Exception:
            pass

    def _renumber_rows(self) -> None:
        """Refresh 1-based position labels after Add/Delete (no rebuild)."""
        for position, row in enumerate(self._rows, start=1):
            try:
                num = self.query_one(f"#projenv-num-{row['uid']}", Static)
                num.update(f"{position}.")
            except Exception:
                pass

    def _rebuild_rows_full(self) -> None:
        """Recreate all row widgets (mount + DSL bulk load only)."""
        try:
            container = self.query_one("#projenv-rows", VerticalScroll)
        except Exception:
            return
        try:
            container.remove_children()
        except Exception:
            try:
                for child in list(container.children):
                    try:
                        child.remove()
                    except Exception:
                        pass
            except Exception:
                return
        if not self._rows:
            try:
                container.mount(
                    Static(
                        "No declarations (optional) — Add row or Edit as text.",
                        id="projenv-empty",
                    )
                )
            except Exception:
                pass
            return
        for position, row in enumerate(self._rows, start=1):
            uid = row["uid"]
            req_label = "required" if row.get("required", True) else "optional"
            sec_label = "secret" if row.get("secret", False) else "inherit"
            try:
                container.mount(
                    Horizontal(
                        Static(f"{position}.", id=f"projenv-num-{uid}"),
                        Input(
                            value=str(row.get("name", "")),
                            placeholder="NAME (e.g. FOO)",
                            id=f"projenv-name-{uid}",
                        ),
                        Button(req_label, id=f"projenv-req-{uid}"),
                        Button(sec_label, id=f"projenv-sec-{uid}"),
                        Button("Del", id=f"projenv-del-{uid}"),
                        id=f"projenv-row-{uid}",
                        classes="projenv-row",
                    )
                )
            except Exception:
                pass

    # Backwards-compat for earlier drafts/tests: full rebuild entry point.
    def _rebuild_rows(self) -> None:
        self._sync_names_from_inputs()
        self._rebuild_rows_full()

    def _refresh_status(self) -> None:
        """Update inline error + count lines from the single validator."""
        try:
            from agentic_debugger.ui.session_config import projenv_entries_error
            from agentic_debugger.ui.setup_display import projenv_builder_count_label
        except Exception:
            return
        try:
            entries = self._current_entries()
        except Exception:
            return
        try:
            error = projenv_entries_error(entries)
        except Exception as exc:
            error = str(exc)
        try:
            count = projenv_builder_count_label(entries)
        except Exception:
            count = ""
        try:
            err_widget = self.query_one("#projenv-error", Static)
            if error:
                err_widget.update(f"[{ERROR}]{_markup_escape(error)}[/]")
            else:
                err_widget.update("")
        except Exception:
            pass
        try:
            self.query_one("#projenv-count", Static).update(
                f"[{FAINT}]{_markup_escape(count)}[/]" if count else ""
            )
        except Exception:
            pass

    # -- events ---------------------------------------------------------

    def on_input_changed(self, event: Any) -> None:
        try:
            control_id = getattr(getattr(event, "control", None), "id", "") or ""
        except Exception:
            control_id = ""
        if control_id.startswith("projenv-name-"):
            self._refresh_status()

    def _row_by_uid(self, uid: int) -> Optional[dict[str, Any]]:
        for row in self._rows:
            if row.get("uid") == uid:
                return row
        return None

    def _position_of_uid(self, uid: int) -> Optional[int]:
        for position, row in enumerate(self._rows):
            if row.get("uid") == uid:
                return position
        return None

    def on_button_pressed(self, event: Any) -> None:
        bid = getattr(event.button, "id", "") or ""
        if bid.startswith("projenv-req-"):
            try:
                uid = int(bid.rsplit("-", 1)[-1])
            except ValueError:
                return
            # Sync names so toggling never drops typed input.
            self._sync_names_from_inputs()
            row = self._row_by_uid(uid)
            if row is None:
                return
            row["required"] = not row.get("required", True)
            try:
                event.button.label = "required" if row["required"] else "optional"
            except Exception:
                pass
            self._refresh_status()
            event.stop()
            return
        if bid.startswith("projenv-sec-"):
            try:
                uid = int(bid.rsplit("-", 1)[-1])
            except ValueError:
                return
            self._sync_names_from_inputs()
            row = self._row_by_uid(uid)
            if row is None:
                return
            row["secret"] = not row.get("secret", False)
            try:
                event.button.label = "secret" if row["secret"] else "inherit"
            except Exception:
                pass
            self._refresh_status()
            event.stop()
            return
        if bid.startswith("projenv-del-"):
            try:
                uid = int(bid.rsplit("-", 1)[-1])
            except ValueError:
                return
            self._sync_names_from_inputs()
            position = self._position_of_uid(uid)
            self._rows = [row for row in self._rows if row.get("uid") != uid]
            # Remove only this row's widget (no full rebuild → no id races).
            try:
                self.query_one(f"#projenv-row-{uid}").remove()
            except Exception:
                pass
            if not self._rows:
                try:
                    container = self.query_one("#projenv-rows", VerticalScroll)
                    container.mount(
                        Static(
                            "No declarations (optional) — Add row or Edit as text.",
                            id="projenv-empty",
                        )
                    )
                except Exception:
                    pass
            else:
                self._renumber_rows()
            self._refresh_status()
            try:
                if self._rows:
                    nxt = min(position if position is not None else 0, len(self._rows) - 1)
                    self.query_one(
                        f"#projenv-name-{self._rows[nxt]['uid']}", Input
                    ).focus()
                else:
                    self.query_one("#projenv-add", Button).focus()
            except Exception:
                pass
            event.stop()
            return
        if bid == "projenv-add":
            self._sync_names_from_inputs()
            row: dict[str, Any] = {
                "uid": self._next_uid,
                "name": "",
                "required": True,
                "secret": False,
            }
            self._next_uid += 1
            self._rows.append(row)
            self._mount_one_row(row, len(self._rows))
            self._refresh_status()
            try:
                self.query_one(f"#projenv-name-{row['uid']}", Input).focus()
            except Exception:
                pass
            event.stop()
            return
        if bid == "projenv-dsl":
            self._open_dsl_editor()
            event.stop()
            return
        if bid == "projenv-save":
            self.action_save()
            event.stop()
            return
        if bid == "projenv-cancel":
            self.action_cancel()
            event.stop()
            return

    def on_key(self, event: Any) -> None:
        # Enter inside a name Input moves focus forward (never saves); toggle
        # buttons keep native Enter-to-activate.  Ctrl+Enter is bound to save.
        try:
            if getattr(event, "key", None) == "enter":
                focused = getattr(self, "focused", None)
                fid = getattr(focused, "id", "") or ""
                if fid.startswith("projenv-name-"):
                    try:
                        uid = int(fid.rsplit("-", 1)[-1])
                        position = self._position_of_uid(uid)
                        if position is not None and position + 1 < len(self._rows):
                            nxt_uid = self._rows[position + 1]["uid"]
                            self.query_one(f"#projenv-name-{nxt_uid}", Input).focus()
                        else:
                            self.query_one("#projenv-add", Button).focus()
                    except Exception:
                        pass
                    event.prevent_default()
                    event.stop()
        except Exception:
            pass

    # -- DSL advanced toggle --------------------------------------------

    def _open_dsl_editor(self) -> None:
        from agentic_debugger.ui.session_config import projenv_entries_to_dsl

        try:
            entries = self._current_entries()
            dsl = projenv_entries_to_dsl(entries)
        except Exception:
            dsl = ""
        self.app.push_screen(
            SingleLineFieldEditorScreen(
                title="Project env names (DSL; e.g. FOO, BAR?, secret:DB_URL — names only)",
                current=dsl,
                on_save=self._on_dsl_saved,
                placeholder="FOO, BAR?, secret:DB_URL",
                caption="Advanced DSL — comma-separated NAMES. Builder ↔ text never loses or reorders.",
            )
        )

    def _on_dsl_saved(self, value: Optional[str]) -> None:
        if value is None:
            # DSL cancelled: stay in the builder unchanged.
            try:
                if self._rows:
                    self.query_one(
                        f"#projenv-name-{self._rows[0]['uid']}", Input
                    ).focus()
            except Exception:
                pass
            return
        from agentic_debugger.ui.session_config import dsl_to_projenv_entries

        try:
            parsed = dsl_to_projenv_entries(value.strip())
        except ValueError:
            parsed = ()
        self._rows = []
        for entry in parsed:
            self._rows.append(
                {
                    "uid": self._next_uid,
                    "name": entry.name,
                    "required": entry.required,
                    "secret": entry.secret,
                }
            )
            self._next_uid += 1
        self._rebuild_rows_full()
        self._refresh_status()
        try:
            if self._rows:
                self.query_one(
                    f"#projenv-name-{self._rows[0]['uid']}", Input
                ).focus()
            else:
                self.query_one("#projenv-add", Button).focus()
        except Exception:
            pass

    # -- save / cancel ---------------------------------------------------

    def action_save(self) -> None:
        from agentic_debugger.ui.session_config import (
            projenv_entries_error,
            projenv_entries_to_dsl,
        )

        entries = self._current_entries()
        error = None
        try:
            error = projenv_entries_error(entries)
        except Exception as exc:
            error = str(exc)
        # Empty names are builder affordance errors: force fill-or-delete
        # instead of persisting a phantom (the DSL validator skips empty
        # tokens, so they could never block readiness honestly).
        if error and "empty variable name" in error:
            self._refresh_status()
            try:
                # Focus the first empty row for immediate repair.
                for row in self._rows:
                    if not str(row.get("name", "")).strip():
                        self.query_one(f"#projenv-name-{row['uid']}", Input).focus()
                        break
            except Exception:
                pass
            return
        # All other states (valid or invalid) serialize to the DSL string so
        # the existing ValueError → Issue readiness path remains the gate.
        try:
            dsl = projenv_entries_to_dsl(entries)
        except Exception:
            dsl = ""
        self.app.pop_screen()
        self._on_save(dsl.strip())

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_save(None)


# Backwards-compat alias: the legacy tiny upper-left editor is removed.
# The reusable SingleLineFieldEditorScreen is the single centered implementation.
_SingleLineEditorScreen = SingleLineFieldEditorScreen
