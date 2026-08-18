# Nemotron 3 Nano model-capability probe

## Identity

`nemotron-3-nano-model-capability-probe-v1`. Selected Ollama Cloud
profile `nemotron-3-nano:30b-cloud` (upstream `nemotron-3-nano:30b`) on
the accepted Local Application configured-command path. This is not the
product-runtime default (`gpt-oss:20b-cloud`).

Frozen contract: [`contract.json`](contract.json). Compact audit capsule:
[`frozen/`](frozen/).

## What was run

One selected lower-capacity Nemotron model on the existing product
runtime, after the multi-model Ollama Cloud generalization
(`756bd2d`) and State-Aware Validate (`4f0a748`, Harness V2). No new
provider session is part of this closeout.

Four records are preserved distinctly:

| Record | Session | Harness | Result |
| --- | --- | --- | --- |
| V1 | `sess-20260817-200956-160723` | before State-Aware Validate (`756bd2d`) | FAILED — classify_outcome before post-patch F2P; verifier did not run |
| V2 | `sess-20260818-050514-20777e` | `4f0a748` | infrastructure-invalid `BASELINE_INVALID` — repository-nested verifier workspace prefixed pytest node IDs; candidate not evaluated |
| V2b | `sess-20260818-052524-f0287d` | `4f0a748`, fresh external `%TEMP%` root | independent verifier COMPLETED / RESOLVED; F2P 1/1; P2P 2/2; full suite PASS 3/3 |
| Five-task Harness V2 treatment | V2b plus four later sessions | `4f0a748`, fresh external root per task | **1 RESOLVED / 5**; all five rows admissible |

V1 and V2 are not treatment rows. V2 is not an ordinary model failure
and is not RESOLVED. V1 is not RESOLVED.

## Main result

On the complete admissible Harness V2 five-task curated treatment the
selected Nemotron model achieved **1/5 RESOLVED**. The one resolved row
is V2b `curated-none-handling-001`. The other four rows failed in the
controller before the independent verifier ran: two after premature
Reproduce → Failed decisions, and two after repeated
current-source-incompatible patch hunks were rejected by PatchManager
until the patch budget was exhausted.

PDB was **NOT EXERCISED** on all five tasks (not PASS, not a failure).

## Accepted interpretation

The selected Nemotron model completed one of the five curated tasks
through the accepted product runtime and independent verifier. The
remaining failures included premature controller failure decisions and
repeated source-incompatible patch proposals. The result reinforces that
model capability remains an important practical variable: a good
deterministic harness does not make underlying model capability
irrelevant.

This does not establish a causal model-strength comparison. R5 remains
separate evidence that a stronger clean base model achieved 5/5 in its
accepted treatment. R6 remains separate evidence that debugger-trajectory
post-training achieved 8/8 disjoint validation, without a matched-base
causal ablation. GPT-OSS and Nemotron do not have a completed matched
five-task comparison.

## Limitations

- One selected model, one policy, one five-task curated treatment.
- Not representative of all small or weaker models.
- Not a model-size causal claim.
- Not a fine-tuning necessity or sufficiency claim.
- Not a PDB-effectiveness result.
- Harness V2 is not directly comparable to historical R5/R6 treatments.
- Ephemeral `%TEMP%` roots and `_ai-review` packages are not the
  professor-facing carriers; use this family.

## Authoritative sources

- `contract.json`
- `frozen/capsule_manifest.json`
- `frozen/records/`
- `docs/results-index.md`
- `docs/project-closeout.md` 2026-08-18 update
