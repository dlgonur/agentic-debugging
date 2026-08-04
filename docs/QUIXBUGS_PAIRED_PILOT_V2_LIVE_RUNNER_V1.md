# QuixBugs paired-pilot v2/v3/v4 live runner (operator guide)

This document describes the fail-closed live-runner infrastructure for the
frozen QuixBugs paired-pilot v2, v3, and v4 campaigns. The next authorized
execution is v4 (`research/quixbugs/PAIRED_PILOT_V4.json`, canonical SHA-256
`020dfc1f7b8f23aa96a4d7c7942429e306cc290906abfed5ce96cde22b90354d`);
v2/v3 remain supported as the frozen derivation/compatibility contracts. All
use accepted baseline `28ec7754336fc53f21ebbae8a851b33e26714932` and live
protocol `1.3`.

Implementation: `scripts/quixbugs_live_runner_v2.py`, wired into the accepted
paired-pilot entry point `scripts/quixbugs_paired_pilot.py` (modes `preflight`,
`template`, and `live` with `--preflight-only`). The runner reuses the
accepted validator path (`quixbugs_paired_pilot`) for manifest validation,
preflight-failure derivation, and strict in-order case-result validation; it
does not create a parallel evaluation framework.

## Runner lifecycle

1. **Authorization boundary** — the artifact is validated strictly against the
   selected v2/v3 manifest (see `docs/QUIXBUGS_PAIRED_PILOT_V2_AUTHORIZATION_V1.md`).
   Rejection produces a `REJECTED` campaign record with zero provider activity.
2. **Repository baseline check** — the current Git HEAD must equal the accepted
   baseline `28ec7754336fc53f21ebbae8a851b33e26714932`; otherwise `REJECTED`.
3. **Attempt ledger claim** — a durable ledger inside the output root rejects
   duplicate attempts, crashed `STARTED` attempts, reruns of a used
   authorization, and the same authorization against a changed manifest,
   baseline, route, or case order; otherwise `REJECTED`.
4. **Pre-provider route gate** — the injected route-evidence provider must
   establish the exact subscription route (billing route `SUBSCRIPTION`,
   confirmed entitlement, exact runtime model identity, exact OpenCode
   version, exact catalog fingerprint, no Zen/free-tier/Ollama/alternate
   provider/model substitution/metered/paid-overage/per-call evidence).
   Missing, unobservable, stale, contradictory, substituted, or unsupported
   evidence blocks with the frozen `live-pre-provider` failure category and
   zero provider activity (`BLOCKED_PRE_PROVIDER`).
5. **Six-case execution** — the six frozen cases run strictly in order, never
   in parallel, with one fresh transport/session/workspace boundary per case
   and deterministic per-attempt IDs. Every produced case record must pass the
   selected frozen result validator (`quixbugs-paired-pilot-result-v2`,
   `quixbugs-paired-pilot-result-v3`, or `quixbugs-paired-pilot-result-v4`)
   before it is written. v3 admits the
   `VALIDATION_NOT_REACHED` pre-Validate budget terminal and records
   `candidate_provenance`; v2 does not. v4 adds the verifier-authoritative
   classification (a case whose verifier executed is classified by the
   verifier semantic outcome before the `PDB_NOT_REACHED` rule) and the
   budget-terminal matrix for the completed post-apply public-evidence
   exhaustion shape (RESOLVED/UNRESOLVED with accounting preserved, the
   exact observed byte count in the termination detail, and the counter
   clamped to the frozen 20,000 limit), plus `VALIDATION_NOT_REACHED` for
   pdb-on-uncertainty / Validate-visited stops and post-contact
   controller/cleanup/evidence-packaging `INFRASTRUCTURE_ERROR`
   terminalization.
6. **Terminal campaign record** — `campaign.json` is written atomically only
   after the campaign ends (`COMPLETED`, `PARTIAL`, `ABORTED`, `BLOCKED`, or
   `REJECTED`); case records are written per case; the ledger is updated
   atomically.

