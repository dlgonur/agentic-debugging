"""R5.4 — local Qwen2.5-Coder-14B-Instruct transport (escalation identity).

Model-identity escalation record: the r5.2/r5.3 RAW-7B matrices proved the
harness mechanics (terminal progression, fence unwrap, whole-file repair,
verifier-feedback loop) but the RAW 7B model repeatedly failed to author
correct minimal semantic repairs (wrong line, repeated wrong patches under
greedy decoding).  Under the goal's escalation authority this module binds a
STRONGER locally-available open model — Qwen2.5-Coder-14B-Instruct, the same
family/chat-template as the pinned RAW base — with the identical frozen
generation configuration and NF4 quantization.

This is a NEW model identity: results produced through this transport are
never labeled as RAW-Qwen-7B results.  The RAW r5.3 matrix is preserved as
the historical baseline.
"""

from __future__ import annotations

import time
from typing import Any

from experiments.debugger_interaction_v2_r5.adapter import (
    ModelTransport,
    TransportError,
    TransportResponse,
)

# Frozen model identity (escalation, recorded in the r5.4 contract).
BASE_REPOSITORY = "Qwen/Qwen2.5-Coder-14B-Instruct"
BASE_REVISION = "aedcc2d42b622764e023cf882b6652e646b95671"

# Identical generation configuration to the RAW-7B treatment.
GENERATION_CONFIG = {
    "do_sample": False,
    "max_new_tokens": 1024,
    "max_input_tokens": 32768,
}


class LocalQwen14BTransport:
    """Local NF4 Qwen2.5-Coder-14B transport with raw-text retention.

    Identical loading/generation semantics to the accepted RAW-7B transport
    (bitsandbytes NF4, bf16/fp16 compute, greedy decoding, same chat
    template family), only the base model changes.
    """

    def __init__(
        self,
        *,
        max_new_tokens: int = 1024,
        max_input_tokens: int = 32768,
        request_timeout_seconds: float = 60.0,
    ) -> None:
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
            from experiments.local_inference_perf.efficient_sdpa import (
                register_efficient_sdpa,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Qwen2.5-Coder-14B transport requires torch, transformers, "
                "and bitsandbytes"
            ) from exc

        if not torch.cuda.is_available():
            raise RuntimeError("Qwen2.5-Coder-14B transport requires a CUDA runtime")

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
        self.tokenizer = AutoTokenizer.from_pretrained(
            BASE_REPOSITORY,
            revision=BASE_REVISION,
            trust_remote_code=False,
        )
        self.model = AutoModelForCausalLM.from_pretrained(
            BASE_REPOSITORY,
            revision=BASE_REVISION,
            trust_remote_code=False,
            device_map="auto",
            quantization_config=quantization,
            torch_dtype=compute_dtype,
            attn_implementation="efficient_sdpa",
        )
        self.model.eval()
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


__all__ = [
    "LocalQwen14BTransport",
    "BASE_REPOSITORY",
    "BASE_REVISION",
    "GENERATION_CONFIG",
]
