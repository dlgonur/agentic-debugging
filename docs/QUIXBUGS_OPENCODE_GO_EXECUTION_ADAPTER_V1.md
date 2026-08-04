# QuixBugs OpenCode Go execution adapter v1 (operator guide)

This document describes the fail-closed OpenCode Go execution adapter for the
frozen QuixBugs paired-pilot v2/v3 live runner. The next authorized execution
uses `research/quixbugs/PAIRED_PILOT_V3.json` (canonical manifest SHA-256
`f5f513a16008ce807b4ed248e0310958940aefd348199e77dc0bbabc9a9e45cf`);
v2 remains the derivation/compatibility contract. Both use accepted baseline
`28ec7754336fc53f21ebbae8a851b33e26714932` and live protocol `1.3`.

The CLI deliberately defaults to v2 for backward compatibility. Every command
for the next attempt must therefore pass
`--manifest research/quixbugs/PAIRED_PILOT_V3.json`; omission is not a v3
fallback and must block operator review.

The adapter implements and validates the execution wiring only. It never
contacts OpenCode Go, DeepSeek V4 Flash, OpenCode Zen, a model catalog, an
account or entitlement endpoint, or any paid endpoint, and it never executes
the real six-case campaign. All validation in this task used local synthetic
executables, deterministic transport doubles, temporary repositories, and fake
route observations.

Implementation:

* `scripts/quixbugs_opencode_go_adapter.py` — adapter configuration contract,
  runtime identity binding, transport factory, case-runner binding, operator
  route capture, operator bundle materialization, CLI
  (`adapter-template`, `adapter-validate`, `route-preflight-only`,
  `route-capture`, `operator-bundle`, `selftest`, `live-wire`);
* `scripts/opencode_go_synthetic_executable.py` — deterministic test-only
  synthetic OpenCode-compatible executable (network-incapable by construction);
* `research/quixbugs/OPENCODE_GO_EXECUTION_ADAPTER_TEMPLATE.json` — the
  tracked non-executable configuration template (rejected as an active
  configuration);
* `agentic_debugger/evaluation/live_quixbugs.py` — bounded backward-compatible
  extension: the accepted QuixBugs live case accepts an explicit
  `pdb_identity_binding` (provider, model id, variant), an explicit
  task-local `RuntimeProbe` for `pdb-on-uncertainty` (validated against the
  selected task manifest and pinned checkout; the historical default gcd
  probe keeps its gcd lock), and exposes the bounded PDB gate decisions and
  malformed-directive rejections in the case evidence for every policy
  (default behavior unchanged);
* `scripts/quixbugs_live_wire_environment.py` — the small task-bound operator
  facts provider (`provide(manifest_path) -> QuixBugsPreflightFacts` and
  `describe_environment()` for `quixbugs-environment.json`), reusing the
  accepted read-only WSL/Bubblewrap readiness; never installs/clones/resets/
  cleans/downloads.

## Architecture and reuse of accepted paths

The adapter does not create a parallel model-evaluation framework, a second
controller, a second result schema, or an alternative campaign runner. It
reuses:

* the accepted live-runner infrastructure
  (`scripts/quixbugs_live_runner_v2.py`): authorization contract, execution-
  commit verification, pre-provider route gate, attempt ledger, output-root
  ownership claim, frozen six-case sequential orchestration, stop rules,
  terminal package commitment, and the campaign/result validators;
* the accepted validator path (`scripts/quixbugs_paired_pilot.py`);
* the accepted protocol transport
  (`scripts/opencode_protocol_transport.py`), adapted by the transport
  factory with structured argv, an explicit working directory, a bounded
  environment allowlist, bounded captures, and process-group-aware cleanup;
* the accepted real-model QuixBugs execution path
  (`agentic_debugger/evaluation/live_quixbugs.py` →
  `run_live_quixbugs_case`) with its controller, model adapter, protocol
  parsing and malformed-response feedback, QuixBugs workspace/source
  preparation, containment, verifier, PDB gate, event and trajectory logging,
  cleanup, and source restoration.

## Adapter configuration contract

