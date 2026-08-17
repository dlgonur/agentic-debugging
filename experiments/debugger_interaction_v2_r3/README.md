# R3 — Debugger-informed patch to independent verifier

## Identity

`debugger-interaction-v2-r3` (interface `r3.2`). Accepted implementation
commit recorded in closeout: `f2291df`. Descends from committed R2
`97cc7fe80b6b1f70d2fd6c6d5e11c1487f443af2`.

## What was run

Same RAW Qwen2.5-Coder-7B identity and `curated-off-by-one-002` task as R2.
Diagnosis in `READY_FOR_DIAGNOSIS` transitions RuntimeEvidence → PATCH
(not a self-transition). The PATCH checkpoint is bounded (patch + failed
before apply); after a successful apply the runner performs deterministic
administrative closeout and the independent `EvaluationVerifier` evaluates
the exact candidate. R3.2 adds a fail-closed metadata-only hunk-count
normalizer: only `old_count`/`new_count` are recomputed from the hunk body.

Frozen contract: [`r3_contract.json`](r3_contract.json).

## Main result

Debugger evidence → model diagnosis → semantic patch → PatchManager →
independent verifier **RESOLVED** (`docs/project-closeout.md` §3).

## Accepted interpretation

This is the first accepted end-to-end debugger-informed repair that reaches
the independent verifier. The mandatory qualifier is that the raw model
patch carried a unified-diff hunk-count metadata error corrected by a
deterministic **COUNT-ONLY** serialization normalization
(`docs/project-closeout.md` §3). That normalization is recorded (A/B/C
SHA-256) and is not a semantic rewrite of the model body.

## Limitations

- Single curated task; RAW 7B only.
- The hunk-count qualifier must travel with the RESOLVED claim.
- Live `runs/` evidence is gitignored.

## Authoritative sources

- This contract, `serialization.py`, and runner
- `docs/project-closeout.md` §3
- `docs/results-index.md`
