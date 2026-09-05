# Agentic Debugger V2 — Control/Execution Plane Separation Architecture Plan

**Document type:** Architecture analysis and migration plan (decision record)
**Status:** Plan — owner/FirstMate reviews 02, 03, and 04 applied (see lineage). Revision 04 is the implementation-readiness reconciliation before V2-01. **Implementation status: V2-01 execution-environment authority + control/provider secret isolation is implemented (see `agentic_debugger/application/execution_environment.py`, `BRIDGE_COMPATIBILITY_IDENTITY = legacy-project-ambient/v1`); V2-02 session/runtime contracts are implemented (`application/session_runtime.py`: `SessionLaunch`/`AgentDefinition`/`EffectiveSessionCapabilities`/`ProjectRuntimeEnvironmentSpec`; `application/executor.py`: `ProductExecutor`; declarative `ExecutionEnvironment.for_local_project`; the bridge is retired from the normal product path — see §15); V2-03 ModelGateway + ModelBinding + truthful status semantics + vocabulary repair is implemented (`application/model_gateway.py`, `application/provider_connections.py`, `ui/screens.py` — see §21); V2-04 and later stages are not implemented.**
**Lineage:** `01` `3481b58` defined V2 boundaries (Alternative B accepted in direction). `02` `3d414c6` tightened the execution and trust boundaries (security-first ordering, role-scoped environments, deferred verifier isolation, credential binding/materialization, truthful status semantics). `03` `ff81f44` finalized the authority rules (secret trust classes, positive/declarative environment target, capability intersection, `ModelBinding` ownership, credential sequencing, scientific fence, history-derived runtime metadata, verifier re-run deferral). `04` (this revision) reconciles two repository facts the prior revisions missed: the repository **already contains a typed verified execution authority** (`runtime/execution.py`) that V2 must not replace, and the positive `ProjectRuntimeEnvironment` target **has no current product ingress**, so V2-01 must use an explicit transitional compatibility bridge with documented residual risk, retired by a V2-02 ingress.
**Baseline:** `4606933` (fix(providers): harden provider runtime and Windows harness), clean tree
**Scope:** Determine whether the application runtime should adopt an explicit CONTROL / EXECUTION plane separation, and define the smallest coherent target architecture and incremental migration path
**Companion decision record:** `docs/adr/0001-control-execution-plane-separation.md` (Accepted — records an accepted target/migration decision, not completed implementation)

---

## 1. Executive recommendation

**Yes — a V2 architecture change is warranted: Alternative B — explicit logical planes and first-class authority contracts in the current principal process topology. No general control-plane/executor process split (Alternative C, rejected).**

The repository's recent boundary incidents (credential authority divergence, TLS/proxy environment loss across the worker/adapter hop, OpenCode auth-store visibility, Windows venv PID indirection, UI treating configuration presence as readiness) are not symptoms of a missing process. The pipeline already has three process tiers and per-tool child processes; each incident was repaired *inside the existing topology* by adding an explicit contract. That history is direct evidence that the architecture's weak point is **undeclared, distributed execution-environment state**, not process count.

The single most important V2 primitive is therefore **`ExecutionEnvironment`** — the **product/local-session** declarative authority for process/runtime policy (interpreter identity, workspace policy, role-scoped environment *rules* classified by trust provenance, per-capability network/trust policy, limits), from which **role-scoped least-authority child environments** are derived per child role (model adapter, project command, PDB worker, verifier command, legacy CLI). It is deliberately distinct from — and never a replacement for — the repository's existing **`VerifiedExecutionContext`** specialized authority for reviewed scientific/contained execution (§6.2a). Centralize the rules, not one shared environment blob. Today the same facts are re-derived at six product construction sites (§3.3), and each observed incident is traceable to one site disagreeing with another.

**The first implementation slice closes the active security defect with a minimal surface** (§11, V2-01): `runtime/command_runner.py::_build_env()` copies the full worker environment — including the forwarded private provider credential variable — into user reproduction/verification and verifier command children (§3.2). This is a currently evidenced execution-boundary exposure, not architecture debt. V2-01 introduces the product execution-environment policy authority and explicit child roles needed to make **Agentic Debugger control/model secrets** structurally unavailable to project execution children, preserving both the current provider credential forwarding (model-adapter behavior unchanged) and the existing `VerifiedExecutionContext` scientific contracts unchanged, with regression tests proving secret exclusion. Authority moves **out of `_build_env()`** — the runner stops independently reading `os.environ` — not merely a denylist inside it.

**Secret classes are distinct (§6.3).** Control/model secrets (provider API credentials, model-channel variables, provider CLI-auth/credential-store material) must never reach project repro/test commands, PDB/project code, or verifier commands — that is the V2-01 invariant. **Project runtime secrets** (test database credentials, local service tokens, application keys) are a *different authority*: an explicitly authorized part of the project's runtime environment, legitimately usable by project execution roles, but never flowing into the model adapter or prompts, never journaled, never fingerprinted, never rendered in diagnostics, never becoming provider credentials, and never implicitly copied merely because they exist in the parent process.

**The target environment contract is positive/declarative**, but **it has no product ingress today**: `SessionSpec`/`ExecutionSourceSpec` carry no project-environment declaration (`application/session.py:155-157`, `application/sources.py:113-125`), and the Local Project start UI exposes no project-environment authorization surface (`ui/app.py:730-742`). V2-01 therefore uses an explicit, temporary **LEGACY PROJECT AMBIENT** compatibility bridge for ordinary project variables — bounded, project-role-only, structurally excluding all Agentic Debugger control/model/provider channels by classified provenance, with documented residual risk (§6.3) — and **V2-02 introduces the explicit `ProjectRuntimeEnvironmentSpec` ingress** whose existence is the bridge's removal criterion. The bridge is a migration compromise, not the target.

**The independent verifier remains the sole correctness authority and keeps its existing substantive independence** (source-commit binding, clean-source export, separate disposable workspaces, independent patch evaluation and outcome taxonomy — all verified in source). `VerifierService` becomes a first-class *logical* boundary; **physical subprocess isolation is deferred** with explicit triggers (§5.6). Same-session verifier re-verification is likewise **deferred** (§13): a verifier failure remains an honest terminal outcome under the existing taxonomy.

Everything else — deterministic controller, typed tools, bounded PDB protocol, disposable workspaces, journal authority, exact provenance, verifier outcome taxonomy, frozen scientific identity, and the existing verified/contained execution contracts — stays structurally untouched. Complexity budget: five stages, each independently shippable, each earning its cost at the time it lands; V2-05 is explicitly optional and may resolve to NOT JUSTIFIED.

---

## 2. Current topology (reconstructed from source)

### 2.1 Process tree at session runtime

```text
UI process (python -m agentic_debugger.ui)                [Textual app]
  └── SessionWorkerProcess.spawn (worker_process.py:284)
      suspended → Windows Job Object (KILL_ON_JOB_CLOSE) → resume
      cwd = durable session dir; one start message on stdin
      └── session worker process (application/worker.py::main)
          owns: journal (single durable writer), cooperative token,
                disposable work dir, cleanup cycle, terminal result
          └── per-source orchestration (local_project_source.py)
              ├── DeterministicController (in-process)
              │     └── model request → CancellableJsonlCommandTransport
              │           └── adapter child process (per request:
              │               scripts/provider_direct_api_adapter.py or
              │               legacy CLI; minimal env + 1 credential var)
              ├── CommandRunner (user repro/verify; _build_env = dict(os.environ))
              │     └── user command children
              ├── PdbSession.start (pdb_session.py:417)
              │     └── PDB worker child (runtime/pdb_worker.py; -I -u -c runpy bootstrap)
              │           └── debug target (imported in-process by PDB worker)
              └── LocalProjectVerifier.evaluate (in-process call)
                    └── 4 disposable TaskWorkspaces, each running
                        repro/regression commands via CommandRunner
                        (plain product path — no VerifiedExecutionContext)
```

Process/lifecycle authorities (all verified in source):

| Responsibility | Owner | Evidence |
|---|---|---|
| Session spawn, Windows job containment, escalation ladder, crash classification, post-mortem cleanup | `SessionWorkerProcess` (UI process) | `worker_process.py:106-951` |
| Journal (single writer, fsync-per-append), cancel token, work dir, cleanup, terminal | session worker (`worker.py::run_worker`) | `worker.py:499-713` |
| Provider/credential/trust state | UI process **and** worker **and** adapter child (three resolutions per session) | §3.2 below |
| Controller state machine, budgets, tool dispatch | worker process (`agent/controller.py:845`) | `local_project_source.py:1072-1076` |
| Model transport | worker process → **per-request** adapter subprocess (`CancellableJsonlCommandTransport.request` spawns each time; `command_transport.py:268`) | `local_project_source.py:1017-1034` |
| PDB protocol, bounded observation | worker → PDB worker subprocess | `pdb_session.py:417`, `pdb_worker.py` |
| Patch parse/apply/revert, allowed-path policy | worker process (`runtime/patcher.py`, 1796 lines) | `local_project_source.py:609-746` |
| Independent verification | **worker process (in-process call)** | `local_project_source.py:1120-1156` |
| Verified scientific/contained execution | **existing specialized authority** (`runtime/execution.py`) — used by BugsInPy, QuixBugs, contained PDB, external evaluation | §2.5 below |
| Session truth | durable journal (`session.events.jsonl`), derived manifest | `journal.py`, `history.py` |

### 2.2 What crosses process boundaries today

- **UI → worker**: one `start` JSON line (`worker_protocol.py`): `SessionSpec` mapping, run id, journal path, work dir, scenario name + params (repo path, HEAD, workspace path, commands, provider/model ids, budget refs), `child_environment` (≤1 credential variable value — passed via `Popen env`, never argv/journal; `worker_process.py:136-151,309-320`).
- **Worker → UI**: `ready` / `event` (sequence number only; journal is authority) / `liveness` side-band / `terminal` / `fatal` / `error` — bounded JSON lines (`worker_protocol.py:1-34`).
- **Worker → adapter child (per model request)**: minimal env (`PATH`, `PYTHONIOENCODING`, `SystemRoot`, config/catalog/quarantine path vars, `HOME`/`USERPROFILE`/`LOCALAPPDATA`) + the allowlisted TLS/proxy subset (`SSL_CERT_FILE/DIR`, `CURL_CA_BUNDLE`, `*_PROXY`, `NO_PROXY`) + one credential variable (`command_transport.py:153-186`, `provider_connections.py:648-694`). The worker retains the session's transport credential environment at session construction (`local_project_source.py:1024-1025`) and injects it into each adapter request child.
- **Worker → PDB worker**: stdin/stdout JSON protocol with strict envelope validation (`pdb_protocol.py`); env = `build_worker_env(None)` (venv identity fixup only, otherwise full inherit; `pdb_session.py:390-405`, `python_launcher.py:137`).
- **Worker → user repro/verify commands (and verifier commands)**: `dict(os.environ)` + `PYTHONIOENCODING` — **full inheritance including the forwarded credential variable** (`command_runner.py:293-296`).
- **Cancellation**: UI writes `cancel` line → worker stdin reader sets token → token checked at controller safe boundaries, transport poll loop, CommandRunner poll, verifier checkpoints → grace period → supervisor escalates via job object/kill ladder (`worker.py:405-433`, `worker_process.py:489-545`).
- **Session truth**: append-only `session.events.jsonl` with per-record fsync, identity + contiguity validation, chain-of-custody fields; manifest + history entries derived read-only (`journal.py`, `history.py:1-33`).

### 2.3 Cleanup/crash containment (current state, verified)

Windows job object assigned to the suspended worker before resume (fail-closed), so every descendant inherits the job; supervisor death closes the job handle and kills the tree (`worker_process.py:22-26,399-433`). Worker owns normal-path cleanup (work dir + Local Project isolated worktree with verified removal); supervisor owns post-mortem cleanup after confirmed worker death (`worker_process.py:717-769`). PDB sessions own their disposable workspaces with release verification (`local_project_source.py:430-450`).

### 2.4 Session persistence and recovery — what exists, honestly

| Capability | Exists? | Evidence / limit |
|---|---|---|
| Durable audit journal (append-only, fsync, identity/contiguity validated) | Yes | `journal.py:67-194` |
| Audit replay (read-only cursor over validated events, same presentation reducer as live) | Yes | `replay.py:1-60`; UI reopen via `history.py:734` |
| UI history (manifests, discovery, reopening, classification complete/interrupted/malformed) | Yes | `history.py:449-802` |
| Deterministic reconstruction of a *finished* session's observable timeline | Yes | journal + replay + workstream projection |
| Checkpoint / resumable session (resume an *interrupted* session from persisted controller state) | **No** | nothing persists mid-run controller state; `history.reopen` is replay-only; a crashed worker classifies INTERRUPTED and the session ends (`worker_process.py:851-916`) |
| Retry (fresh chained session referencing `retry_of_session_id`) | Yes (new session, not resume) | `ui/app.py:905-925` |

**The current replay is audit/UI replay, not resume.** The owner decision on checkpoint/resume is recorded in §13 (deferred).

### 2.5 The existing verified execution authority (reconciled in revision 04)

The repository **already contains** a typed execution-environment authority for reviewed external/scientific/contained execution — `agentic_debugger/runtime/execution.py` — with active contracts (all verified in source):

- **`PreparedEnvironment`** — a reviewed, frozen execution environment: absolute `python_executable`, relative `pythonpath` entries, an explicit environment mapping in which **credential-like environment variables are prohibited** (keys containing `TOKEN`/`PASSWORD`/`SECRET`/`API_KEY`/`CREDENTIAL` fail construction; `execution.py:130-134`), plus a prepared `DependencyPreparation` record (`network_disabled=True` mandatory).
- **`ContainmentGuarantee`** — fail-closed containment declaration: network denied, credentials not visible, process tree isolated, explicit resource limits (`execution.py:150-174`); a context cannot be constructed that asserts network/credential access or missing isolation.
- **`VerifiedExecutionContext`** — `PreparedEnvironment + ContainmentGuarantee + ContainmentRunner` with runner-identity/boundary cross-validation; **approved-command binding** (`bind_argv` admits only pytest-as-python-module invocations under the reviewed interpreter), cwd binding, and `build_environment()` building the child environment from the reviewed mapping (`execution.py:177-235`).
- **`PdbLaunchPlan`** — a reviewed pytest-aware PDB launch plan with an explicit environment mapping; the contract does not launch PDB itself (`execution.py:31-65`).

**Active consumers (verified):** BugsInPy (`bugsinpy/wsl.py`, `wsl_preparation.py`, `adapter.py`), QuixBugs (`quixbugs/adapter.py`, `quixbugs/contained_pdb.py`, `evaluation/live_quixbugs.py`), the scientific verifier's optional context path (`evaluation/verifier.py:145-237` — `TestRunner(workspace, execution_context=...)`), `runtime/test_runner.py` (context-forwarding wrapper), `demo/tools.py`, operator/smoke scripts, and their tests. The **product** Local Project path does *not* use it: `evaluation/local_project_verifier.py` constructs plain `CommandRunner`s via its factory (no context; grep-verified), falling through to `_build_env()`.

This is precisely the dual-authority ambiguity V2 must resolve explicitly (§6.2a): the scientific path already proves the repository can run children under explicit prepared environments — the product path simply never adopted one.

---

## 3. Coupling and failure analysis — why the incidents happened

### 3.1 The pattern behind the incident classes

Each listed incident maps to the same architectural gap: **a fact about execution was owned by whichever process/module happened to need it first, and every consumer re-derived it independently.** When two derivation sites disagreed, the boundary failed — and because the derivations were scattered, each repair (correct, and now regression-tested) hardened one site without eliminating the class:

