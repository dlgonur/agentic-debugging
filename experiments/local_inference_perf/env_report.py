"""Environment/telemetry capture for the local inference performance package.

All gathering is **lazy and fail-closed**: a missing capability is recorded as
``NOT_AVAILABLE`` (never inferred as present), and no heavy ML library is
imported at module import time.  Callers receive a plain JSON-serializable
dict.

Two flash-attention conditions are recorded **separately** because they are not
the same thing (amendment 4):

* ``torch_flash_sdp_available`` — ``torch.backends.cuda.is_flash_attention_available()``:
  whether the *torch SDPA* fused-flash backend can run in this build.  On this
  Windows torch 2.10 dev + cu128 build this is ``False``; this is the root-cause
  observation that forces the stock path onto MATH SDPA.
* ``hf_flash_attn2_package_available`` — Transformers' flash-attn-2 *package*
  availability (``transformers.utils.import_utils.is_flash_attn_2_available()``).
  This is a separate condition (the standalone ``flash_attn`` package).
"""

from __future__ import annotations

import platform
from typing import Any, Dict, Optional

_NOT = "NOT_AVAILABLE"


def _safe(fn, *args, **kwargs):
    """Call ``fn``; return its result or ``NOT_AVAILABLE`` on any error."""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - telemetry must never crash the run
        return _NOT


def capture_environment(*, model: Any = None) -> Dict[str, Any]:
    """Capture the environment/telemetry block.

    ``model`` is optional.  When a loaded model is supplied, the resolved
    device map and ``use_cache`` are recorded from it; otherwise those fields
    are ``NOT_RECORDED``.
    """

    report: Dict[str, Any] = {
        "captured_at_utc": _safe(_utc_now),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "torch_version": _NOT,
        "cuda_runtime_version": _NOT,
        "gpu_name": _NOT,
        "gpu_compute_capability": _NOT,
        "transformers_version": _NOT,
        "peft_version": _NOT,
        "bitsandbytes_version": _NOT,
        "torch_flash_sdp_available": _NOT,
        "hf_flash_attn2_package_available": _NOT,
        "sdp_backends": _NOT,
        "active_attention_implementation": _NOT,
        "model_device_map": _NOT,
        "use_cache": _NOT,
    }

    # torch
    def _torch_block():
        import torch

        report["torch_version"] = getattr(torch, "__version__", _NOT)
        report["cuda_runtime_version"] = getattr(torch.version, "cuda", None) or _NOT
        if torch.cuda.is_available():
            try:
                report["gpu_name"] = torch.cuda.get_device_name(0)
                report["gpu_compute_capability"] = list(
                    torch.cuda.get_device_capability(0)
                )
            except Exception:  # noqa: BLE001
                pass
        # Dual flash flags (amendment 4).
        report["torch_flash_sdp_available"] = _safe(
            torch.backends.cuda.is_flash_attention_available
        )
        # SDPBackend enum members present in this build.
        try:
            from torch.nn.attention import SDPBackend

            report["sdp_backends"] = [
                {"name": b.name, "value": int(b)}
                for b in (
                    SDPBackend.MATH,
                    SDPBackend.FLASH_ATTENTION,
                    SDPBackend.EFFICIENT_ATTENTION,
                    SDPBackend.CUDNN_ATTENTION,
                )
            ]
        except Exception:  # noqa: BLE001
            report["sdp_backends"] = _NOT

    _safe(_torch_block)

    # transformers / peft / bitsandbytes
    def _tf_block():
        import transformers

        report["transformers_version"] = getattr(transformers, "__version__", _NOT)
        try:
            from transformers.utils.import_utils import is_flash_attn_2_available

            report["hf_flash_attn2_package_available"] = bool(
                is_flash_attn_2_available()
            )
        except Exception:  # noqa: BLE001
            report["hf_flash_attn2_package_available"] = _NOT

    _safe(_tf_block)

    def _peft_block():
        import peft

        report["peft_version"] = getattr(peft, "__version__", _NOT)

    _safe(_peft_block)

    def _bnb_block():
        import bitsandbytes

        report["bitsandbytes_version"] = getattr(bitsandbytes, "__version__", _NOT)

    _safe(_bnb_block)

    # Active attention implementation + model placement (when a model exists).
    if model is not None:
        cfg = getattr(model, "config", None)
        if cfg is not None:
            attn = getattr(cfg, "_attn_implementation", None)
            report["active_attention_implementation"] = (
                attn if attn is not None else _NOT
            )
            report["use_cache"] = bool(getattr(cfg, "use_cache", _NOT))
        hf_device_map = getattr(model, "hf_device_map", None)
        if hf_device_map:
            report["model_device_map"] = {
                str(k): str(v) for k, v in dict(hf_device_map).items()
            }
        else:
            report["model_device_map"] = _NOT
    else:
        report["active_attention_implementation"] = _NOT
        report["model_device_map"] = _NOT
        report["use_cache"] = _NOT

    return report


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")