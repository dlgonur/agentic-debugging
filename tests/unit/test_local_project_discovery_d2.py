"""D2 discovery confirm UX on the A1 form — offline proving.

Covers proposal §2 exactly: explicit Discover (d) into the What group,
proposed-tagged Bug/Repro/Verify, a Accept / e Edit / r Re-run, honest Low
fallback card, manual-supersedes, rows never disabled/hidden/reordered,
screen-memory-only metadata, keyboard contract, 80x24 + 120x30 legibility.

Offline only: no providers/live calls/network. The live-model gate uses a
configured command-model profile (never executed), same as the A3 matrix.
"""

from __future__ import annotations

import asyncio
import html
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("textual")

SIZES = [(80, 24), (120, 30)]
SIZE_IDS = ["80x24", "120x30"]

PROFILE_ID = "d2-discovery-model"


def _run_git(cwd: Path, args: list[str]) -> None:
    r = subprocess.run(
        ["git"] + args, cwd=str(cwd),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
    )
    assert r.returncode == 0, r.stderr


def _git_repo(tmp: Path, name: str, files: dict[str, str], message: str = "init") -> Path:
    repo = tmp / name
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, ["init"])
    _run_git(repo, ["config", "user.email", "d2@test.com"])
    _run_git(repo, ["config", "user.name", "D2"])
    for rel, text in files.items():
        full = repo / rel.replace("/", __import__("os").sep)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
    _run_git(repo, ["add", "."])
    _run_git(repo, ["commit", "-m", message])
    return repo


def _medium_repo(tmp: Path, name: str) -> Path:
    """Single test + marker => D1 Medium with deterministic repro."""
    return _git_repo(tmp, name, {
        "calc.py": "def add(a, b):\n    return a - b  # FIXME wrong op\n",
        "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n",
    })


def _low_repo(tmp: Path, name: str) -> Path:
    """Valid inventory, divergent signals => D1 Low, no repro."""
    return _git_repo(tmp, name, {
        "app.py": "def main():\n    return 0\n",
    })


def _high_repo(tmp: Path, name: str) -> Path:
    """repro.py + marker => D1 High."""
    return _git_repo(tmp, name, {
        "repro.py": "print('repro')\n",
        "app.py": "def run():\n    pass  # TODO fix this\n",
    })


def _write_configured_profile(root: Path, profile_id: str = PROFILE_ID) -> None:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "command-models.json").write_text(
        json.dumps({
            "schema_version": "command-models-v1",
            "profiles": [{
                "profile_id": profile_id,
                "display_name": profile_id,
                "executable": sys.executable,
                "argv": ["-c", "print('d2 stub')"],
                "request_timeout_seconds": 60.0,
            }],
        }),
        encoding="utf-8",
    )


def _start_screen(app, project):
    from agentic_debugger.ui.screens import StartSessionScreen

    return StartSessionScreen(
        task_options=list(app.curated_task_options()),
        initial_target="local_project",
        initial_project=str(project),
    )


def _plain(widget) -> str:
    rendered = widget.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


def _run_async(coro):
    return asyncio.run(coro)


def _is_shown(widget) -> bool:
    from textual.screen import Screen

    node = widget
    while node is not None and not isinstance(node, Screen):
        try:
            if node.display is False:
                return False
        except Exception:
            pass
        node = getattr(node, "parent", None)
    return True


def _painted_svg(app) -> str:
    return html.unescape(app.export_screenshot())


async def _wait_for(predicate, timeout: float = 15.0, interval: float = 0.1):
    import time

    start = time.monotonic()
    while True:
        try:
            if predicate():
                return True
        except Exception:
            pass
        if time.monotonic() - start > timeout:
            return False
        await asyncio.sleep(interval)


async def _discover_and_wait(lp, pilot, timeout: float = 20.0) -> bool:
    lp.action_discover()
    await pilot.pause()
    ok = await _wait_for(lambda: not getattr(lp, "_discovery_running", False), timeout=timeout)
    await pilot.pause()
    await asyncio.sleep(0.2)
    return ok


