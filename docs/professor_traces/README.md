# Professor Debug Traces — R6 fine-tuned debugger (checkpoint-30)

Structured, machine-validatable JSON traces of **real debugger executions** by the
project's fine-tuned debugger model.

## 1. What these traces are

Each trace JSON captures one complete, real debugger repair session, derived
**deterministically** from the frozen final-execution evidence of the accepted
R6 evaluation. Every field comes from what actually happened and what the model
actually authored or observed:

- the sanitized **failure reproduction** (production exception only, never
  hidden test source);
- the ordered **debugger lifecycle**: model-authored commands
  (`break` / `stack` / `locals` / `next` / `diagnosis` / `patch`) mapped to the
  real tool actions, with the observed production-region file / function / line
  and pause generations;
- the **error localization** — *"where is the error?"* — derived from real
  debugger observations (breakpoint pause, stack frames, locals, step
  progression), never from an oracle and never from a model guess;
- the **model-authored diagnosis** (exact text from the run record);
- the **repair attempt sequence** with candidate hashes and the independent
  verifier outcome per attempt;
- the **final independent verification**: outcome, fail-to-pass / pass-to-pass
  counts, full-suite status, syntax status, canonical-fixture integrity, and
  workspace cleanup.

Fields that are not present in the evidence are exported with explicit
`null` / `NOT_RECORDED` / `NOT_APPLICABLE` semantics — nothing is fabricated.

## 2. Real executions

These traces are **not** synthetic examples. They are derived from the frozen
evidence records of the actual debugger runs (`debugger-interaction-v2-r5-evidence`
schema, produced by the project's PDB-first debugger harness) that were executed
on a local NVIDIA RTX 5070 Ti Laptop GPU under a bounded, device-pinned
evaluation policy.

## 3. Which model produced them

- **Base model:** `Qwen/Qwen2.5-Coder-7B-Instruct`
  (`c03e6d358207e414f1eca0bb1891e29f1db0e242`)
- **Fine-tuned by this project:** SFT/QLoRA debugger training
  (`train_qlora.py`, `run_bounded_training.py`) over the disjoint QuixBugs
  training split.
- **Selected checkpoint:** `checkpoint-30`
  - adapter model SHA256
    `7ef5d70ab8691ea02f005ec567901932e08fb94b28ebbfab5b175a94ebb492bd`
  - adapter config SHA256
    `92ddf91e67b116a6730792722d6ee93dffeaac152901cd954389615e50cbd44e`

## 4. Primary result — 8/8 RESOLVED

`r6_validation/` contains one trace for each of the **eight contamination-safe
disjoint QuixBugs validation tasks**. All eight were **independently
verifier-confirmed RESOLVED** (strict per-task pass: fail-to-pass repaired,
pass-to-pass intact, full suite consistent, canonical fixture unchanged,
workspace cleaned).

## 5. Checkpoint selection did not use the final holdout

Checkpoint-30 was selected **only** from the disjoint validation evidence
(`holdout_used_for_checkpoint_selection = false`); the final holdout began only
after the selection record was frozen.

## 6. Final holdout — interrupted by hardware, not claimed complete

`r6_holdout_partial/` is a **separate, clearly labeled appendix** containing
the two surviving completed rows of the final five-task curated holdout:

- `curated-none-handling-001` — `RESOLVED`
- `curated-off-by-one-002` — `BREAKING_RESOLVED` (fail-to-pass repaired `1/1`,
  pass-to-pass regression remains `1/2`; the independent verifier **rejected**
  an apparently useful repair — this is preserved honestly, not counted as
  success)

The remaining three holdout tasks were interrupted by a local hardware
power-off and are recorded only as lifecycle/incompletion metadata
(`INCOMPLETE_HARDWARE_STOP`). **The final five-task holdout is not claimed as
complete**, and it is never mixed into the primary 8/8 success set.

## 7. How to read a trace

| Section | Meaning |
|---|---|
| `failure_reproduction` | Sanitized baseline failure (reproduced before repair) |
| `debugger_trace` | Ordered model commands → real tool actions, with observed production file/function/line, frames, and pause generations |
| `error_localization` | *Where the error is* — from real debugger observations, with evidence basis (observation ids), plus `pause_generation` |
| `diagnosis` | The model's own authored root-cause statement (exact text) |
| `repair_attempts` | Each patch attempt (representation, candidate hashes) and its independent verifier outcome |
| `final_verification` | Outcome, F2P/P2P counts, full suite, syntax, canonical integrity, cleanup |

## 8. How the JSON was generated — deterministic regeneration

The traces are **not hand-edited JSON**. They are produced by a deterministic,
fail-closed exporter from the frozen accepted evidence:

```bash
python -m agentic_debugger.evaluation.professor_trace_r6 \
    --output-dir docs/professor_traces
```

Regeneration:

1. **verifies** every evidence file hash against the frozen accepted identity
   (any missing/mismatched evidence fails closed);
2. **builds** one schema-validated trace per task
   (`professor_debug_trace_v1`);
3. **audits** every exported trace with the accepted actual-output anti-leakage
   scanner (hidden test source / node ids / assertion expressions / expected
   literals / oracle root cause / reference repair / chain-of-thought
   reconstruction are all forbidden — any finding fails closed);
4. **writes** traces, indexes, and manifests with stable key order — identical
   frozen evidence produces **byte-identical** output.

The accepted evidence identity (portable logical identities + SHA256, no
machine-local capture paths) is recorded in `source_evidence_manifest.json`;
per-trace SHA256 in `trace_sha_manifest.json`; the audit result per document
in `professor_safe_audit.json`.

## 9. Files

```
docs/professor_traces/
  README.md
  professor_debug_trace_schema_v1.json   # machine-validatable schema
  r6_validation_index.json               # index over the primary 8/8 set
  r6_validation/                         # 8 traces (primary success set)
  r6_holdout_partial_index.json          # index over the partial holdout
  r6_holdout_partial/                    # 2 surviving holdout rows
  source_evidence_manifest.json          # accepted evidence identity (portable
                                         #   logical identities, no machine paths)
  trace_sha_manifest.json                # per-trace SHA256
  professor_safe_audit.json              # professor-safe leakage audit result
```

Total professor trace set: **10 traces** (8 primary validation + 2 partial
holdout).

## 10. Scientific claims boundary

- The **8/8 disjoint validation** result is the accepted completed R6 evidence.
- The **final five-task holdout** was interrupted by local hardware and is
  **not** claimed complete.
- Validation evidence and final-holdout evidence are distinct and are kept
  distinct in this export.
