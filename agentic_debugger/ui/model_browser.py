"""Model browser/selector v2: searchable, provider-aware, readiness-explicit.

Pure helpers here are Textual-free and unit-testable (filtering,
protocol badges, responsive row rendering, details text).  The
:class:`ModelBrowserScreen` composes them into a responsive Textual
surface with search, provider filtering, protocol/readiness badges,
disabled reasons, current-selection marking, and keyboard-first Enter
selection — without raw giant unstructured lists or horizontal
clipping at constrained sizes.
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
    """Search + provider filtering (pure, case-insensitive substring)."""
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
                protocol_badge(_option_protocol(option)).lower(),
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


def details_for_option(option: Any, *, is_current: bool = False) -> str:
    """Multiline details for the footer pane (wraps, never clips)."""
    if option is None:
        return "No model selected."
    display = getattr(option, "display", "") or getattr(option, "model_id", "")
    provider_label = _option_provider_label(option)
    model_id = getattr(option, "model_id", "")
    badge = protocol_badge(_option_protocol(option))
    available = bool(getattr(option, "available", False))
    reason = getattr(option, "unavailable_reason", None)
    detail = getattr(option, "detail", "") or ""
    lines = [
        f"{display}",
        f"Provider: {provider_label}  ·  Model ID: {model_id}",
        f"Protocol: {badge}  ·  Status: {'Ready' if available else 'Unavailable'}"
        + ("  ·  Current selection" if is_current else ""),
    ]
    if type(detail) is str and detail.strip():
        detail_text = detail.strip()
        # The detail carries the route/qualification note (e.g. Level-32
        # non-qualified); surface it once when it adds information beyond
        # the protocol badge line above.
        if detail_text.lower() not in (badge.lower(), f"direct api · {badge}".lower()):
            lines.append(f"Note: {detail_text}")
    if not available and type(reason) is str and reason.strip():
        lines.append(f"Why unavailable: {reason.strip()}")
    if _option_protocol(option) is None:
        lines.append(
            "Tip: set an explicit protocol override when adding this model "
            "manually (Responses / Chat Completions / Messages)."
        )
    return "\n".join(lines)


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
    """One responsive list row as Rich Text (never exceeds width)."""
    from rich.text import Text as _Text

    display = str(getattr(option, "display", "") or getattr(option, "model_id", ""))
    model_id = str(getattr(option, "model_id", ""))
    badge = protocol_badge(_option_protocol(option))
    available = bool(getattr(option, "available", False))
    dot = "●" if available else "○"
    current_mark = " ✓" if is_current else ""
    provider_label = _option_provider_label(option)

    # Width budgets: narrow (<60) shows name + badge only; compact
    # (<90) adds provider; normal shows name + id + badge + provider.
    # Everything is truncated to fit so constrained terminals never clip.
    safe_width = max(20, int(width or 80))
    if safe_width < 60:
        badge_short = {"Chat Completions": "Chat", "Responses": "Resp", "Messages": "Msg"}.get(badge, "—")
        core = f"{dot} {display} [{badge_short}]{current_mark}"
        text = _Text(_truncate(core, safe_width))
    elif safe_width < 90:
        core = f"{dot} {display} [{badge}]{current_mark}"
        suffix = f" {provider_label}"
        room = safe_width - len(suffix)
        if room < 20:
            text = _Text(_truncate(core, safe_width))
        else:
            text = _Text(_truncate(core, room) + suffix)
            if len((core + suffix)) > safe_width:
                text = _Text(_truncate(core + suffix, safe_width))
    else:
        core = f"{dot} {display} ({model_id}) [{badge}] {provider_label}{current_mark}"
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
        """Searchable model browser with provider context and readiness."""

        BINDINGS = [
            Binding("escape", "cancel", "Cancel"),
            Binding("/", "focus_search", "Search"),
            Binding("[", "prev_provider", "Previous provider", show=False),
            Binding("]", "next_provider", "Next provider", show=False),
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
                yield Static("", id="model-browser-current")
                yield Input(
                    placeholder="Search models… (name, id, provider, protocol)",
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
                yield Static("", id="model-browser-details")
                yield Static(
                    "up/down navigate · enter select · [ ] provider · / search · esc cancel",
                    id="model-browser-hint",
                )

        def _provider_cycle(self) -> List[str]:
            """Provider filter order: all, then providers alphabetically."""
            return ["all"] + [p.provider_id for p in self._providers]

        def _filter_button_label(self) -> str:
            """Visible provider-filter value with ready counts (always fits)."""
            if self._provider_filter == "all":
                total = len(self._all_options)
                ready = sum(1 for o in self._all_options if bool(getattr(o, "available", False)))
                return f"Provider: All ({ready}/{total})"
            for provider in self._providers:
                if provider.provider_id == self._provider_filter:
                    return f"Provider: {provider.label} ({provider.ready}/{provider.total})"
            return f"Provider: {self._provider_filter}"

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
                self._populate_list()
            except Exception:
                pass
            self._update_responsive()

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

        def _refresh_header(self) -> None:
            try:
                current = self.query_one("#model-browser-current", Static)
            except Exception:
                return
            total = len(self._all_options)
            ready = sum(1 for o in self._all_options if bool(getattr(o, "available", False)))
            current_opt = None
            if self._current_key:
                for opt in self._all_options:
                    if model_choice_key(getattr(opt, "provider", ""), getattr(opt, "model_id", "")) == self._current_key:
                        current_opt = opt
                        break
            if current_opt is not None:
                current.update(
                    f"Current: {getattr(current_opt, 'display', '')} "
                    f"({getattr(current_opt, 'model_id', '')}) · {ready}/{total} ready"
                )
            else:
                current.update(f"{ready}/{total} ready · type to filter, Enter to select")
            try:
                count = self.query_one("#model-browser-count", Static)
                count.update(f"{len(self._filtered)} shown")
            except Exception:
                pass

        def _populate_list(self) -> None:
            from textual.widgets.option_list import Option

            try:
                opt_list = self.query_one("#model-browser-list", OptionList)
            except Exception:
                return
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
            # Highlight the current selection when visible, else first ready.
            target = None
            for index, option in enumerate(self._filtered):
                key = model_choice_key(getattr(option, "provider", ""), getattr(option, "model_id", ""))
                if key == self._current_key and is_selectable(option):
                    target = index
                    break
            if target is None:
                for index, option in enumerate(self._filtered):
                    if is_selectable(option):
                        target = index
                        break
            if target is not None:
                try:
                    opt_list.highlighted = target
                except Exception:
                    pass
            self._refresh_header()
            self._refresh_details()

        def _refresh_details(self) -> None:
            try:
                details = self.query_one("#model-browser-details", Static)
                opt_list = self.query_one("#model-browser-list", OptionList)
            except Exception:
                return
            highlighted = opt_list.highlighted
            if highlighted is None or highlighted >= len(self._filtered):
                if highlighted == len(self._filtered):
                    details.update("Manage providers, refresh catalogs, and set API keys.")
                else:
                    details.update("")
                return
            option = self._filtered[highlighted]
            key = model_choice_key(getattr(option, "provider", ""), getattr(option, "model_id", ""))
            details.update(details_for_option(option, is_current=(key == self._current_key)))

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

        def on_input_changed(self, event: Input.Changed) -> None:
            if getattr(event.input, "id", "") == "model-browser-search":
                self._search_text = event.value or ""
                self._populate_list()
                event.stop()

        def on_option_list_option_highlighted(self, event: Any) -> None:
            self._refresh_details()

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