# ---------------------------------------------------------------------------
# 1. Discover -> proposal -> Accept fills and READY with zero typing
# ---------------------------------------------------------------------------

def test_d2_discover_accept_reaches_ready_zero_typing(tmp_path):
    async def _inner():
        from textual.widgets import Static

        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen
        from agentic_debugger.ui.setup_display import (
            bug_display_tag, repro_display_tag, verify_display_tag,
        )

        reset_launch_cwd()
        repo = _medium_repo(tmp_path, "d2medium1")
        set_launch_cwd_for_tests(tmp_path)
        hist = tmp_path / "hist-d2-accept"
        _write_configured_profile(hist)
        app = LocalApplicationV1(history_root=hist)
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, repo)
            assert isinstance(lp, StartSessionScreen)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._choice_selected("model", f"configured:{PROFILE_ID}")
            await pilot.pause()
            # Pre-discovery: Bug empty blocks honestly.
            assert lp._config.bug_description.strip() == ""
            assert lp._readiness is not None and not lp._readiness.ready
            # Discover (worker thread, bounded read-only).
            assert await _discover_and_wait(lp, pilot)
            assert lp._discovery_proposal is not None
            assert lp._discovery_proposal.confidence_overall == "medium"
            # Proposals land as proposed-tagged values (Q6 prefills Bug).
            assert lp._config.bug_description.strip() != ""
            assert lp._config.reproduction_command is not None
            assert lp._config.verification_command is not None
            assert bug_display_tag(lp) == "proposed"
            assert repro_display_tag(lp) == "proposed"
            assert verify_display_tag(lp) == "proposed"
            assert lp._bug_is_proposed and lp._repro_is_proposed and lp._verify_is_proposed
            # Proposed values already satisfy readiness (single derivation).
            assert lp._readiness.ready, f"proposed should be READY: {lp._readiness.status_line}"
            # Accept sets user_edited, clears transients, preserves values.
            bug_before = lp._config.bug_description
            repro_before = lp._config.reproduction_command
            verify_before = lp._config.verification_command
            lp.action_accept_proposal()
            await pilot.pause()
            assert lp._config.bug_description == bug_before
            assert lp._config.reproduction_command == repro_before
            assert lp._config.verification_command == verify_before
            assert lp._bug_user_edited and lp._repro_user_edited and lp._verify_user_edited
            assert not lp._bug_is_proposed and not lp._repro_is_proposed and not lp._verify_is_proposed
            assert bug_display_tag(lp) == "" and repro_display_tag(lp) == "" and verify_display_tag(lp) == ""
            assert lp._readiness.ready
            # Start path uses exact accepted strings (mocked, never executed).
            captured: dict = {}
            app.start_local_project_session = lambda **kw: captured.update(kw)  # type: ignore
            lp._start()
            await pilot.pause()
            assert captured.get("bug_description") == bug_before.strip()
            assert captured.get("reproduction_command") == repro_before
            assert captured.get("verification_command") == verify_before
            assert "proposal" not in {k.lower() for k in captured} and "confidence" not in {k.lower() for k in captured}
        reset_launch_cwd()

    _run_async(_inner())


