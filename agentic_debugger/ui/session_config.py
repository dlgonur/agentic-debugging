"""The unified session-configuration and readiness model.

One pure, Textual-free authority answers every question the session-setup
surface can ask:

- what is currently selected (:class:`SessionConfig`);
- what the environment offers (:class:`SessionCatalog`);
- whether the session can start, why (not), which controls are
  applicable, and what the Run action should say
  (:class:`SessionReadiness` from :func:`derive_readiness`).

Every presentation of readiness — the Run button's enabled state, the
status line, the pre-flight rail, the model picker's compatibility
annotations — MUST render from the single :class:`SessionReadiness`
object.  Deriving any of these from a second path is what allowed
mutually contradictory "ready" and "Start unavailable" states.

Design rules that keep the surface coherent:

- rows never disappear: incompatible rows are disabled with a reason
  (:class:`RowState`), so every control keeps a fixed, predictable place;
- one option change never silently mutates another selection; the user's
  selections persist and incompatibilities surface as readiness issues;
- scientific qualification (the capability ladder) disables choices and
  explains why; it never hides them and never relaxes its contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional, Tuple

# -- targets -----------------------------------------------------------------

TARGET_CURATED = "curated"
TARGET_LOCAL_PROJECT = "local_project"
TARGET_LADDER = "ladder"

TARGET_LABELS = {
    TARGET_CURATED: "Curated task",
    TARGET_LOCAL_PROJECT: "Local project",
    TARGET_LADDER: "Capability ladder",
}

# -- providers ---------------------------------------------------------------

PROVIDER_OFFLINE = "offline"
PROVIDER_OLLAMA = "ollama_cloud"
PROVIDER_OPENCODE = "opencode_go"
PROVIDER_COMMANDCODE = "commandcode_goat"
PROVIDER_CONFIGURED = "configured"

PROVIDER_LABELS = {
    PROVIDER_OFFLINE: "Offline",
    PROVIDER_OLLAMA: "Ollama Cloud",
    PROVIDER_OPENCODE: "OpenCode Go",
    PROVIDER_COMMANDCODE: "CommandCode GOAT",
    PROVIDER_CONFIGURED: "Custom command profile",
}

# -- debugger policies ---------------------------------------------------------

POLICY_ON_UNCERTAINTY = "pdb-on-uncertainty"
POLICY_STATIC_BASELINE = "static-baseline"

POLICY_LABELS = {
    POLICY_ON_UNCERTAINTY: "On uncertainty",
    POLICY_STATIC_BASELINE: "Disabled",
}

AUTO_RETRY_MAX = 3

# Row keys: the fixed control order of the session-setup surface.
ROW_TARGET = "target"
ROW_TASK = "task"
ROW_PROJECT = "project"
ROW_BUG = "bug"
ROW_REPRO = "repro"
ROW_VERIFY = "verify"
ROW_MODEL = "model"
ROW_DEBUGGER = "debugger"
ROW_TIME_LIMIT = "time_limit"
ROW_AUTO_RETRY = "auto_retry"

ROW_ORDER = (
    ROW_TARGET,
    ROW_TASK,
    ROW_PROJECT,
    ROW_BUG,
    ROW_REPRO,
    ROW_VERIFY,
    ROW_MODEL,
    ROW_DEBUGGER,
    ROW_TIME_LIMIT,
    ROW_AUTO_RETRY,
)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class ModelChoice:
    """The user's model selection (provider + model identity)."""

    provider: str
    model_id: str
    display: str

    @property
    def is_offline(self) -> bool:
        return self.provider == PROVIDER_OFFLINE


OFFLINE_CHOICE = ModelChoice(PROVIDER_OFFLINE, "", "Offline")


@dataclass(frozen=True)
class ModelOption:
    """One selectable entry of the unified model picker."""

    provider: str
    model_id: str
    display: str
    detail: str = ""
    available: bool = True
    unavailable_reason: Optional[str] = None

    @property
    def choice(self) -> ModelChoice:
        return ModelChoice(self.provider, self.model_id, self.display)


@dataclass(frozen=True)
class TaskOption:
    """One selectable entry of the task picker."""

    task_id: str
    title: str
    ladder: bool = False
    detail: str = ""


