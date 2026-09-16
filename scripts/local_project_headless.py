#!/usr/bin/env python3
"""Headless Local Project Debug operator CLI (first-class, TUI-equivalent path).

Starts one Local Project Debug session without the terminal UI, through the
exact production path the TUI uses: project validation, clean-tree gate,
isolated detached worktree, immutable task contract, configured-profile /
Ollama-roster model resolution, cancellable worker supervision, verified
cleanup, and app-owned history registration.  No second controller, runtime,
workspace, patch, verifier, or event system: this is a thin headless driver
over the single accepted stack.

Usage::

    python scripts/local_project_headless.py --project DIR --bug "..." --profile ID
    python scripts/local_project_headless.py --project DIR --bug-file bug.txt --profile ID --root DIR
    python scripts/local_project_headless.py --project DIR --bug "..." --profile ID --repro "..." --verify "..."

The command-model configuration lives at ``<root>/config/command-models.json``
(the same ``CommandModelConfigStore`` the TUI reads); history is registered
into the same ``<root>`` so finished headless sessions appear in TUI history
and ``--export-session``.  Registry-backed provider models are NOT supported
here (fail-closed with a pointer to the TUI); configured command profiles and
Ollama-roster aliases are.

Exit codes: 0 = the session reached an honest terminal verdict with verified
cleanup and history registration (SUCCEEDED and UNRESOLVED both exit 0 — the
verifier verdict lives in the session report, not the exit code); 1 = the run
did not reach that state (validation passed but startup/cancel/cleanup/
registration failed, or Ctrl+C cancelled); 2 = usage or pre-execution
validation error (nothing was executed).

Offline only in the sense that this driver adds no network itself; the
configured model command is trusted user configuration whose own capabilities
are those of that executable under the host OS.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Bounded operator inputs (the persisted contract enforces its own tighter
# 4 KiB / 2 KiB bounds fail-closed; these only bound pre-validation reads).
MAX_BUG_FILE_BYTES = 8192
MAX_COMMAND_CHARS = 2048

TASK_ID = "local-project-debug"


@dataclass(frozen=True)
class HeadlessSessionOutcome:
    """Machine-readable result of one headless session attempt."""

    session_id: Optional[str]
    status: str
    termination_reason: str
    cleanup_verified: bool
    registered: bool
    registration_error: Optional[str]
    session_dir: Optional[str]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-project-headless",
        description=(
            "Start one Local Project Debug session headlessly through the "
            "production TUI path (validation, isolated worktree, worker, "
            "verified cleanup, history registration)."
        ),
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project path (absolute or relative to the shell cwd; must be a clean Git working tree).",
    )
    bug = parser.add_mutually_exclusive_group(required=True)
    bug.add_argument("--bug", default=None, help="Bug description (non-empty).")
    bug.add_argument(
        "--bug-file",
        default=None,
        help="Path to a UTF-8 file holding the bug description (bounded read).",
    )
    parser.add_argument(
        "--profile",
        required=True,
        help="Configured command-model profile id, or an Ollama-roster alias.",
    )
    parser.add_argument("--repro", default=None, help="Reproduction command (optional).")
    parser.add_argument("--verify", default=None, help="Verification command (optional).")
    parser.add_argument(
        "--root",
        default=None,
        help="App-owned history/config root (default: the TUI default history root).",
    )
    parser.add_argument(
        "--max-elapsed-seconds",
        type=int,
        default=None,
        help="Worker time bound in seconds (default: none, like the TUI No-limit setting).",
    )
    parser.add_argument(
        "--verifier-timeout-seconds",
        type=int,
        default=None,
        help="Verifier test-execution bound 1-600 (default: 120).",
    )
    parser.add_argument(
        "--project-env",
        default="",
        help="ProjEnv DSL: comma-separated variable NAMES only (values never accepted).",
    )
    return parser


def _read_bug_text(args: argparse.Namespace) -> str:
    """Resolve the bug description from --bug / --bug-file (bounded, fail-closed)."""
    if args.bug is not None:
        text = args.bug
    else:
        raw_path = Path(args.bug_file)
        try:
            with raw_path.open("rb") as handle:
                raw = handle.read(MAX_BUG_FILE_BYTES + 1)
        except OSError as exc:
            raise ValueError(f"bug file could not be read: {exc}") from None
        if len(raw) > MAX_BUG_FILE_BYTES:
            raise ValueError("bug file exceeds the bound")
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError(f"bug file is not UTF-8: {exc}") from None
    if not text.strip():
        raise ValueError("bug description must be non-empty")
    return text


def _optional_positive_int(value: Optional[int], label: str) -> Optional[int]:
    if value is None:
        return None
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise ValueError(f"{label} must be a positive int or omitted")
    return value


def run_session(args: argparse.Namespace) -> HeadlessSessionOutcome:
    """Execute one headless Local Project session; mirrors the TUI start path."""
    from agentic_debugger.application.command_config import CommandModelConfigStore
    from agentic_debugger.application.events import SourceKind
    from agentic_debugger.application.history import HistoryStore, default_history_root
    from agentic_debugger.application.local_project import (
        LocalProjectTaskSpec,
        capture_launch_cwd,
        cleanup_parent_tmpdir,
        create_isolated_worktree,
        get_launch_cwd,
        validate_local_project,
    )
    from agentic_debugger.application.local_project_source import (
        LOCAL_PROJECT_SOURCE_NAME,
    )
    from agentic_debugger.application.session import (
        SessionBudgets,
        SessionSpec,
    )
    from agentic_debugger.application.session_runtime import (
        ProjectEnvDeclaration,
        ProjectRuntimeEnvironmentSpec,
        spec_to_param,
    )
    from agentic_debugger.application.sources import ExecutionSourceSpec
    from agentic_debugger.application.worker_process import SessionWorkerProcess
    from agentic_debugger.ui.app_support import make_session_id
    from agentic_debugger.ui.session_config import (
        DEFAULT_VERIFIER_TIMEOUT_SECONDS,
        parse_project_env_declarations,
        validate_verifier_timeout_seconds,
    )

    # Preserve shell cwd before any root handling (same as the TUI launcher).
    try:
        capture_launch_cwd()
        launch_cwd = get_launch_cwd()
    except Exception:
        launch_cwd = Path.cwd().resolve()

    bug_description = _read_bug_text(args)
    max_elapsed_seconds = _optional_positive_int(args.max_elapsed_seconds, "max-elapsed-seconds")
    try:
        verifier_timeout_seconds = (
            DEFAULT_VERIFIER_TIMEOUT_SECONDS
            if args.verifier_timeout_seconds is None
            else validate_verifier_timeout_seconds(int(args.verifier_timeout_seconds))
        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"invalid verifier timeout: {exc}") from None

    history_root = Path(args.root) if args.root else default_history_root()
    store = HistoryStore(history_root)
    config_store = CommandModelConfigStore(store.root)

    profile_id = args.profile
    if type(profile_id) is not str or not profile_id.strip():
        raise ValueError("profile must be a non-empty string")

    # Model resolution parity with the TUI: Ollama roster alias first, then
    # the configured command-model store.  Registry providers stay TUI-only.
    expected_fp = None
    model_config_ref = None
    is_roster_alias = False
    try:
        from agentic_debugger.application.level32 import level32_model_profiles

        for candidate in level32_model_profiles():
            if candidate.alias == profile_id:
                expected_fp = candidate.transport_config_fingerprint
                model_config_ref = candidate.alias
                is_roster_alias = True
                break
    except Exception:
        pass
    if not is_roster_alias:
        try:
            prof = config_store.get(profile_id)
            expected_fp = prof.configuration_fingerprint
            model_config_ref = prof.profile_id
        except Exception as exc:
            raise ValueError(f"selected model profile unavailable: {exc}") from None

    try:
        env_inherit, env_secrets = parse_project_env_declarations(args.project_env or "")
        project_runtime_spec = ProjectRuntimeEnvironmentSpec(
            inherit=tuple(
                ProjectEnvDeclaration(name=name, required=required)
                for name, required in env_inherit
            ),
            secrets=tuple(
                ProjectEnvDeclaration(name=name, required=required)
                for name, required in env_secrets
            ),
        )
        project_runtime_param = spec_to_param(project_runtime_spec)
    except Exception as exc:
        raise ValueError(f"invalid project environment declaration: {exc}") from None

    try:
        validated = validate_local_project(args.project, launch_cwd=launch_cwd)
    except Exception as exc:
        raise ValueError(f"invalid project: {exc}") from None
    if validated.dirty:
        raise ValueError(
            "Project has uncommitted changes. "
            "Commit/stash them first or choose a clean repository."
        )

    try:
        worktree = create_isolated_worktree(validated.repo_root, validated.head_commit)
    except Exception as exc:
        raise ValueError(f"isolated worktree could not be created: {exc}") from None
    isolated_path = worktree.isolated_path
    parent_tmpdir = worktree.parent_tmpdir

    from datetime import datetime, timezone

    worker_owned = False
    worker = None
    try:
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        session_id = make_session_id()
        run_id = f"run-{session_id}"
        spec = SessionSpec(
            task_id=TASK_ID,
            source=ExecutionSourceSpec(
                kind=SourceKind.LOCAL_PROJECT,
                task_id=TASK_ID,
                policy=None,
                model_config_ref=model_config_ref,
            ),
            budgets=SessionBudgets(max_elapsed_seconds=max_elapsed_seconds),
        )
        try:
            local_spec = LocalProjectTaskSpec(
                session_id=session_id,
                source_repo_path=str(validated.repo_root),
                source_head_commit=validated.head_commit,
                isolated_workspace_path=str(isolated_path),
                bug_description=bug_description,
                reproduction_command=args.repro,
                verification_command=args.verify,
                model_runtime=profile_id,
                budgets=SessionBudgets(max_elapsed_seconds=max_elapsed_seconds),
                created_at_utc=created_at,
                project_runtime=project_runtime_spec.to_mapping(),
            )
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"invalid session contract: {exc}") from None
        scenario_params = {
            "project_repo_path": str(validated.repo_root),
            "project_head": validated.head_commit,
            "isolated_workspace": str(isolated_path),
            "bug_description": bug_description,
            "reproduction_command": args.repro,
            "verification_command": args.verify,
            "config_root": str(config_store.root),
            "profile_id": profile_id,
            "expected_fingerprint": expected_fp,
            "parent_tmpdir": str(parent_tmpdir),
            "policy": "pdb-on-uncertainty",
            "project_runtime_spec": project_runtime_param,
            "verifier_timeout_seconds": verifier_timeout_seconds,
        }
        if is_roster_alias:
            scenario_params["provider"] = "ollama_cloud"
            scenario_params["model_id"] = profile_id
            scenario_params["is_ollama"] = True
            scenario_params["ollama_alias"] = profile_id
        else:
            scenario_params["provider"] = "configured"

        worker = SessionWorkerProcess(
            session_dir=store.session_dir(session_id),
            session_id=session_id,
            spec=spec,
            run_id=run_id,
            scenario=LOCAL_PROJECT_SOURCE_NAME,
            scenario_params=scenario_params,
            cooperative_grace_seconds=10.0,
            ready_timeout_seconds=30.0,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        # Pre-write the contract artifact (same as the TUI start path).
        worker.session_dir.mkdir(parents=True, exist_ok=True)
        (worker.session_dir / "local_project_task.json").write_text(
            json.dumps(local_spec.to_mapping(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        immediate = worker.start()
        worker_owned = True
        if immediate is not None:
            result = immediate
        else:
            try:
                result = worker.wait()
            except KeyboardInterrupt:
                worker.cancel()
                result = worker.wait()
                return HeadlessSessionOutcome(
                    session_id=session_id,
                    status=str(getattr(result.status, "name", result.status)),
                    termination_reason=str(
                        getattr(result.termination_reason, "name", result.termination_reason)
                    ),
                    cleanup_verified=bool(result.cleanup_verified),
                    registered=False,
                    registration_error="cancelled by operator before registration",
                    session_dir=str(worker.session_dir),
                )
    except BaseException:
        if not worker_owned:
            try:
                cleanup_parent_tmpdir(parent_tmpdir, validated.repo_root)
            except Exception:
                pass
        raise
    finally:
        try:
            if worker is not None:
                worker.close()
        except Exception:
            pass

    # Post-terminal worktree check: the source owns cleanup; a leftover is
    # repaired here and reported honestly, never silently absorbed.
    worktree_leftover = parent_tmpdir.exists()
    if worktree_leftover:
        try:
            cleanup_parent_tmpdir(parent_tmpdir, validated.repo_root)
            worktree_leftover = parent_tmpdir.exists()
        except Exception:
            pass

    registration_error: Optional[str] = None
    try:
        store.register(worker.session_dir)
        registered = True
    except Exception as exc:
        registered = False
        registration_error = f"history registration failed: {exc}"

    return HeadlessSessionOutcome(
        session_id=session_id,
        status=str(getattr(result.status, "name", result.status)),
        termination_reason=str(
            getattr(result.termination_reason, "name", result.termination_reason)
        ),
        cleanup_verified=bool(result.cleanup_verified) and not worktree_leftover,
        registered=registered,
        registration_error=registration_error,
        session_dir=str(worker.session_dir),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for label, value in (("repro", args.repro), ("verify", args.verify)):
        if value is not None and len(value.encode("utf-8")) > MAX_COMMAND_CHARS:
            parser.error(f"--{label} exceeds the bound")
    try:
        outcome = run_session(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("error: cancelled before the worker started", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: headless session failed: {exc}", file=sys.stderr)
        return 1

    print(f"session: {outcome.session_id}")
    print(f"status: {outcome.status} ({outcome.termination_reason})")
    print(f"cleanup_verified: {outcome.cleanup_verified}")
    print(f"history: {outcome.session_dir}")
    if outcome.registered:
        print("registered: yes (visible in TUI history; use --export-session for the report)")
    else:
        print(f"registered: no ({outcome.registration_error})", file=sys.stderr)
    if outcome.status in ("SUCCEEDED", "UNRESOLVED") and outcome.cleanup_verified and outcome.registered:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
