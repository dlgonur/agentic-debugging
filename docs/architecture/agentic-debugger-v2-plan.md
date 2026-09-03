# Agentic Debugger V2 — Control/Execution Plane Separation Architecture Plan

**Document type:** Architecture analysis and migration plan (decision record)
**Status:** Plan — no implementation has occurred; owner/FirstMate review 02 applied (see lineage)
**Lineage:** `01` `3481b58` defined V2 boundaries (Alternative B accepted in direction). `02` (this revision) tightened the execution and trust boundaries: security-first migration ordering, role-scoped execution environments, deferred verifier process isolation, credential binding/materialization separation, truthful provider status semantics, and resolved owner decisions.
**Baseline:** `4606933` (fix(providers): harden provider runtime and Windows harness), clean tree
**Scope:** Determine whether the application runtime should adopt an explicit CONTROL / EXECUTION plane separation, and define the smallest coherent target architecture and incremental migration path
**Companion decision record:** `docs/adr/0001-control-execution-plane-separation.md` (Accepted — records an accepted target/migration decision, not completed implementation)

---

## 1. Executive recommendation

**Yes — a V2 architecture change is warranted: Alternative B — explicit logical planes and first-class authority contracts in the current principal process topology. No general control-plane/executor process split (Alternative C, rejected).**

The repository's recent boundary incidents (credential authority divergence, TLS/proxy environment loss across the worker/adapter hop, OpenCode auth-store visibility, Windows venv PID indirection, UI treating configuration presence as readiness) are not symptoms of a missing process. The pipeline already has three process tiers and per-tool child processes; each incident was repaired *inside the existing topology* by adding an explicit contract. That history is direct evidence that the architecture's weak point is **undeclared, distributed execution-environment state**, not process count.

The single most important V2 primitive is therefore **`ExecutionEnvironment`** — one typed, versioned declarative authority for process/runtime policy (interpreter identity, workspace policy, environment allowlist *rules*, network/trust policy, limits), from which **role-scoped least-authority child environments** are derived per child role (model adapter, project command, PDB worker, verifier command, legacy CLI). Centralize the rules, not one shared environment blob. Today the same facts are re-derived at six construction sites (§3.3), and each observed incident is traceable to one site disagreeing with another.

**The first implementation slice must close the active security defect**, not introduce typing: `runtime/command_runner.py::_build_env()` copies the full worker environment — including the forwarded private provider credential variable — into user reproduction/verification and verifier command children (§3.2). This is a currently evidenced execution-boundary exposure, not architecture debt. V2-01 therefore establishes the minimum execution-environment/child-environment authority needed to make provider credentials structurally unavailable to project execution children, with regression tests proving secret exclusion. No speculative type scaffolding precedes it.

**The independent verifier remains the sole correctness authority and keeps its existing substantive independence** (source-commit binding, clean-source export, separate disposable workspaces, independent patch evaluation and outcome taxonomy — all verified in source). `VerifierService` becomes a first-class *logical* boundary; **physical subprocess isolation is deferred** as a separately earned hardening step with explicit triggers (§5.7). An OS process boundary improves crash/secret/lifecycle isolation but does not by itself create epistemic correctness independence, and the repository's verifier invariant does not mandate a process boundary. The V2-01 role-scoped environment work already removes the credential exposure that was the one concrete security argument for moving it.

Everything else — deterministic controller, typed tools, bounded PDB protocol, disposable workspaces, journal authority, exact provenance, verifier outcome taxonomy — stays structurally untouched. Complexity budget: five stages, each independently shippable, each earning its cost at the time it lands; V2-05 is explicitly optional and may resolve to NOT JUSTIFIED.

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

**Active execution-boundary exposure (found during this investigation; the reason V2-01 is security-first):** the forwarded credential variable lives in the worker's `os.environ`; `CommandRunner._build_env()` copies the full environment into every user reproduction/verification command and every verifier command child (`command_runner.py:293-296`, `local_project_source.py:163-203`, `evaluation/local_project_verifier.py:54`). Any user-supplied repro/verify script — or anything it invokes — can read the provider API key. The transport allowlist discipline carefully built for the *model* boundary does not yet exist on the *execution* boundary.

### 3.3 The six undeclared execution-environment construction sites

| Site | Env passed to children | Interpreter identity | Network/trust |
|---|---|---|---|
| `SessionWorkerProcess._worker_argv` + `build_worker_env` | full inherit + 1 credential var | `resolve_worker_executable` (venv-aware) | inherited |
| `CancellableJsonlCommandTransport.subprocess_environment` | minimal + config paths + allowlist | `sys.executable` | explicit allowlist |
| `PdbSession._worker_env` | `build_worker_env(None)` (inherit or venv fixup) | `resolve_worker_executable` | inherited |
| `CommandRunner._build_env` (user commands + verifier) | **full inherit + PYTHONIOENCODING** | caller's argv (`python`/`python3` resolved by PATH) | inherited |
| `JsonlCommandTransport.subprocess_environment` (scientific/evaluation path) | minimal (`PATH`, `PYTHONIOENCODING`, `SystemRoot`) | n/a | minimal |
| Legacy CLI routes (`opencode_provider_adapter` etc.) | adapter-owned | n/a | adapter-owned |

Every row is individually justified; the problem is that no single typed object declares the intended environment policy for a session, so each new child type re-decides inheritance, allowlists, and interpreter selection — and the incident history shows they drift.

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