@dataclass(frozen=True)
class ProjectStatus:
    """Cached validation result of the local project path (no mutation)."""

    path: str
    ok: bool
    state: str  # "clean" | "dirty" | "invalid" | "unchecked"
    message: str = ""

    @classmethod
    def unchecked(cls, path: str) -> "ProjectStatus":
        return cls(path=path, ok=False, state="unchecked", message="")


@dataclass(frozen=True)
class SessionCatalog:
    """Everything the environment offers, gathered read-only and offline.

    ``models`` is the unified provider list for curated and local-project
    targets.  ``ladder_models`` is the qualified Ollama Cloud roster the
    scientific ladder contract allows.
    """

    tasks: Tuple[TaskOption, ...] = ()
    models: Tuple[ModelOption, ...] = ()
    ladder_models: Tuple[ModelOption, ...] = ()
    configured_error: Optional[str] = None

    def find_task(self, task_id: Optional[str]) -> Optional[TaskOption]:
        if task_id is None:
            return None
        return next((item for item in self.tasks if item.task_id == task_id), None)

    def find_model(self, choice: ModelChoice) -> Optional[ModelOption]:
        """The catalog entry matching a choice (offline resolves always)."""
        if choice.is_offline:
            return next(
                (m for m in self.models if m.provider == PROVIDER_OFFLINE), None
            )
        return next(
            (
                m
                for m in self.models
                if m.provider == choice.provider and m.model_id == choice.model_id
            ),
            None,
        )

    def ladder_model(self, choice: ModelChoice) -> Optional[ModelOption]:
        """Return the qualified ladder entry for an Ollama Cloud choice.

        Model ids are not globally unique across providers.  Qualification
        therefore binds the frozen roster alias to its provider identity;
        a configured or subscription model with the same text must never be
        reinterpreted as the qualified Ollama model.
        """
        if choice.provider != PROVIDER_OLLAMA:
            return None
        return next(
            (
                m
                for m in self.ladder_models
                if m.provider == PROVIDER_OLLAMA and m.model_id == choice.model_id
            ),
            None,
        )


@dataclass
class SessionConfig:
    """The user's complete selection.  Mutated only by explicit user action."""

    target: str = TARGET_CURATED
    task_id: Optional[str] = None
    project_path: str = ""
    bug_description: str = ""
    reproduction_command: Optional[str] = None
    verification_command: Optional[str] = None
    model: ModelChoice = OFFLINE_CHOICE
    debugger_policy: str = POLICY_ON_UNCERTAINTY
    time_limit_seconds: Optional[int] = None
    auto_retries: int = 1

    def with_target(self, target: str) -> "SessionConfig":
        """Switch target WITHOUT touching any other selection."""
        if target not in TARGET_LABELS:
            raise ValueError(f"unknown target: {target!r}")
        return replace(self, target=target)


@dataclass(frozen=True)
class RowState:
    """Per-row applicability.  Disabled rows stay visible with a reason."""

    enabled: bool
    reason: str = ""


@dataclass(frozen=True)
class ReadinessIssue:
    """One concrete blocker or caution attached to a row."""

    field: str
    severity: str  # SEVERITY_ERROR | SEVERITY_WARNING
    message: str


@dataclass(frozen=True)
class SessionReadiness:
    """The single derived truth for the whole session-setup surface."""

    ready: bool
    run_label: str
    status_line: str
    issues: Tuple[ReadinessIssue, ...] = ()
    rows: dict = field(default_factory=dict)
    notes: Tuple[str, ...] = ()


# -- compatibility -------------------------------------------------------------


def model_compatibility(
    target: str,
    option: ModelOption,
    *,
    ladder_qualified: bool = False,
) -> Tuple[bool, str]:
    """Whether one model option may be selected for one target.

    Pure policy: availability of the provider itself is a separate
    concern (``option.available``); this answers contract compatibility
    only, with the exact reason shown in the picker and the pre-flight.
    """
    provider = option.provider
    if provider == PROVIDER_OFFLINE:
        if target == TARGET_LOCAL_PROJECT:
            return False, "Local Project requires a live model"
        if target == TARGET_LADDER:
            return False, "Ladder runs use qualified Ollama Cloud models"
        return True, ""
    if target == TARGET_LADDER and provider == PROVIDER_OLLAMA:
        if ladder_qualified:
            return True, ""
        return (
            False,
            "Scientific ladder contract: Ollama model is not qualified",
        )
    if target == TARGET_LADDER:
        return (
            False,
            "Scientific ladder contract: qualified Ollama Cloud models only",
        )
    return True, ""


