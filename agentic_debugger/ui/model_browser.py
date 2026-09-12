"""Model browser/selector v2: searchable, provider-aware, quiet by default.

Pure helpers here are Textual-free and unit-testable (filtering and
responsive row rendering).  The :class:`ModelBrowserScreen` composes
them into a responsive Textual surface with search, provider
filtering, disabled (muted) rows, current-selection marking, and
keyboard-first Enter selection — without raw giant unstructured
lists or horizontal clipping at constrained sizes.

Information hierarchy: the picker is a selector, not an
inspection/debugging screen.  Every row carries exactly the scannable
identity (model name + quiet provider text, plus the current-selection
check).  Protocol, model id, routing/route state, readiness counts,
and qualification notes are implementation details and never appear
in the picker.  There is no persistent details pane; unavailable rows
stay disabled/muted with no explanatory routing text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple

try:
    from rich.text import Text
except Exception:  # pragma: no cover - textual always ships rich
    Text = None  # type: ignore[assignment]


PROTOCOL_BADGES = {
    "chat_completions": "Chat Completions",
    "responses": "Responses",
    "messages": "Messages",
}

PROTOCOL_SHORT = {
    "chat_completions": "Chat",
    "responses": "Resp",
    "messages": "Msg",
}


def protocol_badge(protocol: Optional[str]) -> str:
    """Short human label for one protocol family (never empty)."""
    if type(protocol) is str and protocol in PROTOCOL_BADGES:
        return PROTOCOL_BADGES[protocol]
    return "Unresolved"


def _option_protocol(option: Any) -> Optional[str]:
    proto = getattr(option, "protocol", None)
    if type(proto) is str and proto in PROTOCOL_BADGES:
        return proto
    # Fall back to parsing the legacy detail note ("direct API · x").
    detail = getattr(option, "detail", "") or ""
    if type(detail) is str:
        lowered = detail.lower()
        for key in PROTOCOL_BADGES:
            if key in lowered:
                return key
    return None


def _option_provider_label(option: Any) -> str:
    label = getattr(option, "provider_label", None)
    if type(label) is str and label.strip():
        return label.strip()
    provider = getattr(option, "provider", "") or ""
    return str(provider)


def filter_model_options(
    options: Sequence[Any],
    search: str = "",
    provider: str = "all",
) -> List[Any]:
    """Search + provider filtering (pure, case-insensitive substring).

    Search matches model name, model id, and provider label only.
    Protocol/routing families are implementation details and are never
    search keys.
    """
    query = search.strip().lower() if type(search) is str else ""
    provider_key = provider.strip() if type(provider) is str else "all"
    result: List[Any] = []
    for option in options or ():
        if provider_key not in ("", "all"):
            if getattr(option, "provider", "") != provider_key:
                continue
        if query:
            haystacks = [
                str(getattr(option, "display", "") or "").lower(),
                str(getattr(option, "model_id", "") or "").lower(),
                _option_provider_label(option).lower(),
            ]
            if not any(query in hay for hay in haystacks):
                continue
        result.append(option)
    return result


def is_selectable(option: Any) -> bool:
    """Whether one browser entry may be chosen (available, non-error)."""
    if option is None:
        return False
    provider = getattr(option, "provider", "")
    model_id = getattr(option, "model_id", "")
    if provider == "__provider_registry_error__":
        return False
    if type(model_id) is str and model_id.startswith("unavailable:"):
        return False
    return bool(getattr(option, "available", False))


def model_choice_key(provider: str, model_id: str) -> str:
    return f"{provider}:{model_id}"


def _is_routing_only_detail(text: str) -> bool:
    """Whether a catalog note is pure routing noise (never shown)."""
    if not text or not text.strip():
        return True
    low = text.lower()
    tmp = (
        low.replace("direct api", " ")
        .replace("·", " ")
        .replace("•", " ")
        .replace(":", " ")
    )
    for token in (
        "chat_completions",
        "responses",
        "messages",
        "chat",
        "resp",
        "msg",
        "unresolved",
    ):
        tmp = tmp.replace(token, " ")
    tmp = tmp.strip(" .·-—–_,;:!?()[]")
    return not tmp.strip()


def _row_status(option: Any) -> str:
    """Row status (always empty: the picker shows no per-row status).

    Unavailable rows stay disabled/muted via :func:`is_selectable`
    and dim row styling; no routing or explanatory text is rendered.
    """
    return ""


def details_for_option(option: Any, *, is_current: bool = False) -> str:
    """Details text (always empty: the picker has no details pane).

    The picker is a selector, not an inspection/debugging screen, so
    neither ready nor exceptional selections produce details prose —
    no model id, protocol, route state, status, or qualification note.
    Routing truth stays in the provider/runtime layer
    (:func:`is_selectable` and transport resolution), never in picker
    copy.
    """
    return ""


def _truncate(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: max(0, width - 1)] + "…"


def render_model_row(
    option: Any, width: int, *, is_current: bool = False
) -> Any:
    """One quiet list row as Rich Text (never exceeds width).

    Every row carries exactly the scannable identity: model name plus
    quiet secondary provider text (``Display  Provider``) and a small
    current-selection check.  No protocol badge, model id, readiness
    dot, route status, or other per-row chrome — unavailable rows are
    distinguished only by disabled/muted styling via
    :func:`is_selectable`.
    """
    from rich.text import Text as _Text

    display = str(getattr(option, "display", "") or getattr(option, "model_id", ""))
    available = bool(getattr(option, "available", False))
    current_mark = " ✓" if is_current else ""
    provider_label = _option_provider_label(option)
    show_provider = bool(provider_label.strip()) and provider_label.strip() != display.strip()

    # Single layout at every width: name, then provider, then the
    # current-selection check.  Everything is truncated to fit so
    # constrained terminals never clip.
    safe_width = max(20, int(width or 80))
    if show_provider:
        core = f"{display}  {provider_label}"
    else:
        core = f"{display}"
    core = f"{core}{current_mark}"
    text = _Text(_truncate(core, safe_width))
    if not available:
        text.stylize("dim")
    elif is_current:
        text.stylize("bold")
    return text


@dataclass(frozen=True)
class BrowserProvider:
    provider_id: str
    label: str
    total: int = 0
    ready: int = 0


def summarize_providers(options: Sequence[Any]) -> List[BrowserProvider]:
    """Provider filter entries derived from the current option list."""
    by_id: dict = {}
    for option in options or ():
        pid = getattr(option, "provider", "") or ""
        if not pid or pid == "__provider_registry_error__":
            continue
        label = _option_provider_label(option)
        entry = by_id.get(pid)
        if entry is None:
            by_id[pid] = {"label": label, "total": 0, "ready": 0}
            entry = by_id[pid]
        entry["total"] += 1
        if bool(getattr(option, "available", False)):
            entry["ready"] += 1
    providers = [
        BrowserProvider(provider_id=pid, label=info["label"], total=info["total"], ready=info["ready"])
        for pid, info in sorted(by_id.items(), key=lambda kv: kv[1]["label"].lower())
    ]
    return providers


# -- Textual screen -----------------------------------------------------------

try:
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.screen import Screen
    from textual.widgets import Button, Input, OptionList, Static

    _TEXTUAL_AVAILABLE = True
except Exception:  # pragma: no cover - helpers remain testable
    _TEXTUAL_AVAILABLE = False


if _TEXTUAL_AVAILABLE:

    class ModelBrowserScreen(Screen):  # type: ignore[no-redef]
        """Quiet searchable model browser (name + provider only, no details pane)."""

        BINDINGS = [
            Binding("escape", "cancel", "Cancel"),
            Binding("/", "focus_search", "Search"),
            Binding("[", "prev_provider", "Previous provider", show=False),
            Binding("]", "next_provider", "Next provider", show=False),
            Binding("pageup", "page_up", "Page up", show=False),
            Binding("pagedown", "page_down", "Page down", show=False),
        ]

        def __init__(
            self,
            options: Sequence[Any],
            *,
            current_key: Optional[str] = None,
            on_select: Callable[[str], None],
            title: str = "Select model",
        ) -> None:
            super().__init__()
            self._all_options = list(options or [])
            self._current_key = current_key
            self._on_select = on_select
            self._title_text = title
            self._search_text = ""
            self._provider_filter = "all"
            self._filtered: List[Any] = list(self._all_options)
            self._providers = summarize_providers(self._all_options)

        def compose(self) -> ComposeResult:
            with Vertical(id="model-browser-dialog"):
                yield Static(self._title_text, id="model-browser-title")
                yield Input(
                    placeholder="Search models...",
                    id="model-browser-search",
                )
                with Horizontal(id="model-browser-filters"):
                    yield Button(
                        self._filter_button_label(),
                        id="model-browser-provider-button",
                        classes="provider-pill",
                    )
                    yield Static("", id="model-browser-count")
                yield OptionList(id="model-browser-list")
                yield Static(
                    "Enter select   Esc cancel",
                    id="model-browser-hint",
                )

        def _provider_cycle(self) -> List[str]:
            """Provider filter order: all, then providers alphabetically."""
            return ["all"] + [p.provider_id for p in self._providers]

        def _filter_button_label(self) -> str:
            """Visible provider-filter value (quiet, no counts)."""
            if self._provider_filter == "all":
                return "All providers"
            for provider in self._providers:
                if provider.provider_id == self._provider_filter:
                    return provider.label
            return str(self._provider_filter)

        def _refresh_filter_button(self) -> None:
            try:
                button = self.query_one("#model-browser-provider-button", Button)
            except Exception:
                return
            try:
                button.label = self._filter_button_label()
            except Exception:
                pass

        def _set_provider_filter(self, provider_id: str) -> None:
            self._provider_filter = provider_id or "all"
            self._refresh_filter_button()
            self._populate_list()

        def _cycle_provider(self, direction: int) -> None:
            cycle = self._provider_cycle()
            try:
                index = cycle.index(self._provider_filter)
            except ValueError:
                index = 0
            self._set_provider_filter(cycle[(index + direction) % len(cycle)])

        def on_mount(self) -> None:
            self._refresh_header()
            self._populate_list()
            self._update_responsive()
            try:
                self.query_one("#model-browser-list", OptionList).focus()
            except Exception:
                pass

        def on_resize(self, event: Any) -> None:
            try:
                # Re-render rows for the new width but keep the user's
                # highlight (and its scroll position) instead of snapping
                # back to the current selection.
                self._populate_list(preserve_highlight=True)
            except Exception:
                pass
            self._update_responsive()
            try:
                from agentic_debugger.ui.screens_shared import (
                    ensure_option_list_highlight_visible,
                )

                ensure_option_list_highlight_visible(
                    self.query_one("#model-browser-list", OptionList)
                )
            except Exception:
                pass

        def _update_responsive(self) -> None:
            try:
                card = self.query_one("#model-browser-dialog")
                width = self.size.width or 0
                if width and width < 85:
                    card.add_class("narrow")
                else:
                    card.remove_class("narrow")
            except Exception:
                pass

        def _visible_width(self) -> int:
            try:
                return max(20, int(self.size.width or 80) - 8)
            except Exception:
                return 72

        def _is_filtered(self) -> bool:
            if self._provider_filter not in ("", "all"):
                return True
            return bool((self._search_text or "").strip())

        def _refresh_header(self) -> None:
            """Update only the secondary filtered-result count.

            The header stays quiet: title plus list marking carry the
            selection.  A count appears only when search/filtering makes
            it useful, and remains visually secondary.
            """
            try:
                count = self.query_one("#model-browser-count", Static)
            except Exception:
                return
            try:
                if not self._is_filtered():
                    count.update("")
                else:
                    count.update(f"{len(self._filtered)} of {len(self._all_options)}")
            except Exception:
                pass

        def _populate_list(self, *, preserve_highlight: bool = False) -> None:
            from textual.widgets.option_list import Option

            try:
                opt_list = self.query_one("#model-browser-list", OptionList)
            except Exception:
                return
            # When only re-rendering (resize), remember the visible
            # selection by key so the rebuild below restores it instead
            # of snapping back to the stored current selection.
            keep_key: Optional[str] = None
            keep_manage = False
            if preserve_highlight:
                try:
                    current_hl = opt_list.highlighted
                    if current_hl is not None and 0 <= current_hl < len(self._filtered):
                        previous = self._filtered[current_hl]
                        keep_key = model_choice_key(
                            getattr(previous, "provider", ""),
                            getattr(previous, "model_id", ""),
                        )
                    elif current_hl == len(self._filtered):
                        keep_manage = True
                except Exception:
                    keep_key = None
                    keep_manage = False
            self._filtered = filter_model_options(
                self._all_options, self._search_text, self._provider_filter
            )
            # Manage-providers entry stays visible (not filtered away) so
            # the provider surface is always one Enter away.
            opt_list.clear_options()
            width = self._visible_width()
            if not self._filtered:
                opt_list.add_option(Option("No models match the current filter.", disabled=True))
            for option in self._filtered:
                key = model_choice_key(getattr(option, "provider", ""), getattr(option, "model_id", ""))
                row = render_model_row(option, width, is_current=(key == self._current_key))
                opt_list.add_option(
                    Option(row, disabled=not is_selectable(option), id=f"model::{key}")
                )
            opt_list.add_option(Option("Manage model providers…", id="model::providers:manage"))
            # Highlight the preserved row when still present, else the
            # current selection when visible, else first ready.
            target = None
            if keep_manage:
                target = len(self._filtered)
            else:
                wanted_keys = []
                if keep_key is not None:
                    wanted_keys.append(keep_key)
                if self._current_key is not None and self._current_key not in wanted_keys:
                    wanted_keys.append(self._current_key)
                for wanted in wanted_keys:
                    for index, option in enumerate(self._filtered):
                        key = model_choice_key(
                            getattr(option, "provider", ""),
                            getattr(option, "model_id", ""),
                        )
                        if key == wanted and is_selectable(option):
                            target = index
                            break
                    if target is not None:
                        break
            if target is None:
                for index, option in enumerate(self._filtered):
                    if is_selectable(option):
                        target = index
                        break
            from agentic_debugger.ui.screens_shared import (
                ensure_option_list_highlight_visible,
                set_option_list_highlight,
            )

            set_option_list_highlight(opt_list, target)
            self._refresh_header()
            # No details pane: the list keeps the recovered space, so the
            # shared post-layout reveal is the only follow-up needed.
            ensure_option_list_highlight_visible(opt_list)

        def on_button_pressed(self, event: Any) -> None:
            btn_id = getattr(event.button, "id", "") or ""
            if btn_id == "model-browser-provider-button":
                self._cycle_provider(+1)
                try:
                    self.query_one("#model-browser-list", OptionList).focus()
                except Exception:
                    pass
                event.stop()

        def action_prev_provider(self) -> None:
            self._cycle_provider(-1)

        def action_next_provider(self) -> None:
            self._cycle_provider(+1)

        def _page_model_list(self, direction: int) -> None:
            """Page the model list even when focus sits in the search box.

            The list handles page keys natively while focused; this
            forwarding covers the search/button focus case so page
            scrolling always agrees with the scrollbar.
            """
            try:
                opt_list = self.query_one("#model-browser-list", OptionList)
            except Exception:
                return
            try:
                if self.focused is opt_list:
                    return  # native OptionList binding already paged
            except Exception:
                pass
            try:
                if direction < 0:
                    opt_list.action_page_up()
                else:
                    opt_list.action_page_down()
            except Exception:
                pass

        def action_page_up(self) -> None:
            self._page_model_list(-1)

        def action_page_down(self) -> None:
            self._page_model_list(+1)

        def on_input_changed(self, event: Input.Changed) -> None:
            if getattr(event.input, "id", "") == "model-browser-search":
                self._search_text = event.value or ""
                self._populate_list()
                event.stop()

        def on_option_list_option_highlighted(self, event: Any) -> None:
            # Native End/Home/arrows/page keys assign highlighted via the
            # OptionList watcher, bypassing _populate_list.  Re-assert the
            # same shared highlight-visible invariant.
            try:
                from agentic_debugger.ui.screens_shared import (
                    ensure_option_list_highlight_visible,
                )

                ensure_option_list_highlight_visible(
                    self.query_one("#model-browser-list", OptionList)
                )
            except Exception:
                pass

        def on_option_list_option_selected(self, event: Any) -> None:
            index = getattr(event, "option_index", None)
            try:
                opt_list = self.query_one("#model-browser-list", OptionList)
                if index is None:
                    index = opt_list.highlighted
            except Exception:
                return
            if index is None:
                return
            if index == len(self._filtered):
                self.app.pop_screen()
                self._on_select("providers:manage")
                return
            if not 0 <= index < len(self._filtered):
                return
            option = self._filtered[index]
            if not is_selectable(option):
                return
            key = model_choice_key(getattr(option, "provider", ""), getattr(option, "model_id", ""))
            self.app.pop_screen()
            self._on_select(key)

        def action_cancel(self) -> None:
            self.app.pop_screen()

        def action_focus_search(self) -> None:
            try:
                self.query_one("#model-browser-search", Input).focus()
            except Exception:
                pass

else:  # pragma: no cover

    class ModelBrowserScreen:  # type: ignore[no-redef]
        pass
