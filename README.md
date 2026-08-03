# Agentic Debugging Internship

Research and prototype workspace for an agentic debugging system.

The project investigates the path from traditional debugging, fault localization, automated program repair, LLM-based debugging, and repository-level software engineering agents toward interactive debugger-assisted agents.

## Initial research direction

- Python/PDB-first debugging prototype
- Deterministic debugger and test tools
- Single controller agent before multi-agent designs
- Verifier-backed patch validation
- Comparison against non-debugger baselines such as Agentless, SWE-agent, and AutoCodeRover

## Repository structure

- diary/: internship diary notes
- docs/: project documentation
- prompts/: research and agent prompts
- research/reports/raw/: raw AI-generated research reports
- research/reports/synthesis/: cross-report synthesis
- research/notes/: manual paper notes
- research/papers/: local paper archive; PDFs are gitignored
- TODO.md: project TODO list

## Accepted project status (through 2026-07-31)

An MVP agentic debugging implementation is accepted through Task 9: a single
controller agent, typed deterministic tools (file read, code search, test
run, patch apply), PDB session/runtime skills, and a verifier-backed patch
workflow. Task 10A added the real-model evaluation harness. Task 10B-R1
repaired the initial live protocol and accounting contracts, Task 10B-R3
added bounded invalid-directive retry feedback, and Task 10B-R5 completed the
policy-scoped contract repair in accepted source/merge commit
`63fa27cc4d30490b9770ead3ce14b4b6d3ddf222` (protocol version `1.3`).

[Historical] A private-runner, four-case descriptive matrix completed on
`curated-none-handling-001` through OpenCode Zen using
`deepseek-v4-flash-free` with variant `max`. Static policy resolved both
repetitions; the PDB-on-uncertainty policy resolved neither repetition and
terminated with underlying reason `invalid_model_response` before PDB opened.
Corrective-feedback recovery occurred in 4 of 6 observed feedback episodes.
This small, fixture-specific matrix does not establish causal PDB
effectiveness or general model reliability; see `docs/PROJECT_TRACKER.md`.
It is a historical record of the earlier OpenCode Zen free-tier route and is
not the current implementation route.

## Current status (2026-08-02)

The operational routing authority is `CURRENT_AGENT_ROSTER.md`: DeepSeek V4
Flash through the operator's OpenCode Go subscription is the default
implementation route when a task explicitly authorizes model use; GPT-5.6 High
in a separate ChatGPT conversation owns literature review and deep-research
work; research outputs are non-authoritative until reviewed and incorporated
into tracked project artifacts; every task still requires explicit
authorization for provider/model execution; coding agents must not launch
additional models, research agents, MCP, benchmarks, or paid services unless
the current task explicitly authorizes them.

The QuixBugs paired pilot is planned in two versions. v1
(`docs/QUIXBUGS_PAIRED_PILOT_V1.md`,
`research/quixbugs/PAIRED_PILOT_V1.json`) froze the three-task, six-case
static-versus-PDB feasibility design on the historical OpenCode Zen
zero-price route. v2 (`docs/QUIXBUGS_PAIRED_PILOT_V2.md`,
`research/quixbugs/PAIRED_PILOT_V2.json`) is the derived contract against
accepted baseline `18e067f24c337e7215139373edc699a347cf2127`: the same tasks,
six-case order, budgets, protocol 1.3, qualification contract, and
source-integrity authority, with the route replaced by the fail-closed OpenCode
Go subscription contract (DeepSeek V4 Flash; no Zen/free-tier/Ollama/
alternate-provider/model-substitution/metered/paid-overage/per-call fallback;
block before the first provider call if subscription entitlement or
billing-route evidence cannot be established; truthful provider-reported token
and cost metadata preserved). No exact catalog identifier, OpenCode version,
catalog fingerprint, account status, entitlement, or pricing observation is
invented; the exact runtime model/catalog identity remains authorization-bound,
and live execution remains unavailable until a separate implementation task
supplies an explicit authorization artifact.

## Current status (2026-08-03) — OpenCode Go execution adapter v1