Legend for TARGET V2 home: **CP** = control plane (orchestration, session, gateway; stays in UI process + worker shell), **EP** = execution plane (workspace/tools/debugger/tests/patch; worker process body + child processes), **GW** = provider gateway seam, **IND** = must remain independent of the CP execution path (logical VerifierService; physical isolation deferred).

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
| `scripts/provider_direct_api_adapter.py` + CLI adapters + `protocol_prompt_shaper.py` | Protocol-1.3 JSONL adapter children (direct API / legacy CLI), spawned per request | adapter child process | provider_connections (credential resolve) | GW child (unchanged contract) | Keep; consume vault-issued material via the model-adapter role environment (V2-04), stop re-resolving ambient state | Medium (credential contract change) |
| `agent/controller.py`, `controller_policy.py`, `state_machine.py`, `trajectory.py`, `observer.py`, `proof_gate.py` | Deterministic controller: state machine, budgets, directives, tool dispatch, steps | worker process | tool_registry, model adapter (duck-typed) | CP (the brain) | **Keep untouched** — it is already plane-clean: model via adapter interface, tools via registry, cancellation via injected check | None |
| `agent/tool_registry.py` + `skills/` | Typed tool contracts + source inspection skills | worker process | runtime modules | CP↔EP contract (the registry *is* the command vocabulary) | Keep; handlers become Executor-side (V2-02) | Low |
| `runtime/workspace.py`, `patcher.py`, `command_runner.py`, `test_runner.py`, `execution.py`, `exceptions.py` | Disposable workspaces, unified-diff patch policy/apply/revert, bounded command/test execution | worker process (children) | — | **EP** | Keep; `CommandRunner` consumes role-scoped ExecutionEnvironment derivation (V2-01 — closes the §3.2 exposure) | Medium (env behavior change must be tested against real repro scripts) |
| `runtime/pdb_session.py` (3,009) + `pdb_worker.py` (3,853) + `pdb_protocol.py` | Bounded PDB protocol, session lifecycle, worker with safe-eval/locals bounds | worker → PDB child | python_launcher, workspace | EP (already a model of the target pattern: typed protocol, PID identity handshake, bounded vocabulary, per-session disposability) | Keep; PDB role environment declared in V2-01 (credential-free) | Low |
| `runtime/python_launcher.py` | Windows venv interpreter/PID identity authority | shared by all spawners | — | EP platform seam (subsumed into the ExecutionEnvironment interpreter policy) | Keep as the single interpreter authority; V2-01 declares it, does not duplicate it | Low |
| `application/journal.py`, `emitter.py`, `events.py`, `observability.py`, `source_snapshots.py` | Durable append-only evidence + typed events + observability producers | worker process (single writer) | — | CP (session evidence authority) | **Keep untouched** | None |
| `application/history.py`, `replay.py`, `reporting.py`, `presentation.py`, `workstream.py` | Manifests, discovery, read-only replay, pure presentation projections | UI process | journal, events | CP (view) | Keep; presentation stays forbidden from evidence creation (already enforced) | None |
| `evaluation/local_project_verifier.py`, `evaluation/verifier.py`, `runner.py`, `outcome_taxonomy.py`, verifier observers | Independent verification: clean-baseline reproduction, patch re-apply, F2P/P2P, cleanup proof | **worker process (in-process)** | command_runner, patcher, workspace | **IND** — `VerifierService` logical boundary (V2-02 seam); **physical subprocess isolation deferred** (§5.7, V2-05 optional) | Keep logic untouched; add interface + verifier role environment (credential-free, V2-01); process extraction only if triggers fire | Low-Medium now (deferral removes the process-move risk) |
| `application/local_project.py` (1,144) | Project validation, isolated git worktree lifecycle, task-spec contract, containment | UI process (prepare) + worker (verify) + supervisor (post-mortem) | git CLI | EP (workspace lifecycle) | Keep; already correctly cross-process cooperative | Low |
| `application/level32.py`, `ollama_cloud_source.py` (ladder) | Scientific capability-ladder operator (qualification-bound) | worker process | qualified roster, adapters | CP but **scientific boundary fenced** (§8) | Keep; ladder qualification never derives from provider availability (already true — `is_treatment_eligible`) | None (no change) |
| `quixbugs/`, `bugsinpy/` | Pinned dataset adapters, contained PDB, license-gated WSL prep | operator/evaluation processes, not product runtime | runtime, datasets | EP (offline) / frozen research | Keep; out of product runtime scope | None |
| `ui/app.py`, `screens.py`, `widgets.py`, `models.py`, `session_config.py` | Textual UI: home, provider manager, session setup, workspace, history | UI process | application layer | CP (presentation) | Keep; V2-03 only *narrows* what it imports (gateway façade, no transport vocabulary, truthful status facts) | Low-medium |
| `demo/` (catalog, tools, runner, policies, model…) | Offline deterministic tasks + tool context/registry builder shared with live sources | worker process (deterministic + live sources import demo.tools) | runtime | EP; `demo.tools.build_registry` is shared tool-vocabulary authority | Keep (defer consolidation; shared import is a wart, not a risk) | None |

Deliberately omitted: `comparison/`, `rag/`, `preference/`, `events/` (research subsystems), `datasets/`, `experiments/`, frozen research paths — unaffected by plane separation.

---

## 5. Alternatives evaluated

### 5.1 Alternative A — keep process architecture, harden contracts

Evolve nothing structurally; continue the incident-response pattern (add explicit contract at each divergence site).

- **Correctness:** achievable; the six incidents were each fixed this way and hold under regression tests.
- **Prevents the observed classes?** Only until the next construction site appears. §3.3 shows six environment sites and §3.2 shows an *active* credential exposure that contract-hardening would likely rediscover site-by-site rather than close class-wide.
- **Verifier independence:** stays as construction discipline (in-process verifier with substantive logical independence).
- **Latency / complexity / Windows:** optimal (nothing changes); debuggability unchanged.
- **Cost/risk:** lowest.
- **Verdict: insufficient.** It leaves the class-generator (undeclared environment policy, triple credential resolution) in place. Reasonable as a fallback if V2 stalls, but it is the strategy that *produced* the incident list.

### 5.2 Alternative B — explicit logical planes in the current principal process topology (recommended)

First-class contracts — `Session`, `AgentDefinition`, `ExecutionEnvironment` (with role-scoped child derivation), `ModelGateway`, `CredentialVault` (binding vs materialization), `Executor` (interface), `VerifierService` (logical boundary) — **with no new long-lived processes and no committed process moves.** The session worker remains the single orchestration process; brain and hands stay in it but behind typed, testable seams that make the boundary enforceable. Selective future physical isolation (e.g. the verifier) stays *cheap to evaluate later* precisely because the interfaces exist — but nothing in V2 requires it.