def _row_states(target: str) -> dict:
    local = target == TARGET_LOCAL_PROJECT
    ladder = target == TARGET_LADDER
    rows = {
        ROW_TARGET: RowState(True),
        ROW_TASK: RowState(
            not local, "Local Project debug uses your repository, not a fixture task"
        ),
        ROW_PROJECT: RowState(local, "Local Project sessions only"),
        ROW_BUG: RowState(local, "Local Project sessions only"),
        ROW_REPRO: RowState(local, "Local Project sessions only"),
        ROW_VERIFY: RowState(local, "Local Project sessions only"),
        ROW_MODEL: RowState(True),
        ROW_DEBUGGER: RowState(
            not (local or ladder),
            "Fixed by the Local Project contract"
            if local
            else "Frozen by the ladder contract",
        ),
        ROW_TIME_LIMIT: RowState(
            not ladder, "Frozen operator budget" if ladder else ""
        ),
        ROW_AUTO_RETRY: RowState(
            not (ladder or target == TARGET_CURATED),
            "Frozen by the ladder contract"
            if ladder
            else "single attempt; retry with r",
        ),
    }
    return rows


def _ladder_readiness(config: SessionConfig, catalog: SessionCatalog):
    issues = []
    notes = ["Research tasks use the canonical Ollama Cloud operator contract."]
    task = catalog.find_task(config.task_id)
    if task is None or not task.ladder:
        issues.append(
            ReadinessIssue(
                ROW_TASK,
                SEVERITY_ERROR,
                "Capability ladder runs require a ladder rung — choose a "
                "Level rung in Task.",
            )
        )
    if not catalog.ladder_models:
        issues.append(
            ReadinessIssue(
                ROW_MODEL,
                SEVERITY_ERROR,
                "No qualified Ollama models available — the research "
                "operator roster is not installed.",
            )
        )
    elif catalog.ladder_model(config.model) is None:
        issues.append(
            ReadinessIssue(
                ROW_MODEL,
                SEVERITY_ERROR,
                "Choose a qualified Ollama Cloud model for ladder runs.",
            )
        )
    return issues, notes


def _curated_readiness(config: SessionConfig, catalog: SessionCatalog):
    issues = []
    notes = []
    task = catalog.find_task(config.task_id)
    if config.task_id is None:
        issues.append(
            ReadinessIssue(ROW_TASK, SEVERITY_ERROR, "Choose a task.")
        )
    elif task is None:
        issues.append(
            ReadinessIssue(
                ROW_TASK, SEVERITY_ERROR, "Selected task is not available."
            )
        )
    elif task.ladder:
        issues.append(
            ReadinessIssue(
                ROW_TASK,
                SEVERITY_ERROR,
                "Ladder rungs run under the Capability ladder target — "
                "switch Target or choose a curated task.",
            )
        )
    model = catalog.find_model(config.model)
    if not config.model.is_offline:
        if model is None:
            issues.append(
                ReadinessIssue(
                    ROW_MODEL,
                    SEVERITY_ERROR,
                    "Selected model is no longer offered — choose a model.",
                )
            )
        elif not model.available:
            issues.append(
                ReadinessIssue(
                    ROW_MODEL,
                    SEVERITY_ERROR,
                    model.unavailable_reason or "Selected model is unavailable.",
                )
            )
        if config.model.provider == PROVIDER_CONFIGURED and catalog.configured_error:
            issues.append(
                ReadinessIssue(
                    ROW_MODEL,
                    SEVERITY_ERROR,
                    f"Configuration error: {catalog.configured_error}",
                )
            )
    else:
        notes.append("Deterministic offline run — no model provider is contacted.")
    if not config.model.is_offline and config.model.provider == PROVIDER_CONFIGURED:
        notes.append(
            "Custom command profiles are trusted user configuration; "
            "network isolation is not enforced."
        )
    return issues, notes


