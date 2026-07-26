# Agentic Debugging Project Tracker

This file is the operational tracker for the internship/project.

Rules:

- The main numbered items must stay aligned with TODO.md.
- Subtasks can be added under each main item as work becomes concrete.
- Keep numbering stable so we can reorder, reference, and discuss tasks precisely.
- Update this file whenever a meaningful research, reading, implementation, or evaluation step is completed.
- TODO.md is the high-level source of truth; this file is the working execution tracker.

---

## 0. Daily Requirement

- [ ] 0.1 Write a one-page internship diary entry for each workday.

### 0.1 Subtasks / Log

- [x] 0.1.1 Created initial diary draft: diary/day_01.md.
- [x] 0.1.2 Created day 02 draft from cross-report synthesis: research/reports/synthesis/diary_day_02_draft.md.
- [x] 0.1.3 Normalize diary entries into final daily format.

---

## 1. Phase 1 — Literature Review

- [ ] 1.1 Research debugging, automated debugging, fault localization, and program repair.
- [ ] 1.2 Study LLM-based debugging work.
- [ ] 1.3 Study agentic debugging, tool-using agents, and multi-agent debugging.
- [ ] 1.4 Compare traditional debugging, LLM-based debugging, and agentic debugging.
- [ ] 1.5 Study SWE-Agent, OpenHands, AutoCodeRover, Agentless, and ChatDBG.

### 1.1 Subtasks / Log

- [x] 1.1.1 Created initial concept notes: research/literature_notes_01.md.
- [x] 1.1.2 Collected three independent AI research reports from Gemini, ChatGPT, and Claude.
- [x] 1.1.3 Archived raw research reports under research/reports/raw/.
- [x] 1.1.4 Created cross-report synthesis pack under research/reports/synthesis/.
- [x] 1.1.5 Download Tier 1 papers into research/papers/tier1_must_read/.
- [x] 1.1.6 Read ChatDBG fully and write research/notes/2024_chatdbg_notes.md.
  - [x] 1.1.6.1 Created ChatDBG note skeleton.
  - [x] 1.1.6.2 Verify official bibliography and title/version differences.
  - [x] 1.1.6.3 Read architecture and take-the-wheel sections.
  - [x] 1.1.6.4 Read Python/PDB integration details.
  - [x] 1.1.6.5 Read evaluation and threats to validity.
  - [x] 1.1.6.6 Extract reusable PDB adapter requirements.
  - [x] 1.1.6.7 Finalize ChatDBG notes.
- [x] 1.1.7 Read debug-gym fully and write research/notes/2025_debug_gym_notes.md.
- [x] 1.1.8 Read Agentless fully and write research/notes/2024_agentless_notes.md.
- [x] 1.1.9 Read SWE-bench fully and write research/notes/2023_swe_bench_notes.md.

### 1.2 Subtasks / Log

- [x] 1.2.0 Download Tier 2 papers into research/papers/tier2_core_sections/.
- [x] 1.2.1 Read LDB / Debug Like a Human.
- [ ] 1.2.2 Read Self-Debugging.
- [ ] 1.2.3 Read DebugBench.
- [ ] 1.2.4 Summarize how LLM debugging differs from static code repair.

### 1.3 Subtasks / Log

- [x] 1.3.1 Read RepairAgent.
- [ ] 1.3.2 Verify debug-gym.
- [ ] 1.3.3 Verify Debug2Fix.
- [ ] 1.3.4 Verify FramePilot / ADI.
- [ ] 1.3.5 Verify EnIGMA.
- [ ] 1.3.6 Verify SWE-Doctor.
- [ ] 1.3.7 Decide which frontier systems are core evidence and which are only supporting references.

### 1.4 Subtasks / Log

- [x] 1.4.1 Write static vs dynamic debugging taxonomy.
- [x] 1.4.2 Write repository-agent vs debugger-agent comparison.
- [x] 1.4.3 Write fault localization vs root-cause analysis comparison.
- [x] 1.4.4 Write APR patch plausibility vs correctness comparison.

### 1.5 Subtasks / Log

- [ ] 1.5.1 Read ChatDBG system details.
- [x] 1.5.2 Read SWE-Agent ACI details.
- [x] 1.5.3 Read OpenHands architecture details.
- [x] 1.5.4 Read AutoCodeRover retrieval and SBFL details.
- [ ] 1.5.5 Read Agentless localization/repair/validation details.
- [x] 1.5.6 Produce system capability matrix v1.

