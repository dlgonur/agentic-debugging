# Agentic Debugger Roadmap

## Status

**V2 ARCHITECTURE CAMPAIGN COMPLETE & CLOSED (2026-09-07).**
**REPOSITORY-STATE HYGIENE CLOSEOUT COMPLETE (2026-09-07).**
**POST-V2 SESSION TOKEN USAGE TELEMETRY V1 (TASK 34) COMPLETE (2026-09-09).**
**POST-V2 GLOBAL CLI LAUNCH ALIAS V1 (TASK 41) COMPLETE (2026-09-09).**
**POST-V2 PROVIDER-OWNED MODEL REQUEST SIZE V1 (TASK 43) COMPLETE (2026-09-10).**
**POST-V2 UNBOUNDED SESSION PROGRESS V1 (TASK 44) COMPLETE (2026-09-10).**

Release tag `v0.1.0` exists at `d01f7a5`; cycle 1 closed at
`docs/project-closeout.md`. The V2 architecture campaign (Candidates 06–30)
established logical control/execution plane separation, role-scoped execution
environments, session runtime contracts, ModelGateway, CredentialVault,
and closed the V2-05 verifier process-isolation evaluation as NOT JUSTIFIED / DEFERRED.
V2 implementation baseline: `e86ac2d`.

The post-V2 repository-wide documentation and project-state hygiene closeout
(Task 31) is complete. Session Token Usage Telemetry v1 (Task 34) is complete
(2026-09-09) as an additive product telemetry capability. Global CLI Launch
Alias v1 (Task 41) is complete (2026-09-09) as an additive product usability
capability. Universal Model Execution Eligibility v1 (Task 42) is complete
(2026-09-09) separating execution eligibility from scientific qualification.
Provider-Owned Model Request Size v1 (Task 43) is complete (2026-09-10) as a
post-V2 runtime policy correction: model request size is provider-owned
repo-wide and no Agentic-Debugger-owned request-size ceiling remains on any
execution route. Unbounded Session Progress v1 (Task 44) is complete
(2026-09-10) as a post-V2 runtime policy correction: interactive/configured
sessions have no Agentic-Debugger-owned total model-request, directive, or
controller-step execution ceiling repo-wide; progress counters are telemetry
only. No subsequent implementation priority selected. No V3 or
new architecture campaign has been opened.

## V2 architecture outcomes (completed 2026-09-07)

- [x] **V2-01 — ExecutionEnvironment authority and secret isolation:**
  Introduced `ExecutionEnvironment` policy authority deriving role-scoped child
  environments (`PROJECT_COMMAND`, `PRODUCT_PDB`, `VERIFIER`, `CLEANUP`,
  `MODEL_ADAPTER`); control, model, and provider credentials structurally
  excluded from project, PDB, and verifier children; `VerifiedExecutionContext`
  preserved for scientific paths.
- [x] **V2-02 — Session runtime contracts:** Established `SessionLaunch`,
  `AgentDefinition`, `EffectiveSessionCapabilities`, `ProductExecutor` logical
  seam, and declarative `ProjectRuntimeEnvironmentSpec` (`ProjEnv`) ingress,
  retiring the transitional compatibility bridge from the normal product path.
- [x] **V2-03 — ModelGateway and truthful provider status:** Established
  `ModelGateway` and `ModelBinding` as the product model seam; truthful
  history-derived status vocabulary (`Configured`, `Credential ready`,
  `Model runnable`, `Catalog refreshed at T`, `Live verified at T`, `Runtime
  succeeded at T`); UI vocabulary repair and cache invalidation.
- [x] **V2-04 — CredentialVault provider-secret authority:** Established
  `CredentialVault`, separating non-secret serializable `CredentialBinding` from
  ephemeral `CredentialLease`; single-snapshot executable and credential
  authority; opaque in-process ticket binding; removed ambient adapter re-resolution.
- [x] **Post-V2-04 follow-ups resolved:** Unified Local Project model-call
  ceiling authority (Candidates 26–27); aligned `model.configured` event
  schema with `ModelBinding` provenance (Candidate 28); isolated
  `ModelGateway.default()` configuration roots (Candidate 29).
- [x] **V2-05 — Verifier process-isolation evaluation:** Formally evaluated
  against all four §5.6 triggers (lifecycle, environment isolation, security,
  operations); outcome recorded as **NOT JUSTIFIED / DEFERRED** with zero
  implementation created; verifier physical isolation remains trigger-gated;
  campaign closed (Candidate 30).

## Cycle 3 outcomes (completed 2026-08-29)

- [x] Established one semantic Textual/Rich design system: cyan for live
  focus/action, amber for evidence and verifier authority, green for
  independently verified success, and layered blue-black surfaces.
- [x] Rebuilt the welcome screen as a distinctive repair console with a
  direct Local Project route, clear evidence chain, pre-flight context,
  and compact 80x24 behavior.
