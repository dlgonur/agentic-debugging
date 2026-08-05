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

The QuixBugs paired pilot has three versioned manifests. v1
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
supplies an explicit authorization artifact. v3
(`research/quixbugs/PAIRED_PILOT_V3.json`, canonical SHA-256
`f5f513a16008ce807b4ed248e0310958940aefd348199e77dc0bbabc9a9e45cf`)
preserves the v2 tasks, ordering, budgets, qualification authority, and route,
and adds the `VALIDATION_NOT_REACHED` terminal plus candidate provenance for
an observed static-baseline case that applied a patch but exhausted public
evidence before entering Validate. v4
(`research/quixbugs/PAIRED_PILOT_V4.json`, canonical SHA-256
`020dfc1f7b8f23aa96a4d7c7942429e306cc290906abfed5ce96cde22b90354d`)
adds the v4-only verifier-authoritative classification and the budget-terminal
matrix for the observed v3 completed post-apply public-evidence exhaustion
shape (attempt `fddf1e39...`). The next authorized live attempt must use
v4 explicitly; the operator CLI retains a v2 default only for compatibility.

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

A bounded directive-transport repair (2026-08-03, transport-only) fixed the
final protocol blocker exposed by the first provider-connected six-case
attempt (`705aa047...`; provider-connected but protocol-invalid, not a valid
static-versus-PDB experiment): the sanitized public request is now supplied
inline inside the single OpenCode user message (canonical compact JSON
between `=== BEGIN PUBLIC REQUEST ===` / `=== END PUBLIC REQUEST ===`
delimiters) instead of a model-readable `--file` (removed from the real
`opencode run` command; Read/Bash/edit/write stay denied, isolated `--dir`
kept); directive extraction is schema-aware - every JSON object candidate is
validated against the directive schema, action contracts, and controller
context embedded in the request, exactly one fully valid directive is
accepted, zero/ambiguous are rejected, strict top-level directive fields are
enforced (additional fields rejected, never normalized), and copied
request/config objects are ignored only because they fail directive
validation; rejected directives return one compact machine-generated
correction message (the precise bounded validation reason, required
`kind: [...]` envelope, one-JSON-object rule, no tools/code fence/
explanation, never the previous response) that the adapter converts into the
accepted bounded directive-rejection so the existing directive-feedback
cycle carries the exact correction to the model.

A follow-up material repair (2026-08-03, transport-only) replaced the
cmd.exe batch-shim message ceiling with native executable execution: the
wrapper begins from the verified `opencode.cmd` launcher, resolves the
native `opencode.exe` through the trusted npm package root
(`<launcher-dir>\node_modules\opencode-ai` — established path
`node_modules\opencode-windows-x64\bin\opencode.exe`, with the baseline x64
platform package and the direct package `bin` explicitly allowlisted;
hard-linked copies of the single platform binary count as one; exactly one
unique native binary must remain; root containment, regular file, and
version equality with the launcher and the authorization-bound expected
version are required; zero, multiple distinct, and path-escape candidates
fail closed) and invokes it directly for `opencode run` (`shell=False`,
never a silent fallback to the batch shim, PATH lookup, PowerShell, shell
interpolation, or another executable), so the inline message supports the
full frozen public-evidence budget. The 20,000-byte public-evidence limit
applies to the canonical public request serialization, not to the complete
user message — the actual frozen Understand-stage messages are 9189-9752
bytes and canonical requests up to 20000 bytes are accepted with the
canonical request never reduced or truncated. The fully constructed native
command is checked against a conservative Windows command-line bound
(`MAX_NATIVE_COMMAND_LINE_CHARS = 30000` via `subprocess.list2cmdline`,
below the CreateProcess maximum) and fails closed before process creation.
Short inspection commands may continue through the launcher; only bounded
resolution evidence (strategy, package-relative native path,
regular-file/root-containment/version-match flags) is recorded. Diagnostic
classifications (empty output, text without a protocol directive, no JSON
object, zero valid directives, multiple valid directives) are preserved.
Preflight/effective-command validation follows the inline contract, and
audit evidence records only the request hash and byte count. Legacy
extraction without a protocol
`directive_schema` is unchanged. The Authorized Six-Case Live Campaign
remains open.

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

## Current status (2026-08-03) — QuixBugs multi-task PDB live-wire repair

The frozen six-case campaign starts with a `pdb-on-uncertainty` case
(`quixbugs-find-in-sorted-smoke-v1`), but the established live path still
locked PDB to the historical `quixbugs-gcd-smoke-v1` task, always prepared
the gcd probe, could not execute the reviewed task-local probes frozen in
`PAIRED_PILOT_V2.json`, and called one zero-argument generic facts provider
per task while the QuixBugs dependency gate requires `DependencyPreparation`
bound to the exact task manifest, fingerprint, algorithm, and revision —
so `live-wire` aborted before the intended six-case comparison. The
established live path was repaired (no parallel campaign runner):