---

## 2. Phase 2 — Dataset Research

- [ ] 2.1 Research debugging and bug-fix datasets on Hugging Face and open-source platforms.
- [ ] 2.2 Compare SWE-bench, SWE-bench Lite, SWE-bench Verified, BugsInPy, Defects4J, and QuixBugs.
- [ ] 2.3 Select datasets suitable for fine-tuning, RAG, and evaluation.
- [ ] 2.4 Analyze datasets and prepare train/test splits.

### 2.x Subtasks / Log

- [ ] 2.1.1 Build dataset inventory.
- [ ] 2.2.1 Verify SWE-bench variants.
- [ ] 2.2.2 Verify BugsInPy.
- [ ] 2.2.3 Verify Defects4J.
- [ ] 2.2.4 Verify QuixBugs.
- [ ] 2.3.1 Decide first evaluation dataset.
- [ ] 2.4.1 Prepare small reproducible Python bug subset.

---

## 3. Phase 3 — Model and Fine-tuning

- [ ] 3.1 Select an open-source code model.
- [ ] 3.2 Convert dataset to instruction-response format if needed.
- [ ] 3.3 Run supervised fine-tuning with LoRA or QLoRA.
- [ ] 3.4 Compare pre-fine-tuning and post-fine-tuning model performance.

### 3.x Subtasks / Log

- [ ] 3.1.1 Defer final model selection until baseline experiments exist.
- [ ] 3.2.1 Draft instruction-response schema for debugger trajectories.
- [ ] 3.3.1 Collect successful debugger trajectories before SFT.
- [ ] 3.4.1 Define pre/post fine-tuning evaluation protocol.

---

## 4. Phase 4 — RAG and Agent Tools

- [ ] 4.1 Build RAG system over repository code, tests, issue descriptions, and error messages.
- [ ] 4.2 Combine fine-tuned model with RAG.
- [ ] 4.3 Develop file-read, code-search, test-run, and patch-apply tools.
- [ ] 4.4 Create the debugging agent.
- [ ] 4.5 Make the model localize faults, identify root cause, and generate patches.

### 4.x Subtasks / Log

- [ ] 4.1.1 Define repository indexing strategy.
- [x] 4.3.1 Build deterministic file-read tool.
- [x] 4.3.2 Build deterministic code-search tool.
- [x] 4.3.3 Build deterministic test-run tool.
- [x] 4.3.4 Build deterministic patch-apply tool.
- [x] 4.4.1 Build single-agent controller loop.
- [ ] 4.5.1 Add localization output.
- [ ] 4.5.2 Add root-cause explanation output.
- [ ] 4.5.3 Add patch proposal output.
- [ ] 4.5.4 Add verifier pass.
- [x] 4.6.0 MVP Task 1 — Foundation Contracts and Event Skeleton v1 (commit 347f74d)
- [x] 4.6.1 MVP Task 2 — Workspace and Command/Test Runtime v1 (commit 778d38c)
- [x] 4.6.2 MVP Task 3 — Source Retrieval and Deterministic Patch Lifecycle v1 (commit e396799)
- [x] 4.6.3 MVP Task 4A — PDB Session Lifecycle and Protocol Foundation v1 (feature/mvp-pdb-session-foundation-v1, commit c8539a4)
- [x] 4.6.4 MVP Task 4B1 — One-Shot Target Run to First Breakpoint v1 (feature/mvp-pdb-breakpoints-execution-v1, commit 84fe9e2)
- [x] 4.6.5 MVP Task 4B2 — Persistent Paused Target Lifecycle Foundation v1 (feature/mvp-pdb-paused-lifecycle-v1, commits 78471cf and 9a921bd)
- [x] 4.6.6 MVP Task 4B3 — Continue/Resume and Additional Execution Control v1 (feature/mvp-pdb-continue-resume-v1, commit e9032dd)
- [x] 4.6.7 MVP Task 4C — Stack, Frame and Locals Inspection v1 (feature/mvp-pdb-inspection-v1, commit 24ecc7a)
- [x] 4.6.8 MVP Task 4D — Safe Evaluation and PDB Integration Hardening v1 (feature/mvp-pdb-safe-evaluation-v1, commit 17a7ebb)
- [x] 4.6.9 MVP Task 5 — Controller State Machine and Tool Policy v1 (feature/mvp-controller-v1, commits 532214e, e2187e2, 365dc49 and 43d00c8)
- [x] 4.6.10 MVP Task 6 — Curated Benchmark Fixtures v1 (feature/mvp-curated-bugs-v1, commit eedcccb)
- [x] 4.6.11 MVP Task 7 — Verifier and Evaluation Runner v1 (feature/mvp-verifier-runner-v1, commit 1b0af78)
- [x] 4.6.12 MVP Task 9 — First End-to-End Demonstration (commit e7031fa796a738fc80de4c673607eee72254ce56)