- [x] Upgraded the session archive, Local Project form, editor dialogs,
  replay workspace, syntax/evidence renderers, and keyboard focus language.
- [x] Fixed the empty-history `O` crash, restored table-first replay focus,
  prioritized verifier status in compact history, and disabled Local Project
  start while pre-flight gates are unmet.
- [x] Added focused regression coverage, wide/compact visual evidence, and a
  durable root `DESIGN.md`.

## Cycle 2 outcomes (completed 2026-08-28)

The owner-directed "make it fly" pass covered multi-provider model access,
failure visibility, and retry.

- [x] Commit the completed-but-uncommitted public-evidence/verifier work
  from the previous session (independent Local Project verifier, public
  evidence gate + CI job, case briefs) — validated, committed `ebc7787`.
- [x] Close real application defects: liveness reporting now wired for
  configured-command and Local Project transports; magic-string scenario
  dispatch replaced with the source-name constant; strict validation for
  is_ollama/ollama_alias; one canonical tracked-file inventory; dead code
  removed — committed `19b3c19`.
- [x] Unified model-provider platform: registry + CommandCode GOAT and
  OpenCode Go subscription adapters, provider-grouped model picker,
  provider provenance in `model.configured`, doctor provider readiness —
  committed `e2741da`, contract alignment `42e0301`.
- [x] Effort visibility: journal-derived "what the agent tried"
  projection in the workspace (`w`), terminal footer, and exported
  reports — committed `2c4fbea`.
- [x] Linked retry: journal-authoritative `retry_of_session_id` through
  the worker protocol into manifests/history; manual `r` retry; bounded
  auto-retry (0-3, default 1) for retryable Local Project failures —
  committed `2c4fbea`.
- [x] Real end-to-end proof: one Local Project session on CommandCode
  GOAT `deepseek/deepseek-v4-flash` reached RESOLVED (F2P 1/1, P2P 1/1)
  with a model-authored correct patch; evidence in
  `_ai-review/goal-mode-2026-08-28`.
- [x] Providers management screen: the Model Providers manager (press
  `m`) owns the user-configured provider registry — availability, live
  GET /models catalog refresh, manual models, secure credentials, and
  deletion — with zero providers on a fresh installation.
- [x] Claude models through CommandCode route: the direct-API route
  resolves Claude-family (`claude*`, `anthropic/*`) CommandCode models
  to the Anthropic Messages protocol deterministically.

## Completed closeout and capability work
 
- [x] Task 31 repository-wide documentation, project-state, and hygiene
  pass after V2 closeout (reconciled current authorities, historical retentions,
  security model, and stale references).
- [x] Task 34 — Session Token Usage Telemetry v1 (2026-09-09): truthful
  provider-reported token usage tracking across live and replay, per-logical-call
  retry/repair aggregation, coverage-aware arithmetic and lower bounds, and
  live/replay UI widgets.
- [x] Task 41 — Global CLI Launch Alias v1 (2026-09-09): dual console-script
  entry point (`agenticdebugger` alongside canonical `agentic-debugger`)
  mapping to `agentic_debugger.ui.__main__:main`; single launch authority;
  argv prog auto-detection in help/version banners; behavioral parity;
  generic `agentic` namespace unclaimed; idempotent Windows helper
  `scripts/install_windows_alias.ps1` deploying to app-owned isolated venv.
- [x] Task 42 — Universal Model Execution Eligibility v1 (2026-09-09):
  Repository-wide hard product invariant: any model made executable by user configuration
  is runnable everywhere execution is supported. Separated scientific qualification
  (controls benchmark/treatment classification) from execution eligibility (controls runnability);
  unlocked Level 32 (`audreyr__cookiecutter-967`) for all executable provider models with
  `Ready Yes`, informational note `"Selected model is outside frozen official Level-32 treatment"`,
  and dispatch to `SourceKind.CONFIGURED_MODEL` with Level-32 budget contract (25 requests/steps,
  3600s, 1 retry, 2 directive repairs); preserved frozen `SourceKind.LEVEL32_OPERATOR` for qualified
  Ollama Cloud models; preserved fail-closed behavior for concrete runtime blockers (missing credentials,
  offline providers).
- [x] Task 43 — Provider-Owned Model Request Size v1 (2026-09-10):
  Repository-wide hard runtime rule: model request size is provider-owned. Removed every
  Agentic-Debugger-owned model-request size ceiling on all execution routes (live-adapter
  request gates, shared prompt-shaping ceiling, OpenCode/CommandCode/Ollama/AGY/direct-API
  stdin and shaping ceilings, common provider-HTTP request-body ceiling, QuixBugs runner
  pre-transport gates, CLI-arg command-line preflights); provider-originated size/context
  rejections surface truthfully as provider failures; historical values retained as
  unenforced provenance only; count/step/retry/time/credential/tool-safety ceilings unchanged.