## Case-runner contract (task binding)

The explicitly configured case runner (the OpenCode Go execution adapter's
`OpenCodeGoCaseRunner`) binds every frozen case to its task:

* each `pdb-on-uncertainty` case receives the exact task-local `RuntimeProbe`
  built from the frozen inventory entry's reviewed `runtime_probe` fields for
  the selected task — never from corrected source, tests, model output, or
  runtime guesses; static-baseline cases receive no probe and retain zero PDB
  access; missing, malformed, mismatched, or duplicate probe metadata is
  rejected before any provider interaction;
* facts are requested separately per case through the task-bound contract
  `provide(manifest_path: str) -> QuixBugsPreflightFacts` with the exact task
  manifest path; the result must be an exact `QuixBugsPreflightFacts` whose
  dependency preparation matches the selected task manifest (task ID,
  manifest fingerprint, authority revision, algorithm, pinned recipe);
  zero-argument generic facts providers, wrong-task facts, and malformed
  results are rejected before any provider interaction.

The runner itself never bypasses these bindings: it owns the frozen case
order, ledger, terminal commitment, authority checks, stop rules, and result
validation, and treats every case-runner rejection as a fail-closed typed
abort or block.

## Authorization boundary

The runner cannot create a provider process, transport request, or model call
before every gate succeeds. The strict authorization artifact is the only key;
the tracked template
(`research/quixbugs/PAIRED_PILOT_V2_AUTHORIZATION_TEMPLATE.json`) is rejected.
Real authorizations live outside tracked source in the ignored `operator/`
location.

## Pre-provider gate

`run_route_preflight` validates the authorization, obtains fresh route
evidence, normalizes it into the frozen v2 route-observation shape, verifies
the observation shape, checks the authorization-bound catalog fingerprint,
and derives the controlling preflight failure using the accepted
`_derive_preflight_failure_category` logic. A `PreflightVerdict` carries the
route observation, the failure category, and the frozen
`preflight_failure_evidence` object. Preflight-only mode stops after this
gate.

## Operator preparation flow (route preflight v1)

The operator-facing preparation flow in
`scripts/quixbugs_opencode_go_adapter.py` produces the three external
artifacts the pre-provider gate consumes:

1. `route-capture` — read-only local OpenCode inspection (launcher version +
   model catalog) producing a strict `quixbugs-route-evidence-v1` file with
   the deterministic catalog-entry fingerprint
   (`scripts/opencode_protocol_transport.py`), the observed status, variant
   availability, and finite pricing metadata, the operator-supplied account
   status / subscription entitlement / billing-route assertion, and explicit
   denial/fallback observations; zero `opencode run` invocations;
2. `operator-bundle` — materializes the real
   `quixbugs-paired-pilot-authorization-v1` artifact and the real
   `quixbugs-opencode-go-execution-adapter-v1` configuration bound to the
   **actual clean Git HEAD observed (read-only) when the operator runs the
   command after the task has been accepted and merged** — never to a
   caller-supplied commit and never to the task baseline `618c33ff…`
   (retained only as a lineage prerequisite). The observed HEAD must exist,
   descend from the accepted project baseline and from the task baseline, and
   have a clean tracked working tree, a clean real index, and no non-ignored
   untracked files; HEAD and repository cleanliness are re-checked
   immediately before the artifacts are created and any drift fails closed.
   The artifacts are also bound to the frozen manifest hash, the six frozen
   case IDs, protocol `1.3`, and the exact route-observed identities; the
   artifact must pass the strict authorization validator;
3. `adapter-validate` + `route-preflight-only` — the existing
   zero-provider-process handoff that runs every pre-provider gate.

