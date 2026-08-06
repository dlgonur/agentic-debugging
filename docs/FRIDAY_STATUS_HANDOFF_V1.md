# Friday Status Handoff v1

**Date:** 2026-08-05
**Branch:** the original delivery bundle is accepted and integrated on `main`
at `ab464dd` (the earlier presentation plan/deck/cue delivery commit is
`456f0e9`).
**Source baseline:** `456f0e9a6576aab912f5af5980d756ff4e1e9dc3` — the accepted
presentation plan/deck/cue delivery commit. Campaign infrastructure accepted
through `0abb588`; V4 identity correction accepted through `fc7c85b`. The
original handoff is accepted at `ab464dd`. The 2026-08-06 main-repo
completion hardening is accepted and integrated on `main` at `62deca4`, and
the bounded post-mortem evidence layer (exception-argument work bounds with
the omission marker reserved inside the byte budget, huge-int fail-closed
metadata, no-overread local scan) is part of the accepted presentation state
on `main`. The exact presentation-day tip is recorded by the preflight
`git rev-parse HEAD` check.
**Purpose:** one concise, evidence-backed statement of what the internship
delivers on Friday 2026-08-07, what is partial, what is pending, and what is
blocked. Every status below traces to `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md`
(Layer 1/Layer 2 rules), `TODO.md`, `docs/PROJECT_TRACKER.md`, and
`docs/FINAL_TECHNICAL_REPORT_V1.md`. This document changes no instructor item
status.

---

## 1. Instructor scope snapshot (27 items, byte-identical source)

Source: `docs/INSTRUCTOR_AGENTIC_DEBUGGING_TODO.md` (27 items, all unchecked —
the original list is preserved unchanged).

| Status | Count | Items |
|---|---|---|
| COMPLETED | 7 | 5 (five named systems), 6 (HF/open-platform dataset research), 7 (six-dataset comparison), 10 (model selection, branch-bound), 16 (file-read/code-search/test-run/patch-apply tools), 17 (debugging agent), 27 (working demo + technical report) |
| PARTIAL | 10 | 1, 2, 3, 4 (literature/comparison breadth), 8 (RAG corpus selection gap), 18 (localization/root-cause/patch; no verifier-confirmed live repair), 22 (PDB only; GDB/LLDB missing), 24 (PDB mechanism; zero live PDB observations), 25 (patch+test path; no live verified post-PDB repair), 26 (metrics defined; no completed external-dataset model evaluation) |
| IN PROGRESS | 3 | 9 (dataset analysis/train-validation split: real corpus materialized, audit validation and acceptance pending), 11 (instruction-response transformation: executed, acceptance pending), 12 (QLoRA SFT: implemented and accepted at `3f0d3e7`; final training authorized 2026-08-05, results pending FirstMate artifact review) |
| NOT STARTED | 7 | 13 (base-vs-tuned comparison), 14 (RAG), 15 (fine-tuned+RAG), 19 (preference dataset), 20 (DPO/RLHF), 21 (four-way comparison), 23 (fine-tuned model debugger commands) |
| BLOCKED | 0 | — |
| **Total** | **27** | |

Friday horizon means "active work or honest limitation", never "complete".

## 2. Engineering-stream status

### COMPLETED (accepted, evidence-backed)

- Single-controller agentic debugging platform: typed deterministic tools,
  disposable workspaces, unified-diff patching, replay-verified event
  trajectories (Tasks 1–9, Task 10A, 10B-R1/R3/R5; protocol 1.3).
- Real PDB session path: breakpoints, stack/frame/locals inspection, safe
  AST-allowlisted evaluation, stepping (Tasks 4A–4D).
- Independent verifier as the correctness authority (Task 7), canonical
  fixture immutability, F2P/P2P/full-suite, cleanup.
- Deterministic Task 9 demo: 5 curated tasks × 2 policies = 10 cases; 10/10
  verifier `RESOLVED`, F2P 10/10, P2P 22/22, localization
  `CORRECT_TARGET_SYMBOL` 10/10, workspaces cleaned, 21 scripted PDB
  observations.
- QuixBugs infrastructure baselines (no model, literal upstream gold diffs):
  one-task `gcd` real smoke (`ACCEPT CANDIDATE — REAL SMOKE PASSED`) and
  eight-task gold baseline (8/8 tasks, 49/49 nodes, pinned revision
  `4257f44b0ff1181dedaedee6a447e133219fcebf`).
- Campaign infrastructure and paired-pilot v4 terminal contract on `main`
  through `0abb588`; V4 sanitized fixture/replay identity mapping corrected
  at `fc7c85b` (focused suite 389 passed; bounded full suite 3394 passed, 3
  skipped, six known OpenCode wrapper/transport failures).