The OpenCode Go execution-adapter wiring is implemented and validated
(adapter-only; no provider contact). `scripts/quixbugs_opencode_go_adapter.py`
provides a strict versioned adapter-configuration contract
(`quixbugs-opencode-go-execution-adapter-v1`; non-executable tracked template
`research/quixbugs/OPENCODE_GO_EXECUTION_ADAPTER_TEMPLATE.json`, rejected as an
active configuration; real configurations live outside tracked source in the
ignored `operator/` location), a runtime identity binding that derives the
exact catalog-qualified runtime model identity from validated authorization
and route evidence and rejects the historical `opencode/deepseek-v4-flash-free`
Zen identity, an explicit transport factory that adapts the accepted protocol
transport with structured argv, explicit cwd, bounded environment allowlists,
bounded stdout/stderr/diagnostics, process-group-aware timeout and cleanup,
zero automatic retries/fallback/catalog queries, and binding revalidation
before every provider process attempt, and a case-runner binding that reuses
the accepted QuixBugs live path
(`agentic_debugger/evaluation/live_quixbugs.py`) with one fresh transport/
session/workspace boundary per frozen case, no shared model conversation,
static-baseline PDB prohibition, PDB-on-uncertainty through the accepted
controller gate and budgets (with the runtime identity bound explicitly, never
the historical Zen identity), and full reconciliation with the live runner's
ledger, terminal commitment, authority checks, stop rules, and result
validator. CLI surface: `adapter-template`, `adapter-validate`,
`route-preflight-only` (zero provider processes), `selftest` (synthetic only,
via the deterministic network-incapable `scripts/opencode_go_synthetic_
executable.py`), and `live-wire` (unusable without an actively validated
configuration, explicit operator artifacts, and an explicitly constructed
transport factory). This task used only synthetic executables, deterministic
transport doubles, temporary fixtures, and fake route observations; the
adapter requires, before the real campaign, a real operator authorization
artifact, exact runtime route evidence passing preflight, the adapter's
accepted commit bound in that authorization, the operator-supplied QuixBugs
execution environment, and the operator's explicit authorization. See
`docs/QUIXBUGS_OPENCODE_GO_EXECUTION_ADAPTER_V1.md`. Not started and not
marked complete: operator authorization, real route preflight, real OpenCode
Go execution, the six-case live campaign, empirical evaluation, model
performance, PDB effectiveness, RAG, SFT, and DPO; historical OpenCode Zen
records remain historical.

A bounded surgical repair (2026-08-03) then made the adapter command launch
the accepted protocol wrapper (`scripts/opencode_protocol_transport.py`)
explicitly — `[python, wrapper, --model <runtime id>, --variant <v>,
--route-mode opencode-go, --expected-opencode-version <v>,
--expected-catalog-fingerprint <hex>, --expected-runtime-model-id <id>,
--expected-account-status <status>, --expected-billing-route SUBSCRIPTION]`
with `--evidence-file` owned by the wrapper — instead of a direct OpenCode CLI
command; direct-CLI bypass configurations are rejected
(`DIRECT_OPENCODE_COMMAND_REJECTED`/`WRAPPER_NOT_BOUND`). The wrapper gained
an explicit `legacy` (historical Zen zero-price behavior, unchanged) and
`opencode-go` route mode (catalog prices preserved as observed, exact
launcher-version binding, and the outer-validated model/fingerprint/account/
billing-route evidence required and recorded; no hidden fallback or Zen/
free-tier inference). The case execution cost is now the aggregate of the
finite monetary costs explicitly reported by each provider response
(`provider_telemetry.cost`) — absent stays absent, explicit zero stays zero,
subscription never implies zero, the preflight route-observation cost is never
used as the case execution cost — with the frozen v2 case validator's cost
check relaxed accordingly (directly affected compatibility fix). Synthetic
validation runs the fake OpenCode CLI through the real wrapper (request via
stdin, bounded `opencode run` command construction, response reaching the
model adapter boundary), covering absent/zero/positive cost propagation, and
proves zero real provider calls.

## Current status (2026-08-03) — Operator Authorization and Real Route Preflight v1

The operator preparation flow is implemented and packaged (operator-only;
no real OpenCode inspection command was executed by any implementation
agent). Two focused operator-facing modes extend
`scripts/quixbugs_opencode_go_adapter.py`:

* `route-capture` — a read-only command that runs only local/non-model
  OpenCode inspection commands (`opencode.cmd --version` and
  `opencode.cmd models opencode-go --verbose --pure`), never invokes `opencode
  run`, requires the exact operator-selected runtime model ID (rejecting the
  historical `opencode/deepseek-v4-flash-free` Zen identity and every
  non-`opencode-go/` provider) and variant,
  locates exactly one active catalog entry, records its observed status,
  variant availability, and finite pricing metadata, requires explicit
  operator-supplied account status, subscription entitlement
  confirmation/reference, and a billing-route assertion, records every
  denial/fallback observation explicitly, and writes a strict
  `quixbugs-route-evidence-v1` artifact (accepted by the existing live-runner
  validator) with create-once semantics into the ignored `operator/` storage,
  containing no credentials or raw private account data.
* `operator-bundle` — consumes the accepted route-evidence file and
  materializes the real `quixbugs-paired-pilot-authorization-v1` artifact and
  the real `quixbugs-opencode-go-execution-adapter-v1` configuration, bound
  to the actual clean Git HEAD observed (read-only) when the operator runs the
  command after this task has been accepted and merged — never to a
  caller-supplied commit and never to the task baseline (the task baseline
  `618c33ff186493892665ca1233c3edd8b2eec13f` is retained only as a minimum
  lineage prerequisite). The observed HEAD must exist, descend from the
  accepted project baseline and from the task baseline, and have a clean
  tracked working tree, a clean real index, and no non-ignored untracked
  files; HEAD and repository cleanliness are re-checked immediately before
  the artifacts are created and any drift fails closed with no active
  artifact written. The artifacts are also bound to the frozen manifest hash
  `bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171`, the
  exact six frozen case IDs in order, protocol `1.3`, the exact observed
  OpenCode version, runtime model ID, variant, and catalog fingerprint, the
  account status and subscription billing route, one operator authorization
  ID, one fresh attempt identity and output root, an explicit bounded
  validity period, and the operator-resolved Python executable, repository
  wrapper path, working directory, and operator boundary root. Dirty/staged
  source, drift, occupied targets, template values, route drift, unknown
  fields, malformed paths, and contradictory subscription/fallback
  assertions are rejected; active operator artifacts are never committed.

