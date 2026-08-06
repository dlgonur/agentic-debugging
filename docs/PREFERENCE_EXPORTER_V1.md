# Preference Dataset Exporter v1

**Date:** 2026-08-06
**Branch:** `goal/friday-rag-comparison-v1`
**Baseline:** `e92634e3dc016276d22ab9b9197adf4b28abbeb1`
**Package:** `agentic_debugger/preference/`
**Scope:** deterministic export of verifier-backed preference pairs from
normalized comparison attempt records. This sprint produces exporter
infrastructure and a deterministic demo-scale pair set. It does **not**
perform DPO/RLHF or any training.

## 1. Input

One `comparison-v1` experiment document (attempts must be verifier-backed)
plus one `task.json` per task (oracle only, for contamination guards) and an
optional held-out task list.

## 2. Ordered preference rules

1. **rule-1** — RESOLVED verifier outcome beats non-RESOLVED;
2. **rule-2** — strictly valid patch beats invalid/absent patch;
3. **rule-3** — stronger F2P rate beats weaker;
4. **rule-4** — with equal F2P, stronger P2P rate beats weaker;
5. **rule-5** — otherwise-equal verified attempts use fewer changed files as
   a tie-break;
6. equal or incomparable attempts produce **no pair** (no fabricated
   preference).

The first deciding rule is recorded as `rule_id` with a human-readable
`preference_reason`. Attempts with different prompt contracts, attempts where
one side lacks F2P/P2P rates, and attempts with equal evidence are treated as
incomparable (no pair).

## 3. Pair schema (`preference-pair-v1`)

Stable `pair_id` (SHA-256 over canonical identity payload: task, prompt
identity, chosen/rejected attempt ids and condition ids, source comparison
identity); task/prompt identity; chosen and rejected responses (bounded to
64 KiB per response with an explicit truncation marker — never silent
sanitization); both attempt ids; both condition/model identities; both
provenance records; verifier evidence for both sides; `rule_id` and reason;
source comparison identity. Output: deterministic JSONL (sorted by pair id)
plus an audit summary JSON. Strict schema: no unknown fields, no missing
fields, no NaN/Infinity.

## 4. Guards (fail-closed, never silent)

* held-out task exclusion (declared set; counted in the audit);
* oracle-answer contamination rejection — a response containing the
  evaluator-only root-cause summary or runtime-evidence hint verbatim
  refuses the pair (never sanitized). Target file/symbol names are reported
  as non-rejecting identity spans, because any legitimate patch must name
  them;
* duplicate pair identity rejection (hard error);
* same-attempt rejection (by construction);
* same-response rejection;
* no-evidence rejection (both sides lack verifier evidence, or a side has no
  response);
* stable deterministic ordering and stable pair ids.

## 5. Audit summary

`schema_version`, source comparison identity, attempts considered, tasks
considered, held-out exclusions, pairs produced + pair ids, per-rule counts,
rejection counts (held-out / same-response / contamination / no-evidence /
incomparable), per-task pair counts.

## 6. Demo-scale evidence

The deterministic demo exports pairs per task: chosen = the verified correct
imported attempt (RESOLVED), rejected = the deterministic non-repair
attempt (verifier-decided NO_OP). This is a demo-scale pair set — **a
one-pair deterministic demo is not a production preference dataset** — and
no model training of any kind follows from it in this sprint.

## 7. Safety and identity hardening (repair 1, 2026-08-06)

* **Contamination on the full response**: oracle-answer contamination is
  checked against the complete original bounded generation response
  (the attempt's stored response) *before* the pair-storage bound is
  applied; the pair is refused (never sanitized) and the rejection spans
  are recorded in the audit. Oracle text beyond the storage cutoff is
  structurally never stored.
* **Response bounding**: the truncation marker is included inside the exact
  64 KiB budget; the cut never splits a UTF-8 code point, introduces no
  replacement characters, and the exact output byte length never exceeds
  the cap — tested with two-, three- and four-byte characters at the
  boundary.
* **Pair identity**: `pair_id` now binds the schema version, task/prompt
  identity, both attempt identities, both condition/model/adapter
  identities, both full response hashes, both patch hashes, both source
  identities, the verifier-evidence identity and the source comparison
  identity; `PreferencePair.from_mapping()` recomputes and verifies the
  pair id on every load (any tampering is rejected).
* **Stored refs**: every `AttemptRef` carries a valid response hash and a
  non-empty source identity; stored responses respect the byte cap;
  the audit's rejected-key set is complete (including `same_attempt`).