| Incident | Divergent owners of the same fact |
|---|---|
| Parent credential authority ≠ child adapter authority | UI resolves `provider_session_credential_environment` (UI process store/session state) vs adapter child re-resolves via `resolve_runtime_credential` (worker's env/config view) |
| TLS/proxy visible in parent, lost across worker/adapter boundary | parent `os.environ` vs `JsonlCommandTransport.subprocess_environment()` minimal env vs `CancellableJsonlCommandTransport` allowlist merge |
| OpenCode auth store visible in one process only | UI process knows `OPENCODE_CONFIG_DIR`; worker/adapter had to re-discover → fixed by explicit `--auth-file` + forward-as-value |
| Windows venv launcher PID mismatch | `Popen(sys.executable)` redirector PID vs real interpreter PID checked by handshakes → fixed by central `python_launcher` |
| Transport/protocol/auth/subprocess coupling | one resolver (`model_providers.py` + `provider_connections.py`, 4,500 lines combined) interleaving route choice, protocol validation, credential resolution, environment construction, provenance |
| Config presence treated as readiness | `provider_availability()`/`ProviderConnectionStatus.connected` (presence-only) vs UI `Connected` headline vs actual first-request executability — see §3.5 for the post-plan incident that proved this materially misleading |

### 3.2 Credential flow today (three independent resolutions per session)

1. UI: `provider_session_credential_environment(kind)` resolves saved→session→forwarded→env→CLI-auth (endpoint-binding-checked) and forwards **one** variable into the worker spawn env (`ui/app.py:476-494`, `worker_process.py:309-320`).
2. Worker (Local Project source): `provider_transport_environment(provider)` re-resolves the same ladder inside the worker and passes it to the transport constructor (`local_project_source.py:1024-1025`).
3. Adapter child: `_resolve_credential` calls `resolve_runtime_credential(provider)` **a third time** inside the per-request child, which reads the worker's env (the forwarded variable, or ambient env/CLI store) (`scripts/provider_direct_api_adapter.py:132-148`).

This triple resolution is honest (no value in argv/journal/diagnostics anywhere — verified) but it means *credential authority is nowhere*: each tier can disagree (e.g. worker forwards a saved credential while the child's re-resolution picks a stale ambient env var under a changed endpoint). The endpoint/quarantine binding rules in `provider_connections.py:2281-2365` exist precisely to keep the three resolutions convergent — rules that must be re-proven at every new construction site.

**Active execution-boundary exposure (found during this investigation; the reason V2-01 is security-first):** the forwarded credential variable lives in the worker's `os.environ`; `CommandRunner._build_env()` copies the full environment into every user reproduction/verification command and every verifier command child (`command_runner.py:293-296`, `local_project_source.py:163-203`, `evaluation/local_project_verifier.py:54`). Any user-supplied repro/verify script — or anything it invokes — can read the provider API key. The transport allowlist discipline carefully built for the *model* boundary does not yet exist on the *execution* boundary. Notably, the *scientific* path is already protected by construction: `VerifiedExecutionContext` children run only under reviewed `PreparedEnvironment` mappings with credential-like keys prohibited (§2.5) — the exposure is specific to the product path that falls through to implicit inheritance. (That same full-inheritance path is also how project variables — including the project's own runtime configuration — reach project commands today, which is why the positive/declarative contract of §6.3 needs a practical migration bridge.)

### 3.3 The six undeclared product-path execution-environment construction sites

| Site | Env passed to children | Interpreter identity | Network/trust |
|---|---|---|---|
| `SessionWorkerProcess._worker_argv` + `build_worker_env` | full inherit + 1 credential var | `resolve_worker_executable` (venv-aware) | inherited |
| `CancellableJsonlCommandTransport.subprocess_environment` | minimal + config paths + allowlist | `sys.executable` | explicit allowlist |
| `PdbSession._worker_env` | `build_worker_env(None)` (inherit or venv fixup) | `resolve_worker_executable` | inherited |
| `CommandRunner._build_env` (user commands + verifier) | **full inherit + PYTHONIOENCODING** | caller's argv (`python`/`python3` resolved by PATH) | inherited |
| `JsonlCommandTransport.subprocess_environment` (scientific/evaluation transport) | minimal (`PATH`, `PYTHONIOENCODING`, `SystemRoot`) | n/a | minimal |
| Legacy CLI routes (`opencode_provider_adapter` etc.) | adapter-owned | n/a | adapter-owned |

Every row is individually justified; the problem is that no single typed object declares the intended environment policy for a product session, so each new child type re-decides inheritance, allowlists, and interpreter selection — and the incident history shows they drift. (The *contained scientific* path is the counter-example that proves the pattern is fixable: its children already run under explicit reviewed environments — §2.5.)

### 3.4 Interleaving in the provider resolution core

`model_providers.py` (1,018 lines) + `provider_connections.py` (3,490 lines) currently interleave in one call path: route selection (direct vs legacy CLI), effective-protocol resolution + auth/profile capability validation, credential source resolution, live-config command construction, provenance payload construction, and environment forwarding. UI, worker sources, ladder, and local-project all consume this core, so none of them can avoid understanding `transport_profile`/`route`/`auth_mode` vocabulary — which is exactly why that vocabulary leaked into the UI (§3.5). A `ModelGateway` contract (V2-03) exists to draw one seam here, not to rewrite the core.

### 3.5 Provider status truth — and the post-plan incident

Verified in current source:

- **`ProviderConnectionStatus.connected` is presence-only**: "bearer/anthropic → connected iff a usable credential source exists under the endpoint-binding authority" (`provider_connections.py:3064-3081`). It never contacts the provider and makes no reachability claim.
- **During live owner validation immediately after the provider hardening work, the application displayed `Connected · saved` for CommandCode GOAT while the configured endpoint (`http://127.0.0.1:57788`) had no listener; the real model request then failed.** Credential/config presence was displayed as connection while the endpoint was unreachable. This is materially misleading and is direct product evidence for the status-vocabulary repair in §9: presence-only facts must never be labeled "Connected".
- The credential-source labels (`saved` / `session only` / `environment` / `CLI auth`, `ui/screens.py:1277-1282`) and the "Connected · source" summary (`ui/screens.py:1528`) expose *which mechanism* supplied the key, not what the user can actually do next.
- **"Transport Profile (generic unless a historical endpoint contract is intended)"** — add/edit-provider dialog label (`ui/screens.py:1928,2140`); durable-configuration vocabulary surfaced as a primary user decision. The owner decision (§13, resolved) keeps it as an advanced `Provider type` / `Endpoint contract` control.
- Per-model `available` + actionable reasons and cached-catalog staleness markers already exist and are truthful; §9 defines the complete target vocabulary.

---

## 4. Module responsibility map

Legend for TARGET V2 home: **CP** = control plane (orchestration, session, gateway; stays in UI process + worker shell), **EP** = execution plane (workspace/tools/debugger/tests/patch; worker process body + child processes), **GW** = provider gateway seam, **IND** = must remain independent of the CP execution path (logical VerifierService; physical isolation deferred), **SCI** = existing specialized scientific/contained execution authority — preserved, never superseded.

| Module | Current responsibility | Process / lifecycle | Current dependencies | Target V2 home | Action | Migration risk |
|---|---|---|---|---|---|---|
| `application/worker_process.py` | Supervisor: spawn, job containment, cancel, grace/escalate, classify, post-mortem cleanup | UI process, per session | `process_tree`, `python_launcher`, journal reader, worker_protocol | CP (session supervision) | Keep; gains the V2-01 role-scoped spawn environment | Low |
| `application/worker.py` | Worker shell: journal/cancel/work-dir/cleanup/terminal; `SessionCoordinator` | child process | emitter, journal, protocol, sources | CP shell: owns Session identity, delegates execution | Split: lifecycle stays; scenario dispatch moves behind Executor seam (V2-02) | Medium — the run_worker state machine is the repo's most-tested boundary |
| `application/worker_scenarios.py` | Bounded non-product boundary harness scenarios | worker process | — | CP test harness (unchanged role) | Keep | Low |
| `application/local_project_source.py` (1,333) | The product execution: builds task, tools, PDB, patch, transport, controller, verifier, artifacts — all in one function | worker process | controller, tools, patcher, PDB, transport, verifier, providers, level32, demo, scripts | Split across CP/EP (this file is the concrete proof of the brain/hands interleave) | **Split** (V2-01/V2-02): execution children consume role-scoped environments (V2-01); provider+transport+limits → gateway request and tools/PDB/patch/workspace → Executor (V2-02); verifier invocation → VerifierService logical seam | High — highest-traffic module; must stay runnable at every stage (done by seam insertion, not rewrite) |
| `application/local_source.py`, `configured_source.py`, `deterministic_source.py`, `ollama_cloud_source.py` | Other execution sources sharing the same interleave | worker process | same family | same split as above | Split with the same seams; deterministic/configured are the low-risk first movers | Medium |
| `application/model_providers.py` (1,018) | Registry: availability, model listing, route resolution, live-config + provenance construction | UI process (pickers) + worker (session resolve) | provider_connections, scripts adapters, evaluation.live | GW (behind `ModelGateway` façade, V2-03) | Keep module; add narrow gateway interface above it | Low — façade only |
| `application/provider_connections.py` (3,490) | Provider configs, quarantine, credential ladder, endpoint binding, protocol resolution, environment forwarding, truthful status | UI + worker + adapter (imported by all three) | provider_http, wincred, filesystem | GW + `CredentialVault` backend (V2-04 extracts the backend half only) | Split (later stage): backend-agnostic vault interface; resolution ladder and binding rules stay verbatim | Medium — credential tests are extensive; keep behavior byte-identical |
| `application/provider_http.py` | Bounded HTTPS (stdlib + curl fallback), URL canonicalization, error sanitization | parent (discovery) + adapter child | — | GW / adapter child | Keep | Low |
| `application/command_transport.py` | Cancellable JSONL transport; minimal env + allowlist + network parity merge | worker process | evaluation.live, process_tree | CP→GW (model channel) | Keep; env construction becomes an ExecutionEnvironment **role profile** consumer (V2-01) | Low |
| `evaluation/live.py` (JsonlCommandTransport, LiveModelAdapter) | Scientific command transport + live model adapter | worker process + evaluation harness | — | GW (product path) / evaluation (research path, unchanged) | Keep both roles; product gateway wraps them | Low |
| `scripts/provider_direct_api_adapter.py` + CLI adapters + `protocol_prompt_shaper.py` | Protocol-1.3 JSONL adapter children (direct API / legacy CLI), spawned per request | adapter child process | provider_connections (credential resolve) | GW child (unchanged contract) | Keep; V2-01 preserves its existing credential authority; V2-04 switches it to vault-issued material and removes ambient re-resolution | Medium (credential contract change, deferred to V2-04) |
| `agent/controller.py`, `controller_policy.py`, `state_machine.py`, `trajectory.py`, `observer.py`, `proof_gate.py` | Deterministic controller: state machine, budgets, directives, tool dispatch, steps | worker process | tool_registry, model adapter (duck-typed) | CP (the brain) | **Keep untouched** — it is already plane-clean: model via adapter interface, tools via registry, cancellation via injected check | None |
| `agent/tool_registry.py` + `skills/` | Typed tool contracts + source inspection skills | worker process | runtime modules | CP↔EP contract (the registry *is* the command vocabulary) | Keep; handlers become Executor-side (V2-02) | Low |
| **`runtime/execution.py`** | **Existing specialized verified execution authority** (SCI): `PreparedEnvironment` (credential-like keys prohibited, reviewed mapping), `ContainmentGuarantee` (network denied / credentials invisible / tree isolated / resource limits, fail-closed), `VerifiedExecutionContext` (approved pytest-command binding, cwd binding, explicit child env from the reviewed mapping), `PdbLaunchPlan` (reviewed PDB launch, explicit env) | in-process contract object; consumed by BugsInPy/WSL, QuixBugs/contained PDB, `evaluation/verifier.py` context path, `runtime/test_runner.py`, `demo/tools.py`, operator scripts | runtime.command_runner (context path), WSL/bubblewrap runners | **SCI — preserved verbatim.** NOT migrated, weakened, broadened, or replaced by the product `ExecutionEnvironment` (relationship and precedence: §6.2a). Any future unification is a separate explicit compatibility decision | Keep untouched; V2-01 adds NON-REGRESSION gates over its tests if shared runtime modules are touched | None (by explicit fence) |
| `runtime/command_runner.py` | Bounded command execution with **two modes**: `execution_context` supplied → `VerifiedExecutionContext` path (argv rebound, cwd verified, prepared explicit env, containment runner); otherwise → product mode with implicit `_build_env() = dict(os.environ)` | worker process (children); also used by scientific callers via context | runtime.execution (context), workspace | **EP** — product mode authority moves to the V2 environment authority (V2-01): the runner receives an explicit derived environment (or the classified LEGACY PROJECT AMBIENT snapshot) and **stops building child env from `os.environ` itself**; verified mode untouched | Modify product mode only (§6.2a precedence, §11 V2-01) | Medium (env behavior change must be tested against real repro scripts) |
| `runtime/test_runner.py` | Test execution wrapper forwarding an optional `VerifiedExecutionContext` into `CommandRunner` | worker process / evaluation | command_runner, execution | EP; context-forwarding role unchanged | Keep; gains nothing in V2-01 | Low |
| `runtime/workspace.py`, `patcher.py`, `exceptions.py` | Disposable workspaces, unified-diff patch policy/apply/revert, runtime exception taxonomy | worker process (children) | — | **EP** | Keep | Low |
| `runtime/pdb_session.py` (3,009) + `pdb_worker.py` (3,853) + `pdb_protocol.py` | Bounded PDB protocol, session lifecycle, worker with safe-eval/locals bounds; `_worker_env()` inherits via `build_worker_env(None)` | worker → PDB child | python_launcher, workspace | EP (already a model of the target pattern: typed protocol, PID identity handshake, bounded vocabulary, per-session disposability) | Keep; **product** PDB role environment declared in V2-01 (control/provider-secret-free, project-runtime-aware, python_launcher semantics and PID handshake preserved); **contained/scientific PDB** (WSL/bubblewrap overrides, `PdbLaunchPlan` semantics) unchanged | Low-Medium |
| `runtime/python_launcher.py` | Windows venv interpreter/PID identity authority | shared by all spawners | — | EP platform seam (subsumed into the ExecutionEnvironment interpreter policy) | Keep as the single interpreter authority; V2-01 declares it, does not duplicate it | Low |
| `application/journal.py`, `emitter.py`, `events.py`, `observability.py`, `source_snapshots.py` | Durable append-only evidence + typed events + observability producers | worker process (single writer) | — | CP (session evidence authority) | **Keep untouched**; §8.7 fences any additive product provenance fields out of frozen scientific identity | None |
| `application/history.py`, `replay.py`, `reporting.py`, `presentation.py`, `workstream.py` | Manifests, discovery, read-only replay, pure presentation projections | UI process | journal, events | CP (view) | Keep; presentation stays forbidden from evidence creation (already enforced); V2-03 derives runtime-success metadata from this history | None |
| `evaluation/local_project_verifier.py`, `evaluation/verifier.py`, `runner.py`, `outcome_taxonomy.py`, verifier observers | Independent verification: clean-baseline reproduction, patch re-apply, F2P/P2P, cleanup proof; **LocalProjectVerifier constructs plain product `CommandRunner`s via its factory (no verified context); EvaluationVerifier optionally accepts a `VerifiedExecutionContext`** | **worker process (in-process)** | command_runner, patcher, workspace; execution (verifier.py context path) | **IND** — `VerifierService` logical boundary (V2-02 seam); **physical subprocess isolation deferred** (§5.6, V2-05 optional) | Keep logic untouched; add interface + verifier role environment (control/provider-secret-free, project-runtime-aware, V2-01); process extraction only if triggers fire; the verifier's `command_runner_factory` is the V2-01 environment seam (§6.5) | Low-Medium now (deferral removes the process-move risk) |
| `application/local_project.py` (1,144) | Project validation, isolated git worktree lifecycle, task-spec contract, containment | UI process (prepare) + worker (verify) + supervisor (post-mortem) | git CLI | EP (workspace lifecycle) | Keep; already correctly cross-process cooperative | Low |
| `application/level32.py`, `ollama_cloud_source.py` (ladder) | Scientific capability-ladder operator (qualification-bound) | worker process | qualified roster, adapters | CP but **scientific boundary fenced** (§8) | Keep; ladder qualification never derives from provider availability (already true — `is_treatment_eligible`) | None (no change) |
| `quixbugs/`, `bugsinpy/` | Pinned dataset adapters, contained PDB (WSL/bubblewrap), license-gated WSL prep — verified-context consumers | operator/evaluation processes, not product runtime | runtime.execution, runtime | SCI (offline) / frozen research | Keep; **execution-context semantics untouched by V2-01**; out of product runtime scope | None |
| `ui/app.py`, `screens.py`, `widgets.py`, `models.py`, `session_config.py` | Textual UI: home, provider manager, session setup, workspace, history; **no project-environment authorization surface exists today** (`start_local_project_session` params, `ui/app.py:730-742`) | UI process | application layer | CP (presentation) | Keep; V2-02 adds the minimal `ProjectRuntimeEnvironmentSpec` ingress contract (not a UI redesign); V2-03 only *narrows* provider imports (gateway façade, truthful status facts) | Low-medium |
| `demo/` (catalog, tools, runner, policies, model…) | Offline deterministic tasks + tool context/registry builder shared with live sources; verified-context consumer in tool paths | worker process (deterministic + live sources import demo.tools) | runtime | EP; `demo.tools.build_registry` is shared tool-vocabulary authority | Keep (defer consolidation; shared import is a wart, not a risk) | None |

Deliberately omitted: `comparison/`, `rag/`, `preference/`, `events/` (research subsystems), `datasets/`, `experiments/`, frozen research paths — unaffected by plane separation.

---

## 5. Alternatives evaluated

### 5.1 Alternative A — keep process architecture, harden contracts

Evolve nothing structurally; continue the incident-response pattern (add explicit contract at each divergence site).

- **Correctness:** achievable; the six incidents were each fixed this way and hold under regression tests.
- **Prevents the observed classes?** Only until the next construction site appears. §3.3 shows six product environment sites and §3.2 shows an *active* credential exposure that contract-hardening would likely rediscover site-by-site rather than close class-wide.
- **Verifier independence:** stays as construction discipline (in-process verifier with substantive logical independence).
- **Latency / complexity / Windows:** optimal (nothing changes); debuggability unchanged.
- **Cost/risk:** lowest.
- **Verdict: insufficient.** It leaves the class-generator (undeclared environment policy, triple credential resolution) in place. Reasonable as a fallback if V2 stalls, but it is the strategy that *produced* the incident list.

### 5.2 Alternative B — explicit logical planes in the current principal process topology (recommended)

First-class contracts — `Session`, `AgentDefinition` (desired capabilities), `ExecutionEnvironment` (available capabilities; role-scoped child derivation; the **product** authority), `VerifiedExecutionContext` (the existing **specialized scientific/contained** authority, preserved), `EffectiveSessionCapabilities` (computed intersection), `ModelGateway` (truthful status facts; `ModelBinding` runtime provenance), `CredentialVault` (binding vs materialization, V2-04), `Executor` (interface), `VerifierService` (logical boundary) — **with no new long-lived processes and no committed process moves.** The session worker remains the single orchestration process; brain and hands stay in it but behind typed, testable seams that make the boundary enforceable. Selective future physical isolation (e.g. the verifier) stays *cheap to evaluate later* precisely because the interfaces exist — but nothing in V2 requires it.

- **Correctness:** every observed incident class is closed *at its generator*: one `ExecutionEnvironment` policy makes worker/adapter/PDB/user-command/verifier env derivations consumers of one authority with role-scoped least-authority profiles (kills the env-divergence class); one credential authority with explicit trust classes kills authority-divergence; one interpreter identity authority is already done (`python_launcher`) and gets subsumed; capability intersection kills dual tool-set truth; gateway-owned truthful status facts kill config-as-readiness (validated by the post-plan incident). The scientific path already demonstrates the pattern works — `VerifiedExecutionContext` children have run under explicit reviewed environments all along (§2.5).
- **The §3.2 exposure becomes structurally impossible** for new construction: `CommandRunner` product mode consumes a role-scoped derivation in which Agentic Debugger control/model secret channels are excluded by identity (provenance classification, with name/value detection as fail-safe), so the provider credential can never reach user code.
- **Isolation/recoverability:** unchanged process containment (job object, PID identity, cleanup verification all stay); typed seams make restart/recovery *possible to add later* without redesign.
- **Latency:** zero added IPC on any hot path (model/PDB/tools/verifier all unchanged in-process).
- **Process complexity:** none added in V2-01…V2-04; V2-05 is an optional, trigger-gated evaluation.
- **Windows behavior:** all new seams reuse existing Windows authorities (job object, python_launcher, taskkill ladder); no new Win32 surface.
- **Debugging complexity:** improves — one place to inspect "what environment policy did this session declare, which role profile did this child consume, and which capabilities were granted".
- **Credential exposure:** strictly reduced for control/model secrets (single authority; execution roles structurally exclude them; provider proxy values — which may themselves embed credentials (`provider_connections.py:642`) — no longer flow to arbitrary project code merely because the model adapter needs them), while project runtime secrets become an explicit, separately authorized authority instead of ambient accident. During the V2-01 transitional bridge, unclassified operator environment variables may still reach project code — exactly as today; this residual risk is documented, not hidden (§6.3, §10).
- **Reproducibility:** improves — the safe declarative execution contract is fingerprinted into product session provenance (§6.6, fenced out of frozen scientific identity per §8.7), so replay can assert the policy identity that produced the evidence.
- **Testability:** each seam testable in isolation with fakes; the existing suite keeps passing because behavior is preserved stage-by-stage.
- **Cost/risk:** moderate; the highest-risk stage is the V2-01 environment change (user-visible, security-motivated, tested with real projects).
- **Verdict: recommended.** It is the smallest change whose failure-prevention claim is about *classes*, not instances, and it follows the plan's own principle — complexity must earn its cost — including for the verifier: the logical seam earns its place immediately, the process boundary does not.

### 5.3 Alternative C — full control-plane / execution-plane process separation

A long-lived control runtime (session state machine, controller, gateway, journal) in one process; a disposable/restartable executor process owning filesystem/PDB/tests/patching, connected by typed IPC; verifier independent.

- **Prevents observed classes?** Also yes — but not better than B for *any* of them (the incidents were env/credential/identity divergence, which B closes at the declaration level).
- **Latency:** every tool call (repro runs, PDB interactions, patch applies) crosses IPC. PDB especially: the controller-side dispatch would add a full round-trip per `continue/step/locals` — on Windows pipes, against a 5–60 s request budget, meaningful.
- **Process complexity:** two long-lived processes per session + lifecycle state machine for the executor (restart, workspace re-attachment, generation counters) — a new class of bugs (workspace ownership across executor restart, journal/event ordering across two writers) that this repository has never needed.
- **Recoverability:** the theoretical win (executor dies → restart, session continues) requires real checkpoint/resume of controller state (§7), which does not exist and is explicitly deferred by owner decision (§13): today a worker death already classifies honestly (INTERRUPTED), cleans up via job object, preserves the journal, and the operator retries — the failure mode is handled, not open.
- **Windows behavior:** job-object containment must be restructured (which process owns the job?); PID identity handshake generalized; new named-pipe/stdin lifecycle machinery — all new Windows surface, high risk.
- **Scientific reproducibility:** neutral-to-negative; extra nondeterminism sources (IPC timing) in evidence paths.
- **Cost/risk:** the highest in every dimension, for benefits (crash-resume, parallel executors) nobody has requested and no incident requires.
- **Verdict: rejected.** No observed failure requires long-lived executor processes, and the costs are not earned. B's seams make a later *selective* move toward C possible if the checkpoint/resume trigger ever fires — that optionality is worth more than the split itself.

### 5.4 Alternative D (considered, rejected quickly) — replace worker with a vendor agent runtime

Explicitly out of scope by mandate and by repository contract: no vendor-managed agent runtime as primary architecture. The deterministic controller, PDB-first evidence, and local reproducibility are the product; nothing further to evaluate.

### 5.5 Why the verifier process move left the committed plan

Revision 01 of this plan committed the verifier to a subprocess as V2-03, arguing that verifier independence is a mandated invariant. That conclusion was stronger than repository evidence supports. The repository invariant is *epistemic*: **the independent verifier is the correctness authority — controller completion, patch application, provider success, and model confidence are not proof.** `LocalProjectVerifier` already gains substantive independence by construction (all verified in source, `evaluation/local_project_verifier.py:304-470`): binding to the source commit, rejecting dirty/mismatched canonical source, exporting clean source, evaluating the exact candidate patch independently of controller claims, four separate disposable workspaces, independently running baseline reproduction/regression, patch application, syntax, post-patch reproduction/regression, classification, and cleanup/source-integrity proof. An OS process boundary may improve crash/secret/lifecycle isolation, but it does not create epistemic independence — and V2-01's role-scoped environments already remove the one concrete secret-exposure argument. Physical isolation is therefore deferred behind explicit triggers (§5.6). This also makes the architecture consistent with its own principle: complexity must earn its cost.

### 5.6 Deferred-verifier-isolation triggers (evaluate before any V2-05 work)

Promote the verifier to a subprocess only if one of these is evidenced:

1. **Lifecycle:** verifier crash/hang materially threatens the worker lifecycle (today a verifier hang is bounded by plan timeouts and produces an honest verifier failure — if field evidence shows it instead wedging or crashing workers, isolation is earned).
2. **Environment:** verifier dependency/environment isolation cannot be guaranteed in-process (e.g. verification requires interpreter/library isolation from the worker's own imports).
3. **Security:** a concrete security boundary requires it (e.g. verification of untrusted code that must not share address space with control/credential-bearing state — note V2-01 already removes control/model secrets from the verifier role, so this trigger is about *untrusted code*, not secrets).
4. **Operations:** measurable evidence shows process isolation pays for its Windows lifecycle/serialization cost.

Until a trigger fires, the stage resolves to **NOT JUSTIFIED / DEFERRED** and no implementation work is created merely to complete the numbered list.

---

## 6. Target V2 topology and boundary contracts

### 6.1 Conceptual planes

```text
CONTROL / ORCHESTRATION PLANE ("brain")
  UI presentation (Textual)
  Session supervision (worker_process)
  Session identity + journal + observability (worker shell, SessionCoordinator)
  DeterministicController + policy + budgets
  AgentDefinition (desired capabilities) + ExecutionEnvironment (available
    capabilities; PRODUCT authority) → EffectiveSessionCapabilities
    (computed once; provenance)
  ModelGateway (provider selection, ModelBinding runtime provenance,
                truthful status facts)
  CredentialVault (CredentialBinding decisions + material issuance;
                  the ONLY Agentic Debugger control-secret reader)   [V2-04]
  History / replay / presentation projections

EXECUTION PLANE ("hands") — behind typed Executor interface, in worker body + children
  Disposable workspaces (TaskWorkspace, isolated git worktree)
  CommandRunner / TestRunner (bounded; role-scoped child environments
                              by trust class; verified-context mode
                              untouched for scientific callers)
  PdbSession → PDB worker child (unchanged protocol; product role env
                                  control/provider-secret-free,
                                  project-runtime-aware)
  PatchManager (unified diff, allowed-path policy)
  Process cleanup verification

SPECIALIZED SCIENTIFIC/CONTAINED EXECUTION (existing authority; preserved verbatim)
  VerifiedExecutionContext / PreparedEnvironment / ContainmentGuarantee /
  PdbLaunchPlan (runtime/execution.py) + WSL/bubblewrap runners
  BugsInPy · QuixBugs · contained PDB · evaluation-verifier context path
  NOT replaced, migrated, or reinterpreted by the product ExecutionEnvironment

INDEPENDENT VERIFIER (logical VerifierService; in-process in V2)
  LocalProjectVerifier / EvaluationVerifier behind a first-class interface
  Own disposable workspaces; own CommandRunner with the verifier role
  environment (control/provider-secret-free; receives the declared
  project-runtime inputs it needs for reproducibility)
  Sole correctness authority
  Physical subprocess isolation: deferred behind §5.6 triggers
```

The worker process remains one process in normal operation — but its internal structure now has a hard seam (the tool/registry boundary the controller already uses), a declared environment policy with role-scoped derivations, and a gateway-owned model channel, so "control" and "execution" are distinguishable, testable, and — only if a §5.6 trigger fires — cheap to separate physically.

### 6.2 The primitives — what becomes first-class and what must NOT

**`Session`** (evolve existing `SessionSpec`/`SessionCoordinator`): the durable identity + authority object.
- Contents: `session_id`, `task_id`, `AgentDefinition`, `ExecutionEnvironment`, `EffectiveSessionCapabilities`, `ModelBinding`, `ProjectRuntimeEnvironmentSpec` (from V2-02), budgets, `retry_of`, provenance.
- Authority ordering (explicit, matches current code): **durable journal is the event/evidence authority; `Session` is the lifecycle authority; UI state is a projection.** The manifest remains derived, never authoritative. The worker remains the only journal writer.

**`AgentDefinition` — what the agent is ALLOWED / REQUESTS to use** (product/runtime provenance identity, V2-02):
- controller/prompt policy (version + prompt profile);
- requested model identity/configuration: provider *logical* identity, model identity, prompt-profile/model policy;
- allowed tool capabilities (what the agent may use);
- budget defaults.
- It deliberately does **not** contain runtime-resolved transport state (§6.2 `ModelBinding` below) and never becomes the qualification authority for frozen scientific treatment identity (§8.2).

**`ExecutionEnvironment` — what the current machine/session CAN physically provide** (the keystone, V2-01; the **product/local-session** authority): one declarative authority, many derived environments. A frozen dataclass declaring *policy*, not an environment blob:
- policy/schema version;
- **available execution capabilities** (what this machine/session can provide — including whether PDB is available; workspace/process policy; resource-policy declarations);
- role-scoped child-environment policies (§6.3): which variables each child role receives, classified by **environment trust class** (provenance/capability-based — see below), by derivation rule (`inherit-platform`, `constant`, `model-channel-only`, `control-secret-name`, `project-variable`, `project-secret-binding`, `legacy-project-ambient` (transitional, §6.3) — never a resolved secret value);
- interpreter/runtime identity (base executable, venv marker policy — subsumes `python_launcher`, which remains the single implementation);
- workspace policy + generation identifiers (worker-owned vs verifier-owned roots);
- network/trust policy **per capability**: *provider* transport networking (proxy/TLS allowlist, model-adapter role only) and *project* network/trust capability (HTTPS_PROXY / NO_PROXY / custom CA trust explicitly authorized for the project) are separate authorities — the same variable name may appear in both with different provenance, and neither is inferred from the other;
- timeout/resource-policy declarations.
- **Must NOT contain:** resolved secret *values* of any class (control or project — only names/binding references), resolved provider transport internals (route/protocol/endpoint stay gateway-side in `ModelBinding`), UI state, journal contents.
- **Minimal-surface rule (V2-01):** only the fields the first slice actually consumes are introduced (§11); workspace/tool/resource policy fills in incrementally in V2-02. No speculative "complete future dataclass" scaffolding.

**Environment trust classes — the primary classification authority** (§6.3). Every environment variable is classified by provenance/capability, not by name-shape guessing:

| Trust class | Examples | May reach |
|---|---|---|
| **Platform/runtime** | `PATH`, `SystemRoot`, `TEMP`, `PYTHONIOENCODING` | all roles (essentials subset per role) |
| **Agentic Debugger internal control** | config/catalog/quarantine path vars, test isolation flags | control-plane children only |
| **Model/provider transport** | provider TLS/proxy allowlist, model-channel credential channel, provider CLI-auth material | model-adapter role only |
| **Project runtime** | variables the project declares as required (`DJANGO_SETTINGS_MODULE`, feature flags, paths) | project/PDB/verifier roles, explicitly declared |
| **Project runtime secret** | test DB credentials, local service tokens, application API keys, signed fixture credentials, private package/test-service access — explicitly authorized | project/PDB/verifier roles, explicit binding only; never the model channel, prompts, journal, fingerprints, or diagnostics |
| **Diagnostic-only** | redacted diagnostics context | never a child environment |

Known-name/value shape detection (the existing `contains_credential_shape` discipline) remains as a **secondary fail-safe** only: it can catch an obvious misclassification, but it can never be the architectural authority — a variable named `FOO` can hold a secret and `TOKENIZER_CACHE` may not.

**`EffectiveSessionCapabilities`** — computed once per session from the explicit intersection:

```text
AgentDefinition.allowed_capabilities
  ∩ ExecutionEnvironment.available_capabilities
  ∩ task/product policy
```

The result becomes session provenance. No consumer independently recomputes the intersection; there are no two independent "tool sets" without precedence semantics — `AgentDefinition` says what the agent may *request*, `ExecutionEnvironment` says what the machine can *provide*, the intersection (plus task policy) is what the session *gets*.

**`ModelGateway`** — the narrow interface the rest of the app sees (V2-03):
- `models()` / `status()` returning the truthful facts of §9 (no presence-only "Connected"), `resolve(session, agent_def) → ModelBinding`, `executability(model) → {runnable, blocker}`.
- Internally wraps today's `model_providers` + `provider_connections` + transports + adapters unchanged; the UI stops importing transport vocabulary (the durable `transport_profile` field remains and stays safety-critical).
- Does **not** remove provider transport identity from durable internal configuration (explicit requirement).

**`ModelBinding`** — the session's runtime-resolved model transport provenance, produced by `ModelGateway` (V2-03): direct API vs legacy route; effective protocol; endpoint contract/transport profile as resolved; resolved endpoint identity; credential binding reference; adapter route/provenance. These are *runtime facts* and belong to session provenance — **not** to `AgentDefinition` — so an agent definition cannot become stale merely because provider runtime configuration changes. Scientific identity remains separately authoritative (§8.2).

**`CredentialVault`** — provider-neutral control-secret authority with **two separated concepts** (V2-04):

*`CredentialBinding` — non-secret, session-stable authority:*
- provider identity; endpoint/profile binding; chosen credential source/backend identity; safe binding/epoch/fingerprint metadata.
- The binding decision may be fixed at session start for provenance and route consistency. It carries **no secret value** and may be journaled as provenance.

*Credential material / lease — secret-bearing ephemeral materialization:*
- obtained only by the minimal trusted control/model path; injected into the exact model-adapter request child;
- never present in `ExecutionEnvironment` (the environment carries only the binding *name*, and only in the model-channel role);
- never available to project commands, PDB, verifier, journal, argv, evidence, or UI text;
- never independently re-resolved from ambient state by the adapter (the adapter consumes the issued channel).
- **Honest lifecycle wording for the current topology:** the worker may retain the session-authorized secret in bounded private process state — this is what the current code already effectively does, since the transport spawns a fresh adapter child *per request* (`command_transport.py:268`) and re-injects the credential each time. The secret's lifetime is therefore the *session* (or the authorized lease window), not a single child. The architecture states this honestly rather than claiming single-child secret lifetime; what is per-request is the *injection*, not the *materialization*.

- **Sequencing honesty:** through V2-01…V2-03 the *existing* provider credential authority (`provider_connections.py:2417,2458` forwarding) remains the mechanism; the Vault interface, the binding/materialization separation, and removal of the adapter's ambient re-resolution arrive in **V2-04**. Migration sections state which authority exists at each stage.
- Backends: Windows Credential Manager (current ctypes/advapi32 implementation, first), session memory (current), environment (endpoint-bound, current), CLI auth store (forwarded-as-value, current); future macOS Keychain / Linux Secret Service as new backend *implementations only* — possible, not claimed today.
- Strict endpoint/profile binding and quarantine rules move into the vault verbatim (`provider_connections.py:2281-2365`).
- No cross-machine secret synchronization (explicit non-goal). **A future project-secret binding may reuse a vault backend if justified, but provider `CredentialBinding` and project execution-secret authorization remain distinct authorities** — project-secret storage/synchronization is *not* designed in this plan.

**`Executor`** — an **interface only** (V2-02): the typed execution-service contract the tool handlers implement against — `run_command`, `start_pdb`, `apply_patch`, `syntax_check`, `revert`, `run_tests` — each taking the session's `ExecutionEnvironment` and deriving the correct role profile. The existing runtime modules are its implementation. This is the CP/EP boundary made explicit so the controller never touches filesystem/process APIs except through it. (Promotion to a real process is Alternative C, rejected; a *verifier* process is separately deferred per §5.6.)

**`VerifierService`** — the independent correctness authority as a **first-class logical boundary** (V2-02): same verifier code and invariants, behind an interface the execution sources call without any ability to select, parameterize (beyond supplying the typed plan), or short-circuit it; its command children consume the **verifier role environment** — control/provider-secret-free, and receiving the explicitly authorized ProjectRuntimeEnvironment the project needs for reproducibility (V2-01). Clean-source export, four-workspace isolation, outcome taxonomy, and cleanup proof all remain unchanged. Physical subprocess extraction is deferred behind the §5.6 triggers; the seam must keep that extraction easy.

**`ProjectRuntimeEnvironmentSpec`** — the explicit product/session ingress for project environment authorization (V2-02; name illustrative): capable of declaring, **without embedding secret values into durable session evidence** — project variable names (and explicit non-secret values where appropriate); project network/trust requirements; project-secret binding *references*; the provenance of each declaration. It exists to retire the V2-01 LEGACY PROJECT AMBIENT bridge (§6.3). Minimal product/session contract only — no UI redesign and no cross-machine secret synchronization are designed in this plan.

### 6.2a Relationship to the existing `VerifiedExecutionContext` authority (terminology and precedence)

The repository already has a specialized execution authority (§2.5), and V2 must not create dual-authority ambiguity:

- **Terminology (used consistently throughout this plan and the ADR):** the **verified execution context** (`VerifiedExecutionContext` and its `PreparedEnvironment`/`ContainmentGuarantee`/`PdbLaunchPlan` contracts) is the *specialized reviewed scientific/contained execution authority*. The product **`ExecutionEnvironment`** is the *local/product session execution policy authority*. The two are related but different concepts and are never casually interchanged as "the execution context".
- **Disposition:** `VerifiedExecutionContext` **remains the authoritative specialized contract** for already-reviewed external/scientific/contained execution (BugsInPy, QuixBugs, contained PDB, external evaluation, frozen callers). Its semantics are **not migrated, weakened, broadened, or replaced** in any V2 stage: the reviewed `PreparedEnvironment`, its credential-like-variable prohibition, containment guarantees, approved pytest-command binding, network/credential denial assumptions, and frozen/scientific callers all stay exactly as they are.
- **Precedence (fail closed):** where both authorities could reach one call site, precedence is explicit and merging is forbidden. The verified path stays self-contained and authoritative (`CommandRunner`'s `execution_context` mode is untouched); ordinary product `CommandRunner` calls receive the V2 child-environment role/policy; **supplying incompatible product and verified-scientific environment authorities simultaneously must be rejected, never merged.**
- **Shared lower-level primitive:** a small shared environment-derivation helper *may* eventually be extracted, but **only when** the semantics are actually identical, frozen/scientific behavior is proven unchanged, and the existing contained-execution tests remain authoritative. **V2-01 must not refactor `VerifiedExecutionContext` merely for conceptual elegance.** Until such evidence exists, the two higher-level authorities stay separate, and this document is the record of why.
- **The key requirement:** no second product abstraction may accidentally supersede or reinterpret the existing scientific execution authority. The product `ExecutionEnvironment` governs the paths that *currently fall through to implicit parent-environment inheritance* — nothing else.

### 6.3 Role-scoped child-environment policy and secret trust classes

`ExecutionEnvironment` centralizes the RULES; each child role derives a least-authority environment. Conceptual roles (names illustrative, not prescribed):

| Role | Receives | Never receives | Rationale |
|---|---|---|---|
| **Model-adapter child** (per request) | minimal platform set; config/catalog/quarantine path vars; provider TLS/proxy allowlist (these may reach the provider HTTP path — proxy values may themselves embed credentials, `provider_connections.py:642`, so they are confined to this role); the authorized credential channel (existing forwarding through V2-03; vault-issued material from V2-04) | workspace paths; project runtime variables/secrets; anything from the project roles | The one role authorized to carry Agentic Debugger control/provider trust material |
| **Project repro/test command** (user commands) | **target contract**: platform essentials; explicitly authorized project variables; explicitly authorized project-secret bindings where the project requires them; optionally a project network/trust capability (HTTPS_PROXY / NO_PROXY / CA trust) separately authorized as a *project* capability. **V2-01 transitional**: platform essentials + the LEGACY PROJECT AMBIENT snapshot (below) | Agentic Debugger control/model/provider secrets (excluded by trust-class provenance, fail-safe name detection secondary); provider transport/network material; (after V2-02) implicit ambient inheritance | Untrusted-by-default project code; the §3.2 exposure closes here; project secrets are legitimate project state, not accidents |
| **PDB worker** (product) | platform essentials; interpreter/venv identity per `python_launcher`; the project runtime environment needed by debugged code (V2-01 transitional: LEGACY PROJECT AMBIENT snapshot) | control/model/provider secrets; provider transport/network material (unless a project network capability is separately authorized) | Debugger executes project code — same trust class as project commands |
| **Verifier command** | platform essentials; the **same declared project-runtime inputs** required for reproducibility (V2-01 transitional: LEGACY PROJECT AMBIENT snapshot), through its own verifier role derivation | control/model/provider secrets; provider transport/network material (unless separately required by an explicit project capability); model-channel state | Verification must be able to reproduce the project (hence project-runtime parity) while remaining independent of provider state; the verifier role may still be stricter than the main execution role where justified |
| **Legacy CLI adapter** (where the historical transport profile still applies) | adapter-owned minimal set + explicit `--auth-file`/forwarded value under the existing endpoint-binding rules | ambient credential re-resolution | Preserves the accepted legacy contract exactly |

**"Credential-free" therefore means precisely: control/provider-credential-free.** PDB and verifier roles are never incapable in general — they consume the explicitly authorized ProjectRuntimeEnvironment (including project-scoped secrets the operator/project authorized) necessary to execute or reproduce the project; they are incapable specifically of *Agentic Debugger control/model/provider secrets*.

**Target contract (positive/declarative) — and its missing ingress:**

```text
ProjectRuntimeEnvironment =
    platform/runtime essentials
  + explicitly authorized project variables
  + explicitly authorized project-secret bindings (where required)
```

No arbitrary ambient inheritance — **as the target**. Verified against live source: `SessionSpec` (`application/session.py:155-157`) and `ExecutionSourceSpec` (`application/sources.py:113-125`) contain **no** project-environment declaration or project-secret binding, and the Local Project start UI exposes **no** project-environment authorization surface (`ui/app.py:730-742`). The positive contract therefore cannot land directly in V2-01 without inventing a new product/session contract and its authorization UX — which V2-01 must not do (minimal surface, §11). Hence the transitional bridge below, and the V2-02 ingress that retires it.

**V2-01 transitional bridge — `LEGACY PROJECT AMBIENT` (explicit, bounded, temporary):**

- **What it is:** a compatibility snapshot of ordinary parent/worker environment variables preserved for **project execution roles only** (project commands, product PDB, verifier commands), created and classified by the session's V2 environment authority and passed explicitly to the runners — so current projects that depend on ambient variables keep working.
- **What it structurally removes (by provenance/known authority identity, not generic name heuristics):** private provider session credential variables (the `AGENTIC_DEBUGGER_PROVIDER_*_API_KEY` hop family); provider config/catalog/quarantine internal authority variables where the project does not require them; provider CLI-auth authority material; provider transport-specific credential channels; every other explicitly classified Agentic Debugger control/model secret. Generic `TOKEN`/`PASSWORD`/`API_KEY` name matching remains available only as a secondary fail-safe.
- **What it must never do:** feed the model adapter or model prompts in any form; journal or fingerprint environment *values*; bypass project/PDB/verifier parity (the same snapshot feeds all three project roles where reproducibility requires it).
- **Identity and exit:** the bridge carries a named compatibility identity/version and explicit removal criteria — it is removed once the V2-02 `ProjectRuntimeEnvironmentSpec` ingress exists and ordinary Local Project execution no longer requires legacy ambient inheritance (§11 V2-02).
- **Residual risk (documented, not hidden):** during the bridge, *unrelated operator environment variables not owned or classified by Agentic Debugger may still reach project code — exactly as they can today*. V2-01 closes the evidenced **cross-domain** leak (Agentic Debugger control/model secrets into project execution); it does not claim the final least-authority model, and no document or UI may represent the bridge as such.
- **Not a rollback switch:** no user-facing "full environment" switch exists; the bridge is the bounded migration state from today's implicit behavior, not a mode.

**Compatibility beyond the bridge is bounded extension, never security rollback.** Compatibility mechanisms are limited to: explicit extra environment-variable *names* (opt-in, classified), role-specific inherited-variable policy adjustments, fail-closed rejection of known control-secret variables, and diagnostics that identify a *missing* variable by name without printing any value. No switch may reintroduce control/model secret flow into project/PDB/verifier roles.

### 6.4 Boundary contract: worker shell ↔ executor (the brain/hands seam, in-process in V2)

- **Commands crossing (typed, existing vocabulary):** tool invocations (the `ToolSpec` names the controller already emits: `run_reproduction`, `run_regression_tests`, `classify_outcome`, `find_function`, `get_source_window`, `express_root_cause_hypothesis`, `apply_patch`, `revert_patch`, `syntax_check`, PDB actions) — each becomes a declared Executor operation with its existing argument contract.
- **Events crossing back:** existing `ToolResult` + observability events through the shared emitter (unchanged kinds).
- **Identity fields:** session id, run id, task id (existing) + role profile identity + execution-contract fingerprint on *product* execution events (new, additive — fenced per §8.7).
- **Cancellation:** the cooperative token (existing semantics exactly: check at safe boundaries, never converted to model/verifier outcomes).
- **Timeouts:** per-operation bounds as today; declared in the environment policy for fingerprinting, not behavior change.
- **Error taxonomy:** existing split preserved and made explicit at the seam — model-correctable (bounded, sanitized, `recoverable=True` only where declared) vs infrastructure (`ToolExecutionError` fatal kinds, `PdbSessionError`, `WorkspaceError`, `CommandExecutionError`) — never conflated into success (already enforced by `tool_registry` + proof gates).
- **Credential policy:** executor operations receive **no Agentic Debugger control/model secrets and no provider transport material**; only the model-adapter role ever does. Project runtime secrets reach project/PDB/verifier roles solely through explicit authorization, and never flow back toward the model channel.
- **Environment policy:** all product executor children build env from the role-scoped, trust-classified derivations (or the classified LEGACY PROJECT AMBIENT snapshot during V2-01). **`CommandRunner` product mode stops building child environments from `os.environ` itself** — authority moves out of `_build_env()`, not merely a denylist inside it. The verified-context mode is untouched.
- **Workspace ownership:** unchanged (worker-owned session work dir; PDB per-session workspace; verifier-owned export root + four clean workspaces; supervisor post-mortem for worker-owned roots).
- **Restart/recovery (V2 scope):** none — worker death remains terminal for the session, honestly classified, with cleanup; retry (new chained session) remains the recovery story (§13 owner decision).

### 6.5 Boundary contracts: PDB roles and the verifier environment seam

**Product PDB vs contained/scientific PDB — distinct authorities, distinct treatment:**

- *Product PDB* (`PdbSession` launched by the Local Project worker): V2-01 gives it an explicit project/PDB role environment — control/provider-secret-free, project-runtime-aware, `python_launcher` venv/PID semantics and the PID handshake/containment preserved, project runtime state needed by debugged code preserved. Today its `_worker_env()` inherits from the worker via `build_worker_env(None)` — part of the active leak surface; V2-01 closes it.
- *Contained/scientific PDB* (WSL/bubblewrap bridge overrides, `PdbLaunchPlan` environment semantics — `quixbugs/contained_pdb.py`, BugsInPy paths): **unchanged** by V2-01 unless a directly proven compatibility requirement exists. These paths already run under reviewed launch plans with explicit environments (§2.5).

**Verifier environment injection preserves construction independence:**

- The session/execution authority supplies the declared ProjectRuntimeEnvironment (or, in V2-01, the classified bridge snapshot) and the verifier role policy **to** the verifier — `LocalProjectVerifier` already constructs its own `CommandRunner`s via its factory (`local_project_verifier.py:286-297`), and that seam is reused: the factory receives the fixed verifier role environment.
- The controller/model path cannot mutate or override the verifier's environment after verification begins; verifier environment policy is never collapsed into controller tool arguments.
- No provider/control secret channel enters it; project-runtime parity with the main execution role is preserved where reproducibility requires it.

### 6.6 ExecutionEnvironment fingerprint — safe, precisely scoped, scientifically fenced

The fingerprinted artifact is the **safe declarative execution contract**, not proof of full environment equality:
- policy/schema version; role policies (rules, not resolved values); interpreter/runtime identity as safely representable; workspace policy/generation identifiers; allowed capability declarations; timeout/resource-policy declarations; safe normalized network/trust policy *identifiers* where appropriate.
- **Never hashed or journaled:** credential values of any class (control or project), potentially credential-bearing proxy URLs, machine-local secrets, project-secret binding values, **any LEGACY PROJECT AMBIENT snapshot contents**. Since secret and machine-local values are intentionally excluded, the fingerprint is a *contract/provenance* fingerprint — it proves which declared policy produced the evidence, not byte-for-byte equality of the entire operator environment. The document and the journal payload must describe it exactly this way.
- **Scientific fence:** the execution-contract fingerprint is PRODUCT/local-session metadata initially. It must not silently alter canonical frozen scientific event serialization, treatment identity, hash inputs, evidence manifests, or qualification (§8.7). Adding it to frozen scientific evidence requires a separate explicit compatibility decision.

### 6.7 Failure-domain analysis (recommended architecture)

| Failure | Lost | Durable | Cleaned | Resume? | Must fail? | Evidence remaining |
|---|---|---|---|---|---|---|
| UI process dies | live view | journal (per-record fsync), manifest, artifacts | job object closes → worker tree killed; worker cleanup may not run; supervisor is gone so post-mortem is OS-driven (job kill) — session classifies INTERRUPTED on next open | reopen as replay only | no | full journal to last append; history classification `interrupted` |
| Session worker dies (crash) | in-flight turn, in-memory controller state | journal | supervisor reaps tree, runs post-mortem (work dir, isolated worktree) | no — INTERRUPTED, operator may start retry chain (new session) | honest fail | journal + crash classification + cleanup diagnostics |
| Verifier fails/crashes (in-process, V2) | verification result | journal up to `verifier.started` | verifier workspaces released by its own ledger/cleanup paths; a hang is bounded by plan timeouts | no — a verifier failure is an honest terminal outcome under the existing taxonomy; same-session re-verification is deferred (§13); the session is not resumed | verifier failure ≠ success; session ends UNRESOLVED or FAILED per existing taxonomy | verifier stage events, cleanup proof; the retained candidate artifact remains for a future, separately designed product feature |
| PDB worker dies | debugger session | PDB events already journaled | `PdbSession.stop` ladder + workspace release (existing) | controller continues without PDB evidence (existing policy paths) | no | bounded PDB observations |
| Adapter child dies | that model request | model provenance, prior steps | process-tree termination (existing) | `LiveModelAdapter` bounded retries (existing) | no | transport error kind, termination reason |
| Provider HTTP timeout | that request | everything journaled | child killed (existing) | bounded retries then honest `model_error` (existing) | no | `LiveTransportError` kind, timing |
| Model protocol violation | the directive | everything journaled | n/a | bounded directive-repair attempts (existing, `9fab308`) | eventually directive-exhausted honest failure | rejected-directive events |
| Credential unavailable | model channel | config, provenance | n/a | no — fail closed before session starts (existing `ScenarioInputError` path) | **yes** (fail-closed is the invariant) | config state, quarantine record |
| Test/repro process hangs | wall time | journal | CommandRunner timeout ladder (existing) | no | timeout status (existing) | bounded output, timeout record |
| Workspace invalid | that operation | journal | cleanup verification flags failure (existing CLEANUP_FAILED) | no — workspace policy failure is terminal for the session (existing) | yes | cleanup events, workspace identity |
| Verifier *fails* (logic) | nothing | full verifier result + taxonomy | four clean workspaces released (existing) | no — terminal under existing taxonomy (re-verification deferred, §13) | outcome is the result (UNRESOLVED/etc.) — never a crash | verification certificate, F2P/P2P records |
| Machine restarts | everything in-memory | session dir (fsynced journal, manifest, artifacts) | nothing automatic — stale work dirs/worktrees are detected by next launch (history classification) and by git worktree prune guidance | no | n/a | durable artifacts intact |

(A verifier *subprocess* death row is intentionally absent: physical isolation is deferred per §5.6; the in-process row above reflects current and V2-02 behavior.)

---

## 7. Session persistence: checkpoint/resume assessment

**Owner decision (resolved, §13): real checkpoint/resume is DEFERRED.** Current retry-chain + durable journal + replay remain the product behavior. Replay remains audit/UI replay, never called resume.

Rationale: no observed failure class is *caused* by the lack of resume; the current honest-INTERRUPTED + retry-chain + full-journal behavior satisfies the audit contract; and resume would require solving workspace-generation equivalence and credential epochs — new review surface on the most safety-critical paths with no current demand. If a future session's `ControllerSnapshot` is already a typed frozen value (favorable), the seams V2 introduces (contract fingerprint, journaled snapshot availability) make a future resume feature *designable* without making V2 pay for it.

**Reopen trigger (owner-agreed):** actual evidence that loss of long-running controller progress is a material operator problem, or another concrete requirement needing resumable sessions. At that trigger, Alternative C may be reconsidered but is not required.

---

## 8. Scientific architecture boundary (mandatory constraints)

1. **Treatment qualification never derives from provider availability or gateway status.** Today the ladder binds qualification to the frozen roster (`is_treatment_eligible` in `scripts/ollama_cloud_command_adapter.py`, qualified profiles via `level32`), and `session_config.py:373` keeps ladder qualification separate from interactive availability. V2 preserves this exactly: `ModelGateway.executability()` and every §9 status fact are *interactive* facts; the ladder/qualification authority stays its own contract, never merged into the gateway.
2. **`AgentDefinition` is product/runtime provenance only — existing scientific identity authorities remain authoritative.** The treatment roster, prompt-profile identity, qualification/eligibility rules, and frozen experiment provenance are not replaced, redefined, or superseded by `AgentDefinition`. If a scientific run references an `AgentDefinition`, it must reference the already-qualified scientific identity; ordinary interactive `AgentDefinition`s never become treatment-qualified merely because they are immutable/versioned. Any future migration of scientific treatment identity into `AgentDefinition` requires its own explicit compatibility decision and is **not part of this V2 plan**.
3. **Prompt/treatment identity:** existing prompt-profile identity (made explicit in `77a4b3f`) remains the scientific authority; gateway provenance continues to record provider/model/protocol/route (existing `MODEL_CONFIGURED` payload); `AgentDefinition` may *reference* that identity, never mint it.
4. **No architecture change may auto-qualify providers for frozen experiments.** Migration stages explicitly do not touch `evaluation/transport_qualification`, the paired-pilot manifests, or the frozen OpenCode Go path (CURRENT_AGENT_ROSTER retains authority).
5. **Verifier independence is preserved** by the VerifierService logical boundary, the control/provider-credential-free verifier role environment (with declared project-runtime parity), and unchanged clean-source/workspace/taxonomy invariants; physical isolation remains available behind §5.6 triggers if a concrete boundary requirement ever demands it.
6. **Offline/deterministic testability is preserved**: every new seam is fakeable (gateway, vault, executor interface, VerifierService); no stage introduces a network or live-model requirement for tests.
7. **Execution-contract provenance is fenced out of frozen scientific identity.** The V2 execution-contract fingerprint and role-profile identity are PRODUCT/local-session metadata initially. V2-01 must not silently alter canonical frozen scientific event serialization, treatment identity, hash inputs, evidence manifests, or qualification. If a shared event schema carries an additive product field, that field must be proven non-authoritative and non-participating for frozen scientific identity — or kept out of the frozen path entirely. Adding ExecutionEnvironment fingerprinting to frozen scientific evidence requires a separate explicit compatibility decision. No new scientific provenance authority is created by accident.
8. **Existing scientific/contained execution contracts are fenced from V2-01 product work.** V2-01 must not alter: `PreparedEnvironment` behavior or serialization; `VerifiedExecutionContext` binding/serialization; `ContainmentGuarantee`; existing WSL/bubblewrap execution; QuixBugs/BugsInPy execution; contained-PDB launch-plan environment semantics; frozen scientific environment/evidence identity. The directly relevant test suites (`tests/unit/test_bugsinpy_authorized.py`, `test_bugsinpy_wsl.py`, `test_live_quixbugs.py`, `test_quixbugs_adapter.py`, `test_quixbugs_contained_pdb.py`, `test_opencode_go_case_runner.py`) are V2-01 **NON-REGRESSION gates** whenever shared runtime modules (`runtime/command_runner.py`, `runtime/test_runner.py`, `runtime/execution.py`) are touched transitively.

---

## 9. Product/UI consequence (vocabulary, not redesign)

Keep the UI structure; make it stop speaking implementation dialects and stop implying reachability it did not verify. The post-plan incident (`Connected · saved` displayed for CommandCode GOAT while `http://127.0.0.1:57788` had no listener, followed by a failed model request) proved that presence-only `Connected` is materially misleading; the vocabulary repair is evidence-driven, not cosmetic.

**Target status vocabulary (precise, separate facts):**

| Fact | Meaning | Reachability claim |
|---|---|---|
| `Configured` | valid, enabled durable provider configuration exists | none |
| `Credential ready` | an authorized credential binding can currently be obtained | none |
| `Model runnable` / `Ready` | static auth × protocol × profile × route preflight succeeds | none |
| `Catalog refreshed at T` | last catalog operation succeeded at T (cached fact, may be stale) | historical only |
| `Live verified at T` / `Reachable at T` | an explicit real connection/model probe succeeded at T | historical fact, never a permanent guarantee |
| `Runtime succeeded at T` | a real session using that provider/model completed the model transport path at T | historical evidence only |

**Headline decision:** remove the ambiguous `Connected` headline (recommended — show the precise facts above), or, if a headline is kept, reserve `Connected` exclusively for a successful live probe with a visible timestamp and staleness rule. **Credential presence alone must never be labeled Connected.** None of these facts is ever conflated with scientific qualification (§8).

**`Runtime succeeded at T` is observational history, not provider-config authority (§13, resolved).** It is derived from the durable session/event history keyed by provider/model/runtime binding — not produced by mutating durable provider configuration after each successful session. It must never become provider-config truth, a route-selection input, or a qualification input. If implementation later adds a cache/index for UI speed, that cache is derived and rebuildable from authoritative history. Implementation is deferred to the V2-03 ModelGateway/UI stage.

Remaining UI vocabulary decisions (already owner-resolved, §13): the advanced provider-type/endpoint-contract control replaces primary "Transport Profile (generic unless a historical endpoint contract is intended)" wording (`ui/screens.py:1928,2140`); credential-source labels move to a diagnostic details line; per-model `Ready / Needs API key / Protocol unsupported` (gateway-issued, actionable) replaces `available` + reason strings; catalog staleness markers stay as-is (already truthful). The V2-02 `ProjectRuntimeEnvironmentSpec` ingress is a product/session contract first; its eventual UI surface is designed later, minimally (no redesign now).

---

## 10. Security model (target, least authority)

**Two secret classes, two authorities.** *Control/model secrets* (provider API credentials, model-channel variables, provider CLI-auth/credential-store material) are owned by the control plane and confined to the model channel. *Project runtime secrets* (test DB credentials, service tokens, application keys, fixture credentials, private package/test-service access) are owned by the explicitly authorized ProjectRuntimeEnvironment and confined to the project execution roles. Neither class crosses into the other's domain, and neither appears where it is not authorized.

**Where control/model secrets may exist:** UI process memory (vault resolution), the single worker-spawn env hop (one variable, value only, `worker_process.py:136-151` — mechanism unchanged through V2-03), the worker's bounded private session state for the duration of the authorized lease (stated honestly, §6.2), the per-request adapter child env (vault-issued material in the model-channel role only, from V2-04), the OS secure store, and (legacy routes) the operator CLI auth store read in place at the vault layer.
**Where they must never exist:** argv, the start message, scenario params, journals, events, manifests, reports, UI text beyond presence-only labels, checkpoints (none), diagnostics (existing `contains_credential_shape` scrubbing stays), `ExecutionEnvironment` (bindings-by-name only), and — **after V2-01 — any project/PDB/verifier child env** (today's `CommandRunner._build_env` full-inherit is the one deviation; V2-01 closes it).

**Where project runtime secrets may exist:** explicitly authorized project-secret bindings supplied for the session (operator/project authorization; the V2-02 ingress contract); the project/PDB/verifier role environments that received them.
**Where they must never exist:** the model adapter, model prompts, journals, events, fingerprints, diagnostics/reports/UI, durable provider configuration, and the control-plane credential channels. They are never implicitly copied merely because they exist in the parent process — authorization is explicit and per-session-binding.

**Transitional honesty (V2-01 bridge):** until the V2-02 ingress retires the LEGACY PROJECT AMBIENT snapshot, unclassified operator environment variables may still reach project code — exactly as today. The architecture closes the *cross-domain* leak (Agentic Debugger control/model/provider authorities) first and says so plainly everywhere the bridge is described; the residual risk is a documented property of the migration state, not a hidden weakness and not the target model.
**Who may resolve credentials:** only the designated authority for its class. The control plane resolves provider bindings at session start (existing forwarding through V2-03; vault from V2-04); the worker materializes the authorized secret into adapter request children; the adapter child consumes the issued channel and never re-resolves ambient state (V2-04 removes the current third resolution). Project-secret authorization is a separate authority; no project-secret storage/synchronization is designed in this plan (a future project-secret binding may reuse a vault backend if justified, remaining a distinct authority).
**Does the executor need provider credentials?** No — and after V2-01 it structurally cannot see them.
**Does the gateway need workspace filesystem access?** No — it constructs transport configs and bindings only; it never touches execution workspaces (already true; the interface makes it a rule).
**Environment classification:** trust classes by provenance/capability (§6.3) are the primary authority; known-name/value detection is a secondary fail-safe only. The TLS/proxy subset (`provider_connections.py:648-657`) is a *provider-transport capability* confined to the model-adapter role; a project network/trust capability (proxy/CA) is separately authorized for project roles and never inferred from the provider transport environment; the same variable name may legitimately appear in both authorities with different provenance.
**Endpoint/credential binding:** preserved verbatim (vault-owned): ambient sources are canonical-endpoint-bound; saved/session are provider-identity-bound; quarantine blocks resolution (existing rules, `provider_connections.py:2281-2365`).
**Process identity and containment:** unchanged — Windows job object on the worker tree, PID identity via `python_launcher` (declared in the environment policy, not duplicated), per-command tree-kill ladders; the verified scientific containment path (`ContainmentGuarantee`) is untouched.
**Compatibility boundary:** the V2-01 LEGACY PROJECT AMBIENT bridge (project-role-only, classified provenance exclusions, named identity/version, explicit removal criteria) plus bounded extension only — explicit extra classified variable names, role-policy adjustments, fail-closed control-secret rejection, name-only diagnostics. **No user-facing full-environment mode may ever exist** (see §6.3).

---

## 11. Migration strategy (vertical, incremental, complexity earned immediately)

Each stage leaves the repository runnable, is testable, has one compatibility seam, and never requires touching UI + providers + debugger + verifier simultaneously.

### V2-01 — ExecutionEnvironment authority + credential isolation (first slice: minimal coherent surface)
- **MUST achieve (the whole slice):**
  1. a typed product execution-environment policy authority exists;
  2. child roles are explicit;
  3. project/PDB/verifier children can no longer inherit Agentic Debugger provider/model credential channels;
  4. the model adapter keeps its current authorized credential behavior (existing forwarding authority preserved — the Vault does not exist yet; V2-04 introduces it; the session worker remains a trusted control process receiving the current parent/session environment and the private provider credential hop as temporary internal control-plane state);
  5. Windows interpreter/PID behavior remains unchanged (`python_launcher` stays the single authority; product PDB keeps its venv/PID handshake);
  6. project runtime compatibility is proven (via the LEGACY PROJECT AMBIENT bridge where projects depend on ambient variables);
  7. regression tests demonstrate control/provider-secret exclusion (the authority-conflict acceptance set below).
- **Minimal surface:** the environment policy type, the role derivations actually consumed by this slice, the classified LEGACY PROJECT AMBIENT snapshot mechanism, and the fail-closed exclusion of classified control-secret channels. Other `ExecutionEnvironment` fields (workspace ownership, tool availability, resource limits, full network policy, fingerprint/event schemas) are introduced **only if used by this slice**; workspace/tool/resource policy fills in incrementally in V2-02. No speculative "complete future dataclass" scaffolding.
- **Authority placement (CommandRunner):** the session's V2 environment authority creates and classifies each child environment (including the bridge snapshot) and passes it explicitly to the runner; `CommandRunner` product mode **stops reading `os.environ` itself** — authority moves out of `_build_env()`, not merely a denylist inside it. The `VerifiedExecutionContext` mode is untouched (§6.2a precedence: conflicting authorities are rejected, never merged).
- **PDB distinction:** product PDB receives the explicit project/PDB role environment; contained/scientific PDB (`PdbLaunchPlan`/WSL/bubblewrap) is untouched unless a directly proven compatibility requirement exists.
- **Verifier seam:** the session/execution authority supplies the declared verifier role environment (bridge snapshot in V2-01) through `LocalProjectVerifier`'s existing `command_runner_factory`; the controller/model path cannot mutate it after verification begins; verifier environment policy is never collapsed into controller tool arguments.
- **Compatibility:** the LEGACY PROJECT AMBIENT bridge (§6.3) — project-role-only, classified provenance exclusions by known authority identity, named compatibility identity/version, explicit removal criteria, documented residual risk; plus bounded extension only. No full-environment mode.
- **Acceptance tests (minimum set):**
  1. provider private session credential present in the worker → absent from an ordinary project command child;
  2. same credential → absent from the ordinary product PDB worker;
  3. same credential → absent from a `LocalProjectVerifier` command child;
  4. provider TLS/proxy/control environment does not implicitly enter project roles;
  5. a benign arbitrary project ambient variable remains available through the LEGACY PROJECT AMBIENT bridge (compatibility proven);
  6. project/PDB/verifier roles receive equivalent declared project runtime state where reproducibility requires it;
  7. the model adapter retains its existing provider credential/network behavior;
  8. `VerifiedExecutionContext` command execution remains behaviorally unchanged;
  9. supplying conflicting product and `VerifiedExecutionContext` authorities to one call site fails closed rather than merging;
  10. QuixBugs/BugsInPy/contained-PDB scientific execution regressions remain green if shared runtime modules were touched (NON-REGRESSION gate, §8.8);
  11. Windows venv/PID identity regressions remain green.
- **Validation:** the acceptance set above; existing command-runner/transport/PDB tests; real Windows project sessions proving project runtime compatibility (the concrete bridge/allowlist set is determined here from repository evidence and these tests).
- **Rollback/exit:** exit criterion = no repro/verify behavior regression across curated fixtures and real projects, with the full acceptance set green; rollback = revert the stage (no durable format consumed it yet beyond additive product-only provenance fields, which are dropped before any frozen run uses them).

### V2-02 — Session/AgentDefinition/Executor logical seams + capability intersection + ProjectRuntimeEnvironmentSpec ingress
- **Outcome:** typed `SessionLaunch`/`Session`; `AgentDefinition` (desired capabilities; requested model identity — no runtime route) and `ExecutionEnvironment` (available capabilities) with **`EffectiveSessionCapabilities` computed once** as session provenance; `Executor` interface around the existing execution modules; `VerifierService` logical boundary (verifier code and invariants untouched); incremental fill-in of environment fields actually consumed (workspace/tool/resource policy as needed); **the explicit `ProjectRuntimeEnvironmentSpec` product/session ingress** (§6.2) — project variable names / explicit non-secret values, project network/trust requirements, project-secret binding *references*, per-declaration provenance, never embedding secret values in durable evidence; no new process; no scientific identity migration; no UI redesign.
- **Bridge exit criterion (the stage's key architectural outcome):** ordinary Local Project execution can be conducted entirely through the declared `ProjectRuntimeEnvironmentSpec` — i.e., it **no longer requires arbitrary legacy ambient inheritance**. Only after that criterion is met may the V2-01 LEGACY PROJECT AMBIENT bridge be removed (removal itself may be sequenced within or after this stage on implementation evidence; the exit mechanism is defined here, not left undefined).
- **Affected:** `application/worker_process.py`, `worker.py`, `session.py`, `sources.py` (ingress contract), sources, `application/verifier_observer.py` (call-site only), `ui/app.py` (call sites only), tests.
- **New contract:** `SessionLaunch`, `AgentDefinition`, `ExecutionEnvironment` capability declarations, `EffectiveSessionCapabilities`, `ProjectRuntimeEnvironmentSpec`, `Executor` (interface), `VerifierService` (interface).
- **Compatibility:** old call paths adapter-shimmed for one stage; behavior byte-identical except the new ingress being optional-with-bridge-fallback during the stage.
- **Validation:** worker-process integration tests, local-project integration tests (including a session started purely through the declared ingress), verifier observability tests, offline demo.
- **Rollback/exit:** revert the commit; the bridge remains until the ingress proves out; no durable format change beyond additive fields.

### V2-03 — ModelGateway product/runtime seam (+ ModelBinding)
- **Outcome:** UI and sources consume `ModelGateway` only; `ModelBinding` (route, effective protocol, endpoint contract as resolved, resolved endpoint identity, credential binding reference, adapter provenance) becomes session/runtime provenance — never `AgentDefinition` state; UI no longer understands provider transport internals; precise §9 status vocabulary implemented (including removal/redefinition of `Connected` and history-derived `Runtime succeeded at T` metadata); `Provider type` / `Endpoint contract` advanced disclosure replaces primary transport-profile wording; the current provider core modules stay intact beneath.
- **Affected:** `ui/app.py`, `ui/screens.py`, `ui/session_config.py`, sources, `model_providers.py` (façade methods only).
- **New contract:** `ModelGateway` interface + `ModelBinding` provenance + the §9 status-fact semantics (history-derived).
- **Compatibility:** the façade delegates to existing functions; durable `transport_profile` unchanged; UI changes are labels/organization only.
- **Validation:** UI tests (`test_ui_provider_connections.py`, `test_ui_configured.py`), provider integration tests, updated vocabulary assertions (including a regression test that credential presence alone never renders a connected-style headline).
- **Rollback/exit:** UI can call the old functions for one stage; exit when no UI import of provider internals remains.

### V2-04 — CredentialVault binding/backend seam
- **Outcome:** `CredentialVault` interface arrives here (it does not exist in V2-01–V2-03): `CredentialBinding` (non-secret, session-stable) separated from credential material/lease (per-request injection, honest session-scoped retention §6.2); single session authority decision; the adapter child consumes the issued channel and stops re-resolving ambient state (removing the third resolution from §3.2); Windows Credential Manager backend first; future macOS/Linux backends possible but not claimed.
- **Affected:** `application/provider_connections.py` (backend extraction), `scripts/provider_direct_api_adapter.py` (binding consumption), tests.
- **New contract:** `CredentialVault` interface, `CredentialBinding`, lease/materialization semantics.
- **Compatibility:** endpoint-binding/quarantine rules move verbatim; the extensive existing credential tests must pass with unchanged behavior.
- **Rollback/exit:** backend extraction is internal; exit when the adapter no longer imports `resolve_runtime_credential` and all credential tests pass.

### V2-05 — OPTIONAL verifier process-isolation evaluation (not a committed migration requirement)
- **Outcome:** evaluate the §5.6 triggers against field evidence. By this stage the logical `VerifierService` seam and the verifier role environment already exist, so extraction would be a transport adapter — but it happens **only if a trigger is evidenced**. Otherwise the stage is recorded as **NOT JUSTIFIED / DEFERRED** and no implementation work is created merely to complete the numbered list.
- **Validation (if triggered):** verifier integration + kill/hang tests for the child, offline demo; exit criteria defined at that time.
- **Rollback/exit:** not triggered ⇒ no work, stage closed as deferred with the evidence review attached.

**Sequencing rationale:** V2-01 lands first because the credential exposure is active and evidenced — with a minimal coherent surface so the first build stays buildable, and with the bridge honestly labeled transitional; V2-02 provides the seam vocabulary, the capability intersection, and the ingress whose existence retires the bridge; V2-03/V2-04 are pure interface seams ordered by user-visible value (status truthfulness) then portability; V2-05 stays optional and trigger-gated. Each stage states which credential authority exists at that stage (existing forwarding through V2-03; Vault from V2-04) and which environment authority governs each call site (verified context for scientific callers; product role profiles for product callers).

---

## 12. Recommendation summary (decisive)

1. **Is a V2 architecture change warranted?** Yes — Alternative B: explicit logical planes and first-class authority contracts in the current principal process topology. The observed incidents form a class (undeclared execution-environment/credential/identity state re-derived at multiple sites), one active instance of that class (credential variables inherited by project repro/verify and verifier children) is present in current source, and one post-plan product incident (presence-only `Connected` with a dead endpoint) confirms the status-vocabulary defect. The class is worth closing at its generator.
2. **Which alternative?** B — `Session`, `AgentDefinition` (desired capabilities, requested model identity), `ExecutionEnvironment` (available capabilities; one **product** authority, role-scoped trust-classified derivations), `VerifiedExecutionContext` (the existing **specialized scientific/contained** authority, preserved verbatim with explicit precedence), `EffectiveSessionCapabilities` (computed intersection, session provenance), `ModelGateway` + `ModelBinding` (runtime route provenance), `CredentialVault` (binding vs materialization, V2-04), `Executor` and `VerifierService` interfaces, `ProjectRuntimeEnvironmentSpec` ingress (V2-02); **no new long-lived processes, no committed process moves**; verifier subprocess isolation and same-session re-verification both deferred; Alternative C rejected.
3. **What remains untouched?** `DeterministicController` and its policy/state machine; the PDB protocol/worker/session (contained/scientific PDB environment semantics included); `VerifiedExecutionContext`/`PreparedEnvironment`/`ContainmentGuarantee`/`PdbLaunchPlan` and their WSL/bubblewrap/QuixBugs/BugsInPy callers; the journal/emitter/events/observability evidence chain (with the §8.7 scientific fence on any additive product provenance); patch policy (`PatchManager`); disposable-workspace semantics; the verifier's logic, outcome taxonomy, and clean-source/workspace invariants; history/replay/presentation projections; the scientific qualification boundary and existing treatment-identity authorities; frozen experiment paths; dataset adapters; the demo offline path; Windows job-object containment.
4. **First implementation slice?** V2-01 — ExecutionEnvironment authority + credential isolation with the minimal coherent surface (§11): typed product policy authority, explicit child roles, structural exclusion of Agentic Debugger control/model secret channels from project/PDB/verifier children, unchanged model-adapter and Windows interpreter behavior, the LEGACY PROJECT AMBIENT bridge for compatibility (transitional, residual risk documented), `VerifiedExecutionContext` untouched, and the eleven-item acceptance test set. Authority moves out of `_build_env()`; no typing-only or scaffolding-first stage.
5. **Explicitly deferred:** real checkpoint/resume (owner-deferred; reopen on the §7 trigger); verifier physical subprocess isolation (§5.6 triggers); same-session verifier re-verification (owner-deferred; a verifier failure remains an honest terminal outcome; a future re-verify is a separate product feature behind the VerifierService seam with explicit evidence-lineage semantics); the LEGACY PROJECT AMBIENT bridge's removal (until the V2-02 ingress proves out); project-secret storage/synchronization (policy defined, design deferred); macOS/Linux credential backends (interface prepared in V2-04, implementations out of scope, none claimed); UI redesign beyond vocabulary; scientific-path verifier changes; any migration of scientific treatment identity into `AgentDefinition` or of the execution fingerprint into frozen scientific evidence; any unification of the product `ExecutionEnvironment` with the verified execution context (a later explicit compatibility decision, never an implicit V2-01 consequence).
6. **What would make us reconsider?**
   - *Toward C (process split):* the §7 checkpoint/resume trigger firing (material operator loss of long controller progress or a concrete resumable-session requirement), or PDB/tool serialization becoming a demonstrated bottleneck.
   - *Toward A (stop V2):* if V2-01's role-scoped environments cannot land without destabilizing the worker boundary tests, or if real user repro scripts cannot be satisfied safely through the bridge plus explicit declaration (escalate per §13; fallback is Alternative A with documented residual credential-exposure risk).
   - *Toward verifier physical isolation (V2-05 trigger):* verifier crashes/hangs materially threatening worker lifecycle, in-process environment isolation proving insufficient, a concrete untrusted-code security boundary, or measured operational evidence that isolation pays for its Windows cost.
   - *Toward same-session re-verification:* operator demand showing real value in re-running verification over a retained candidate — designed then as a separate product feature behind the VerifierService seam with explicit evidence-lineage semantics.
   - *Toward authority unification:* if field evidence shows the product/verified environment authorities diverge in ways that cause real defects, a shared lower-level primitive may be extracted under the §6.2a conditions — proven-identical semantics, unchanged frozen/scientific behavior, contained-execution tests still authoritative.
   - *Away from B's status vocabulary:* if owner testing shows the precise-facts display (§9) confuses users more than it informs, the headline form may return only under the live-probe-with-timestamp rule — never presence-only.

---

## 13. Owner decisions

**Resolved (recorded 2026-09-03, FirstMate architecture reviews 02, 03, and 04):**

1. **Transport-profile UI (resolved, review 02):** explicit transport identity remains in durable internal configuration (safety-critical) and stays discoverable in the UI under an **advanced `Provider type` / `Endpoint contract` control**; ordinary users do not see "historical transport profile" as primary vocabulary; known presets (CommandCode, OpenCode, Ollama) may expose their explicit contract through the advanced control; Generic remains the normal custom-provider default; no inference from provider technical ID/name/URL. Implemented in V2-03.
2. **Runtime-tested timestamp (resolved, review 02; refined 03):** `Runtime succeeded at T` is observational history **derived from the durable session/event history** keyed by provider/model/runtime binding — not provider-config mutation. Must state exactly what succeeded, include provider/model identity, never imply current availability, never affect scientific qualification, never become a route-selection input; any UI cache is derived and rebuildable. Implementation deferred to V2-03.
3. **Checkpoint/resume (resolved, review 02):** DEFERRED. Current retry-chain + durable journal + replay remain the product behavior; replay is never called resume. Reopen only on the §7 trigger; Alternative C may be reconsidered then but is not required.
4. **Execution-environment allowlist (resolved as policy, review 02; target refined 03/04):** the architecture decision is the policy — role-scoped least-authority derivation from trust-class provenance; Agentic Debugger control/model secrets never reach execution roles; required platform/runtime variables preserved; project variables and project-secret bindings explicitly authorized via the V2-02 ingress; the V2-01 LEGACY PROJECT AMBIENT bridge covers the transitional gap with documented residual risk; tests + real Windows projects determine the concrete set. The implementation stage owns discovering it; escalate only if a real user-script compatibility choice cannot be resolved safely.
5. **Verifier re-run policy (resolved, review 03):** DEFER same-session re-verification. Current terminal semantics remain authoritative during V2-01/V2-02; a verifier failure remains an honest terminal outcome under the existing taxonomy; no new event kinds or state-machine transitions are added for architecture work. If operator demand later shows value, re-verification over a retained candidate is designed as a separate product feature behind the VerifierService seam with explicit evidence-lineage semantics.
6. **Secret trust classes (resolved, review 03):** control/model secrets and project runtime secrets are distinct authorities (§6.3, §10); the positive/declarative ProjectRuntimeEnvironment is the target contract; project-secret storage/synchronization is out of plan scope.
7. **Capability authority (resolved, review 03):** `AgentDefinition` declares desired/requested capabilities; `ExecutionEnvironment` declares available capabilities; `EffectiveSessionCapabilities` is the single computed intersection (plus task/product policy) and becomes session provenance; `ModelBinding` owns the resolved runtime route, never `AgentDefinition`.
8. **Verified execution authority disposition (resolved, review 04):** the existing `VerifiedExecutionContext`/`PreparedEnvironment`/`ContainmentGuarantee`/`PdbLaunchPlan` contracts remain the specialized reviewed scientific/contained execution authority — not migrated, weakened, broadened, or replaced by the product `ExecutionEnvironment`; precedence at shared call sites is explicit and fail-closed (conflicting authorities are rejected, never merged); any future unification requires its own explicit compatibility decision under the §6.2a conditions.
9. **Project-environment ingress staging (resolved, review 04):** because no product ingress exists today (verified: `session.py`, `sources.py`, Local Project UI), V2-01 uses the transitional LEGACY PROJECT AMBIENT bridge (project-role-only, classified provenance exclusions, named identity/version, documented residual risk) and **V2-02 introduces the `ProjectRuntimeEnvironmentSpec` ingress** whose adoption retires the bridge. The exit mechanism is defined here, not left undefined.

**No owner decisions remain open.** Reopen triggers are documented in §5.6 (verifier isolation), §7 (checkpoint/resume), and §12.6 (re-verification demand, status-vocabulary form, authority unification).

---

## 14. Source references (validation)

All current-state claims in this document trace to the following inspected source/tests (baseline `4606933`):

- Worker topology: `agentic_debugger/application/worker_process.py`, `worker.py`, `worker_protocol.py`, `worker_scenarios.py`; `tests/integration/test_worker_process.py` (handshake, crash classification, pre-start cancel, journal catch-up).
- Sources and execution interleave: `application/local_project_source.py` (1,333 lines — controller+tools+PDB+patch+transport+verifier in one function), `local_source.py`, `configured_source.py`, `deterministic_source.py`, `ollama_cloud_source.py`.
- Existing verified execution authority (reconciled in revision 04): `runtime/execution.py` (`PreparedEnvironment` with the credential-like-key prohibition at 130-134; `ContainmentGuarantee` fail-closed validation at 150-174; `VerifiedExecutionContext` approved-command binding/build_environment at 177-235; `PdbLaunchPlan` at 31-65); consumers verified by grep: `bugsinpy/wsl.py`, `wsl_preparation.py`, `adapter.py`; `quixbugs/adapter.py`, `quixbugs/contained_pdb.py`; `evaluation/live_quixbugs.py`; `evaluation/verifier.py:145-237` (optional `execution_context` → `TestRunner(workspace, execution_context=...)`); `runtime/test_runner.py:58-61`; `runtime/command_runner.py:183-196` (two-mode dispatch); `demo/tools.py`; operator scripts (`scripts/quixbugs_*.py`); tests (`tests/unit/test_bugsinpy_authorized.py`, `test_bugsinpy_wsl.py`, `test_live_quixbugs.py`, `test_quixbugs_adapter.py`, `test_quixbugs_contained_pdb.py`, `test_opencode_go_case_runner.py`). **`evaluation/local_project_verifier.py` uses the plain product `CommandRunner` path (no verified context; grep-verified).**
- Missing ingress (reconciled in revision 04): `application/session.py:155-157` (`SessionSpec` = task_id/source/budgets/artifact_destination); `application/sources.py:113-125` (`ExecutionSourceSpec` = kind/task_id/policy/model_config_ref); `ui/app.py:730-742` (`start_local_project_session` parameters — no project-environment surface).
- Providers/credentials/status: `application/model_providers.py`, `provider_connections.py` (credential ladder 2319-2509; existing session/transport forwarding authorities at 2417/2458; endpoint binding 2281-2316; network allowlist 648-694 and the proxy-credentials note at 642; wincred 432-538; truthful presence-only `ProviderConnectionStatus` 3020-3081), `provider_http.py`, `command_transport.py:153-186` (and per-request `Popen` at 268), `scripts/provider_direct_api_adapter.py:132-148`; `tests/unit/test_configured_provider_params.py`, `tests/integration/test_ui_provider_connections.py`, `test_ladder_unified_provider_runtime.py`, `test_provider_direct_api_session.py`.
- Environment sites (incl. the active exposure): `runtime/command_runner.py:293-296` (`_build_env = dict(os.environ)`), `evaluation/live.py:523` (minimal env), `command_transport.py:153-186` (merged env), `runtime/pdb_session.py:390-405` + `runtime/python_launcher.py` (venv/PID authority), `worker_process.py:309-320` (credential hop); worker-side credential retention for per-request re-injection: `local_project_source.py:1024-1025`.
- Project runtime/network capability precedent: `application/local_project_source.py:256` (`Constraints(..., network_allowed=False, external_services_allowed=False, ...)` — project networking is already a declared task-policy capability today, supporting the separate project network/trust authority in §6.3).
- PDB: `runtime/pdb_session.py`, `pdb_worker.py`, `pdb_protocol.py`; contained/scientific PDB distinction: `quixbugs/contained_pdb.py` (WSL/bubblewrap launch overrides over `PdbSession`); `tests/integration/test_pdb_session_integration.py`, `test_pdb_interactive_controls.py`.
- Verifier (substantive logical independence, in-process today; factory seam at `local_project_verifier.py:286-297`): `evaluation/local_project_verifier.py` (304-470: commit binding, clean-source export, four clean workspaces, independent baseline/candidate evaluation, cleanup/source-integrity proof), `evaluation/verifier.py`, `outcome_taxonomy.py`; invoked in-process at `application/local_project_source.py:1120-1156`; `tests/integration/test_evaluation_verifier.py`, `test_verifier_observability.py`.
- Journal/history/replay: `application/journal.py` (fsync-per-append, 119-194), `emitter.py`, `events.py`, `history.py` (manifests/reopen/classification), `replay.py` (read-only cursor — "not resume" claim); `tests/unit/test_application_journal.py`, `test_application_history.py`.
- Controller/tools: `agent/controller.py:845-930` (cancel-check contract), `tool_registry.py`, `skills/`; `tests/unit/test_controller.py`, `test_controller_cancellation.py`.
- UI vocabulary: `ui/screens.py:1277-1282,1520-1528,1928,2140`, `ui/session_config.py` (availability vs qualification separation), `ui/app.py:476-494,655-671,895-911` (credential hop + worker spawn).
- Windows harness: `application/process_tree.py` (job object, suspended spawn, kill ladders); roster/scientific boundary: `CURRENT_AGENT_ROSTER.md`; prompt-profile identity: commit `77a4b3f`.
- Post-plan provider-status incident (`Connected · saved` with unreachable `127.0.0.1:57788` CommandCode GOAT endpoint during live owner validation, 2026-09-03): owner/FirstMate review evidence; consistent with the presence-only `connected` definition at `provider_connections.py:3072-3081`.

Validation performed for this revision: documentation-only repair; every current-source claim touched by the repair was re-verified against the live repository — `runtime/execution.py` read in full (contract semantics, credential prohibition, fail-closed containment, approved-command binding); `VerifiedExecutionContext` consumer inventory by grep across `agentic_debugger/`, `scripts/`, `tests/` (BugsInPy, QuixBugs, contained PDB, verifier context path, test_runner, demo tools, operator scripts, tests); absence of project-environment ingress verified in `application/session.py`, `application/sources.py`, and the Local Project start UI; `evaluation/local_project_verifier.py` confirmed on the plain product `CommandRunner` path. Internal section-reference audit re-run. Documentation navigation and static checks re-run (`tests/unit/test_public_documentation_navigation.py`, `tests/unit/test_professor_trace_r6.py` — passing; `compileall` clean). No production changes, no dependency changes, no provider configuration changes, no credential operations, no live provider calls, no frozen experiment runs, no full test suite run — docs-only candidate per repository validation policy.

## 15. V2-01 implementation note (status only — decision unchanged)

V2-01 implements the §11 first slice only: the product `ExecutionEnvironment` authority (`agentic_debugger/application/execution_environment.py`, bridge identity `legacy-project-ambient/v1`), explicit `PROJECT_COMMAND` / `PRODUCT_PDB` / `VERIFIER` role derivations threaded to Local Project reproduction/regression/tool commands, the product PDB worker (via the existing `build_worker_env` authority), and `LocalProjectVerifier` command children (via its existing `command_runner_factory`); `CommandRunner` product mode consumes the explicit mapping and rejects a simultaneous `VerifiedExecutionContext` + product environment; `runtime/execution.py` and the model-adapter transport are behaviorally unchanged. V2-02+ (Session/AgentDefinition/Executor/ProjectRuntimeEnvironmentSpec/ModelGateway/CredentialVault/verifier isolation) is not implemented.

Proxy/TLS provenance clarification demonstrated by the implementation: ordinary ambient `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` / `SSL_CERT_FILE` / `SSL_CERT_DIR` / `CURL_CA_BUNDLE` values that exist solely as parent ambient state pass through the LEGACY PROJECT AMBIENT bridge unchanged in V2-01 (there is no project-network ingress yet to authorize them separately — documented residual compatibility risk for V2-02). What V2-01 forbids by provenance is merging/copying a provider/model child environment or provider-derived transport override into project roles. Tests characterize this distinction rather than asserting ambient proxy absence.

Repair 06 (same V2-01 slice, no architecture change): the one per-session authority is created once in the worker before source dispatch and carried on the scenario context, so it also covers the worker-owned direct Git utility children found by the 06 subprocess inventory — tracked-source inventory (`git ls-files`), all verifier-owned Git commands (`rev-parse`/`status`/`archive`/cleanup reinspection via the single verifier `_run_git` authority), and normal worker terminal cleanup (`git worktree prune`/`list`). The verifier takes ONE fixed product environment for both its CommandRunner and Git children (custom-factory + explicit-env fails closed). Direct `Git` helpers in `runtime/patcher.py` are proven unreachable from the normal Local Project controller/verifier path (behind `official_patch_compatibility`, default `False`) and are unchanged.

## 16. V2-02 implementation note (status only — decision unchanged)

V2-02 implements the §11 second slice only: the typed product
session/runtime contracts with no new process and no provider-transport,
scientific-identity, journal-schema, or UI-redesign changes:

- **Session/SessionLaunch authority** (`application/session_runtime.py::SessionLaunch`,
  built only via `build_local_project_launch`): binds session/task identity,
  `AgentDefinition`, the session `ExecutionEnvironment`, the
  `ProjectRuntimeEnvironmentSpec`, the computed `EffectiveSessionCapabilities`,
  pre-ModelGateway provider/model request identity (`provider_id`/`model_id`/
  `profile_id`), `DemoPolicy` value, budgets, and `retry_of`. `SessionSpec`
  remains the serialized Task-1 compatibility representation; `SessionLaunch`
  is the authoritative in-process binding (never deserialized). The worker
  builds it once after the pre-start gate and carries it on the scenario
  context; the source consumes it, falling back to the same factory for
  direct non-worker callers.
- **AgentDefinition** (same module): `controller_policy` (a `DemoPolicy`
  value — references, never replaces, the existing policy authority),
  requested `provider_id`/`model_id`, requested `allowed_capabilities`.
  Excludes route/protocol/endpoint/transport-profile, credentials/bindings,
  catalog, live status, and scientific qualification.
- **EffectiveSessionCapabilities** (same module): requested ∩ available ∩
  task policy, computed ONCE (`compute_effective_capabilities`). The
  vocabulary is exactly `project_command` / `pdb` / `patch` / `verifier`
  (no network capability: `Constraints.network_allowed` stays authoritative).
  Task policy denies `pdb` for `static-baseline` sessions. All four are
  enforced end-to-end (project commands + PDB through the Executor seam,
  patch gates at the tool handlers, verifier invocation gate).
- **ProjectRuntimeEnvironmentSpec ingress** (same module; UI `ProjEnv` row
  → `SessionConfig.project_env_text` → `parse_project_env_declarations` →
  `start_local_project_session(project_env_text=...)` → transported as the
  safe `project_runtime_spec` scenario param, durable as safe
  `LocalProjectTaskSpec.project_runtime`): explicit non-secret values,
  inherit-by-NAME declarations (`NAME` required, `NAME?` optional), and
  project-secret binding NAMES (`secret:NAME`, `secret:NAME?`).
- **Secret-value lifetime**: declared names resolve ONCE at session launch
  from the fixed launch snapshot (`materialize_project_runtime`); values
  live only in trusted session-process memory and authorized project-role
  child envs. The spec carries names only: never values in
  spec/params/history/journal/repr/fingerprints; never provider
  credentials. No CredentialVault (V2-04) and no plaintext secret textbox
  (values are never entered in the UI).
- **Secret egress seal (repair 10)**: the project child itself receives the
  real secret; the enforceable boundary is the return direction. Raw
  materialized project-secret values are redacted at the product
  execution-result boundaries — `ProductExecutor.run_project_command`
  stdout/stderr, product PDB tool/observability payloads, and product
  `LocalProjectVerifier` evidence (TestRecord output, subprocess-derived
  diagnostics, verifier-owned Git error text) — before any Agentic Debugger
  journal/model/evidence exposure, by ONE per-session `ProjectSecretRedactor`
  derived from the same `ProjectRuntimeMaterialization` as the role child
  environments (no re-resolution from `os.environ`, no second secret
  authority, non-serializable, values never repr'd). This is the
  application-owned raw-value boundary, not a hostile-project DLP system:
  a trusted project that deliberately transforms, encodes, hashes, splits,
  or writes a secret into unrelated files is not detected, and no such
  claim is made.
- **Materialization timing**: worker snapshot (or source fallback snapshot)
  → fixed materialization → role derivation; every role derives from the
  fixed mapping; post-start parent mutation is invisible (tested).
- **Bridge retirement**: normal newly launched Local Project sessions use
  `ExecutionEnvironment.for_local_project` (platform essentials allowlist +
  fixed materialization; `uses_legacy_bridge is False`). The
  `legacy-project-ambient/v1` constructor/`snapshot_process` path remains
  ONLY for test-only compatibility and legacy direct-API callers
  (V2-01 unit tests over it stay green); there is no full-environment
  escape hatch in the UI or session API. Platform essentials were
  determined from implementation evidence (`PATH`, Windows startup dirs,
  temp/home/profile dirs incl. `APPDATA`/`LOCALAPPDATA` for user
  site-packages resolution, locale).
- **Executor seam** (`application/executor.py::ProductExecutor`):
  in-process façade with exactly the adopted operations
  (`run_project_command`, `open_product_pdb`, plus fixed
  `verifier_environment`/`cleanup_environment` role mappings); existing
  `CommandRunner`/`PdbSession` underneath; no process/RPC/queue. Patch and
  verifier execution stay in their modules (compatibility delegation) but
  are capability-gated at their call sites.
- **Verifier parity**: the verifier receives the same VERIFIER-role fixed
  mapping (declared project inputs, no control/provider secrets) through
  its existing factory seam; construction independence, workspaces,
  taxonomy, and cleanup proof unchanged. Terminal cleanup runs under the
  new least-authority CLEANUP role (essentials only — no project
  application variables or secrets). Verifier-owned Git children keep the
  single fixed verifier environment (documented remaining gap for a later
  bounded refinement). Repair 10 adds the session redaction authority
  alongside that fixed environment (fail-closed ownership, never merged
  with a custom runner factory): verifier command evidence and Git error
  text are redacted before entering review-facing structures.
- **Deferred sub-pieces**: journal carries no new session-launch event
  (provenance available via the safe task artifact + launch fingerprint);
  explicit non-secret VALUES are API-level only (the UI exposes
  names-only); no ModelGateway/CredentialVault/verifier-isolation work.

## 17. V2-02 repair 09 note (status only — decision unchanged)

A narrow post-review repair made the 08 contracts genuinely singular and
Windows-correct, with no direction change:

- **One session-start authority**: `AgentDefinition` owns the requested
  controller policy/provider/model identity (the former duplicate
  `SessionLaunch` fields are now read-only views, so contradiction is
  unrepresentable). The source rebinds policy/provider/model/profile from
  the launch after resolving it; legacy scenario params remain the worker
  transport/compatibility input used to BUILD the launch, and a supplied
  launch is additionally corroborated against them
  (`check_launch_matches_params`) — corroboration-only, fail-closed on
  mismatch before any project/model execution. Source-specific facts
  (paths, bug text, repro/verify commands, config root, legacy Ollama
  markers) stay on the validated params.
- **Platform-aware environment-name identity**: one helper authority
  (`canonical_env_name`, Windows case-insensitive / POSIX case-sensitive,
  explicit-platform testable). Declaration duplicates, platform-essential
  collisions (now rejected at the spec ingress, including `Path`-style
  Windows variants), snapshot lookup, and essentials derivation all use
  it; a Windows snapshot with conflicting case variants fails closed
  name-only. Original spellings are preserved for provenance/UI. Durable
  declarations stay names-only/platform-neutral; the worker's platform is
  canonical at materialization. Secret semantics unchanged (names only in
  the spec; values never normalized, serialized, or inspected).

## 18. V2-02 repair 10 note (status only — decision unchanged)

A narrow post-review repair sealed the one missing V2-02 boundary —
project-secret EGRESS — with no direction change:

- **One redaction authority per session**
  (`application/execution_environment.py::ProjectSecretRedactor`): derived
  ONCE by `ExecutionEnvironment.for_local_project` from the SAME
  `ProjectRuntimeMaterialization` that supplies the role child environments.
  Redaction values are the materialized values of exactly the declarations
  whose provenance kind is `secret` (no shape guessing, no re-resolution
  from `os.environ`, no second secret authority, no global registry).
  Replacement is deterministic and bounded (`<PROJECT_SECRET:NAME>`
  markers, non-empty exact values, longest first, single-pass so inserted
  markers are never rescanned); empty secret values are excluded
  explicitly. The redactor is non-serializable (no value API, pickle/copy
  fail closed, count-only repr) and never enters params, journals, child
  environments, or provider transport. The legacy bridge path exposes no
  redaction authority and keeps its historical behavior.
- **Boundaries sealed**: `ProductExecutor.run_project_command` redacts
  child stdout/stderr before the `CommandResult` crosses back into the
  Local Project/control plane (exit code, timeout state, argv, cwd, and
  truncation flags untouched; generic `CommandRunner` unchanged); the
  product PDB tool handlers sanitize each response ONCE (same sanitized
  object for the observability event and the model payload — locals,
  safe-eval, stack, execution control, start location, and PDB exception
  diagnostics) without modifying the raw `PdbSession` protocol or PDB's
  project-secret access; the PRODUCT `LocalProjectVerifier` path takes the
  same authority alongside its fixed environment (fail-closed ownership:
  never supplied without the environment, never merged with a custom
  runner factory) and redacts TestRecord stdout/stderr,
  subprocess-derived diagnostics, and verifier-owned Git error text before
  they enter review-facing structures, with classification and exit codes
  untouched. Provider/model transport is unchanged: redaction happens on
  project-domain OUTPUT before the model sees a result; no secret
  redaction material enters provider argv/env/prompts/config.
- **FirstMate regression closed**: the reproduced leak (a declared project
  secret echoed by a normal project command returning raw
  `CommandResult.stdout`, then flowing into the durable journal via the
  initial-reproduction diagnosis and into the model channel via the
  `run_reproduction` `failure_output`) is now a redaction-marker
  regression suite (`tests/unit/test_project_secret_redaction.py`).

## 19. V2-02 repair 11 note (status only — decision unchanged)

A narrow post-review repair closed the one remaining application-owned
egress gap inside the repair-10 seal: BOUNDING/TRUNCATION BEFORE REDACTION.
Several lower layers transform project output before the one
`ProjectSecretRedactor` sees it; when such a cut goes through the middle of
a secret, the complete value no longer exists in the text and exact-value
replacement cannot match — the leaking fragment would be manufactured by
Agentic Debugger itself.  This is distinct from the documented non-goal
(a project deliberately transforming its own secret remains outside the
DLP contract).  Implementation status:

- **Repair 10** established raw-value product egress redaction (complete
  exact values at the executor/PDB/verifier boundaries).
- **Repair 11** closes application-owned pre-redaction
  bounding/truncation fragment exposure, on the SAME one per-session
  authority (no second resolver, no value API, no serializable state):
  - `CommandRunner` gains an OPTIONAL neutral output-sanitization seam
    (`output_sanitizer_factory`, duck-typed `feed`/`flush`, one instance
    per stream, no application-layer import, refused together with a
    `VerifiedExecutionContext`, byte-identical behavior when absent).
    `ProductExecutor` and the product `LocalProjectVerifier` supply the
    session redactor through it, so complete secret values are removed
    from the stream text BEFORE the head/tail bound can cut a fragment;
    truncation flags truthfully describe the produced (sanitized) stream.
  - The redactor understands explicitly application-marked bounded
    representations: PDB worker `kind:"str"`/`truncated:true` preview
    structures (fragment replaced, `kind`/`type`/`size`/`truncated`
    metadata untouched, recursion covers nested collections) and
    marker-terminated bounded diagnostic texts (the PDB worker's `…`).
    Unmarked text is never boundary-scanned.
  - Ordering repairs: the verifier Git diagnostic now REDACTS the
    complete decoded child text before the public 1000-character bound;
    product PDB/tool exception diagnostics and `tool_errors` redact the
    full exception text through the session authority before the
    400-character diagnostic bound (`safe_project_diagnostic`).
- Regression suite: `tests/unit/test_bounded_secret_fragment_redaction.py`
  (FirstMate head/tail reproductions, verifier command/Git cases, PDB
  long-local/safe-eval/nested previews, exception diagnostics, and
  byte/behavior compatibility for ordinary and no-secret sessions).

## 20. V2-02 repair 12 note (status only — decision unchanged)

A targeted post-review repair resolved two defects inside the accepted
project-secret redaction authority: marker self/cross collision leaks and
streaming overlap chunk-boundary sensitivity.

- **Replacement marker safety (Finding A)**:
  - Every replacement marker emitted by `ProjectSecretRedactor` is proven
    disjoint from all materialized session secret values at construction
    time (`S not in marker` for every non-empty session secret S).
  - A readable name-bearing marker is used when proven safe; if collisions
    occur, deterministic safe generic candidates are selected; as a final
    fail-safe, the empty string removes the value entirely rather than
    emit a marker that contains raw secret material.
  - Secret names remain safe durable metadata; marker selection state
    remains non-serializable, non-logged, non-journaled, and deterministic.
- **Streaming chunk-boundary invariance (Finding B)**:
  - The streaming sanitizer undecided state holds raw stream text (at
    most `longest secret length - 1` characters) rather than post-redaction
    text.
  - Redaction decisions are chunk-boundary invariant: for any complete
    decoded text and any segmentation into read chunks, streaming output
    is semantically identical to canonical single-pass full-input
    redaction.
  - Longest-first overlap decisions and equal-value tiebreak semantics are
    preserved regardless of read chunk sizes; no raw secret fragment can
    be manufactured across chunk boundaries.
- Application-created secret fragments remain sealed; deliberate project
  transformations remain outside the DLP claim.
- Regression suite: `tests/unit/test_marker_collision_and_stream_overlap.py`
  (FirstMate reproductions A1-A5, B1-B8, bounded text / string preview marker
  safety, and real `ProductExecutor` overlapping secret streaming regression).

## 21. V2-03 implementation note (status only — decision unchanged)

V2-03 implements the §11 third slice: the product `ModelGateway` facade,
`ModelBinding` runtime provenance, truthful multi-dimensional provider status
semantics, and user-facing vocabulary repair:

- **ModelGateway & ModelBinding authority** (`application/model_gateway.py`):
  logical provider/model requests resolve into an immutable, frozen
  `ModelBinding` holding only safe runtime facts (provider ID, model ID, API
  model ID, effective protocol, endpoint contract / transport profile, route,
  safe endpoint identity, auth mode, tool version, protocol version, and safe
  config fingerprint). It never carries credentials or secret material and
  enforces fail-closed credential scrubbing (`contains_credential_shape`).
  Transport creation (`create_transport`), static preflight (`static_preflight`),
  explicit reachability probes (`probe_reachability`), and catalog refreshes
  (`refresh_catalog`) are owned by `ModelGateway`.
- **Truthful Status Semantics** (ADR 0001 §9, V2 Plan §9):
  credential presence or static configuration is NEVER labeled "Connected".
  The six distinguished factual dimensions (`Configured`, `Credential ready`,
  `Model runnable / Ready`, `Catalog refreshed at T`, `Live verified at T`,
  `Runtime succeeded at T`) are tracked independently. `Runtime succeeded at T`
  is observational history derived from durable session journal events, never
  mutating provider configuration. The owner-observed CommandCode GOAT defect
  (loopback offline displayed as Connected) is closed.
- **User-Facing Vocabulary Repair**:
  primary "Transport Profile" labels are replaced with "Endpoint contract";
  "(historical)" markers are removed from display labels; provider dialog preset
  buttons are modernized to "Generic / OpenAI-compatible", "CommandCode",
  "OpenCode", and "Ollama".
- **Catalog Cache Invalidation**:
  `update_provider_config` automatically purges cached catalog state and resets
  `last_refresh_utc`/`models` when endpoint URL, auth mode, or transport profile
  is mutated.
- **Regression suites**: `tests/unit/test_model_gateway.py`,
  `tests/unit/test_provider_status_semantics.py`, and
  `tests/unit/test_ui_provider_vocabulary.py`.

### 21.1 Candidate 14 post-review repair (authoritative bindings and status facts)

Following independent source-level architecture and runtime review by
FirstMate, Candidate 14 repairs five critical runtime authority boundaries:

1. **Binding Stability and Corroboration**: `ModelGateway.create_transport()`
   corroborates `ModelBinding` against the authoritative current state before
   instantiating child transports. For direct API routes, it validates that
   `base_url`, `auth_mode`, `endpoint_contract`, `api_format`, and config
   fingerprint have not drifted, and that the provider is enabled and not
   quarantined. For profile routes, it corroborates `configuration_fingerprint`
   and `tool_version` against `CommandModelConfigStore`. Any drift fails closed
   with `StaleModelBindingError` before any model call is dispatched.
2. **Fail-Closed Resolution & Compatibility**: `ModelGateway.resolve()` removes
   broad `except Exception:` fallbacks. Unconfigured providers fail closed with
   `ProviderConfigurationError`. Model and protocol compatibility are validated
   against the authoritative endpoint contract, raising `IncompatibleModelError`
   on mismatch. Missing credentials on configured providers yield safe static
   bindings that fail preflight cleanly without fabricating credentials.
3. **Runtime Identity Binding for Probes & Observational History**: Live probe
   records are tied to a deterministic `provider_runtime_identity(cfg)` hash
   (provider ID, base URL, endpoint contract, auth mode, API format). Mutating
   provider configuration invalidates live verification immediately without
   manual cache flushes. Session journal inspection (`inspect_last_runtime_success`)
   verifies that `model.configured` recorded an `endpoint` identical to the
   provider's current `base_url`, preventing stale or unbound history from
   falsely certifying new endpoints.
4. **Provider Manager UI Authority**: `ModelProvidersScreen` consumes the public
   `ModelGateway.list_provider_statuses(history_root=...)` interface as the single
   source of truth, eliminating out-of-band object assembly and private attribute
   access. Solid dot `●` is driven exclusively by `live_verified`, NOT historical
   `runtime_succeeded_at_utc`. Provider CRUD operations invalidate gateway probe
   state via `gateway.invalidate_provider()`.
5. **Qualified Ollama vs Direct API Route Distinction**: Qualified Ollama Cloud
   ladder models use explicit `ROUTE_QUALIFIED_LADDER`, preventing shadowing or
   misdirection to configured Ollama instances running under `ROUTE_DIRECT_API`.
6. **Truthful Headline Semantics**: Providers with `auth_mode="none"` report
   `Configured · loopback` only when the endpoint hostname is loopback (`127.0.0.1`,
   `localhost`, `::1`); non-loopback endpoints report `Configured · no auth`.
   Quarantined providers retain `is_configured=True` while reporting
   `Quarantined · recovery required`.

### 21.2 Candidate 15 final authority repair (runtime identity and binding invariants)

Following final FirstMate review, Candidate 15 establishes strict runtime
identity authority, binding invariants, and fail-closed status resolution:

1. **Fail-Closed Command Profile Resolution**: `ModelGateway.resolve()` resolves
   `configured` profile models directly via `CommandModelConfigStore.get()`.
   Missing or unparseable profiles fail closed immediately with
   `ProviderConfigurationError`; successful resolutions capture the store's
   authoritative `configuration_fingerprint` and `tool_version` without
   speculative or unbound fallback.
2. **Narrow Registry Fallback**: `resolve()` narrows `ProviderRegistryError`
   fallback exclusively to missing credentials or quarantined recovery states,
   producing safe static bindings that report `needs_auth` at preflight. All
   structural failures, unknown models, or contract incompatibilities fail
   closed as `ProviderConfigurationError`.
3. **Strict ModelBinding Semantic Invariants**: Constructor and
   `from_mapping()` enforce structural invariants: `ROUTE_CONFIGURED_PROFILE`
   requires provider ID `configured` (or None); `ROUTE_QUALIFIED_LADDER`
   requires provider ID `ollama`; `ROUTE_DIRECT_API` requires a concrete
   configured provider ID, endpoint URL, and authentication mode.
   `create_transport()` dispatches authoritatively by route and corroborates
   these invariants before spawning any transport.
4. **Binding Preflight Corroboration**: `static_preflight(ModelBinding)`
   validates the binding against current durable configuration, failing closed
   with descriptive blockers upon configuration drift (endpoint, contract,
   auth mode, protocol, fingerprint, or tool version).
5. **Complete Runtime-Identity History Corroboration**:
   `inspect_last_runtime_success()` corroborates the full tuple: `endpoint`,
   `auth_mode`, `endpoint_contract`, `api_format`, and
   `provider_runtime_identity`. Historical journals lacking complete provenance
   remain unbound and cannot certify modified endpoints.
6. **Truthful Headline Priority**: `ProviderStatusSnapshot.summary_headline`
   strictly prioritizes current operational readiness: Disabled → Quarantined →
   Degraded → Live verified → Configured / Credential ready. Historical runtime
   success never masks current credential deficits or configuration errors.
7. **Provider Manager Dual Timestamps**: Detail pane renders separate, honest
   lines for `Live verified <time> UTC` and `Runtime success <time> UTC`.
8. **Resilient Status Enumeration**: `list_provider_statuses()` catches
   per-provider evaluation exceptions and yields degraded `ProviderStatusSnapshot`
   records (`is_provider_ready=False`), ensuring configured providers never
   silently disappear from management views.
9. **Zero Category C Production Repair Calls**: Production repair runtime
   calls directly to provider-core are strictly eliminated. Parameter validation
   in `local_project_source.py` routes through `ModelGateway.is_known_provider()`.
   The complete production call inventory is:
   - **Category A (ModelGateway Façade Internals)**: Authoritative delegation to
     `provider_connections.py` and `model_providers.py`.
   - **Category B (UI Provider Manager CRUD)**: Configuration store persistence
     in `screens.py` and `session_config.py`.
   - **Category C (Production Repair Runtime)**: **STRICTLY ZERO (0)** direct
     calls; all access flows through `ModelGateway`.
   - **Category D (Cross-Cutting & Legacy Runners)**: Safety name classification
     in `execution_environment.py` / `session_runtime.py` and legacy ladder
     evaluation runners in `configured_source.py`.

### 21.3 Candidate 16 final bounded authority repair (provider routing and runtime identity)

Following independent FirstMate review, Candidate 16 establishes strict provider
routing authority, structured error classification, distinct runtime identity,
and truthful status hierarchy:

1. **Legacy-CLI Fallback Preservation for Historical Profiles**:
   `ModelGateway` delegates route authority to accepted provider core
   (`model_providers.resolve_provider_live_config`), preserving `ROUTE_LEGACY_CLI`
   when direct protocol is unresolved or credentials are absent for historical
   profiles (`TRANSPORT_OPENCODE_GO`, `TRANSPORT_COMMANDCODE_GOAT`). Generic
   providers (`TRANSPORT_GENERIC`) are isolated and never fall back to legacy CLI.
   `static_preflight` truthfully reports `route=ROUTE_LEGACY_CLI` and
   `is_runnable=True` when the legacy CLI route is ready.
2. **Structured Provider Error Handling**:
   Parsing of English exception strings (`str(exc)`) is completely eliminated.
   Provider readiness and route selection evaluate structured provider facts
   (`ProviderConfig`, `credential_source_for`, `_legacy_for_config`,
   `is_provider_quarantined`). Unexpected registry failures fail closed as
   `ProviderConfigurationError`.
3. **Strict ModelBinding Route Invariants**:
   `ROUTE_DIRECT_API` requires an explicit non-configured provider identity,
   non-empty endpoint URL, `auth_mode in AUTH_MODES`, supported `effective_protocol`
   (`chat_completions`, `messages`, `responses`), and recognized `endpoint_contract`.
   `ROUTE_LEGACY_CLI` requires an explicit provider identity, non-empty model ID,
   and historical contract profile (`TRANSPORT_OPENCODE_GO`, `TRANSPORT_COMMANDCODE_GOAT`),
   strictly rejecting `TRANSPORT_GENERIC`. These invariants are enforced both in
   `ModelBinding.__post_init__` and `ModelBinding.from_mapping()`.
4. **Distinct Provider Runtime Identity vs ModelBinding Authority**:
   `provider_runtime_identity(cfg)` captures the live provider endpoint, contract,
   auth mode, and default protocol identity. `ModelBinding` captures full session
   authority (provider runtime identity + model name + parameters + protocol variant
   + configuration fingerprint). Model-specific protocol choices (e.g. Claude
   `messages` on CommandCode default `chat_completions`) do not invalidate
   provider-level runtime success in `inspect_last_runtime_success()`. When
   `target_binding` is supplied, `model_binding_fingerprint` is strictly matched.
5. **Fail-Closed Per-Event Journal Scanning**:
   In `inspect_last_runtime_success()`, `session_matches_provider = False` is reset
   at the start of evaluating every `model.configured` event sequentially. Each
   configuration event defines the active provider for subsequent requests until
   the next configuration event, preventing cross-provider success attribution
   leakage in multi-configuration session journals.
6. **Truthful Status Priority Over Historical Probes**:
   Current operational deficits (missing credentials, unready, quarantined, disabled)
   strictly supersede past `Live verified` probes in `summary_headline`. Solid dot `●`
   in UI screens (`screens.py`) requires current readiness (`st.connected`), rendering
   a hollow dot `○` when credentials are lost while preserving the historical
   timestamp `Live verified <time> UTC` in detail views.