- [x] Task 44 — Unbounded Session Progress v1 (2026-09-10):
  Repository-wide hard runtime rule: interactive/configured sessions have no
  Agentic-Debugger-owned total model-request, directive, or controller-step
  execution ceiling. Removed count-based termination authority on all generic
  routes (controller `max_model_calls=None`, adapter `max_model_requests=None`,
  provider-adapter logical ceiling `0`=unbounded for CommandCode/Direct-API/
  OpenCode/Ollama/AGY/OpenCode-Go); counters remain observational telemetry.
  Preserved single-operation retry/repair bounds, per-action tool budgets,
  time/phase limits, response/output bounds, security/containment, and the
  isolated frozen Level-32 treatment envelope (40) as provenance for official
  runs only. STEP UI renders `STEP N` with no false `/M` budget.

## Active work

After Task 44, no subsequent implementation priority is selected.
No V3 or new architecture campaign has been opened.

## Product backlog

- [ ] OpenCode Go end-to-end Local Project session (adapters proven at
  the transport level and by unit tests; a full product session on the
  subscription remains to be run and recorded).
- [ ] Headless Local Project CLI (the smoke script demonstrates the
  worker path; a tracked operator CLI would make it a first-class entry).
- [ ] Deterministic regeneration of the README welcome screenshot (the
  current PNG predates the Model Providers home action; no tracked
  regeneration script exists yet).

## Future / trigger-gated architecture (not unfinished V2 debt)

- Checkpoint/resume (§7 trigger: concrete operator requirement for resumable
  sessions after unexpected termination).
- Same-session verifier re-verification (§13 trigger: operator requirement to
  re-verify retained candidates with separate evidence-lineage semantics).
- Physical verifier subprocess isolation (§5.6 triggers: verifier crash/hang
  threatening worker, in-process isolation failure, concrete untrusted-code
  security boundary, or measured operational justification).
- Project-secret storage/synchronization across machines.
- macOS/Linux credential backends for CredentialVault.
- ExecutionEnvironment / VerifiedExecutionContext unification (§6.2a trigger:
  field evidence of divergence causing real defects).

The former chronological TODO is retained at
`outdated/roadmap/TODO-pre-closure-2026-08-28.md`.

## Completed outcomes (cycle 1)

- [x] Single-controller Python/PDB repair architecture with typed directives,
  deterministic tools, explicit budgets, and fail-closed state transitions.
- [x] Disposable workspaces, bounded subprocesses, unified-diff patch lifecycle,
  syntax and regression checks, cleanup, and canonical immutability proof.
- [x] Independent verifier with fail-to-pass, pass-to-pass, full-suite,
  infrastructure, and semantic outcome classification.
- [x] Immutable event schema, journal, replay, golden trajectories, and
  review-safe structured traces.
- [x] Terminal application, configured-command transport, Ollama Cloud route,
  capability-ladder surface, Local Project Debug, history, replay, and Apply To
  Project gates.
- [x] Curated, QuixBugs, RAG, comparison, preference, and license-gated
  BugsInPy adapter infrastructure.
- [x] R1-R6 scientific cycle, exact-PDB capability ladder, repaired Level-32
  artifact boundary, and frozen 15-model matrix.
- [x] Pre-release hardening, feature freeze, release documentation, and local
  `v0.1.0` tag.
- [x] Post-freeze application debt: deterministic/configured modes are reachable
  again; cancellation request state is race-safe.
- [x] Post-freeze documentation debt: campaign finalization timestamps and
  `ModelRequestBudgetExceeded` are confirmed implemented and covered; stale
  debt wording is reconciled in the current tracker/closeout.
- [x] Repository hygiene: superseded status, report, plan, historical delivery,
  conversation-summary, TODO, and tracker material moved under `outdated/`.

## Closed boundaries — not positive completions

- [x] **BugsInPy execution — CLOSED / NOT EXECUTED.** License and redistribution
  authority remain unresolved. Metadata and fail-closed preflight are retained.
- [x] **OpenCode Go six-case campaign — CLOSED / NOT EXECUTED.** The frozen
  operator path is retained as historical infrastructure. It is not required
  for product or scientific closeout and was not run for checkbox completion.
- [x] **Stronger R6 five-task holdout — CLOSED / INCOMPLETE_HARDWARE_STOP.** Two
  tasks produced outcomes; three did not. It is not reported as 1/5 or 2/5.
- [x] **Fine-tuned + RAG — CLOSED / PARTIAL / NOT_EVALUATED.** No RAG success or
  failure claim is made.
- [x] **DPO — CLOSED / NOT JUSTIFIED.** No additional campaign is scheduled.
- [x] **Capability escalation — CLOSED / PAUSED.** Level-32 is the accepted
  stopping point for this research cycle.

New work requires a new owner-approved roadmap. It is not carried as hidden
debt in this closed TODO.
