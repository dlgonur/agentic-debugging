# Agentic Debugger V2 — Control/Execution Plane Separation Architecture Plan

**Document type:** Architecture analysis and migration plan (decision record)
**Status:** Plan — no implementation has occurred
**Baseline:** `plan/architecture-v2-control-execution-separation` at `4606933` (fix(providers): harden provider runtime and Windows harness), clean tree
**Scope:** Determine whether the application runtime should adopt an explicit CONTROL / EXECUTION plane separation, and define the smallest coherent target architecture and incremental migration path
**Companion decision record:** `docs/architecture/adr-0001-control-execution-plane-separation.md`

---

## 1. Executive recommendation

**Yes — a V2 architecture change is warranted, but the smallest one that addresses the observed evidence: Alternative B (explicit logical planes) with one targeted process-boundary repair, not Alternative C (a general control-plane + execution-plane process split).**

The repository's recent boundary incidents (credential authority divergence, TLS/proxy environment loss across the worker/adapter hop, OpenCode auth-store visibility, Windows venv PID indirection, UI treating configuration presence as readiness) are not symptoms of a missing process. The pipeline already has three process tiers and per-tool child processes; each incident was repaired *inside the existing topology* by adding an explicit contract (credential forwarding variable, network-environment allowlist, `--auth-file`, `python_launcher`, truthful availability). That history is direct evidence that the architecture's weak point is **undeclared, distributed execution-environment state**, not process count.

The single most important V2 primitive is therefore **`ExecutionEnvironment`** — one typed, versioned, immutable declaration of everything a child process needs and is allowed: interpreter identity, workspace roots, network/trust policy, credential *bindings* (references, never values), environment allowlist, resource limits. Today the same facts are scattered across at least six construction sites (§3.3), and each of the observed incidents is traceable to one of those sites disagreeing with another.

The one process-boundary change V2 *should* make is narrow and evidence-driven: **the independent verifier must stop executing inside the session worker process** (§5.7, V2-03). This is the only place where the current topology weakens a mandated product invariant (verifier independence) rather than merely complicating maintenance.

Everything else — deterministic controller, typed tools, bounded PDB protocol, disposable workspaces, journal authority, verifier outcome taxonomy — stays structurally untouched. Complexity budget: five stages, each independently shippable, no stage requires rewriting UI, providers, debugger, and verifier simultaneously.

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
              │           └── adapter child process (scripts/provider_direct_api_adapter.py
              │               or opencode/commandcode CLI; minimal env + 1 credential var)
              ├── CommandRunner (user repro/verify; _build_env = dict(os.environ))
              │     └── user command children
              ├── PdbSession.start (pdb_session.py:417)
              │     └── PDB worker child (runtime/pdb_worker.py; -I -u -c runpy bootstrap)
              │           └── debug target (imported in-process by PDB worker)
              └── LocalProjectVerifier.evaluate (in-process!)
                    └── 4 disposable TaskWorkspaces, each running
                        repro/regression commands via CommandRunner