* `run_live_quixbugs_case` now takes an explicit task-local `RuntimeProbe`
  for `pdb-on-uncertainty`: static-baseline accepts no probe and keeps zero
  PDB access; PDB requires the selected task's own reviewed probe validated
  against the task ID (the default gcd probe keeps its gcd lock), buggy
  module path, corrected/test/support exclusion, reviewed target symbol,
  source containment, and a resolvable breakpoint anchor; probe preparation
  uses `prepare_quixbugs_pdb_probe`; the historical standalone GCD APIs and
  their default GCD lock are unchanged, and the contained-PDB, resource,
  cleanup, and identity gates are not weakened.
* `OpenCodeGoCaseRunner` resolves the exact inventory entry per frozen case,
  builds each PDB case's probe only from that entry's frozen `runtime_probe`
  fields (never from corrected source, tests, model output, or runtime
  guesses), rejects missing/malformed/mismatched/duplicate probe metadata
  before provider interaction, and passes the probe only for
  `pdb-on-uncertainty` (the three selected PDB tasks:
  `quixbugs-find-in-sorted-smoke-v1`,
  `quixbugs-is-valid-parenthesization-smoke-v1`, `quixbugs-hanoi-smoke-v1`).
* The facts-provider contract is now task-bound:
  `provide(manifest_path: str) -> QuixBugsPreflightFacts`; the case runner
  requests facts separately for every frozen case with the exact manifest
  path, requires an exact `QuixBugsPreflightFacts` result whose dependency
  preparation matches the selected task manifest, and rejects zero-argument
  generic facts, wrong-task facts, and malformed results. `--facts-provider
  module:callable` remains the explicit operator selection.
* `scripts/quixbugs_live_wire_environment.py` is the small operator facts
  provider: it reuses the accepted read-only WSL/Bubblewrap environment
  readiness (never installs/clones/resets/cleans/downloads), creates
  task-bound verified facts from the selected manifest, and exposes
  `describe_environment()` returning the existing repository root and
  sources parent needed to materialize `quixbugs-environment.json`.
* Focused tests prove per-case probe delivery, static zero-PDB preservation,
  non-GCD PDB acceptance with a reviewed probe, pre-provider rejection of
  missing/mismatched probe metadata, unchanged GCD legacy APIs, per-case
  exact-manifest facts requests, wrong-task facts rejection, and six-case
  binding entry with synthetic transport and no real provider.

Validation was intentionally not run (FirstMate owns validation); no real
OpenCode command, catalog, provider, or paid endpoint was contacted; no
commit/stage/push. The live campaign TODO stays open pending FirstMate
review and real operator execution; not marked complete: operator
authorization execution, real route preflight, real OpenCode Go execution,
the six-case live campaign, empirical evaluation, model performance, PDB
effectiveness, RAG, SFT, and DPO.

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

## Current status (2026-08-04) — case-budget terminal and paired-pilot v3

Live attempts proved that valid public-evidence exhaustion could occur before
PDB, after a completed unresolved or resolved verifier lifecycle, or after a
static case applied a candidate but before Validate. The runner now treats
only schema-supported, internally consistent exhaustion shapes as honest
case-level terminals, preserves all completed provider/controller/verifier
accounting, writes the case record, and continues to the next frozen case.
Corrupt counters, contradictory evidence, and unsupported shapes still abort.
Paired-pilot v3 adds the last required terminal representation without
weakening the 20,000-byte limit or the verifier authority.

The next live campaign remains open and unauthorized. Before it can run, the
focused adapter/self-test validation must pass, the repository must be clean
at the accepted execution commit, and the operator must create fresh v3 route
evidence, authorization, adapter configuration, attempt identity, and output
root. Every v3 operator command must pass the v3 manifest explicitly. No
campaign result or PDB-effectiveness claim exists yet.

### Reusing the curated-task correctness authority

The smallest existing entry point for a saved unified diff is the Python API;
no additional verifier CLI or correctness path is needed:

```python
from pathlib import Path

from agentic_debugger.evaluation.verifier import EvaluationVerifier

root = Path.cwd()
task_path = root / "agentic_debugger/datasets/curated/curated-off-by-one-002/task.json"
patch_path = Path("candidate.patch")
result_path = Path("verification-result.json")

result = EvaluationVerifier(str(root)).evaluate(
    str(task_path),
    patch_path.read_text(encoding="utf-8"),
)
result_path.write_text(result.to_json() + "\n", encoding="utf-8")
```

