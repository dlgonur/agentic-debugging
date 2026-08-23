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
| 32/100 | `audreyr__cookiecutter-967` | `gpt-oss:20b-cloud` | Historical V3 retained unchanged; repaired `workspace-derived-official-git-diff-v1` replay reached official tests for 16/18 retained candidates, with no 5/5 F2P + 0 P2P resolution; durable summary: [`analysis/level32_candidate_artifact_replay_20260823.md`](../../analysis/level32_candidate_artifact_replay_20260823.md) |

No success rate, generalization, model ranking, or causal PDB benefit follows
from a single rung. The original V3 `0/5, 9/9` summary was historically
recorded, but raw model-diff serialization was not proven to be official-Git
compatible. The repaired treatment keeps that evidence immutable, uses
`candidate.patch` only as raw provenance, and evaluates the deterministic
workspace-derived `candidate-official.patch` after equivalence proof. The
frozen task and hidden tests were not changed or exposed.

The repair contract is `workspace-derived-official-git-diff-v1`: raw semantic
materialization through the authorized tolerant PatchManager, canonical Git
diff generation, strict application to a pristine equivalent baseline, and
byte-for-byte comparison before official evaluation. Durable replay evidence is
in `../../analysis/level32_candidate_artifact_replay_20260823.md`; the local review
package is `_ai-review/L32-ARTIFACT-01/historical-replay/`.
