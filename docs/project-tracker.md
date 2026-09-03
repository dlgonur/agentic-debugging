# Agentic Debugging Project Tracker

## Current state

**GOAL-MODE CYCLE 3 — COMPLETE (2026-08-29).**

- Release tag: `v0.1.0` at `d01f7a5` (cycle 1 closed).
- Cycle 3 (owner-authorized goal mode): shared visual language, welcome
  redesign, full terminal UI polish, and UI bug repair — see `TODO.md`.
- Git: requested local checkpoints were attempted, but the host Git guard
  rejected commit execution; the combined candidate remains split across the
  index and working tree.
- Merge/push: none.
- Full historical tracker:
  `outdated/roadmap/project-tracker-pre-closure-2026-08-28.md`.

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
- [x] Effort projection ("what the agent tried") in workspace modal,
  terminal footer, and exported reports.
- [x] Journal-linked retry (`retry_of_session_id`) with manual `r` and
  bounded auto-retry (0-3, default 1) on retryable failures; the
  remaining chain budget is carried forward so auto-retries=N yields at
  most N automatic retries total, and manual retry starts with zero budget.
- [x] Real product proof: Local Project session on CommandCode GOAT
  `deepseek/deepseek-v4-flash` — RESOLVED, F2P 1/1, P2P 1/1, 13/13 model
  requests ok, model-authored correct one-line patch, 107 s.

- Release tag: `v0.1.0` at `d01f7a5`.
- Mandatory roadmap: complete.
- Active required engineering campaign: provider-platform integrity
  convergence (`fix/provider-platform-integrity-v1`, 2026-09-03) —
  user-owned provider registry routing, endpoint/credential binding
  safety, strict fail-closed persistence, and documentation/CI truth for
  the Model Providers platform.
- Merge/push during the 2026-08-28 closure pass: none.
- Full historical tracker:
  `outdated/roadmap/project-tracker-pre-closure-2026-08-28.md`.

## Accepted system

| Area | Accepted outcome |
|---|---|
| Controller | One fail-closed controller with typed directives, state, policy, and budgets |
| Runtime | Bounded commands, disposable workspaces, patch lifecycle, test runner, PDB protocol/session/worker |
| Verification | Independent baseline, F2P, P2P, syntax, full-suite, cleanup, and immutability authority |
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
