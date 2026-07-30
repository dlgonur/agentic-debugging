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
- [x] 0.1.4 Extend the consolidated diary through 30 July 2026, including Tasks 10A, 10B-R1/R3, the Zen matrix, the R4 audit, and the R5 source closeout.

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
- [x] 1.3.2 Verify debug-gym.
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

- [x] 1.5.1 Read ChatDBG system details.
- [x] 1.5.2 Read SWE-Agent ACI details.
- [x] 1.5.3 Read OpenHands architecture details.
- [x] 1.5.4 Read AutoCodeRover retrieval and SBFL details.
- [x] 1.5.5 Read Agentless localization/repair/validation details.
- [x] 1.5.6 Produce system capability matrix v1.

---

## 2. Phase 2 — Dataset Research

- [x] 2.1 Research debugging and bug-fix datasets on Hugging Face and open-source platforms.
- [x] 2.2 Compare SWE-bench, SWE-bench Lite, SWE-bench Verified, BugsInPy, Defects4J, and QuixBugs.
- [x] 2.3 Select datasets suitable for fine-tuning, RAG, and evaluation. (BugsInPy primary; QuixBugs fallback; sequencing decisions recorded in Dataset and Evaluation Decision v1.)
- [ ] 2.4 Analyze datasets and prepare train/test splits.

### 2.x Subtasks / Log

- [x] 2.1.1 Build dataset inventory. (Dataset and Evaluation Decision v1.)
- [x] 2.2.1 Verify SWE-bench variants. (Primary official sources recorded; execution deferred.)
- [x] 2.2.2 Verify BugsInPy. (Primary official paper/repository sources recorded; execution deferred.)
- [x] 2.2.3 Verify Defects4J. (Primary official repository/paper sources recorded; Python/PDB track no-go-for-now.)
- [x] 2.2.4 Verify QuixBugs. (Primary official repository/paper sources recorded; fallback decision.)
- [x] 2.3.1 Decide first evaluation dataset. (BugsInPy primary; QuixBugs fallback; five curated fixtures as smoke gate.)
- [x] 2.4.1 Prepare small reproducible Python bug subset (five curated pytest-compatible fixtures; Task 6).

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
- [x] 4.3 Develop file-read, code-search, test-run, and patch-apply tools.
- [x] 4.4 Create the debugging agent.
- [ ] 4.5 Make the model localize faults, identify root cause, and generate patches.

### 4.x Subtasks / Log

- [ ] 4.1.1 Define repository indexing strategy.
- [x] 4.3.1 Build deterministic file-read tool.
- [x] 4.3.2 Build deterministic code-search tool.
- [x] 4.3.3 Build deterministic test-run tool.
- [x] 4.3.4 Build deterministic patch-apply tool.
- [x] 4.4.1 Build single-agent controller loop.
- [x] 4.5.1 Add localization output.
- [x] 4.5.2 Add root-cause explanation output.
- [x] 4.5.3 Add patch proposal output.
- [x] 4.5.4 Add verifier pass.
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
- [x] 4.6.11a MVP Task 8 — Golden Trajectories v1 (commit ab9b8b7181524acf329b25f3547eb8a9e0695228)
- [x] 4.6.12 MVP Task 9 — First End-to-End Demonstration (commit e7031fa796a738fc80de4c673607eee72254ce56)
- [x] 4.6.13 Task 10A — Real-Model Evaluation Harness v1 (commit 14a0287a763553038549eb8d84d6d9f8a432f44a)
- [x] 4.6.14 Task 10B-R1 — Live Protocol and Accounting Repair v1 (commit 2996f16f7c95baf0860d0736d8ab67d13af60b9e; protocol 1.1)
- [x] 4.6.15 Task 10B-R3 — Invalid Directive Retry Feedback v1 (commit 1bb1d5251cc732f331ce2f5fdd163d9e46309d29; protocol 1.2)
- [x] 4.6.16 Task 10B-R4 — Offline PDB-policy Directive-Path Audit (no live/model call)
- [x] 4.6.17 Task 10B-R5 — Policy-Scoped Live Contract Repair v1 (commit 63fa27cc4d30490b9770ead3ce14b4b6d3ddf222; protocol 1.3)

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