- **Correctness:** every observed incident class is closed *at its generator*: one `ExecutionEnvironment` policy makes worker/adapter/PDB/user-command/verifier env derivations consumers of one authority with role-scoped least-authority profiles (kills the env-divergence class); one vault-issued credential binding replaces triple re-resolution (kills authority-divergence); one interpreter identity authority is already done (`python_launcher`) and gets subsumed; gateway-owned truthful status facts kill config-as-readiness (validated by the post-plan incident).
- **The §3.2 exposure becomes structurally impossible** for new construction: `CommandRunner` consumes a role-scoped derivation in which credential variables are fail-closed rejected, so the forwarded provider credential can never reach user code.
- **Isolation/recoverability:** unchanged process containment (job object, PID identity, cleanup verification all stay); typed seams make restart/recovery *possible to add later* without redesign.
- **Latency:** zero added IPC on any hot path (model/PDB/tools/verifier all unchanged in-process).
- **Process complexity:** none added in V2-01…V2-04; V2-05 is an optional, trigger-gated evaluation.
- **Windows behavior:** all new seams reuse existing Windows authorities (job object, python_launcher, taskkill ladder); no new Win32 surface.
- **Debugging complexity:** improves — one place to inspect "what environment policy did this session declare, and which role profile did this child consume".
- **Credential exposure:** strictly reduced (single vault resolution; execution roles structurally exclude credential variables; proxy values — which may themselves embed credentials (`provider_connections.py:642`) — no longer flow to arbitrary project code merely because the model adapter needs them).
- **Reproducibility:** improves — the safe declarative execution contract is fingerprinted into session provenance (§6.6), so replay can assert the policy identity that produced the evidence.
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

Revision 01 of this plan committed the verifier to a subprocess as V2-03, arguing that verifier independence is a mandated invariant. That conclusion was stronger than repository evidence supports. The repository invariant is *epistemic*: **the independent verifier is the correctness authority — controller completion, patch application, provider success, and model confidence are not proof.** `LocalProjectVerifier` already gains substantive independence by construction (all verified in source, `evaluation/local_project_verifier.py:304-470`): binding to the source commit, rejecting dirty/mismatched canonical source, exporting clean source, evaluating the exact candidate patch independently of controller claims, four separate disposable workspaces, independently running baseline reproduction/regression, patch application, syntax, post-patch reproduction/regression, classification, and cleanup/source-integrity proof. An OS process boundary may improve crash/secret/lifecycle isolation, but it does not create epistemic independence — and V2-01's role-scoped environments already remove the one concrete secret-exposure argument. Physical isolation is therefore deferred behind explicit triggers (§5.7). This also makes the architecture consistent with its own principle: complexity must earn its cost.

### 5.7 Deferred-verifier-isolation triggers (evaluate before any V2-05 work)

Promote the verifier to a subprocess only if one of these is evidenced:

1. **Lifecycle:** verifier crash/hang materially threatens the worker lifecycle (today a verifier hang is bounded by plan timeouts and produces an honest verifier failure — if field evidence shows it instead wedging or crashing workers, isolation is earned).
2. **Environment:** verifier dependency/environment isolation cannot be guaranteed in-process (e.g. verification requires interpreter/library isolation from the worker's own imports).
3. **Security:** a concrete security boundary requires it (e.g. verification of untrusted code that must not share address space with credential-bearing state — note V2-01 already removes credentials from the verifier role, so this trigger is about *untrusted code*, not secrets).
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
  ModelGateway (provider selection, route/protocol/credential binding,
                truthful status facts)
  CredentialVault (CredentialBinding decisions + material issuance;
                  the ONLY secret reader)
  History / replay / presentation projections

EXECUTION PLANE ("hands") — behind typed Executor interface, in worker body + children
  Disposable workspaces (TaskWorkspace, isolated git worktree)
  CommandRunner / TestRunner (bounded; role-scoped child environments)
  PdbSession → PDB worker child (unchanged protocol; credential-free env)
  PatchManager (unified diff, allowed-path policy)
  Process cleanup verification

INDEPENDENT VERIFIER (logical VerifierService; in-process in V2)
  LocalProjectVerifier / EvaluationVerifier behind a first-class interface
  Own disposable workspaces; own CommandRunner with the verifier role
  environment (credential-free); sole correctness authority
  Physical subprocess isolation: deferred behind §5.7 triggers
```

The worker process remains one process in normal operation — but its internal structure now has a hard seam (the tool/registry boundary the controller already uses), a declared environment policy with role-scoped derivations, and a gateway-owned model channel, so "control" and "execution" are distinguishable, testable, and — only if a §5.7 trigger fires — cheap to separate physically.

### 6.2 The primitives — what becomes first-class and what must NOT

**`Session`** (evolve existing `SessionSpec`/`SessionCoordinator`): the durable identity + authority object.
- Contents: `session_id`, `task_id`, `AgentDefinition`, `ExecutionEnvironment` (id + contract fingerprint), task binding, budgets, `retry_of`, provenance.
- Authority ordering (explicit, matches current code): **durable journal is the event/evidence authority; `Session` is the lifecycle authority; UI state is a projection.** The manifest remains derived, never authoritative. The worker remains the only journal writer.

**`AgentDefinition`** — versioned controller/policy identity **for product/runtime provenance only**: controller version, prompt profile, model binding (provider+model+gateway route), allowed tools/capability set, budget defaults, PDB policy. Today these facts exist (prompt profiles were made explicit in `77a4b3f`; `ControllerRunConfig`, `DemoPolicy`, tool sets) but are assembled ad hoc per source. Verdict: **first-class, V2-02** (defined after the security-first V2-01). Scientific scope is explicitly fenced in §8.2: it never becomes the qualification authority for frozen scientific treatment identity.

**`ExecutionEnvironment`** — the keystone. **One declarative authority, many derived environments.** A frozen dataclass declaring *policy*, not an environment blob:
- policy/schema version;
- role-scoped child-environment policies (§6.3): which variables each child role receives, by derivation rule (`inherit-platform`, `constant`, `model-channel-only`, `credential-binding-name` — never a value);
- interpreter/runtime identity (base executable, venv marker policy — subsumes `python_launcher`, which remains the single implementation);
- workspace policy + generation identifiers (worker-owned vs verifier-owned roots);
- network/trust policy **per capability**: provider-transport networking (proxy/TLS allowlist, model-adapter role only) is a *separate capability* from project-test networking (project/PDB/verifier roles do not receive proxy variables merely because the adapter needs them);
- filesystem/process containment declarations;
- timeout/resource-policy declarations;
- tool availability set.
- **Must NOT contain:** credential *values* (not even binding values — only binding *names/references*), provider transport internals (route/protocol resolution stays gateway-side), UI state, journal contents.
- **Not one identical environment:** each child role derives its own least-authority environment from the declared rules. Platform essentials (`SystemRoot`, `PATH` on Windows) are modeled separately from provider transport configuration.

**`ModelGateway`** — the narrow interface the rest of the app sees (V2-03):
- `models()` / `status()` returning the truthful facts of §9 (no presence-only "Connected"), `resolve(session, agent_def) → ModelBinding {transport config, provenance payload, credential binding reference}`, `executability(model) → {runnable, blocker}`.
- Internally wraps today's `model_providers` + `provider_connections` + transports + adapters unchanged; the UI stops importing transport vocabulary (the durable `transport_profile` field remains and stays safety-critical).
- Does **not** remove provider transport identity from durable internal configuration (explicit requirement).

**`CredentialVault`** — provider-neutral credential authority with **two separated concepts** (V2-04):

*`CredentialBinding` — non-secret, session-stable authority:*
- provider identity; endpoint/profile binding; chosen credential source/backend identity; safe binding/epoch/fingerprint metadata.
- The binding decision may be fixed at session start for provenance and route consistency. It carries **no secret value** and may be journaled as provenance.

*Credential material / lease — secret-bearing ephemeral materialization:*
- obtained only by the minimal trusted control/model path; injected into the exact model-adapter request child;
- never present in `ExecutionEnvironment` (the environment carries only the binding *name*, and only in the model-channel role);
- never available to project commands, PDB, verifier, journal, argv, evidence, or UI text;
- never independently re-resolved from ambient state by the adapter (the adapter consumes the issued channel).
- **Honest lifecycle wording for the current topology:** the worker may retain the session-authorized secret in bounded private process state — this is what the current code already effectively does, since the transport spawns a fresh adapter child *per request* (`command_transport.py:268`) and re-injects the credential each time. The secret's lifetime is therefore the *session* (or the authorized lease window), not a single child. The architecture states this honestly rather than claiming single-child secret lifetime; what is per-request is the *injection*, not the *materialization*.

- Backends: Windows Credential Manager (current ctypes/advapi32 implementation, first), session memory (current), environment (endpoint-bound, current), CLI auth store (forwarded-as-value, current); future macOS Keychain / Linux Secret Service as new backend *implementations only* — possible, not claimed today.
- Strict endpoint/profile binding and quarantine rules move into the vault verbatim (`provider_connections.py:2281-2365`).
- No cross-machine secret synchronization (explicit non-goal).

**`Executor`** — an **interface only** (V2-02): the typed execution-service contract the tool handlers implement against — `run_command`, `start_pdb`, `apply_patch`, `syntax_check`, `revert`, `run_tests` — each taking the session's `ExecutionEnvironment` and deriving the correct role profile. The existing runtime modules are its implementation. This is the CP/EP boundary made explicit so the controller never touches filesystem/process APIs except through it. (Promotion to a real process is Alternative C, rejected; a *verifier* process is separately deferred per §5.7.)

**`VerifierService`** — the independent correctness authority as a **first-class logical boundary** (V2-02): same verifier code and invariants, behind an interface the execution sources call without any ability to select, parameterize (beyond supplying the typed plan), or short-circuit it; its command children consume the **verifier role environment** (credential-free, V2-01). Clean-source export, four-workspace isolation, outcome taxonomy, and cleanup proof all remain unchanged. Physical subprocess extraction is deferred behind the §5.7 triggers; the seam must keep that extraction easy.

### 6.3 Role-scoped child-environment policy

`ExecutionEnvironment` centralizes the RULES; each child role derives a least-authority environment. Conceptual roles (names illustrative, not prescribed):

| Role | Receives | Never receives | Rationale |
|---|---|---|---|
| **Model-adapter child** (per request) | minimal platform set; config/catalog/quarantine path vars; provider TLS/proxy allowlist (these may reach the provider HTTP path — proxy values may themselves embed credentials, `provider_connections.py:642`, so they are confined to this role); the vault-issued credential channel | workspace paths; user/project variables; anything from the project roles | The one role authorized to carry provider trust material |
| **Project repro/test command** (user commands) | platform essentials (`SystemRoot`, `PATH`, `TEMP`…); explicitly opted-in project variables; `PYTHONIOENCODING` | provider credential variables (fail-closed rejection of known credential names); provider proxy/TLS variables not explicitly opted in | Untrusted-by-default project code; the §3.2 exposure closes here |
| **PDB worker** | platform essentials; interpreter/venv identity per `python_launcher`; workspace-scoped process identity | provider credential variables; provider transport configuration | Debugger executes project code — same trust class as project commands |
| **Verifier command** | platform essentials; verifier workspace identity | provider credential variables; provider transport configuration; session model-channel state | Verification must not depend on (or leak) provider state |
| **Legacy CLI adapter** (where the historical transport profile still applies) | adapter-owned minimal set + explicit `--auth-file`/forwarded value under the existing endpoint-binding rules | ambient credential re-resolution | Preserves the accepted legacy contract exactly |

**Compatibility is bounded extension, never security rollback.** There is no "full environment" mode. Compatibility mechanisms are limited to: explicit extra environment-variable *names* (opt-in, non-secret), role-specific inherited-variable policy adjustments, fail-closed rejection of known credential-shaped variables, and diagnostics that identify a *missing* variable by name without printing any value. No switch may reintroduce credential flow into project/PDB/verifier roles.

### 6.4 Boundary contract: worker shell ↔ executor (the brain/hands seam, in-process in V2)

- **Commands crossing (typed, existing vocabulary):** tool invocations (the `ToolSpec` names the controller already emits: `run_reproduction`, `run_regression_tests`, `classify_outcome`, `find_function`, `get_source_window`, `express_root_cause_hypothesis`, `apply_patch`, `revert_patch`, `syntax_check`, PDB actions) — each becomes a declared Executor operation with its existing argument contract.
- **Events crossing back:** existing `ToolResult` + observability events through the shared emitter (unchanged kinds).
- **Identity fields:** session id, run id, task id (existing) + role profile identity + execution-contract fingerprint on journaled execution events (new, additive).
- **Cancellation:** the cooperative token (existing semantics exactly: check at safe boundaries, never converted to model/verifier outcomes).
- **Timeouts:** per-operation bounds as today; declared in the environment policy for fingerprinting, not behavior change.
- **Error taxonomy:** existing split preserved and made explicit at the seam — model-correctable (bounded, sanitized, `recoverable=True` only where declared) vs infrastructure (`ToolExecutionError` fatal kinds, `PdbSessionError`, `WorkspaceError`, `CommandExecutionError`) — never conflated into success (already enforced by `tool_registry` + proof gates).
- **Credential policy:** executor operations receive **no credential variables and no credential channel**; only the model-adapter role ever does.
- **Environment policy:** all executor children build env from the role-scoped derivations; no `dict(os.environ)` sites remain in execution paths.
- **Workspace ownership:** unchanged (worker-owned session work dir; PDB per-session workspace; verifier-owned export root + four clean workspaces; supervisor post-mortem for worker-owned roots).
- **Restart/recovery (V2 scope):** none — worker death remains terminal for the session, honestly classified, with cleanup; retry (new chained session) remains the recovery story (§13 owner decision).

### 6.5 Boundary contract: session ↔ VerifierService (logical, in-process in V2)

- Invoked by the worker shell after the controller run ends with an active candidate; the execution source supplies only the typed `LocalProjectEvaluationPlan` (existing schema, unchanged) and cannot influence verification beyond it.
- The verifier role environment is credential-free and derived independently of the model channel (V2-01).
- Results return through the existing typed result + `VerifierSessionEventAdapter` journaling (existing kinds).
- Verifier hang/crash is bounded by the existing plan timeouts and produces an honest verifier failure (existing `EvaluationStatus` values), never a model success.
- The interface is designed so a future subprocess extraction (§5.7) needs only a transport adapter, not a redesign: plan in, typed result out, bounded timeout, honest failure taxonomy.

### 6.6 ExecutionEnvironment fingerprint — safe and precisely scoped

The fingerprinted artifact is the **safe declarative execution contract**, not proof of full environment equality:
- policy/schema version; role policies (rules, not resolved values); interpreter/runtime identity as safely representable; workspace policy/generation identifiers; allowed capability declarations; timeout/resource-policy declarations; safe normalized network/trust policy *identifiers* where appropriate.
- **Never hashed or journaled:** credential values, potentially credential-bearing proxy URLs, machine-local secrets. Since secret and machine-local values are intentionally excluded, the fingerprint is a *contract/provenance* fingerprint — it proves which declared policy produced the evidence, not byte-for-byte equality of the entire operator environment. The document and the journal payload must describe it exactly this way.

### 6.7 Failure-domain analysis (recommended architecture)

| Failure | Lost | Durable | Cleaned | Resume? | Must fail? | Evidence remaining |
|---|---|---|---|---|---|---|
| UI process dies | live view | journal (per-record fsync), manifest, artifacts | job object closes → worker tree killed; worker cleanup may not run; supervisor is gone so post-mortem is OS-driven (job kill) — session classifies INTERRUPTED on next open | reopen as replay only | no | full journal to last append; history classification `interrupted` |
| Session worker dies (crash) | in-flight turn, in-memory controller state | journal | supervisor reaps tree, runs post-mortem (work dir, isolated worktree) | no — INTERRUPTED, operator may start retry chain (new session) | honest fail | journal + crash classification + cleanup diagnostics |
| Verifier fails/crashes (in-process, V2) | verification result | journal up to `verifier.started` | verifier workspaces released by its own ledger/cleanup paths; a hang is bounded by plan timeouts | re-verification possible as a new verifier run (owner decision §13); session itself not resumed | verifier failure ≠ success; session ends UNRESOLVED or FAILED per existing taxonomy | verifier stage events, cleanup proof |
| PDB worker dies | debugger session | PDB events already journaled | `PdbSession.stop` ladder + workspace release (existing) | controller continues without PDB evidence (existing policy paths) | no | bounded PDB observations |
| Adapter child dies | that model request | model provenance, prior steps | process-tree termination (existing) | `LiveModelAdapter` bounded retries (existing) | no | transport error kind, termination reason |
| Provider HTTP timeout | that request | everything journaled | child killed (existing) | bounded retries then honest `model_error` (existing) | no | `LiveTransportError` kind, timing |
| Model protocol violation | the directive | everything journaled | n/a | bounded directive-repair attempts (existing, `9fab308`) | eventually directive-exhausted honest failure | rejected-directive events |
| Credential unavailable | model channel | config, provenance | n/a | no — fail closed before session starts (existing `ScenarioInputError` path) | **yes** (fail-closed is the invariant) | config state, quarantine record |
| Test/repro process hangs | wall time | journal | CommandRunner timeout ladder (existing) | no | timeout status (existing) | bounded output, timeout record |
| Workspace invalid | that operation | journal | cleanup verification flags failure (existing CLEANUP_FAILED) | no — workspace policy failure is terminal for the session (existing) | yes | cleanup events, workspace identity |
| Verifier *fails* (logic) | nothing | full verifier result + taxonomy | four clean workspaces released (existing) | no | outcome is the result (UNRESOLVED/etc.) — never a crash | verification certificate, F2P/P2P records |
| Machine restarts | everything in-memory | session dir (fsynced journal, manifest, artifacts) | nothing automatic — stale work dirs/worktrees are detected by next launch (history classification) and by git worktree prune guidance | no | n/a | durable artifacts intact |

(A verifier *subprocess* death row is intentionally absent: physical isolation is deferred per §5.7; the in-process row above reflects current and V2-02 behavior.)

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
5. **Verifier independence is preserved** by the VerifierService logical boundary, verifier role environment (credential-free), and unchanged clean-source/workspace/taxonomy invariants; physical isolation remains available behind §5.7 triggers if a concrete boundary requirement ever demands it.
6. **Offline/deterministic testability is preserved**: every new seam is fakeable (gateway, vault, executor interface, VerifierService); no stage introduces a network or live-model requirement for tests.

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

Remaining UI vocabulary decisions (already owner-resolved, §13): the advanced provider-type/endpoint-contract control replaces primary "Transport Profile (generic unless a historical endpoint contract is intended)" wording (`ui/screens.py:1928,2140`); credential-source labels move to a diagnostic details line; per-model `Ready / Needs API key / Protocol unsupported` (gateway-issued, actionable) replaces `available` + reason strings; catalog staleness markers stay as-is (already truthful).

`Runtime succeeded at T` (owner-resolved): a last-success timestamp may be recorded/displayed as non-authoritative historical metadata — it must state exactly what succeeded (catalog probe, connection probe, or real model runtime), include provider/model identity where relevant, never imply current availability, never affect scientific qualification, and never become a hidden routing authority. Implementation is deferred to the V2-03 ModelGateway/UI stage.

---

## 10. Security model (target, least authority)

**Where secrets may exist:** UI process memory (vault resolution), the single worker-spawn env hop (one variable, value only, `worker_process.py:136-151` — mechanism unchanged), the worker's bounded private session state for the duration of the authorized lease (stated honestly, §6.2), the per-request adapter child env (vault-issued material in the model-channel role only), the OS secure store, and (legacy routes) the operator CLI auth store read in place at the vault layer.
**Where they must never exist:** argv, the start message, scenario params, journals, events, manifests, reports, UI text beyond presence-only labels, checkpoints (none), diagnostics (existing `contains_credential_shape` scrubbing stays), `ExecutionEnvironment` (bindings-by-name only), and — **after V2-01 — any project/PDB/verifier child env** (today's `CommandRunner._build_env` full-inherit is the one deviation; V2-01 closes it).
**Who may resolve credentials:** only the vault. The UI process resolves the binding at session start; the worker materializes the authorized secret into adapter request children; the adapter child consumes the issued channel and never re-resolves ambient state (V2-04 removes the current third resolution).
**Does the executor need provider credentials?** No — and after V2-01 it structurally cannot see them.
**Does the gateway need workspace filesystem access?** No — it constructs transport configs and bindings only; it never touches execution workspaces (already true; the interface makes it a rule).
**Environment allowlisting:** role-scoped derivations per §6.3; the TLS/proxy subset (`provider_connections.py:648-657`) becomes a declared *provider-transport capability* confined to the model-adapter role; project/PDB/verifier roles receive platform essentials plus explicit opt-ins only; known credential-shaped variables are fail-closed rejected everywhere outside the model channel.
**Endpoint/credential binding:** preserved verbatim (vault-owned): ambient sources are canonical-endpoint-bound; saved/session are provider-identity-bound; quarantine blocks resolution (existing rules, `provider_connections.py:2281-2365`).
**Process identity and containment:** unchanged — Windows job object on the worker tree, PID identity via `python_launcher` (declared in the environment policy, not duplicated), per-command tree-kill ladders.
**Compatibility boundary:** bounded extension only — explicit extra non-secret variable names, role-policy adjustments, fail-closed credential rejection, name-only diagnostics. **No full-environment mode may ever exist** (see §6.3).

---

## 11. Migration strategy (vertical, incremental, complexity earned immediately)

Each stage leaves the repository runnable, is testable, has one compatibility seam, and never requires touching UI + providers + debugger + verifier simultaneously.

### V2-01 — ExecutionEnvironment authority + credential isolation (first slice: real invariant change)
- **Outcome:** one session-owned execution-environment declaration (the minimum contract needed — no speculative typing beyond it); role-scoped child-environment derivation; **provider credential removed from project repro/test/PDB/verifier environments**; the model adapter keeps exactly its explicitly authorized credential channel (existing functionality preserved); Windows venv/PID behavior preserved (`python_launcher` stays the single interpreter authority); regression tests proving no secret reaches project execution children; compatibility limited to bounded non-secret extension (§6.3 — no full-environment mode).
- **Affected:** `runtime/command_runner.py`, `application/command_transport.py` (role-profile consumption), `runtime/pdb_session.py` (PDB role declaration), `application/local_project_source.py` + sibling sources (construction), `application/local_project.py` (workspace identity), `application/worker_process.py` (spawn-role wiring), tests.
- **New contract:** the environment policy type + role-profile derivations + fail-closed credential rejection; contract/provenance fingerprint (safe fields only, §6.6).
- **Compatibility:** the initial project-command role includes the ambient set *minus* credential/credential-shaped variables; additional user/project variables require explicit bounded opt-in by name; diagnostics name missing variables without values.
- **Validation:** new unit tests asserting credential variables never reach project/PDB/verifier children (the falsifying test for this stage) and fail-closed rejection of credential-shaped opt-ins; existing command-runner/transport/PDB tests; one real Windows manual session with a repro script that reads its environment (verification artifact recorded in the stage's review notes).
- **Rollback/exit:** exit criterion = no repro/verify behavior regression across curated fixtures and one real project, with secret-exclusion tests green; rollback = revert the stage (no durable format consumed it yet beyond additive provenance fields, which are dropped before any frozen run uses them).

### V2-02 — Session / AgentDefinition / Executor logical seams
- **Outcome:** typed `SessionLaunch`/`Session`; `AgentDefinition` as **product/runtime provenance only** (no scientific identity migration — §8.2); `Executor` interface around the existing execution modules; `VerifierService` logical boundary (verifier code and invariants untouched); no new process.
- **Affected:** `application/worker_process.py`, `worker.py`, `session.py`, sources, `application/verifier_observer.py` (call-site only), `ui/app.py` (call sites only).
- **New contract:** `SessionLaunch`, `AgentDefinition`, `Executor` (interface), `VerifierService` (interface).
- **Compatibility:** old call paths adapter-shimmed for one stage; behavior byte-identical.
- **Validation:** worker-process integration tests, local-project integration tests, verifier observability tests, offline demo.
- **Rollback/exit:** revert the commit; no durable format change beyond additive fields.

### V2-03 — ModelGateway product/runtime seam
- **Outcome:** UI and sources consume `ModelGateway` only; UI no longer understands provider transport internals; precise §9 status vocabulary implemented (including the removal/redefinition of `Connected` and the `Runtime succeeded at T` metadata); `Provider type` / `Endpoint contract` advanced disclosure replaces primary transport-profile wording; the current provider core modules stay intact beneath.
- **Affected:** `ui/app.py`, `ui/screens.py`, `ui/session_config.py`, sources, `model_providers.py` (façade methods only).
- **New contract:** `ModelGateway` interface + the §9 status-fact semantics.
- **Compatibility:** the façade delegates to existing functions; durable `transport_profile` unchanged; UI changes are labels/organization only.
- **Validation:** UI tests (`test_ui_provider_connections.py`, `test_ui_configured.py`), provider integration tests, updated vocabulary assertions (including a regression test that credential presence alone never renders a connected-style headline).
- **Rollback/exit:** UI can call the old functions for one stage; exit when no UI import of provider internals remains.

### V2-04 — CredentialVault binding/backend seam
- **Outcome:** `CredentialBinding` (non-secret, session-stable) separated from credential material/lease (per-request injection, honest session-scoped retention §6.2); single session authority decision; the adapter child consumes the issued channel and stops re-resolving ambient state (removing the third resolution from §3.2); Windows Credential Manager backend first; future macOS/Linux backends possible but not claimed.
- **Affected:** `application/provider_connections.py` (backend extraction), `scripts/provider_direct_api_adapter.py` (binding consumption), tests.
- **New contract:** `CredentialVault` interface, `CredentialBinding`, lease/materialization semantics.
- **Compatibility:** endpoint-binding/quarantine rules move verbatim; the extensive existing credential tests must pass with unchanged behavior.
- **Validation:** the provider-connections credential test family, native wincred smoke test, adapter credential tests.
- **Rollback/exit:** backend extraction is internal; exit when the adapter no longer imports `resolve_runtime_credential` and all credential tests pass.

### V2-05 — OPTIONAL verifier process-isolation evaluation (not a committed migration requirement)
- **Outcome:** evaluate the §5.7 triggers against field evidence. By this stage the logical `VerifierService` seam and the verifier role environment already exist, so extraction would be a transport adapter — but it happens **only if a trigger is evidenced**. Otherwise the stage is recorded as **NOT JUSTIFIED / DEFERRED** and no implementation work is created merely to complete the numbered list.
- **Validation (if triggered):** verifier integration + kill/hang tests for the child, offline demo; exit criteria defined at that time.
- **Rollback/exit:** not triggered ⇒ no work, stage closed as deferred with the evidence review attached.

**Sequencing rationale:** V2-01 lands first because the credential exposure is active and evidenced — complexity earns itself immediately; V2-02 provides the seam vocabulary 01 deliberately minimized; V2-03/V2-04 are pure interface seams ordered by user-visible value (status truthfulness) then portability; V2-05 stays optional and trigger-gated.

---

## 12. Recommendation summary (decisive)

1. **Is a V2 architecture change warranted?** Yes — Alternative B: explicit logical planes and first-class authority contracts in the current principal process topology. The observed incidents form a class (undeclared execution-environment/credential/identity state re-derived at multiple sites), one active instance of that class (credential variables inherited by project repro/verify and verifier children) is present in current source, and one post-plan product incident (presence-only `Connected` with a dead endpoint) confirms the status-vocabulary defect. The class is worth closing at its generator.
2. **Which alternative?** B — `Session`, `AgentDefinition` (product provenance only), `ExecutionEnvironment` (one authority, role-scoped derivations), `ModelGateway`, `CredentialVault` (binding vs materialization), `Executor` and `VerifierService` interfaces; **no new long-lived processes, no committed process moves**; verifier subprocess isolation deferred behind §5.7 triggers; Alternative C rejected (no failure requires long-lived executors; IPC/Windows/workspace-restart costs unearned).
3. **What remains untouched?** `DeterministicController` and its policy/state machine; the PDB protocol/worker/session; the journal/emitter/events/observability evidence chain; patch policy (`PatchManager`); disposable-workspace semantics; the verifier's logic, outcome taxonomy, and clean-source/workspace invariants; history/replay/presentation projections; the scientific qualification boundary and existing treatment-identity authorities (roster, prompt profiles, frozen provenance — `AgentDefinition` never supersedes them); frozen experiment paths; dataset adapters; the demo offline path; Windows job-object containment.
4. **First implementation slice?** V2-01 — ExecutionEnvironment authority + credential isolation: the minimum session/environment contract needed to make provider credentials structurally unavailable to project repro/test/PDB/verifier children, with secret-exclusion regression tests and bounded (non-secret-only) compatibility. No typing-only stage precedes it.
5. **Explicitly deferred:** real checkpoint/resume (owner-deferred; retry-chain + journal + replay remain the behavior; reopen on the §7 trigger); verifier physical subprocess isolation (§5.7 triggers; logical VerifierService seam lands in V2-02); macOS/Linux credential backends (interface prepared in V2-04, implementations out of scope, none claimed); UI redesign beyond vocabulary; scientific-path verifier changes; any migration of scientific treatment identity into `AgentDefinition`.
6. **What would make us reconsider?**
   - *Toward C (process split):* the §7 checkpoint/resume trigger firing (material operator loss of long controller progress or a concrete resumable-session requirement), or PDB/tool serialization becoming a demonstrated bottleneck.
   - *Toward A (stop V2):* if V2-01's role-scoped environments cannot land without destabilizing the worker boundary tests, or if real user repro scripts cannot be satisfied safely through bounded non-secret opt-ins (escalate per §13; fallback is Alternative A with documented residual credential-exposure risk).
   - *Toward verifier physical isolation (V2-05 trigger):* verifier crashes/hangs materially threatening worker lifecycle, in-process environment isolation proving insufficient, a concrete untrusted-code security boundary, or measured operational evidence that isolation pays for its Windows cost.
   - *Away from B's status vocabulary:* if owner testing shows the precise-facts display (§9) confuses users more than it informs, the headline form may return only under the live-probe-with-timestamp rule — never presence-only.

---

## 13. Owner decisions

**Resolved (recorded 2026-09-03, FirstMate architecture-review 02):**

1. **Transport-profile UI (resolved):** explicit transport identity remains in durable internal configuration (safety-critical) and stays discoverable in the UI under an **advanced `Provider type` / `Endpoint contract` control**; ordinary users do not see "historical transport profile" as primary vocabulary; known presets (CommandCode, OpenCode, Ollama) may expose their explicit contract through the advanced control; Generic remains the normal custom-provider default; no inference from provider technical ID/name/URL. Implemented in V2-03.
2. **Runtime-tested timestamp (resolved):** may be recorded/displayed as non-authoritative historical metadata (`Runtime succeeded at T` semantics, §9) — must state exactly what succeeded, include provider/model identity, never imply current availability, never affect scientific qualification, never become a hidden routing authority. Implementation deferred to V2-03.
3. **Checkpoint/resume (resolved):** DEFERRED. Current retry-chain + durable journal + replay remain the product behavior; replay is never called resume. Reopen only on the §7 trigger (material operator loss of long-running controller progress, or another concrete resumable-session requirement); Alternative C may be reconsidered then but is not required.
4. **Execution-environment allowlist (resolved as policy, not as a plan-time list):** the architecture decision is the policy — role-scoped least-authority derivation; known secrets never reach execution roles; required platform/runtime variables are preserved; additional user/project variables require explicit bounded opt-in; tests + real Windows projects determine the concrete initial set. The implementation stage owns discovering the exact set through repository evidence and compatibility tests; escalate only if a real user-script compatibility choice cannot be resolved safely. No owner-fixed variable list is required at plan time.

**Still open:**

5. **Verifier re-run policy:** after a verifier failure (hang/crash/logic failure under the existing honest taxonomy), should the operator be able to re-run verification on the retained candidate within the same session (a new verifier run over the same evidence chain, requiring an event-kind addition), or does the session stay terminal as today? (Recommendation: allow a re-verify action behind the VerifierService seam; not implementation-blocking for V2-01/02.)

---

## 14. Source references (validation)

All current-state claims in this document trace to the following inspected source/tests (baseline `4606933`):

- Worker topology: `agentic_debugger/application/worker_process.py`, `worker.py`, `worker_protocol.py`, `worker_scenarios.py`; `tests/integration/test_worker_process.py` (handshake, crash classification, pre-start cancel, journal catch-up).
- Sources and execution interleave: `application/local_project_source.py` (1,333 lines — controller+tools+PDB+patch+transport+verifier in one function), `local_source.py`, `configured_source.py`, `deterministic_source.py`, `ollama_cloud_source.py`.
- Providers/credentials/status: `application/model_providers.py`, `provider_connections.py` (credential ladder 2319-2509; endpoint binding 2281-2316; network allowlist 648-694 and the proxy-credentials note at 642; wincred 432-538; truthful presence-only `ProviderConnectionStatus` 3020-3081), `provider_http.py`, `command_transport.py:153-186` (and per-request `Popen` at 268), `scripts/provider_direct_api_adapter.py:132-148`; `tests/unit/test_configured_provider_params.py`, `tests/integration/test_ui_provider_connections.py`, `test_ladder_unified_provider_runtime.py`, `test_provider_direct_api_session.py`.
- Environment sites (incl. the active exposure): `runtime/command_runner.py:293-296` (`_build_env = dict(os.environ)`), `evaluation/live.py:523` (minimal env), `command_transport.py:153-186` (merged env), `runtime/pdb_session.py:390-405` + `runtime/python_launcher.py` (venv/PID authority), `worker_process.py:309-320` (credential hop); worker-side credential retention for per-request re-injection: `local_project_source.py:1024-1025`.
- PDB: `runtime/pdb_session.py`, `pdb_worker.py`, `pdb_protocol.py`; `tests/integration/test_pdb_session_integration.py`, `test_pdb_interactive_controls.py`.
- Verifier (substantive logical independence, in-process today): `evaluation/local_project_verifier.py` (304-470: commit binding, clean-source export, four clean workspaces, independent baseline/candidate evaluation, cleanup/source-integrity proof), `evaluation/verifier.py`, `outcome_taxonomy.py`; invoked in-process at `application/local_project_source.py:1120-1156`; `tests/integration/test_evaluation_verifier.py`, `test_verifier_observability.py`.
- Journal/history/replay: `application/journal.py` (fsync-per-append, 119-194), `emitter.py`, `events.py`, `history.py` (manifests/reopen/classification), `replay.py` (read-only cursor — "not resume" claim); `tests/unit/test_application_journal.py`, `test_application_history.py`.
- Controller/tools: `agent/controller.py:845-930` (cancel-check contract), `tool_registry.py`, `skills/`; `tests/unit/test_controller.py`, `test_controller_cancellation.py`.
- UI vocabulary: `ui/screens.py:1277-1282,1520-1528,1928,2140`, `ui/session_config.py` (availability vs qualification separation), `ui/app.py:476-494,655-671,895-911` (credential hop + worker spawn).
- Windows harness: `application/process_tree.py` (job object, suspended spawn, kill ladders); roster/scientific boundary: `CURRENT_AGENT_ROSTER.md`; prompt-profile identity: commit `77a4b3f`.
- Post-plan provider-status incident (`Connected · saved` with unreachable `127.0.0.1:57788` CommandCode GOAT endpoint during live owner validation, 2026-09-03): owner/FirstMate review evidence; consistent with the presence-only `connected` definition at `provider_connections.py:3072-3081`.

Validation performed for this revision: documentation-only repair; every current-source claim touched by the repair was re-verified against the live repository (credential sites, per-request adapter spawn, worker-side credential retention, presence-only status semantics, verifier in-process invocation and independence construction, documentation-navigation tests). `tests/unit/test_public_documentation_navigation.py` run before and after the `docs/README.md` change (10 passed both times). No production changes, no dependency changes, no provider configuration changes, no credential operations, no live provider calls, no frozen experiment runs, no full test suite run — docs-only candidate per repository validation policy.
