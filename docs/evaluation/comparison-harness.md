# Unified Comparison Harness v1

**Date:** 2026-08-06
**Branch:** `goal/friday-rag-comparison-v1`
**Baseline:** `e92634e3dc016276d22ab9b9197adf4b28abbeb1`
**Package:** `agentic_debugger/comparison/`
**Scope:** one normalized experiment schema comparing base generation,
fine-tuned generation, RAG-assisted and full agentic conditions through the
existing verifier, controller, demo runner, event/replay and workspace
contracts. No second agent framework, no second verifier, no second campaign
format.

## 1. Conditions

| Condition | Mode | Execution path |
|---|---|---|
| `base` | imported | `generation-artifact-v1` → strict parser → `EvaluationVerifier` |
| `fine-tuned` | imported | same import path (adapter identity optional) |
| `agentic` | native | accepted demo runner path (controller + tools + verifier + replay + cleanup) |
| `rag-assisted` | native | identical native path plus explicit bounded `RagContext` |

Conditions are declared per experiment (unique identities, one declared
baseline condition that must be among them); all four are not required.

## 2. Imported generation artifacts (`generation-artifact-v1`)

One self-contained JSON file per attempt: experiment/attempt/condition/task
identity, model repository and revision, optional adapter identity,
prompt-contract identity, generation configuration, bounded raw output
(≤ 256 KiB, preserved exactly), optional patch text (≤ 128 KiB), optional
runtime/memory/cost/token fields, provenance. Strict rules: unknown or
missing fields rejected; NaN/Infinity rejected; oversized raw output
rejected; duplicate attempt identities rejected at experiment level;
malformed patches are never normalized — the existing strict parser and the
verifier decide; patches apply only inside a disposable workspace;
verification happens only through `EvaluationVerifier`; canonical fixture
immutability is preserved and reported; workspaces are always cleaned.
Synthetic fixtures MUST carry provenance generator
`offline-deterministic-demo`; they are infrastructure evidence, not model
performance.

## 3. Native agentic mode

Reuses `run_demo_case` (controller → events → replay → verifier → cleanup →
offline guard) with the static-baseline policy. The `rag-assisted` condition
passes the same candidate patch and identical directives; RAG changes only
retrieval/citation metrics. A fail-closed parity check requires identical
patch digest and verifier outcome between the two native conditions and
exactly one RAG-enabled side.

## 4. Normalized metrics (per attempt)

generation produced; strict valid patch; correct target file;
localization/target-symbol result where ground truth exists; F2P passed/
total; P2P passed/total; verifier outcome; normalized failure category
(closed vocabulary: NO_GENERATION, NO_PATCH, PATCH_INVALID,
PATCH_NOT_APPLIED, SYNTAX_FAILED, VERIFIER_FAILED, NO_OP, F2P_NOT_PASSED,
P2P_REGRESSION, NOT_REPRODUCED, UNCLASSIFIED; `None` = RESOLVED); runtime;
cost/tokens when supplied; retrieval count/bytes/latency (RAG only); replay
validity; cleanup status; canonical source unchanged; provider/network
attempt counts.

Optional root-cause explanation evidence uses a nested, independently
validated `root-cause-assessment-v1` record in attempt provenance. Aggregates,
deltas, and CSV expose assessment coverage, explicit missingness, closed
outcomes, and correct rates. Patch success and lexical similarity to hidden
oracle text are never treated as root-cause correctness. See
`docs/architecture/root-cause-metric.md`.

## 5. Report outputs

* canonical JSON (`experiment.json`, `comparison-v1`) with deterministic and
  nondeterministic sections separated (`environment`, `timing`);
* one-row-per-attempt CSV (`comparison.csv`) with fixed column order;
* Markdown report (`comparison.md`): per-task results, per-condition
  aggregates, delta against the declared baseline, notes;
* per-condition aggregates (resolved rate, valid patch, F2P/P2P totals,
  localization, root-cause assessment coverage/outcomes, cleanup, offline
  counters, failure categories);
* delta entries (aggregate + per-task) against the baseline condition.

Every report states: **"This deterministic pilot is not statistically
representative."** plus the same-patch parity note and the synthetic-identity
note.

## 6. CLI

```
python -m agentic_debugger.comparison build-index --corpus-root DIR [--mode fixture|repo] [--task-id X] --output-root DIR
python -m agentic_debugger.comparison retrieve --index FILE --query Q --output-root DIR
python -m agentic_debugger.comparison import-attempt --artifact FILE --task-manifest FILE --repo-root DIR --output-root DIR
python -m agentic_debugger.comparison compare --tasks T [T...] --repo-root DIR --output-root DIR
python -m agentic_debugger.comparison export-preferences --results FILE --task-manifest FILE [FILE...] --output-root DIR
python -m agentic_debugger.comparison demo --repo-root DIR --output-root DIR
```

All output roots are explicit and uniquely claimed (create-once semantics);
nothing is ever written into tracked source directories.

## 7. Determinism and honesty

* Deterministic view = document minus `environment`/`timing` and the
  per-attempt wall-clock fields; proven byte-identical across two runs.
* The parity demo does **not** establish a causal RAG performance
  improvement.
* Synthetic base/tuned identities never imply actual QLoRA evaluation.
* A five-task pilot is not statistically representative; the demo runs two
  curated tasks.

## 8. Contract hardening (repair 1, 2026-08-06)

* **Raw-output-to-patch binding**: the candidate patch is always derived
  from the artifact's `raw_output` through a strict `patch_extraction`
  contract — `exact` (patch-only output, `patch == raw_output`) or
  `substring` (exact UTF-8 byte offsets into the raw output). At load the
  importer reconstructs the derived patch and requires substring equality,
  recomputed SHA-256 equality and in-bounds offsets; an unrelated passing
  patch, a one-byte modification, or offsets outside the raw output are
  rejected. A model comparison never credits a patch that was not produced
  in the recorded raw output.
* **Attempt roles**: every attempt carries a strict `role` —
  `evaluation` (primary) or `preference-fixture` (auxiliary). At most one
  primary attempt per `(task_id, condition_id)` (duplicates rejected).
  Primary aggregates, baseline deltas and the report's performance metrics
  use evaluation-role attempts only; auxiliary attempts are identified in
  JSON/CSV/Markdown and can never lower the base rate or manufacture a
  tuned-over-base gain (regression-tested). The deterministic demo runs
  exactly two primary attempts per condition and two labeled auxiliary
  negative attempts.
* **Recursive JSON bounds**: free-form payloads (generation configuration,
  provenance, verifier evidence, environment/timing/notes) are validated
  recursively with bounded depth/entries/strings, finite numbers only, and
  bounded canonical serialized bytes — nested NaN/Infinity fails at schema
  load, never later during identity generation.
* **Imported-attempt invariants**: strict parse / apply / syntax / verifier
  phases are distinguished in the failure-category vocabulary; the
  `EvaluationInputError` branch always initializes `evaluation`
  (no `UnboundLocalError`); response storage uses a marker-inclusive
  UTF-8-safe 64 KiB bound so any valid artifact yields a valid attempt
  record; artifact telemetry (runtime, memory bytes, cost, tokens) is
  carried into the normalized record; external generation provider/network
  telemetry is separated from local verification offline counters and never
  fabricated as zero.
* **AttemptRecord invariants**: identifier patterns; `passed <= total`;
  valid-patch identity consistency; response hash matching; closed
  failure-category vocabulary; cleanup/verifier consistency.