The strict versioned contract (`schema_version`
`quixbugs-opencode-go-execution-adapter-v1`) binds: adapter identity; the
paired-pilot campaign identity and manifest hash; the operator authorization
identity and authorization hash; the authorization-bound execution commit; the
exact operator-resolved executable and the full structured argv (argv[0] must
resolve to the executable, and argv[1] must resolve to the accepted protocol
wrapper `scripts/opencode_protocol_transport.py`); the exact working directory
and operator boundary root; the exact protocol version (`1.3`); the exact
catalog-qualified runtime model identity, provider, model family, variant,
OpenCode version, catalog fingerprint, route class, and account status;
per-call and total-case timeouts; the environment-variable allowlist; bounded
stdout/stderr/diagnostic limits; transport retry limits; explicit denial of
Zen, free-tier substitution, Ollama, alternate providers, model substitution,
metered fallback, paid overage, and per-call billing fallback; no automatic
route discovery; no global model selection; and the required active-
authorization binding.

The validator rejects unknown fields, missing fields, wrong types, string
shell commands, empty argv elements, relative or ambiguous executable paths,
shell metacharacters, an executable or working directory outside the accepted
operator boundary, hidden environment inheritance, credential values embedded
in argv/logs/evidence/tracked configuration, and any authorization, manifest,
protocol, commit, route, catalog, or model mismatch. A direct OpenCode CLI
command that bypasses the accepted protocol wrapper
(`DIRECT_OPENCODE_COMMAND_REJECTED` / `WRAPPER_NOT_BOUND`) is rejected, the
wrapper command must bind `--route-mode opencode-go`
(`ROUTE_MODE_NOT_BOUND`), and every route-binding flag must be bound to the
configuration values (`ROUTE_BINDING_FLAGS_MISSING`). The tracked template is
explicitly non-executable (`template: true`) and fails validation as an active
configuration.

No real active configuration or credential-bearing file is committed. An
active configuration is created by the operator outside tracked source in the
ignored `operator/` location.

## Real wrapper argv shape

The adapter's active structured argv explicitly launches the accepted
protocol wrapper (never a direct OpenCode CLI command):

```
[<operator-resolved Python executable>,
 <repository>/scripts/opencode_protocol_transport.py,
 --model <exact runtime model identity from validated route evidence>,
 --variant <exact variant>,
 --route-mode opencode-go,
 --expected-opencode-version <exact OpenCode runtime version>,
 --expected-catalog-fingerprint <64-hex catalog fingerprint>,
 --expected-runtime-model-id <exact runtime model identity>,
 --expected-account-status <required account/route status>,
 --expected-billing-route SUBSCRIPTION]
```

The adapter appends `--evidence-file <path>` only because the wrapper
explicitly owns that argument. The wrapper performs the protocol conversion,
isolation, directive extraction, usage parsing, redaction, and evidence
handling; the request reaches the wrapper through stdin and the wrapper
constructs the bounded `opencode run ...` command with the exact model and
variant.

The sanitized public request is supplied inline inside the single OpenCode
user message (canonical compact JSON between the exact
`=== BEGIN PUBLIC REQUEST ===` / `=== END PUBLIC REQUEST ===` delimiters),
never through a model-readable `--file`: the real `opencode run` command has
no `--file` argument, and every read/bash/edit/write permission stays denied
so the model cannot read files or call tools. The message also carries a
brief protocol instruction, compact exact output-shape examples (action,
transition, add_hypothesis, revise_hypothesis), and explicit prohibitions
against code fences, explanations, tool calls, protocol/version wrappers, and
alternate envelopes; the allowed actions and their argument contracts inside
the embedded request are authoritative. The canonical request is never
reduced, truncated, omitted, summarized, split, or otherwise mutated, and
must fit inside the frozen public-evidence byte budget
(`MAX_PUBLIC_EVIDENCE_BYTES = 20000`, matching the campaign
`max_public_evidence_bytes`); exceeding it fails closed before any model
process may run, never silently truncating the request the model sees.

