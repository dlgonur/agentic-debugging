"""Unit tests for user-facing vocabulary repair (V2-03, ADR 0001 §9, V2 Plan §9).

Validates:
1. 'Transport Profile' is replaced with 'Endpoint contract'.
2. '(historical)' is removed from user-facing labels.
3. Provider dialog buttons use honest, modern preset labels:
   'Generic / OpenAI-compatible', 'CommandCode', 'OpenCode', 'Ollama'.
"""

from __future__ import annotations

import asyncio
from typing import Coroutine
import pytest
from textual.app import App
from textual.widgets import Button, Static

from agentic_debugger.application.provider_connections import (
    ENDPOINT_CONTRACT_DISPLAY_LABELS,
    TRANSPORT_DISPLAY_LABELS,
    ProviderConfig,
)
from agentic_debugger.ui.screens import (
    AddProviderDialogScreen,
    EditProviderDialogScreen,
)


class _VocabularyTestApp(App):
    pass


def _run_async(coro: Coroutine) -> None:
    asyncio.run(coro)


def test_no_historical_markers_in_display_labels() -> None:
    """'(historical)' must not appear in any user-facing contract or transport label."""
    for key, label in ENDPOINT_CONTRACT_DISPLAY_LABELS.items():
        assert "(historical)" not in label, f"Key {key} contains '(historical)' in {label}"

    for key, label in TRANSPORT_DISPLAY_LABELS.items():
        assert "(historical)" not in label, f"Key {key} contains '(historical)' in {label}"


def test_endpoint_contract_labels_honest_and_accurate() -> None:
    """Endpoint contract labels clearly express generic vs specialized contracts."""
    assert ENDPOINT_CONTRACT_DISPLAY_LABELS["generic"] == "Generic / OpenAI-compatible"
    assert ENDPOINT_CONTRACT_DISPLAY_LABELS["commandcode_goat"] == "CommandCode"
    assert ENDPOINT_CONTRACT_DISPLAY_LABELS["opencode_go"] == "OpenCode"
    assert ENDPOINT_CONTRACT_DISPLAY_LABELS["ollama_cloud"] == "Ollama"


def test_add_provider_dialog_vocabulary_and_buttons() -> None:
    """AddProviderDialogScreen contains 'Endpoint contract' and accurate preset buttons."""
    async def _action() -> None:
        app = _VocabularyTestApp()
        async with app.run_test() as pilot:
            dialog = AddProviderDialogScreen(on_save=lambda cfg: None)
            await pilot.app.push_screen(dialog)
            await pilot.pause()

            b_gen = dialog.query_one("#prof-generic", Button)
            assert str(b_gen.label) == "Generic / OpenAI-compatible"

            b_cmd = dialog.query_one("#prof-commandcode", Button)
            assert str(b_cmd.label) == "CommandCode"

            b_open = dialog.query_one("#prof-opencode", Button)
            assert str(b_open.label) == "OpenCode"

            b_ollama = dialog.query_one("#prof-ollama", Button)
            assert str(b_ollama.label) == "Ollama"

            static_texts = [str(s.render().plain) for s in dialog.query(Static)]
            assert any("Endpoint contract" in t for t in static_texts)
            assert not any("Transport Profile" in t for t in static_texts)

    _run_async(_action())


def test_edit_provider_dialog_vocabulary_and_buttons() -> None:
    """EditProviderDialogScreen contains 'Endpoint contract' and accurate preset buttons."""
    async def _action() -> None:
        cfg = ProviderConfig(
            name="Test Provider",
            base_url="https://api.test.com/v1",
            api_format="chat_completions",
            provider_id="test_prov",
            auth_mode="bearer",
            catalog_mode="openai",
            transport_profile="generic",
        )
        app = _VocabularyTestApp()
        async with app.run_test() as pilot:
            dialog = EditProviderDialogScreen(config=cfg, on_save=lambda cfg: None)
            await pilot.app.push_screen(dialog)
            await pilot.pause()

            b_gen = dialog.query_one("#prof-generic", Button)
            assert str(b_gen.label) == "Generic / OpenAI-compatible"

            b_cmd = dialog.query_one("#prof-commandcode", Button)
            assert str(b_cmd.label) == "CommandCode"

            b_open = dialog.query_one("#prof-opencode", Button)
            assert str(b_open.label) == "OpenCode"

            b_ollama = dialog.query_one("#prof-ollama", Button)
            assert str(b_ollama.label) == "Ollama"

            static_texts = [str(s.render().plain) for s in dialog.query(Static)]
            assert any("Endpoint contract" in t for t in static_texts)
            assert not any("Transport Profile" in t for t in static_texts)

    _run_async(_action())
