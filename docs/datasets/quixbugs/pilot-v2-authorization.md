# QuixBugs paired-pilot v2/v3/v4 live authorization contract

This document defines the strict, versioned authorization artifact required to
run a frozen QuixBugs paired-pilot v2, v3, or v4 live campaign. The next
authorized execution uses `research/quixbugs/PAIRED_PILOT_V4.json` (canonical
manifest SHA-256 `020dfc1f7b8f23aa96a4d7c7942429e306cc290906abfed5ce96cde22b90354d`);
v2 remains the compatibility/derivation contract at SHA-256
`bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171` and v3 at
`f5f513a16008ce807b4ed248e0310958940aefd348199e77dc0bbabc9a9e45cf`.
All use accepted baseline `28ec7754336fc53f21ebbae8a851b33e26714932`
and live protocol `1.3`.

The machine-readable schema reference is the non-authorizing template
`research/quixbugs/PAIRED_PILOT_V2_AUTHORIZATION_TEMPLATE.json` for v2 or
`research/quixbugs/PAIRED_PILOT_V3_AUTHORIZATION_TEMPLATE.json` for v3, which is
**rejected** by the runner (`TEMPLATE_IS_NOT_AUTHORIZATION`). The strict
validator is `authorization_failure` /
`validate_authorization_artifact` in `scripts/quixbugs_live_runner_v2.py`.

## What the artifact must bind

| Field | Requirement |
| --- | --- |
| `schema_version` | exactly `quixbugs-paired-pilot-authorization-v1` |
| `template` | must be `false` (the tracked template is `true` and is rejected) |
| `authorize_live` | must be `true` |
| `campaign_id` / `campaign_version` | exactly the selected manifest: `quixbugs-paired-pilot-v2` / `2`, `quixbugs-paired-pilot-v3` / `3`, or `quixbugs-paired-pilot-v4` / `4` |
| `campaign_manifest_hash` | exactly the selected canonical hash: v2 `bc3df312…f3171`, v3 `f5f513a1…e45cf`, or v4 `020dfc1f…b90354d` |
| `accepted_baseline` | exactly the accepted repository baseline `28ec7754336fc53f21ebbae8a851b33e26714932` |
| `planning_baseline_commit` | exactly `18e067f24c337e7215139373edc699a347cf2127` |
| `qualification_contract_hash` | exactly `7246d289fcc689e93d93385751cbae5fa75a3c52e3c04e001f2c977a1990c52d` |
| `accepted_campaign_commit` | 40-hex execution commit, distinct from the planning baseline |
| `permitted_case_ids` | the exact six selected-manifest case IDs in order, no duplicates |
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
| `campaign_attempt_identity` | selected version: `quixbugs-paired-pilot-v2-attempt-<64 hex>`, `quixbugs-paired-pilot-v3-attempt-<64 hex>`, or `quixbugs-paired-pilot-v4-attempt-<64 hex>` |
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

For the next execution, a v2/v3 authorization or an invocation that omits the
explicit v4 manifest is a campaign-identity mismatch, not a permitted
fallback. All route capture, bundle, adapter validation, preflight, and live
commands must pass `--manifest research/quixbugs/PAIRED_PILOT_V4.json`.

The v1 zero-price authorization fields are **not** part of the v2 contract;
a v2 authorization that requires zero pricing is a contradiction and fails
closed.

## Where authorizations live

Real operator authorizations must be created **outside tracked source** in an
ignored operator-artifact location (`operator/`, ignored via `.gitignore`).
No real authorization is committed by this task; the tracked template cannot
be mistaken for one and is rejected by the validator.

## Operator bundle materialization

The `operator-bundle` mode of `scripts/quixbugs_opencode_go_adapter.py`
materializes the real authorization artifact from an accepted
`quixbugs-route-evidence-v1` file (produced by the `route-capture` mode):
`operator/quixbugs-operator-bundles-v1/<attempt identity>/authorization.json`
(plus the matching `adapter-config.json`). The artifact's
`accepted_campaign_commit` is the **actual clean Git HEAD observed (read-only)
when the operator runs the command after the task has been accepted and
merged** — never a caller-supplied commit and never the task baseline. The
observed HEAD must exist, must descend from the accepted project baseline
`28ec7754336fc53f21ebbae8a851b33e26714932` and from the minimum task lineage
baseline `618c33ff186493892665ca1233c3edd8b2eec13f` (lineage prerequisite
only), and must have a clean tracked working tree, a clean real index, and no
non-ignored untracked files; HEAD and repository cleanliness are re-checked
immediately before the artifacts are created and any drift fails closed with
no active artifact written. The artifact is also bound to the explicitly
selected manifest hash (v3 for the next attempt:
`f5f513a16008ce807b4ed248e0310958940aefd348199e77dc0bbabc9a9e45cf`), the
exact six selected case IDs in order, protocol `1.3`, the exact observed
OpenCode version, runtime model ID, variant, and catalog fingerprint (the
deterministic catalog-entry fingerprint contract in
`scripts/opencode_protocol_transport.py`), the account status and
subscription billing route from the route evidence, one operator
authorization ID, one fresh attempt identity and output root, an explicit
bounded validity period, and the operator-resolved Python executable, working
directory, and operator boundary root. Dirty/staged source, drift, occupied
targets, template values, route drift, unknown fields, malformed paths, and
contradictory subscription/fallback assertions are rejected before any
artifact is written, and the materialized authorization passes the strict
`authorization_failure` / `validate_authorization_artifact` validator. The
`operator-bundle` flow is implemented and packaged but not executed by any
implementation agent; real operator preflight remains pending independent
review and project-owner execution.

## Interaction with the runner

1. Use `scripts/quixbugs_opencode_go_adapter.py operator-bundle` with the
   explicit v3 manifest to create the authorization and matching adapter
   configuration from accepted route evidence.
2. Use `route-preflight-only` with the same manifest and artifacts to validate
   authorization, repository, adapter, and route bindings with zero provider
   processes.
3. Use `live-wire` only with those exact artifacts, the explicit v3 manifest,
   a QuixBugs environment artifact, the task-bound facts provider, and the
   explicit live confirmation flag. The complete command sequence is in
   `docs/datasets/quixbugs/opencode-adapter.md`.

See `docs/datasets/quixbugs/pilot-v2-runner.md` for the full runner
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
