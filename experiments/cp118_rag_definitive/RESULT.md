# S4 result — cp118 + frozen repository RAG

The how-to and contract live in [`README.md`](README.md) and
[`s4_contract.json`](s4_contract.json). This note records the accepted
scientific outcome only.

## Identity

Definitive cp118 (accepted tuned checkpoint, adapter tree `65b5ed9a...`) +
frozen repository RAG over the frozen quix40 40-task cohort under protocol
v1.2.1. Measurement, not rescue: the only treatment difference vs accepted
cp118 RAG-OFF (40/40 extracted, 0/40 applied, 0/40 RESOLVED) is RAG OFF →
frozen repository RAG ON.

## What was run

The live generation campaign was source-frozen and launched on the full
40-task cohort, then terminated after 10 valid tasks for compute/runtime
feasibility (`docs/final-report.md` §10). The 10 tasks are the first 10 in
frozen manifest order — not random, not representative. Task 11 was
interrupted and discarded atomically. `S4_GENERATION_COMPLETE.json` was
correctly not written.

## Main result

- 10/40 tasks produced valid immutable raw/meta/retrieval pairs
- 5/10 generations reached the frozen 4096-token output cap (descriptive
  only; **do not extrapolate to 40**)
- Primary frozen C9 evaluation: **NOT_EVALUATED**
- Patch apply / RESOLVED: **NOT_EVALUATED**
- P2P: **NOT_RECORDED**

## Accepted interpretation

**CLOSED — PARTIAL / COMPUTE-CONSTRAINED / NOT_EVALUATED.**
No RAG success or failure claim is made from the S4 partial condition
(`docs/project-closeout.md` §4; `TODO.md`).

## Limitations

Per-task run records (`PARTIAL_RUN_RECORD.json`, `run-identity.json`) are
local/untracked. The tracked contract plus S5 ledger / final-report are the
clean-checkout carriers. Peak CUDA allocator figures in the report are
untrusted descriptive instrumentation, not physical VRAM.

## Authoritative sources

- `s4_contract.json`, this directory's README / SOURCE_AUDIT
- `docs/final-report.md` §10
- `analysis/s5_final_controlled_comparison/s5_controlled_comparison_report.md`
- `docs/results-index.md`
