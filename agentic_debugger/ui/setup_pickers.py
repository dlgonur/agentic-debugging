"""Session-setup catalog gathering and choice pickers.

This module owns the catalog/model gathering and the choice-picker
construction family for
:class:`~agentic_debugger.ui.screens_setup.StartSessionScreen`,
operating on the SCREEN passed explicitly: target/task/model/
debugger/auto-retry pickers and the provider model catalog gathering.
The setup screen remains the one session-configuration authority;
``list_provider_models`` is resolved through the setup module at call
time so its monkeypatch seam keeps working.
"""

from __future__ import annotations

from typing import Optional

from agentic_debugger.application.level32 import (
    LEVEL32_TASK_ID,
    is_ladder_task,
    ladder_task_metadata,
)
from agentic_debugger.application.model_providers import format_model_display_name
from agentic_debugger.ui.screens_editors import ChoiceOption, ChoicePickerScreen
from agentic_debugger.ui.session_config import (
    AUTO_RETRY_MAX,
    OFFLINE_CHOICE,
    POLICY_LABELS,
    POLICY_ON_UNCERTAINTY,
    POLICY_STATIC_BASELINE,
    PROVIDER_CONFIGURED,
    PROVIDER_OFFLINE,
    PROVIDER_OLLAMA,
    ROW_AUTO_RETRY,
    ROW_DEBUGGER,
    ROW_MODEL,
    ROW_TARGET,
    ROW_TASK,
    TARGET_CURATED,
    TARGET_LABELS,
    TARGET_LADDER,
    TARGET_LOCAL_PROJECT,
    ModelChoice,
    ModelOption,
    SessionCatalog,
    TaskOption,
    model_compatibility,
)
from agentic_debugger.ui.setup_display import (
    _provider_label,
    _short_unavailable_reason,
)


def _setup_list_provider_models(*args, **kwargs):
    """Resolve through the setup module so its monkeypatch seam holds."""
    from agentic_debugger.ui import screens_setup

    return screens_setup.list_provider_models(*args, **kwargs)

def gather_catalog(screen) -> None:
    """Rebuild the read-only environment catalog (offline, no provider
    contact, no mutation)."""
    tasks: list[TaskOption] = []
    for label, task_id in screen._task_options:
        title = label.split("·", 1)[0].strip() or task_id
        ladder = is_ladder_task(task_id)
        detail = ""
        if ladder:
            meta = ladder_task_metadata(task_id)
            detail = f"{meta.treatment} · {meta.evaluation}"
        tasks.append(TaskOption(task_id, title, ladder=ladder, detail=detail))

    models: list[ModelOption] = [
        ModelOption(
            PROVIDER_OFFLINE,
            "",
            "Offline",
            detail="",
        )
    ]
    provider_reasons: dict[str, Optional[str]] = {}
    provider_registry_error: Optional[str] = None
    try:
        for item in _setup_list_provider_models(include_ollama=True):
            if not item.available and item.provider_label not in provider_reasons:
                provider_reasons.setdefault(
                    item.kind, item.unavailable_reason or "provider unavailable"
                )
            models.append(
                ModelOption(
                    item.kind,
                    item.model_id,
                    item.display_name,
                    detail=item.note or "",
                    available=item.available,
                    unavailable_reason=item.unavailable_reason,
                    protocol=getattr(item, "protocol", None),
                    provider_label=item.provider_label,
                )
            )
    except Exception as exc:
        # Fail-closed: a corrupt provider registry must never look like
        # a healthy fresh install.  Surface a disabled, credential-safe
        # configuration-error entry instead of an empty state.
        from agentic_debugger.application.model_providers import ProviderRegistryError

        if isinstance(exc, ProviderRegistryError):
            provider_registry_error = str(exc)[:160]
        else:
            provider_registry_error = "provider configuration error"
        models.append(
            ModelOption(
                "__provider_registry_error__",
                "",
                "Configuration Error",
                detail="",
                available=False,
                unavailable_reason=provider_registry_error,
            )
        )

    configured_error: Optional[str] = None
    try:
        summaries, configured_error = screen.app.configured_profiles()
    except Exception as exc:  # pragma: no cover - bounded diagnostics
        summaries, configured_error = (), str(exc)
    for profile in summaries:
        models.append(
            ModelOption(
                PROVIDER_CONFIGURED,
                profile.profile_id,
                profile.display_name,
                detail="",
            )
        )

    ladder_models: list[ModelOption] = []
    try:
        for item in screen.app.ollama_cloud_model_profiles():
            ladder_models.append(
                ModelOption(
                    PROVIDER_OLLAMA,
                    item.alias,
                    item.display_name,
                    detail="",
                )
            )
    except Exception:
        pass

    screen._catalog = SessionCatalog(
        tasks=tuple(tasks),
        models=tuple(models),
        ladder_models=tuple(ladder_models),
        configured_error=configured_error,
    )

# -- project validation ---------------------------------------------------


def model_choice_key(screen, choice: ModelChoice) -> str:
    return f"{choice.provider}:{choice.model_id}"


def open_target_picker(screen) -> None:
    descriptions = {
        TARGET_CURATED: "Reproducible in-repo fixture; offline or any provider.",
        TARGET_LOCAL_PROJECT: "Your clean Git repository; describe the bug.",
        TARGET_LADDER: "Levels 6/12/18: configured provider models allowed; Level 32: frozen qualified treatment.",
    }
    choices = [
        ChoiceOption(
            target,
            TARGET_LABELS[target],
            descriptions[target],
        )
        for target in (TARGET_CURATED, TARGET_LOCAL_PROJECT, TARGET_LADDER)
    ]
    screen.app.push_screen(
        ChoicePickerScreen(
            title="Debug what?",
            choices=choices,
            current=screen._config.target,
            on_select=lambda value: screen._choice_selected(ROW_TARGET, value),
        )
    )