Model execution invokes the native `opencode.exe` directly through the
trusted npm-installation resolution contract. The wrapper begins only from
the independently verified `opencode.cmd` launcher path, defines the trusted
npm package root as `<launcher-directory>\node_modules\opencode-ai`, and
resolves the native executable exclusively from an explicit allowlist of
package-managed relative locations under that root — including the
established Windows x64 platform-package path
`node_modules\opencode-windows-x64\bin\opencode.exe`, the baseline x64
platform package, and the direct package `bin` (the npm shim's own target).
The genuine npm layout hard-links the single platform binary into those
locations, so candidates sharing one file identity count as one; exactly one
unique native binary must remain. Every candidate must resolve to an
absolute path inside the trusted root (no symlink/reparse escape) and exist
as a regular executable file; zero candidates, multiple distinct candidates,
and path-escape candidates fail closed. The resolved native must report the
exact same OpenCode version as the batch launcher (and, in OpenCode Go mode,
the exact authorization-bound version), and is used as argv[0] for
`opencode run` with `shell=False` — never a silent fallback to the batch
shim, PATH lookup, environment-supplied executable paths, PowerShell, shell
interpolation, parsing an unrestricted command from the batch file, or
another executable. The cmd.exe batch-shim line limit (~8191 characters)
therefore no longer applies to the inline message; the fully constructed
command is checked against a conservative native Windows command-line bound
(`MAX_NATIVE_COMMAND_LINE_CHARS = 30000` via `subprocess.list2cmdline`, below
the Windows CreateProcess maximum of 32767) and fails closed before process
creation when exceeded. Short non-model inspection commands
(`--version`, `models ...`, `debug config --pure`) may continue through the
established launcher, and the native executable and batch launcher are proven
to represent the same expected OpenCode installation/version. Only bounded
resolution evidence is recorded (resolution strategy `npm-package-layout`,
the package-relative native path, and the regular-file/root-containment/
version-match flags) — never executable bytes or unrestricted environment
data. Evidence records the request as a SHA-256 hash plus byte count, never
as unrestricted request contents.

The 20,000-byte public-evidence limit applies to the canonical public
request serialization, not to the complete user message: a canonical request
up to and including 20000 bytes is accepted and its complete inline message
is constructed unchanged (the canonical request is never truncated,
reduced, summarized, split, or mutated), and the fully constructed native
command is independently bounded by `MAX_NATIVE_COMMAND_LINE_CHARS` and
fails closed before process creation when exceeded.

## Protocol wrapper route modes

`scripts/opencode_protocol_transport.py` supports two explicit route modes:

* `legacy` (default; historical OpenCode Zen behavior preserved unchanged):
  the exact model must be active with zero input/output/cache prices and the
  requested variant available; the catalog query remains
  `models opencode --verbose --pure`;
* `opencode-go`: catalog prices are preserved as observed (never required to
  be zero), the model and variant must be exactly present and active, the
  launcher version must equal `--expected-opencode-version` exactly, and the
  wrapper requires the exact model identity
  (`--expected-runtime-model-id`), catalog fingerprint, account status, and
  billing route already validated by the outer authorization/preflight
  contract — recording them in evidence without hidden fallback, model
  selection, catalog/account re-queries, or Zen/free-tier inference.  Go mode
  queries exactly `models opencode-go --verbose --pure` and requires the
  catalog-qualified identity to use the `opencode-go/` provider prefix:
  `opencode/`, the historical `opencode/deepseek-v4-flash-free` identity, and
  any other provider are rejected before model execution.

In `opencode-go` mode the wrapper **independently recomputes** the exact
selected catalog entry's deterministic fingerprint and compares it with the
authorization-bound expected fingerprint (`--expected-catalog-fingerprint`)
before any model process may run; a mismatch blocks with a catalog-fingerprint
drift error and zero `opencode run` invocations.

## Deterministic catalog-entry fingerprint contract

One deterministic fingerprint is used identically in route evidence, the
operator authorization, the adapter configuration, and wrapper verification:

1. parse the exact selected catalog entry (`providerID`/`id` match, exactly
   one entry);
2. serialize it with the project's canonical JSON rules (sorted keys, compact
   separators, ASCII-escaped, strict finite JSON — the same rules used by the
   paired-pilot validators);
3. SHA-256 of that canonical representation.

Implemented once in `scripts/opencode_protocol_transport.py`
(`catalog_entry_fingerprint`, with shared `select_catalog_entry` and
`catalog_entry_facts` parsing) and reused by the operator route capture and
the operator bundle; the wrapper recomputes the fingerprint from the live
catalog during its OpenCode Go preflight, so route evidence, authorization,
adapter configuration, and the wrapper's preflight comparison all bind the
same independently computed value.

## Operator route capture (`route-capture`)

A read-only operator command that:

* runs only local/non-model OpenCode inspection commands —
  `opencode.cmd --version` and `opencode.cmd models opencode-go --verbose --pure`;
* never invokes `opencode run` (an accidental invocation is a hard error);
* requires the exact operator-selected runtime model ID (catalog-qualified
  `provider/id` with the `opencode-go/` provider prefix, never the historical
  `opencode/deepseek-v4-flash-free` Zen identity or any other provider) and
  variant;
