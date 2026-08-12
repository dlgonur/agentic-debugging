#!/usr/bin/env python3
"""R6 — QLoRA SFT of the debugger-oriented model (local GPU).

Frozen treatment (mirrors the accepted cp118 D2 training configuration):

- base: Qwen/Qwen2.5-Coder-7B-Instruct @ c03e6d358207e414f1eca0bb1891e29f1db0e242
- QLoRA: NF4 double-quant, LoraConfig r=16 alpha=32 dropout=0.05, all
  linear modules, task CAUSAL_LM, lr 2e-4, cosine, warmup_ratio 0.03,
  weight_decay 0.01, max_grad_norm 1.0, microbatch 1, gradient
  accumulation 16, gradient checkpointing, paged_adamw_8bit, fixed seeds
- completion-only loss over observable model outputs (prompt tokens are
  masked with -100); the prompt is the EXACT r5.9 bridge system+user prompt
  and the completion is the EXACT accepted directive — never hidden
  chain-of-thought

Data: experiments/r6_debugger_training/sft/sft_{train,validation}.jsonl
(task-disjoint QuixBugs trajectories; the five R6 curated holdouts are
structurally absent).  Outputs: training runs under
experiments/r6_debugger_training/runs/train-<run-id>/ with per-step
checkpoints (adapter), training config, provenance, and eval-loss curve.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

BASE_REPOSITORY = "Qwen/Qwen2.5-Coder-7B-Instruct"
BASE_REVISION = "c03e6d358207e414f1eca0bb1891e29f1db0e242"

TRAIN_CONFIG: dict[str, Any] = {
    "method": "QLoRA_PEFT_SFT",
    "quantization": {"load_in_4bit": True, "bnb_4bit_quant_type": "nf4",
                     "bnb_4bit_use_double_quant": True},
    "lora": {"r": 16, "alpha": 32, "dropout": 0.05, "bias": "none",
             "target_modules": "all-linear", "task_type": "CAUSAL_LM"},
    "optimizer": {"lr": 2e-4, "weight_decay": 0.01, "max_grad_norm": 1.0,
                  "optim": "paged_adamw_8bit"},
    "schedule": {"scheduler": "cosine", "warmup_ratio": 0.03},
    "data": {"microbatch": 1, "gradient_accumulation_steps": 16,
             "gradient_checkpointing": True,
        "gradient_checkpointing_kwargs": {"use_reentrant": False},
        "allocator": "cudaMallocAsync (returns memory to the driver; bounds "
                     "WDDM committed memory; set via "
                     "PYTORCH_CUDA_ALLOC_CONF=backend:cudaMallocAsync)",
             "completion_only_loss": True},
    "seed": 20260812,
}


@dataclass(frozen=True)
class SftExample:
    system_prompt: str
    user_prompt: str
    completion: str
    task_id: str


def load_examples(path: Path) -> list[SftExample]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows.append(SftExample(
            system_prompt=row["system_prompt"],
            user_prompt=row["user_prompt"],
            completion=row["completion"],
            task_id=row["task_id"],
        ))
    return rows


def build_training_run(
    run_id: str,
    output_root: Path,
    sft_dir: Path,
    *,
    epochs: int,
    max_length: int,
    save_steps: int,
    save_total_limit: int,
) -> Path:
    """Materialize the frozen training run directory + provenance."""
    run_dir = output_root / run_id
    if run_dir.exists():
        raise RuntimeError(f"training run already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    train_examples = load_examples(sft_dir / "sft_train.jsonl")
    validation_examples = load_examples(sft_dir / "sft_validation.jsonl")

    def _sha256_file(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    provenance = {
        "schema_version": "r6-qlora-sft-run-v1",
        "run_id": run_id,
        "base_repository": BASE_REPOSITORY,
        "base_revision": BASE_REVISION,
        "train_config": TRAIN_CONFIG,
        "epochs": epochs,
        "max_length": max_length,
        "save_steps": save_steps,
        "save_total_limit": save_total_limit,
        "train_examples": len(train_examples),
        "validation_examples": len(validation_examples),
        "train_tasks": sorted({e.task_id for e in train_examples}),
        "validation_tasks": sorted({e.task_id for e in validation_examples}),
        "data_hashes": {
            "sft_train.jsonl": _sha256_file(sft_dir / "sft_train.jsonl"),
            "sft_validation.jsonl": _sha256_file(sft_dir / "sft_validation.jsonl"),
            "sft_manifest.json": _sha256_file(sft_dir / "sft_manifest.json"),
        },
        "holdout_excluded": "curated-none-handling-001, curated-off-by-one-002, "
                            "curated-wrong-branch-003, curated-mutation-alias-004, "
                            "curated-caller-callee-005 (structurally absent)",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (run_dir / "training_provenance.json").write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="R6 QLoRA SFT (local GPU)")
    parser.add_argument("--run-id", type=str, default=None)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--max-length", type=int, default=1792)
    parser.add_argument("--save-steps", type=int, default=10)
    parser.add_argument("--save-total-limit", type=int, default=4)
    parser.add_argument("--preview", action="store_true",
                        help="materialize the run dir + provenance WITHOUT loading the model")
    parser.add_argument("--preflight-steps", type=int, default=0,
                        help="bounded memory preflight: load the EXACT intended config, "
                             "run this many training steps + one eval, and fail closed if the "
                             "working set is not physically VRAM-bound")
    args = parser.parse_args()

    run_id = args.run_id or f"r6-sft-{time.strftime('%Y%m%d-%H%M%S')}"
    sft_dir = THIS_FILE.parent / "sft"
    output_root = THIS_FILE.parent / "runs"
    run_dir = build_training_run(
        run_id, output_root, sft_dir,
        epochs=args.epochs, max_length=args.max_length,
        save_steps=args.save_steps, save_total_limit=args.save_total_limit,
    )
    print(f"run dir: {run_dir}")
    if args.preview:
        return 0

    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import (
        AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig,
        Trainer, TrainingArguments,
    )
    from transformers.trainer_utils import set_seed

    if not torch.cuda.is_available():
        raise RuntimeError("training requires a CUDA runtime")
    set_seed(TRAIN_CONFIG["seed"])
    random.seed(TRAIN_CONFIG["seed"])

    # STABLE + PHYSICAL-VRAM-BOUND (R6 continuation amendment):
    # - efficient SDPA (validated repeat_kv path; stock SDPA falls to the
    #   pathological MATH backend on this torch build: 15.3 GiB vs 7.2 GiB
    #   peak on the long-context case) — registered fail-closed;
    # - explicit GPU-only placement assertion (never silent CPU/disk spill);
    # - max_length selected from the measured dataset distribution (drops are
    #   recorded, never truncated).
    from experiments.local_inference_perf.efficient_sdpa import (
        register_efficient_sdpa,
    )
    sdpa_registration = register_efficient_sdpa()

    compute_dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    quantization = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=compute_dtype,
    )
    tokenizer = AutoTokenizer.from_pretrained(
        BASE_REPOSITORY, revision=BASE_REVISION, trust_remote_code=False,
    )
    model = AutoModelForCausalLM.from_pretrained(
        BASE_REPOSITORY, revision=BASE_REVISION, trust_remote_code=False,
        device_map="auto", quantization_config=quantization,
        torch_dtype=compute_dtype,
        attn_implementation="efficient_sdpa",
    )
    # Placement guard: fail closed unless the ENTIRE model is on a CUDA
    # device (no uncontrolled CPU/disk spill that would silently degrade to
    # paging).  accelerate reports device_map values as int device indices
    # (0) or strings ("cuda:0", "cpu", "disk", "offload").
    hf_device_map = getattr(model, "hf_device_map", None) or {}
    offloaded = {}
    for module_name, device in hf_device_map.items():
        if isinstance(device, int):
            if device < 0:
                offloaded[module_name] = device
        elif not str(device).startswith("cuda"):
            offloaded[module_name] = device
    if offloaded:
        raise RuntimeError(
            f"model placement offloaded {len(offloaded)} modules off GPU: "
            f"{sorted(str(k) for k in offloaded)[:5]}... — refusing unsafe placement"
        )
    # Minimal QLoRA preparation (STABLE + PHYSICAL-VRAM-BOUND amendment):
    # the standard peft ``prepare_model_for_kbit_training`` additionally
    # casts every non-4bit parameter (embeddings + lm_head, ~2.1 GB on this
    # model) to fp32, which pushes the working set past physical VRAM on this
    # 12.2 GB laptop GPU.  The equivalent minimal protocol is applied instead:
    # freeze the base, disable KV caching, enable input-requires-grad (for
    # gradient checkpointing with frozen embeddings) and gradient
    # checkpointing.  LoRA adapters still receive gradients; the base stays
    # frozen; layernorms/embeddings remain bf16 (documented deviation, small
    # 3-epoch SFT, recorded in the training provenance).
    for param in model.parameters():
        param.requires_grad = False
    model.config.use_cache = False
    if hasattr(model, "enable_input_require_grads"):
        model.enable_input_require_grads()
    model.gradient_checkpointing_enable(
        gradient_checkpointing_kwargs={"use_reentrant": False}
    )
    lora_config = LoraConfig(
        r=TRAIN_CONFIG["lora"]["r"],
        lora_alpha=TRAIN_CONFIG["lora"]["alpha"],
        lora_dropout=TRAIN_CONFIG["lora"]["dropout"],
        bias=TRAIN_CONFIG["lora"]["bias"],
        target_modules="all-linear",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"trainable params: {trainable} / {total} "
          f"({100.0 * trainable / total:.3f}%)")

    # Record the resolved memory configuration in the run provenance.
    provenance_path = run_dir / "training_provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["memory_config"] = {
        "attention": "efficient_sdpa (validated repeat_kv EFFICIENT_ATTENTION "
                     "backend; stock SDPA falls to the pathological MATH "
                     "backend on this torch build)",
        "preparation": "minimal QLoRA protocol (base frozen, use_cache=False, "
                       "input-requires-grad, gradient checkpointing); the peft "
                       "fp32 cast of embeddings/lm_head (~2.1 GB) is "
                       "deliberately skipped to stay physically VRAM-bound",
        "microbatch": TRAIN_CONFIG["data"]["microbatch"],
        "gradient_accumulation_steps": TRAIN_CONFIG["data"]["gradient_accumulation_steps"],
        "gradient_checkpointing": True,
        "gradient_checkpointing_kwargs": {"use_reentrant": False},
        "allocator": "cudaMallocAsync (returns memory to the driver; bounds "
                     "WDDM committed memory; set via "
                     "PYTORCH_CUDA_ALLOC_CONF=backend:cudaMallocAsync)",
        "use_cache": False,
        "quantization": "NF4 double-quant, bf16 compute",
        "optimizer": TRAIN_CONFIG["optimizer"]["optim"],
        "placement": "device_map=auto with GPU-only assertion (no CPU/disk spill)",
        "max_length": args.max_length,
        "measured_wddm_dedicated_peak_gib": 11.49,
        "measured_wddm_headroom_gib": 0.74,
        "sequence_length_selection": "measured SFT distribution: p50=832 p75=1073 "
                                     "p90=1607 p95=1761 max=2415; max_length "
                                     "chosen per preflight VRAM measurement; "
                                     "examples exceeding it are dropped and "
                                     "recorded, never truncated",
    }
    provenance_path.write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

    train_examples = load_examples(sft_dir / "sft_train.jsonl")
    validation_examples = load_examples(sft_dir / "sft_validation.jsonl")

    def _tokenize_pair(examples: list[SftExample]) -> dict[str, Any]:
        prompts = []
        completions = []
        for ex in examples:
            rendered = tokenizer.apply_chat_template(
                [{"role": "system", "content": ex.system_prompt},
                 {"role": "user", "content": ex.user_prompt}],
                tokenize=False, add_generation_prompt=True,
            )
            prompts.append(rendered)
            completions.append(ex.completion)
        batch = tokenizer(
            prompts, add_special_tokens=False, padding=False, truncation=False,
        )
        completion_batch = tokenizer(
            completions, add_special_tokens=False, padding=False, truncation=False,
        )
        records = []
        dropped: list[dict[str, Any]] = []
        for i in range(len(examples)):
            prompt_ids = batch["input_ids"][i]
            completion_ids = completion_batch["input_ids"][i]
            total_len = len(prompt_ids) + len(completion_ids)
            if total_len > args.max_length:
                dropped.append({
                    "task_id": examples[i].task_id,
                    "controller_state": "?",
                    "total_tokens": total_len,
                    "reason": f"exceeds max_length {args.max_length}",
                })
                continue
            input_ids = prompt_ids + completion_ids
            labels = [-100] * len(prompt_ids) + completion_ids
            records.append({"input_ids": input_ids, "labels": labels})
        return {"records": records, "dropped": dropped}

    train_out = _tokenize_pair(train_examples)
    validation_out = _tokenize_pair(validation_examples)
    train_records = train_out["records"]
    validation_records = validation_out["records"]
    dropped = train_out["dropped"] + validation_out["dropped"]
    if dropped:
        print(f"DROPPED {len(dropped)} examples exceeding max_length "
              f"{args.max_length}: {dropped}")
        provenance_path = run_dir / "training_provenance.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance["dropped_examples"] = dropped
        provenance_path.write_text(
            json.dumps(provenance, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )

    class Collator:
        def __call__(self, features):
            max_len = max(len(f["input_ids"]) for f in features)
            input_ids = []
            labels = []
            attention = []
            for f in features:
                pad = max_len - len(f["input_ids"])
                input_ids.append(f["input_ids"] + [tokenizer.pad_token_id] * pad)
                labels.append(f["labels"] + [-100] * pad)
                attention.append([1] * len(f["input_ids"]) + [0] * pad)
            return {
                "input_ids": torch.tensor(input_ids, dtype=torch.long),
                "labels": torch.tensor(labels, dtype=torch.long),
                "attention_mask": torch.tensor(attention, dtype=torch.long),
            }

    train_dataset = _ArrayDataset(train_records)
    validation_dataset = _ArrayDataset(validation_records)

    steps_per_epoch = max(
        1, len(train_records) // TRAIN_CONFIG["data"]["gradient_accumulation_steps"]
    )
    training_args = TrainingArguments(
        output_dir=str(run_dir / "trainer"),
        per_device_train_batch_size=TRAIN_CONFIG["data"]["microbatch"],
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=TRAIN_CONFIG["data"]["gradient_accumulation_steps"],
        learning_rate=TRAIN_CONFIG["optimizer"]["lr"],
        weight_decay=TRAIN_CONFIG["optimizer"]["weight_decay"],
        max_grad_norm=TRAIN_CONFIG["optimizer"]["max_grad_norm"],
        optim=TRAIN_CONFIG["optimizer"]["optim"],
        lr_scheduler_type=TRAIN_CONFIG["schedule"]["scheduler"],
        warmup_ratio=TRAIN_CONFIG["schedule"]["warmup_ratio"],
        num_train_epochs=args.epochs,
        gradient_checkpointing=True,
        logging_steps=max(1, steps_per_epoch // 5),
        eval_strategy="steps",
        eval_steps=steps_per_epoch,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=args.save_total_limit,
        seed=TRAIN_CONFIG["seed"],
        fp16=(compute_dtype is torch.float16),
        bf16=(compute_dtype is torch.bfloat16),
        remove_unused_columns=False,
        report_to=[],
        dataloader_pin_memory=False,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=validation_dataset,
        data_collator=Collator(),
    )

    if args.preflight_steps > 0:
        return _run_memory_preflight(
            trainer, run_dir, model, args.preflight_steps,
            sdpa_registration=sdpa_registration,
            train_examples=len(train_records),
            validation_examples=len(validation_records),
            dropped=dropped,
        )

    trainer.train()
    eval_results = trainer.evaluate()
    print(json.dumps({"final_eval": eval_results}, indent=2, ensure_ascii=False))
    return 0


def _run_memory_preflight(
    trainer: Any,
    run_dir: Path,
    model: Any,
    steps: int,
    *,
    sdpa_registration: dict[str, Any],
    train_examples: int,
    validation_examples: int,
    dropped: list[dict[str, Any]],
) -> int:
    """Bounded STABLE + PHYSICAL-VRAM-BOUND preflight with the EXACT intended
    configuration: N consecutive training steps + one evaluation, recording
    torch allocator telemetry and step wall time.  Fails closed unless the
    working set is physically bounded with real headroom."""
    import torch
    from transformers import Trainer, TrainingArguments

    # Bounded preflight arguments: exactly N steps, no eval/save during the
    # loop; one evaluation runs after the steps.
    preflight_args = TrainingArguments(
        output_dir=str(run_dir / "preflight-trainer"),
        per_device_train_batch_size=trainer.args.per_device_train_batch_size,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=trainer.args.gradient_accumulation_steps,
        learning_rate=trainer.args.learning_rate,
        weight_decay=trainer.args.weight_decay,
        max_grad_norm=trainer.args.max_grad_norm,
        optim=trainer.args.optim,
        lr_scheduler_type="constant",
        warmup_ratio=0.0,
        max_steps=steps,
        num_train_epochs=1,
        gradient_checkpointing=True,
        logging_steps=max(1, steps // 2),
        eval_strategy="no",
        save_strategy="no",
        seed=TRAIN_CONFIG["seed"],
        fp16=trainer.args.fp16,
        bf16=trainer.args.bf16,
        remove_unused_columns=False,
        report_to=[],
        dataloader_pin_memory=False,
    )
    preflight_trainer = Trainer(
        model=trainer.model,
        args=preflight_args,
        train_dataset=trainer.train_dataset,
        eval_dataset=trainer.eval_dataset,
        data_collator=trainer.data_collator,
    )

    torch.cuda.reset_peak_memory_stats()
    torch.cuda.synchronize()
    started = time.monotonic()
    try:
        preflight_trainer.train()
        eval_results = preflight_trainer.evaluate()
    except torch.cuda.OutOfMemoryError as exc:
        print(json.dumps({"preflight": "FAIL", "reason": "CUDA OOM", "detail": str(exc)}))
        return 1
    elapsed = time.monotonic() - started

    allocated = torch.cuda.memory_allocated()
    max_allocated = torch.cuda.max_memory_allocated()
    reserved = torch.cuda.memory_reserved()
    max_reserved = torch.cuda.max_memory_reserved()
    props = torch.cuda.get_device_properties(0)
    physical_vram_gib = props.total_memory / (1024 ** 3)

    # Physical-VRAM budget: the amendment targets ~10-11 GiB max dedicated
    # with ~1-2 GiB headroom on this 12.2 GiB GPU.  Fail closed if the peak
    # reservation does not leave that headroom.
    headroom_gib = physical_vram_gib - max_reserved / (1024 ** 3)
    vram_bound = max_reserved / (1024 ** 3) <= 11.0
    telemetry = {
        "preflight": "PASS" if vram_bound else "FAIL",
        "step_count": steps,
        "elapsed_seconds": round(elapsed, 1),
        "step_wall_time_seconds": round(elapsed / max(1, steps), 2),
        "torch_allocated_mib": round(allocated / (1024 ** 2), 1),
        "torch_max_allocated_mib": round(max_allocated / (1024 ** 2), 1),
        "torch_reserved_mib": round(reserved / (1024 ** 2), 1),
        "torch_max_reserved_mib": round(max_reserved / (1024 ** 2), 1),
        "physical_vram_gib": round(physical_vram_gib, 2),
        "headroom_gib": round(headroom_gib, 2),
        "vram_bound_ceiling_gib": 11.0,
        "train_examples_used": train_examples,
        "validation_examples_used": validation_examples,
        "dropped_examples": dropped,
        "attention": "efficient_sdpa",
        "sdpa_registration": {
            k: v for k, v in (sdpa_registration or {}).items()
            if k != "registration"
        },
        "eval_loss": eval_results.get("eval_loss"),
    }
    report_path = run_dir / "preflight_report.json"
    report_path.write_text(
        json.dumps(telemetry, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(telemetry, indent=2, ensure_ascii=False))
    return 0 if vram_bound else 2


class _ArrayDataset:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.records[index]


if __name__ == "__main__":
    sys.exit(main())
