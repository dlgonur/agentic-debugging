# Exact-PDB capability ladder

This family raises task difficulty one task at a time on the same accepted
controller, PDB, PatchManager, verifier, cleanup, and replay path. A rung is a
model-capability result only when a real selected model authors the directives
and candidate patch and the independent verifier reports `RESOLVED`.

The scores are an internal ordinal rubric, not benchmark percentages:

- 6/100: one-function boundary/off-by-one repair, one public F2P and one P2P;
- 12/100: caller/callee unit contract across two functions, normalized input,
  one public F2P, two public P2P checks, and verifier-only generalization checks.
- 18/100: three-stage unit normalization/conversion/retry expansion across
  three functions and four public inputs, with public and verifier-only
  invariants for both converted and already-normalized units.
- 32/100: a pinned external Cookiecutter configuration regression with a
  real package import graph, exact public pytest/PDB execution, model-authored
  repository patch, and the official SWE-rebench 5 F2P + 9 P2P verifier.

Every rung is single-task and exact-PDB-required. Thinking output is permitted
and counted only as aggregate activity; it is not action authority and its text
is not retained. Only strict final directive content can cause a controller
action. A 300-second inactivity watchdog stops a request with no stream,
stdout, or stderr activity; active responses may continue. A 3,600-second
cumulative model-phase guard is retained only as a broad between-request
failsafe.

## Rungs

| Rung | Task | Model | State |
| --- | --- | --- | --- |
| 6/100 | `pdb-required-boundary-006` | `gpt-oss:20b-cloud` | Accepted `RESOLVED`; see [`level06-gpt-oss-v1`](level06-gpt-oss-v1/result.md) |
| 12/100 | `pdb-required-caller-callee-007` | `gpt-oss:20b-cloud` | Accepted `RESOLVED`; see [`level12-gpt-oss-v1`](level12-gpt-oss-v1/result.md) |
| 18/100 | `pdb-required-multistage-units-008` | `gpt-oss:20b-cloud` | Accepted `RESOLVED`; see [`level18-gpt-oss-v1`](level18-gpt-oss-v1/result.md) |
| 32/100 | `audreyr__cookiecutter-967` | `gpt-oss:20b-cloud` | Not accepted: valid V3 harness/model run, local `RESOLVED`, official F2P 0/5; see [`level32-cookiecutter-967-gpt-oss-v3`](level32-cookiecutter-967-gpt-oss-v3/result.md) |

No success rate, generalization, model ranking, or causal PDB benefit follows
from a single rung. This treatment now has a descriptive single-task boundary:
18/100 accepted and 32/100 failed after valid official evaluation. A different
model may be tested on the same frozen 32/100 rung under a new explicitly
authorized identity; the GPT-OSS task must not be tuned from hidden outcomes.