```

Process/lifecycle authorities (all verified in source):

| Responsibility | Owner | Evidence |
|---|---|---|
| Session spawn, Windows job containment, escalation ladder, crash classification, post-mortem cleanup | `SessionWorkerProcess` (UI process) | `worker_process.py:106-951` |
| Journal (single writer, fsync-per-append), cancel token, work dir, cleanup, terminal | session worker (`worker.py::run_worker`) | `worker.py:499-713` |
| Provider/credential/trust state | UI process **and** worker **and** adapter child (three copies) | §3.2 below |
| Controller state machine, budgets, tool dispatch | worker process (`agent/controller.py:845`) | `local_project_source.py:1072-1076` |
| Model transport | worker process → adapter subprocess (`CancellableJsonlCommandTransport`) | `local_project_source.py:1017-1034` |
| PDB protocol, bounded observation | worker → PDB worker subprocess | `pdb_session.py:417`, `pdb_worker.py` |
| Patch parse/apply/revert, allowed-path policy | worker process (`runtime/patcher.py`, 1796 lines) | `local_project_source.py:609-746` |
| Independent verification | **worker process (in-process call)** | `local_project_source.py:1120-1156` |
| Session truth | durable journal (`session.events.jsonl`), derived manifest | `journal.py`, `history.py` |

### 2.2 What crosses process boundaries today

- **UI → worker**: one `start` JSON line (`worker_protocol.py`): `SessionSpec` mapping, run id, journal path, work dir, scenario name + params (repo path, HEAD, workspace path, commands, provider/model ids, budget refs), `child_environment` (≤1 credential variable value — passed via `Popen env`, never argv/journal; `worker_process.py:136-151,309-320`).
- **Worker → UI**: `ready` / `event` (sequence number only; journal is authority) / `liveness` side-band / `terminal` / `fatal` / `error` — bounded JSON lines (`worker_protocol.py:1-34`).
- **Worker → adapter child**: minimal env (`PATH`, `PYTHONIOENCODING`, `SystemRoot`, config/catalog/quarantine path vars, `HOME`/`USERPROFILE`/`LOCALAPPDATA`) + the allowlisted TLS/proxy subset (`SSL_CERT_FILE/DIR`, `CURL_CA_BUNDLE`, `*_PROXY`, `NO_PROXY`) + one credential variable (`command_transport.py:153-186`, `provider_connections.py:648-694`).
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

**The current replay is audit/UI replay, not resume.** This distinction matters for V2 scoping (§7).

---

## 3. Coupling and failure analysis — why the incidents happened

### 3.1 The pattern behind all six incident classes

Each listed incident maps to the same architectural gap: **a fact about execution was owned by whichever process/module happened to need it first, and every consumer re-derived it independently.** When two derivation sites disagreed, the boundary failed — and because the derivations were scattered, each repair (correct, and now regression-tested) hardened one site without eliminating the class:

| Incident | Divergent owners of the same fact |
|---|---|
| Parent credential authority ≠ child adapter authority | UI resolves `provider_session_credential_environment` (UI process store/session state) vs adapter child re-resolves via `resolve_runtime_credential` (worker's env/config view) |
| TLS/proxy visible in parent, lost across worker/adapter boundary | parent `os.environ` vs `JsonlCommandTransport.subprocess_environment()` minimal env vs `CancellableJsonlCommandTransport` allowlist merge |
| OpenCode auth store visible in one process only | UI process knows `OPENCODE_CONFIG_DIR`; worker/adapter had to re-discover → fixed by explicit `--auth-file` + forward-as-value |
| Windows venv launcher PID mismatch | `Popen(sys.executable)` redirector PID vs real interpreter PID checked by handshakes → fixed by central `python_launcher` |
| Transport/protocol/auth/subprocess coupling | one resolver (`model_providers.py` + `provider_connections.py`, 4,500 lines combined) interleaving route choice, protocol validation, credential resolution, environment construction, provenance |
| Config presence treated as readiness | `provider_availability()` (presence-only) vs UI `connected` display vs actual first-request executability |

### 3.2 Credential flow today (three independent resolutions per session)

1. UI: `provider_session_credential_environment(kind)` resolves saved→session→forwarded→env→CLI-auth (endpoint-binding-checked) and forwards **one** variable into the worker spawn env (`ui/app.py:476-494`, `worker_process.py:309-320`).
2. Worker (Local Project source): `provider_transport_environment(provider)` re-resolves the same ladder inside the worker and passes it to the transport constructor (`local_project_source.py:1024-1025`).
3. Adapter child: `_resolve_credential` calls `resolve_runtime_credential(provider)` **a third time** inside the child, which reads the worker's env (the forwarded variable, or ambient env/CLI store) (`scripts/provider_direct_api_adapter.py:132-148`).

This triple resolution is honest (no value in argv/journal/diagnostics anywhere — verified) but it means *credential authority is nowhere*: each tier can disagree (e.g. worker forwards a saved credential while the child's re-resolution picks a stale ambient env var under a changed endpoint). The endpoint/quarantine binding rules in `provider_connections.py:2281-2365` exist precisely to keep the three resolutions convergent — rules that must be re-proven at every new construction site.

**Unaddressed leak (found during this investigation, not part of the six incidents):** the forwarded credential variable lives in the worker's `os.environ`; `CommandRunner._build_env()` copies the full environment into every user reproduction/verification command and every verifier command child (`command_runner.py:293-296`, `local_project_source.py:163-203`, `evaluation/local_project_verifier.py:54`). Any user-supplied repro/verify script (or anything it invokes, e.g. a `pip install` in a setup path, or a malicious print hook) can read the provider API key. The transport allowlist discipline that was carefully built for the *model* boundary does not yet exist for the *execution* boundary. This is the concrete, repository-grounded argument that execution-plane environment policy must become a first-class contract (V2-02).

### 3.3 The six undeclared execution-environment construction sites

| Site | Env passed to children | Interpreter identity | Network/trust |
|---|---|---|---|
| `SessionWorkerProcess._worker_argv` + `build_worker_env` | full inherit + 1 credential var | `resolve_worker_executable` (venv-aware) | inherited |
| `CancellableJsonlCommandTransport.subprocess_environment` | minimal + config paths + allowlist | `sys.executable` | explicit allowlist |
| `PdbSession._worker_env` | `build_worker_env(None)` (inherit or venv fixup) | `resolve_worker_executable` | inherited |
| `CommandRunner._build_env` (user commands + verifier) | **full inherit + PYTHONIOENCODING** | caller's argv (`python`/`python3` resolved by PATH) | inherited |
| `JsonlCommandTransport.subprocess_environment` (scientific/evaluation path) | minimal (`PATH`, `PYTHONIOENCODING`, `SystemRoot`) | n/a | minimal |
| Legacy CLI routes (`opencode_provider_adapter` etc.) | adapter-owned | n/a | adapter-owned |

Every row is individually justified; the problem is that no single typed object declares the intended environment for a session, so each new child type re-decides inheritance, allowlists, and interpreter selection — and the incident history shows they drift.

### 3.4 Interleaving in the provider resolution core

`model_providers.py` (1,018 lines) + `provider_connections.py` (3,490 lines) currently interleave in one call path: route selection (direct vs legacy CLI), effective-protocol resolution + auth/profile capability validation, credential source resolution, live-config command construction, provenance payload construction, and environment forwarding. UI, worker sources, ladder, and local-project all consume this core, so none of them can avoid understanding `transport_profile`/`route`/`auth_mode` vocabulary — which is exactly why that vocabulary leaked into the UI (§3.5). A `ModelGateway` contract (V2-04) exists to draw one seam here, not to rewrite the core.

### 3.5 UI exposure of implementation vocabulary

Verified in current UI source:

- **"Transport Profile (generic unless a historical endpoint contract is intended)"** — an add/edit-provider dialog label (`ui/screens.py:1928,2140`). This is durable-configuration vocabulary (needed for safety: it gates legacy CLI eligibility and endpoint binding) surfaced as a primary user decision. Most users should never choose it; the dialog should default to Generic and hide the selector behind an advanced disclosure, while the durable config keeps the field (the plan explicitly does **not** remove transport identity from durable internal configuration).
- **"(historical)"**-style qualification framing — the credential-source labels (`saved` / `session only` / `environment` / `CLI auth`, `ui/screens.py:1277-1282`) and the "Connected · source" summary line (`ui/screens.py:1528`) expose *which mechanism* supplied the key, not what the user can do next.
- **Readiness vocabulary** — the current UI states are binary-ish: `connected` (a usable credential source exists: `provider_availability()`, presence-only by design), `Catalog N models / stale/unverified / not connected`, per-model `available` + `unavailable_reason` ("no direct API credential — connect in Model Providers (press m)"), and picker rows. The proposed statuses in the task brief (`Configured`, `Credential available`, `Catalog reachable`, `Model runnable`, `Runtime tested`, `Connected`) do **not** exist as such today; they are a *target vocabulary* V2 should define with exact meanings (§9), all offline/presence-only until a session actually runs.

**Recommendation:** keep `Connected` as the only user-facing top-level state (meaning: a configured provider with a currently usable credential binding — presence-only, never implying reachability), keep `Catalog` as cached-discovery metadata with its existing staleness qualifier, and move everything else (transport profile, credential mechanism, protocol family, route) into an advanced/diagnostic view. `Runtime tested` must remain a per-session historical fact ("last succeeded on …"), never a predictive status.

---

## 4. Module responsibility map

Legend for TARGET V2 HOME: **CP** = control plane (orchestration, session, gateway; stays in UI process + worker shell), **EP** = execution plane (workspace/tools/debugger/tests/patch; worker process body + child processes), **GW** = provider gateway seam, **IND** = must remain independent of CP execution path.

| Module | Current responsibility | Process / lifecycle | Current dependencies | Target V2 home | Action | Migration risk |
|---|---|---|---|---|---|---|
| `application/worker_process.py` | Supervisor: spawn, job containment, cancel, grace/escalate, classify, post-mortem cleanup | UI process, per session | `process_tree`, `python_launcher`, journal reader, worker_protocol | CP (session supervision) | Keep; gains typed `SessionLaunch` (V2-01) and verifier-service handle (V2-03) | Low |
| `application/worker.py` | Worker shell: journal/cancel/work-dir/cleanup/terminal; `SessionCoordinator` | child process | emitter, journal, protocol, sources | CP shell: owns Session identity, delegates execution | Split: lifecycle stays; scenario dispatch moves behind Executor seam (V2-01) | Medium — the run_worker state machine is the repo's most-tested boundary |
| `application/worker_scenarios.py` | Bounded non-product boundary harness scenarios | worker process | — | CP test harness (unchanged role) | Keep | Low |
| `application/local_project_source.py` (1,333) | The product execution: builds task, tools, PDB, patch, transport, controller, verifier, artifacts — all in one function | worker process | controller, tools, patcher, PDB, transport, verifier, providers, level32, demo, scripts | Split across CP/EP (this file is the concrete proof of the brain/hands interleave) | **Split** (V2-01/V2-02): provider+transport+limits → gateway request; tools/PDB/patch/workspace → Executor; verifier invocation → independent service | High — highest-traffic module; must stay runnable at every stage (done by seam insertion, not rewrite) |
| `application/local_source.py`, `configured_source.py`, `deterministic_source.py`, `ollama_cloud_source.py` | Other execution sources sharing the same interleave | worker process | same family | same split as above | Split with the same seams; deterministic/configured are the low-risk first movers | Medium |
| `application/model_providers.py` (1,018) | Registry: availability, model listing, route resolution, live-config + provenance construction | UI process (pickers) + worker (session resolve) | provider_connections, scripts adapters, evaluation.live | GW (behind `ModelGateway` façade, V2-04) | Keep module; add narrow gateway interface above it | Low — façade only |
| `application/provider_connections.py` (3,490) | Provider configs, quarantine, credential ladder, endpoint binding, protocol resolution, environment forwarding | UI + worker + adapter (imported by all three) | provider_http, wincred, filesystem | GW + `CredentialVault` backend (V2-05 extracts the OS-backend half only) | Split (later stage): backend-agnostic vault interface; resolution ladder stays | Medium — credential tests are extensive; keep behavior byte-identical |
| `application/provider_http.py` | Bounded HTTPS (stdlib + curl fallback), URL canonicalization, error sanitization | parent (discovery) + adapter child | — | GW / adapter child | Keep | Low |
| `application/command_transport.py` | Cancellable JSONL transport; minimal env + allowlist + network parity merge | worker process | evaluation.live, process_tree | CP→GW (model channel) | Keep; env construction becomes an ExecutionEnvironment consumer (V2-02) | Low |
| `evaluation/live.py` (JsonlCommandTransport, LiveModelAdapter) | Scientific command transport + live model adapter | worker process + evaluation harness | — | GW (product path) / evaluation (research path, unchanged) | Keep both roles; product gateway wraps them | Low |
| `scripts/provider_direct_api_adapter.py` + CLI adapters + `protocol_prompt_shaper.py` | Protocol-1.3 JSONL adapter children (direct API / legacy CLI) | adapter child process | provider_connections (credential resolve) | GW child (unchanged contract) | Keep; consume vault-issued binding (V2-05), stop re-resolving ambient state | Medium (credential contract change) |
| `agent/controller.py`, `controller_policy.py`, `state_machine.py`, `trajectory.py`, `observer.py`, `proof_gate.py` | Deterministic controller: state machine, budgets, directives, tool dispatch, steps | worker process | tool_registry, model adapter (duck-typed) | CP (the brain) | **Keep untouched** — it is already plane-clean: model via adapter interface, tools via registry, cancellation via injected check | None |
| `agent/tool_registry.py` + `skills/` | Typed tool contracts + source inspection skills | worker process | runtime modules | CP↔EP contract (the registry *is* the command vocabulary) | Keep; handlers become Executor-side (V2-02) | Low |
| `runtime/workspace.py`, `patcher.py`, `command_runner.py`, `test_runner.py`, `execution.py`, `exceptions.py` | Disposable workspaces, unified-diff patch policy/apply/revert, bounded command/test execution | worker process (children) | — | **EP** | Keep; `CommandRunner` gains ExecutionEnvironment-driven env construction (closes the §3.2 leak) | Medium (env behavior change must be tested against real repro scripts) |
| `runtime/pdb_session.py` (3,009) + `pdb_worker.py` (3,853) + `pdb_protocol.py` | Bounded PDB protocol, session lifecycle, worker with safe-eval/locals bounds | worker → PDB child | python_launcher, workspace | EP (already a model of the target pattern: typed protocol, PID identity handshake, bounded vocabulary, per-session disposability) | Keep unchanged | None |
| `runtime/python_launcher.py` | Windows venv interpreter/PID identity authority | shared by all spawners | — | EP platform seam (`PlatformRuntime` impl) | Keep; becomes one implementation behind a platform interface (V2-02) | Low |
| `application/journal.py`, `emitter.py`, `events.py`, `observability.py`, `source_snapshots.py` | Durable append-only evidence + typed events + observability producers | worker process (single writer) | — | CP (session evidence authority) | **Keep untouched** | None |
| `application/history.py`, `replay.py`, `reporting.py`, `presentation.py`, `workstream.py` | Manifests, discovery, read-only replay, pure presentation projections | UI process | journal, events | CP (view) | Keep; presentation stays forbidden from evidence creation (already enforced) | None |
| `evaluation/local_project_verifier.py`, `evaluation/verifier.py`, `runner.py`, `outcome_taxonomy.py`, `local_project_verifier` adapters | Independent verification: clean-baseline reproduction, patch re-apply, F2P/P2P, cleanup proof | **worker process (in-process)** | command_runner, patcher, workspace | IND (verifier service — V2-03 moves it out of the worker process) | Move (target: separate short-lived child process per evaluation, spawned *not* by the execution path) | High — most safety-critical module; process move must preserve outcome taxonomy exactly |
| `application/local_project.py` (1,144) | Project validation, isolated git worktree lifecycle, task-spec contract, containment | UI process (prepare) + worker (verify) + supervisor (post-mortem) | git CLI | EP (workspace lifecycle) | Keep; already correctly cross-process cooperative | Low |
| `application/level32.py`, `ollama_cloud_source.py` (ladder) | Scientific capability-ladder operator (qualification-bound) | worker process | qualified roster, adapters | CP but **scientific boundary fenced** (§8) | Keep; ladder qualification never derives from provider availability (already true — `is_treatment_eligible`) | None (no change) |
| `quixbugs/`, `bugsinpy/` | Pinned dataset adapters, contained PDB, license-gated WSL prep | operator/evaluation processes, not product runtime | runtime, datasets | EP (offline) / frozen research | Keep; out of product runtime scope | None |
| `ui/app.py`, `screens.py`, `widgets.py`, `models.py`, `session_config.py` | Textual UI: home, provider manager, session setup, workspace, history | UI process | application layer | CP (presentation) | Keep; V2-04/V2-05 only *narrow* what it imports (gateway façade, no transport vocabulary) | Low-medium |
| `demo/` (catalog, tools, runner, policies, model…) | Offline deterministic tasks + tool context/registry builder shared with live sources | worker process (deterministic + live sources import demo.tools) | runtime | EP; `demo.tools.build_registry` is shared tool-vocabulary authority | Keep (defer consolidation; shared import is a wart, not a risk) | None |

Deliberately omitted: `comparison/`, `rag/`, `preference/`, `events/` (research subsystems), `datasets/`, `experiments/`, frozen research paths — unaffected by plane separation.

---

## 5. Alternatives evaluated

### 5.1 Alternative A — keep process architecture, harden contracts

Evolve nothing structurally; continue the incident-response pattern (add explicit contract at each divergence site).

- **Correctness:** achievable; the six incidents were each fixed this way and hold under regression tests.
- **Prevents the observed classes?** Only until the next construction site appears. §3.3 shows six environment sites and §3.2 shows an *active* leak (credential in user-command env) that contract-hardening would likely rediscover site-by-site rather than close class-wide.
- **Verifier independence:** stays as construction discipline (in-process verifier) — the invariant rests on reviewer vigilance, not boundary.
- **Latency / complexity / Windows:** optimal (nothing changes); debuggability unchanged.
- **Cost/risk:** lowest.
- **Verdict: insufficient.** It leaves the class-generator (undeclared environment state, triple credential resolution, in-process verifier) in place. Reasonable as a fallback if V2 stalls, but it is the strategy that *produced* the incident list.

### 5.2 Alternative B — explicit logical planes, current process topology + one verifier boundary change (recommended)

First-class contracts — `Session`, `AgentDefinition`, `ExecutionEnvironment`, `ModelGateway`, `CredentialVault`, `Executor` (interface), `Verifier` (service) — with exactly one change to process topology: verifier evaluation runs as a short-lived child process outside the session worker (V2-03). No new long-lived processes; the session worker remains the single orchestration process; brain and hands stay in it but behind typed seams that make the boundary *enforceable* and testable.

- **Correctness:** every observed incident class is closed *at its generator*: one `ExecutionEnvironment` makes worker/adapter/PDB/user-command/verifier env derivations consumers of one declaration (kills the env-divergence class); one vault-issued credential binding replaces triple re-resolution (kills authority-divergence); one interpreter identity authority is already done (`python_launcher`) and gets subsumed; gateway-owned executability status kills config-as-readiness.
- **The §3.2 leak becomes structurally impossible** for new construction: `CommandRunner` consumes an allowlist-based environment, so the forwarded credential variable is never inherited by user code.
- **Isolation/recoverability:** unchanged process containment (job object, PID identity, cleanup verification all stay); typed seams make restart/recovery *possible to add later* without redesign.
- **Latency:** zero added IPC on the hot path (model/PDB/tools unchanged); verifier child adds one process spawn per evaluation (~100–300 ms on Windows, off the interactive path's critical perception window, and the verifier already dominates wall time with four clean-workspace command runs).
- **Process complexity:** +1 short-lived child per verification only.
- **Windows behavior:** all new seams reuse existing Windows authorities (job object, python_launcher, taskkill ladder); no new Win32 surface.
- **Debugging complexity:** improves — one place to inspect "what environment did this session intend".
- **Credential exposure:** strictly reduced (single vault resolution; execution env excludes credential variables).
- **Reproducibility:** improves — `ExecutionEnvironment` is fingerprinted into session provenance, so replay can assert the environment identity that produced the evidence.
- **Testability:** each seam testable in isolation with fakes; existing 218-test suite keeps passing because behavior is preserved stage-by-stage.
- **Cost/risk:** moderate; highest-risk stages are the source-file seams (V2-01) and verifier move (V2-03), each mitigated by keeping the old path until parity is proven.
- **Verdict: recommended.** It is the smallest change whose failure-prevention claim is about *classes*, not instances — and it matches how this repository has successfully evolved (explicit contracts, fail-closed, typed) rather than how it hasn't (no evidence of process-splits earning their cost).

### 5.3 Alternative C — full control-plane / execution-plane process separation

A long-lived control runtime (session state machine, controller, gateway, journal) in one process; a disposable/restartable executor process owning filesystem/PDB/tests/patching, connected by typed IPC; verifier independent.

- **Prevents observed classes?** Also yes — but not better than B for *any* of them (the incidents were env/credential/identity divergence, which B closes at the declaration level).
- **Latency:** every tool call (repro runs, PDB interactions, patch applies) crosses IPC. PDB especially: the current protocol already batches into a worker, but controller-side tool dispatch would add a full round-trip per `continue/step/locals` — on Windows pipes, against a 5–60 s request budget, meaningful.
- **Process complexity:** two long-lived processes per session + lifecycle state machine for the executor (restart, workspace re-attachment, generation counters) — a new class of bugs (workspace ownership across executor restart, journal/event ordering across two writers) that this repository has never needed.
- **Recoverability:** the theoretical win (executor dies → restart, session continues) requires real checkpoint/resume of controller state (§7), which does not exist and is explicitly *not* justified by any observed failure: today a worker death already classifies honestly (INTERRUPTED), cleans up via job object, preserves the journal, and the operator retries — the failure mode is handled, not open.
- **Windows behavior:** job-object containment must be restructured (which process owns the job?); PID identity handshake generalized; new named-pipe/stdin lifecycle machinery — all new Windows surface, high risk.
- **Scientific reproducibility:** neutral-to-negative; extra nondeterminism sources (IPC timing) in evidence paths.
- **Cost/risk:** the highest in every dimension, for benefits (crash-resume, parallel executors) nobody has requested and no incident requires.
- **Verdict: rejected.** Elegance does not earn its cost here. B's seams make a later *selective* move to C possible if evidence ever demands it (e.g. if long-running interactive debugging sessions with mid-session recovery become a product requirement) — that optionality is worth more than the split itself.

### 5.4 Alternative D (considered, rejected quickly) — replace worker with a vendor agent runtime

Explicitly out of scope by mandate and by repository contract: no vendor-managed agent runtime as primary architecture. The deterministic controller, PDB-first evidence, and local reproducibility are the product; nothing further to evaluate.

**Decision: Alternative B.**

---

## 6. Target V2 topology and boundary contracts

### 6.1 Conceptual planes

```text
CONTROL / ORCHESTRATION PLANE ("brain")
  UI presentation (Textual)
  Session supervision (worker_process)
  Session identity + journal + observability (worker shell, SessionCoordinator)
  DeterministicController + policy + budgets
  ModelGateway (provider selection, route/protocol/credential binding, executability status)
  CredentialVault (resolve + issue ephemeral bindings; the ONLY secret reader)
  History / replay / presentation projections