* locates exactly one active catalog entry and records its observed status,
  variant availability, and finite pricing metadata;
* requires explicit operator-supplied account status, subscription
  entitlement confirmation and evidence reference, and a billing-route
  assertion (`SUBSCRIPTION`) — nothing is guessed;
* records every denial/fallback observation explicitly (Zen, free-tier,
  Ollama, alternate provider, model substitution, metered fallback, paid
  overage, per-call billing — all explicitly not used);
* writes a strict `quixbugs-route-evidence-v1` JSON artifact (schema
  `schema_version: quixbugs-route-evidence-v1`) accepted by the existing
  live-runner validator, with create-once semantics, into the ignored
  `operator/` storage;
* contains no credentials, auth tokens, cookies, or raw private account data
  (a non-authoritative `<target>.capture-record.json` companion records the
  launcher/catalog observation, the operator assertions, and the
  zero-model-contact proof).

## Operator bundle materialization (`operator-bundle`)

Consumes the accepted route-evidence file and creates the real
`quixbugs-paired-pilot-authorization-v1` artifact (`authorization.json`) and
the real `quixbugs-opencode-go-execution-adapter-v1` configuration
(`adapter-config.json`), both bound to:

* the **actual clean Git HEAD observed (read-only) when the operator runs the
  command after this task has been accepted and merged** — never to a
  caller-supplied commit and never to the task baseline. The observed HEAD
  must be a valid existing commit, must descend from the accepted project
  baseline `28ec7754336fc53f21ebbae8a851b33e26714932` and from the minimum
  task lineage baseline `618c33ff186493892665ca1233c3edd8b2eec13f` (retained
  only as a lineage prerequisite), and must have a clean tracked working
  tree, a clean real index, and no non-ignored untracked files. HEAD and
  repository cleanliness are re-checked immediately before the artifacts are
  created; any drift between observation and materialization fails closed and
  creates neither active artifact;
* the frozen manifest hash
  `bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171`;
* the exact six frozen case IDs in order;
* protocol `1.3`;
* the exact observed OpenCode version, runtime model ID, variant, and catalog
  fingerprint (all from the route evidence);
* the account status and subscription billing route;
* one operator authorization ID and one fresh attempt identity + output root
  (occupied targets are rejected);
* an explicit bounded validity period;
* the operator-resolved Python executable, repository wrapper path, working
  directory, and operator boundary root.

The same independently observed HEAD is used consistently in the
authorization's `accepted_campaign_commit`, the adapter configuration's
`execution_commit`, the route-preflight execution binding, the runtime
identity binding, and the returned operator-bundle record.

Template values, route drift, unknown fields, malformed paths, and
contradictory subscription/fallback assertions are rejected; the artifacts are
written create-once into the ignored `operator/` storage and are never
committed. The materialized authorization and configuration pass the strict
authorization validator and the strict adapter validator with the full
authorization/route binding.

## Operator preflight handoff (PowerShell)

The generated artifacts work with the existing zero-provider-process command
`route-preflight-only`:

```powershell
# 1. Route capture (local inspection only; zero model/provider contact)
python scripts/quixbugs_opencode_go_adapter.py route-capture `
  --manifest research/quixbugs/PAIRED_PILOT_V3.json `
  --runtime-model-id opencode-go/deepseek-v4-flash `
  --variant max `
  --account-status ACTIVE `
  --subscription-entitlement-confirmed `
  --entitlement-evidence-reference operator/account-observation-20260803-001 `
  --billing-route-assertion SUBSCRIPTION `
  --output operator/route-evidence/quixbugs-route-evidence-v1-20260803-001.json

# 2. Operator bundle materialization (authorization + adapter configuration)
#    Binds both artifacts to the clean current Git HEAD present after Git
#    closeout (observed read-only at bundle time; the task baseline is only a
#    lineage prerequisite).
python scripts/quixbugs_opencode_go_adapter.py operator-bundle `
  --manifest research/quixbugs/PAIRED_PILOT_V3.json `
  --route-evidence-json operator/route-evidence/quixbugs-route-evidence-v1-20260803-001.json `
  --operator-authorization-id op-auth-20260803-001 `
  --attempt-identity quixbugs-paired-pilot-v3-attempt-<64-hex> `
  --output operator/attempts/quixbugs-paired-pilot-v3-attempt-<64-hex> `
  --valid-until 2026-08-10T00:00:00Z `
  --entitlement-evidence-reference operator/account-observation-20260803-001 `
  --python-executable <absolute path to python.exe> `
  --working-directory <absolute working directory> `
  --operator-boundary-root <absolute operator boundary> `
  --bundle-root operator/bundles/quixbugs-paired-pilot-v3-attempt-<64-hex>

