"""S4 — cp118 one-shot transport for the frozen v1.2.1 generation protocol.

The ONLY model change vs the frozen RAW generation protocol is the model
condition: the identity-verified cp118 PEFT/QLoRA adapter is attached to
the pinned base (``Qwen/Qwen2.5-Coder-7B-Instruct`` @
``c03e6d358207e414f1eca0bb1891e29f1db0e242``) through the established
tuned-pilot loading mechanism (``PeftModel.from_pretrained(base,
adapter_path, is_trainable=False)`` over the same 4-bit NF4 double-quant
base load).  Generation settings stay frozen: greedy
``do_sample=False, num_beams=1``, ``max_new_tokens=4096``; temperature /
top_p are never set (frozen protocol; recorded NOT_RECORDED).

Heavy imports (torch, bitsandbytes, transformers, peft) happen lazily in
``load()`` only, so ``--validate-only`` and the offline tests never load or
run the model.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from experiments.cp118_rag_definitive.s4_identity import (
    BASE_REPOSITORY,
    BASE_REVISION,
    verify_adapter_identity,
)
from experiments.cp118_rag_definitive.s4_payload import (
    MAX_NEW_TOKENS,
    MAX_PROMPT_TOKENS,
)


class TransportError(RuntimeError):
    """Raised on transport/generation failures (fail closed)."""


class LocalCp118QwenTransport:
    """Identity-verified local cp118 one-shot generation transport."""

    def __init__(
        self,
        *,
        adapter_path: str,
        expected_adapter_identity: Dict[str, Any],
        max_new_tokens: int = MAX_NEW_TOKENS,
        max_input_tokens: int = MAX_PROMPT_TOKENS,
    ) -> None:
        # Fail closed BEFORE any GPU load: the definitive cp118 checkpoint
        # must be located and verified exactly.
        self.adapter_identity = verify_adapter_identity(
            Path(adapter_path), expected_adapter_identity
        )
        self.adapter_path = str(Path(adapter_path).resolve())
        self.max_new_tokens = max_new_tokens
        self.max_input_tokens = max_input_tokens
        self.model = None
        self.tokenizer = None
        self._load_seconds: Optional[float] = None

    def load(self) -> None:
        """Load the quantized base + verified adapter (CUDA environment)."""

        if self.model is not None:
            return
        try:
            import torch
            import transformers
            from peft import PeftModel
            from transformers import BitsAndBytesConfig
        except ImportError as exc:
            raise TransportError(
                "cp118 transport requires torch/transformers/peft/"
                f"bitsandbytes: {exc}"
            ) from exc

        t0 = time.perf_counter()
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
        try:
            base = transformers.AutoModelForCausalLM.from_pretrained(
                BASE_REPOSITORY,
                revision=BASE_REVISION,
                quantization_config=quantization_config,
                device_map="auto",
                torch_dtype=torch.bfloat16,
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
            raise TransportError(f"cp118 model load failed: {exc}") from exc
        self._load_seconds = time.perf_counter() - t0

    def generate_one(self, payload_text: str) -> Dict[str, Any]:
        """One greedy v1.2.1 generation (mirrors the frozen C9 call)."""

        if self.model is None or self.tokenizer is None:
            raise TransportError("transport not loaded; call load() first")
        try:
            import torch
        except ImportError as exc:  # pragma: no cover
            raise TransportError(f"torch unavailable: {exc}") from exc

        messages = [{"role": "user", "content": payload_text}]
        batch = self.tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        )
        input_len = int(batch["input_ids"].shape[-1])
        if input_len > self.max_input_tokens:
            raise TransportError(
                f"prompt overflow: {input_len} tokens > max_input_tokens "
                f"{self.max_input_tokens}"
            )
        device = next(self.model.parameters()).device
        batch = {
            k: (v.to(device) if torch.is_tensor(v) else v)
            for k, v in batch.items()
        }
        torch.cuda.reset_peak_memory_stats()
        t0 = time.perf_counter()
        with torch.inference_mode():
            out = self.model.generate(
                **batch,
                do_sample=False,
                num_beams=1,
                max_new_tokens=self.max_new_tokens,
            )
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - t0
        new_ids = out[0][input_len:]
        text = self.tokenizer.decode(new_ids, skip_special_tokens=True)
        return {
            "text": text,
            "prompt_tokens": input_len,
            "output_tokens": int(new_ids.shape[-1]),
            "generation_latency_s": elapsed,
            "peak_allocated_gib": torch.cuda.max_memory_allocated() / 1024**3,
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
