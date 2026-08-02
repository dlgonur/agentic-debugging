# QuixBugs paired pilot v2

This document freezes the second bounded paired static-versus-PDB feasibility
pilot contract before any live case. It is derived from the accepted v1
preregistration (`docs/QUIXBUGS_PAIRED_PILOT_V1.md`) against accepted baseline
`18e067f24c337e7215139373edc699a347cf2127`. Everything in v1 that is not
strictly changed by the new subscription route is carried over unchanged:
the three selected tasks, the six-case order, the controller budgets, protocol
1.3 behavior, the qualification contract, the source-integrity authority, the
public/private boundary, containment requirements, and the no-rerun rules.

The machine-readable source of truth is
\`research/quixbugs/PAIRED_PILOT_V2.json\`. Its campaign-manifest hash is
\`bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171\`. It
derives from the accepted v1 manifest
(\`research/quixbugs/PAIRED_PILOT_V1.json\`, hash
\`5d84ea22820ca38ce80dd90a5d36e6f80160220178496950f9b45be41fae19ce\`), which
remains the retained v1 authority and is not rewritten. The frozen
qualification-contract hash is
\`7246d289fcc689e93d93385751cbae5fa75a3c52e3c04e001f2c977a1990c52d\`; the
sanitized qualification evidence file is bound by SHA-256
\`29851dd98da64a400b030d372d85d02a47f20b0b3cc6c696a6c750be29841c41\`.

The independent source-integrity authority is
\`research/quixbugs/QUIXBUGS_SOURCE_INTEGRITY_V1.json\`, schema
\`quixbugs-source-integrity-v1\`, digest
\`a3ccf9d083f3405f0811b66c69a5e93d8a347d77b5f8ccb9d168d93102bd1977\`. It
records only buggy-source/test hashes and repository/path/revision identity
for all eight canonical tasks.

## Scope and selection

The canonical inventory contains the same eight task manifests, and the same
exclusions apply: gcd is excluded because it was repeatedly used for
transport/PDB engineering and prior live-model implementation; the other
seven received bounded, task-local pre-freeze screening. Eligibility is
nonjudgmental and frozen from v1: eligible task IDs are hashed as
\`SHA-256("quixbugs-paired-pilot-v1:<task-id>")\`, sorted by hash, and the
first three are selected. The v1 campaign salt is retained because selection
must not change between versions. The selected tasks are:

1. \`quixbugs-find-in-sorted-smoke-v1\`
2. \`quixbugs-is-valid-parenthesization-smoke-v1\`
3. \`quixbugs-hanoi-smoke-v1\`

The six-case order is frozen from the accepted v1 manifest (same task/policy
sequence, same order indices); only the case IDs and case hashes are
re-stamped with the v2 campaign prefix so v2 results cannot be confused with
v1 results:

1. \`quixbugs-paired-pilot-v2:quixbugs-find-in-sorted-smoke-v1:pdb-on-uncertainty\`
2. \`quixbugs-paired-pilot-v2:quixbugs-find-in-sorted-smoke-v1:static-baseline\`
3. \`quixbugs-paired-pilot-v2:quixbugs-hanoi-smoke-v1:static-baseline\`
4. \`quixbugs-paired-pilot-v2:quixbugs-is-valid-parenthesization-smoke-v1:pdb-on-uncertainty\`
5. \`quixbugs-paired-pilot-v2:quixbugs-is-valid-parenthesization-smoke-v1:static-baseline\`
6. \`quixbugs-paired-pilot-v2:quixbugs-hanoi-smoke-v1:pdb-on-uncertainty\`

## Planned route and subscription contract

The future route is the operator-selected OpenCode Go subscription running
DeepSeek V4 Flash, variant \`max\`, protocol 1.3. The exact runtime model and
catalog identity is not frozen in this manifest: no exact catalog identifier,
OpenCode version, catalog fingerprint, account status, entitlement, or
pricing observation that is not available from repository evidence is
invented. The future authorization artifact and the first-provider preflight
must bind the exact runtime model/catalog identity before contact.

The v1 "zero input and output price" eligibility rule is replaced by a
fail-closed subscription-route contract:

- the authorized route is the OpenCode Go subscription;
- no Zen route;
- no free-tier substitution;
- no Ollama route;
- no alternate provider;
- no model substitution;
- no metered fallback, paid overage route, or per-call billing fallback;
- if subscription entitlement or billing-route evidence cannot be established
  before contact, the campaign must block before the first provider call.

Observed route observations record the billing route
(\`SUBSCRIPTION\`/\`ZEN\`/\`FREE_TIER\`/\`OLLAMA\`/\`METERED\`/\`PER_CALL\`/
\`UNKNOWN\`), subscription-entitlement confirmation, and the exact runtime
model identity, and each preflight failure category has one exact predicate.
A successful preflight requires \`SUBSCRIPTION\` billing route, confirmed
entitlement, and a bound exact runtime model identity; anything else blocks
before the first provider call.

Provider-reported token and cost metadata remain truthful: v2 results do not
force \`provider_reported_cost\` (or observed input/output prices) to zero
merely because access is subscription-based. Reported cost must match the
route observation's provider-reported cost.

## Pair contract

Each task has one \`static-baseline\` case and one \`pdb-on-uncertainty\` case.
Each pair shares task material, public request construction, route, transport,
budgets, verifier, containment, restoration checks, telemetry, and evidence
schema. Every case uses a fresh provider process and fresh owned workspace.
The static policy has zero PDB gate openings, sessions, and observations. The
PDB policy can use PDB only through the real policy gate; not reaching PDB is
a valid result.

## Frozen budgets

The v2 budgets are identical to the accepted v1 budgets:

| Budget | Limit |
| --- | ---: |
| logical model calls | 64 |
| transport attempts per logical call / retries per logical call | 3 / 2 |
| total provider-process attempts / total transport retries | 192 / 128 |
| per-call / total-case timeout | 60 s / 900 s |
| accepted directives / malformed feedback cycles | 64 / 2 |
| hypotheses | 3 |
| PDB gate openings / observations | 3 / 3 |
| patch submissions | 1 |
| verifier runs | 20 |
| public evidence | 20,000 bytes |

The transport retry pair is per logical call. The case-total limits are
explicitly frozen separately. Any future deviation requires a new campaign
version.

## No-model qualification

Qualification is unchanged from v1: it uses only local pinned QuixBugs
material and the accepted WSL/Bubblewrap boundary, runs the buggy baseline
twice, requires a COMPLETED evaluator lifecycle for the expected non-resolved
no-op outcome, verifies a private COMPLETED/RESOLVED correct qualification
candidate with expected F2P/P2P behavior, checks canonical source hashes and
owned-workspace cleanup, validates task-local probe identity, and runs a
synthetic qualification hypothesis through the real PDB gate/session. PDB
accounting is read from the named success/failure count fields. The v2
manifest binds the same frozen qualification contract
(\`7246d289...\`) and the same sanitized evidence file
(\`29851dd9...\`); no qualification rerun is required or authorized by this
task.

Qualification details are private evidence. Gold patches, oracle outputs, and
private correct candidates are not copied into the public manifest or provider
requests.

The validator derives qualification status from the bound evidence, exactly as
in v1. Stored PASS strings are not authoritative.

Case results use the explicit terminal matrix from v1, extended for the
subscription route. LIVE_CASE blocks before provider contact preserve the
observed route and preflight values, including inactive models, unavailable
variants, mismatched route identities, observed billing routes, unconfirmed
entitlement, mismatched runtime model identities, and fallback/substitution
observations. They require zero case activity and a stable reason code
supported by a complete structured \`preflight_failure_evidence\` object.
DRY_RUN blocks use only the dry-run block kind and synthetic transport.
Campaign-stop blocks are reserved for subsequent unstarted cases and require
structured trigger and reason-specific expected/observed evidence.

Every result binds its top-level provider/model/variant to the observed route.
Contacted LIVE_CASE results additionally bind successful preflight to the
authorization artifact's OpenCode version, catalog fingerprint, and exact
runtime model identity, with subscription billing route and confirmed
entitlement. A structured pre-provider infrastructure failure may occur
before the first logical call; its stage controls the relaxed pre-contact
identity requirements.

Pre-provider failures use a documented controlling-failure precedence that
places the subscription-route reasons before the generic route reasons:
authorization validity; manifest, qualification, source-authority, and
campaign-commit bindings; observed Zen route; free-tier substitution; Ollama
route; metered fallback; paid overage; per-call billing fallback; unestablished
subscription entitlement; provider; model substitution; exact runtime model
identity; model; variant; protocol; OpenCode version; explicit catalog
failure; inactive model; unavailable variant; paid fallback; and
alternate-provider requirements. Each category also has an exclusive allowed
observation state.

## Commit binding and dry-run

\`18e067f24c337e7215139373edc699a347cf2127\` is the v2 planning baseline only;
it is not reported as a future campaign execution commit. Before any provider
call, a separately approved authorization artifact must bind the accepted
campaign Git commit, the v2 manifest hash
(\`bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171\`), the
qualification-contract hash, all six frozen case IDs, the exact
route/model/variant/protocol, the subscription route requirement
(\`subscription_route_required\`), the expected billing route
(\`SUBSCRIPTION\`), the exact expected runtime model identity
(\`expected_runtime_model_id\`), confirmed subscription entitlement evidence,
expected OpenCode version, expected catalog fingerprint, and the no-fallback
requirement. The v1 zero-price authorization fields are not part of the v2
authorization; a v2 authorization that requires zero pricing is a
contradiction and fails closed.

Dry-run records use null campaign and candidate commits. The model-free
dry-run issues fresh bounded IDs for every case and records an ordinary
model-result failure once and advances, while an injected infrastructure
failure stops before the next case. No provider process, network activity, or
OpenCode transport is reachable in this task.

## Stop rules

The campaign stops before the next case on route/model/variant mismatch,
billing-route deviation, subscription entitlement loss, model substitution,
metered/paid-overage/per-call fallback observation, paid/fallback routing,
lost transport evidence, containment uncertainty, source mutation, cleanup
failure, schema inconsistency, verifier integrity failure, tracked source
change, or manifest-hash change. If subscription entitlement or billing-route
evidence cannot be established before the first provider call, the campaign
blocks before that call. A post-start infrastructure defect ends version 2;
repair requires a new preregistration. No case or policy is rerun to balance
an unfavorable result. Bounded within-case retries remain allowed.
\`INVALID_MODEL_RESPONSE\` and \`PDB_NOT_REACHED\` are valid terminal results.

## Harness modes

\`python scripts/quixbugs_paired_pilot.py\` defaults to \`validate\` and never
contacts a provider. \`validate\`, \`plan\`, \`dry-run\`, and \`qualify\` are
model-free. \`live\` fails closed unless a separate matching authorization
artifact is supplied, and live execution remains unavailable in this
preregistration task. The validator entry point
(\`python scripts/validate_quixbugs_paired_pilot.py\`) validates both tracked
manifest versions: v1 (\`PAIRED_PILOT_V1.json\`) and v2
(\`PAIRED_PILOT_V2.json\`).

## Campaign-stop provenance

Identical to v1: a prior-case stop carries a SHA-256 of the complete
previously validated trigger result; source mutation is bound to the trigger
case task and source hash; authority-only stops carry a SHA-256 of a typed
authority-check record stored in the validator-owned
\`CampaignResultValidator\` ledger. Prior results and authority records are
never caller-supplied. Every use re-verifies the stored canonical digest, and
plain mappings, including \`validated: true\`, are rejected.

## Infrastructure and semantic outcomes

Identical to v1: infrastructure failures use an exact stage/reason/
classification matrix and a coherent aggregate/terminal transport lifecycle.
The \`repair_outcome\` field is orthogonal: a verifier-accepted repair remains
RESOLVED semantically even if cleanup or evidence packaging makes the case
terminal status INFRASTRUCTURE_ERROR.

## Catalog binding

As in v1, the future live authorization must contain one exact non-null
catalog fingerprint, and contacted cases must match the authorized
fingerprint. v2 additionally requires the exact runtime model identity to be
authorization-bound and observed before contact; the exact runtime model and
catalog identity is intentionally authorization-bound and is not frozen in
this planning manifest.