# 3. Adapter validation (authorization + route binding)
python scripts/quixbugs_opencode_go_adapter.py adapter-validate `
  --manifest research/quixbugs/PAIRED_PILOT_V3.json `
  --adapter-config operator/bundles/quixbugs-paired-pilot-v3-attempt-<64-hex>/adapter-config.json `
  --authorization operator/bundles/quixbugs-paired-pilot-v3-attempt-<64-hex>/authorization.json `
  --route-evidence-json operator/route-evidence/quixbugs-route-evidence-v1-20260803-001.json

# 4. Route-preflight-only execution (zero provider processes)
python scripts/quixbugs_opencode_go_adapter.py route-preflight-only `
  --manifest research/quixbugs/PAIRED_PILOT_V3.json `
  --authorization operator/bundles/quixbugs-paired-pilot-v3-attempt-<64-hex>/authorization.json `
  --route-evidence-json operator/route-evidence/quixbugs-route-evidence-v1-20260803-001.json `
  --adapter-config operator/bundles/quixbugs-paired-pilot-v3-attempt-<64-hex>/adapter-config.json `
  --output operator/attempts/quixbugs-paired-pilot-v3-attempt-<64-hex>
```

The implementation agent must not execute these real commands; real operator
preflight remains pending FirstMate review and Onur's manual execution.
`operator-bundle` binds the authorization and adapter configuration to the
clean current HEAD present after Git closeout, so the manual sequence runs
after the candidate is accepted and merged.

After preflight passes, materialize `quixbugs-environment.json` from
`scripts.quixbugs_live_wire_environment.describe_environment()` in ignored
operator storage. A separately authorized live invocation uses the same
explicit v3 manifest and artifacts:

```powershell
python scripts/quixbugs_opencode_go_adapter.py live-wire `
  --manifest research/quixbugs/PAIRED_PILOT_V3.json `
  --authorization operator/bundles/quixbugs-paired-pilot-v3-attempt-<64-hex>/authorization.json `
  --route-evidence-json operator/route-evidence/quixbugs-route-evidence-v1-20260803-001.json `
  --adapter-config operator/bundles/quixbugs-paired-pilot-v3-attempt-<64-hex>/adapter-config.json `
  --output operator/attempts/quixbugs-paired-pilot-v3-attempt-<64-hex> `
  --quixbugs-environment-json operator/quixbugs-environment.json `
  --facts-provider scripts.quixbugs_live_wire_environment:provide `
  --confirm-opencode-go-adapter
```

The nominal descriptive denominator is six cases (three per policy).
Budget-terminal cases remain completed cases and are never dropped from that
denominator. Authority-invalidated cases are excluded from evaluation while
their resource use remains accounted; blocked, aborted, and unstarted cases
remain explicit and prevent interpreting the attempt as a complete paired
comparison.

## Cost propagation

The case execution cost is the aggregate of the finite monetary costs
explicitly reported by each provider response (the wrapper's
`provider_telemetry.cost`). Absent cost metadata is never replaced with a
fabricated value (zero is the frozen schema's absence representation and is
never claimed as a reported zero); an explicitly reported zero stays zero;
subscription access alone never implies zero monetary cost; token usage comes
from the actual responses; and the preflight route-observation cost is never
used as the case execution cost. The frozen v2 case validator's
provider-reported-cost check was relaxed accordingly (directly affected
compatibility fix): the case execution cost must be a finite non-negative
number, and the per-call reported costs are recorded in bounded evidence.

## Runtime identity and drift protection