In `opencode-go` route mode the protocol wrapper independently recomputes the
selected catalog entry fingerprint and compares it with the
authorization-bound expected fingerprint before any model process may run.
The flow is implemented and packaged but not executed by any implementation
agent; real operator preflight remains pending FirstMate review and Onur's
manual execution.

## Preflight-only versus live execution

* `preflight` (or `live --preflight-only`): completes authorization, baseline,
  ledger-free (no attempt claim), and route-gate checks only. It never creates
  a transport and never contacts a provider. With `--route-evidence-json` it
  yields a full verdict; without a route-evidence provider it is rejected with
  zero provider activity.
* `live`: additionally claims the attempt ledger and executes cases. It
  **requires an explicitly configured provider transport factory and case
  runner**; without them the campaign is `ABORTED` with
  `TRANSPORT_NOT_CONFIGURED` / `CASE_RUNNER_NOT_CONFIGURED` and zero provider
  activity. In this task no real transport exists, so live execution is
  rejected; the future execution task supplies the transport bound to the
  accepted QuixBugs live path.

## Frozen case order

1. `quixbugs-paired-pilot-v2:quixbugs-find-in-sorted-smoke-v1:pdb-on-uncertainty`
2. `quixbugs-paired-pilot-v2:quixbugs-find-in-sorted-smoke-v1:static-baseline`
3. `quixbugs-paired-pilot-v2:quixbugs-hanoi-smoke-v1:static-baseline`
4. `quixbugs-paired-pilot-v2:quixbugs-is-valid-parenthesization-smoke-v1:pdb-on-uncertainty`
5. `quixbugs-paired-pilot-v2:quixbugs-is-valid-parenthesization-smoke-v1:static-baseline`
6. `quixbugs-paired-pilot-v2:quixbugs-hanoi-smoke-v1:pdb-on-uncertainty`

No reordering, no parallel execution, no automatic retry of completed or
failed cases, no policy-balancing rerun, no result-dependent case selection,
no model or provider substitution. Static-baseline cases never open PDB;
PDB-on-uncertainty cases may open PDB only through the existing controller
gate and within the frozen budgets. Every budget (model calls, attempts,
retries, directives, hypotheses, patches, verifier runs, PDB openings,
observations, case timeout, public evidence bytes) is enforced from the v2
manifest.

## No-rerun behavior

The attempt ledger (per output root) prevents silent restart, rerun of an
attempted frozen case, replacing failed evidence with a later preferred
result, resume after an integrity/authorization failure without a new explicit
operator decision (a new authorization artifact), and running the same
authorization against a changed manifest, baseline, route, or case order.

## Output package

```
<output_root>/
  campaign.json       # terminal campaign record (atomic, written last)
  ledger.json         # durable attempt ledger (atomic)
  cases/case-<NN>-<case_id>.json   # validated per-case records
  private/evidence.jsonl           # private operator evidence (separately classified)
  preflight evidence is embedded in campaign.json#preflight
```

Public output is sanitized: a public/private boundary violation aborts the
campaign (`PUBLIC_PRIVATE_BOUNDARY_VIOLATION`) before the violating record is
written. Gold patches, corrected source, oracle data, and qualification
evidence never enter the model-visible context or public records.

## Stop behavior

`INVALID_MODEL_RESPONSE`, `PDB_NOT_REACHED`, `PROVIDER_ERROR`, and completed
case outcomes are valid case terminal states and the campaign continues.
Case infrastructure errors (cleanup failure, source mutation, transport
evidence loss, verifier integrity failure, containment uncertainty, result
schema inconsistency) stop the campaign before the next case; the remaining
unstarted cases are written as frozen `campaign-stop` BLOCKED records with
typed trigger or authority evidence. Pre-provider blocks stop the campaign
with the first case BLOCKED and the rest unstarted. Budget violations,
invalid records, sanitization violations, unexpected case-runner failures,
and missing transport/runner configuration abort the campaign honestly
(`ABORTED`); no case is silently skipped, and no case is reported as
attempted when provider contact never occurred.

