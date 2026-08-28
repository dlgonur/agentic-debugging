# S5 — Final controlled comparison

## Identity

`s5-controlled-comparison-v1`. Baseline HEAD
`acfe131a0a99b994fd3d34e520d0022191246025`. Synthesis of already-accepted
evidence — not another model campaign. No model was run, S4 was not
resumed, RAG was not changed, and no historical evidence was modified.

## What was compared

Eight axes kept separate: localized executable repair, fine-tuning
transfer, RAG treatment, debugger interaction, model-generated test
capability, serialization sensitivity, static verifier success, local
inference engineering. Conditions A–E (RAW frozen repair, cp118 RAG-OFF,
cp118+RAG partial, RAW debugger D1, cp118 debugger S2) plus auxiliary DPO
and S1-P notes.

## Main result (accepted, not re-scored here)

- Localized repair: RAW 5/40 RESOLVED vs cp118 0/40 apply / 0/40 RESOLVED
- S4 RAG: 10/40 partial; primary correctness NOT_EVALUATED
- DPO (auxiliary): B1 27/30, matched SFT 27/30, DPO 21/30; CLOSED
- Pre-R1 debugger interaction: D1/S2 produced zero successful observations
  (later superseded as the project conclusion by R1–R4)

## Accepted interpretation

This directory is the canonical controlled comparison of the
then-accepted evidence. Later R1–R6 work superseded the overall
bounded-negative project conclusion; S5's axis values remain historical
comparison evidence and are not rewritten.

## Limitations

Some aggregates are `master_plan_prose_only` or
`aggregate_external_per_task` (Drive-hosted D7). The untracked
`Agentic_Debugging_Master_Execution_Plan_2026-08-11_S5_CURRENT.md` is
cited as the build-time plan; the tracked 2026-08-10 master plan (now at
`outdated/docs-archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md`)
and the files in this directory are the clean-checkout carriers.

## Authoritative sources

- [`s5_controlled_comparison_report.md`](s5_controlled_comparison_report.md)
- [`s5_comparison_ledger.json`](s5_comparison_ledger.json)
- [`s5_provenance_source_map.md`](s5_provenance_source_map.md)
- `docs/final-report.md` §11 / appendix
- `docs/results-index.md`