The adapter never hardcodes the historical OpenCode Zen model identifier
`opencode/deepseek-v4-flash-free` as an execution identity; that identifier is
explicitly rejected, and every Go runtime identity must use the
`opencode-go/` provider prefix (any other provider is rejected before model
execution). The manifest may identify the model family, but the exact
runtime model/catalog identity comes from validated authorization and route
evidence. Configuration, authorization, route observation, and transport
invocation must agree exactly; the binding is revalidated before every
provider process attempt, and independently observed identity values (model,
billing route, substitution markers) reported by the provider are recorded
rather than only copying expected values. Alias rewriting, catalog identity
drift, OpenCode version drift, variant drift, route-class/billing-route drift,
and any observed Zen/free-tier/Ollama/alternate-provider/fallback state are
rejected (typed `RouteDriftError` categories map to the accepted
`TRANSPORT_EVIDENCE_LOSS` infrastructure stop contract). Tests use synthetic
route observations; no real current catalog ID is invented in production
artifacts.

## Transport factory

`OpenCodeGoTransportFactory` requires already validated authorization,
execution commit, route observation, adapter configuration, and runtime
identity binding; construction creates no process. `prepare(case)` returns one
fresh transport per frozen case only after the output/attempt ownership gates
(the exclusive `.attempt-owner` record and the `STARTED` ledger entry) are
verified on disk. Every `request()`:

* revalidates the binding and the ownership gates before the provider process
  attempt;
* spawns the structured wrapper argv with `shell=False`, an explicit working
  directory, and a bounded environment allowlist (no inherited environment,
  no credential-shaped names; explicit allowlist-bounded overrides only);
* captures bounded stdout/stderr and bounded diagnostics;
* applies a process-group-aware timeout and tree cleanup;
* performs zero automatic retries (retry accounting belongs to the accepted
  live harness), zero model/provider fallback, zero catalog queries, and no
  reliance on a globally selected OpenCode model or prior interactive session
  state;
* records the finite monetary cost explicitly reported by each provider
  response (`reported_costs`), propagates provider-reported token and
  monetary-cost metadata truthfully (subscription access never forces cost to
  zero; absent cost data stays absent; non-finite metadata is rejected), and
  aggregates the actual per-call reported cost into the case outcome;
* records independently observed identity values in bounded, credential-
  redacted evidence;
* converts a wrapper `directive_error` rejection (zero valid or ambiguous
  protocol directives) into the accepted bounded directive rejection carrying
  the wrapper's compact machine-generated correction message; it is never a
  transport/process failure, never contains the previous model response, and
  is carried to the model by the existing bounded directive-feedback cycle.

The protocol wrapper performs schema-aware directive extraction: every JSON
object candidate in the model text is validated through the strict
protocol-1.3 directive parser against the directive schema, action contracts,
and controller context embedded in the request; the result is accepted only
when exactly one candidate is a fully valid directive (copied request/config
objects are ignored only because they fail directive validation, never
through heuristic key stripping), zero valid candidates are rejected, and
multiple valid candidates are rejected as ambiguous. Wrong envelopes
(`{"action": ...}`, `params`/`payload`, protocol/version wrappers), unknown
fields, and malformed arguments are never silently normalized; correction
flows through the bounded directive-feedback cycle.

Separation of accounting: one `request()` is one provider process attempt; one
`LiveModelAdapter.next_directive()` cycle is one logical model call; retries
are the accepted adapter retry loop; an accepted directive is one validated
controller directive; and one case attempt is one frozen case. The case runner
reconciles these counters with the campaign record, and the accepted
`ProviderCallCounter` proves zero provider activity on every blocked path.

## Case-runner binding

`OpenCodeGoCaseRunner` connects the six-case live runner to the accepted
`run_live_quixbugs_case` path with one fresh transport/process/session
boundary per frozen case, no shared model conversation across cases, and the
frozen case order owned by the live runner. Static-baseline cases cannot use
PDB (the accepted policy hard-lock plus frozen budget enforcement); PDB-on-
uncertainty receives the **exact task-local `RuntimeProbe` built from the
frozen inventory entry's reviewed `runtime_probe` fields** for the selected
task and uses only the accepted controller gate and budgets, with the
runtime model identity bound explicitly through `pdb_identity_binding` (never
the historical Zen identity).

Probe binding rules:

* the exact inventory entry is resolved per frozen case; a missing or
  duplicated entry is rejected before any provider interaction;
* the probe is built only from the entry's frozen `runtime_probe` fields
  (`module_path`, `focus_function`, `call_expression`, `breakpoint_anchor`,
  `inspect_names`) — never from corrected source, tests, model output, or
  runtime guesses;
* missing, malformed, mismatched (against `implementation_path` and the
  frozen task manifest's buggy path, target symbols, corrected/test/support
  material), and duplicate probe metadata is rejected before any provider
  interaction (validated at case-runner construction for all six frozen
  cases and re-validated per case);
