# R5 — Clean base-14B generalized holdout

## Identity

`debugger-interaction-v2-r5`. The accepted scientific treatment is **r5.9**
on `Qwen/Qwen2.5-Coder-14B-Instruct` @
`aedcc2d42b622764e023cf882b6652e646b95671`, `adapter_applied=false`,
`rag_enabled=false`. Reproducibility closeout commit recorded in closeout:
`54828db1d5dec4e95105f1c1d07ba5dd7518060c`.

Frozen contract: [`r5_contract_14b.json`](r5_contract_14b.json)
(experiment_id `debugger-interaction-v2-r5-coder14b`). Companion contracts
`r5_contract.json` and `r5_contract_cp118.json` are earlier / contrast
identities.

## What was run

The complete tracked five-task curated set
(`curated-none-handling-001` … `curated-caller-callee-005`) under one
common sanitized r5.9 treatment: cwd-safe pytest launcher; production-region
breakpoint/stack filtering; sanitizer that forwards production exception
frames only (hidden test source/assertions/literals/node ids never
forwarded); fail-closed actual-prompt anti-leakage audit; independent
`EvaluationVerifier` with bounded PATCH retries. Earlier r5.x matrices
(including r5.7) remain on disk under gitignored `runs/` as historical
evidence.

## Main result

BASE 14B resolved all five curated bugs **5/5** under r5.9, with **0
leakage findings across the 41 audited actual prompts**
(`docs/project-closeout.md` §3).

The earlier r5.7 5/5 was **disqualified** because hidden-test content leaked
into PATCH prompts. It is preserved as historical upper-bound evidence that
must fail the audit.

## Accepted interpretation

R5 establishes that a clean base-14B model can complete the repaired
debugger-informed repair loop on the five curated fixtures without prompt
leakage. **R5 does not claim that fine-tuning caused an improvement.**

## Limitations

- Curated fixtures only; not a repository-scale claim.
- No matched-base fine-tuning ablation (that remains absent after R6 as
  well).
- Live matrix directories under `runs/` are gitignored; the tracked
  contracts plus closeout/final-report carry the accepted 5/5 claim.

## Authoritative sources

- `r5_contract_14b.json`
- `docs/project-closeout.md` §3
- `docs/final-report.md`
- `docs/results-index.md`