A deterministic catalog-entry fingerprint contract is implemented once in
`scripts/opencode_protocol_transport.py` (parse the exact selected entry,
serialize with the project's canonical JSON rules, SHA-256) and is used
identically in route evidence, authorization, adapter configuration, and
wrapper verification; the wrapper's OpenCode Go preflight independently
recomputes the selected entry fingerprint and compares it with the
authorization-bound expected fingerprint before any model process may run.
The materialized artifacts work with the existing zero-provider-process
`route-preflight-only` command (PowerShell example in
`docs/QUIXBUGS_OPENCODE_GO_EXECUTION_ADAPTER_V1.md`). Tests were added for
deterministic fingerprinting, exact selected-entry matching, malformed/
duplicate/inactive/missing-variant/historical-free-route rejection, route
evidence schema production, authorization/config cross-binding, dirty-Git and
occupied-target rejection, execution-commit binding to a clean descendant
HEAD different from the task baseline, rejection of nonexistent,
non-descendant, dirty, staged, and drifting HEADs, wrapper fingerprint
mismatch rejection, and the proof that capture never constructs or invokes
`opencode run`. Validation was
intentionally not run by the implementation agent; validation belongs to
FirstMate. Real operator preflight remains pending FirstMate review and
Onur's manual execution; `TODO.md` keeps this item open. Not started and not
marked complete: real operator authorization execution, real route preflight,
real OpenCode Go execution, the six-case live campaign, empirical evaluation,
model performance, PDB effectiveness, RAG, SFT, and DPO.

## Current status (2026-08-03) — OpenCode Go catalog provider selection

Real Windows inspection proved that Go mode previously queried
`opencode.cmd models opencode --verbose --pure` and therefore saw the
historical Zen/free identity `opencode/deepseek-v4-flash-free`. The
route-capture and protocol-wrapper paths were repaired so that:

* OpenCode Go mode queries exactly `models opencode-go --verbose --pure`
  (`scripts/opencode_protocol_transport.py` selects the catalog provider by
  route mode; legacy mode continues querying `models opencode` unchanged);
* Go runtime identities must use the `opencode-go/` provider prefix —
  `opencode/`, the historical `opencode/deepseek-v4-flash-free` identity, and
  any other provider are rejected before model execution (wrapper preflight,
  operator `route-capture`, `operator-bundle` route evidence, and adapter
  configuration validation all gate on the prefix);
* the selected `opencode-go/<model>` catalog entry is fingerprinted and the
  wrapper's OpenCode Go preflight independently recomputes and verifies that
  fingerprint against the authorization-bound expected fingerprint;
* route capture still never constructs or runs `opencode run` (the operator
  example now uses `--runtime-model-id opencode-go/deepseek-v4-flash`; no
  model variant is invented before the real Go catalog is inspected);
* the existing TODO item stays open pending the repeated Windows route
  capture. No test/build/lint/compile validation was run (FirstMate owns
  validation); no real OpenCode command, catalog, provider, or paid endpoint
  was contacted; no commit/stage/push.

The QuixBugs paired-pilot v2 live-runner infrastructure is implemented and
validated (runner-only, accepted baseline `28ec7754336fc53f21ebbae8a851b33e26714932`):
a strict versioned authorization contract
(`docs/QUIXBUGS_PAIRED_PILOT_V2_AUTHORIZATION_V1.md`; non-authorizing schema
reference `research/quixbugs/PAIRED_PILOT_V2_AUTHORIZATION_TEMPLATE.json`,
rejected by the validator; real authorizations live outside tracked source in
the ignored `operator/` location), a mandatory pre-provider route gate that
blocks before any provider process on missing/unobservable/stale/
contradictory/substituted evidence, frozen six-case sequential orchestration
with fresh per-case boundaries, fail-closed stop/abort behavior with honest
partial campaign records, deterministic versioned output packages, and a
durable attempt ledger with no-rerun enforcement. A bounded material repair
hardened the boundary: `accepted_campaign_commit` is now the exact commit
whose code will execute the campaign (actual HEAD equality, commit existence,
baseline descent, and clean tracked working tree + Git index verified before
ledger claim/preflight/transport creation and re-verified before every case;
post-preflight drift stops the campaign with typed authority evidence);
raw route evidence is strict (every acceptance-critical field explicitly
typed, no defaulting, no fabricated zeros/false flags, account-status and
timestamp freshness enforced); one output root belongs to exactly one attempt
identity with create-once, never-overwritten artifacts and a non-authoritative
`rejections/` directory; the ledger claim is cross-process exclusive, the
terminal ledger state finalizes in lockstep with `campaign.json` (created
first, ledger second, so a `COMPLETED` ledger always has a matching validated
terminal campaign.json), and lifecycle counts reconcile exactly with the
frozen six cases. A second bounded repair hardened the boundary further: the
`.attempt-owner` claim is the single-winner gate (a second process never
passes, even with matching identity/hash; typed errors distinguish duplicate
attempts from owner conflicts); occupied output roots (any pre-existing file,
directory, symlink, or contradictory owner data) are rejected before claim
with zero provider activity; repository state and tracked authorities are
re-verified after every case and immediately before terminal ledger
finalization, so source drift can never produce a `COMPLETED` campaign; and
all numeric evidence must be finite with strict `allow_nan=False` JSON
everywhere. The runner
reuses the accepted validator path and is wired into the paired-pilot entry
point (`python scripts/quixbugs_paired_pilot.py preflight|live|template`);
live execution is impossible without the strict authorization artifact, the
verified execution commit, a successful route gate, and an explicitly
configured provider transport and case runner. This task used only synthetic
transports, temporary fixtures, and deterministic test doubles, with zero
provider calls proven by counter. Operator guide:
`docs/QUIXBUGS_PAIRED_PILOT_V2_LIVE_RUNNER_V1.md`. The separate future task
(real authorization + route evidence + transport/case runner) is not started;
no live campaign or accepted benchmark was run.

BugsInPy execution remains blocked by its license gate. A resource-limited
QuixBugs (Python `gcd`) real no-model smoke completed successfully through
the accepted WSL2/Bubblewrap infrastructure, extended with a
live-self-tested `prlimit` CPU/memory/process-count profile: pinned revision
`4257f44b0ff1181dedaedee6a447e133219fcebf`, verdict
`ACCEPT CANDIDATE — REAL SMOKE PASSED`. See
`docs/QUIXBUGS_SMOKE_USAGE_V1.md`. That single-task smoke has since been
expanded into an eight-task no-model gold baseline on the same pinned
revision (`gcd`, `bucketsort`, `find_in_sorted`, `flatten`, `kth`, `hanoi`,
`is_valid_parenthesization`, `kheapsort`), reusing the same adapter, WSL
runner, resource profile, and verifier: 8/8 selected tasks solved (gold
patch verified end-to-end), verdict
`ACCEPT CANDIDATE — EIGHT-TASK BASELINE COMPLETE`. See
`docs/QUIXBUGS_EIGHT_TASK_BASELINE_V1.md`. Both validate infrastructure
only — no model, PDB, or broader benchmark campaign was run; every "patch"
applied is the literal upstream buggy→corrected diff, not a generated one.
No external dataset execution or larger live policy comparison is justified
until containment, task mapping, and a controlled real-model path that
actually opens PDB are ready.

Dataset and Evaluation Decision v1 selects BugsInPy as the primary external
dataset, QuixBugs Python as fallback, and the current five curated fixtures as
the architecture smoke gate. RAG is NO-GO-FOR-NOW for a research comparison,
SFT is DEFER, and DPO/preference optimization is NO-GO-FOR-NOW. See `TODO.md`
and `docs/DATASET_EVALUATION_DECISION_V1.md`.

[Historical] The Model, RAG, Fine-Tuning and DPO Decision Gate v1
(`docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md`) and the Final Technical Report
and Demo Package v1 (`docs/FINAL_TECHNICAL_REPORT_V1.md`,
`docs/DEMO_GUIDE_V1.md`) are complete and accepted as of 2026-07-31,
documentation-only, from baseline `2236775`. The Decision Gate reaffirms RAG
NO-GO-FOR-NOW, SFT DEFER, and DPO NO-GO-FOR-NOW, and at the time added
PROCEED (narrow) on future model-access strategy — the smallest credible next
experiment was one QuixBugs task under the static-baseline policy through the
existing protocol-1.3 live harness on the then-current free-tier route, not a
broader or paid campaign — and records that the eight-task QuixBugs baseline
is sufficient evidence for infrastructure validation only, not for model
selection, training, or generalization claims. That free-tier PROCEED
predates the operator-selected OpenCode Go subscription route recorded in
`CURRENT_AGENT_ROSTER.md` and the paired-pilot v2 contract. The Final
Technical Report synthesizes the architecture, dataset/provenance decisions,
sandbox and containment boundaries, BugsInPy's license block, the QuixBugs
methodology and results (and what they do not prove), and limitations/future
work. The Demo Guide reuses only existing entry points (the Task 9 offline
demo and the QuixBugs WSL smoke/baseline scripts); it adds no parallel demo
framework and states plainly that it validates evaluation infrastructure, not
model debugging performance. No model, RAG, training, PDB, or paid API ran to
produce any of this, and the accepted QuixBugs benchmark campaigns were not
rerun.

A final bounded repair added the crash-safe terminal package commitment and
authority-invalidated case accounting: terminalization is a three-step
durable protocol (`campaign.json` PREPARED payload, ledger terminalization,
create-once `terminal-commit.json` written last and binding the attempt
identity, authorization hash, execution commit, status, campaign SHA-256,
terminal ledger entry SHA-256, manifest hash, and case inventory); no
standalone `campaign.json` is accepted as a terminal campaign without the
commitment, and `verify_attempt_package` plus every loader reject
uncommitted/interrupted packages (`TERMINAL_COMMIT_MISSING`). Cases whose
post-case authority check fails are authority-invalidated: excluded from
`completed_case_count`, counted in `invalidated_case_count`, preserved only
as quarantined evidence, with reconciliation
completed + blocked + aborted + invalidated + unstarted == 6.