* the probe is passed to the live executor only for `pdb-on-uncertainty`;
  static-baseline cases receive no probe;
* the live case path independently re-validates the probe against the
  selected task ID (the historical default gcd probe keeps its gcd lock),
  the buggy module path, corrected-source exclusion, the reviewed target
  symbol, source containment, and a resolvable breakpoint anchor, and
  prepares it with `prepare_quixbugs_pdb_probe`.

The facts-provider contract is **task-bound**: the case runner requests
facts separately for every frozen case with the exact task manifest path
(`provide(manifest_path: str) -> QuixBugsPreflightFacts`), requires an exact
`QuixBugsPreflightFacts` result whose `DependencyPreparation` is bound to the
selected task manifest (task ID, manifest fingerprint, authority revision,
algorithm, pinned recipe), and rejects zero-argument generic facts providers,
wrong-task facts, and malformed results before any provider interaction.
Explicit operator selection remains `--facts-provider module:callable`;
`scripts/quixbugs_live_wire_environment.py` provides the task-bound provider
and `describe_environment()` for the `quixbugs-environment.json` artifact.

Model-visible inputs are exactly the accepted path's public inputs; corrected
source, gold patch, evaluator oracle, private qualification evidence, and
private authorization/account evidence are never exposed. The case runner
maps every produced `LiveCaseResult` into the frozen runner outcome contract
(terminal status/reason, transport and terminal transport evidence, PDB
accounting from the controller gate decisions and the event trajectory,
tokens/cost, verifier and patch accounting, cleanup and source restoration
flags, request/source hashes) and never bypasses the live runner's ledger,
terminal commitment, authority checks, stop rules, or result validator.
Route drift, transport failure, malformed-response exhaustion, budget
exhaustion, containment failure, verifier failure, cleanup failure, and
public/private evidence violations map to the existing typed stop/result
contracts.

## CLI lifecycle

```
python scripts/quixbugs_opencode_go_adapter.py adapter-template --output <path>
python scripts/quixbugs_opencode_go_adapter.py adapter-validate --manifest research/quixbugs/PAIRED_PILOT_V3.json --adapter-config <path> [--authorization <path> --route-evidence-json <path>]
python scripts/quixbugs_opencode_go_adapter.py route-preflight-only --manifest research/quixbugs/PAIRED_PILOT_V3.json --authorization <path> --route-evidence-json <path> --adapter-config <path> --output <root>
python scripts/quixbugs_opencode_go_adapter.py route-capture --manifest research/quixbugs/PAIRED_PILOT_V3.json --runtime-model-id <id> --variant <v> --account-status <status> --subscription-entitlement-confirmed --entitlement-evidence-reference <ref> --billing-route-assertion SUBSCRIPTION --output <operator/route-evidence target>
python scripts/quixbugs_opencode_go_adapter.py operator-bundle --manifest research/quixbugs/PAIRED_PILOT_V3.json --route-evidence-json <path> --operator-authorization-id <id> --attempt-identity <id> --output <root> --valid-until <ISO> --entitlement-evidence-reference <ref> --python-executable <path> --working-directory <path> --operator-boundary-root <path> [--bundle-root <path>]
python scripts/quixbugs_opencode_go_adapter.py selftest --output <root> [--scenario <name>]
python scripts/quixbugs_opencode_go_adapter.py live-wire --manifest research/quixbugs/PAIRED_PILOT_V3.json --authorization <path> --route-evidence-json <path> --adapter-config <path> --output <root> --quixbugs-environment-json <path> --facts-provider scripts.quixbugs_live_wire_environment:provide --confirm-opencode-go-adapter
```

The operator path requires, for live wiring, all of: explicit `live-wire`
mode, an external authorization-artifact path, an explicit route-evidence
path, an explicit adapter-configuration path, an explicit attempt/output
root, explicit confirmation that the operator intends to configure the
OpenCode Go adapter, a clean authorization-bound execution commit
(verified against the actual repository), an explicit
`quixbugs-environment.json` artifact (repository root and sources parent,
materialized from `scripts/quixbugs_live_wire_environment.py
describe_environment()`), and an explicit task-bound facts provider
(`--facts-provider module:callable` with the contract
`provide(manifest_path: str) -> QuixBugsPreflightFacts`; the module's
`provide` function is the operator-provided task-bound provider).
Zero-argument generic facts providers are rejected. The CLI provides an
adapter-config validation mode, a route/preflight-only mode with zero
provider process creation, a synthetic adapter self-test mode that cannot
contact a real executable, and a live wiring mode that remains unusable
without an actively validated configuration and an explicitly constructed
transport factory. It never defaults to OpenCode Go, any model, any
executable, any environment, any account, or any provider transport, and
has no hidden "best available model" or fallback route.