def test_d2_high_proposal_overwrites_auto_with_proposed(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.setup_display import repro_display_tag

        reset_launch_cwd()
        repo = _high_repo(tmp_path, "d2high1")
        set_launch_cwd_for_tests(tmp_path)
        hist = tmp_path / "hist-d2-high"
        _write_configured_profile(hist)
        app = LocalApplicationV1(history_root=hist)
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            # Tracked repro.py gives the auto default before discovery.
            assert lp._config.reproduction_command == "python repro.py"
            assert lp._repro_is_auto is True
            lp._choice_selected("model", f"configured:{PROFILE_ID}")
            await pilot.pause()
            assert await _discover_and_wait(lp, pilot)
            assert lp._discovery_proposal is not None
            assert lp._discovery_proposal.confidence_overall == "high"
            # Proposed wins over auto (precedence), auto flag clears.
            assert lp._config.reproduction_command == "python repro.py"
            assert repro_display_tag(lp) == "proposed"
            assert lp._repro_is_proposed is True
            assert lp._repro_is_auto is False
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 2. Edit opens existing editors prefilled
# ---------------------------------------------------------------------------

def test_d2_edit_opens_existing_editors_prefilled(tmp_path):
    async def _inner():
        from textual.widgets import Input, TextArea

        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import (
            BugDescriptionEditorScreen,
            SingleLineFieldEditorScreen,
            StartSessionScreen,
        )

        reset_launch_cwd()
        repo = _medium_repo(tmp_path, "d2edit1")
        set_launch_cwd_for_tests(tmp_path)
        hist = tmp_path / "hist-d2-edit"
        _write_configured_profile(hist)
        app = LocalApplicationV1(history_root=hist)
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._choice_selected("model", f"configured:{PROFILE_ID}")
            await pilot.pause()
            assert await _discover_and_wait(lp, pilot)
            proposed_bug = lp._config.bug_description
            proposed_repro = lp._config.reproduction_command or ""
            assert proposed_bug.strip() != ""
            # Edit defaults to the focused What row; focus Bug explicitly.
            lp._focus_row("bug")
            await pilot.pause()
            lp.action_edit_proposal()
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert isinstance(app.screen, BugDescriptionEditorScreen)
            assert app.screen.query_one("#bug-editor", TextArea).text == proposed_bug
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert isinstance(app.screen, StartSessionScreen)
            # Repro editor prefilled with the proposed command + caption.
            lp._focus_row("repro")
            await pilot.pause()
            lp.action_edit_proposal()
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            assert app.screen.query_one("#single-line-editor", Input).value == proposed_repro
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.2)
            # Verify editor likewise.
            lp._focus_row("verify")
            await pilot.pause()
            lp.action_edit_proposal()
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            assert app.screen.query_one("#single-line-editor", Input).value == (lp._config.verification_command or "")
            await pilot.press("escape")
            await pilot.pause()
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 3. Re-run budget then manual-only
# ---------------------------------------------------------------------------

