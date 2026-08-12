"""Reusable efficient-SDPA attention backend for the cp118 Windows/Qwen2.5
local inference optimization.

This package turns the *already-proven* Windows + Qwen2.5-Coder-7B workaround
into a small, fail-closed, reusable helper.  It does **not** redesign the
workaround; it packages it.

The proven workaround
---------------------

Qwen2.5-Coder-7B has GQA geometry ``28 query heads / 4 KV heads``
(``num_key_value_groups = 7``).  The native ``EFFICIENT_ATTENTION`` SDPA
backend rejects unequal Q/K/V head counts, so the stock Transformers ``sdpa``
path falls back to the slow ``MATH`` backend on this Windows torch build where
fused Flash SDP is unavailable (``torch.backends.cuda.is_flash_attention_available()
== False``).

The workaround expands the KV cache explicitly using the existing Transformers
``repeat_kv`` semantics (``4 KV heads -> 28 heads``) and then forces
``SDPBackend.EFFICIENT_ATTENTION`` via ``torch.nn.attention.sdpa_kernel``.

How it is wired in
------------------

Transformers 4.57.3 exposes a functional attention interface:

* ``ALL_ATTENTION_FUNCTIONS`` (``transformers.modeling_utils``) maps an
  ``attn_implementation`` key to a ``forward(module, query, key, value,
  attention_mask, dropout, scaling, is_causal=None, **kwargs)`` callable;
  Qwen2's ``Qwen2Attention`` dispatches through it.
* ``ALL_MASKING_FUNCTIONS`` (``transformers.masking_utils``) maps the same key
  to a mask builder.  We bind the *existing* ``sdpa_mask`` so the mask behavior
  is byte-for-byte identical to stock ``sdpa`` — only the SDPA backend changes.

``register_efficient_sdpa()`` registers the key ``"efficient_sdpa"`` in both
registries (idempotent + conflict-guarded).  The model is then loaded with
``attn_implementation="efficient_sdpa"``.

Compatibility contract
----------------------

This module is **import-tolerant**: it imports no heavy ML libraries at module
import time.  All torch/transformers imports happen lazily inside
``register_efficient_sdpa()`` / ``efficient_sdpa_forward``.

* Transformers 4.57.3 is the *tested/supported* integration version.  We do
  **not** claim generic ``>=4.57`` compatibility because this code relies on
  internal integration symbols (``AttentionInterface``, ``sdpa_mask``,
  ``repeat_kv``, ``ALL_ATTENTION_FUNCTIONS``) whose behavior may drift.  An
  unsupported Transformers version fails closed unless every required symbol and
  behavioral assumption is explicitly proven at registration time.
* Torch is capability-gated, not version-pinned: registration requires CUDA to
  be available, ``SDPBackend.EFFICIENT_ATTENTION`` to be present, the
  ``sdpa_kernel`` context manager to be importable, and a one-shot backend probe
  to succeed.  ``MATH`` is never silently substituted for the requested
  optimized backend.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

#: The ``attn_implementation`` key this package registers.
EFFICIENT_SDPA_KEY = "efficient_sdpa"

#: Transformers version explicitly tested/supported by this package.
SUPPORTED_TRANSFORMERS_VERSION = "4.57.3"

#: Stable sentinel tagged onto every forward closure produced by this module
#: so the conflict guard can recognize our own implementation across repeated
#: registrations (which may build new closures) and treat them as idempotent
#: rather than as a foreign binding.
_EFFICIENT_SDPA_MARKER = object()


class EfficientSdpaError(RuntimeError):
    """Raised on any efficient-SDPA registration/application failure.

    Fail-closed is the contract: the optimized backend is never silently
    replaced by the pathological ``MATH`` backend.  An unavailable capability
    surfaces as this error.
    """


def validate_transformers_version(actual: str) -> None:
    """Fail closed unless ``actual == SUPPORTED_TRANSFORMERS_VERSION``.

    This package relies on *internal* Transformers integration symbols
    (``AttentionInterface``, ``ALL_ATTENTION_FUNCTIONS``,
    ``AttentionMaskInterface``, ``ALL_MASK_ATTENTION_FUNCTIONS``,
    ``sdpa_mask``, ``repeat_kv``, ``sdpa_attention_forward``) whose behavior may
    drift across releases.  For v1 the integration is explicitly version-bound
    to the tested/supported version; we do **not** claim compatibility with any
    other version.  A mismatch raises :class:`EfficientSdpaError` reporting the
    actual version, the supported version, and the reason.

    This is a small pure helper so it can be unit-tested without installing
    another Transformers version.
    """

    if actual != SUPPORTED_TRANSFORMERS_VERSION:
        raise EfficientSdpaError(
            f"unsupported transformers version: actual={actual!r}, "
            f"supported={SUPPORTED_TRANSFORMERS_VERSION!r}. "
            "efficient_sdpa is intentionally version-bound for v1 because it "
            "relies on internal Transformers attention-interface APIs "
            "(AttentionInterface, ALL_ATTENTION_FUNCTIONS, "
            "AttentionMaskInterface, ALL_MASK_ATTENTION_FUNCTIONS, sdpa_mask, "
            "repeat_kv, sdpa_attention_forward) whose behavior may drift; "
            "do not assume compatibility with other versions."
        )


# ---------------------------------------------------------------------------
# Registration state (introspection only; not relied on for correctness).
# ---------------------------------------------------------------------------

_REGISTRATION: Optional[Dict[str, Any]] = None


def _lazy_imports() -> Dict[str, Any]:
    """Lazily import the torch + transformers symbols this package needs.

    Importing this module never imports torch/transformers.  All heavy imports
    happen here, inside registration/application, so the package stays
    import-tolerant in a torch-less Python environment.
    """

    try:
        import torch
        from torch.nn.attention import SDPBackend, sdpa_kernel
    except Exception as exc:  # noqa: BLE001 - aggregate import failure
        raise EfficientSdpaError(
            f"efficient_sdpa requires torch with torch.nn.attention.sdpa_kernel: {exc}"
        ) from exc

    # Capability gates (not version pins).
    if not torch.cuda.is_available():
        raise EfficientSdpaError(
            "efficient_sdpa requires CUDA available for optimized execution; "
            "torch.cuda.is_available() is False."
        )
    if not hasattr(SDPBackend, "EFFICIENT_ATTENTION"):
        raise EfficientSdpaError(
            "installed torch lacks SDPBackend.EFFICIENT_ATTENTION."
        )
    if sdpa_kernel is None or not callable(sdpa_kernel):
        raise EfficientSdpaError("torch.nn.attention.sdpa_kernel is unavailable.")

    # Transformers version gate (BLOCKER 1): the integration is intentionally
    # version-bound for v1. Enforce the exact tested/supported version before
    # touching any internal attention-interface symbols.  Lazy import so the
    # package stays import-tolerant in a torch-less environment.
    try:
        import transformers as _transformers
    except Exception as exc:  # noqa: BLE001
        raise EfficientSdpaError(
            f"efficient_sdpa requires transformers: {exc}"
        ) from exc
    validate_transformers_version(getattr(_transformers, "__version__", "UNKNOWN"))

    # transformers integration symbols.
    try:
        from transformers.integrations.sdpa_attention import (
            repeat_kv as _tf_repeat_kv,
            sdpa_attention_forward as _tf_sdpa_forward,
        )
        from transformers.masking_utils import (
            ALL_MASK_ATTENTION_FUNCTIONS,
            AttentionMaskInterface,
            sdpa_mask,
        )
        from transformers.modeling_utils import (
            ALL_ATTENTION_FUNCTIONS,
            AttentionInterface,
        )
    except Exception as exc:  # noqa: BLE001
        raise EfficientSdpaError(
            "efficient_sdpa requires transformers attention-interface symbols "
            f"(AttentionInterface, ALL_ATTENTION_FUNCTIONS, AttentionMaskInterface, "
            f"ALL_MASK_ATTENTION_FUNCTIONS, sdpa_mask, repeat_kv, sdpa_attention_forward): {exc}"
        ) from exc

    return {
        "torch": torch,
        "SDPBackend": SDPBackend,
        "sdpa_kernel": sdpa_kernel,
        "repeat_kv": _tf_repeat_kv,
        "sdpa_attention_forward": _tf_sdpa_forward,
        "ALL_ATTENTION_FUNCTIONS": ALL_ATTENTION_FUNCTIONS,
        "AttentionInterface": AttentionInterface,
        "ALL_MASK_ATTENTION_FUNCTIONS": ALL_MASK_ATTENTION_FUNCTIONS,
        "AttentionMaskInterface": AttentionMaskInterface,
        "sdpa_mask": sdpa_mask,
    }


def _backend_probe(torch: Any, sdpa_kernel: Any, SDPBackend: Any) -> None:
    """Run a tiny one-shot SDPA call forcing EFFICIENT_ATTENTION.

    If the fused backend cannot actually execute in this environment (e.g. the
    torch build does not ship the efficient kernel), this raises and
    registration fails closed instead of letting ``efficient_sdpa_forward``
    raise later at generation time.  Uses a tiny CUDA tensor with equal head
    counts (the post-``repeat_kv`` shape the forward feeds to SDPA).
    """

    import torch as _torch

    dev = "cuda"
    q = _torch.randn(1, 8, 16, 64, dtype=_torch.bfloat16, device=dev)
    try:
        with sdpa_kernel([SDPBackend.EFFICIENT_ATTENTION]):
            _torch.nn.functional.scaled_dot_product_attention(q, q, q)
    except Exception as exc:  # noqa: BLE001
        raise EfficientSdpaError(
            "EFFICIENT_ATTENTION backend probe failed; the optimized backend "
            f"cannot execute in this environment: {exc}"
        ) from exc


def _conflict_guard(
    registry: Any,
    key: str,
    expected_fn: Callable[..., Any],
    registry_name: str,
) -> None:
    """Idempotent + conflict-guarded registration for a GeneralInterface.

    * key absent -> ok to register;
    * key bound to our implementation -> no-op;
    * key bound to a different implementation -> EfficientSdpaError.

    ``GeneralInterface`` exposes the *current* bound value via
    ``registry[key]`` (local override takes precedence over the class
    ``_global_mapping``).

    Idempotency across calls: ``register_efficient_sdpa`` may build a new
    closure on each call, so pure object identity would wrongly flag a repeat
    registration as a conflict.  We therefore tag every forward produced by
    this module with a stable marker attribute (``_efficient_sdpa_marker``)
    and treat any function carrying that marker as "our implementation".  The
    mask function is always the exact ``sdpa_mask`` object, so it is compared
    by identity.  A function without the marker that is not the exact expected
    object is treated as a foreign binding and rejected.
    """

    try:
        current = registry[key]
    except KeyError:
        return
    if current is expected_fn:
        return
    # Idempotent re-registration: same module, possibly a new closure.
    if getattr(current, "_efficient_sdpa_marker", None) is _EFFICIENT_SDPA_MARKER:
        return
    raise EfficientSdpaError(
        f"{registry_name} key {key!r} is already bound to a different "
        f"implementation ({current!r}); refusing to overwrite another backend."
    )


def register_efficient_sdpa(*, force: bool = False) -> Dict[str, Any]:
    """Register the ``efficient_sdpa`` attention backend in Transformers.

    Returns a description dict (for telemetry) of what was registered and the
    capability evidence.  Idempotent: a second call is a no-op when the exact
    same implementation is already bound (unless ``force=True``, which
    re-runs the import/probe and re-asserts idempotency).
    """

    global _REGISTRATION

    deps = _lazy_imports()
    torch = deps["torch"]
    sdpa_kernel = deps["sdpa_kernel"]
    SDPBackend = deps["SDPBackend"]

    # Capability probe: prove the backend actually runs here before claiming
    # the optimization is available.
    _backend_probe(torch, sdpa_kernel, SDPBackend)

    # The forward closure captures only the deps it needs.
    efficient_fn = _make_efficient_sdpa_forward(
        repeat_kv=deps["repeat_kv"],
        sdpa_kernel=sdpa_kernel,
        SDPBackend=SDPBackend,
    )
    mask_fn = deps["sdpa_mask"]

    # Conflict-guarded registration in BOTH registries.
    _conflict_guard(
        deps["ALL_ATTENTION_FUNCTIONS"],
        EFFICIENT_SDPA_KEY,
        efficient_fn,
        "ALL_ATTENTION_FUNCTIONS",
    )
    _conflict_guard(
        deps["ALL_MASK_ATTENTION_FUNCTIONS"],
        EFFICIENT_SDPA_KEY,
        mask_fn,
        "ALL_MASK_ATTENTION_FUNCTIONS",
    )

    deps["AttentionInterface"].register(EFFICIENT_SDPA_KEY, efficient_fn)
    deps["AttentionMaskInterface"].register(EFFICIENT_SDPA_KEY, mask_fn)

    # Post-conditions (explicit fail-closed checks).
    if EFFICIENT_SDPA_KEY not in deps["ALL_ATTENTION_FUNCTIONS"].valid_keys():
        raise EfficientSdpaError(
            f"{EFFICIENT_SDPA_KEY!r} not in ALL_ATTENTION_FUNCTIONS.valid_keys() "
            "after registration."
        )
    if EFFICIENT_SDPA_KEY not in deps["ALL_MASK_ATTENTION_FUNCTIONS"].valid_keys():
        raise EfficientSdpaError(
            f"{EFFICIENT_SDPA_KEY!r} not in ALL_MASK_ATTENTION_FUNCTIONS.valid_keys() "
            "after registration."
        )

    info = {
        "key": EFFICIENT_SDPA_KEY,
        "attention_forward": getattr(efficient_fn, "__qualname__", repr(efficient_fn)),
        "mask_function": getattr(mask_fn, "__qualname__", repr(mask_fn)),
        "backend": "EFFICIENT_ATTENTION",
        "backend_value": int(SDPBackend.EFFICIENT_ATTENTION),
        "transformers_version": _safe_transformers_version(),
        "torch_version": getattr(torch, "__version__", "UNKNOWN"),
        "cuda_available": bool(torch.cuda.is_available()),
        "registered": True,
    }
    _REGISTRATION = info
    return info


def registration_info() -> Optional[Dict[str, Any]]:
    """Return the last registration description, or ``None`` if unregistered."""
    return None if _REGISTRATION is None else dict(_REGISTRATION)


def is_registered() -> bool:
    """True iff ``register_efficient_sdpa()`` has succeeded in this process."""
    return _REGISTRATION is not None


def _safe_transformers_version() -> str:
    try:
        import transformers

        return getattr(transformers, "__version__", "UNKNOWN")
    except Exception:  # noqa: BLE001
        return "UNAVAILABLE"


# ---------------------------------------------------------------------------
# The attention forward function.
# ---------------------------------------------------------------------------


def _make_efficient_sdpa_forward(
    *,
    repeat_kv: Callable[..., Any],
    sdpa_kernel: Any,
    SDPBackend: Any,
) -> Callable[..., Any]:
    """Build the ``efficient_sdpa_forward`` callable.

    The body mirrors ``transformers.integrations.sdpa_attention.sdpa_attention_forward``
    (Transformers 4.57.3) with exactly two deliberate differences:

    1. We **always** expand KV with ``repeat_kv`` when
       ``module.num_key_value_groups != 1`` — we never set ``enable_gqa=True``.
       Equal head counts are what makes ``EFFICIENT_ATTENTION`` accept the
       call (this is the whole point of the workaround).
    2. The SDPA call is wrapped in
       ``with sdpa_kernel([SDPBackend.EFFICIENT_ATTENTION]):`` with **no fallback
       list**.  If the efficient backend cannot run, PyTorch raises and that
       propagates as fail-closed behavior — we never silently fall back to the
       pathological ``MATH`` backend when the optimized mode was requested.

    Everything else (4D mask slicing, ``is_causal`` derivation, jit-tracing
    SymBool handling, ``output_attentions``/``head_mask`` warning, scaling,
    dropout, final transpose/contiguous) is preserved verbatim from the stock
    ``sdpa`` path so the only numerical difference vs stock is the fused-vs-math
    backend choice.
    """

    def efficient_sdpa_forward(
        module: Any,
        query: Any,
        key: Any,
        value: Any,
        attention_mask: Optional[Any],
        dropout: float = 0.0,
        scaling: Optional[float] = None,
        is_causal: Optional[bool] = None,
        **kwargs: Any,
    ):
        # Heavy import deferred to call site so module import stays torch-free.
        import torch
        from transformers.integrations.sdpa_attention import (
            logger as _sdpa_logger,  # reuse stock logger for the warning_once
        )

        if kwargs.get("output_attentions", False) or kwargs.get("head_mask") is not None:
            _sdpa_logger.warning_once(
                "`sdpa` attention does not support `output_attentions=True` or `head_mask`."
                " Please set your attention to `eager` if you want any of these features."
            )

        n_rep = int(getattr(module, "num_key_value_groups", 1))
        if n_rep != 1:
            # Explicit repeat_kv: 4 KV heads -> 28 heads.  NEVER enable_gqa.
            key = repeat_kv(key, n_rep)
            value = repeat_kv(value, n_rep)

        # Preserve attention_mask slicing exactly like the stock path.
        if attention_mask is not None and attention_mask.ndim == 4:
            attention_mask = attention_mask[:, :, :, : key.shape[-2]]

        # is_causal derivation: identical to stock sdpa_attention_forward.
        if is_causal is None:
            is_causal = (
                query.shape[2] > 1
                and attention_mask is None
                and getattr(module, "is_causal", True)
            )

        # jit tracing produces tensor-valued is_causal; convert to bool.
        if torch.jit.is_tracing() and isinstance(is_causal, torch.Tensor):
            is_causal = is_causal.item()

        # Force the efficient backend. No fallback -> fail closed.
        with sdpa_kernel([SDPBackend.EFFICIENT_ATTENTION]):
            attn_output = torch.nn.functional.scaled_dot_product_attention(
                query,
                key,
                value,
                attn_mask=attention_mask,
                dropout_p=dropout,
                scale=scaling,
                is_causal=is_causal,
            )

        attn_output = attn_output.transpose(1, 2).contiguous()
        return attn_output, None

    efficient_sdpa_forward.__name__ = "efficient_sdpa_forward"
    efficient_sdpa_forward.__qualname__ = "efficient_sdpa_forward"
    # Tag the closure so the conflict guard recognizes our own implementation
    # across repeated registrations (idempotent re-registration).
    efficient_sdpa_forward._efficient_sdpa_marker = _EFFICIENT_SDPA_MARKER  # type: ignore[attr-defined]
    return efficient_sdpa_forward