- [x] 6.1 Develop a debugger adapter for PDB, GDB, or LLDB. (Completed for PDB only; GDB and LLDB remain unimplemented.)
- [ ] 6.2 Enable the fine-tuned model to generate debugger commands and interpret outputs.
- [x] 6.3 Enable breakpoint placement, variable inspection, stack trace reading, and step-by-step debugging.
- [x] 6.4 Enable patch generation and test validation after debugger interaction.

### 6.x Subtasks / Log

- [x] 6.1.1 Start with PDB only.
- [x] 6.1.2 Define PDB command schema.
- [ ] 6.1.3 Implement post-mortem PDB entry for failing Python script/test.
- [x] 6.2.1 Serialize debugger outputs into model-readable structured text.
- [x] 6.3.1 Support stack inspection.
- [x] 6.3.2 Support local variable inspection.
- [x] 6.3.3 Support expression evaluation.
- [x] 6.3.4 Support stepping / next / continue.
- [x] 6.4.1 Feed debugger findings into patch proposal.
- [x] 6.4.2 Validate patch with tests.

---

## 7. Phase 7 — Evaluation and Final Report

- [ ] 7.1 Evaluate results by success rate, localization accuracy, test pass rate, cost, and runtime.
- [ ] 7.2 Prepare a working agentic debugging demo and technical report.

### 7.x Subtasks / Log

- [x] 7.1.1 Define localization metric (`CORRECT_TARGET_SYMBOL` and related localization outcomes).
- [ ] 7.1.2 Define root-cause explanation metric.
- [x] 7.1.3 Define patch correctness metric (verifier outcome, fail-to-pass, pass-to-pass, and full-suite consistency).
- [x] 7.1.4 Define cost/runtime metric (transport timing and provider-reported usage/cost metadata with qualification).
- [x] 7.1.5 Define debugger-action metric (PDB openings, observations, action counts, and policy restrictions).
- [x] 7.2.1 Prepare demo scenario (Task 9 deterministic five-task, two-policy demonstration).
- [ ] 7.2.2 Prepare final technical report outline.

---

## Current Focus

Current active items:

