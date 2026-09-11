"""Session UI widgets: the public widget surface.

This module is the public import surface for the session UI widgets.
The implementations live in cohesive modules:

* :mod:`agentic_debugger.ui.render_helpers` — shared styles, escaping,
  source highlighting, bounded text helpers;
* :mod:`agentic_debugger.ui.panels` — evidence/source/debugger/patch/
  verifier panels and the status header with token summaries;
* :mod:`agentic_debugger.ui.timing` — timing computation and the
  timeline panel/report;
* :mod:`agentic_debugger.ui.live_widgets` — live context, workstream
  and live-trace rendering, live/workstream/replay bars;
* :mod:`agentic_debugger.ui.exports` — live/activity/timeline text
  exports.

Rendering stays callback/data driven over presentation state; no
authoritative application state lives in widgets.
"""

from __future__ import annotations

from agentic_debugger.ui.exports import (
    activity_export_text,
    live_export_text,
)
from agentic_debugger.ui.live_widgets import (
    LiveBar,
    LivePanel,
    LiveRunContextPanel,
    ReplayBar,
    WorkstreamPanel,
    render_live_trace,
    render_workstream,
)
from agentic_debugger.ui.panels import (
    DebuggerPanel,
    EvidenceReviewPanel,
    PatchPanel,
    SourcePanel,
    StatusHeader,
    VerifierPanel,
    format_token_count_compact,
    session_tokens_breakdown,
    session_tokens_summary,
)
from agentic_debugger.ui.render_helpers import (
    PATCH_PANE_PREVIEW_LIMITS,
    _ACTIVITY_FILTER_KINDS,
    _KIND_STYLE,
    _entry_style,
    _highlight_source_lines,
    EvidenceState,
)
from agentic_debugger.ui.timing import (
    SessionTiming,
    TimedOperation,
    TimingCategorySummary,
    TimelinePanel,
    _local_project_identity,
    timeline_export_text,
    _official_tests_label,
    compute_session_timing,
    render_timeline_report,
)

__all__ = [
    "DebuggerPanel",
    "EvidenceState",
    "LiveBar",
    "LivePanel",
    "LiveRunContextPanel",
    "PATCH_PANE_PREVIEW_LIMITS",
    "PatchPanel",
    "ReplayBar",
    "SessionTiming",
    "SourcePanel",
    "StatusHeader",
    "TimedOperation",
    "TimelinePanel",
    "TimingCategorySummary",
    "VerifierPanel",
    "WorkstreamPanel",
    "activity_export_text",
    "compute_session_timing",
    "live_export_text",
    "render_live_trace",
    "render_timeline_report",
    "render_workstream",
    "timeline_export_text",
]
