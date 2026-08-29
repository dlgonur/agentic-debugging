# Agentic Debugger Roadmap

## Status

**GOAL-MODE CYCLE 3 COMPLETE (2026-08-29, owner-authorized goal mode).**

Release tag `v0.1.0` exists at `d01f7a5`; cycle 1 closed at
`docs/project-closeout.md`. Cycle 3 raised the terminal product from a
functional developer shell to one coherent evidence-led visual system.

## Cycle 3 outcomes

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
  durable root `DESIGN.md`; review evidence is under the cycle's `_ai-review`
  package.

The requested checkpoint commits could not be created because the host Git
guard rejected commit execution. The combined candidate remains split across
the index and working tree for owner/FirstMate review; merge and push were not
attempted.

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

## Follow-up candidates (not hidden debt)

- [ ] OpenCode Go end-to-end Local Project session (adapters proven at
  the transport level and by unit tests; a full product session on the
  subscription remains to be run and recorded).
- [ ] Claude models through CommandCode route via the CLI (works through
  `cmdc`; raw-API `/messages` not implemented).
- [ ] Headless Local Project CLI (the smoke script demonstrates the
  worker path; a tracked operator CLI would make it a first-class entry).
- [ ] Providers management screen (availability + live catalog refresh +
  app-managed configured profiles for curated tasks).

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