## Synthetic validation

`opencode_go_synthetic_executable.py` is a deterministic, test-only,
network-incapable fake OpenCode CLI that runs *behind the real protocol
wrapper* (via a local `opencode.cmd` shim on the bounded environment PATH).
The real wrapper still performs protocol conversion, isolation, directive
extraction, usage parsing, redaction, and evidence handling, and the request
reaches the wrapper through stdin. The synthetic CLI recovers the request
from the inline message (never from a `--file`), mirroring the real
model-facing contract. Scenarios cover: valid protocol response; malformed
response followed by valid recovery (driven by the request's
`directive_feedback`); malformed exhaustion; state-legal directives for each
frozen controller state (action in `Reproduce`, add_hypothesis in
`Understand`, revise_hypothesis in `RuntimeEvidence`); a response that copies
the entire embedded request JSON and appends one valid directive; DSML
tool-call text that tries to read the request file; process startup failure;
timeout; oversized stdout; non-zero exit; protocol identity mismatch; runtime
model drift; Zen/free-tier/model-substitution route drift; missing usage;
finite provider-reported token/cost metadata; explicitly reported zero cost;
absent cost; non-finite metadata; credential-like output requiring
sanitization; and child-process cleanup. The self-test mode and the test
suites prove zero real OpenCode/provider/catalog/account calls, no
network-enabled command, no Zen/free-tier route, no fallback, exact
process-attempt and logical-call accounting, a fresh process/session boundary
per case, and correct cleanup.

## Secrets boundary

The adapter does not expose or persist API keys, tokens, cookies, account
identifiers beyond approved sanitized evidence, authorization credentials,
the full inherited environment, private operator configuration, credential-
bearing argv, or raw provider diagnostics containing secrets. Evidence writes
use the accepted `redact_for_recording` convention with bounded sizes and
truncation facts; environment allowlists may not contain credential-shaped
names; argv may not contain credential-shaped content; and the adapter never
attempts to discover credentials or log whether a specific secret exists.

## Explicit non-goals

The adapter does not: contact any live provider/catalog/account/entitlement
endpoint; execute the real six-case campaign; establish operator
authorization; perform a real route preflight; run an empirical evaluation;
measure model performance or PDB effectiveness; or advance RAG, SFT, or DPO.

## What remains required before a real campaign

1. A real operator authorization artifact (created by the operator
   `operator-bundle` flow outside tracked source) whose
   `accepted_campaign_commit` is the actual clean Git HEAD observed at bundle
   time (after this task is accepted and merged; the task baseline
   `618c33ff186493892665ca1233c3edd8b2eec13f` is retained only as a lineage
   prerequisite) and whose identity fields match the validated route
   evidence.
2. Exact runtime route evidence passing the pre-provider gate (real
   `route-capture` observation, not synthetic).
3. An actively validated adapter configuration (real operator-resolved
   executable inside the operator boundary, no placeholders; created by the
   same `operator-bundle` flow).
4. The operator-supplied QuixBugs execution environment for the case-runner
   binding: the `quixbugs-environment.json` artifact (repository root,
   per-case task manifests, sources parent) and the task-bound facts provider
   selected with `--facts-provider module:callable`
   (`provide(manifest_path: str) -> QuixBugsPreflightFacts`).
   `scripts/quixbugs_live_wire_environment.py` exposes `describe_environment()`
   (existing repository root and sources parent) and the `provide` facts
   provider, which reuses the accepted read-only WSL/Bubblewrap readiness and
   never installs/clones/resets/cleans/downloads.
5. The operator explicitly authorizing the real campaign.

The operator preparation flow (`route-capture` → `operator-bundle` →
`adapter-validate` → `route-preflight-only`) is implemented and packaged, but
no real OpenCode inspection command has been executed by an implementation
agent. Real operator preflight remains pending FirstMate review and Onur's
manual execution.

Until all five exist, `live-wire` remains blocked before any provider process.
