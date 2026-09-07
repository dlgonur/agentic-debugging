# Agentic Debugging Project Tracker

## Current state

**V2 ARCHITECTURE CAMPAIGN — COMPLETE & CLOSED (2026-09-07).**

- Baseline: `e86ac2d25beb5114e9a3c805f6bc468f77905deb`.
- V2 architecture campaign (Candidates 06–30): logical control/execution plane
  separation, role-scoped execution environments, session runtime contracts,
  ModelGateway, CredentialVault, and formal V2-05 verifier process-isolation
  trigger evaluation (NOT JUSTIFIED / DEFERRED). Campaign closed.
- Provider-platform integrity convergence: COMPLETE (2026-09-03).
- Goal-Mode Cycle 3 (shared visual language, welcome redesign, terminal UI polish): COMPLETE (2026-08-29).
- Release tag: `v0.1.0` at `d01f7a5` (cycle 1 closed).
- Current priority: Task 31 repository-wide documentation, project-state, and
  hygiene pass. No next implementation priority has been chosen yet.
- Full historical tracker through cycle 1:
  `outdated/roadmap/project-tracker-pre-closure-2026-08-28.md`.

## 2026-09-07 V2 architecture implementation campaign

- [x] **V2-01 — ExecutionEnvironment authority and secret isolation (commits c8ec944, c42a703):**
  Introduced `ExecutionEnvironment` policy authority deriving role-scoped child
  environments (`PROJECT_COMMAND`, `PRODUCT_PDB`, `VERIFIER`, `CLEANUP`,
  `MODEL_ADAPTER`); control, model, and provider credentials structurally
  excluded from project, PDB, and verifier children; `legacy-project-ambient/v1` bridge;
  `VerifiedExecutionContext` preserved for scientific paths.
- [x] **V2-02 — Session runtime contracts (commits 1bab2f8, 4769b3e, 86fa02b, 1c044fd, d1f58e6):**
  Established `SessionLaunch`, `AgentDefinition`, `EffectiveSessionCapabilities`,
  `ProductExecutor` logical seam, and declarative `ProjectRuntimeEnvironmentSpec`
  (`ProjEnv`) ingress; retired transitional compatibility bridge from normal product path;
  project secret redaction with bounded markers.
- [x] **V2-03 — ModelGateway and truthful provider status (commits 499cbec, fb61385, 62dc37e, 801d175, 101a5a6, 1353726, 47bc5ae):**
  Established `ModelGateway` and `ModelBinding` product seam; truthful status
  semantics separating `Configured`, `Credential ready`, `Model runnable`,
  `Catalog refreshed at T`, `Live verified at T`, and `Runtime succeeded at T`;
  UI vocabulary repair and catalog cache invalidation.
- [x] **V2-04 — CredentialVault provider-secret authority (commits d516a4c, 69b892f, 18a38f8, a542629, 0e8a3e1, 8c680d1):**
  Established `CredentialVault`, separating non-secret `CredentialBinding` from
  ephemeral `CredentialLease`; single-snapshot executable and credential
  authority; opaque in-process ticket binding; removed ambient adapter re-resolution.
- [x] **Post-V2-04 follow-ups resolved (commits 1fda1db, 897eea6, d149dad, 0c72cf0):**
  Unified Local Project model-call ceiling authority (Candidates 26–27); aligned
  `model.configured` event schema with `ModelBinding` provenance (Candidate 28);
  isolated `ModelGateway.default()` configuration roots (Candidate 29).
- [x] **V2-05 — Verifier process-isolation evaluation (commit e86ac2d):**
  Formally evaluated against §5.6 criteria (lifecycle, environment isolation,
  security boundary, operational cost); recorded as **NOT JUSTIFIED / DEFERRED**
  with zero implementation created; physical isolation remains trigger-gated;
  campaign closed.

## 2026-08-29 goal-mode UI work

- [x] Registered a semantic Textual theme shared by TCSS and Rich renderers.
- [x] Rebuilt the welcome surface around the primary Local Project route and
  an explicit failure → PDB evidence → patch → verifier verdict chain.
- [x] Restyled archive, local pre-flight, dialogs, replay panes, event syntax,
  workstream, context panels, and help surfaces into one forensic-console
  language.
- [x] Preserved 80x24 navigation and expanded wide-terminal context; verified
  empty, recorded, local form, workspace, and modal states visually.
- [x] Fixed empty archive open, replay table focus, compact verifier priority,
  and invalid Local Project start affordance.
- [x] Added a durable `DESIGN.md` contract and focused regression coverage.

Validation evidence:

- `python -m compileall -q agentic_debugger/ui` — passed.
- UI rendering contracts — 30 passed.
- Keyboard contracts — 13 passed.
- Local Project form consistency — 9 passed; the final dirty-gate node also
  passed after its assertion was added.
- Home/replay/terminal-size matrix — 12 passed.
- Independent visual finish review — `ready` after one bounded polish round.

## 2026-08-28 goal-mode work

- [x] Committed the previous session's completed public-evidence and
  independent-verifier work after validation (181 tests).
- [x] Closed application defects (liveness wiring, dispatch constants,
  strict provider-adjacent params, canonical inventory, dead code).
