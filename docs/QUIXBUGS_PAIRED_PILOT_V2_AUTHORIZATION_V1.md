# QuixBugs paired-pilot v2 live authorization contract

This document defines the strict, versioned authorization artifact required to
run the frozen QuixBugs paired-pilot v2 live campaign
(`research/quixbugs/PAIRED_PILOT_V2.json`, canonical manifest SHA-256
`bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171`, accepted
baseline `28ec7754336fc53f21ebbae8a851b33e26714932`, live protocol `1.3`).

The machine-readable schema reference is the non-authorizing template
`research/quixbugs/PAIRED_PILOT_V2_AUTHORIZATION_TEMPLATE.json`, which is
**rejected** by the runner (`TEMPLATE_IS_NOT_AUTHORIZATION`). The strict
validator is `authorization_failure` /
`validate_authorization_artifact` in `scripts/quixbugs_live_runner_v2.py`.

## What the artifact must bind

| Field | Requirement |
| --- | --- |
| `schema_version` | exactly `quixbugs-paired-pilot-authorization-v1` |
| `template` | must be `false` (the tracked template is `true` and is rejected) |
| `authorize_live` | must be `true` |
| `campaign_id` / `campaign_version` | exactly `quixbugs-paired-pilot-v2` / `2` |
| `campaign_manifest_hash` | exactly the canonical v2 manifest hash `bc3df312…f3171` |
| `accepted_baseline` | exactly the accepted repository baseline `28ec7754336fc53f21ebbae8a851b33e26714932` |
| `planning_baseline_commit` | exactly `18e067f24c337e7215139373edc699a347cf2127` |
| `qualification_contract_hash` | exactly `7246d289fcc689e93d93385751cbae5fa75a3c52e3c04e001f2c977a1990c52d` |
| `accepted_campaign_commit` | 40-hex execution commit, distinct from the planning baseline |
| `permitted_case_ids` | the exact six frozen v2 case IDs in manifest order, no duplicates |
| `provider` / `model` / `variant` / `protocol` | `OpenCode Go` / `deepseek-v4-flash` / `max` / `1.3` |
| `expected_opencode_version` | exact OpenCode runtime version resolved by the operator before contact |
| `expected_catalog_fingerprint` | exact 64-hex catalog fingerprint observed by the operator |
| `expected_runtime_model_id` | exact catalog-qualified runtime model identity resolved before contact |
| `subscription_route_required` | `true` |
| `expected_billing_route` | `SUBSCRIPTION` |
| `subscription_entitlement_confirmed` | `true` |
| `subscription_account_observation` | `{"entitlement_confirmed": true, "evidence_reference": "<ref>"}` |
| `expected_account_status` | the required account/route status (e.g. `ACTIVE`) |
| `billing_route_classification` | `SUBSCRIPTION` |
| `deny_*` (eight flags) | all `true`: Zen, free-tier, Ollama, alternate provider, model substitution, metered fallback, paid overage, per-call billing |
| `no_fallback_required` | `true` |
| `operator_authorization_id` | operator authorization identity / record ID |
| `authorization_created_at` / `authorization_valid_until` | ISO-8601 UTC; validity may be `null` or bounded; an expired artifact is rejected |
| `output_root` | the exact output/attempt directory for this campaign |
| `campaign_attempt_identity` | `quixbugs-paired-pilot-v2-attempt-<64 hex>` |
| `single_frozen_six_case_campaign_confirmation` | `true` |

## What is rejected

The artifact is rejected (with the failure category reported as the campaign
`stop_reason` and zero provider activity) for: unknown fields; missing fields;
wrong types; `template: true`; wrong schema version; wrong campaign identity;
wrong manifest hash; wrong accepted or planning baseline; wrong qualification
contract hash; invalid or planning-baseline campaign commit; duplicate case
IDs; changed case order; wrong case set; wrong provider/model/variant/
protocol; missing OpenCode version, catalog fingerprint, or runtime model
identity; missing subscription-route requirement; billing route other than
`SUBSCRIPTION`; unconfirmed entitlement; missing/contradictory account
observation; missing account status; any denial flag not true;
`no_fallback_required` false; missing operator identity; unparseable or
expired validity; output-root mismatch; invalid attempt identity; missing
single-campaign confirmation; and any v1 zero-price contradiction
(`zero_price_required`).