def test_d2_rerun_budget_then_manual_only(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1

        reset_launch_cwd()
        repo = _low_repo(tmp_path, "d2rerun1")
        set_launch_cwd_for_tests(tmp_path)
        hist = tmp_path / "hist-d2-rerun"
        _write_configured_profile(hist)
        app = LocalApplicationV1(history_root=hist)
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._choice_selected("model", f"configured:{PROFILE_ID}")
            await pilot.pause()
            assert await _discover_and_wait(lp, pilot)
            assert lp._discovery_proposal is not None
            assert lp._discovery_proposal.confidence_overall == "low"
            assert lp._discovery_visible_reruns == 0
            # First visible re-run.
            lp.action_rerun_discovery()
            await pilot.pause()
            ok = await _wait_for(lambda: not getattr(lp, "_discovery_running", False), timeout=20.0)
            assert ok
            await asyncio.sleep(0.2)
            assert lp._discovery_visible_reruns == 1
            assert lp._discovery_manual_only is False
            # Second visible re-run exhausts the budget on consecutive Low.
            lp.action_rerun_discovery()
            await pilot.pause()
            ok = await _wait_for(lambda: not getattr(lp, "_discovery_running", False), timeout=20.0)
            assert ok
            await asyncio.sleep(0.2)
            assert lp._discovery_visible_reruns == 2
            assert lp._discovery_manual_only is True
            # Third re-run performs no recon (generation unchanged, still manual-only).
            gen_before = lp._discovery_generation
            lp.action_rerun_discovery()
            await pilot.pause()
            await asyncio.sleep(0.4)
            assert lp._discovery_generation == gen_before
            assert lp._discovery_running is False
            assert lp._discovery_manual_only is True
            card = lp.query_one("#discovery-card")
            from agentic_debugger.ui.screens_shared import _markup_escape  # noqa: F401
            assert "Manual form is the path" in card.render().plain
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 4. Low fallback card, Bug empty, readiness blocks honestly
# ---------------------------------------------------------------------------

def test_d2_low_fallback_card_blocks_honestly(tmp_path):
    async def _inner():
        from textual.widgets import Button, Static

        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen
        from agentic_debugger.ui.screens_editors import SessionSettingRow

        reset_launch_cwd()
        repo = _low_repo(tmp_path, "d2low1")
        set_launch_cwd_for_tests(tmp_path)
        hist = tmp_path / "hist-d2-low"
        _write_configured_profile(hist)
        app = LocalApplicationV1(history_root=hist)
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._choice_selected("model", f"configured:{PROFILE_ID}")
            await pilot.pause()
            assert await _discover_and_wait(lp, pilot)
            assert lp._discovery_proposal is not None
            assert lp._discovery_proposal.confidence_overall == "low"
            assert list(lp._discovery_proposal.hypotheses) == []
            # Bug stays empty; readiness still blocks with the honest reason.
            assert lp._config.bug_description.strip() == ""
            assert not lp._readiness.ready
            assert any("Describe the bug" in i.message for i in lp._readiness.issues if i.field == "bug")
            # Fallback card lives above the rows, never placeholder-as-value.
            card = lp.query_one("#discovery-card", Static)
            assert card.display is True
            text = card.render().plain
            assert "couldn't determine" in text.lower()
            assert "Checked:" in text
            assert "Could NOT determine" in text
            assert "Apply-eligible" in text
            assert lp._config.bug_description == ""
            # Rows never disabled/hidden/reordered; Run still visible.
            for key in ("bug", "repro", "verify"):
                row = lp.query_one(f"#{key}-row", SessionSettingRow)
                assert row.display is not False and _is_shown(row)
                assert row.is_disabled is False
            assert lp._focusable_row_ids() == list(
                __import__("agentic_debugger.ui.session_config", fromlist=["LOCAL_SETUP_FOCUS_ORDER"]).LOCAL_SETUP_FOCUS_ORDER
            )
            button = lp.query_one("#start-session-button", Button)
            assert button.disabled is True
            assert isinstance(lp, StartSessionScreen)
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 5. Manual supersedes; metadata nowhere persisted
# ---------------------------------------------------------------------------

def test_d2_manual_supersedes_and_metadata_memory_only(tmp_path):
    async def _inner():
        import ast as _ast

        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.session_config import SessionConfig

        reset_launch_cwd()
        repo = _medium_repo(tmp_path, "d2manual1")
        set_launch_cwd_for_tests(tmp_path)
        hist = tmp_path / "hist-d2-manual"
        _write_configured_profile(hist)
        app = LocalApplicationV1(history_root=hist)
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._choice_selected("model", f"configured:{PROFILE_ID}")
            await pilot.pause()
            assert await _discover_and_wait(lp, pilot)
            proposed_bug = lp._config.bug_description
            assert proposed_bug.strip() != ""
            # Manual typing supersedes proposed (Bug).
            lp._on_bug_saved("my own words: off-by-one in add")
            await pilot.pause()
            assert lp._config.bug_description == "my own words: off-by-one in add"
            assert lp._bug_user_edited is True
            assert lp._bug_is_proposed is False
            # Manual typing supersedes proposed (Repro).
            lp._on_repro_saved("python -m pytest tests/test_calc.py -q -k add")
            await pilot.pause()
            assert lp._repro_user_edited is True
            assert lp._repro_is_proposed is False
            # A subsequent Re-run never overwrites manual values.
            lp.action_rerun_discovery()
            await pilot.pause()
            ok = await _wait_for(lambda: not getattr(lp, "_discovery_running", False), timeout=20.0)
            assert ok
            await asyncio.sleep(0.2)
            assert lp._config.bug_description == "my own words: off-by-one in add"
            assert lp._config.reproduction_command == "python -m pytest tests/test_calc.py -q -k add"
            # Metadata screen-memory-only: SessionConfig carries no proposal fields.
            fields = {f.name for f in SessionConfig.__dataclass_fields__.values()}
            assert "discovery_proposal" not in fields and "confidence" not in fields
            assert "proposed" not in fields and "could_not_determine" not in fields
            assert isinstance(lp._discovery_proposal, object)
            assert lp._discovery_proposal is not lp._config
            # Start carries only the three strings (mocked).
            captured: dict = {}
            app.start_local_project_session = lambda **kw: captured.update(kw)  # type: ignore
            lp.render_state()
            await pilot.pause()
            assert lp._readiness.ready
            lp._start()
            await pilot.pause()
            assert captured.get("bug_description") == "my own words: off-by-one in add"
            assert set(captured) == {
                "project_path", "bug_description", "reproduction_command",
                "verification_command", "profile_id", "model_provider",
                "max_elapsed_seconds", "auto_retries", "project_env_text",
            }
            # Recon is read-only: the source repo stays clean.
            r = subprocess.run(
                ["git", "status", "--porcelain"], cwd=str(repo),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
            )
            assert r.returncode == 0 and r.stdout.strip() == ""
            # No contract/verifier/journal code path was created for proposals.
            setup_src = Path("agentic_debugger/ui/screens_setup.py").read_text(encoding="utf-8")
            display_src = Path("agentic_debugger/ui/setup_display.py").read_text(encoding="utf-8")
            for needle in ("local_project_contracts", "local_project_verifier", "journal", "history_store"):
                assert needle not in display_src, needle
            tree = _ast.parse(setup_src)
            imported: list[str] = []
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Import):
                    imported.extend(a.name for a in node.names)
                elif isinstance(node, _ast.ImportFrom):
                    imported.append(node.module or "")
            joined = "\n".join(imported).lower()
            assert "local_project_contracts" not in joined
            assert "local_project_verifier" not in joined
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 6. Keyboard contract (old + new scoped keys)
# ---------------------------------------------------------------------------