def open_task_picker(screen) -> None:
    target = screen._config.target
    choices: list[ChoiceOption] = []
    for task in screen._catalog.tasks:
        if task.ladder:
            disabled = target != TARGET_LADDER
            reason = (
                "" if not disabled else "runs under the Capability ladder target"
            )
            group = "CAPABILITY LADDER"
        else:
            disabled = target == TARGET_LADDER
            reason = "" if not disabled else "ladder runs use Level rungs"
            group = "CURATED TASKS"
        choices.append(
            ChoiceOption(
                task.task_id,
                task.title,
                task.detail,
                secondary=task.task_id,
                group=group,
                disabled=disabled,
                disabled_reason=reason,
            )
        )
    screen.app.push_screen(
        ChoicePickerScreen(
            title="Select task",
            choices=choices,
            current=screen._config.task_id,
            on_select=lambda value: screen._choice_selected(ROW_TASK, value),
        )
    )


def open_model_picker(screen) -> None:
    """Model browser v2: searchable, provider-aware, readiness-explicit."""
    from agentic_debugger.ui.model_browser import ModelBrowserScreen

    screen._gather_catalog()
    target = screen._config.target

    browser_options: list[ModelOption] = []
    # Offline entry first (compatibility-gated like before).
    offline_ok, offline_reason = model_compatibility(
        target, ModelOption(PROVIDER_OFFLINE, "", "Offline")
    )
    browser_options.append(
        ModelOption(
            PROVIDER_OFFLINE,
            "",
            "Offline",
            detail="" if offline_ok else (offline_reason or ""),
            available=offline_ok,
            unavailable_reason=None if offline_ok else offline_reason,
            protocol=None,
            provider_label="Offline",
        )
    )

    # One stable provider world for every target (same merge as before):
    # configured catalog + qualified ladder roster (deduped).
    seen_keys: set[tuple[str, str]] = {(PROVIDER_OFFLINE, "")}
    merged: list[ModelOption] = []
    for option in list(screen._catalog.models) + list(screen._catalog.ladder_models):
        if option.provider == PROVIDER_OFFLINE:
            continue
        key = (option.provider, option.model_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        merged.append(option)

    try:
        from agentic_debugger.application.provider_connections import (
            list_configured_providers as _list_cfgs,
        )

        _provider_labels = {c.provider_id: c.name for c in _list_cfgs() if c.enabled}
    except Exception:
        _provider_labels = {}

    for option in merged:
        qualified = screen._catalog.ladder_model(option.choice)
        effective = qualified or option
        is_level32 = target == TARGET_LADDER and screen._config.task_id == LEVEL32_TASK_ID
        compatible, compat_reason = model_compatibility(
            target,
            effective,
            ladder_qualified=qualified is not None,
        )
        effective_available = bool(effective.available) and compatible
        if not compatible:
            effective_reason: Optional[str] = compat_reason
        else:
            effective_reason = effective.unavailable_reason
        _eff_label = getattr(effective, "provider_label", None)
        if type(_eff_label) is str and _eff_label.strip():
            label = _eff_label.strip()
        else:
            label = _provider_labels.get(effective.provider, effective.provider)
        # Level-32 qualification note stays visible without blocking
        # execution (qualification controls classification, never
        # eligibility).
        detail = effective.detail or ""
        if is_level32 and qualified is None and bool(effective.available) and effective.provider != PROVIDER_OFFLINE:
            extra = "not qualified for frozen Level-32 comparison"
            detail = f"{detail} · {extra}" if detail else extra
        if getattr(option, "provider", "") == PROVIDER_CONFIGURED:
            display_name = effective.display or effective.model_id
        else:
            display_name = format_model_display_name(effective.display or effective.model_id)
        browser_options.append(
            ModelOption(
                effective.provider,
                effective.model_id,
                display_name,
                detail=detail,
                available=effective_available,
                unavailable_reason=effective_reason,
                protocol=getattr(effective, "protocol", getattr(option, "protocol", None)),
                provider_label=label,
            )
        )

    current_key = screen._model_choice_key(screen._config.model)
    screen.app.push_screen(
        ModelBrowserScreen(
            browser_options,
            current_key=current_key,
            on_select=lambda value: screen._choice_selected(ROW_MODEL, value),
            title="Select model",
        )
    )


def open_debugger_picker(screen) -> None:
    choices = [
        ChoiceOption(
            POLICY_ON_UNCERTAINTY,
            POLICY_LABELS[POLICY_ON_UNCERTAINTY],
            "Attach PDB when runtime evidence is useful.",
        ),
        ChoiceOption(
            POLICY_STATIC_BASELINE,
            POLICY_LABELS[POLICY_STATIC_BASELINE],
            "Static reasoning only; no debugger session.",
        ),
    ]
    screen.app.push_screen(
        ChoicePickerScreen(
            title="Select debugger policy",
            choices=choices,
            current=screen._config.debugger_policy,
            on_select=lambda value: screen._choice_selected(ROW_DEBUGGER, value),
        )
    )


def open_auto_retry_picker(screen) -> None:
    choices = [
        ChoiceOption("0", "No auto-retry", "fail fast; retry manually with r"),
        ChoiceOption("1", "1 auto-retry", "one fresh attempt on retryable failure"),
        ChoiceOption("2", "2 auto-retries", "two fresh attempts"),
        ChoiceOption("3", "3 auto-retries", "maximum"),
    ]
    screen.app.push_screen(
        ChoicePickerScreen(
            title="Auto-retry on failure",
            choices=choices,
            current=str(screen._config.auto_retries),
            on_select=lambda value: screen._choice_selected(ROW_AUTO_RETRY, value),
        )
    )