- QLoRA experiment implementation (tracked `independent_ai` audit contract
  and run-provenance) accepted at `3f0d3e7` on the unmerged
  `experiment/qlora-patch-pilot-v1` branch (FirstMate implementation review;
  owner suite 3457 passed, 3 skipped, 36 unrelated pre-existing OpenCode
  failures).
- Documentation and decisions: Dataset and Evaluation Decision v1, Model/RAG/
  SFT/DPO Decision Gate v1, Final Technical Report v1 (+2026-08-05 revision),
  Demo Guide v1, Demo Task 9, Friday presentation plan/deck/cue sheet v1.2,
  and this delivery bundle (manifest, preflight checklist, handoff).
- 2026-08-06 main-repo completion hardening (integrated at `62deca4`):
  campaign ledger timestamp provenance; repair of deterministic defects —
  the transport teardown race, the output-drain/transport ordering defect,
  four wrapper/transport test-contract defects, and two environment-gated
  preflight tests made hermetic, with deterministic unit-level regression
  coverage; and the post-mortem PDB entry (TODO 6.1.3) — bounded,
  side-effect-safe structured traceback evidence on unhandled exception,
  107 unique focused tests in `tests/unit/test_pdb_post_mortem.py`. The
  bounded post-mortem evidence layer (exception argument count/byte
  ceilings, huge-int fail-closed metadata, no-overread local scan, omission
  marker reserved inside the byte budget) is part of the accepted
  presentation state on `main`. The remaining nondeterministic family —
  synthetic wrapper-preflight subprocess-chain failures under cumulative
  resource pressure — is NOT repaired and no repair is claimed: the recorded
  full suite is NOT green (3448 passed / 3 skipped / 32 failed), the family
  passes 85/85 in isolation and 395/395 in the heavy subset, and a bounds-v2
  A/B reproduced the identical 32-node failure set on both the clean
  `62deca4` checkpoint and the candidate, classifying the family as
  environmental resource pressure, not candidate-caused.

### PARTIAL / IN PROGRESS (material progress, honest limits)

- Literature review (items 1–4): Tier 1 reading and synthesis packs exist;
  Tier 2/3 items and consolidated reviewed survey remain open.
- Real model evidence (V4 attempt `3b5d7488…`, 2026-08-04): real OpenCode Go /
  DeepSeek V4 Flash interaction; correct diagnosis and one-line fix proposal
  on Case 1; strict hunk-header rejection (`old_count=7`, 6-line body);
  Case 2 applied a patch and visited Validate but exhausted public evidence
  (38,534 bytes) before verifier execution. Zero verifier-confirmed repairs,
  zero live PDB observations, no valid static-versus-PDB comparison.
- Corpus and transformation (items 9, 11): 56,025 candidates → 1,000 train /
  150 validation rows, zero leakage/overlap; owner-delegated independent
  FirstMate AI audit of 75 frozen rows complete externally (39 ACCEPT / 36
  REJECT; an AI audit, not human review); fail-closed validation and corpus
  acceptance pending.
- QLoRA (item 12): one-step real CUDA weight update and adapter save/reload
  succeeded (Layer 2); final training authorized externally 2026-08-05; no
  accepted final-training artifact exists.

### PENDING (authorized but not yet produced)

- Final QLoRA training results (pending FirstMate artifact review; no value
  may be predicted).
- Fail-closed validation of the completed independent AI audit and the corpus
  acceptance decision (items 9, 11).
- Held-out base-versus-tuned generation and comparison (item 13) — remains
  **unauthorized**; the historical freeze record at `3f0d3e7` still carries
  `held_out_generation_authorized: false` (and `final_training_authorized:
  false`, which is historical, not evidence about the 2026-08-05 external
  authorization).

### BLOCKED

- BugsInPy source acquisition and execution: license-gated
  (`docs/BUGSINPY_LICENSE_GATE_V1.md`); metadata/preflight work only.
- Authorized Six-Case Live Campaign: not blocked by code, but not authorized
  and not scheduled; requires fresh operator artifacts against the v4
  manifest (`research/quixbugs/PAIRED_PILOT_V4.json`, canonical SHA-256
  `020dfc1f7b8f23aa96a4d7c7942429e306cc290906abfed5ce96cde22b90354d`) and a
  separate explicit authorization.
- No item on the instructor list is classified BLOCKED (classification result,
  not a success claim).

## 3. Boundary statements that must hold on Friday

- The demo proves the platform with an offline scripted stand-in; it proves
  nothing about model quality.