Select exactly one of the five curated `task.json` files and provide a unified
diff whose paths are relative to that fixture. `EvaluationResult.to_json()` is
the durable record: it includes lifecycle status, semantic outcome, baseline,
patch and syntax records, fail-to-pass and pass-to-pass checks, full-suite
evidence, bounded counters, diagnostics, workspace cleanup, and canonical
fixture immutability. The verifier copies the fixture to a disposable workspace
and cleans it after success or failure; only the caller-selected JSON output is
persistent.

## Current status (2026-08-05) — campaign infrastructure on main, V4 attempt record, QLoRA implementation

The campaign infrastructure and the paired-pilot v4 terminal contract are now
accepted on `main` through commit `0abb588` (`eb63c76` hardened the campaign
budget and verifier path; `9f53df7` added the actual V4 interrupted budget
terminal; `0abb588` added the terminal, exact-identity validation, and
fail-closed budget-exhaustion provenance infrastructure — run persistence,
campaign-record validation, and attempt-package verification). Accepted
campaign validation: the focused campaign integration suite passed 389 tests;
the bounded full suite produced 3394 passed, 3 skipped, and the same six
pre-existing OpenCode wrapper/transport failures — no new failure was
introduced.

Note on recorded-case identity: the sanitized attempt fixture and replay
assertions accepted at `0abb588` associated the two observed shapes with the
wrong frozen cases (the malformed shape with `is-valid-parenthesization` /
`pdb-on-uncertainty` and the applied-patch interrupted shape with
`find-in-sorted` / `pdb-on-uncertainty`). The 2026-08-05 Friday-readiness
candidate in this branch corrects that fixture/test identity mapping using
the preserved campaign record, private transport evidence, provider-reported
cost sums, and the frozen v4 case order. Production budgets, the frozen
manifest, route, provider, authorization, and controller behavior are
unchanged.

The recorded V4 attempt (`quixbugs-paired-pilot-v4-attempt-3b5d7488...`,
2026-08-04, preserved under ignored `operator/`) has these exact case
boundaries:

- **V4 Case 1** — `quixbugs-find-in-sorted-smoke-v1` under
  `pdb-on-uncertainty` (order 1): 10 provider process attempts, 9 logical
  model calls, 1 bounded retry, 26,139 cumulative public-evidence bytes, every
  patch attempt rejected as a malformed unified diff (hunk header declared
  `old_count=7` while the body carries 6 lines), no candidate applied, zero
  verifier runs, provider-reported cost `$0.007378`. Terminal:
  `INFRASTRUCTURE_ERROR` / controller stage.
- **V4 Case 2** — `quixbugs-find-in-sorted-smoke-v1` under
  `static-baseline` (order 2): 15 provider process attempts, 14 logical model
  calls, 1 bounded retry, 38,534 cumulative public-evidence bytes, a candidate
  applied with Validate visited, zero verifier runs, run interrupted,
  provider-reported cost `$0.012323`. The original campaign aborted
  `ABORTED / BUDGET_EXCEEDED` because the shape was unrepresentable; the
  accepted repair now materializes both shapes as schema-valid terminals
  (`INFRASTRUCTURE_ERROR`, `ABORTED / INTERRUPTED`) with the exact observed
  byte counts preserved in machine-readable `budget_exhaustion` provenance and
  counters clamped to the frozen 20,000-byte limit.

This repair does not establish a verifier-confirmed live repair, does not
demonstrate any live PDB benefit, and is not a post-repair provider campaign.
The Authorized Six-Case Live Campaign remains open and unauthorized; the next
authorized attempt must use `research/quixbugs/PAIRED_PILOT_V4.json`
explicitly with fresh operator artifacts.

The QLoRA experiment implementation is accepted at commit `3f0d3e7` on the
unmerged branch `experiment/qlora-patch-pilot-v1` (FirstMate implementation
review passed), including the tracked `independent_ai` audit contract and
run-provenance implementation. Its owner suite review: 3457 passed, 3 skipped,
36 unrelated pre-existing OpenCode transport/wrapper failures, no
QLoRA-focused failure.

QLoRA status (2026-08-05):

- The owner-delegated independent FirstMate AI audit of the 75 frozen corpus
  rows is complete externally: 39 ACCEPT / 36 REJECT with a disclosed AI
  reviewer identity. This is an AI audit, not a human audit, and must never be
  described as human review; final corpus acceptance and the remaining
  fail-closed audit/corpus-quality decisions remain pending.
- Final QLoRA training was externally authorized by FirstMate on 2026-08-05.
  No accepted final-training artifact or result exists yet; final-training
  results remain pending FirstMate artifact review.
- Held-out generation and the base-versus-tuned comparison remain
  unauthorized. No predicted training values or completion claims are
  implied.
- The tracked freeze record at branch head `3f0d3e7` still contains
  `final_training_authorized: false` and `held_out_generation_authorized:
  false`; that is the historical branch-bound freeze record, not evidence
  about the current external operational authorization of final training
  (which was granted on 2026-08-05).