- [x] Unified provider platform: Ollama Cloud + OpenCode Go +
  CommandCode GOAT + configured profiles through one registry;
  doctor readiness; provider-grouped Local Project model picker;
  `provider` provenance in `model.configured` (additive schema field).
- [x] Effort visibility: journal-derived "what the agent tried"
  projection in the workspace (`w`), terminal footer, and exported
  reports — committed `2c4fbea`.
- [x] Linked retry: journal-authoritative `retry_of_session_id` through
  the worker protocol into manifests/history; manual `r` retry; bounded
  auto-retry (0-3, default 1) for retryable Local Project failures —
  committed `2c4fbea`.
- [x] Real end-to-end proof: one Local Project session on CommandCode
  GOAT `deepseek/deepseek-v4-flash` — RESOLVED, F2P 1/1, P2P 1/1, 13/13 model
  requests ok, model-authored correct one-line patch, 107 s.

## Accepted system

| Area | Accepted outcome |
|---|---|
| Controller | One fail-closed controller with typed directives, state, policy, and budgets |
| Control/Execution plane | Logical plane separation via `ExecutionEnvironment` (role-scoped least authority), `SessionLaunch`, `ProductExecutor`, `ModelGateway`, and `CredentialVault` |
| Runtime | Bounded commands, disposable workspaces, patch lifecycle, test runner, PDB protocol/session/worker |
| Credential authority | `CredentialVault` separating safe `CredentialBinding` provenance from ephemeral `CredentialLease`; secret non-serialization; OS secure store |
| Model gateway | `ModelGateway` & `ModelBinding` product seam; user-owned provider registry with truthful status facts |
| Verification | Independent baseline, F2P, P2P, syntax, full-suite, cleanup, and immutability authority in clean disposable workspaces |
| Evidence | Strict JSON-compatible events, durable journal, replay, golden trajectories, frozen evaluation traces |
| Product | Terminal application with live/replay history, configured command, user-owned provider platform with capability ladder, and local-project debugging |
| Research | R1-R6, RAG/comparison/preference infrastructure, Level-32 repaired treatment and matrix |
| Datasets | Five curated fixtures; QuixBugs infrastructure; BugsInPy fail-closed license gate |

## Scientific results

- [x] R1: real repaired-interface breakpoint and PDB observation.
- [x] R2: multi-turn breakpoint, stack, locals, step/next, and diagnosis.
- [x] R3: debugger evidence to patch to independent verifier RESOLVED.
- [x] R4: model-generated regression test failed buggy and passed repaired code.
- [x] R5: clean base-14B curated holdout 5/5; zero findings in 41 leakage-audited prompts.
- [x] R6: tuned 7B task-disjoint QuixBugs validation 8/8 RESOLVED; no matched-base causal claim.
- [x] Exact-PDB ladder: accepted 6/100, 12/100, and 18/100 single-task proofs.
- [x] Repaired Level-32: GLM 5.1 and GLM 5.2 authoritative resolutions;
  frozen 15-model matrix complete.

Detailed evidence: `docs/results-index.md`.

## 2026-08-28 closure work

- [x] Restored all accepted session sources to the Start task picker while
  preserving the frozen ladder order and treatment metadata.
- [x] Made the user-visible cancellation request state immediate and stable;
  the durable worker cancel event remains the journal authority.
- [x] Reconciled stale integration expectations with the accepted streaming
  Ollama transport and `directive_rejected` classification.
- [x] Confirmed campaign ledger `updated_at` uses finalization time and request
  budget exhaustion is raised before provider process launch.
- [x] Moved superseded documentation into `outdated/` and repaired current
  navigation plus the frozen delivery-manifest verifier.
- [x] Closed optional OpenCode and blocked BugsInPy entries as explicit negative
  boundaries, without running or relabeling them as successes.

## Validation evidence

- `python -m pytest --collect-only -q` — 6002 tests collected.
- Focused application/UI run — 38 passed.
- Cancellation regression — 3 consecutive passes after repair.
- Broad application/UI run — 746 passed; five stale expectations identified.
- Repaired nodes — 8 passed.
- `python -m compileall -q agentic_debugger/ui` — passed.
- `python scripts/verify_delivery_manifest_hashes.py` — 13/13 matched.
- Core release package — 1145 passed in 718.17 seconds.
- Full-suite attempt — 76 passed before controlled interrupt at 1071.50
  seconds and about 1%; no failure observed, not claimed as complete.
- `python -m compileall -q agentic_debugger scripts` — passed.
- Offline deterministic demo — both policy cases RESOLVED; PDB observations
  5/5; F2P 1/1 and P2P 2/2; provider/network attempts 0.
- Technical Word report — eight rendered pages visually inspected; a11y
  findings 0/0/0; style lint passed; all nine table geometries exact; DOCX
  package and placeholder scans passed.

Detailed boundary: `docs/release-closeout-2026-08-28.md`.

## Closed boundaries

- BugsInPy: license-gated, not executed.
- OpenCode Go six-case campaign: optional path retired, not executed.
- Stronger R6 holdout: `INCOMPLETE_HARDWARE_STOP`, not a completed benchmark.
- Fine-tuned + RAG: partial and not evaluated for correctness.
- DPO: not justified.
- Capability escalation: paused at the accepted Level-32 boundary.