- The PDB-enabled policy attaches to a driver script with a pre-known
  breakpoint, not the failing test.
- QuixBugs baselines are gold-patch, no-model, infrastructure-only.
- No verifier-confirmed live repair, zero live PDB observations, no valid
  static-versus-PDB comparison.
- QLoRA has a frozen methodology, a leakage-checked real corpus, and an
  accepted implementation; final-training results are pending and must not be
  predicted; held-out generation is unauthorized.
- The owner-delegated independent FirstMate AI audit is an AI audit, not
  human review; corpus acceptance is pending.
- RAG NO-GO-FOR-NOW, SFT DEFER, DPO NO-GO-FOR-NOW are recorded decisions, not
  completions.
- No durable claim rests on ignored `_ai-review/` or `operator/` artifacts.

## 4. Operational notes for the handoff

- Git state: the original Friday delivery bundle is accepted and integrated
  on `main` at `ab464dd` (the earlier presentation plan/deck/cue delivery
  commit is `456f0e9`). The 2026-08-06 main-repo completion hardening is
  accepted and integrated on `main` at `62deca4`; the bounded post-mortem
  evidence layer is part of the accepted presentation state on `main`. The
  exact presentation-day tip is recorded by the preflight `git rev-parse
  HEAD` check; presentation runs from clean `main == origin/main`.
- The daily-requirement item (0.1, one diary page per workday) remains open;
  the diary was extended through 2026-08-06; the 2026-07-20, 2026-07-23, and
  2026-07-24 weekday gaps were backfilled from tracked git evidence, and a
  2026-08-06 entry records the main-repo completion hardening.
- Review package: `_ai-review/friday-main-repo-completion-v1/` (ignored) with
  the candidate patch, changed files, validation logs, claim-to-evidence
  matrix, and delivery artifact inventory.
- Presentation day runs from clean `main == origin/main` containing the final
  accepted delivery bundle files.

## 5. Post-Friday technical task batches

Next logical engineering batches (compact; no execution authorization implied
by listing):

| Batch | Work | Instructor items | Prerequisites / evidence |
|---|---|---|---|
| **B1 — QLoRA acceptance path** | Fail-closed validation of the completed `independent_ai` audit; corpus-quality gate and final corpus acceptance; FirstMate artifact review of the accepted final-training package (training record, adapter, manifest); branch merge of `experiment/qlora-patch-pilot-v1` | 9, 11, 12 | Accepted final-training artifact; validated audit record; `external_artifacts.json` manifest |
| **B2 — Held-out comparison** | Frozen base-versus-tuned generation (same prompt/generation contract, five held-out curated tasks) and independent-verifier evaluation (strict-parser counts, F2P/P2P, RESOLVED) | 13 | B1 accepted; separate held-out authorization |
| **B3 — Authorized six-case campaign** | Fresh operator artifacts (route evidence, authorization, adapter config, attempt identity/root) against `research/quixbugs/PAIRED_PILOT_V4.json`; run the frozen six cases; verifier-authoritative results across all five metric families | 18, 26 | Separate explicit authorization; clean execution commit; real route preflight |
| **B4 — Fine-tuned-model debugger path** | Fine-tuned model generating protocol-valid PDB directives; interpretation of debugger outputs in an accepted trajectory | 23 | B1/B2 accepted |
| **B5 — Literature consolidation** | Finish Tier 2/3 reading (Self-Debugging, DebugBench, Debug2Fix, FramePilot/ADI, EnIGMA, SWE-Doctor); close claims-to-verify; consolidated reviewed survey; tracker Phase 1 closure | 1, 2, 3, 4 | Review route (GPT-5.6 High) output reviewed and tracked |
| **B6 — BugsInPy unblock path** | License/redistribution review resolution; OS/container-level containment upgrade; execution only after both clear | 6, 7, 8, 26 | License authority; containment design accepted |
| **B7 — Long-term model work** | RAG over repo code/tests/issues/errors (14), fine-tuned+RAG integration (15), preference dataset (19), DPO/RLHF (20), four-way comparison (21), GDB/LLDB adapters (22) | 14, 15, 19, 20, 21, 22 | B1–B3 accepted; separate decisions |

Housekeeping item (resolved 2026-08-06 at `62deca4`): the campaign ledger
`updated_at`, the create-once `terminal-commit.json` `created_at`, and the
post-campaign authority `observed_at` timestamps now reflect the actual
finalization/detection time rather than the campaign-start `reference_time`;
only the ledger `created_at` and the pre-campaign/in-loop authority gates
keep using the frozen `reference_time` by design.
