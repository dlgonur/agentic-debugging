# QuixBugs OpenCode Go execution adapter v1 (operator guide)

This document describes the fail-closed OpenCode Go execution adapter for the
frozen QuixBugs paired-pilot v2 live runner
(`research/quixbugs/PAIRED_PILOT_V2.json`, canonical manifest SHA-256
`bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171`, accepted
baseline `28ec7754336fc53f21ebbae8a851b33e26714932`, live protocol `1.3`).

The adapter implements and validates the execution wiring only. It never
contacts OpenCode Go, DeepSeek V4 Flash, OpenCode Zen, a model catalog, an
account or entitlement endpoint, or any paid endpoint, and it never executes
the real six-case campaign. All validation in this task used local synthetic
executables, deterministic transport doubles, temporary repositories, and fake
route observations.

Implementation:

* `scripts/quixbugs_opencode_go_adapter.py` — adapter configuration contract,
  runtime identity binding, transport factory, case-runner binding, CLI
  (`adapter-template`, `adapter-validate`, `route-preflight-only`, `selftest`,
  `live-wire`);
* `scripts/opencode_go_synthetic_executable.py` — deterministic test-only
  synthetic OpenCode-compatible executable (network-incapable by construction);
* `research/quixbugs/OPENCODE_GO_EXECUTION_ADAPTER_TEMPLATE.json` — the
  tracked non-executable configuration template (rejected as an active
  configuration);
* `agentic_debugger/evaluation/live_quixbugs.py` — bounded backward-compatible
  extension: the accepted QuixBugs live case accepts an explicit
  `pdb_identity_binding` (provider, model id, variant) and exposes the bounded
  PDB gate decisions and malformed-directive rejections in the case evidence
  for every policy (default behavior unchanged).

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

## Protocol wrapper route modes

`scripts/opencode_protocol_transport.py` supports two explicit route modes:

* `legacy` (default; historical OpenCode Zen behavior preserved unchanged):
  the exact model must be active with zero input/output/cache prices and the
  requested variant available;
* `opencode-go`: catalog prices are preserved as observed (never required to
  be zero), the model and variant must be exactly present and active, the
  launcher version must equal `--expected-opencode-version` exactly, and the
  wrapper requires the exact model identity
  (`--expected-runtime-model-id`), catalog fingerprint, account status, and
  billing route already validated by the outer authorization/preflight
  contract — recording them in evidence without hidden fallback, model
  selection, catalog/account re-queries, or Zen/free-tier inference.

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
explicitly rejected. The manifest may identify the model family, but the exact
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
  redacted evidence.

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
uncertainty uses only the accepted controller gate and budgets, with the
runtime model identity bound explicitly through `pdb_identity_binding` (never
the historical Zen identity). Model-visible inputs are exactly the accepted
path's public inputs; corrected source, gold patch, evaluator oracle, private
qualification evidence, and private authorization/account evidence are never
exposed. The case runner maps every produced `LiveCaseResult` into the frozen
runner outcome contract (terminal status/reason, transport and terminal
transport evidence, PDB accounting from the controller gate decisions and the
event trajectory, tokens/cost, verifier and patch accounting, cleanup and
source restoration flags, request/source hashes) and never bypasses the live
runner's ledger, terminal commitment, authority checks, stop rules, or result
validator. Route drift, transport failure, malformed-response exhaustion,
budget exhaustion, containment failure, verifier failure, cleanup failure, and
public/private evidence violations map to the existing typed stop/result
contracts.

## CLI lifecycle

```
python scripts/quixbugs_opencode_go_adapter.py adapter-template --output <path>
python scripts/quixbugs_opencode_go_adapter.py adapter-validate --adapter-config <path> [--authorization <path> --route-evidence-json <path>]
python scripts/quixbugs_opencode_go_adapter.py route-preflight-only --authorization <path> --route-evidence-json <path> --adapter-config <path> --output <root>
python scripts/quixbugs_opencode_go_adapter.py selftest --output <root> [--scenario <name>]
python scripts/quixbugs_opencode_go_adapter.py live-wire --authorization <path> --route-evidence-json <path> --adapter-config <path> --output <root> --quixbugs-environment-json <path> --facts-provider <module:callable> --confirm-opencode-go-adapter
```

The operator path requires, for live wiring, all of: explicit `live-wire`
mode, an external authorization-artifact path, an explicit route-evidence
path, an explicit adapter-configuration path, an explicit attempt/output
root, explicit confirmation that the operator intends to configure the
OpenCode Go adapter, and a clean authorization-bound execution commit
(verified against the actual repository). The CLI provides an adapter-config
validation mode, a route/preflight-only mode with zero provider process
creation, a synthetic adapter self-test mode that cannot contact a real
executable, and a live wiring mode that remains unusable without an actively
validated configuration and an explicitly constructed transport factory. It
never defaults to OpenCode Go, any model, any executable, any environment, any
account, or any provider transport, and has no hidden "best available model"
or fallback route.

## Synthetic validation

`opencode_go_synthetic_executable.py` is a deterministic, test-only,
network-incapable fake OpenCode CLI that runs *behind the real protocol
wrapper* (via a local `opencode.cmd` shim on the bounded environment PATH).
The real wrapper still performs protocol conversion, isolation, directive
extraction, usage parsing, redaction, and evidence handling, and the request
reaches the wrapper through stdin. Scenarios cover: valid protocol response;
malformed response followed by valid recovery (driven by the request's
`directive_feedback`); malformed exhaustion; process startup failure; timeout;
oversized stdout; non-zero exit; protocol identity mismatch; runtime model
drift; Zen/free-tier/model-substitution route drift; missing usage; finite
provider-reported token/cost metadata; explicitly reported zero cost; absent
cost; non-finite metadata; credential-like output requiring sanitization; and
child-process cleanup. The self-test mode and the test suites prove zero real
OpenCode/provider/catalog/account calls, no network-enabled command, no
Zen/free-tier route, no fallback, exact process-attempt and logical-call
accounting, a fresh process/session boundary per case, and correct cleanup.

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

1. A real operator authorization artifact (created outside tracked source)
   whose `accepted_campaign_commit` binds this adapter's accepted commit and
   whose identity fields match validated route evidence.
2. Exact runtime route evidence passing the pre-provider gate (real
   observation, not synthetic).
3. An actively validated adapter configuration (real operator-resolved
   executable inside the operator boundary, no placeholders).
4. The operator-supplied QuixBugs execution environment (repository root,
   per-case task manifests, sources parent, verified execution context /
   facts provider) for the case-runner binding.
5. The operator explicitly authorizing the real campaign.

Until all five exist, `live-wire` remains blocked before any provider process.
