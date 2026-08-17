# R6 — Debugger-oriented fine-tuning and disjoint validation

## Identity

Debugger-trajectory SFT / QLoRA on
`Qwen/Qwen2.5-Coder-7B-Instruct` @
`c03e6d358207e414f1eca0bb1891e29f1db0e242`, then executable debugger
evaluation of the selected adapter. Preserved implementation/evidence
commit recorded in closeout: `4610785713832daaba6aa133374506a2d200391a`.

Contracts: [`r6_train_contract.json`](r6_train_contract.json),
[`r6_eval_contract.json`](r6_eval_contract.json) (eval schema
`debugger-interaction-v2-r5`, experiment_id
`debugger-interaction-v2-r6-model-evaluation`, interface `r6.8`).

## What was run

A debugger-trajectory SFT dataset was built from the pinned QuixBugs
revision `4257f44b0ff1181dedaedee6a447e133219fcebf`: 29/40 usable fixtures,
frozen 21 train / 8 validation split, 164 train / 61 validation SFT pairs
(token statistics: p50 ≈ 832, p90 ≈ 1607, p95 ≈ 1761, max 2415)
(`docs/project-closeout.md` §3).

checkpoint-30 was selected from the disjoint validation only
(`holdout_used_for_checkpoint_selection = false`):

- adapter model SHA256 `7ef5d70ab8691ea02f005ec567901932e08fb94b28ebbfab5b175a94ebb492bd`
- adapter config SHA256 `92ddf91e67b116a6730792722d6ee93dffeaac152901cd954389615e50cbd44e`

The stronger five-task curated final holdout was started after that freeze
and was interrupted by local hardware power-offs.

## Main result

The project-fine-tuned 7B debugger achieved **8/8 RESOLVED** on the frozen
task-disjoint QuixBugs validation using real debugger/tool execution and
independent verification (97 model calls, 64,783 tokens, 841,702 ms task
runtime, zero row errors).

Final five-task curated holdout — **INCOMPLETE_HARDWARE_STOP**:

- `curated-none-handling-001` — RESOLVED (F2P 1/1, P2P 2/2, strict pass)
- `curated-off-by-one-002` — BREAKING_RESOLVED (F2P 1/1, P2P 1/2; verifier
  rejected an apparently useful repair)
- `curated-wrong-branch-003` interrupted during a model request
- `curated-mutation-alias-004` and `curated-caller-callee-005` never started

This is not 2/5, not 1/5, and not a failed 5-task benchmark. Holdout
leakage=0 was not established (only the two completed tasks' 18 prompts
show 0 findings).

## Accepted interpretation

R6 is the strongest accepted tuned-debugger validation in this repository.
Fine-tuning is **not** claimed to have causally improved over a matched
base — no matched-base R6 ablation exists. The incomplete holdout is a
closed hardware-stop boundary, not a score.

## Limitations

- No matched-base ablation.
- Holdout incomplete; no sustained local GPU rerun is scheduled.
- Regenerable gold/SFT trees are gitignored; the tracked frozen capsule
  under `runs/frozen/` is the regeneration source for
  `docs/professor_traces/`.

## Authoritative sources

- `runs/frozen/` (`capsule_manifest.json`, validation evidence, partial
  holdout evidence, ancillary reports)
- `docs/professor_traces/` (10 traces; do not rename that directory)
- `docs/project-closeout.md` §3 and §6
- `docs/results-index.md`