## What remains prohibited in this task

This task implements and validates the runner only. It does not contact
OpenCode Go, DeepSeek V4 Flash, OpenCode Zen, any live model catalog,
provider, account, entitlement service, paid service, or external model
endpoint; it does not execute the six-case live campaign, the accepted
QuixBugs benchmarks, or the PDB qualification campaign. Only synthetic
transports, temporary fixtures, and deterministic test doubles are used.

## The separate future authorization and execution task

A separate future task must (a) create a real operator authorization artifact
outside tracked source that passes the strict contract, (b) bind the exact
runtime route evidence via the real OpenCode transport preflight, and (c)
supply the explicitly configured provider transport and case runner bound to
the accepted QuixBugs live execution path (`run_live_quixbugs_case`). Until
then, `live` fails closed before any provider contact.

## Material repair: execution-commit binding (Blocker 1)

`authorization.accepted_campaign_commit` is the exact commit whose code will
execute the future campaign. Before ledger claim, preflight, transport
creation, or provider contact the runner independently observes the
repository state and requires: actual HEAD equals the bound commit; the
commit exists; it descends from accepted baseline `28ec7754…`; and the tracked
working tree plus the real Git index are clean (only ignored
operator/output artifacts allowed). Failures are `EXECUTION_COMMIT_MISMATCH`,
`EXECUTION_COMMIT_NOT_FOUND`, `EXECUTION_COMMIT_ANCESTRY_FAILED`, or
`TRACKED_STATE_DIRTY` rejections with zero provider activity. The verified
execution commit is recorded in campaign, case, authority, route-binding, and
ledger evidence, and is re-verified before every case; post-preflight commit
or tracked-state drift stops the campaign with the typed
`TRACKED_SOURCE_CHANGED` authority/campaign-stop evidence (the observed value
is the runner's tracked-source state fingerprint).

## Material repair: strict raw route evidence (Blocker 2)

Raw route evidence must explicitly and correctly type every
acceptance-critical field (provider, model, variant, protocol, exact OpenCode
version, catalog fingerprint, runtime model ID, billing route, entitlement
confirmation, account status, active-model status, variant availability,
every Zen/free-tier/Ollama/alternate-provider/model-substitution/metered/
paid-overage/per-call observation, prices, provider-reported cost, and
`observed_at`). Missing fields are rejected (`MISSING_FIELD`), never
defaulted from the manifest or authorization; missing denial/fallback
observations are never converted to `False`; missing price/cost evidence is
never converted to zero; unknown fields are rejected (`UNKNOWN_FIELD`) unless
they are the versioned optional metadata `schema_version`; account status
must equal `authorization.expected_account_status`
(`ACCOUNT_STATUS_MISMATCH`); `observed_at` must parse
(`INVALID_TIMESTAMP`), must not be materially in the future
(`FUTURE_TIMESTAMP`, 120 s clock-skew allowance), and must be within the
freshness window (`STALE_TIMESTAMP`). The route observation preserves
`account_status` and `observed_at` and is bound to the verified execution
commit. Strict-contract violations reject the campaign
(`ROUTE_EVIDENCE_INVALID:<reason>`) with zero provider activity.

## Material repair: immutable output (Blocker 3)

One output/attempt root belongs to exactly one campaign-attempt identity.
`claim_output_root` atomically creates the `.attempt-owner` record (exclusive
create); an occupied or contradictory root is rejected (`OUTPUT_ROOT_OWNED`)
before the ledger claim. Authoritative artifacts (`campaign.json`, case
records) use create-once semantics (`atomic_create_json`: temp file plus
atomic no-overwrite link or exclusive create) — existing evidence is never
replaced. Rejection records are written to the non-authoritative
`rejections/` directory and can never replace accepted attempt evidence;
pre-execution dispositions (REJECTED, BLOCKED_PRE_PROVIDER) never write
`campaign.json` or claim the ledger.

## Material repair: atomic ledger lifecycle (Blocker 4)

The output-root ownership claim plus the ledger provide cross-process
exclusive claiming (exactly one of two simultaneous claims for the same
authorization/output root succeeds). Missing transport/case-runner
configuration rejects before the ledger claim and never consumes the
authorization. Terminalization is two-phase (see the second-round section
below): the terminal `campaign.json` is created first with create-once
semantics and the ledger is finalized to the same terminal status, so a
`COMPLETED` ledger always has a matching validated terminal `campaign.json`;
the campaign record embeds the same terminal ledger snapshot that exists in
`ledger.json`; ledger-finalization failures never leave a valid-looking
completed campaign artifact (`LEDGER_FINALIZATION_FAILED`). Lifecycle states
reconcile exactly with the frozen six cases
(completed + blocked + aborted + unstarted == 6), and every campaign record
passes the runner-level `validate_campaign_record` consistency validator;
`verify_attempt_package` checks the on-disk package (status, ledger status,
case counts, hashes, provider-call proof).

## Note on preflight-only artifacts

Preflight-only mode never claims an attempt: its result is written to
`preflight.json` (or the `rejections/` directory for a blocked/rejected
preflight) and never touches `campaign.json`, `.attempt-owner`, or the
ledger.

## Material repair (second round): single-winner claim, occupied roots, post-case verification, strict JSON

### Single-winner attempt claim

The exclusive `.attempt-owner` creation (`O_CREAT|O_EXCL`) is the ONLY gate an
attempt can pass, and it never lets a second process return successfully —
even when the attempt identity and authorization hash match. Typed errors
distinguish a same-identity duplicate (`SameAttemptClaimError`, stop reason
`DUPLICATE_ATTEMPT`) from a different-owner conflict (`OutputRootOwnedError`,
`OUTPUT_ROOT_OWNED`); both stop before any ledger mutation. The single-winner
primitive covers the complete transition from output-root acquisition through
the initial durable `STARTED` ledger entry, and a crashed or abandoned claim
is never silently reclaimed (the owner record persists). The deterministic
two-process test uses an explicit barrier so both processes observe the
pre-claim state simultaneously; exactly one claims and the other receives a
typed rejection.

### Occupied output roots

Before creating the owner record or claiming the ledger, the authoritative
attempt root must be absent or structurally empty. Pre-existing
`campaign.json`, `ledger.json`, case files, private evidence, temporary
files, unknown files, directories, symlinks, or contradictory owner data are
rejected (`OutputRootOccupiedError`, stop reason `OUTPUT_ROOT_OCCUPIED`) with
zero case execution and zero provider activity. Pre-claim rejection evidence
is stored OUTSIDE the authoritative attempt root in a parent-level
non-authoritative location (`rejections-<rootname>/`), so it can never make
the root appear usable; preflight-only records also live there.

### Terminalization ordering and honest output integrity

Terminalization is two-phase: the terminal `campaign.json` is created FIRST
(create-once), then the ledger is finalized to the SAME terminal status. A
`COMPLETED` ledger therefore always has a matching, validated terminal
`campaign.json`. On campaign-artifact creation failure the ledger
terminalizes `ABORTED` with `OUTPUT_INTEGRITY_FAILURE` (never `COMPLETED`),
no completed-looking package exists, and the runner never returns
`COMPLETED` when `campaign.json` was not successfully created. On
ledger-finalization failure the just-created `campaign.json` is removed again
(best effort) so no terminal artifact persists without a matching ledger
state.

### Post-case and pre-terminal authority verification

After every case runner returns (including its cleanup/restoration phase),
the runner independently re-verifies the actual Git HEAD, the
authorization-bound execution commit, baseline ancestry, index and tracked
working-tree cleanliness, non-ignored untracked files, and the tracked
manifest and source-integrity authorities; another final authority check runs
immediately before terminal ledger finalization. Drift detected at either
point stops the campaign with the accepted typed
`TRACKED_SOURCE_CHANGED` authority/campaign-stop evidence, the affected case
keeps its truthful record, and the campaign can never return or persist a
terminal `COMPLETED` ledger or campaign (terminal `PARTIAL` with the
`authority_stop` evidence). The dirty repository state is preserved honestly;
no destructive cleanup is attempted.

### Non-finite numeric evidence and strict JSON

All numeric evidence (authorization, route evidence, case outcomes, cost
summaries, timing, budgets, persisted artifacts) must be finite: `NaN`,
`+Infinity`, and `-Infinity` are rejected with `math.isfinite()`, and
booleans are never accepted as numbers (`NON_FINITE_VALUE`). All persisted
JSON (canonical hashing, authorization hashing, atomic JSON creation, ledger
serialization, rejection records, case records, campaign records, private
evidence) is emitted with `allow_nan=False`, and non-finite values are
recursively rejected before any evidence hashing or writing. A serialization
failure fails closed and never leaves a partial authoritative file. Valid
finite zero values are preserved when explicitly observed; zero is never a
replacement for missing evidence.

## Material repair (third/final round): crash-safe terminal commitment and authority-invalidated cases

### Crash-safe terminal package commitment

A standalone `campaign.json` is never an accepted terminal campaign merely
because its internal `status` says COMPLETED/PARTIAL/ABORTED. Terminalization
is a three-step durable protocol:

1. **T1** — the terminal campaign payload is created once
   (`campaign.json`, `commit_state: "PREPARED"`, `terminal_commit: null`);
   it is clearly non-authoritative until committed.
2. **T2** — the attempt ledger is finalized to the same terminal status.
3. **T3** — the create-once `terminal-commit.json` is written LAST, binding
   the campaign-attempt identity, authorization hash, execution commit,
   intended terminal status, the SHA-256 of the actual `campaign.json`, the
   SHA-256 of the exact terminal ledger entry, the frozen manifest hash, and
   the case-record inventory (case IDs, order indices, record hashes).

A process death at any transition (including `SystemExit`, forced
termination, or power loss) leaves either a fully committed and verifiable
package or an explicitly uncommitted/interrupted package; no best-effort
deletion is relied on. `verify_attempt_package()` and every operator-facing
loader require the terminal commitment and reject: campaign without
commitment, ledger without commitment, mismatched campaign/ledger hashes,
wrong status or attempt identity, and any interrupted PREPARED state
(`TERMINAL_COMMIT_MISSING`). Interrupted attempts are never silently resumed
or finished; they remain consumed and require an operator decision.
Deterministic fault injection covers every terminalization step, including a
`BaseException` simulated process death.

### Authority-invalidated cases

A case whose post-case authority check fails (tracked-source, commit,
manifest, qualification, or source-integrity drift) is no longer counted or
classified as completed. Its raw execution outcome is preserved only as
quarantined authority-invalidated evidence (`authority_invalidated_cases`),
recorded with the affected case ID, original raw terminal outcome, authority
failure reason, authority-check record hash, whether provider contact
occurred, and `excluded_from_evaluation: true`. The lifecycle state is
`authority-invalidated`; `completed_case_count` excludes it;
`invalidated_case_count` counts it; and the frozen six-case reconciliation is
completed + blocked + aborted + invalidated + unstarted == 6. Cost/token/
provider-attempt accounting truthfully retains consumed resources, but
success/evaluation counts exclude invalidated cases. Subsequent cases remain
blocked per the frozen campaign-stop contract. Final-case drift yields
`PARTIAL` + `TRACKED_SOURCE_CHANGED` with affected_case_id = the final case,
completed 5, invalidated 1, unstarted 0; pre-terminal drift (all post-case
checks passed) yields `PARTIAL` with affected_case_id null as a
campaign-level authority failure.
