"""Model browser v2 focused gates (Textual-free pure helpers).

Proves the quiet information hierarchy: every row is model name +
provider only (no protocol badge, id, readiness counts, route status,
or routing detail); there is no details pane.  Unavailable rows stay
non-selectable (disabled/muted) with no explanatory routing text.
Covers search, provider filtering, selection gating, quiet row
rendering, empty details, current marking in the list, and responsive
widths without horizontal clipping.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.ui.model_browser import (  # noqa: E402
    details_for_option,
    filter_model_options,
    is_selectable,
    model_choice_key,
    protocol_badge,
    render_model_row,
    summarize_providers,
)
from agentic_debugger.ui.session_config import ModelOption  # noqa: E402


def _options():
    return [
        ModelOption("oc", "gpt-5.6-luna", "GPT-5.6 Luna", detail="direct API · responses",
                    available=True, unavailable_reason=None,
                    protocol="responses", provider_label="OpenCode Go"),
        ModelOption("oc", "deepseek-v4-flash", "DeepSeek V4 Flash", detail="direct API · chat_completions",
                    available=True, unavailable_reason=None,
                    protocol="chat_completions", provider_label="OpenCode Go"),
        ModelOption("oc", "qwen3.8-max", "Qwen 3.8 Max", detail="direct API · messages",
                    available=False, unavailable_reason="no direct API credential — connect in Model Providers (press m)",
                    protocol="messages", provider_label="OpenCode Go"),
        ModelOption("oc", "future-xyz", "Future Xyz", detail="",
                    available=False, unavailable_reason="Protocol not yet resolved for direct API",
                    protocol=None, provider_label="OpenCode Go"),
        ModelOption("gen", "generic-model", "Generic Model", detail="",
                    available=True, unavailable_reason=None,
                    protocol="chat_completions", provider_label="Generic Gateway"),
        ModelOption("offline", "", "Offline", detail="", available=True,
                    unavailable_reason=None, protocol=None, provider_label="Offline"),
    ]


class TestSearch:
    def test_name_substring(self):
        assert [o.model_id for o in filter_model_options(_options(), "deepseek")] == ["deepseek-v4-flash"]

    def test_id_substring_case_insensitive(self):
        assert [o.model_id for o in filter_model_options(_options(), "GPT-5.6")] == ["gpt-5.6-luna"]

    def test_provider_substring(self):
        assert {o.model_id for o in filter_model_options(_options(), "generic")} == {"generic-model"}

    def test_protocol_is_not_a_search_key(self):
        # Protocol/routing is an implementation detail, never selection
        # metadata: searching a protocol family matches nothing.
        assert filter_model_options(_options(), "messages") == []
        assert filter_model_options(_options(), "responses") == []

    def test_empty_search_returns_all(self):
        assert len(filter_model_options(_options(), "")) == 6


class TestProviderFiltering:
    def test_all(self):
        assert len(filter_model_options(_options(), "", "all")) == 6

    def test_single_provider(self):
        only = filter_model_options(_options(), "", "gen")
        assert [o.model_id for o in only] == ["generic-model"]

    def test_combined_search_and_provider(self):
        assert filter_model_options(_options(), "qwen", "gen") == []
        assert [o.model_id for o in filter_model_options(_options(), "qwen", "oc")] == ["qwen3.8-max"]


class TestSelection:
    def test_ready_is_selectable_unready_is_not(self):
        opts = {o.model_id: o for o in _options()}
        assert is_selectable(opts["gpt-5.6-luna"]) is True
        assert is_selectable(opts["qwen3.8-max"]) is False
        assert is_selectable(opts["future-xyz"]) is False

    def test_choice_key_round_trip(self):
        assert model_choice_key("oc", "gpt-5.6-luna") == "oc:gpt-5.6-luna"


class TestProtocolReadinessRendering:
    def test_badges(self):
        # Routing truth is preserved (transport authority), even though
        # normal rows no longer display it.
        assert protocol_badge("responses") == "Responses"
        assert protocol_badge("chat_completions") == "Chat Completions"
        assert protocol_badge("messages") == "Messages"
        assert protocol_badge(None) == "Unresolved"

    def test_ready_details_are_quiet(self):
        text = details_for_option(_options()[0], is_current=True)
        assert text == ""
        for banned in (
            "GPT-5.6 Luna",
            "gpt-5.6-luna",
            "Responses",
            "responses",
            "chat_completions",
            "messages",
            "Route needed",
            "Route unresolved",
            "Ready",
            "Current selection",
            "Current:",
            "Status:",
            "direct API",
            "not qualified",
            "Level-32",
            "OpenCode Go",
        ):
            assert banned not in text

    def test_disabled_reason_produces_no_details(self):
        # The picker is a selector, not an inspection screen: even an
        # exceptional selection produces no details prose.  Routing truth
        # stays in is_selectable/transport, never in picker copy.
        for option in (_options()[2], _options()[3]):
            text = details_for_option(option)
            assert text == ""
            for banned in (
                "Route needed",
                "Route unresolved",
                "direct API",
                "responses",
                "chat_completions",
                "messages",
                "not qualified",
                "Level-32",
                "Status:",
                "no direct API credential",
                "Protocol not yet resolved",
            ):
                assert banned not in text
            assert "\n" not in text

    def test_unresolved_option_produces_no_details(self):
        text = details_for_option(_options()[3])
        assert text == ""
        for banned in (
            "Route needed",
            "Route unresolved",
            "Unresolved",
            "direct API",
            "Status:",
        ):
            assert banned not in text
        assert "\n" not in text


class TestResponsiveRows:
    def test_no_clipping_at_constrained_and_normal_sizes(self):
        for width in (30, 40, 50, 60, 80, 100, 120):
            for option in _options():
                row = render_model_row(option, width)
                plain = row.plain if hasattr(row, "plain") else str(row)
                assert len(plain) <= width, (width, plain)
                assert "\n" not in plain

    def test_constrained_row_has_no_protocol_badge(self):
        row = render_model_row(_options()[0], 40)
        assert "GPT-5.6 Luna" in row.plain
        for banned in ("Responses", "responses", "Resp", "Chat", "chat_completions",
                        "Messages", "messages", "Msg", "Route needed", "Route unresolved",
                        "●", "○", "[", "]"):
            assert banned not in row.plain

    def test_normal_row_shows_name_and_provider_only(self):
        row = render_model_row(_options()[0], 100, is_current=True)
        assert "GPT-5.6 Luna" in row.plain
        assert "OpenCode Go" in row.plain
        assert "✓" in row.plain
        for banned in ("Responses", "responses", "Chat Completions", "chat_completions",
                        "Messages", "messages", "Unresolved",
                        "Route needed", "Route unresolved", "Unavailable",
                        "gpt-5.6-luna", "●", "○", "Ready", "Status:", "direct API",
                        "not qualified", "Level-32"):
            assert banned not in row.plain

    def test_normal_row_without_current_has_no_check(self):
        row = render_model_row(_options()[0], 100)
        assert "✓" not in row.plain
        assert "GPT-5.6 Luna" in row.plain
        assert "OpenCode Go" in row.plain

    def test_exceptional_row_shows_name_and_provider_only(self):
        # Unresolved rows stay disabled/muted with no routing text.
        option = _options()[3]
        assert is_selectable(option) is False
        row = render_model_row(option, 100)
        assert "Future Xyz" in row.plain
        assert "OpenCode Go" in row.plain
        assert "!" not in row.plain
        for banned in ("Route needed", "Route unresolved", "Unresolved",
                        "Responses", "responses", "chat_completions", "messages",
                        "direct API", "Status:", "future-xyz", "●", "○"):
            assert banned not in row.plain

    def test_unavailable_row_with_known_protocol_shows_no_status(self):
        # Known-protocol but unavailable rows are also status-free.
        option = _options()[2]
        assert is_selectable(option) is False
        row = render_model_row(option, 100)
        assert "Qwen 3.8 Max" in row.plain
        assert "OpenCode Go" in row.plain
        assert "!" not in row.plain
        for banned in ("Unavailable", "Route needed", "Route unresolved",
                        "Messages", "messages", "chat_completions",
                        "direct API", "Status:", "qwen3.8-max", "●", "○"):
            assert banned not in row.plain

    def test_normal_row_and_details_hide_id(self):
        # Model ids are implementation details: neither the quiet row nor
        # the quiet details repeat them for a normal selection.
        row = render_model_row(_options()[0], 100)
        assert "gpt-5.6-luna" not in row.plain
        details = details_for_option(_options()[0])
        assert details == ""
        assert "gpt-5.6-luna" not in details

    def test_offline_row_dedupes_provider(self):
        row = render_model_row(_options()[5], 100)
        assert row.plain.strip() == "Offline"

    def test_offline_unavailable_row_has_no_routing_text(self):
        # Presentation-only: an unavailable Offline entry stays
        # non-selectable but renders as plain "Offline".
        from agentic_debugger.ui.session_config import ModelOption as _ModelOption

        option = _ModelOption(
            "offline", "", "Offline",
            detail="Local Project requires a live model",
            available=False,
            unavailable_reason="Local Project requires a live model",
            protocol=None, provider_label="Offline",
        )
        assert is_selectable(option) is False
        row = render_model_row(option, 100)
        assert row.plain.strip() == "Offline"
        for banned in ("Route needed", "Route unresolved", "Unresolved",
                        "!", "●", "○", "direct API", "Status:"):
            assert banned not in row.plain

    def test_mid_width_row_shows_provider_without_protocol(self):
        row = render_model_row(_options()[0], 70)
        assert "OpenCode Go" in row.plain
        for banned in ("Responses", "responses", "Resp", "Chat", "chat_completions",
                        "Messages", "messages", "Route needed", "direct API"):
            assert banned not in row.plain

    def test_ready_details_empty_single_line(self):
        text = details_for_option(_options()[0], is_current=True)
        assert text == ""
        assert "Note:" not in text
        assert "Why unavailable:" not in text
        for banned in ("Route needed", "responses", "chat_completions",
                        "messages", "direct API", "Status:"):
            assert banned not in text

    def test_details_never_shows_notes(self):
        # Even genuine non-routing notes (e.g. Level-32 qualification)
        # never appear: the picker has no details pane.
        from agentic_debugger.ui.session_config import ModelOption as _ModelOption

        option = _ModelOption(
            "oc", "m", "M",
            detail="not qualified for frozen Level-32 comparison",
            available=True, unavailable_reason=None,
            protocol="responses", provider_label="OpenCode Go",
        )
        text = details_for_option(option)
        assert text == ""
        for banned in ("not qualified", "Level-32", "Note:", "Status:"):
            assert banned not in text
        assert "\n" not in text

    def test_provider_summary_counts(self):
        providers = {p.provider_id: p for p in summarize_providers(_options())}
        assert providers["oc"].total == 4
        assert providers["oc"].ready == 2
        assert providers["gen"].ready == 1