---

## 5. Phase 5 — Preference Optimization

- [ ] 5.1 Create preference dataset from successful and failed debugging outputs.
- [ ] 5.2 Apply DPO or an appropriate RLHF method.
- [ ] 5.3 Compare base model, fine-tuned model, RAG-supported model, and agentic system.

### 5.x Subtasks / Log

- [ ] 5.1.1 Defer until enough real/debugger trajectories exist.
- [ ] 5.2.1 Defer DPO/RLHF until SFT baseline is measured.
- [ ] 5.3.1 Define comparison protocol after MVP.

---

## 6. Phase 6 — Debugger Adapter

- [ ] 6.1 Develop a debugger adapter for PDB, GDB, or LLDB.
- [ ] 6.2 Enable the fine-tuned model to generate debugger commands and interpret outputs.
- [ ] 6.3 Enable breakpoint placement, variable inspection, stack trace reading, and step-by-step debugging.
- [ ] 6.4 Enable patch generation and test validation after debugger interaction.

### 6.x Subtasks / Log

- [ ] 6.1.1 Start with PDB only.
- [ ] 6.1.2 Define PDB command schema.
- [ ] 6.1.3 Implement post-mortem PDB entry for failing Python script/test.
- [ ] 6.2.1 Serialize debugger outputs into model-readable structured text.
- [ ] 6.3.1 Support stack inspection.
- [ ] 6.3.2 Support local variable inspection.
- [ ] 6.3.3 Support expression evaluation.
- [ ] 6.3.4 Support stepping / next / continue.
- [ ] 6.4.1 Feed debugger findings into patch proposal.
- [ ] 6.4.2 Validate patch with tests.

---

## 7. Phase 7 — Evaluation and Final Report

- [ ] 7.1 Evaluate results by success rate, localization accuracy, test pass rate, cost, and runtime.
- [ ] 7.2 Prepare a working agentic debugging demo and technical report.

### 7.x Subtasks / Log

- [ ] 7.1.1 Define localization metric.
- [ ] 7.1.2 Define root-cause explanation metric.
- [ ] 7.1.3 Define patch correctness metric.
- [ ] 7.1.4 Define cost/runtime metric.
- [ ] 7.1.5 Define debugger-action metric.
- [ ] 7.2.1 Prepare demo scenario.
- [ ] 7.2.2 Prepare final technical report outline.

---

## Current Focus

Current active items:

- No remaining implementation item in the accepted nine-task MVP sequence.
- Post-MVP research, dataset, model, and broader evaluation work remains active or deferred as noted below.

Notes:

- Task 4A complete.
- Task 4B complete.
- Task 4C complete.
- Task 4D complete.
- parent Task 4 complete.
- Task 5 complete.
- Task 5 was fast-forward merged into `main` at `43d00c8`.
- Task 6 complete: five curated pytest-compatible bug fixtures were reviewed, repaired, merged and pushed.
- Task 6 was fast-forward merged into main at eedcccb.
- Task 7 complete: Verifier and Evaluation Runner v1.
- Task 7 was fast-forward merged into main at 1b0af78.
- Task 7 provides authoritative DebugTask loading and validation, disposable workspace preparation, canonical fixture immutability checks, baseline reproduction, F2P/P2P execution, candidate unified-diff application, syntax validation, post-patch reproduction, exact test-node collection, full-suite consistency checks, bounded typed result records, deterministic JSON-compatible mappings, workspace-relative path normalization, cleanup lifecycle reporting, verifier command accounting separate from controller max_test_runs, and trusted-local execution-boundary disclosure.
- Task 7 evaluates trusted local benchmark fixtures and benign candidate patches. It is not an OS-level hostile-code security sandbox.
- Task 8 complete: Golden Trajectories v1 (commit ab9b8b7). At the Task 8 implementation closeout point, main and origin/main point to ab9b8b7.
- Task 8 provides immutable record/replay architecture, RunEvent sequence validation, controller state transition reconstruction, action/observation linkage, semantic trajectory projection and first-mismatch reporting, scripted model sequences with exact model-call accounting (rejecting exhausted/unused outputs), static/PDB-gated/deterministic-rejection trajectories, verifier integration, provider/network attempt guards, portable disposable workspace handling, and exception-safe cleanup across success/rejection/exhaustion/PDB/tool/evaluator/cleanup-error paths.
- Task 8 is not an OS-level hostile-code sandbox and does not claim causal PDB efficacy proof for agentic debugging.
- Task 9 complete and accepted: First End-to-End Demonstration, implementation commit `e7031fa796a738fc80de4c673607eee72254ce56`.
- Task 9 integrated the real controller, tool registry, workspace, test runner, source-skill, PatchManager, PDB session, event replay, and Task 7 verifier paths into an offline, deterministic demonstration over five curated tasks and two policies. The implementation scope was 19 changed files, 6709 insertions, and 75 deletions; no external model-provider execution was used.
- The accepted demonstration covered 5 curated tasks × 2 policies = 10 cases: controller Done 10/10, verifier COMPLETED / RESOLVED 10/10, fail-to-pass 10/10, pass-to-pass 22/22, localization `CORRECT_TARGET_SYMBOL` in all 10 cases, full suite passed for every case, canonical fixtures unchanged 10/10, disposable workspaces cleaned 10/10, provider attempts 0, and network attempts 0.
- Static policy covered 5/5 verifier COMPLETED, 5/5 RESOLVED, 5/5 fail-to-pass, 11/11 pass-to-pass, and 0 PDB observations. PDB-on-uncertainty covered the same 5/5, 5/5, 5/5, and 11/11 results with 21 successful PDB observations.
- The two clean strict demonstration executions produced identical deterministic views: 10 semantic trajectories compared and 0 semantic differences; the generated source-tree digest matched the accepted live tree and no stale summary placeholder values remained.
- Task 9 validation passed: focused Task 9 suite 177 tests; relevant controller/PDB/replay/golden/evaluator regression suite 1229 passed with 2 warnings; full repository suite 2020 passed, 2 skipped, and 5 warnings; compile validation and whitespace validation passed. The skips and warnings were pre-existing. One managed-sandbox `.pytest_cache` permission warning occurred during evidence inventory generation and was not a product defect.
- Static-versus-PDB parity is structural because both policies use the same deterministic offline catalog repair. The demonstration does not establish causal PDB superiority. Provider/network guards measure in-process attempts and are not an operating-system-level network sandbox.
- The accepted nine-task MVP implementation sequence is complete. This does not complete the broader research or internship project: dataset expansion/inventory, training-data work, fine-tuning, RAG beyond the implemented tool foundations, DPO/RLHF, broad benchmarking, and later technical evaluation work remain deferred, partial, or not started where indicated by the phase checkboxes.
- Hostile-code filesystem, process and network containment remains deferred.
- Real model integration, adaptive PDB gating, BugsInPy, and Tier 3/supporting-paper reading remain deferred.
- Planned decomposition:
  - [x] Task 4A — PDB Session Lifecycle and Protocol Foundation
  - [x] Task 4B — Breakpoints and Execution Control
    - [x] Task 4B1 — One-Shot Target Run to First Breakpoint
    - [x] Task 4B2 — Persistent Paused Target Lifecycle Foundation
      - [x] Task 4B2A — Worker-Side Persistent Pause Lifecycle
      - [x] Task 4B2B — Public PdbSession Paused-Target API and Lifecycle Guards
    - [x] Task 4B3 — Continue/Resume and Additional Execution Control
  - [x] Task 4C — Stack, Frame and Locals Inspection
  - [x] Task 4D — Safe Evaluation and PDB Integration Hardening
- [x] Task 4 — PDB Session and Runtime Skills
- [x] Task 5 — Controller State Machine and Tool Policy
- [x] Task 6 — Curated Benchmark Fixtures v1
- [x] Task 7 — Verifier and Evaluation Runner v1 (feature/mvp-verifier-runner-v1, commit 1b0af78)
- [x] Task 8 — Golden Trajectories v1 (commit ab9b8b7)
- [x] Task 9 — First End-to-End Demonstration (accepted implementation commit e7031fa796a738fc80de4c673607eee72254ce56)

## Last Updated

2026-07-26














