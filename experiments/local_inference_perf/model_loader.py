"""Lazy, fail-closed model loading for the local inference performance
benchmark.

This loader mirrors the *proven* cp118 experiment condition
(``experiments/cp118_rag_definitive/s4_transport.py``) — same pinned Qwen base
revision, same NF4/double-quant/BF16 loading, same greedy generation settings —
but is a fully separate implementation that **never imports the frozen S4
modules**, so the frozen scientific run stays untouched.

Hard model-loading imports (torch, transformers, peft, bitsandbytes) are lazy:
they happen inside ``load()`` only, so ``--validate-only``/offline tests never
load the model and the package stays import-tolerant in a torch-less Python.

Defaults (amendment 7): ``device_map="auto"``.  After load we inspect
``hf_device_map`` and record it.  In *optimized* benchmark mode
(``require_gpu_placement=True``) the loader fails closed if the resolved
placement contains CPU or disk/offload — we never silently switch the
benchmark condition to ``device_map={"": 0}`` for convenience.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

# Pinned proven constants (duplicated from the frozen S4 contract, NOT
# imported, so the frozen run is never touched).  See experiments/
# cp118_rag_definitive/s4_identity.py + s4_contract.json for the canonical
# source of truth.
BASE_REPOSITORY = "Qwen/Qwen2.5-Coder-7B-Instruct"
BASE_REVISION = "c03e6d358207e414f1eca0bb1891e29f1db0e242"


class LoaderError(RuntimeError):
    """Raised on any model-load or placement failure (fail closed)."""


class LocalQwenAdapterLoader:
    """Load the pinned Qwen2.5-Coder-7B base + a cp118 PEFT/QLoRA adapter.

    Parameters
    ----------
    adapter_path:
        Path to the verified cp118 adapter directory.
    attn_implementation:
        Attention key.  ``"efficient_sdpa"`` requires
        :func:`efficient_sdpa.register_efficient_sdpa` to have succeeded first.
    require_gpu_placement:
        When True (optimized benchmark mode), fail closed if the resolved
        ``hf_device_map`` contains CPU or disk/offload.  Stock mode may keep
        this False so a stock run still loads (the stock path is the reference).
    """

    def __init__(
        self,
        *,
        adapter_path: str,
        attn_implementation: str = "efficient_sdpa",
        require_gpu_placement: bool = True,
        torch_dtype: Optional[str] = "bfloat16",
    ) -> None:
        self.adapter_path = adapter_path
        self.attn_implementation = attn_implementation
        self.require_gpu_placement = require_gpu_placement
        self.torch_dtype_name = torch_dtype
        self.model = None
        self.tokenizer = None
        self.load_seconds: Optional[float] = None
        self.resolved_device_map: Optional[Dict[str, str]] = None

    def load(self) -> Dict[str, Any]:
        """Load base + adapter. Returns placement/load metadata."""

        if self.model is not None:
            return self._meta()
        try:
            import torch
            import transformers
            from peft import PeftModel
            from transformers import BitsAndBytesConfig
        except ImportError as exc:
            raise LoaderError(
                "local inference perf loader requires torch/transformers/peft/"
                f"bitsandbytes: {exc}"
            ) from exc

        dtype = self._resolve_dtype(torch)
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
        t0 = time.perf_counter()
        try:
            base = transformers.AutoModelForCausalLM.from_pretrained(
                BASE_REPOSITORY,
                revision=BASE_REVISION,
                quantization_config=quantization_config,
                device_map="auto",  # amendment 7: proven condition
                torch_dtype=dtype,
                attn_implementation=self.attn_implementation,
            )
            self.model = PeftModel.from_pretrained(
                base, self.adapter_path, is_trainable=False
            )
            self.model.eval()
            self.tokenizer = transformers.AutoTokenizer.from_pretrained(
                BASE_REPOSITORY, revision=BASE_REVISION
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on any load error
            self.model = None
            raise LoaderError(f"model load failed: {exc}") from exc
        self.load_seconds = time.perf_counter() - t0

        # Inspect resolved placement (amendment 7).
        self.resolved_device_map = self._read_device_map()
        if self.require_gpu_placement:
            self._assert_gpu_placement(self.resolved_device_map)

        return self._meta()

    def _resolve_dtype(self, torch: Any) -> Any:
        name = self.torch_dtype_name
        if name is None:
            return None
        mapping = {
            "bfloat16": getattr(torch, "bfloat16", None),
            "float16": getattr(torch, "float16", None),
            "float32": getattr(torch, "float32", None),
        }
        dt = mapping.get(name)
        if dt is None:
            raise LoaderError(f"unknown torch_dtype name: {name!r}")
        return dt

    def _read_device_map(self) -> Dict[str, str]:
        if self.model is None:
            return {}
        hdm = getattr(self.model, "hf_device_map", None)
        if not hdm:
            return {}
        return {str(k): str(v) for k, v in dict(hdm).items()}

    def _assert_gpu_placement(self, device_map: Dict[str, str]) -> None:
        # BLOCKER 3: in require_gpu_placement mode an empty/unavailable
        # device_map is NOT acceptable.  Without a resolved placement we cannot
        # prove the model is fully on GPU, so we fail closed rather than
        # implicitly assume it.
        if not device_map:
            raise LoaderError(
                "optimized benchmark mode requires a resolved GPU placement "
                "but hf_device_map is empty/unavailable.  Cannot prove the "
                "model is fully on GPU; refusing to proceed. "
                "(Normal resolved CUDA mappings such as {\"\": \"0\"} are "
                "accepted; only empty/CPU/disk/offload are rejected.)"
            )
        bad = []
        for k, v in device_map.items():
            lv = str(v).lower()
            if "cpu" in lv or "disk" in lv or "offload" in lv:
                bad.append((k, v))
        if bad:
            raise LoaderError(
                "optimized benchmark mode requires fully-GPU placement but "
                f"hf_device_map contains non-GPU targets: {bad}. "
                "Refusing to silently switch the benchmark condition."
            )

    def _meta(self) -> Dict[str, Any]:
        return {
            "adapter_path": self.adapter_path,
            "base_repository": BASE_REPOSITORY,
            "base_revision": BASE_REVISION,
            "attn_implementation": self.attn_implementation,
            "load_seconds": self.load_seconds,
            "resolved_device_map": self.resolved_device_map,
            "require_gpu_placement": self.require_gpu_placement,
        }

    def close(self) -> None:
        if self.model is not None:
            try:
                import torch

                del self.model
                self.model = None
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass