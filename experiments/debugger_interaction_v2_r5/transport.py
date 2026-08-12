"""R5 — local RAW Qwen2.5-Coder-7B transport with raw-text retention.

Verbatim copy of the accepted R3 transport (frozen model identity, revision,
chat template, NF4 quantization, generation configuration) with imports
bound to the R5 adapter.  No provider/model/route/billing substitution.

The transport does NOT parse or validate the model output.  It returns a
``TransportResponse`` with ``raw_text`` (the exact decoded text) and
``usage`` (token counts).  Parsing is the adapter's responsibility.

Lazy imports: the model stack (torch, transformers, bitsandbytes) is
imported only in ``__init__`` so that offline tests never trigger a
model/GPU dependency.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from experiments.debugger_interaction_v2_r5.adapter import (
    ModelTransport,
    NOT_AVAILABLE,
    TransportError,
    TransportResponse,
)
from experiments.debugger_interaction_v2_r5.gpu_placement import (
    audit_single_cuda_placement,
    explicit_cuda_device_map,
)

# Frozen model identity (accepted R3 value).
BASE_REPOSITORY = "Qwen/Qwen2.5-Coder-7B-Instruct"
BASE_REVISION = "c03e6d358207e414f1eca0bb1891e29f1db0e242"

# Frozen generation configuration (identical to R3).
GENERATION_CONFIG = {
    "do_sample": False,
    "max_new_tokens": 1024,
    "max_input_tokens": 32768,
}

LifecycleEvent = Optional[Callable[[str, dict[str, Any]], None]]


def _emit_lifecycle(
    callback: LifecycleEvent,
    event: str,
    **details: Any,
) -> None:
    if callback is not None:
        callback(event, details)


class LocalRawQwenTransport:
    """Local pinned RAW Qwen2.5-Coder-7B transport with raw-text retention.

    Parameters
    ----------
    max_new_tokens
        Frozen at 1024 (R3 value).
    max_input_tokens
        Frozen at 32768 (R3 value).
    request_timeout_seconds
        Frozen at 60.0 (R3 value).

    The transport loads the model in ``__init__`` (lazy GPU/torch import).
    For offline tests, use ``FakeTransport`` — never instantiate this class.
    """

    def __init__(
        self,
        *,
        max_new_tokens: int = 1024,
        max_input_tokens: int = 32768,
        request_timeout_seconds: float = 60.0,
        cuda_device_index: int = 0,
        lifecycle_event: LifecycleEvent = None,
    ) -> None:
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
            from experiments.local_inference_perf.efficient_sdpa import (
                register_efficient_sdpa,
            )
        except ImportError as exc:
            raise RuntimeError(
                "RAW Qwen2.5 transport requires torch, transformers, "
                "and bitsandbytes"
            ) from exc

        if not torch.cuda.is_available():
            raise RuntimeError("RAW Qwen2.5 transport requires a CUDA runtime")
        requested_device_map = explicit_cuda_device_map(cuda_device_index)
        if cuda_device_index >= torch.cuda.device_count():
            raise RuntimeError(
                f"CUDA device {cuda_device_index} is unavailable; "
                f"device_count={torch.cuda.device_count()}"
            )
        torch.cuda.set_device(cuda_device_index)
        _emit_lifecycle(
            lifecycle_event,
            "transport_init_start",
            transport="LocalRawQwenTransport",
            base_repository=BASE_REPOSITORY,
            base_revision=BASE_REVISION,
            requested_device_map=requested_device_map,
        )

        # STABLE + PHYSICAL-VRAM-BOUND (R6 amendment): stock SDPA falls to the
        # pathological MATH backend on this torch build; the validated
        # repeat_kv EFFICIENT_ATTENTION path runs the fused kernels.  Fail-closed
        # registration; the model loads with attn_implementation="efficient_sdpa".
        register_efficient_sdpa()

        compute_dtype = (
            torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        )
        quantization = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=compute_dtype,
        )
        _emit_lifecycle(lifecycle_event, "tokenizer_load_start")
        self.tokenizer = AutoTokenizer.from_pretrained(
            BASE_REPOSITORY,
            revision=BASE_REVISION,
            trust_remote_code=False,
        )
        _emit_lifecycle(lifecycle_event, "tokenizer_load_complete")
        _emit_lifecycle(lifecycle_event, "base_model_load_start")
        self.model = AutoModelForCausalLM.from_pretrained(
            BASE_REPOSITORY,
            revision=BASE_REVISION,
            trust_remote_code=False,
            device_map=requested_device_map,
            quantization_config=quantization,
            torch_dtype=compute_dtype,
            attn_implementation="efficient_sdpa",
        )
        self.model.eval()
        placement = audit_single_cuda_placement(
            self.model, expected_device_index=cuda_device_index
        )
        self.placement_audit = {
            "final_model": placement,
            "runtime": {
                "torch_version": str(torch.__version__),
                "torch_cuda_version": str(torch.version.cuda),
                "cuda_device_index": cuda_device_index,
                "cuda_device_name": str(torch.cuda.get_device_name(cuda_device_index)),
                "compute_dtype": str(compute_dtype),
                "attention_implementation": "efficient_sdpa",
                "quantization": "NF4 double-quant",
            },
        }
        _emit_lifecycle(
            lifecycle_event,
            "base_model_load_complete",
            placement=placement,
        )
        _emit_lifecycle(
            lifecycle_event,
            "transport_init_complete",
            placement_audit=self.placement_audit,
        )
        self.torch = torch
        self.max_new_tokens = max_new_tokens
        self.max_input_tokens = max_input_tokens
        self.request_timeout = request_timeout_seconds
        self.device = next(self.model.parameters()).device

    def request(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> TransportResponse:
        """Call the model and return raw text + usage.

        The raw decoded text is ALWAYS returned, even if it is empty or
        would fail parsing.
        """

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        try:
            rendered = self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = self.tokenizer(
                rendered,
                return_tensors="pt",
                add_special_tokens=False,
            )
        except Exception as exc:
            raise TransportError(
                "request_serialization",
                f"tokenization failed: {type(exc).__name__}",
            ) from exc

        prompt_tokens = int(inputs["input_ids"].shape[-1])
        if prompt_tokens > self.max_input_tokens:
            raise TransportError(
                "request_too_large",
                f"prompt {prompt_tokens} exceeds {self.max_input_tokens}",
            )
        inputs = {key: value.to(self.device) for key, value in inputs.items()}

        started = time.monotonic()
        try:
            with self.torch.inference_mode():
                generated = self.model.generate(
                    **inputs,
                    do_sample=False,
                    max_new_tokens=self.max_new_tokens,
                    max_time=float(timeout_seconds),
                    pad_token_id=(
                        self.tokenizer.eos_token_id
                        if self.tokenizer.pad_token_id is None
                        else self.tokenizer.pad_token_id
                    ),
                )
        except Exception as exc:
            raise TransportError(
                "generation_error",
                f"model.generate failed: {type(exc).__name__}",
            ) from exc

        elapsed = time.monotonic() - started
        if elapsed > timeout_seconds + 5.0:
            raise TransportError(
                "request_timeout",
                f"generation exceeded {timeout_seconds}s",
            )

        new_tokens = generated[0, prompt_tokens:]
        completion_tokens = int(new_tokens.shape[-1])
        text = self.tokenizer.decode(new_tokens, skip_special_tokens=True).strip()

        return TransportResponse(
            raw_text=text,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        )


class FakeTransport:
    """A deterministic transport for offline tests.

    Returns pre-written raw text strings, simulating model responses without
    loading any model.  Used by the evidence-retention unit tests to verify
    that raw text is retained on parse failure, transport failure, etc.
    """

    def __init__(self, responses: tuple[str, ...]) -> None:
        self._responses = responses
        self._index = 0

    def request(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> TransportResponse:
        if self._index >= len(self._responses):
            raise TransportError("exhausted", "no more fake responses")
        text = self._responses[self._index]
        self._index += 1
        return TransportResponse(
            raw_text=text,
            usage={
                "prompt_tokens": 100,
                "completion_tokens": len(text.split()),
                "total_tokens": 100 + len(text.split()),
            },
        )


class FailingTransport:
    """A transport that always raises TransportError.

    Used to test the NOT_AVAILABLE / transport_failure telemetry path.
    """

    def __init__(self, category: str = "generation_error") -> None:
        self._category = category

    def request(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> TransportResponse:
        raise TransportError(self._category, "simulated transport failure")


__all__ = [
    "LocalRawQwenTransport",
    "FakeTransport",
    "FailingTransport",
    "BASE_REPOSITORY",
    "BASE_REVISION",
    "GENERATION_CONFIG",
]
