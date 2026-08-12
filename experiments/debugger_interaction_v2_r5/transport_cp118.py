"""R6 — local Qwen2.5-Coder-7B-Instruct + project-fine-tuned cp118 adapter
transport (matched r5.9 clean-holdout interface).

The historical project-fine-tuned checkpoint cp118 (QLoRA/PEFT LoRA adapter
trained from Qwen/Qwen2.5-Coder-7B-Instruct @ c03e6d3... on the accepted
SWE-rebench V2 training view; selected as the best surviving saved checkpoint
by held-out SWE-rebench validation eval_loss) is attached to the SAME pinned
base revision the r5.1-r5.3 RAW-7B treatment used, under the IDENTICAL frozen
r5.9 clean-holdout treatment (same system prompt template, sanitizer,
production-exception path, region-filtered observations, budgets, generation
configuration, and anti-leakage audit) that produced the successful 14B
matrix.  No interface simplification versus the successful model.

Loading semantics mirror the accepted RAW-7B and 14B transports:
bitsandbytes NF4 double-quant, bf16/fp16 compute dtype, greedy decoding,
max_new_tokens=1024, max_input_tokens=32768.  The only delta is the PEFT
adapter attachment (PeftModel.from_pretrained(..., is_trainable=False)),
identical to the accepted tuned_debugger_pilot_v1 loader.

The transport does NOT parse or validate the model output.  It returns a
``TransportResponse`` with ``raw_text`` (the exact decoded text) and
``usage`` (token counts).  Parsing is the adapter's responsibility.

Lazy imports: the model stack (torch, transformers, peft, bitsandbytes) is
imported only in ``__init__`` so that offline tests never trigger a
model/GPU dependency.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

from experiments.debugger_interaction_v2_r5.adapter import (
    ModelTransport,
    TransportError,
    TransportResponse,
)
from experiments.debugger_interaction_v2_r5.gpu_placement import (
    audit_single_cuda_placement,
    explicit_cuda_device_map,
)

# Frozen model identity — identical to the accepted RAW-7B treatment base.
BASE_REPOSITORY = "Qwen/Qwen2.5-Coder-7B-Instruct"
BASE_REVISION = "c03e6d358207e414f1eca0bb1891e29f1db0e242"

# Frozen generation configuration (identical to RAW-7B / 14B treatments).
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


def adapter_tree_identity(adapter_path: Path) -> dict[str, Any]:
    """Compute the cp118 adapter identity with the accepted D1 convention.

    The tree identity is the SHA-256 over, for every file under the adapter
    directory sorted by path: ``relative_posix_path + NUL + sha256_hex + NUL``.
    This is the exact convention the accepted tuned-debugger-pilot-v1
    ``_adapter_identity`` used (tree identity 65b5ed9a... for the frozen
    cp118 bundle).
    """
    if not adapter_path.is_dir():
        raise RuntimeError(f"adapter path is not a directory: {adapter_path}")
    config_path = adapter_path / "adapter_config.json"
    weights_path = adapter_path / "adapter_model.safetensors"
    if not config_path.is_file():
        raise RuntimeError("adapter_config.json is missing from the adapter")
    if not weights_path.is_file():
        raise RuntimeError("adapter_model.safetensors is missing from the adapter")
    files: list[dict[str, Any]] = []
    combined = hashlib.sha256()
    for path in sorted(item for item in adapter_path.rglob("*") if item.is_file()):
        relative = path.relative_to(adapter_path).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({
            "path": relative,
            "sha256": digest,
            "size_bytes": path.stat().st_size,
        })
        combined.update(relative.encode("utf-8"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        combined.update(b"\0")
    return {
        "path": str(adapter_path.resolve()),
        "tree_identity_sha256": combined.hexdigest(),
        "files": files,
    }


def validate_adapter_identity(adapter_path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed on-disk adapter identity check against the frozen contract.

    Verifies: the tree identity matches the frozen contract value, every
    frozen per-file SHA-256 matches, and the adapter_config.json declares the
    same base model as the frozen treatment base.  Returns the computed
    identity (including the resolved path) on success.
    """
    frozen_tree = expected.get("tree_identity_sha256")
    frozen_files = expected.get("files") or {}
    if type(frozen_tree) is not str or not frozen_tree:
        raise RuntimeError("contract adapter tree_identity_sha256 missing")
    if not isinstance(frozen_files, dict) or not frozen_files:
        raise RuntimeError("contract adapter files identity missing")
    computed = adapter_tree_identity(adapter_path)
    if computed["tree_identity_sha256"] != frozen_tree:
        raise RuntimeError(
            "adapter tree identity mismatch: "
            f"computed {computed['tree_identity_sha256'][:16]}... != frozen {frozen_tree[:16]}..."
        )
    actual_by_path = {f["path"]: f["sha256"] for f in computed["files"]}
    mismatches = [
        (rel, frozen_files[rel], actual_by_path.get(rel))
        for rel in sorted(frozen_files)
        if actual_by_path.get(rel) != frozen_files[rel]
    ]
    if mismatches:
        raise RuntimeError(f"adapter file hash mismatch: {mismatches[:3]}")
    config_path = adapter_path / "adapter_config.json"
    try:
        adapter_config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"adapter_config.json unreadable: {exc}") from exc
    declared_base = adapter_config.get("base_model_name_or_path")
    if declared_base not in {None, "", BASE_REPOSITORY}:
        raise RuntimeError(
            "adapter declares a different base model: "
            f"{declared_base!r} != {BASE_REPOSITORY!r}"
        )
    return computed