- Task 10B-R4 is complete: the offline PDB-policy directive-path audit identified concrete contract/gating defects without making any live provider, model, OpenCode, or network call.
- Task 10B-R5 is complete and accepted: Policy-Scoped Live Contract Repair v1, source/merge commit `63fa27cc4d30490b9770ead3ce14b4b6d3ddf222`, current protocol version `1.3`.
- This decision branch starts from accepted baseline `51e7dc0faabe84a36d60486c420de9ba0af95878`; documentation changes in this task are intentionally not source changes.
- R5 final validation collected 2,110 tests: 2,108 passed and 2 skipped. The final immutable audit ZIP SHA-256 is `6f65acf77a43b1f44897e2bd3b846a47d63114ec9b59c7b9a38e341a8e0a2e82`.
- The accepted four-case Zen matrix remains descriptive only. Its PDB-on-uncertainty cases opened PDB 0/2 times, so it still supports no causal PDB-effectiveness or policy-superiority claim.
- No new live/model run or dataset execution is authorized or scheduled. Dataset and Evaluation Decision v1 is the documentation-only decision point; the next bounded implementation task is the BugsInPy eligibility manifest and adapter design.
- The decision selects BugsInPy as primary, QuixBugs Python as fallback, and the five current curated fixtures as an architecture smoke gate. RAG is NO-GO-FOR-NOW for a research comparison, SFT is DEFER, and DPO/preference optimization is NO-GO-FOR-NOW.
- Post-MVP research, containment, dataset execution, broader evaluation, model training, and final-report work remains active or deferred as indicated by the phase checkboxes.

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
- Task 10A complete and accepted: Real-Model Evaluation Harness v1, implementation commit `14a0287a763553038549eb8d84d6d9f8a432f44a`.
- Task 10A delivers an explicitly authorized, offline-by-default real-model evaluation harness over the existing integrated runtime. It provides dual explicit live-access authorization before configuration is read, credential-free configuration, credential-shaped configuration and argv rejection, secret-safe events, diagnostics, JSON reports, and human reports, UUID-based evaluation identities, unique namespaces for reports, cases, runs, trajectories, and requests, duplicate task and policy rejection, stable credential-free configuration fingerprinting, full controller/tool-registry/policy/PDB/patch-lifecycle/RunEvent/localization/verifier/cleanup integration, accepted-patch-only verifier submission, static-policy PDB prohibition, positive PDB-enabled live-path validation, bounded model requests/retries/stdin/stdout-stderr/request-timeouts/model-transport timing, explicit unknown provider token fields, non-destructive workspace ownership and cleanup, versioned machine-readable reports, human-readable reports, authoritative report-schema validation before configured CLI output, coherent resolved/unresolved/rejected/failed/cleanup-failed/interrupted/partial semantics, deterministic local fake and fault-injection validation, and no external provider execution during Task 10A.
- Task 10A does not claim that a real model solved any task, does not claim PDB improves model performance, and does not claim a provider-specific integration has been validated.
- Task 10B-R1 complete and accepted: Live Protocol and Accounting Repair v1, accepted implementation/merge commit `2996f16f7c95baf0860d0736d8ab67d13af60b9e`. It exposed truthful state-specific action and transition contracts and preserved unique transport-attempt identities, bounded rejection diagnostics, and usage accounting for provider-completed invalid model responses. The live wire protocol version became `1.1`.
- The private Task 10B live runner remains operator tooling outside this repository. The original controlled live baseline evidence package (SHA-256 `87ac568c74aaa4b6d2e726003a5a1cafd238215411f691dd3aaa7d46e135db08`) received verdict `ACCEPT`; the baseline received verdict `ACCEPT_WITH_LIMITATION`.
- In that original baseline, the static policy result was `RESOLVED`. The PDB policy terminated with underlying reason `invalid_model_response`; the case-status layer reported `PROVIDER_ERROR`, which is not evidence of a provider outage. The model repeated the illegal action `extract_failing_test`, and PDB was never opened.
- Task 10B-R3 complete and accepted: Invalid Directive Retry Feedback v1, accepted implementation/merge commit `1bb1d5251cc732f331ce2f5fdd163d9e46309d29`. It added bounded, redacted, structured `directive_feedback` after provider-completed invalid directives while preserving retry identity, accounting, and transport-failure semantics. The live wire protocol version became `1.2`.
- Task 10B-R3 evidence was archived outside the repository with SHA-256 `4b32ec09a2f6bae58c63c42123bbfd9323711f2c07d4ecc6024c97aaed360b5c`.
- A minimal retry-recovery diagnostic then ran through the private runner. Its evidence package SHA-256 is `4681de9c02ca8f222cf6067293e59a8dd3c1eb605d4ee4be245ddf13e9cea88a`. The diagnostic directly observed one legal recovery after feedback and one later failed recovery in the same case; the case still terminated with `invalid_model_response`, did not attempt a patch, and never opened PDB.
- Private-runner follow-up work added protocol-1.2 compatibility, direct sanitized feedback evidence, episode classification, a locked small repeated matrix profile, per-case stop gates, aggregate budget enforcement, infrastructure exception closure, redaction hardening, and telemetry fail-closed behavior. This tooling remains outside the repository and is not part of the source commit history.
- The final locked matrix used OpenCode Zen provider ID `opencode`, model ID `deepseek-v4-flash-free`, variant `max`, fixture `curated-none-handling-001`, policies `static-baseline` and `pdb-on-uncertainty`, two repetitions per policy, four total cases, and concurrency 1.
- The matrix evidence package SHA-256 is `96675c3995683169c440411deef84429277bcf5289c03375863f6bc65b3ac43d`; the evidence package and matrix execution received verdict `ACCEPT`, while experimental interpretation remains limited.
- Static policy resolved 2/2 cases and produced 2/2 accepted patches. PDB-on-uncertainty resolved 0/2 cases; both terminated with underlying reason `invalid_model_response`, no patch or verifier phase was reached, and PDB openings were 0/2.
- Across all four cases, there were 31 logical model calls, 37 transport attempts, 226,385 provider-reported total tokens, provider-reported cost metadata of 0, and approximately 396.5 seconds wall-clock duration. Provider-reported cost metadata is descriptive and is not proof of actual billing.
- Six corrective-feedback episodes were observed: 4 `RECOVERED_AFTER_FEEDBACK`, 2 `INVALID_AFTER_FEEDBACK`, and 0 `INTERRUPTED_AFTER_FEEDBACK`. This 4/6 descriptive recovery fraction is not a causal estimate or generalized reliability claim.
- The historical OpenCode Go baseline and the OpenCode Zen free-model matrix use different provider routes and must not be pooled as one provider population.
- Because neither PDB-enabled matrix case opened PDB, the matrix still does not measure PDB effectiveness. It supports no claim that static debugging is superior, that PDB is harmful, or that protocol 1.2 caused a higher success rate.
- Task 10B-R4 offline audit completed. It found that the live PDB policy did not fully machine-enforce `decide_pdb_access`, advertised actions outside the exact state/registry/policy/lifecycle/budget intersection, exposed lifecycle-invalid PDB actions, and allowed some state-illegal hypothesis directives to bypass protocol-1.2 corrective feedback.
- Task 10B-R5 repaired the live boundary in four bounded stages: policy-scoped transition/action enforcement; total directive-kind parsing and validator-contract parity; protocol `1.3` plus deep contract detachment; and mandatory exact-registry plus PDB-observation-budget filtering.
- Protocol `1.3` now has one authoritative nested validator-derived action-contract shape. `LiveModelAdapter` fails closed without an exact `ToolRegistry`; no manually maintained flat fallback remains.
- Effective PDB actions are filtered by authoritative budget classification. At zero remaining PDB observations, observation-consuming actions disappear; an active session retains `stop_pdb_session` for cleanup, and hidden exhausted actions receive bounded `illegal_action` feedback before controller execution.
- R5 changed exactly seven tracked files and was accepted after final focused, unit/golden, integration, collection, manifest, hash, CRC, secret-scan, and Git-state review. No live/model/network/OpenCode call occurred.
- Dataset and Evaluation Decision v1 is the current documentation-only decision. Any later dataset or real-model validation requires separate explicit authorization and must remain narrow; the previous matrix must not be reused as evidence of PDB effectiveness.
- The accepted ten-task implementation sequence (Tasks 1–9 plus Task 10A) is complete. Dataset inventory and primary/fallback selection are now documented in Dataset and Evaluation Decision v1; external dataset execution, training-data work, fine-tuning, RAG beyond the implemented tool foundations, DPO/RLHF, broad benchmarking, and later technical evaluation work remain deferred, partial, or not started where indicated by the phase checkboxes.
- Hostile-code filesystem, process and network containment remains deferred.
- Adaptive PDB gating and Tier 3/supporting-paper reading remain deferred. BugsInPy is selected as the primary external target, but its eligibility manifest, adapter, containment, and execution remain future work.
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
- [x] Task 10A — Real-Model Evaluation Harness v1 (implementation commit 14a0287a763553038549eb8d84d6d9f8a432f44a)
- [x] Task 10B-R1 — Live Protocol and Accounting Repair v1 (accepted implementation/merge commit 2996f16f7c95baf0860d0736d8ab67d13af60b9e; protocol version 1.1)
- [x] Controlled live baseline run (private-runner operator tooling; evidence package verdict ACCEPT; baseline verdict ACCEPT_WITH_LIMITATION)
- [x] Task 10B-R3 — Invalid Directive Retry Feedback v1 (accepted implementation/merge commit 1bb1d5251cc732f331ce2f5fdd163d9e46309d29; protocol version 1.2)
- [x] Minimal controlled retry-recovery diagnostic (private-runner operator tooling; mixed episode result; no PDB opening)
- [x] Private-runner feedback evidence, episode classification, locked matrix, and enforceable stop-gate hardening
- [x] Four-case OpenCode Zen descriptive matrix (2 static + 2 PDB-on-uncertainty; exact locked order; matrix/evidence accepted; PDB openings 0)
- [x] Offline PDB-policy directive-path audit (Task 10B-R4; completed without live/model call)
- [x] Task 10B-R5 — Policy-Scoped Live Contract Repair v1 (commit `63fa27cc4d30490b9770ead3ce14b4b6d3ddf222`; protocol `1.3`; final audit accepted)

## Last Updated

2026-07-30
