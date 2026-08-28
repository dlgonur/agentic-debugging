"""Read-only, portable reports over validated application session evidence.

Reports are derived from the same immutable presentation reducer used by the
terminal UI.  They never execute a controller, model, debugger, tool, patch,
or verifier and deliberately omit source and patch bodies.  The result is a
concise Markdown artifact suitable for review without copying an entire run
directory or exposing app-owned storage roots.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Sequence

from agentic_debugger.application import ApplicationError, ApplicationInputError
from agentic_debugger.application.case_brief import project_case_brief
from agentic_debugger.application.events import contains_credential_shape
from agentic_debugger.application.history import ReopenedSession, SessionHistoryEntry
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    SessionViewState,
    initial_session_view,
    reduce_event,
)

__all__ = [
    "SessionReportError",
    "render_session_listing",
    "render_session_report",
    "write_session_report",
]


class SessionReportError(ApplicationError):
    """Raised when a session report cannot be rendered or written safely."""


def _display(
    value: object | None,
    *,
    hidden_roots: Sequence[tuple[str, str]] = (),
) -> str:
    """Bound one value to a single escaped Markdown line."""

    if value is None:
        return "Not recorded"
    raw = str(value).replace("\r", " ").replace("\n", " ")
    if contains_credential_shape(raw):
        raise SessionReportError(
            "session report withheld because recorded presentation text "
            "contains a credential-shaped value"
        )
    for root, replacement in hidden_roots:
        if not root:
            continue
        variants = {root, root.replace("\\", "/"), root.replace("/", "\\")}
        for variant in sorted(variants, key=len, reverse=True):
            raw = re.sub(
                re.escape(variant),
                lambda _match: replacement,
                raw,
                flags=re.IGNORECASE if os.name == "nt" else 0,
            )
    # Values originate in validated events, but Markdown punctuation can
    # still alter the report structure.  Escape it rather than trusting a
    # task name, diagnosis, file name, or tool-produced summary as markup.
    escaped = raw.replace("\\", "\\\\")
    for char in "`*_{}[]<>#|":
        escaped = escaped.replace(char, f"\\{char}")
    return escaped or "Not recorded"


def _enum_value(value: object | None) -> object | None:
    return getattr(value, "value", value)


def _count(passed: Optional[int], total: Optional[int]) -> str:
    if passed is None or total is None:
        return "Not recorded"
    return f"{passed}/{total}"


def _build_view(reopened: ReopenedSession) -> SessionViewState:
    replay = reopened.replay
    view = initial_session_view(
        PresentationIdentity(
            task_id=replay.task_id,
            source_kind=replay.source_kind,
            session_id=replay.session_id,
        )
    )
    for event in replay.events:
        view = reduce_event(view, event)
    return view


def render_session_report(reopened: ReopenedSession) -> str:
    """Render one validated app-owned session as concise Markdown.

    The input must come from :meth:`HistoryStore.reopen`, which has already
    validated containment, journal structure, identity, and any manifest.
    """

    if type(reopened) is not ReopenedSession:
        raise ApplicationInputError("reopened must be a ReopenedSession")

    entry = reopened.entry
    view = _build_view(reopened)
    event_count = len(reopened.replay.events)
    hidden_roots = tuple(
        (value, replacement)
        for value, replacement in (
            (entry.directory, "[session]"),
            (str(Path.home().resolve()), "[home]"),
        )
        if value is not None
    )

    def display(value: object | None) -> str:
        return _display(value, hidden_roots=hidden_roots)

    cleanup = (
        "Verified"
        if view.cleanup_verified is True
        else "Not required"
        if view.cleanup_not_required
        else "Unverified"
        if view.cleanup_verified is False
        else "Not recorded"
    )
    brief = project_case_brief(view)

    lines = [
        "# Agentic Debugger Session Report",
        "",
        (
            "Generated read-only from validated, app-owned session evidence. "
            "No model, debugger, tool, patch, or verifier was executed while "
            "creating this report. Source text and patch bodies are omitted."
        ),
        "",
        "## Session",
        "",
        f"- Session ID: {display(entry.session_id or view.session_id)}",
        f"- Task: {display(view.task_id)}",
        f"- Source: {display(view.source_kind.value)}",
        f"- History classification: {display(entry.classification.value)}",
        f"- Application status: {display(view.status.value)}",
        f"- Termination reason: {display(_enum_value(view.termination_reason))}",
        f"- Run ID: {display(view.run_id)}",
        f"- Started (UTC): {display(entry.started_at_utc)}",
        f"- Ended (UTC): {display(entry.ended_at_utc)}",
        f"- Durable events: {event_count}",
        f"- Cleanup: {cleanup}",
        f"- Configuration fingerprint: {display(entry.config_fingerprint)}",
        "",
        "## Evidence chain",
        "",
        (
            "A controller diagnosis is a claim. The independent verifier is "
            "the correctness authority."
        ),
        "",
    ]

    for stage in brief.stages:
        lines.append(
            f"- {display(stage.kind.value.title())} "
            f"[{display(stage.state.value.upper())}]: "
            f"{display(stage.title)} - {display(stage.detail)}"
        )

    lines.extend(
        [
            "",
            f"Case verdict: {display(brief.verdict)}",
            (
                "Verdict authority: Independent verifier"
                if brief.verdict_authoritative
                else "Verdict authority: Not yet authoritative"
            ),
            "",
            "## Independent verification",
            "",
        ]
    )

    verifier = view.verifier_summary
    if verifier is None:
        lines.append("Not recorded.")
    else:
        lines.extend(
            [
                f"- Status: {display(verifier.status)}",
                f"- Semantic outcome: {display(_enum_value(verifier.outcome))}",
                f"- Fail-to-pass: {_count(verifier.f2p_passed, verifier.f2p_total)}",
                f"- Pass-to-pass: {_count(verifier.p2p_passed, verifier.p2p_total)}",
                f"- Classification: {display(verifier.classification)}",
                (
                    "- Official test execution proven: "
                    f"{display(verifier.official_test_execution_proven)}"
                ),
                f"- Verifier workspace cleaned: {display(verifier.workspace_cleaned)}",
            ]
        )

    lines.extend(["", "## Diagnosis", ""])
    diagnosis = view.diagnosis
    if diagnosis is None:
        lines.append("Not recorded.")
    else:
        lines.extend(
            [
                f"- Summary: {display(diagnosis.text)}",
                f"- File: {display(diagnosis.file_path)}",
                f"- Symbol: {display(diagnosis.symbol)}",
                f"- Confidence: {display(diagnosis.confidence)}",
            ]
        )

    lines.extend(["", "## What the agent tried", ""])
    from agentic_debugger.application.effort_summary import (
        render_effort_summary,
        summarize_events,
    )

    lines.append(
        render_effort_summary(
            summarize_events(reopened.replay.events),
            title="Counted effort (journal-derived)",
        )
    )
    if entry.retry_of_session_id is not None:
        lines.extend(
            [
                "",
                f"- Retry of session: {display(entry.retry_of_session_id)}",
            ]
        )

    lines.extend(["", "## Debugger evidence", ""])
    debugger = view.debugger
    if not view.pdb_observed and not debugger.session_started:
        lines.append("Not recorded.")
    else:
        location = "Not recorded"
        if debugger.function is not None or debugger.line is not None:
            location = (
                f"{display(debugger.function)} at line "
                f"{display(debugger.line)}"
            )
        lines.extend(
            [
                f"- Script: {display(debugger.script)}",
                f"- Last location: {location}",
                f"- Stack frames recorded: {len(debugger.frames)}",
                f"- Local values recorded: {len(debugger.locals)}",
            ]
        )

    lines.extend(["", "## Patch attempts", ""])
    if not view.patch_attempts:
        lines.append("None recorded.")
    else:
        for attempt in view.patch_attempts:
            changed = ", ".join(display(path) for path in attempt.changed_files)
            detail = (
                f"stage={display(attempt.stage.value)}; "
                f"sha256={display(attempt.patch_sha256)}; "
                f"changed files={changed or 'Not recorded'}; "
                f"syntax passed={display(attempt.syntax_passed)}"
            )
            lines.append(f"- Attempt {attempt.attempt_index + 1}: {detail}")

    lines.extend(["", "## Timeline", ""])
    if len(view.timeline) < event_count:
        lines.extend(
            [
                (
                    f"The presentation safety bound retains the most recent "
                    f"{len(view.timeline)} of {event_count} event summaries."
                ),
                "",
            ]
        )
    if not view.timeline:
        lines.append("No events recorded.")
    else:
        for item in view.timeline:
            lines.append(
                f"- #{item.sequence} `{item.event_kind.value}` - "
                f"{display(item.summary)}"
            )
    report = "\n".join(lines) + "\n"
    if contains_credential_shape(report):
        raise SessionReportError(
            "session report withheld because recorded presentation text "
            "contains a credential-shaped value"
        )
    return report


def render_session_listing(entries: Sequence[SessionHistoryEntry]) -> str:
    """Render a deterministic, headless index for discovering session IDs."""

    if not entries:
        return "No app-owned sessions found.\n"
    lines = ["SESSION ID\tCLASSIFICATION\tSTATUS\tTASK\tVERIFIER OUTCOME"]
    for entry in entries:
        fields = (
            entry.session_id,
            entry.classification.value,
            _enum_value(entry.status),
            entry.task_id,
            entry.verifier_outcome,
        )
        lines.append(
            "\t".join(
                str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
                if value is not None
                else "-"
                for value in fields
            )
        )
    return "\n".join(lines) + "\n"


def write_session_report(
    reopened: ReopenedSession,
    destination: str | os.PathLike[str],
) -> Path:
    """Write a report once, refusing to overwrite an existing path."""

    path = Path(destination)
    if not path.name:
        raise SessionReportError("report destination must name a file")
    if not path.parent.is_dir():
        raise SessionReportError(
            f"report destination directory does not exist: {path.parent}"
        )
    report = render_session_report(reopened)
    created = False
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as stream:
            created = True
            stream.write(report)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise SessionReportError(f"report destination already exists: {path}") from exc
    except OSError as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise SessionReportError(f"could not write session report at {path}: {exc}") from exc
    return path.resolve()
