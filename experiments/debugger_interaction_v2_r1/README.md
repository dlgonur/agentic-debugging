# R1 — Repaired debugger-interface breakpoint

## Identity

`debugger-interaction-v2-r1` (contract schema `debugger-interaction-v2-r1.1`).
Accepted implementation commit recorded in closeout: `c842d69`.

## What was run

Same frozen RAW `Qwen/Qwen2.5-Coder-7B-Instruct` @
`c03e6d358207e414f1eca0bb1891e29f1db0e242` (`adapter_applied=false`,
`rag_enabled=false`) on `curated-off-by-one-002`, with the repaired
model-facing interface: production source + breakpoint-eligible lines,
source rendering, error diagnostics, and lifecycle visibility. The model
selects the breakpoint line; the affordance is `compile()+co_lines()`
derived, production-only, non-oracle.

Frozen contract: [`r1_contract.json`](r1_contract.json).

## Main result

A real model authored a valid breakpoint; the real PDB session paused and
returned a production-region observation (`docs/project-closeout.md` §3).

## Accepted interpretation

This is the first accepted positive real-model debugger milestone after the
historical D1/S2 failures under the old interface (RAW `break 20` → tool
error; cp118 `continue` → rejected, no session). It does not by itself
prove a multi-turn loop or a verifier-confirmed repair.

## Limitations

- Single curated task; RAW 7B only.
- Live evidence JSON under `runs/` is gitignored; the tracked contract plus
  `docs/project-closeout.md` / `docs/final-report.md` are the professor-facing
  carriers.

## Authoritative sources

- This contract and runner
- `docs/project-closeout.md` §3
- `docs/results-index.md`
