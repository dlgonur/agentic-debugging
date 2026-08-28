# Agentic Debugging Project Tracker

## Current state

**CLOSED — PROJECT CYCLE COMPLETE (2026-08-28).**

- Release tag: `v0.1.0` at `d01f7a5`.
- Mandatory roadmap: complete.
- Active required engineering campaign: none.
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
| Product | Terminal application with live/replay history, configured command, Ollama ladder, and local-project debugging |
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
