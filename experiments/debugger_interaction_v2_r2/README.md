# R2 — Staged multi-turn debugger interaction

## Identity

`debugger-interaction-v2-r2` (contract schema `debugger-interaction-v2-r2.1`,
interface `r2.1`). Accepted implementation commit recorded in closeout:
`97cc7fe`. Descends from accepted R1 `c842d697890bc5c2c18dcc6a81f60b98fe835044`.

## What was run

Same frozen RAW Qwen2.5-Coder-7B identity, same `curated-off-by-one-002`
task and budgets as R1, with the staged PAUSED lifecycle:
break → stack → locals/print → step/next → stack → diagnosis. Only
staged-legal commands are advertised. The model still chooses every value
(line, locals vs print, expression, step vs next, diagnosis text).

Frozen contract: [`r2_contract.json`](r2_contract.json).

## Main result

A multi-turn dynamic loop — breakpoint → stack → locals → step/next →
post-step stack → diagnosis — was completed by a real model
(`docs/project-closeout.md` §3).

## Accepted interpretation

R2 shows that a real model can use the repaired staged debugger interface
for a genuine multi-turn observation loop. It does not yet claim an
independent-verifier RESOLVED repair (that is R3).

## Limitations

- Single curated task; RAW 7B only.
- Live `runs/` evidence is gitignored; closeout and the tracked contract
  carry the accepted claim.

## Authoritative sources

- This contract and runner
- `docs/project-closeout.md` §3
- `docs/results-index.md`