EXECUTION PLANE ("hands") — behind typed Executor interface, in worker body + children
  Disposable workspaces (TaskWorkspace, isolated git worktree)
  CommandRunner / TestRunner (bounded, env from ExecutionEnvironment)
  PdbSession → PDB worker child (unchanged protocol)
  PatchManager (unified diff, allowed-path policy)
  Process cleanup verification

INDEPENDENT VERIFIER (outside the session worker process)
  LocalProjectVerifier / EvaluationVerifier as a short-lived child
  Own disposable workspaces; own CommandRunner with its own env derivation
  Outcome taxonomy + cleanup proof; sole correctness authority
```

The worker process remains one process in normal operation — but its internal structure now has a hard seam (the tool/registry boundary the controller already uses), a declared environment, and a gateway-owned model channel, so "control" and "execution" are distinguishable, testable, and (only where mandated: verifier) physically separated.

### 6.2 The primitives — what becomes first-class and what must NOT

**`Session`** (evolve existing `SessionSpec`/`SessionCoordinator`): the durable identity + authority object.
- Contents: `session_id`, `task_id`, `AgentDefinition`, `ExecutionEnvironment` (id + fingerprint), task binding, budgets, `retry_of`, provenance.
- Authority ordering (explicit, matches current code): **durable journal is the event/evidence authority; `Session` is the lifecycle authority; UI state is a projection.** The manifest remains derived, never authoritative. The worker remains the only journal writer.

**`AgentDefinition`** — versioned controller/policy identity: controller version, prompt profile, model binding (provider+model+gateway route), allowed tools/capability set, budget defaults, PDB policy. Today these facts exist (prompt profiles were just made explicit in `77a4b3f`; `ControllerRunConfig`, `DemoPolicy`, tool sets) but are assembled ad hoc per source. Making them a typed, fingerprinted object gives scientific runs an immutable treatment identity and gives provenance one place to record "which brain". Verdict: **first-class, low urgency** (V2-01 defines the type; sources populate it).

**`ExecutionEnvironment`** — the keystone. A frozen dataclass, fingerprinted, journaled as provenance:
- interpreter identity (base executable, venv marker policy — subsumes `python_launcher`),
- workspace roots + parent dirs + ownership (worker-owned vs verifier-owned),
- environment allowlist (name→derivation rule: `inherit`, `constant`, `credential-binding-name` — never a value),
- network/trust policy (the existing `PROVIDER_TRANSPORT_NETWORK_ENV_ALLOWLIST` becomes a *declared* policy consumed by both the model channel and execution commands),
- resource limits (timeouts already exist as per-call arguments; declared here for fingerprinting),
- tool availability set.
- **Must NOT contain:** credential *values* (bindings only), provider transport internals (route/protocol resolution stays gateway-side; the environment only says which network policy applies), UI state, journal contents.

**`ModelGateway`** — the narrow interface the rest of the app sees:
- `models()` / `availability()` (presence-only), `resolve(session, agent_def) → ModelBinding {transport config, provenance payload, credential binding}`, `executability(model) → {runnable, blocker}`.
- Internally wraps today's `model_providers` + `provider_connections` + transports + adapters unchanged; the UI stops importing transport vocabulary (transport profile stays in durable config and advanced views only).
- Does **not** remove provider transport identity from durable internal configuration (explicit requirement).

**`CredentialVault`** — provider-neutral credential authority:
- one interface: `resolve(provider_id, endpoint_binding) → EphemeralCredentialBinding {env_var_name, value-lifetime=single transport child}`; backends: Windows Credential Manager (current ctypes/advapi32 implementation), session memory (current), environment (endpoint-bound), CLI auth store (forwarded-as-value, current), future macOS Keychain / Linux Secret Service as new backend *implementations only*.
- Transports/adapters consume the issued binding; they stop knowing which backend produced it and stop re-resolving the ladder. UI→worker hop remains exactly one variable (current mechanism, now vault-issued).
- Strict endpoint/profile binding rules move into the vault (they exist today in `provider_connections.py:2281-2365` and are preserved verbatim).
- No cross-machine secret synchronization (explicit non-goal).

**`Executor`** — an **interface only** in V2 (no new process): the typed execution-service contract the tool handlers implement against — `run_command`, `start_pdb`, `apply_patch`, `syntax_check`, `revert`, `run_tests` — each taking the session's `ExecutionEnvironment`. The existing runtime modules are its implementation. This is the CP/EP boundary made explicit so the controller never touches filesystem/process APIs except through it. (Promotion of this interface to a real process is Alternative C, deferred with explicit triggers, §12.)

**`Verifier`** — the independent correctness authority as a *service boundary*: same code, run as a short-lived child process of the supervisor-side session flow (spawned by the worker shell, not by the execution source), with its own `ExecutionEnvironment` (no credential variables), its own disposable workspaces (current), and its outcome taxonomy unchanged. The controller never sees it except through the typed result. Controller completion remains non-proof (already true).

### 6.3 Boundary contract: worker shell ↔ executor (the brain/hands seam, in-process in V2)

- **Commands crossing (typed, existing vocabulary):** tool invocations (the `ToolSpec` names the controller already emits: `run_reproduction`, `run_regression_tests`, `classify_outcome`, `find_function`, `get_source_window`, `express_root_cause_hypothesis`, `apply_patch`, `revert_patch`, `syntax_check`, PDB actions) — each becomes a declared Executor operation with its existing argument contract.
- **Events crossing back:** existing `ToolResult` + observability events through the shared emitter (unchanged: `SourceInspection`, `PdbLocationChanged`, `PatchProposed/Applied/Reverted`, `ToolCallFailed`…).
- **Identity fields:** session id, run id, task id (existing) + `ExecutionEnvironment` fingerprint on every journaled execution event (new, additive).
- **Cancellation:** the cooperative token (existing semantics exactly: check at safe boundaries, never converted to model/verifier outcomes).
- **Timeouts:** per-operation bounds as today; declared in the environment for fingerprinting, not behavior change.
- **Error taxonomy:** existing split preserved and made explicit at the seam — model-correctable (bounded, sanitized, `recoverable=True` only where declared) vs infrastructure (`ToolExecutionError` fatal kinds, `PdbSessionError`, `WorkspaceError`, `CommandExecutionError`) — never conflated into success (already enforced by `tool_registry` + proof gates).
- **Credential policy:** executor operations receive **no credential variables**; only the model channel (gateway-issued binding) ever does. This is the §3.2 fix.
- **Environment policy:** all executor children build env from the declared allowlist; no `dict(os.environ)` sites remain in execution paths.
- **Workspace ownership:** unchanged (worker-owned session work dir; PDB per-session workspace; verifier-owned export root + four clean workspaces; supervisor post-mortem for worker-owned roots).
- **Restart/recovery (V2 scope):** none — worker death remains terminal for the session, honestly classified, with cleanup; retry (new chained session) remains the recovery story.

### 6.4 Boundary contract: session ↔ verifier child (V2-03, real process boundary)

- Spawned by the worker shell after the controller run ends with an active candidate; short-lived; receives on stdin: the typed `LocalProjectEvaluationPlan` (existing schema, unchanged) **minus any credentials**; its `ExecutionEnvironment` derives from the session's with the credential channel removed.
- Returns: the existing typed `LocalProjectEvaluationResult`/`EvaluationResult` mapping on stdout; worker journals `verifier.*` events (existing kinds, existing `VerifierSessionEventAdapter`).
- Dies/hangs: bounded by the existing plan timeout; failure is an honest verifier failure (existing `EvaluationStatus` values), never a model success; supervisor-side cleanup of verifier-owned workspaces on crash mirrors the existing post-mortem contract.
- Crucially: spawned by the *session shell* (control side), not by the execution source, so the execution path cannot select, parameterize, or short-circuit the verifier beyond supplying the plan.

### 6.5 Failure-domain analysis (recommended architecture)

| Failure | Lost | Durable | Cleaned | Resume? | Must fail? | Evidence remaining |
|---|---|---|---|---|---|---|
| UI process dies | live view | journal (per-record fsync), manifest, artifacts | job object closes → worker tree killed; worker cleanup may not run; supervisor is gone so post-mortem is OS-driven (job kill) — session classifies INTERRUPTED on next open | reopen as replay only | no | full journal to last append; history classification `interrupted` |
| Session worker dies (crash) | in-flight turn, in-memory controller state | journal | supervisor reaps tree, runs post-mortem (work dir, isolated worktree) | no — INTERRUPTED, operator may start retry chain (new session) | honest fail | journal + crash classification + cleanup diagnostics |
| Verifier child dies (post-V2-03) | verification result | journal up to `verifier.started` | verifier workspaces via supervisor post-mortem extension (same pattern as worker post-mortem) | re-verification possible as a new verifier run (owner decision, §12); session itself not resumed | verifier failure ≠ success; session ends UNRESOLVED or FAILED per existing taxonomy | verifier stage events, cleanup proof |
| PDB worker dies | debugger session | PDB events already journaled | `PdbSession.stop` ladder + workspace release (existing) | controller continues without PDB evidence (existing policy paths) | no | bounded PDB observations |
| Adapter child dies | that model request | model provenance, prior steps | process-tree termination (existing) | `LiveModelAdapter` bounded retries (existing) | no | transport error kind, termination reason |
| Provider HTTP timeout | that request | everything journaled | child killed (existing) | bounded retries then honest `model_error` (existing) | no | `LiveTransportError` kind, timing |
| Model protocol violation | the directive | everything journaled | n/a | bounded directive-repair attempts (existing, `9fab308`) | eventually directive-exhausted honest failure | rejected-directive events |
| Credential unavailable | model channel | config, provenance | n/a | no — fail closed before session starts (existing `ScenarioInputError` path) | **yes** (fail-closed is the invariant) | config state, quarantine record |
| Test/repro process hangs | wall time | journal | CommandRunner timeout ladder (existing) | no | timeout status (existing) | bounded output, timeout record |
| Workspace invalid | that operation | journal | cleanup verification flags failure (existing CLEANUP_FAILED) | no — workspace policy failure is terminal for the session (existing) | yes | cleanup events, workspace identity |
| Verifier *fails* (logic) | nothing | full verifier result + taxonomy | four clean workspaces released (existing) | no | outcome is the result (UNRESOLVED/etc.) — never a crash | verification certificate, F2P/P2P records |
| Machine restarts | everything in-memory | session dir (fsynced journal, manifest, artifacts) | nothing automatic — stale work dirs/worktrees are detected by next launch (history classification) and by git worktree prune guidance | no | n/a | durable artifacts intact |

---

## 7. Session persistence: checkpoint/resume assessment

**Recommendation: real checkpoint/resume is deferred (non-goal for V2), with one exception — verifier re-runs.**

Rationale from evidence: no observed failure class is *caused* by the lack of resume, and the current honest-INTERRUPTED + retry-chain + full-journal behavior satisfies the audit contract. Checkpoint/resume would require persisting at minimum: controller snapshot (`ControllerSnapshot` is already a typed frozen value — favorable), turn/budget counters (in snapshot), accepted diagnosis (in journal), phase (in journal), workspace generation/identity (today implicit in paths), patch state (`candidate.patch` + applied marker), verifier state (its own result), provider/model binding (already journaled as `MODEL_CONFIGURED`), and execution-environment identity (V2-02 adds it). The hard part is not serialization — it is **re-establishing workspace equivalence** (proving the resumed workspace matches the checkpointed generation) and the credential epoch (bindings must not survive across restarts). Both are solvable but neither is required by current demand; both would add review surface to the most safety-critical paths. The seams V2 introduces (`ExecutionEnvironment` fingerprint, journaled controller snapshot availability) make a future resume feature *designable* without making V2 pay for it. If mid-session recovery for long interactive debugging sessions becomes a product requirement, that is the trigger to revisit (§12) — and Alternative C's process split would then deserve re-evaluation.

---

## 8. Scientific architecture boundary (mandatory constraints)

1. **Treatment qualification never derives from provider availability.** Today the ladder binds qualification to the frozen roster (`is_treatment_eligible` in `scripts/ollama_cloud_command_adapter.py`, qualified profiles via `level32`), and `session_config.py:373` keeps ladder qualification separate from interactive availability. V2 preserves this exactly: `ModelGateway.executability()` is an *interactive* fact; the ladder/qualification authority stays its own contract and is not merged into the gateway.
2. **Prompt/treatment identity:** `AgentDefinition` fingerprints the prompt profile (recently made explicit in `77a4b3f`) so experiments record immutable treatment identity; gateway provenance continues to record provider/model/protocol/route (existing `MODEL_CONFIGURED` payload).
3. **No architecture change may auto-qualify providers for frozen experiments.** Migration stages explicitly do not touch `evaluation/transport_qualification`, the paired-pilot manifests, or the frozen OpenCode Go path (CURRENT_AGENT_ROSTER retains authority).
4. **Verifier independence is strengthened, not weakened**, by V2-03 (physical boundary + own environment + own workspaces).
5. **Offline/deterministic testability is preserved**: every new seam is fakeable (gateway, vault, executor interface, verifier child protocol all have offline test doubles); no stage introduces a network or live-model requirement for tests.

---

## 9. Product/UI consequence (vocabulary, not redesign)

Keep the UI structure; make it stop speaking implementation dialects:

| Current user-facing item | Verdict | Target vocabulary |
|---|---|---|
| "Transport Profile (generic unless a historical endpoint contract is intended)" (add/edit provider dialogs) | **Should not be a primary user choice**; keep in durable config + advanced disclosure | Default "Generic" for new providers; advanced "Endpoint contract" section for the three historical profiles; internal config field unchanged (`transport_profile` stays safety-critical) |
| Credential-source labels ("saved / session only / environment / CLI auth") + "Connected · source" | Keep as **diagnostic detail**; not the headline | Headline is "Connected" or "Not connected — add your API key"; mechanism appears in an advanced details line |
| `Catalog N models · stale/unverified` | Keep (already truthful cached-discovery metadata) | "Catalog: N models (updated …)" with stale marker; refresh remains explicit |
| Per-model `available` + reasons | Keep | "Ready" / "Needs API key" / "Protocol unsupported" (gateway-issued, actionable) |
| Statuses `Configured / Credential available / Catalog reachable / Model runnable / Runtime tested / Connected` | **Define precisely; do not show all** | `Configured` = provider row exists and is enabled (no credential claim). `Credential available` = vault resolves a usable binding now (presence-only). `Catalog reachable` = NOT a status (no offline reachability claims) — only "catalog refreshed at T". `Model runnable` = gateway executability check passes (protocol × auth × route possible). `Runtime tested` = historical per-model fact ("last succeeded <date>"), never predictive. **`Connected` = the single user-facing state = Configured ∧ Credential available.** No status ever implies scientific qualification (§8) |

---

## 10. Security model (target, least authority)

**Where secrets may exist:** UI process memory (vault resolution), the single worker-spawn env hop (one variable, value only, `worker_process.py:136-151` — mechanism unchanged), the adapter child env (vault-issued binding), the OS secure store, and (legacy routes) the operator CLI auth store read in place at the vault layer.
**Where they must never exist:** argv, the start message, scenario params, journals, events, manifests, reports, UI text beyond presence-only labels, checkpoints (none), diagnostics (existing `contains_credential_shape` scrubbing stays), and — **after V2-02 — any execution-plane child env** (today's `CommandRunner._build_env` full-inherit is the one deviation; V2-02 closes it).
**Who may resolve credentials:** only the vault, in the UI process (session start) and in the adapter child via the issued binding. The worker never re-resolves ambient credential state (V2-05 removes `provider_transport_environment`'s second resolution in favor of the forwarded binding).
**Does the executor need provider credentials?** No — and after V2-02 it structurally cannot see them.
**Does the gateway need workspace filesystem access?** No — it constructs transport configs and bindings only; it never touches execution workspaces (already true; the interface makes it a rule).
**Environment allowlisting:** one declared `ExecutionEnvironment` allowlist per session; both model channel and execution commands derive from it; the TLS/proxy subset (`provider_connections.py:648-657`) becomes a declared policy consumed on both sides (currently model-side only).
**Endpoint/credential binding:** preserved verbatim (vault-owned): ambient sources are canonical-endpoint-bound; saved/session are provider-identity-bound; quarantine blocks resolution (existing rules, `provider_connections.py:2281-2365`).
**Process identity and containment:** unchanged — Windows job object on the worker tree, PID identity via `python_launcher`, per-command tree-kill ladders, verifier child inside the same job (killed on escalation) but outside the worker process.

---

## 11. Migration strategy (vertical, incremental)

Each stage leaves the repository runnable, is testable, has one compatibility seam, and never requires touching UI + providers + debugger + verifier simultaneously.

### V2-01 — Session/launch contract + Executor interface (typing only)
- **Outcome:** `SessionLaunch` (spec + environment id + gateway binding references) replaces ad-hoc `SessionWorkerProcess` kwargs; `AgentDefinition` type introduced and journaled (additive event field); `Executor` interface defined with the existing tool operations; `local_project_source` (and siblings) construct the typed objects but behavior is byte-identical.
- **Affected:** `application/worker_process.py`, `application/worker.py`, `application/session.py`, `application/local_project_source.py`, `ui/app.py` (call sites only).
- **New contract:** `SessionLaunch`, `AgentDefinition`, `Executor` (interface).
- **Compatibility:** old kwargs accepted via a thin adapter for one stage; tests unchanged and green.
- **Validation:** worker-process integration tests (`tests/integration/test_worker_process.py`), local-project tests, `python -m agentic_debugger.demo` offline run.
- **Rollback/exit:** revert the commit; no durable format changed yet (new event fields are additive-only; if the additive field proves premature, drop it before any frozen run consumes it).

### V2-02 — ExecutionEnvironment + execution-plane environment authority (the leak fix)
- **Outcome:** typed `ExecutionEnvironment` (interpreter identity, workspace roots, env allowlist rules, network policy, limits) constructed once per session; `CommandRunner`/`CommandTransport`/`PdbSession` env construction consumes it; **user repro/verify and verifier commands stop inheriting the full environment** (§3.2 leak closed); environment fingerprint journaled in provenance.
- **Affected:** `runtime/command_runner.py`, `runtime/python_launcher.py` (subsumed, kept), `application/command_transport.py`, `runtime/pdb_session.py`, sources, `local_project.py` (workspace identity).
- **New contract:** `ExecutionEnvironment`; the allowlist rule vocabulary; fingerprint in session provenance.
- **Compatibility:** the allowlist initially includes the ambient set minus credential variables, so real user scripts keep working; a compatibility flag (`AGENTIC_DEBUGGER_EXECUTION_ENV_V1=full`) documents the delta during one release, then is removed.
- **Validation:** new unit tests asserting credential variables never reach execution children (the falsifying test for this stage); existing command-runner/transport/PDB tests; one real Windows manual session with a repro script that reads its environment (verification artifact recorded in the stage's review notes).
- **Rollback/exit:** env behavior is the one user-visible change; exit criterion = no regression in repro/verify behavior across curated fixtures and one real project; rollback = the compat flag.

### V2-03 — Verifier out-of-process (independence boundary)
- **Outcome:** `LocalProjectVerifier.evaluate` runs as a short-lived child spawned by the worker shell with a typed stdin/stdout envelope (existing plan/result schemas); execution sources lose the ability to call it in-process; verifier gets its own environment derivation (no credentials) and supervisor post-mortem cleanup parity.
- **Affected:** `application/verifier_observer.py` (journaling stays), `application/local_project_source.py` (invocation only), `evaluation/local_project_verifier.py` (entry-point wrapper, evaluation logic untouched), `application/worker_process.py` (post-mortem extension), worker shell.
- **New contract:** verifier child protocol (plan in, typed result out, bounded timeout, honest failure taxonomy).
- **Compatibility:** evaluation/dataset harnesses that import the verifier directly keep the in-process path (scientific tooling unchanged); only the product session path moves.
- **Validation:** `tests/integration/test_evaluation_verifier.py` + local-project integration tests + new kill/hang tests for the child; end-to-end offline demo.
- **Rollback/exit:** revert to in-process call; the outcome taxonomy and all verifier logic are untouched, so rollback is a call-site change.

### V2-04 — ModelGateway façade (vocabulary seam)
- **Outcome:** UI and sources consume `ModelGateway` only; `transport_profile`/`route`/protocol vocabulary leaves the UI (except the advanced endpoint-contract disclosure); gateway owns the status vocabulary of §9; the provider core modules stay intact beneath.
- **Affected:** `ui/app.py`, `ui/screens.py`, `ui/session_config.py`, sources, `model_providers.py` (façade methods only).
- **New contract:** `ModelGateway` interface; the defined status meanings.
- **Compatibility:** the façade delegates to existing functions; UI behavior changes are label/organization only (no removal of durable transport identity).
- **Validation:** UI tests (`test_ui_provider_connections.py`, `test_ui_configured.py`), provider integration tests, updated vocabulary assertions.
- **Rollback/exit:** UI can call the old functions for one stage; exit when no UI import of provider internals remains.

### V2-05 — CredentialVault backend interface (portability, last)
- **Outcome:** credential backends (wincred today; session/env/cli sources) sit behind a vault interface; the adapter child consumes the issued binding instead of re-resolving ambient state (V2-02 already stopped worker re-resolution); macOS Keychain / Linux Secret Service become *future backend implementations* with no transport changes (no such support exists today and none is claimed).
- **Affected:** `application/provider_connections.py` (backend extraction), `scripts/provider_direct_api_adapter.py` (binding consumption), tests.
- **New contract:** `CredentialVault` interface + `EphemeralCredentialBinding`.
- **Compatibility:** endpoint-binding/quarantine rules move verbatim; extensive existing credential tests must pass unchanged in behavior.
- **Validation:** the full provider-connections test family (the repo's largest boundary test set), native wincred smoke test, adapter credential tests.
- **Rollback/exit:** backend extraction is internal; exit when the adapter no longer imports `resolve_runtime_credential` from ambient state and all credential tests pass.

**Sequencing rationale:** V2-01 (types) precedes V2-02 (the environment needs the session object) which precedes V2-03 (verifier needs its own environment derivation); V2-04/V2-05 are independent of 03 and ordered last because they are pure seams with lowest urgency. Each stage's review package records the falsification evidence for its own claim.

---

## 12. Recommendation summary (decisive)

1. **Is a V2 architecture change warranted?** Yes — but as contract/seam work (Alternative B), not a process redesign. The observed incidents form a class (undeclared execution-environment/credential/identity state re-derived at multiple sites), and one active instance of that class (credential variables inherited by user repro/verify and verifier commands) is present in current source. The class is worth closing at its generator.
2. **Which alternative?** B — explicit logical planes (`Session`, `AgentDefinition`, `ExecutionEnvironment`, `ModelGateway`, `CredentialVault`, `Executor` interface) in the current process topology, plus exactly one targeted process change: the independent verifier runs as a short-lived child outside the session worker (V2-03). Alternative C is rejected: no observed failure requires long-lived executor processes, and its costs (IPC on every tool call, new Windows lifecycle surface, workspace-ownership-across-restart complexity) are not earned.
3. **What remains untouched?** `DeterministicController` and its policy/state machine; the PDB protocol/worker/session (already the model of the target pattern); the journal/emitter/events/observability evidence chain; patch policy (`PatchManager`); disposable-workspace semantics; the outcome taxonomy; history/replay/presentation projections; the scientific qualification boundary (ladder/roster/prompt-profile identity); frozen experiment paths; dataset adapters; the demo offline path; the Windows job-object containment design.
4. **First implementation slice?** V2-01 (typed `SessionLaunch`/`AgentDefinition`/`Executor` interface with zero behavior change), immediately followed by V2-02 because the environment leak is the one finding with security consequence and is falsifiable by a single test (credential variable must not appear in a repro command's environment).
5. **Explicitly deferred:** real checkpoint/resume (needs workspace-generation equivalence + credential epochs; no current demand — §7); executor as an actual process (Alternative C; revisit only on the trigger below); macOS/Linux credential backends (interface prepared in V2-05, implementations out of scope — no current support is claimed); UI redesign beyond vocabulary; scientific-path verifier changes (in-process evaluation tooling unchanged).
6. **What would make us reconsider?**
   - *Toward C (process split):* recurring worker deaths during long interactive debugging sessions where operators demonstrably lose expensive controller progress; a requirement for mid-session environment/workspace swaps; or PDB/tool latency becoming the bottleneck only because of in-process serialization (none observed).
   - *Toward A (stop V2):* if V2-01/02 seams cannot land without destabilizing the worker boundary tests, or if the environment allowlist breaks a material set of real user repro scripts (V2-02 exit criterion), or if the owner judges the §3.2 leak acceptable risk for a single-operator local tool.
   - *Toward a different cut:* if verifier-out-of-process (V2-03) measurably complicates Windows cleanup or adds user-visible latency to verification, keep it in-process and restore independence by a stricter code-boundary rule instead — that would be a documented deviation, not a failure of B.

---

## 13. Unresolved owner decisions

1. **Verifier re-run policy (V2-03):** after a verifier child dies, should the operator be able to re-run verification on the retained candidate within the same session (new verifier run, same evidence chain), or does the session stay terminal as today? (Recommendation: allow a re-verify action; needs an event-kind addition.)
2. **Execution-environment allowlist contents (V2-02):** the exact inherited-variable set for user repro/verify commands (PATH, SYSTEMROOT, LOCALAPPDATA, HOME, …) beyond the credential exclusions — a product decision affecting real user scripts; the stage proposes the minimal set with the compat flag, but the final list needs owner sign-off.
3. **Advanced endpoint-contract disclosure (V2-04):** confirm the UI keeps a discoverable advanced control for the three historical transport profiles (recommended) versus hiding it entirely from the UI (config-file only).
4. **`Runtime tested` historical status (V2-04):** whether to record and display last-success timestamps per provider/model (pure UI metadata, no authority) or omit the concept.
5. **Checkpoint/resume trigger:** confirm deferral (§7) and agree on the product signal that would reopen it (e.g. interactive long-session recovery requests).

---

## 14. Source references (validation)

All current-state claims in this document trace to the following inspected source/tests (baseline `4606933`):

- Worker topology: `agentic_debugger/application/worker_process.py`, `worker.py`, `worker_protocol.py`, `worker_scenarios.py`; `tests/integration/test_worker_process.py` (handshake, crash classification, pre-start cancel, journal catch-up).
- Sources and execution interleave: `application/local_project_source.py` (1,333 lines — controller+tools+PDB+patch+transport+verifier in one function), `local_source.py`, `configured_source.py`, `deterministic_source.py`, `ollama_cloud_source.py`.
- Providers/credentials: `application/model_providers.py`, `provider_connections.py` (credential ladder 2319-2509; endpoint binding 2281-2316; network allowlist 648-694; wincred 432-538), `provider_http.py`, `command_transport.py:153-186`, `scripts/provider_direct_api_adapter.py:132-148`; `tests/unit/test_configured_provider_params.py`, `tests/integration/test_ui_provider_connections.py`, `test_ladder_unified_provider_runtime.py`, `test_provider_direct_api_session.py`.
- Environment sites (incl. the leak): `runtime/command_runner.py:293-296` (`_build_env = dict(os.environ)`), `evaluation/live.py:523` (minimal env), `command_transport.py:153-186` (merged env), `runtime/pdb_session.py:390-405` + `runtime/python_launcher.py` (venv/PID authority), `worker_process.py:309-320` (credential hop).
- PDB: `runtime/pdb_session.py`, `pdb_worker.py`, `pdb_protocol.py`; `tests/integration/test_pdb_session_integration.py`, `test_pdb_interactive_controls.py`.
- Verifier: `evaluation/local_project_verifier.py` (four-clean-workspace evaluation, 304-470), `evaluation/verifier.py`, `outcome_taxonomy.py`; invoked in-process at `application/local_project_source.py:1120-1156`; `tests/integration/test_evaluation_verifier.py`, `test_verifier_observability.py`.
- Journal/history/replay: `application/journal.py` (fsync-per-append, 119-194), `emitter.py`, `events.py`, `history.py` (manifests/reopen/classification), `replay.py` (read-only cursor — "not resume" claim); `tests/unit/test_application_journal.py`, `test_application_history.py`.
- Controller/tools: `agent/controller.py:845-930` (cancel-check contract), `tool_registry.py`, `skills/`; `tests/unit/test_controller.py`, `test_controller_cancellation.py`.
- UI vocabulary: `ui/screens.py:1277-1282,1520-1528,1928,2140`, `ui/session_config.py` (availability vs qualification separation), `ui/app.py:476-494,655-671,895-911` (credential hop + worker spawn).
- Windows harness: `application/process_tree.py` (job object, suspended spawn, kill ladders); roster/scientific boundary: `CURRENT_AGENT_ROSTER.md`; prompt-profile identity: commit `77a4b3f`.

Validation performed for this plan: documentation-only static inspection of the above (no behavior changes, no tests executed beyond read-only checks, no provider contact, no WSL/dataset execution, no credential operations). No full suite run — docs-only candidate per repository validation policy.
