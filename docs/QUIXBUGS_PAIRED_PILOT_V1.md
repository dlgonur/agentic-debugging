# QuixBugs paired pilot v1

This document freezes the first bounded paired static-versus-PDB feasibility
pilot before any live case. It is descriptive and feasibility-oriented, not
statistically powered and not evidence that either policy is superior.

The machine-readable source of truth is
\`research/quixbugs/PAIRED_PILOT_V1.json\`. Its campaign-manifest hash is
\`5d84ea22820ca38ce80dd90a5d36e6f80160220178496950f9b45be41fae19ce\`.
The frozen qualification-contract hash is
\`7246d289fcc689e93d93385751cbae5fa75a3c52e3c04e001f2c977a1990c52d\`; the
sanitized evidence file is bound by SHA-256
\`29851dd98da64a400b030d372d85d02a47f20b0b3cc6c696a6c750be29841c41\`.

The independent source-integrity authority is
\`research/quixbugs/QUIXBUGS_SOURCE_INTEGRITY_V1.json\`, schema
\`quixbugs-source-integrity-v1\`, digest
\`a3ccf9d083f3405f0811b66c69a5e93d8a347d77b5f8ccb9d168d93102bd1977\`.
It records only buggy-source/test hashes and repository/path/revision identity
for all eight canonical tasks.

## Scope and selection

The canonical inventory contains eight task manifests. gcd is excluded because
it was repeatedly used for transport/PDB engineering and prior live-model
implementation. The other seven received bounded, task-local pre-freeze
screening. Every one passed derived dependency, deterministic-baseline,
verifier-lifecycle, source-restoration, and contained-PDB reachability checks.
Each probe points to the task's buggy implementation module, a reviewed target
symbol, and a resolved breakpoint in that same module.

Eligibility is nonjudgmental: eligible task IDs are hashed as
\`SHA-256("quixbugs-paired-pilot-v1:<task-id>")\`, sorted by hash, and the first
three are selected. The selected tasks are:

1. \`quixbugs-find-in-sorted-smoke-v1\`
2. \`quixbugs-is-valid-parenthesization-smoke-v1\`
3. \`quixbugs-hanoi-smoke-v1\`

The full inventory, paths, statuses, source/test hashes, ranking, and case
order are recorded in the JSON manifest and review package.

## Planned route and pair contract

The future route is OpenCode Zen,
\`opencode/deepseek-v4-flash-free\`, variant \`max\`, protocol 1.3. Input and
output provider prices must both be zero. There is no paid fallback, alternate
provider, Ollama fallback, or model substitution. Exact catalog, model,
variant, protocol, and pricing preflight must run immediately before the first
provider call.

Each task has one \`static-baseline\` case and one \`pdb-on-uncertainty\` case.
Each pair shares task material, public request construction, route, transport,
budgets, verifier, containment, restoration checks, telemetry, and evidence
schema. Every case uses a fresh provider process and fresh owned workspace.
The static policy has zero PDB gate openings, sessions, and observations. The
PDB policy can use PDB only through the real policy gate; not reaching PDB is a
valid result.

## Frozen budgets

The harness freezes the accepted live/controller defaults where applicable:

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
explicitly frozen separately, preventing a per-call value from being confused
with the case aggregate. The PDB bounds and verifier bound are explicit
pilot-level caps aligned with the accepted contained-PDB and task verifier
limits. Any future deviation requires a new campaign version.

## No-model qualification

Qualification uses only local pinned QuixBugs material and the accepted
WSL/Bubblewrap boundary. It runs the buggy baseline twice, requires a
COMPLETED evaluator lifecycle for the expected non-resolved no-op outcome,
verifies a private COMPLETED/RESOLVED correct qualification candidate with
expected F2P/P2P behavior, checks canonical source hashes and owned-workspace
cleanup, validates task-local probe identity, and runs a synthetic
qualification hypothesis through the real PDB gate/session. PDB accounting is
read from the named success/failure count fields; it is not inferred from
mapping truthiness or mapping length. Each screened task had one allowed gate,
one session, two successful observations, and zero failed observations.

Qualification details are private evidence. Gold patches, oracle outputs, and
private correct candidates are not copied into the public manifest or provider
requests.

The validator derives qualification status from the bound evidence. It checks
the exact seven-task set, task-manifest fingerprints, source/test hashes,
reviewed probes, COMPLETED evaluator lifecycles, balanced PDB gate decisions,
actual session/observation counts, event sequence/provenance, cleanup, and
canonical-source preservation. Stored PASS strings are not authoritative.

Case results use an explicit terminal matrix with structured transport,
infrastructure, and blocked evidence. Infrastructure classification never
depends on free-text substrings; PDB activity requires the PDB policy, a
reproduced baseline, and an active hypothesis.

LIVE_CASE blocks before provider contact preserve the observed route and
preflight values, including inactive models, unavailable variants, mismatched
route identities, and nonzero prices. They require zero case activity and a
stable reason code supported by a complete structured
\`preflight_failure_evidence\` object. Each reason has one exact predicate;
catalog failure requires an explicit catalog error category and is not inferred
from a false preflight boolean. DRY_RUN blocks use only the dry-run block kind
and synthetic transport. Campaign-stop blocks are reserved for subsequent
unstarted cases and require structured trigger and reason-specific
expected/observed evidence; prose alone is not sufficient. PDB_NOT_REACHED
requires a reproduced baseline, at least one valid directive, one logical
call, successful preflight, a completed non-malformed response, and zero PDB
openings, sessions, and observations.

Every result binds its top-level provider/model/variant to the observed route.
Contacted LIVE_CASE results additionally bind successful preflight to the
authorization artifact's OpenCode version and catalog fingerprint, with zero
prices and no fallback. A structured pre-provider infrastructure failure may
occur before the first logical call; its stage controls the relaxed
pre-contact identity requirements.

Pre-provider failures use a documented controlling-failure precedence:
authorization validity; manifest, qualification, source-authority, and
campaign-commit bindings; provider; model; variant; protocol; OpenCode
version; explicit catalog failure; inactive model; unavailable variant;
nonzero pricing; and fallback/alternate-provider requirements. Each category
also has an exclusive allowed observation state. Catalog failure requires a
NOT_RUN-style route plus an explicit category and error; it is not inferred
from a false preflight flag.

Campaign-stop evidence is typed by reason and requires a frozen prior case or
one of the documented pre-case authority-check identities. Generic
expected/observed strings are not accepted. Infrastructure failures use the
frozen stage matrix from pre-provider through evidence packaging, require
terminal classification `INFRASTRUCTURE_FAILURE`, and distinguish pre-contact
zero activity from post-contact lifecycle evidence. Invalid authorization
claims are derived from the supplied artifact's canonical SHA-256; a missing
authorization argument is the only case that omits an artifact hash.

## Commit binding and dry-run

\`fe91deb273f485c75ad50f58d0623b947f22631a\` is the planning baseline only; it
is not reported as a future campaign execution commit. Before any provider
call, a separately approved authorization artifact must bind the accepted
campaign Git commit, this final manifest hash, the qualification-contract hash,
all six frozen case IDs, the exact route/model/variant/protocol, expected
OpenCode version, expected catalog fingerprint (or the frozen first-preflight
capture procedure), zero-price requirement, and no-fallback requirement.
Dry-run records use null campaign and candidate commits.

The model-free dry-run issues fresh bounded IDs for the case context, adapter
factory, provider-process factory, workspace, controller/session state,
directive feedback, and task memory. It records an ordinary model-result
failure once and advances, while an injected infrastructure failure stops
before the next case. No provider process, network activity, or OpenCode
transport is reachable in this task.

## Stop rules

The campaign stops before the next case on route/model/variant mismatch,
nonzero pricing, fallback routing, lost transport evidence, containment
uncertainty, source mutation, cleanup failure, schema inconsistency, verifier
integrity failure, tracked source change, or manifest-hash change. A
post-start infrastructure defect ends version 1; repair requires a new
preregistration. No case or policy is rerun to balance an unfavorable result.
Bounded within-case retries remain allowed. \`INVALID_MODEL_RESPONSE\` and
\`PDB_NOT_REACHED\` are valid terminal results.

## Harness modes

\`python scripts/quixbugs_paired_pilot.py\` defaults to \`validate\` and never
contacts a provider. \`validate\`, \`plan\`, \`dry-run\`, and \`qualify\` are
model-free. \`live\` fails closed unless a separate matching authorization
artifact is supplied, and live execution remains unavailable in this
preregistration task.


## Campaign-stop provenance

A prior-case stop carries a SHA-256 of the complete previously validated
trigger result. Source mutation is bound to the trigger case task and source
hash, not to the blocked case. Authority-only stops carry a SHA-256 of a typed
authority-check record stored in the validator-owned
`CampaignResultValidator` ledger, registered only after the typed authority
record passes validation. The validator owns the canonical ledgers of
previously validated case results and authority-check records; prior results
and authority records are therefore never caller-supplied and cannot be
constructed, forged, or mutated after storage. Every use re-verifies the
stored canonical digest, campaign-stop triggers resolve only from the
validator's own case ledger, and authority-triggered stops resolve only from
its typed authority ledger. Plain mappings, including `validated: true`, are
rejected. Reason-to-trigger compatibility is frozen and arbitrary prose or
unrelated authority checks are rejected.

## Infrastructure and semantic outcomes

Infrastructure failures use an exact stage/reason/classification matrix and a
coherent aggregate/terminal transport lifecycle. The repair_outcome field is
orthogonal: a verifier-accepted repair remains RESOLVED semantically even if
cleanup or evidence packaging makes the case terminal status
INFRASTRUCTURE_ERROR; campaign analysis must not count that as a clean resolved
case.

## Catalog binding

The future live authorization must contain one exact non-null catalog
fingerprint. The first-preflight capture procedure is not a campaign-v1
freeze mechanism; all contacted cases must match the authorized fingerprint.

