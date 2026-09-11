"""Model-gateway status, probe, and history coordination authority.

Single implementation authority beneath :class:`ModelGateway` for
observational provider facts:

* explicit provider reachability probing (:func:`probe_reachability`);
* vault-issued probe credential materialization (:func:`probe_credential`,
  the authoritative replacement for ``ModelGateway._probe_credential``);
* explicit catalog refresh (:func:`refresh_catalog`);
* durable-history inspection for runtime-success evidence
  (:func:`inspect_last_runtime_success`);
* provider status snapshot construction (:func:`get_provider_status`,
  :func:`list_provider_statuses`);
* model listing (:func:`list_models`).

Truthfulness contract (preserved exactly):

* configuration presence is not connection evidence;
* credential readiness is not connection evidence;
* static runnable/readiness is not live evidence;
* ``live_verified`` derives only from explicit live-probe evidence bound
  to the current provider runtime identity;
* runtime success is observational history from durable session/event
  evidence, never configuration truth.

Dependency direction (acyclic)::

    contracts <- resolution <- status <- model_gateway (facade)

This module owns no mutable gateway state and no credential authority.
It reads/writes explicit live-probe facts only through the passed
``gateway._live_probe_results`` mapping and credential facts only through
``gateway._vault``.  It never instantiates its own ``CredentialVault``.
Existing lazy imports (provider catalog refresh, history/journal/events,
model listing) are preserved inside function bodies.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from agentic_debugger.application.credential_vault import StaleCredentialBindingError
from agentic_debugger.application.model_gateway_contracts import (
    CatalogProbeError,
    CredentialUnavailableError,
    ModelBinding,
    ProviderConfigurationError,
    ProviderStatusSnapshot,
    provider_runtime_identity,
)
from agentic_debugger.application.provider_connections import (
    AUTH_NONE,
    PROTOCOL_CHAT_COMPLETIONS,
    TRANSPORT_GENERIC,
    ProviderCatalogSnapshot,
    ProviderConfig,
    ProviderConnectionError,
    get_provider_config,
    is_provider_quarantined,
    list_configured_providers,
    load_cached_catalog,
    test_provider_connection,
)

__all__ = [
    "get_provider_status",
    "inspect_last_runtime_success",
    "list_models",
    "list_provider_statuses",
    "probe_credential",
    "probe_reachability",
    "refresh_catalog",
]

def probe_credential(gateway, provider_id: str) -> Optional[str]:
    """Vault-issued credential for the trusted provider HTTP boundary.

    The ONLY normal product materialization path for the in-process
    provider HTTP operations (probe / catalog refresh): safe binding
    -> one lease -> ``lease.reveal()`` HERE and only here.  Returns
    ``None`` intentionally for no-auth providers; raises typed vault
    errors (credential-free) when a required authority is missing,
    quarantined, stale, or otherwise unavailable.
    """
    cfg = get_provider_config(provider_id)
    if cfg is None or cfg.auth_mode == AUTH_NONE:
        return None
    binding = gateway._vault.safe_binding(provider_id)
    if binding is None:
        readiness = gateway._vault.readiness(provider_id)
        raise CredentialUnavailableError(
            f"Credential unavailable for provider {provider_id!r}: "
            + (readiness.reason or "no usable credential source")
        )
    lease = gateway._vault.resolve_lease(binding)
    return lease.reveal() if lease is not None else None


def probe_reachability(
    gateway,
    provider_id: str,
    *,
    model_id: Optional[str] = None,
    timeout_seconds: float = 10.0,
    engine: Optional[str] = None,
) -> Dict[str, Any]:
    """Perform an explicit reachability probe of the provider endpoint.

    V2-04: the probe credential is materialized through the
    CredentialVault (safe binding -> one lease -> explicit credential
    at this trusted HTTP boundary).  A missing/quarantined/stale
    credential authority fails safely BEFORE any HTTP attempt; the
    low-level probe never rediscovers a credential for this product
    path.
    """
    cfg = get_provider_config(provider_id)
    current_identity = provider_runtime_identity(cfg) if cfg is not None else None
    now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        explicit_credential = gateway._probe_credential(provider_id)
    except (CredentialUnavailableError, StaleCredentialBindingError) as exc:
        gateway._live_probe_results[provider_id] = {
            "verified": False,
            "timestamp": now_utc,
            "error": str(exc),
            "runtime_identity": current_identity,
        }
        return {
            "ok": False,
            "connected": False,
            "reachable": False,
            "reason": str(exc),
            "error": str(exc),
            "verified_at_utc": None,
            "timestamp": now_utc,
            "endpoint": None,
        }
    try:
        result = test_provider_connection(
            provider_id,
            model_id=model_id,
            timeout_seconds=timeout_seconds,
            engine=engine,
            credential=explicit_credential,
        )
    except ProviderConnectionError as exc:
        gateway._live_probe_results[provider_id] = {
            "verified": False,
            "timestamp": now_utc,
            "error": str(exc),
            "runtime_identity": current_identity,
        }
        return {
            "ok": False,
            "connected": False,
            "reachable": False,
            "reason": str(exc),
            "error": str(exc),
            "verified_at_utc": None,
            "timestamp": now_utc,
            "endpoint": None,
        }

    ok = bool(result.get("ok", False))
    reason = str(result.get("reason") or "")
    if ok:
        gateway._live_probe_results[provider_id] = {
            "verified": True,
            "timestamp": now_utc,
            "error": None,
            "runtime_identity": current_identity,
        }
        return {
            "ok": True,
            "connected": True,
            "reachable": True,
            "reason": reason,
            "error": None,
            "verified_at_utc": now_utc,
            "timestamp": now_utc,
            "endpoint": result.get("endpoint"),
            "model_count": result.get("model_count", 0),
        }
    else:
        gateway._live_probe_results[provider_id] = {
            "verified": False,
            "timestamp": now_utc,
            "error": reason,
            "runtime_identity": current_identity,
        }
        return {
            "ok": False,
            "connected": False,
            "reachable": False,
            "reason": reason,
            "error": reason,
            "verified_at_utc": None,
            "timestamp": now_utc,
            "endpoint": result.get("endpoint"),
        }


def refresh_catalog(gateway, provider_id: str) -> ProviderCatalogSnapshot:
    """Explicitly refresh the provider's live model catalog (GET /models).

    V2-04: the catalog credential is materialized through the
    CredentialVault (safe binding -> one lease -> explicit credential
    at this trusted HTTP boundary).  A missing/quarantined/stale
    credential authority fails safely BEFORE any HTTP attempt; the
    low-level refresh never rediscovers a credential for this product
    path.
    """
    cfg = get_provider_config(provider_id)
    current_identity = provider_runtime_identity(cfg) if cfg is not None else None
    now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        explicit_credential = gateway._probe_credential(provider_id)
    except (CredentialUnavailableError, StaleCredentialBindingError) as exc:
        gateway._live_probe_results[provider_id] = {
            "verified": False,
            "timestamp": now_utc,
            "error": str(exc),
            "runtime_identity": current_identity,
        }
        raise CatalogProbeError(str(exc)) from exc
    try:
        from agentic_debugger.application import provider_connections as pc

        snapshot = pc.refresh_provider_catalog(
            provider_id, credential=explicit_credential
        )
    except ProviderConnectionError as exc:
        gateway._live_probe_results[provider_id] = {
            "verified": False,
            "timestamp": now_utc,
            "error": str(exc),
            "runtime_identity": current_identity,
        }
        raise CatalogProbeError(str(exc)) from exc

    # Successful catalog refresh is reachability evidence for tested identity
    gateway._live_probe_results[provider_id] = {
        "verified": True,
        "timestamp": now_utc,
        "error": None,
        "runtime_identity": current_identity,
    }
    return snapshot


def inspect_last_runtime_success(
    provider_id_or_history_root: Any,
    second_arg: Any = None,
    model_id: Optional[str] = None,
    *,
    history_root: Optional[Path | str] = None,
    sessions_root: Optional[Path | str] = None,
    target_config: Optional[ProviderConfig] = None,
    target_binding: Optional[ModelBinding] = None,
) -> Optional[str]:
    """Derive the timestamp of the last successful model request from durable history.

    Pure observational derivation: inspects authoritative session history
    without mutating durable provider configuration. Bounded to scanning
    recent completed sessions. Requires journal provenance to match the
    current target provider runtime identity; unbound historical runs do
    not verify the current configuration. Returns ISO UTC timestamp or None.
    """
    effective_root = history_root or sessions_root
    if second_arg is not None:
        # Called as inspect_last_runtime_success(history_root, provider_id)
        # or inspect_last_runtime_success(provider_id, model_id, history_root=...)
        if isinstance(provider_id_or_history_root, (Path, str)) and Path(str(provider_id_or_history_root)).is_dir():
            effective_root = effective_root or provider_id_or_history_root
            provider_id = str(second_arg)
        else:
            provider_id = str(provider_id_or_history_root)
            model_id = str(second_arg)
    else:
        provider_id = str(provider_id_or_history_root)

    if not effective_root:
        return None
    root_path = Path(effective_root)
    if not root_path.is_dir():
        return None

    effective_target_cfg = target_config
    if effective_target_cfg is None and provider_id:
        effective_target_cfg = get_provider_config(provider_id)

    from agentic_debugger.application.history import JOURNAL_FILE_NAME
    from agentic_debugger.application.events import ModelRequestStatus, SessionEventKind
    from agentic_debugger.application.journal import read_session_journal

    def _find_journal(s_dir: Path) -> Optional[Path]:
        for name in (JOURNAL_FILE_NAME, "journal.jsonl", "events.jsonl", "session.jsonl"):
            p = s_dir / name
            if p.is_file():
                return p
        return None

    # Collect session dirs sorted by modification time (newest first), bounded to 30
    try:
        candidates = [
            p for p in root_path.iterdir()
            if p.is_dir() and _find_journal(p) is not None
        ]
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        candidates = candidates[:30]
    except OSError:
        return None

    for s_dir in candidates:
        journal_path = _find_journal(s_dir)
        if journal_path is None:
            continue
        session_matches_provider = False
        last_request_success_at: Optional[str] = None

        try:
            journal = read_session_journal(journal_path)
            events = journal.events
        except Exception:
            events = ()

        if events:
            for ev in events:
                if ev.event_kind is SessionEventKind.MODEL_CONFIGURED:
                    session_matches_provider = False
                    p_prov = ev.payload.get("provider")
                    p_model = ev.payload.get("profile_id")
                    p_api_model = ev.payload.get("provider_model_id")
                    p_endpoint = (ev.payload.get("endpoint") or "").strip().rstrip("/")
                    p_auth = ev.payload.get("auth_mode")
                    p_contract = ev.payload.get("endpoint_contract") or ev.payload.get("transport_profile")
                    p_format = ev.payload.get("api_format")
                    p_runtime_id = ev.payload.get("provider_runtime_identity")
                    p_fp = ev.payload.get("config_fingerprint")
                    p_binding_fp = ev.payload.get("model_binding_fingerprint")

                    if p_prov != provider_id:
                        continue
                    if model_id is not None and not (p_model == model_id or p_api_model == model_id):
                        continue

                    if target_binding is not None:
                        if p_binding_fp:
                            session_matches_provider = (p_binding_fp == target_binding.fingerprint())
                        else:
                            session_matches_provider = False
                    elif effective_target_cfg is not None:
                        cur_endpoint = (effective_target_cfg.base_url or "").strip().rstrip("/")
                        cur_contract = effective_target_cfg.transport_profile or TRANSPORT_GENERIC
                        cur_auth = effective_target_cfg.auth_mode
                        cur_format = effective_target_cfg.api_format
                        cur_runtime_id = provider_runtime_identity(effective_target_cfg)

                        if p_runtime_id:
                            session_matches_provider = (p_runtime_id == cur_runtime_id)
                        else:
                            if not (p_endpoint and p_auth and p_contract and p_format):
                                session_matches_provider = False
                            else:
                                session_matches_provider = (
                                    p_endpoint == cur_endpoint
                                    and p_auth == cur_auth
                                    and p_contract == cur_contract
                                    and p_format == cur_format
                                )
                    else:
                        session_matches_provider = False

                elif ev.event_kind is SessionEventKind.MODEL_REQUEST_COMPLETED:
                    if session_matches_provider:
                        status_val = ev.payload.get("status")
                        if status_val == ModelRequestStatus.OK.value or status_val == "ok":
                            last_request_success_at = ev.timestamp_utc
        elif journal_path.is_file():
            try:
                with open(journal_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        raw = json.loads(line)
                        kind = raw.get("kind") or raw.get("event_kind")
                        if kind in ("model.configured", "model_configured"):
                            session_matches_provider = False
                            p_prov = raw.get("provider") or raw.get("payload", {}).get("provider")
                            p_model = raw.get("model") or raw.get("profile_id") or raw.get("payload", {}).get("profile_id")
                            p_api_model = raw.get("provider_model_id") or raw.get("payload", {}).get("provider_model_id")
                            p_endpoint = (raw.get("endpoint") or raw.get("payload", {}).get("endpoint") or "").strip().rstrip("/")
                            p_auth = raw.get("auth_mode") or raw.get("payload", {}).get("auth_mode")
                            p_contract = (
                                raw.get("endpoint_contract")
                                or raw.get("transport_profile")
                                or raw.get("payload", {}).get("endpoint_contract")
                                or raw.get("payload", {}).get("transport_profile")
                            )
                            p_format = raw.get("api_format") or raw.get("payload", {}).get("api_format")
                            p_runtime_id = (
                                raw.get("provider_runtime_identity")
                                or raw.get("payload", {}).get("provider_runtime_identity")
                            )
                            p_fp = raw.get("config_fingerprint") or raw.get("payload", {}).get("config_fingerprint")
                            p_binding_fp = (
                                raw.get("model_binding_fingerprint")
                                or raw.get("payload", {}).get("model_binding_fingerprint")
                            )

                            if p_prov != provider_id:
                                continue
                            if model_id is not None and not (p_model == model_id or p_api_model == model_id):
                                continue

                            if target_binding is not None:
                                if p_binding_fp:
                                    session_matches_provider = (p_binding_fp == target_binding.fingerprint())
                                else:
                                    session_matches_provider = False
                            elif effective_target_cfg is not None:
                                cur_endpoint = (effective_target_cfg.base_url or "").strip().rstrip("/")
                                cur_contract = effective_target_cfg.transport_profile or TRANSPORT_GENERIC
                                cur_auth = effective_target_cfg.auth_mode
                                cur_format = effective_target_cfg.api_format
                                cur_runtime_id = provider_runtime_identity(effective_target_cfg)

                                if p_runtime_id:
                                    session_matches_provider = (p_runtime_id == cur_runtime_id)
                                else:
                                    if not (p_endpoint and p_auth and p_contract and p_format):
                                        session_matches_provider = False
                                    else:
                                        session_matches_provider = (
                                            p_endpoint == cur_endpoint
                                            and p_auth == cur_auth
                                            and p_contract == cur_contract
                                            and p_format == cur_format
                                        )
                            else:
                                session_matches_provider = False

                        elif kind in ("llm.request", "model_request_completed", "step.action"):
                            if session_matches_provider:
                                st_val = raw.get("status") or raw.get("payload", {}).get("status")
                                if st_val in ("ok", "success"):
                                    last_request_success_at = (
                                        raw.get("timestamp")
                                        or raw.get("timestamp_utc")
                                        or raw.get("payload", {}).get("timestamp")
                                    )
            except Exception:
                pass

        if last_request_success_at:
            return last_request_success_at

    return None


def get_provider_status(
    gateway,
    provider_id: str,
    *,
    history_root: Optional[Path | str] = None,
    sessions_root: Optional[Path | str] = None,
) -> ProviderStatusSnapshot:
    """Produce a truthful ProviderStatusSnapshot with distinct factual dimensions."""
    cfg = get_provider_config(provider_id)
    if cfg is None:
        raise ProviderConfigurationError(f"Provider {provider_id!r} is not configured")

    is_enabled = bool(cfg.enabled)
    is_quarantined = is_provider_quarantined(provider_id)
    is_configured = True

    vault_readiness = gateway._vault.readiness(provider_id)
    credential_ready = vault_readiness.credential_ready
    cred_source = vault_readiness.source_kind

    # Provider-level static readiness (via gateway to preserve
    # ModelGateway dispatch/monkeypatch semantics).
    is_ready, readiness_reason = gateway.provider_readiness(provider_id)

    # Catalog state
    cat_snapshot = load_cached_catalog(provider_id)
    if cat_snapshot is not None and cat_snapshot.models:
        cat_count = len(cat_snapshot.models)
        cat_refreshed = cat_snapshot.fetched_at_utc
        cat_source = cat_snapshot.source
        try:
            fetched = datetime.fromisoformat(
                cat_snapshot.fetched_at_utc.replace("Z", "+00:00")
            )
            from datetime import timedelta
            cat_stale = datetime.now(timezone.utc) - fetched > timedelta(days=7)
        except ValueError:
            cat_stale = False
        cached_models = cat_snapshot.models
    elif cfg.models:
        cat_count = len(cfg.models)
        cat_refreshed = cfg.last_refresh_utc
        cat_source = cfg.last_refresh_source
        cat_stale = False
        cached_models = cfg.models
    else:
        cat_count = 0
        cat_refreshed = None
        cat_source = None
        cat_stale = False
        cached_models = ()

    # Probe state bound strictly to current runtime identity
    current_identity = provider_runtime_identity(cfg)
    probe_record = gateway._live_probe_results.get(provider_id)
    live_verified = False
    live_verified_at = None
    live_probe_error = None
    if probe_record is not None and probe_record.get("runtime_identity") == current_identity:
        live_verified = bool(probe_record.get("verified", False))
        live_verified_at = probe_record.get("timestamp")
        live_probe_error = probe_record.get("error")

    # Runtime success fact derived from history (bound to current target_config).
    # Via gateway to preserve ModelGateway dispatch/monkeypatch semantics.
    effective_history_root = history_root or sessions_root
    runtime_succeeded_at = gateway.inspect_last_runtime_success(
        provider_id, history_root=effective_history_root, target_config=cfg
    )

    return ProviderStatusSnapshot(
        provider_id=provider_id,
        label=cfg.name,
        base_url=cfg.base_url,
        endpoint_contract=cfg.transport_profile,
        auth_mode=cfg.auth_mode,
        api_format=cfg.api_format,
        is_configured=is_configured,
        is_enabled=is_enabled,
        is_quarantined=is_quarantined,
        credential_ready=credential_ready,
        credential_source=cred_source,
        is_provider_ready=is_ready,
        provider_readiness_reason=readiness_reason,
        is_runnable=is_ready,
        runnable_reason=readiness_reason,
        catalog_model_count=cat_count,
        catalog_refreshed_at_utc=cat_refreshed,
        catalog_refreshed_source=cat_source,
        catalog_stale=cat_stale,
        live_verified=live_verified,
        live_verified_at_utc=live_verified_at,
        live_probe_error=live_probe_error,
        runtime_succeeded_at_utc=runtime_succeeded_at,
        cached_models=cached_models,
    )


def list_provider_statuses(
    gateway, *, history_root: Optional[Path | str] = None
) -> List[ProviderStatusSnapshot]:
    """Return truthful status snapshots for all configured providers."""
    configs = list_configured_providers()
    statuses = []
    for c in configs:
        try:
            statuses.append(gateway.get_provider_status(c.provider_id, history_root=history_root))
        except Exception as exc:
            err_msg = f"Status evaluation error: {type(exc).__name__}"
            statuses.append(
                ProviderStatusSnapshot(
                    provider_id=c.provider_id,
                    label=c.name or c.provider_id,
                    base_url=c.base_url or "",
                    endpoint_contract=c.transport_profile or TRANSPORT_GENERIC,
                    auth_mode=c.auth_mode or "none",
                    api_format=c.api_format or PROTOCOL_CHAT_COMPLETIONS,
                    is_configured=True,
                    is_enabled=bool(c.enabled),
                    is_quarantined=is_provider_quarantined(c.provider_id),
                    credential_ready=False,
                    credential_source=None,
                    is_provider_ready=False,
                    provider_readiness_reason=err_msg,
                    is_runnable=False,
                    runnable_reason=err_msg,
                    catalog_model_count=0,
                    catalog_refreshed_at_utc=None,
                    catalog_refreshed_source=None,
                    catalog_stale=False,
                    catalog_error=err_msg,
                    live_verified=False,
                    live_verified_at_utc=None,
                    live_probe_error=None,
                    runtime_succeeded_at_utc=None,
                    cached_models=(),
                )
            )
    return statuses


def list_models(*, include_ollama: bool = True) -> List[Any]:
    """Return selectable models offered across configured providers."""
    from agentic_debugger.application.model_providers import list_provider_models
    return list_provider_models(include_ollama=include_ollama)
