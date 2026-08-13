# Root-Cause Explanation Metric v1

## Purpose

Tracker item 7.1.2 requires a root-cause explanation metric. Patch correctness,
localization, and a fluent explanation are different claims: passing F2P/P2P
tests cannot prove that an explanation identifies the causal mechanism. This
metric therefore records an independent rubric assessment and never derives
correctness from patch success or lexical similarity to hidden oracle text.

Implementation: `agentic_debugger/evaluation/root_cause_metric.py`.

## Artifact contract

`root-cause-assessment-v1` binds one assessment to:

- exact task and attempt identifiers;
- assessor kind (`independent-human`, `independent-ai`, or
  `deterministic-fixture`) and a disclosed assessor identifier;
- SHA-256 of the bounded claim text (the claim and oracle text are not stored
  in the assessment artifact);
- a `claim_binding` that ties `claim_sha256` to the exact attempt evidence the
  claim was extracted from — a bounded response substring (with byte offset and
  SHA-256 of the source response) or a trajectory hypothesis reference (with
  the hypothesis offset and SHA-256 of the hypothesis statement);
- rubric version `root-cause-rubric-v1`;
- three explicit dimensions;
- contradiction judgment and evidence references;
- a derived closed outcome and content-derived assessment identity.

`claim_binding` and `claim_sha256` must both be present (assessed claim) or
both absent (`NOT_PROVIDED`).  Mismatched or invented claim hashes are rejected
at load time.  Evidence references must use the `{trajectory|verifier}:ref`
shape and, when the attempt's declared evidence set is supplied, every
reference must appear in that declared set — arbitrary strings are not
accepted.

The three dimensions are:

1. `mechanism`: does the claim identify the defect/state/control-flow
   mechanism?
2. `failure_connection`: does it connect that mechanism to the observed
   failure?
3. `repair_alignment`: does it explain why the proposed repair addresses the
   mechanism rather than merely suppressing the symptom?

Each dimension is `SATISFIED`, `PARTIAL`, `NOT_SATISFIED`, or
`NOT_ASSESSED`.

## Closed outcomes

- `CORRECT`: all dimensions satisfied, no contradiction, evidence present.
- `PARTIALLY_CORRECT`: an assessed, non-contradictory claim with mixed rubric
  results that does not meet the incorrect rule.
- `INCORRECT`: the claim contradicts observed evidence, or both the mechanism
  and failure connection are not satisfied.
- `NOT_PROVIDED`: the trajectory contains no claim; this must itself cite the
  trajectory evidence.
- `NOT_ASSESSED`: a claim exists but no rubric judgment was performed.

Partially assessed records fail closed. Assessed claims require all three
dimensions, a boolean contradiction judgment, and at least one unique bounded
evidence reference. Artifact loading recomputes the outcome and identity and
rejects missing, unknown, tampered, oversized, duplicate, or contradictory
fields.

## Aggregation and comparison integration

`aggregate_root_cause_assessments` reports explicit denominators:

- expected attempts;
- present and missing assessment records;
- assessed claims;
- every closed outcome count;
- assessment coverage rate;
- correct rate over all attempts;
- correct rate over assessed claims.

The existing `comparison-v1` wire schema remains compatible. An optional
assessment is stored under attempt provenance key `root_cause_assessment`.
Comparison metric derivation validates the nested v1 artifact, requires exact
task/attempt binding, adds per-condition counts/rates and baseline deltas, and
projects outcome/assessment identity into CSV. A missing record is counted as
missing—not as correct, incorrect, or not-provided.

## Evidence boundary

The metric is now defined and integrated, satisfying tracker subtask 7.1.2.
It does not claim that any live model has a correct root-cause rate. Real
results require an authorized campaign, blinded independent assessment, and
the same frozen tasks, prompts, verifier, and evidence policy across compared
conditions.
