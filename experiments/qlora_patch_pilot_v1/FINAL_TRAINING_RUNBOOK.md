# Final QLoRA Training Runbook — qlora-patch-pilot-v1

Methodology: **Owner-delegated independent FirstMate AI audit; not human review.**

This runbook prepares the single authorized bounded final-training execution.
Final training is **prepared but not executed** by this change set.

## Authorization

- Record: `experiments/qlora_patch_pilot_v1/final_training_authorization.json`
- Scope: `final_training_only`; `authorized: true`
- Held-out generation: **false** (never implied by this record)
- Base-versus-tuned evaluation: **false**
- Approver: `FirstMate / GPT-5.6 Thinking` (`owner-delegated FirstMate gate`)
- Audit result bound: 39/11 accepted-packet, 0/25 rejected-packet
- Corpus accepted: unchanged 1,000 train / 150 validation (minimum tier), no
  top-up; the 39/50 accepted-sample uphold rate is a disclosed quality signal,
  not a claim of corpus cleanliness or precision.

## Prerequisites (exact)

1. Repository checkout on the Colab VM at
   `/content/agentic-debugging-internship`, branch
   `experiment/qlora-patch-pilot-v1` (bundle clone or git clone), with a
   CRLF-faithful checkout (`git config core.autocrlf true` + re-checkout) so
   the frozen fixture tree digests match — see the smoke-run deviation record.
2. Google Drive mounted; experiment root
   `MyDrive/agentic-debugging/qlora_patch_pilot_v1/` with:
   - `corpus/` — the frozen build (`train.jsonl`, `validation.jsonl`,
     `corpus_summary.json`, `dedup_report.json`, audit packets);
   - `independent-audit/firstmate_independent_audit_completed.csv` — the
     completed independent audit;
   - `model-cache/` — the pinned Qwen 7B cache (reused; ~15 GB).
3. Drive capacity ≥ ~25 GB free (model cache + adapter + logs).

## Drive paths

- Corpus: `.../qlora_patch_pilot_v1/corpus/`
- Completed audit: `.../qlora_patch_pilot_v1/independent-audit/firstmate_independent_audit_completed.csv`
- Output root: `.../qlora_patch_pilot_v1/final-training/`
- Model cache: `.../qlora_patch_pilot_v1/model-cache/`

## Notebook order

`experiments/qlora_patch_pilot_v1/colab/agentic_debugging_qlora_final_training.ipynb`

1. Install frozen user-space dependencies (Cell 1).
2. Mount Drive, identify repository and assert corpus files (Cell 2).
3. Verify frozen identities (`verify-freeze`, LOCKED 25/25) and freeze gates
   (Cell 3).
4. **Validate the final-training authorization record** via
   `validate-final-training-auth` (Cell 4) — fail-closed before any model load.
5. **Re-run the independent audit validation** against the completed audit CSV
   (Cell 5) — fail-closed before any model load.
6. Verify corpus counts (1,000/150) and repository-disjointness from the saved
   records; **never rebuild or top up** (Cell 6).
7. Record runtime identity; require CUDA (Cell 7).
8. Load frozen tokenizer and the frozen train/validation JSONL; tokenize
   completion-only (Cell 8).
9. Load the pinned `Qwen/Qwen2.5-Coder-7B-Instruct` at
   `c03e6d358207e414f1eca0bb1891e29f1db0e242` in frozen 4-bit NF4 QLoRA (Cell 9).
10. Train **exactly one epoch** with the frozen SFT configuration (Cell 10);
    record wall-clock elapsed, peak CUDA memory allocated/reserved, and the
    training log history.
11. Save adapter, tokenizer, trainer state, log history, final summary, sizes
    and SHA-256 identities, and the external artifact manifest (Cell 11).
12. Reload the saved adapter and verify it loads (Cell 12).
13. Hard gate: held-out generation authorization remains false; print
    `FINAL_TRAINING_COMPLETE_AWAITING_FIRSTMATE_REVIEW` (Cell 13).

## Gates before model load (all must pass)

- freeze LOCKED 25/25;
- authorization record validates (`final_training_only`, authorized true,
  held-out false, approver correct, configuration identities match, audit
  result 39/11/0/25, corpus 1,000/150, no top-up);
- independent audit validation `COMPLETE` (39/11/0/25, reviewer
  `FirstMate / GPT-5.6 Thinking`, type `independent_ai_reviewer`);
- corpus summary counts and empty repository overlap.

## Expected artifacts (under the run directory)

Every attempt writes exclusively under
`.../qlora_patch_pilot_v1/final-training/runs/<run-id>/`:

- `run_context.json` + `INCOMPLETE` marker (written before model load),
- `adapter-final/` (adapter + tokenizer files),
- `trainer-output/` (training run outputs),
- `trainer_state.json`, `training_log_history.json`,
- `reload_verification.json`,
- `final_training_summary.json` (full provenance chain: run ID, authorization
  SHA-256, corpus and audit artifact identities, model/config identities,
  loss, elapsed seconds, peak CUDA allocated and reserved bytes, GPU/package
  versions, adapter hashes, reload result, held-out state, final status),
- `runtime_environment.json`, `external_artifacts.json` (generated only from
  the active run directory),
- on completion: `run_status.json` (status COMPLETE) + `RUN_COMPLETE` marker
  (the `INCOMPLETE` marker is removed).

A run directory is **unambiguously incomplete** while `INCOMPLETE` is present
or `run_status.json` is absent; it is complete only when `run_status.json`
says COMPLETE and `RUN_COMPLETE` exists.

## Expected runtime/memory fields

- `elapsed_seconds` (wall clock of the training loop);
- `peak_cuda_memory_allocated_bytes` / `peak_cuda_memory_reserved_bytes`
  (`torch.cuda.max_memory_allocated/reserved`);
- `gpu`, `cuda_runtime`, `torch`, `python`, pinned package versions.

## How to stop safely and restart policy

- Do not interrupt mid-epoch unless necessary; the adapter is only saved at the
  end. An interrupted run leaves `INCOMPLETE` in its run directory and no
  adapter — the run is preserved as evidence, never renamed to appear complete
  and never deleted by the notebook.
- **Restart policy (required):**
  1. preserve the incomplete run directory untouched (evidence);
  2. do not rename it, do not delete it during the notebook;
  3. start a fresh Colab runtime when appropriate;
  4. rerun the notebook from the beginning;
  5. a new run ID is generated (UTC timestamp + authorization-hash component)
     and a new empty run directory is created; the notebook fails closed if
     that exact run directory already exists;
  6. every authorization, corpus and audit gate is re-run from the real files;
  7. never copy adapter/trainer artifacts from a failed run into a new run;
  8. never manually fill missing summary fields;
  9. when diagnosing a failure, return both the successful run directory and
     any relevant failed-run record to FirstMate.
- A completed adapter is accepted only after FirstMate reviews the complete
  provenance package (summary, manifest, run status, reload verification).

## What not to rerun

- dataset acquisition; corpus construction; audit validation decisions;
  model-cache download (reuse); the smoke notebook cells; anything touching the
  frozen corpus files.

## What must be returned to FirstMate

- `final_training_summary.json`, `external_artifacts.json`, `trainer_state.json`,
  `training_log_history.json`, adapter directory listing with sizes/SHA-256,
  runtime environment, and the notebook execution record.

## Boundary

Held-out generation remains forbidden. Do not open, load, or generate any
held-out task content in this run; base-versus-tuned evaluation and any further
training require a separate FirstMate review.