class LocalQwenPeftTransport:
    """Local NF4 Qwen2.5-Coder-7B-Instruct + frozen PEFT adapter transport.

    Identical loading/generation semantics to the accepted RAW-7B and 14B
    transports (bitsandbytes NF4 double-quant, bf16/fp16 compute, greedy
    decoding, same chat-template family), with the project-fine-tuned cp118
    LoRA adapter attached via PeftModel.
    """

    def __init__(
        self,
        *,
        adapter_path: Optional[str] = None,
        max_new_tokens: int = 1024,
        max_input_tokens: int = 32768,
        request_timeout_seconds: float = 60.0,
        cuda_device_index: int = 0,
        lifecycle_event: LifecycleEvent = None,
    ) -> None:
        if adapter_path is None:
            raise RuntimeError("cp118 transport requires adapter_path")
        self._adapter_path = Path(adapter_path).resolve()
        try:
            import torch
            from peft import PeftModel
            from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
            from experiments.local_inference_perf.efficient_sdpa import (
                register_efficient_sdpa,
            )
        except ImportError as exc:
            raise RuntimeError(
                "cp118 transport requires torch, transformers, peft, "
                "and bitsandbytes"
            ) from exc

        if not torch.cuda.is_available():
            raise RuntimeError("cp118 transport requires a CUDA runtime")
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
            transport="LocalQwenPeftTransport",
            base_repository=BASE_REPOSITORY,
            base_revision=BASE_REVISION,
            adapter_path=str(self._adapter_path),
            requested_device_map=requested_device_map,
        )

        adapter_config = json.loads(
            (self._adapter_path / "adapter_config.json").read_text(encoding="utf-8")
        )
        declared_base = adapter_config.get("base_model_name_or_path")
        if declared_base not in {None, "", BASE_REPOSITORY}:
            raise RuntimeError(
                "cp118 adapter declares a different base model: "
                f"{declared_base!r}"
            )

        # STABLE + PHYSICAL-VRAM-BOUND (R6 amendment): stock SDPA falls to the
        # pathological MATH backend on this torch build (15.3 GiB peak on the
        # long-context case); the validated repeat_kv EFFICIENT_ATTENTION path
        # runs the fused kernels (7.2 GiB).  Registered fail-closed before the
        # model load; the model loads with attn_implementation="efficient_sdpa".
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
        base = AutoModelForCausalLM.from_pretrained(
            BASE_REPOSITORY,
            revision=BASE_REVISION,
            trust_remote_code=False,
            device_map=requested_device_map,
            quantization_config=quantization,
            torch_dtype=compute_dtype,
            attn_implementation="efficient_sdpa",
        )
        base_placement = audit_single_cuda_placement(
            base, expected_device_index=cuda_device_index
        )
        _emit_lifecycle(
            lifecycle_event,
            "base_model_load_complete",
            placement=base_placement,
        )
        _emit_lifecycle(lifecycle_event, "adapter_load_start")
        self.model = PeftModel.from_pretrained(
            base,
            str(self._adapter_path),
            is_trainable=False,
        )
        self.model.eval()
        final_placement = audit_single_cuda_placement(
            self.model, expected_device_index=cuda_device_index
        )
        self.placement_audit = {
            "base": base_placement,
            "final_peft_model": final_placement,
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
            "adapter_load_complete",
            placement=final_placement,
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


__all__ = [
    "LocalQwenPeftTransport",
    "adapter_tree_identity",
    "validate_adapter_identity",
    "BASE_REPOSITORY",
    "BASE_REVISION",
    "GENERATION_CONFIG",
]