The v1 zero-price authorization fields are **not** part of the v2 contract;
a v2 authorization that requires zero pricing is a contradiction and fails
closed.

## Where authorizations live

Real operator authorizations must be created **outside tracked source** in an
ignored operator-artifact location (`operator/`, ignored via `.gitignore`).
No real authorization is committed by this task; the tracked template cannot
be mistaken for one and is rejected by the validator.

## Interaction with the runner

1. `python scripts/quixbugs_paired_pilot.py preflight --authorization <path> --output <dir> [--route-evidence-json <file>]`
   validates the artifact, the repository baseline, and the route gate with
   zero provider contact.
2. `python scripts/quixbugs_paired_pilot.py live --authorization <path> --output <dir>`
   prints the pre-provider plan and then fails closed unless an explicitly
   configured provider transport and case runner are supplied; in this task
   no transport exists, so live execution is rejected with zero provider
   activity.
3. `python scripts/quixbugs_paired_pilot.py template --output <path>` writes
   the non-authorizing template.

See `docs/QUIXBUGS_PAIRED_PILOT_V2_LIVE_RUNNER_V1.md` for the full runner
lifecycle.

## Execution-commit binding (material repair)

`accepted_campaign_commit` is not just a 40-hex marker: it is the **exact
commit whose code will execute the future campaign**. Before ledger claim,
preflight, transport creation, or provider contact, the runner independently
observes the actual repository state and requires:

* actual Git HEAD equals `accepted_campaign_commit`
  (`EXECUTION_COMMIT_MISMATCH` otherwise);
* the bound commit exists in the repository
  (`EXECUTION_COMMIT_NOT_FOUND`);
* the bound commit descends from the accepted baseline
  `28ec7754336fc53f21ebbae8a851b33e26714932`
  (`EXECUTION_COMMIT_ANCESTRY_FAILED`);
* the tracked working tree and the real Git index are clean, allowing only
  ignored operator/output artifacts (`TRACKED_STATE_DIRTY` otherwise).

The independently observed and verified execution commit is recorded in the
campaign record (`execution_commit` evidence), every case record, authority
records, the route observation, and the attempt-ledger entry. Result commit
fields are never populated by copying caller-supplied authorization data
alone: the observed HEAD must match the bound commit first. The execution
commit and tracked-clean state are re-verified before every case; a
post-preflight drift stops the campaign with typed authority/campaign-stop
evidence (`TRACKED_SOURCE_CHANGED`).

The future real authorization will be created after the runner and execution
wiring have an accepted commit; no future commit is invented in tracked
files.

## Nested authorization strictness (material repair)

* `subscription_account_observation` has the exact field set
  `{"entitlement_confirmed", "evidence_reference"}` with strict types;
  unknown nested fields, wrong types, unconfirmed entitlement, and empty or
  whitespace-only evidence references are rejected
  (`ACCOUNT_OBSERVATION_INVALID`).
* `authorization_created_at` must not be materially in the future
  (`CREATED_AT_FUTURE`; clock-skew allowance 120 s).
* When `authorization_valid_until` is present it must be later than
  `authorization_created_at` (`VALIDITY_NOT_AFTER_CREATION`) and later than
  the execution time (`AUTHORIZATION_EXPIRED`).

## Output-root ownership (material repair)

One output/attempt root belongs to exactly one campaign-attempt identity. The
runner claims the root atomically (`.attempt-owner`, exclusive create); a
fresh authorization must use a fresh output root or a distinct immutable
attempt subdirectory. Occupied or contradictory roots are rejected before the
ledger claim (`OUTPUT_ROOT_OWNED`), and authoritative artifacts use
create-once semantics — existing `campaign.json`, case records, private
evidence, and terminal ledger data are never replaced.
