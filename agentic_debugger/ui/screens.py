"""Screens for the Agentic Debugger terminal application.

Compatibility facade: every screen now lives in a responsibility-owned
module; this module only assembles and re-exports the public surface so
existing ``from agentic_debugger.ui.screens import ...`` imports keep
working.

Module map (ownership, not size chunks):

- :mod:`agentic_debugger.ui.screens_shared` -- footer vocabulary, style
  maps, recorded-text formatting helpers, shared view header, help modal.
- :mod:`agentic_debugger.ui.screens_home` -- welcome + session archive.
- :mod:`agentic_debugger.ui.screens_editors` -- session-setup editor
  family (choice picker, setting row, field editors, browser, bug editor).
- :mod:`agentic_debugger.ui.screens_providers` -- provider-management
  workflow (manager, catalog browser, add/edit/manual/delete dialogs).
- :mod:`agentic_debugger.ui.screens_setup` -- the one session-setup
  surface (StartSessionScreen) plus its row-fitting helpers.
- :mod:`agentic_debugger.ui.screens_workspace` -- REPLAY/LIVE workspace
  plus the jump-to-sequence and effort modals.

Screens are presentation-only.  The home screen exposes app-owned history
through the accepted :class:`HistoryStore`; the workspace renders one
:class:`SessionViewState` in either read-only REPLAY mode or LIVE mode; the
start-session screen is the only place a bounded new deterministic session
may be requested.  No screen executes controller, PDB, patch, verifier, or
model work.
"""

from __future__ import annotations

from agentic_debugger.ui.screens_editors import (
    BrowseScreen,
    BugDescriptionEditorScreen,
    ChoiceOption,
    ChoicePickerScreen,
    SessionSettingRow,
    SingleLineEditorInput,
    SingleLineFieldEditorScreen,
    TimeLimitEditorInput,
    TimeLimitEditorScreen,
)
from agentic_debugger.ui.screens_editors import (
    _SingleLineEditorScreen as _SingleLineEditorScreen,
)
from agentic_debugger.ui.screens_home import (
    BANNER_3LINE,
    BANNER_WIDE_SLANT,
    HistoryScreen,
    HomeActionRow,
    HomeScreen,
    verifier_cell,
)
from agentic_debugger.ui.screens_providers import (
    AddManualModelDialogScreen,
    AddProviderDialogScreen,
    ConfirmDeleteProviderDialogScreen,
    EditProviderDialogScreen,
    ModelCatalogBrowserScreen,
    ModelProvidersScreen,
    ProviderConnectionsScreen,
)
from agentic_debugger.ui.screens_providers import (
    _PROVIDER_CREDENTIAL_SOURCE_LABELS as _PROVIDER_CREDENTIAL_SOURCE_LABELS,
)
from agentic_debugger.ui.screens_setup import (
    StartSessionScreen,
    format_model_display_name,
    list_provider_models,
)
from agentic_debugger.ui.screens_setup import (
    _clip_cells as _clip_cells,
    _fit_row_cells as _fit_row_cells,
    _provider_label as _provider_label,
    _short_unavailable_reason as _short_unavailable_reason,
)
from agentic_debugger.ui.screens_shared import (
    REPLAY_FOOTER,
    REPLAY_FOOTER_COMPACT,
    START_FOOTER,
    START_FOOTER_COMPACT,
    WORKSPACE_FOOTER_ACTIVE,
    WORKSPACE_FOOTER_ACTIVE_COMPACT,
    WORKSPACE_FOOTER_IDLE,
    WORKSPACE_FOOTER_IDLE_COMPACT,
    HelpModalScreen,
    render_view_header,
)
from agentic_debugger.ui.screens_shared import (
    _CLASSIFICATION_STYLE as _CLASSIFICATION_STYLE,
    _RESULT_STYLE as _RESULT_STYLE,
    _TERMINAL_KINDS as _TERMINAL_KINDS,
    _compact_session_id as _compact_session_id,
    _compact_source_label as _compact_source_label,
    _format_duration as _format_duration,
    _format_timestamp as _format_timestamp,
    _markup_escape as _markup_escape,
    _operator_stage_label as _operator_stage_label,
)
from agentic_debugger.ui.screens_workspace import (
    CopyAllButton,
    EffortModalScreen,
    JumpToSequenceScreen,
    WorkspaceMode,
    WorkspaceScreen,
)

__all__ = [
    "AddManualModelDialogScreen",
    "AddProviderDialogScreen",
    "BANNER_3LINE",
    "BANNER_WIDE_SLANT",
    "BrowseScreen",
    "BugDescriptionEditorScreen",
    "ChoiceOption",
    "ChoicePickerScreen",
    "ConfirmDeleteProviderDialogScreen",
    "CopyAllButton",
    "EditProviderDialogScreen",
    "EffortModalScreen",
    "HelpModalScreen",
    "HistoryScreen",
    "HomeActionRow",
    "HomeScreen",
    "JumpToSequenceScreen",
    "ModelCatalogBrowserScreen",
    "ModelProvidersScreen",
    "ProviderConnectionsScreen",
    "REPLAY_FOOTER",
    "REPLAY_FOOTER_COMPACT",
    "START_FOOTER",
    "START_FOOTER_COMPACT",
    "SessionSettingRow",
    "SingleLineEditorInput",
    "SingleLineFieldEditorScreen",
    "StartSessionScreen",
    "TimeLimitEditorInput",
    "TimeLimitEditorScreen",
    "WORKSPACE_FOOTER_ACTIVE",
    "WORKSPACE_FOOTER_ACTIVE_COMPACT",
    "WORKSPACE_FOOTER_IDLE",
    "WORKSPACE_FOOTER_IDLE_COMPACT",
    "WorkspaceMode",
    "WorkspaceScreen",
    "format_model_display_name",
    "list_provider_models",
    "render_view_header",
    "verifier_cell",
]
