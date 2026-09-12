"""Model browser v2 focused gates (Textual-free pure helpers).

Proves the selection UX exposes runtime readiness instead of a raw list:
search, provider filtering, selection gating, protocol/readiness
rendering, disabled reasons, current marking, and responsive row
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

    def test_protocol_substring(self):
        assert {o.model_id for o in filter_model_options(_options(), "messages")} == {"qwen3.8-max"}

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
        assert protocol_badge("responses") == "Responses"
        assert protocol_badge("chat_completions") == "Chat Completions"
        assert protocol_badge("messages") == "Messages"
        assert protocol_badge(None) == "Unresolved"

    def test_ready_details(self):
        text = details_for_option(_options()[0], is_current=True)
        assert "GPT-5.6 Luna" in text
        assert "gpt-5.6-luna" in text
        assert "Responses" in text
        assert "Ready" in text
        assert "Current selection" in text

    def test_disabled_reason_shown(self):
        text = details_for_option(_options()[2])
        assert "Unavailable" in text
        assert "no direct API credential" in text

    def test_unresolved_tip_shown(self):
        text = details_for_option(_options()[3])
        assert "Unresolved" in text
        assert "Protocol not yet resolved" in text
        assert "explicit protocol override" in text


class TestResponsiveRows:
    def test_no_clipping_at_constrained_and_normal_sizes(self):
        for width in (30, 40, 50, 60, 80, 100, 120):
            for option in _options():
                row = render_model_row(option, width)
                plain = row.plain if hasattr(row, "plain") else str(row)
                assert len(plain) <= width, (width, plain)
                assert "\n" not in plain

    def test_constrained_row_keeps_badge(self):
        row = render_model_row(_options()[0], 40)
        assert "Resp" in row.plain or "Responses" in row.plain

    def test_normal_row_shows_identity(self):
        row = render_model_row(_options()[0], 100, is_current=True)
        assert "gpt-5.6-luna" in row.plain
        assert "Responses" in row.plain

    def test_provider_summary_counts(self):
        providers = {p.provider_id: p for p in summarize_providers(_options())}
        assert providers["oc"].total == 4
        assert providers["oc"].ready == 2
        assert providers["gen"].ready == 1