def _local_readiness(config: SessionConfig, catalog: SessionCatalog, project: ProjectStatus):
    issues = []
    notes = []
    if project.state == "unchecked" or not project.ok:
        message = project.message or "Choose a clean Git repository to debug."
        issues.append(ReadinessIssue(ROW_PROJECT, SEVERITY_ERROR, message))
    if not config.bug_description.strip():
        issues.append(
            ReadinessIssue(ROW_BUG, SEVERITY_ERROR, "Describe the bug to debug.")
        )
    if config.model.is_offline:
        issues.append(
            ReadinessIssue(
                ROW_MODEL,
                SEVERITY_ERROR,
                "Select a live model.",
            )
        )
    else:
        model = catalog.find_model(config.model)
        if model is None:
            issues.append(
                ReadinessIssue(
                    ROW_MODEL,
                    SEVERITY_ERROR,
                    "Selected model is no longer offered — choose a model.",
                )
            )
        elif not model.available:
            issues.append(
                ReadinessIssue(
                    ROW_MODEL,
                    SEVERITY_ERROR,
                    model.unavailable_reason or "Selected model is unavailable.",
                )
            )
        if config.model.provider == PROVIDER_CONFIGURED:
            if catalog.configured_error:
                issues.append(
                    ReadinessIssue(
                        ROW_MODEL,
                        SEVERITY_ERROR,
                        f"Configuration error: {catalog.configured_error}",
                    )
                )
            else:
                notes.append(
                    "Custom command profiles are trusted user configuration; "
                    "network isolation is not enforced."
                )
    return issues, notes


def _run_label(config: SessionConfig) -> str:
    if config.target == TARGET_LOCAL_PROJECT:
        return "Start debugging"
    if config.target == TARGET_LADDER:
        return "Start session"
    if config.model.is_offline:
        return "Run evidence demo"
    return "Start session"


def derive_readiness(
    config: SessionConfig,
    catalog: SessionCatalog,
    project: ProjectStatus,
) -> SessionReadiness:
    """Derive the one authoritative readiness object for a configuration.

    Fail-closed by construction: any error-severity issue blocks ``ready``;
    warnings never do.  The status line always states the FIRST error, or
    the affirmative summary — the Run button, status line, and pre-flight
    rail render this same object, so they cannot disagree.
    """
    if config.target == TARGET_LADDER:
        issues, notes = _ladder_readiness(config, catalog)
    elif config.target == TARGET_LOCAL_PROJECT:
        issues, notes = _local_readiness(config, catalog, project)
    else:
        issues, notes = _curated_readiness(config, catalog)

    errors = [item for item in issues if item.severity == SEVERITY_ERROR]
    ready = not errors
    if ready:
        status_line = f"Ready — {_run_label(config).lower()}"
    else:
        status_line = f"Start unavailable — {errors[0].message}"
    return SessionReadiness(
        ready=ready,
        run_label=_run_label(config),
        status_line=status_line,
        issues=tuple(issues),
        rows=_row_states(config.target),
        notes=tuple(notes),
    )


__all__ = [
    "AUTO_RETRY_MAX",
    "ModelChoice",
    "ModelOption",
    "OFFLINE_CHOICE",
    "POLICY_LABELS",
    "POLICY_ON_UNCERTAINTY",
    "POLICY_STATIC_BASELINE",
    "PROVIDER_COMMANDCODE",
    "PROVIDER_CONFIGURED",
    "PROVIDER_LABELS",
    "PROVIDER_OFFLINE",
    "PROVIDER_OLLAMA",
    "PROVIDER_OPENCODE",
    "ProjectStatus",
    "ROW_AUTO_RETRY",
    "ROW_BUG",
    "ROW_DEBUGGER",
    "ROW_MODEL",
    "ROW_ORDER",
    "ROW_PROJECT",
    "ROW_REPRO",
    "ROW_TARGET",
    "ROW_TASK",
    "ROW_TIME_LIMIT",
    "ROW_VERIFY",
    "ReadinessIssue",
    "RowState",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "SessionCatalog",
    "SessionConfig",
    "SessionReadiness",
    "TARGET_CURATED",
    "TARGET_LABELS",
    "TARGET_LADDER",
    "TARGET_LOCAL_PROJECT",
    "derive_readiness",
    "model_compatibility",
]
