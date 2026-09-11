"""Apply-to-project helper authority for the workspace screen.

This module owns the apply-to-project helper family operating on the
WORKSPACE SCREEN passed explicitly (the workspace screen remains the
one apply-action authority): the local-project candidate discovery,
session-directory resolution, the independent verifier-certificate
proof gate, the off-UI-thread gated apply worker, and the bounded
outcome reporting.  The apply ACTION methods on
:class:`~agentic_debugger.ui.screens_workspace.WorkspaceScreen`
delegate here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from agentic_debugger.application.events import SourceKind
from agentic_debugger.application.presentation import (
SessionViewState,
active_candidate_attempt,
)

def local_project_apply_candidate(
    screen,
) -> tuple[Optional[SessionViewState], Optional[str]]:
    """(session-final view, active candidate patch text) or reasons."""
    view = screen._full_session_view()
    if view is None or view.source_kind is not SourceKind.LOCAL_PROJECT:
        return view, None
    if not view.status.terminal:
        return view, None
    from agentic_debugger.application.presentation import active_candidate_attempt

    attempt = active_candidate_attempt(view)
    if attempt is None or not attempt.patch_text:
        return view, None
    return view, attempt.patch_text


def session_directory(screen) -> Optional[Path]:
    if screen._runner is not None:
        try:
            return Path(screen._runner.worker.session_dir)
        except Exception:
            return None
    if screen.entry is not None and screen.entry.directory:
        return Path(screen.entry.directory)
    return None


def local_project_apply_proof(
    screen,
    view: SessionViewState,
    patch_text: str,
) -> tuple[Optional[Path], Optional[str], str]:
    """Resolve and validate the independent certificate for one Apply.

    Old sessions without the versioned certificate remain inspectable but
    are deliberately not applyable.  A terminal/session-success claim is
    insufficient: the independent verifier must have returned RESOLVED,
    and its certificate must match both the recorded source HEAD and the
    exact candidate bytes.
    """
    summary = view.verifier_summary
    if (
        summary is None
        or summary.status != "COMPLETED"
        or summary.outcome is None
        or summary.outcome.value != "RESOLVED"
    ):
        return None, None, "candidate is not independently verified as RESOLVED"
    session_dir = screen._session_directory()
    if session_dir is None:
        return None, None, "session artifact directory is unavailable"
    try:
        from agentic_debugger.application.local_project import (
            check_verification_certificate,
            load_apply_verification_materials,
            local_project_task_spec_sha256,
        )

        task, certificate = load_apply_verification_materials(session_dir)
    except FileNotFoundError:
        return None, None, "independent verification certificate is missing"
    except Exception as exc:
        return None, None, f"independent verification certificate is invalid: {exc}"
    ok, reason = check_verification_certificate(
        certificate,
        expected_task_id=view.task_id,
        expected_session_id=view.session_id or "",
        expected_task_spec_sha256=local_project_task_spec_sha256(task),
        expected_head=task.source_head_commit,
        patch_text=patch_text,
    )
    if not ok:
        return None, None, reason
    return Path(task.source_repo_path), task.source_head_commit, "verified"


def apply_to_project_worker(
    screen, repo_path: Path, expected_head: str, patch_text: str
) -> None:
    """Gate + apply off the UI loop; report through the event loop."""
    from agentic_debugger.application.local_project import (
        apply_patch_to_project,
        check_apply_gates,
    )

    try:
        ok, reason = check_apply_gates(repo_path, expected_head, patch_text)
        if not ok:
            screen._report_apply_outcome(False, f"Apply To Project blocked: {reason}")
            return
        success, msg = apply_patch_to_project(
            repo_path,
            patch_text,
            expected_head=expected_head,
        )
        screen._report_apply_outcome(success, msg if success else f"Apply failed: {msg}")
    except Exception as exc:
        screen._report_apply_outcome(False, f"Apply To Project failed: {exc}")


def report_apply_outcome(screen, success: bool, message: str) -> None:
    try:
        # Only App provides call_from_thread; a Screen/DOMNode has no
        # such method, so the marshal must go through the running app.
        screen.app.call_from_thread(
            lambda: screen.notify(message, severity="information" if success else "error")
        )
    except Exception:
        pass