def test_d2_keyboard_contract_holds(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen

        reset_launch_cwd()
        repo = _medium_repo(tmp_path, "d2keys1")
        set_launch_cwd_for_tests(tmp_path)
        hist = tmp_path / "hist-d2-keys"
        _write_configured_profile(hist)
        app = LocalApplicationV1(history_root=hist)
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            keys = {b.key for b in StartSessionScreen.BINDINGS}
            for required in ("s", "p", "t", "c", "h", "enter", "escape", "up", "down", "d", "a", "e", "r"):
                assert required in keys, f"missing binding {required}"
            # Existing up/down navigation still works with the new rows present.
            lp._focus_row("project")
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            assert getattr(app.screen.focused, "row_key", None) == "bug"
            await pilot.press("up")
            await pilot.pause()
            assert getattr(app.screen.focused, "row_key", None) == "project"
            # Scoped keys are no-ops without Local proposal (no crash, no mutation).
            before = (lp._config.bug_description, lp._config.reproduction_command)
            lp.action_accept_proposal()
            lp.action_edit_proposal()
            await pilot.pause()
            assert (lp._config.bug_description, lp._config.reproduction_command) == before
            # `d` discovers once Where+Model valid; `a` accepts afterwards.
            lp._choice_selected("model", f"configured:{PROFILE_ID}")
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            ok = await _wait_for(lambda: not getattr(lp, "_discovery_running", False), timeout=20.0)
            assert ok
            await asyncio.sleep(0.2)
            assert lp._discovery_proposal is not None
            await pilot.press("a")
            await pilot.pause()
            assert lp._bug_user_edited is True
        reset_launch_cwd()

    _run_async(_inner())


def test_d2_scoped_keys_noop_when_not_local(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen

        reset_launch_cwd()
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist-d2-nonlocal")
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = StartSessionScreen(
                task_options=list(app.curated_task_options()),
                initial_target="curated",
            )
            app.push_screen(screen)
            await pilot.pause()
            await asyncio.sleep(0.2)
            before = (screen._config.bug_description, screen._config.reproduction_command)
            screen.action_discover()
            screen.action_accept_proposal()
            screen.action_edit_proposal()
            screen.action_rerun_discovery()
            await pilot.pause()
            assert (screen._config.bug_description, screen._config.reproduction_command) == before
            assert screen._discovery_proposal is None
            assert screen._discovery_running is False
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 7. Readiness parity (single derivation, Accept changes only flags)
# ---------------------------------------------------------------------------

def test_d2_readiness_parity_proposed_vs_accepted(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.session_config import derive_readiness

        reset_launch_cwd()
        repo = _medium_repo(tmp_path, "d2parity1")
        set_launch_cwd_for_tests(tmp_path)
        hist = tmp_path / "hist-d2-parity"
        _write_configured_profile(hist)
        app = LocalApplicationV1(history_root=hist)
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._choice_selected("model", f"configured:{PROFILE_ID}")
            await pilot.pause()
            assert await _discover_and_wait(lp, pilot)
            proposed_ready = derive_readiness(lp._config, lp._catalog, lp._project_status)
            assert proposed_ready.ready
            n_issues = len(proposed_ready.issues)
            lp.action_accept_proposal()
            await pilot.pause()
            accepted_ready = derive_readiness(lp._config, lp._catalog, lp._project_status)
            # Same values, only flags differ: identical readiness object content.
            assert accepted_ready.ready == proposed_ready.ready
            assert accepted_ready.run_label == proposed_ready.run_label
            assert accepted_ready.status_line == proposed_ready.status_line
            assert len(accepted_ready.issues) == n_issues
            assert lp._readiness.ready and lp._readiness.status_line == accepted_ready.status_line
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 8. Progress + Esc-cancel with zero mutation
# ---------------------------------------------------------------------------

def test_d2_esc_cancels_with_zero_mutation(tmp_path, monkeypatch):
    async def _inner():
        import time as _time

        import agentic_debugger.application.local_project_discovery as disc

        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1

        reset_launch_cwd()
        repo = _medium_repo(tmp_path, "d2cancel1")
        set_launch_cwd_for_tests(tmp_path)
        hist = tmp_path / "hist-d2-cancel"
        _write_configured_profile(hist)

        real_discover = disc.discover_local_project

        def _slow(project_path, launch_cwd=None):
            _time.sleep(2.5)
            return real_discover(project_path, launch_cwd)

        monkeypatch.setattr(disc, "discover_local_project", _slow)
        # screens_setup imports inside the worker, so patch the source module.
        import agentic_debugger.ui.screens_setup as setup_mod

        app = LocalApplicationV1(history_root=hist)
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._choice_selected("model", f"configured:{PROFILE_ID}")
            await pilot.pause()
            bug_before = lp._config.bug_description
            repro_before = lp._config.reproduction_command
            lp.action_discover()
            await pilot.pause()
            await asyncio.sleep(0.3)
            assert lp._discovery_running is True
            card_text = lp.query_one("#discovery-card").render().plain
            assert "Discovering" in card_text and "Esc cancels" in card_text
            # Esc cancels: zero mutation, stays on the form.
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert lp._discovery_running is False
            assert lp._config.bug_description == bug_before
            assert lp._config.reproduction_command == repro_before
            assert lp._discovery_proposal is None
            # Late worker result is ignored (still zero mutation after 3 s).
            await asyncio.sleep(3.0)
            await pilot.pause()
            assert lp._config.bug_description == bug_before
            assert lp._config.reproduction_command == repro_before
            assert lp._discovery_running is False
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 9. Sizes: groups + proposal + fallback legible, Run reachable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size", SIZES, ids=SIZE_IDS)
def test_d2_proposal_legible_run_reachable(tmp_path, size):
    async def _inner():
        from textual.widgets import Button

        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1

        width, height = size
        reset_launch_cwd()
        repo = _medium_repo(tmp_path, f"d2prop-{width}x{height}")
        set_launch_cwd_for_tests(tmp_path)
        hist = tmp_path / f"hist-d2-prop-{width}x{height}"
        _write_configured_profile(hist)
        app = LocalApplicationV1(history_root=hist)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._choice_selected("model", f"configured:{PROFILE_ID}")
            await pilot.pause()
            assert await _discover_and_wait(lp, pilot)
            await pilot.pause()
            # Groups + proposal card legible in the initial viewport (What
            # group, above rows); the form scrolls, so vertical overflow is
            # reachability (scroll + click), not a clip. Horizontal must never clip.
            svg = _painted_svg(app)
            assert "WHAT" in svg and "Discover" in svg
            assert "Proposed" in svg or "proposed" in svg
            bad: list[str] = []
            for widget in lp.query("*"):
                if widget.screen is not lp or not _is_shown(widget):
                    continue
                try:
                    region = widget.region
                except Exception:
                    continue
                if region.width == 0 or region.height == 0:
                    continue
                if region.x + region.width > width:
                    bad.append(f"{type(widget).__name__}#{getattr(widget, 'id', None)} {region}")
            assert bad == [], f"horizontal clip at {width}x{height}: {bad}"
            button = lp.query_one("#start-session-button", Button)
            assert button.disabled is False
            # Run reachable via scroll, then clickable exactly once.
            try:
                button.scroll_visible(animate=False)
            except Exception:
                pass
            await pilot.pause()
            await asyncio.sleep(0.2)
            calls: list[dict] = []
            app.start_local_project_session = lambda **kw: calls.append(kw)  # type: ignore
            await pilot.click("#start-session-button")
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert len(calls) == 1
        reset_launch_cwd()

    _run_async(_inner())


@pytest.mark.parametrize("size", SIZES, ids=SIZE_IDS)
def test_d2_fallback_legible_run_visible(tmp_path, size):
    async def _inner():
        from textual.widgets import Button

        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1

        width, height = size
        reset_launch_cwd()
        repo = _low_repo(tmp_path, f"d2fall-{width}x{height}")
        set_launch_cwd_for_tests(tmp_path)
        hist = tmp_path / f"hist-d2-fall-{width}x{height}"
        _write_configured_profile(hist)
        app = LocalApplicationV1(history_root=hist)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._choice_selected("model", f"configured:{PROFILE_ID}")
            await pilot.pause()
            assert await _discover_and_wait(lp, pilot)
            await pilot.pause()
            svg = _painted_svg(app)
            assert "WHAT" in svg
            assert "couldn" in svg.lower() and "determine" in svg.lower()
            bad: list[str] = []
            for widget in lp.query("*"):
                if widget.screen is not lp or not _is_shown(widget):
                    continue
                try:
                    region = widget.region
                except Exception:
                    continue
                if region.width == 0 or region.height == 0:
                    continue
                if region.x + region.width > width:
                    bad.append(f"{type(widget).__name__}#{getattr(widget, 'id', None)} {region}")
            assert bad == [], f"horizontal clip at {width}x{height}: {bad}"
            button = lp.query_one("#start-session-button", Button)
            try:
                button.scroll_visible(animate=False)
            except Exception:
                pass
            await pilot.pause()
            region = button.region
            assert region.x + region.width <= width
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 10. D1 called unchanged (caps + hygiene pinned)
# ---------------------------------------------------------------------------

def test_d2_d1_function_unchanged() -> None:
    from agentic_debugger.application import local_project_discovery as disc

    assert disc.MAX_OPS == 12
    assert disc.WALL_BUDGET_S == 60.0
    assert disc.MAX_CHARS_PER_FILE == 4000
    assert disc.MAX_DEEP_FILES == 3
    assert disc.MAX_HYPOTHESES == 3
    assert disc.MAX_LOG_SUBJECTS == 20
    assert disc.MAX_TEST_CANDIDATES == 20
    text = Path("agentic_debugger/application/local_project_discovery.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            assert "textual" not in stripped.lower()
