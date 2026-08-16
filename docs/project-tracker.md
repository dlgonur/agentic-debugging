# Agentic Debugging Project Tracker

This file is the operational tracker for the internship/project.

Rules:

- The main numbered items must stay aligned with TODO.md.
- Subtasks can be added under each main item as work becomes concrete.
- Keep numbering stable so we can reorder, reference, and discuss tasks precisely.
- Update this file whenever a meaningful research, reading, implementation, or evaluation step is completed.
- TODO.md is the high-level source of truth; this file is the working execution tracker.

---

## Local Application V1

- [x] **Task 1 — Establish application contracts** — ACCEPTED (2026-08-14).
  Added the UI-independent `agentic_debugger.application` contract layer:
  validated immutable session/event contracts, lifecycle and termination
  semantics, live-vs-replay source boundaries, immutable presentation state,
  fail-closed presentation identity/provenance, cleanup lifecycle validation,
  and the stable contract reference
  `docs/architecture/local-application-contracts-v1.md`.
  Acceptance validation: 320 application-contract tests + 234 focused existing
  regression tests = **554 passed**. No controller, verifier, PDB, canonical
  `RunEvent`, trajectory/replay-golden, demo, Textual, GPU, or campaign behavior
  changed. The known local `test_demo_catalog` failure remains environmental:
  gitignored QuixBugs materializations expand the locally discovered catalog.
- [x] **Task 2 — Add incremental controller observability** — ACCEPTED (2026-08-14).
  Added a typed controller-native observer seam plus the application-side
  controller-to-`SessionEvent` prefix adapter. Every native observation carries
  explicit run/task identity; replay-only source kinds are rejected by the live
  adapter; directive acceptance is distinct from subsequent tool success; and
  observer `Exception`s cannot alter controller decisions/results. Cancellation
  remains intentionally deferred to Task 3. Acceptance evidence includes
  **73/73** focused observer/adapter tests and **464/464** directly affected
  application/controller compatibility tests after the repair pass. Previously
  exercised golden, trajectory/replay, demo/live, R1–R3, and QuixBugs controller
  gates remained compatible; known baseline-local failures were independently
  reported as pre-existing. Canonical `RunEvent` 1.0 and post-run trajectory
  generation remain unchanged.
- [x] **Task 3 — Build the cancellable worker boundary** — ACCEPTED (2026-08-14).
  Added the isolated subprocess worker/supervisor boundary, neutral cancellation
  token, controller/runtime cancellation propagation, crash-durable
  `SessionEvent` journal, strict local worker protocol, verified disposable
  workspace cleanup, and fail-closed Windows process-tree containment through a
  kill-on-close Job Object. Full event bodies remain journal-authoritative while
  worker notifications carry sequence information only. Startup, pre-start
  cancel/timeout, journal-fatal, crash, cleanup-failure, cooperative cancel, and
  forced-escalation paths are classified honestly. Final Windows Task-3
  validation: **76 unit + 34 integration tests passed**, including real sleeping
  child and real paused-PDB descendant forced-kill gates plus containment
  create/assign/resume fail-closed gates. Directly affected Task-1/2, controller,
  CommandRunner, events/replay/golden/PDB, and demo compatibility gates remained
  green except the two known baseline-local demo-catalog failures caused by
  gitignored QuixBugs copies. Production deterministic application-source wiring
  remains intentionally deferred to Task 7.
- [x] **Task 4 — Expose patch, source, debugger, and verifier progress** —
  ACCEPTED (2026-08-15). Added structured application observability for real PDB
  locations/stack/locals, bounded safe source snapshots, diagnosis and real patch
  lifecycle, plus optional verifier-stage progress/cancellation. A shared
  `SessionEventEmitter` now provides one session-wide identity/clock/sequence
  authority across lifecycle, controller, debugger/source/patch, and verifier
  producers. Runtime credential-shaped locals are explicitly redacted; unsafe
  source/patch bodies are withheld; UTF-8 source truncation remains inside the
  event contract. Patch application remains distinct from repair correctness and
  final `EvaluationResult` remains verifier authority.
- [x] **Task 5 — Add app-owned history and replay** — ACCEPTED (2026-08-15).
  Added filesystem-backed app-owned history with atomic validated manifests,
  journal/artifact integrity checks, app-owned path containment for register,
  discovery, and reopen, and honest complete/interrupted/malformed states. Replay
  is read-only and uses the same pure presentation reducer as live events.
  Canonical trajectories, R5 evidence, and professor-safe traces have explicit
  read-only adapters that preserve genuine run identity/provenance and represent
  missing facts as not recorded; frozen/historical evidence is never modified.
  Final Goal-mode validation reported **563 application unit tests**, **36
  worker-process integration tests**, **11 verifier-observability integration
  tests**, plus PDB/canonical-replay/golden/controller/demo/verifier/patch
  compatibility gates. FirstMate independently reconstructed the candidate and
  confirmed 529 directly available application tests; the only two unavailable
  tests required R5 run artifacts absent from the supplied repository snapshot.
- [x] **Task 6 — Build the replay-first Textual application** —
  ACCEPTED (2026-08-15). Added the optional Textual 8 application surface and a
  documented launch path (`python -m agentic_debugger.ui`). Home/history and the
  full session workspace render exclusively from the accepted
  `SessionViewState`: source/current-line, debugger stack/locals, patch
  lifecycle/diff, verifier authority, activity, timeline, and read-only replay
  controls. Replay remains execution-free. Repair Pass 1 fixed sequential-run
  ownership, successful Start-screen replacement (`Home -> Workspace`), and
  literal Rich rendering of recorded evidence. Headless validation reports
  **65/65 UI tests**, including real Start-session navigation, sequential
  sessions, cancellation, history/replay, resize/adversarial coverage, and
  markup-safety tests. The scientific/core install remains Textual-free unless
  the optional `app` extra is installed.
- [x] **Task 7 — Wire deterministic live sessions** — ACCEPTED (2026-08-15).
  Added the production deterministic offline execution source using the real
  `DeterministicController`, tool registry, PDB, `PatchManager`, and independent
  `EvaluationVerifier` inside the accepted Task-3 worker boundary. Live events
  use the coordinator's one shared `SessionEventEmitter`; parent notifications
  catch up from the authoritative journal; Textual supervision runs off the UI
  event loop; cancellation and application shutdown use the accepted bounded
  worker/process semantics; completed sessions register into app-owned history.
  Final real-run evidence: **175 events**, operational `succeeded/done`,
  cleanup verified, verifier `COMPLETED/RESOLVED` with f2p **1/1** and p2p
  **2/2**, and final live/replay `SessionViewState` equality. Sequential real
  sessions produced distinct history entries and the first remained replayable
  after the second. Task 8 external/configured command-model execution remains
  intentionally unimplemented.
- [x] **Task 8 — Add configured command-model execution and harden V1** —
  ACCEPTED (2026-08-16). Added the validated app-owned `command-models-v1`
  profile contract and configured-command execution through the existing
  `JsonlCommandTransport` / `LiveModelAdapter` protocol while preserving the
  same controller, PDB, PatchManager, verifier, worker, emitter, journal,
  history, replay, and Textual presentation architecture used by deterministic
  sessions. Configuration uses explicit argv with `shell=False`, safe
  fingerprints, protocol-version authority, bounded direct file reads, and
  structural secret-free diagnostics. Profile fingerprints are pinned from UI
  selection through worker load so changed configuration fails closed before
  executable launch.

  Final V1 hardening adds bounded stdout/stderr and request diagnostics,
  blocked-stdin cancellation, distinct request-timeout semantics, safe
  candidate-patch persistence, truthful configured-command network trust
  boundaries, Windows Job Object / `taskkill` descendant containment, and POSIX
  request-owned process groups with cleanup across successful response,
  natural error, cancellation, timeout, and worker-shutdown paths. Final
  Repair-Pass-4 evidence reports real WSL POSIX tests **8/8 passed** plus
  cross-platform transport **22/22 passed**; Windows configured-source
  integration **19/19 passed**, configured UI **15/15 passed**, config
  validation **59/59 passed**, and Task-3 worker gates **36/36 passed**.
  Configured live sessions reach the independent verifier, persist to app-owned
  history, replay without executing commands, and retain final
  `SessionViewState` parity. Deterministic mode remains compatible, canonical
  `RunEvent` 1.0 and verifier correctness authority are unchanged, and frozen
  R1-R6 evidence remains untouched.
- [x] **Local Application V1 — V1 COMPLETE** (2026-08-16).
  All eight roadmap tasks are accepted. Current V1 supports deterministic
  offline sessions and explicitly configured command-model sessions through one
  shared application architecture. Provider marketplaces/SDKs, credential
  management, arbitrary-repository IDE behavior, GPU/model hosting, browser UI,
  and campaign orchestration remain explicit non-goals.

## 0. Daily Requirement

- [x] 0.1 Write a one-page internship diary entry for each workday. (Completed through 2026-08-13 — consolidated `diary/diary.md`, including the R1-R6 phase entries appended to the 2026-08-12/2026-08-13 entries; chronology sourced from Git commit / frozen run timestamps.)

### 0.1 Subtasks / Log

- [x] 0.1.1 Created initial diary draft: diary/day_01.md.
- [x] 0.1.2 Created day 02 draft from cross-report synthesis: research/reports/synthesis/diary_day_02_draft.md.
- [x] 0.1.3 Normalize diary entries into final daily format.
- [x] 0.1.4 Extend the consolidated diary through 30 July 2026, including Tasks 10A, 10B-R1/R3, the Zen matrix, the R4 audit, and the R5 source closeout.
- [x] 0.1.5 Extend the consolidated diary through 7 August 2026, including status reconciliation, root-cause scoring, post-mortem trajectory persistence, and the full-suite cache repair.

---

## 1. Phase 1 — Literature Review

- [x] 1.1 Research debugging, automated debugging, fault localization, and program repair. (Bounded reviewed survey accepted at `3c23b6e`; unresolved claims are excluded rather than asserted.)
- [x] 1.2 Study LLM-based debugging work. (Bounded reviewed synthesis accepted at `3c23b6e`; additional frontier reading remains optional follow-up.)
- [ ] 1.3 Study agentic debugging, tool-using agents, and multi-agent debugging.
- [x] 1.4 Compare traditional debugging, LLM-based debugging, and agentic debugging. (`docs/research/debugging-approaches.md`, accepted at `3c23b6e`.)
- [x] 1.5 Study SWE-Agent, OpenHands, AutoCodeRover, Agentless, and ChatDBG. (All five have dedicated reviewed notes; capability matrix v1 is tracked. This aligns the parent with completed subtasks 1.5.1–1.5.6 and instructor item 5.)

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
- [x] 1.2.4 Summarize how LLM debugging differs from static code repair. (`docs/research/llm-debugging.md` §5 records the evidence acquisition, hypothesis revision, causal target, tool, validation, and data distinctions.)

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
- [x] 2.3 Select datasets suitable for fine-tuning, RAG, and evaluation. (Final decision: SWE-rebench V2 = primary authentic SFT/post-training dataset; QuixBugs = controlled evaluation cohort, kept entirely outside SFT training; BugsInPy = historical candidate considered during dataset research, not the final primary SFT source. Sequencing decisions recorded in Dataset and Evaluation Decision v1.)
- [ ] 2.4 Analyze datasets and prepare train/test splits.

### 2.x Subtasks / Log

- [x] 2.1.1 Build dataset inventory. (Dataset and Evaluation Decision v1.)
- [x] 2.2.1 Verify SWE-bench variants. (Primary official sources recorded; execution deferred.)
- [x] 2.2.2 Verify BugsInPy. (Primary official paper/repository sources recorded; execution deferred.)
- [x] 2.2.3 Verify Defects4J. (Primary official repository/paper sources recorded; Python/PDB track no-go-for-now.)
- [x] 2.2.4 Verify QuixBugs. (Primary official repository/paper sources recorded; fallback decision.)
- [x] 2.3.1 Decide first evaluation dataset. (Final decision: QuixBugs = controlled evaluation cohort; five curated fixtures as smoke gate; BugsInPy = historical candidate, license-gated, not the final evaluation/SFT source.)
- [x] 2.4.1 Prepare small reproducible Python bug subset (five curated pytest-compatible fixtures; Task 6).

---

## 3. Phase 3 — Model and Fine-tuning

- [x] 3.1 Select an open-source code model. (External/branch-bound selection recorded: `Qwen/Qwen2.5-Coder-7B-Instruct` at the pinned revision; no QLoRA repository change in this reconciliation.)
- [ ] 3.2 Convert dataset to instruction-response format if needed.
- [ ] 3.3 Run supervised fine-tuning with LoRA or QLoRA.
- [ ] 3.4 Compare pre-fine-tuning and post-fine-tuning model performance.

### 3.x Subtasks / Log

- [x] 3.1.1 Record the selected model identity, revision, and license. (Completed on the external QLoRA branch at `3f0d3e7`; main repository records the decision without importing or modifying that repository.)
- [ ] 3.2.1 Draft instruction-response schema for debugger trajectories.
- [ ] 3.3.1 Collect successful debugger trajectories before SFT.
- [ ] 3.4.1 Define pre/post fine-tuning evaluation protocol.

---

## 4. Phase 4 — RAG and Agent Tools

- [x] 4.1 Build RAG system over repository code, tests, issue descriptions, and error messages. (Completed 2026-08-06 — deterministic repository-native lexical RAG v1, `agentic_debugger/rag/`: fixture-scoped default + declared corpus-root/repo mode; source/test/issue/failure documents; safe task/issue projection with unit-tested oracle exclusion; explicit exclusion rules; `repository-index-v1`/`retrieval-result-v1` strict artifacts; revision binding; documented bounds; fail-closed. Infrastructure completion only; no RAG performance claim. See `docs/architecture/repository-rag.md`, `docs/evaluation/rag-comparison.md`.)
- [ ] 4.2 Combine fine-tuned model with RAG. (Partial 2026-08-06 — optional `rag_context` injection ready at the demo/model-adapter and `LiveModelAdapter` boundaries (additive; default request/case byte-identity proven; 20 KB public-request bound enforced pre-transport). Real fine-tuned generation import + verified combination remains open.)
- [x] 4.3 Develop file-read, code-search, test-run, and patch-apply tools.
- [x] 4.4 Create the debugging agent.
- [ ] 4.5 Make the model localize faults, identify root cause, and generate patches.

### 4.x Subtasks / Log

- [x] 4.1.1 Define repository indexing strategy. (Completed 2026-08-06 — deterministic chunking (AST symbol boundaries + line windows), exclusion rules, revision binding.)
- [x] 4.1.2 RAG retrieval and bounds v1 (2026-08-06): identifier-aware tokenization, integer token-overlap scoring, dedup, deterministic tie order, max-results/max-context-bytes budgets, truncation flags, latency excluded from identity. (Repair 1: retrieval result recomputes and verifies query/retrieval identities and selection byte counts on load; selections verified against the bound index.)
- [x] 4.1.3 RAG agent-context v1 (2026-08-06): bounded `RagContext` (`to_request_mapping` / `to_record_mapping`), 20 KB public-request budget mirror. (Repair 1: strict `RagChunkRef` validation, bound retrieval identity, lookalike-object rejection at demo/live boundaries; full module line coverage via deterministic gap chunks.)
- [x] 4.1.4 RAG index integrity v1 (2026-08-06, repair 1): recomputed index_id/corpus_digest/chunk identities on build and load, document uniqueness, chunk→document binding, final-size cap including index_id, tampering tests.
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

- [ ] 5.1 Create preference dataset from successful and failed debugging outputs. (Partial 2026-08-06 — exporter v1 infrastructure complete and deterministic demo-scale pairs produced from verifier evidence; production corpus awaits real attempts. See `docs/architecture/preference-export.md`.)
- [ ] 5.2 Apply DPO or an appropriate RLHF method.
- [ ] 5.3 Compare base model, fine-tuned model, RAG-supported model, and agentic system. (Partial 2026-08-06 — comparison harness v1 complete (imported `generation-artifact-v1` + native agentic conditions; normalized `comparison-v1` metrics; JSON/CSV/Markdown; aggregates; delta vs baseline; see `docs/evaluation/comparison-harness.md`); real base-versus-tuned comparison awaits real imported generations. Synthetic `offline-deterministic-demo` identities are not model performance.)

### 5.x Subtasks / Log

- [ ] 5.1.1 Defer until enough real/debugger trajectories exist.
- [x] 5.1.2 Preference-pair exporter v1 (2026-08-06): ordered rules, `preference-pair-v1` schema, held-out/oracle-answer-contamination/duplicate/same-response/no-evidence guards, JSONL + audit; no DPO/RLHF. (Repair 1: pair identity binds response/patch/verifier-evidence hashes and is verified on load; contamination checked on the full response before any storage bound; marker-inclusive UTF-8-safe response bounding; complete audit keys.)
- [ ] 5.2.1 Defer DPO/RLHF until SFT baseline is measured.
- [x] 5.3.1 Comparison protocol v1 (2026-08-06): `comparison-v1` schema, condition identities, declared baseline, imported + native modes. (Repair 1: strict attempt roles — evaluation vs preference-fixture — with at-most-one primary per task/condition; raw-output-to-patch binding; recursive JSON bounds; telemetry separation; `memory_bytes` in metrics/CSV.)

---

## 6. Phase 6 — Debugger Adapter

- [x] 6.1 Develop a debugger adapter for PDB, GDB, or LLDB. (Completed for the instructor's “or” requirement with the accepted Python/PDB-first adapter; GDB/LLDB are outside current scope.)
- [ ] 6.2 Enable the fine-tuned model to generate debugger commands and interpret outputs.
- [ ] 6.3 Enable breakpoint placement, variable inspection, stack trace reading, and step-by-step debugging. (Partial: full mechanism and scripted trajectories exist; no accepted live-model full sequence.)
- [ ] 6.4 Enable patch generation and test validation after debugger interaction. (Partial: mechanism is verifier-backed; no accepted PDB-after-live-model resolved case.)

### 6.x Subtasks / Log

- [x] 6.1.1 Start with PDB only.
- [x] 6.1.2 Define PDB command schema.
- [x] 6.1.3 Implement post-mortem PDB entry for failing Python script/test. (Tamamlandı ve main history'ye entegre: `f7ba129`..`e92634e`; bounded, side-effect-safe `run_post_mortem` protocol/worker/session operation; 107 unique focused tests. 2026-08-07 follow-up: existing `get_failure_trace` action üzerinden PDB-observation budget'ına bağlı ToolResult/controller Observation/RunEvent/replay/semantic-projection yolu ve cleanup kanıtı eklendi; bkz. `docs/architecture/pdb-trajectory.md`.)
- [x] 6.2.1 Serialize debugger outputs into model-readable structured text.
- [x] 6.3.1 Support stack inspection.
- [x] 6.3.2 Support local variable inspection.
- [x] 6.3.3 Support expression evaluation.
- [x] 6.3.4 Support stepping / next / continue.
- [x] 6.4.1 Feed debugger findings into patch proposal.
- [x] 6.4.2 Validate patch with tests.

---

## 7. Phase 7 — Evaluation and Final Report

- [ ] 7.1 Evaluate results by success rate, localization accuracy, test pass rate, cost, and runtime. (Metrics are defined — see 7.1.1-7.1.5 — and the comparison harness now derives them (`comparison-v1`: failure categories, aggregates, delta, cost/tokens, retrieval, replay, cleanup; see `docs/evaluation/comparison-harness.md`), but no real model has been evaluated against an external dataset yet, so there is no cross-dataset model result to report against these metrics.)
- [x] 7.2 Prepare a working agentic debugging demo and technical report. (Demo Guide v1 and Final Technical Report v1 completed and accepted 2026-07-31 — infrastructure/evaluation-platform demo and report, explicitly not a model-debugging-performance demo; see below.)

### 7.x Subtasks / Log

- [x] 7.1.1 Define localization metric (`CORRECT_TARGET_SYMBOL` and related localization outcomes).
- [x] 7.1.2 Define root-cause explanation metric. (Completed 2026-08-07 — strict `root-cause-assessment-v1` / `root-cause-rubric-v1`; three explicit causal dimensions, contradiction and evidence binding, derived closed outcomes, content identity, bounds/tamper rejection, explicit missingness and denominators, additive validated `comparison-v1` provenance integration with aggregates/deltas/CSV. No lexical-oracle scoring and no live-model performance claim. See `docs/architecture/root-cause-metric.md`.)
- [x] 7.1.3 Define patch correctness metric (verifier outcome, fail-to-pass, pass-to-pass, and full-suite consistency).
- [x] 7.1.4 Define cost/runtime metric (transport timing and provider-reported usage/cost metadata with qualification).
- [x] 7.1.5 Define debugger-action metric (PDB openings, observations, action counts, and policy restrictions).
- [x] 7.1.6 Comparison-harness metric derivation v1 (2026-08-06): normalized failure-category vocabulary, per-condition aggregates, baseline delta, CSV projection.
- [x] 7.2.1 Prepare demo scenario (Task 9 deterministic five-task, two-policy demonstration).
- [x] 7.2.2 Prepare final technical report outline. (Superseded by the completed Final Technical Report v1 — `docs/archive/reports/final-report-v1.md`.)
- [x] 7.2.3 Model, RAG, Fine-Tuning and DPO Decision Gate v1 — `docs/evaluation/model-rag-sft-dpo.md`. PROCEED (narrow) on model-access strategy, NO-GO-FOR-NOW on RAG, DEFER on SFT, NO-GO-FOR-NOW on DPO; eight QuixBugs tasks judged sufficient for infrastructure validation only, not model selection, training, or generalization claims.
- [x] 7.2.4 Final Technical Report v1 — `docs/archive/reports/final-report-v1.md`. Documentation-only synthesis of architecture, dataset/provenance, boundaries, BugsInPy/QuixBugs findings, exact results and their limits, and future work.
- [x] 7.2.5 Demo Guide v1 — `docs/demo/guide.md`. Reuses existing entry points only (Task 9 demo, `scripts/quixbugs_live_smoke.py`, `scripts/quixbugs_eight_task_baseline.py`); no parallel demo framework. The Task 9 demo command was re-verified live on this checkout; the QuixBugs WSL entry points were verified by source/CLI inspection only (not re-executed, per instruction not to re-run accepted benchmarks).

---

## Current Focus — R1-R6 documentation closeout (2026-08-13)

**Current project status (2026-08-13):** the S8/S9 bounded-negative closeout
is **historical**; the R1-R6 phase superseded the overall conclusion while
preserving the old experiments and negative results. The single canonical
authority for current project status and fresh-reviewer handoff is
`docs/project-closeout.md`; the 2026-08-11 closeout is archived unchanged at
`docs/archive/status/project-closeout-2026-08-11.md`; the full technical
report through 2026-08-13 is `docs/final-report.md` (§22).

Status summary:

DONE:
- real-model PDB interaction (R1, `c842d69`);
- multi-turn debugger use (R2, `97cc7fe`);
- debugger evidence → diagnosis → patch → verifier (R3, `f2291df`);
- model-generated regression test (R4, `372d51f`);
- R5 clean base-14B holdout 5/5 (r5.9, `e568b16`/`eeff17e`/`54828db`);
- project-fine-tuned debugger positive validation 8/8 (R6, `4610785`);
- professor structured traces (10 documents, `c9afe37`).

INCOMPLETE / CLOSED BOUNDARY:
- stronger R6 tuned-model final five-task holdout — status =
  **INCOMPLETE_HARDWARE_STOP** (curated-none-handling-001 RESOLVED;
  curated-off-by-one-002 BREAKING_RESOLVED; three tasks never produced
  outcomes after repeated local hardware power-offs). Not 2/5, not 1/5, not a
  failed benchmark. No sustained local rerun is scheduled in current scope.

Remaining project closeout work after this task:
- FirstMate review (`_ai-review/R1-R6-DOCS-CLOSEOUT-FIRSTMATE.zip`) —
  **ACCEPTED** (2026-08-13);
- Git commit/push of the documentation candidate (Final Git operator);
- eventual integration to main.

Closed historical boundaries (not reopened): DPO (CLOSED / NOT JUSTIFIED),
RAG correctness campaign (S4 PARTIAL / NOT_EVALUATED), BugsInPy execution
(license-gated), cp118 (historical negative transfer), D1/S2 debugger
failures (historical; superseded by R1-R6).

The historical focus entries below are dated 2026-08-02/03/11 snapshots and
are not current state.

### QuixBugs paired-pilot v2 live runner (2026-08-02)

- The fail-closed live-runner infrastructure for the frozen v2 campaign is
  implemented and validated (runner-only task): strict versioned authorization
  contract, pre-provider route gate, frozen six-case sequential orchestration,
  campaign stop/abort behavior, deterministic versioned result packaging,
  durable attempt ledger with no-rerun enforcement, and the CLI wiring through
  the accepted paired-pilot entry point (`preflight`, `template`, `live` with
  `--preflight-only`). Implementation:
  `scripts/quixbugs_live_runner_v2.py`; operator docs:
  `docs/datasets/quixbugs/pilot-v2-runner.md` and
  `docs/datasets/quixbugs/pilot-v2-authorization.md`; non-authorizing schema
  reference: `research/quixbugs/PAIRED_PILOT_V2_AUTHORIZATION_TEMPLATE.json`
  (rejected by the validator; real authorizations belong outside tracked
  source in the ignored `operator/` location).
- The runner reuses the accepted validator path
  (`scripts/quixbugs_paired_pilot.py`) and produces only frozen
  `quixbugs-paired-pilot-result-v2` case records; every case record must pass
  the strict in-order result validation before it is written. The runner never
  defaults into live execution and has no hidden provider selection or
  fallback: live execution requires the strict authorization artifact, the
  accepted repository baseline, a successful pre-provider route gate, an
  explicitly configured provider transport, and an explicitly configured case
  runner. None of those exist in this task, so every path used here used only
  synthetic transports, temporary fixtures, and deterministic test doubles,
  and the live CLI fails closed with zero provider activity.
- Validation performed: paired-pilot v1 and v2 validators (both valid);
  complete paired-pilot unit suites (v1 + v2 = 267 passed); new live-runner
  unit suite (`tests/unit/test_quixbugs_live_runner_v2.py`, 151 passed)
  covering the authorization contract (unknown/missing/wrong-type fields,
  duplicate/reordered cases, wrong hashes/baseline/protocol, v1
  contradictions, expiry, output-root and attempt-identity bindings, template
  rejection), every prohibited route and fallback, unobservable/stale/
  contradictory evidence, zero provider calls for every failed preflight,
  exact six-case order with fresh per-case boundaries and no parallelism,
  static-policy PDB prohibition, PDB gate/budget semantics, every frozen
  budget (model calls, attempts, retries, directives, hypotheses, patches,
  verifier runs, PDB openings/observations, case timeout, public evidence
  bytes), first/middle/final case failures, route drift and model substitution
  after preflight, malformed-response exhaustion, transport timeout, cleanup
  and source-restoration failure, verifier integrity failure, sanitization
  boundary violations, atomic partial-result behavior, duplicate-attempt and
  forbidden resume/rerun, and truthful token/cost semantics; directly
  affected controller/live/transport/verifier/QuixBugs suites are unaffected;
  broader unit suite run once; `python -m py_compile` on changed Python files;
  `git diff --check` clean.
- No live campaign, empirical evaluation, model-performance result, PDB
  effectiveness, RAG, SFT, or DPO work was run or marked complete. The
  historical OpenCode Zen records remain historical. The separate future task
  (real operator authorization + real route evidence + explicitly configured
  transport/case runner) is documented and not started.

### QuixBugs paired-pilot v2 live runner (2026-08-02)

A bounded material repair then hardened the runner boundary
(runner-only, same baseline `28ec7754…`): (1) execution-commit binding —
`accepted_campaign_commit` is the exact commit whose code will execute the
campaign; the actual Git HEAD must equal it, the commit must exist and
descend from the accepted baseline, and the tracked working tree plus the
real Git index must be clean (only ignored operator/output artifacts
allowed), verified before ledger claim/preflight/transport creation and
re-verified before every case; post-preflight drift stops the campaign with
typed `TRACKED_SOURCE_CHANGED` authority evidence; the verified commit is
recorded in campaign, case, authority, route-binding, and ledger evidence.
(2) Strict raw route evidence — every acceptance-critical field must be
explicitly typed (identity, version, catalog fingerprint, runtime model ID,
billing route, entitlement, account status, active status, variant
availability, all fallback observations, prices, cost, `observed_at`);
missing fields are never defaulted from the manifest/authorization; missing
denial/price evidence is never fabricated as False/zero; account status must
match the authorization; timestamps must parse and be fresh (not future, not
stale). (3) Immutable output — one output root belongs to exactly one attempt
identity (atomic `.attempt-owner` claim); authoritative artifacts use
create-once semantics and are never replaced; rejections go to a
non-authoritative `rejections/` directory; fresh authorizations require fresh
roots. (4) Atomic ledger lifecycle — cross-process exclusive claim (exactly
one of two concurrent claims succeeds); missing transport/runner rejects
before consuming the authorization; terminal ledger state finalizes before
`campaign.json` (written last); ledger-finalization failures leave no
completed artifact; lifecycle counts reconcile exactly with the frozen six
cases; `validate_campaign_record` and `verify_attempt_package` automate
campaign/package consistency. Authorization strictness: exact
`subscription_account_observation` field set, no future creation timestamps,
validity after creation and execution. Adversarial tests cover all of the
above, including a two-process concurrent-claim test and forged-commit
rejection with prior-evidence immutability. Post-repair counts: live-runner
suite 222 passed; paired-pilot suites 267 passed.

### QuixBugs paired-pilot v2 live runner (2026-08-02)

A second bounded material repair hardened the runner boundary further
(runner-only, same baseline `28ec7754…`): (1) single-winner attempt claim —
the exclusive `.attempt-owner` gate never lets a second process pass, even
with matching identity/authorization hash; typed errors distinguish
same-identity duplicates (`DUPLICATE_ATTEMPT`) from owner conflicts
(`OUTPUT_ROOT_OWNED`); a deterministic barrier two-process test proves exactly
one winner. (2) Occupied output roots — the authoritative root must be absent
or structurally empty before claim; pre-existing campaign/ledger/case/private/
temp/unknown files, directories, symlinks, or contradictory owner data are
rejected (`OUTPUT_ROOT_OCCUPIED`) with zero case execution and zero provider
activity; rejection evidence and preflight records moved to a parent-level
non-authoritative location. (3) Post-case and pre-terminal authority
verification — repository state and tracked authorities are re-verified after
every case and immediately before terminal ledger finalization; drift stops
the campaign with typed `TRACKED_SOURCE_CHANGED` authority evidence and the
campaign can never return or persist `COMPLETED`. (4) Non-finite numeric
evidence and strict JSON — `NaN`/`±Infinity` rejected via `math.isfinite()`
everywhere; all persisted JSON uses `allow_nan=False`; serialization failures
fail closed without partial files. Terminalization is now two-phase
(campaign.json first, ledger second) so a `COMPLETED` ledger always has a
matching validated terminal campaign.json; artifact creation failures
terminalize `ABORTED`/`OUTPUT_INTEGRITY_FAILURE`. Post-repair counts:
live-runner suite 251 passed; paired-pilot suites 267 passed.

### OpenCode Go directive transport repair v1 (2026-08-03, transport-only)

The first provider-connected six-case attempt
(`quixbugs-paired-pilot-v2-attempt-705aa04741064933b84767e095cd95bf`)
reached the real OpenCode Go model (16 logical model calls, 10 accepted
directives, $0.008036 provider-reported cost) but all six cases produced zero
hypotheses, PDB sessions, patch submissions, and verifier runs; accepted
directives were limited to baseline reproduction and the transition to
Understand. Transport evidence proved two related protocol failures: (A) the
model tried to open the `--file`-supplied `public-request.json` with Read,
Bash, or PowerShell (emitting DSML tool-call text instead of a directive;
Read/Bash must stay denied); (B) direct answers frequently used structurally
invalid envelopes such as `{"action":"find_function","name":"hanoi","path":"..."}`
and the extractor rejected any output containing multiple JSON objects before
checking whether exactly one was a valid directive. Bounded transport-only
repair (campaign, controller, case runner, PDB gates, facts provider,
verifier, authorization, and route identity unchanged):

- Inline public request: the sanitized request now travels inside the single
  OpenCode user message as canonical compact JSON between explicit
  `=== BEGIN PUBLIC REQUEST ===` / `=== END PUBLIC REQUEST ===` delimiters
  (one argv value, never shell interpolation; evidence records only
  `request_sha256` + `request_byte_count`); the message carries a brief
  protocol instruction, compact exact output-shape examples (action,
  transition, add_hypothesis, revise_hypothesis), and explicit prohibitions
  (no code fences, explanations, tool calls, protocol/version wrappers, or
  alternate envelopes; the embedded request is authoritative); the message
  must fit `MAX_PUBLIC_EVIDENCE_BYTES = 20000` (frozen
  `max_public_evidence_bytes`); `--file` was removed from the real
  `opencode run` command; isolated `--dir` and every permission denial
  (read/bash/edit/write) were preserved.
- Schema-aware extraction: every JSON object candidate is validated through
  the strict protocol-1.3 parser against the request's embedded
  `directive_schema`, `action_contracts`, and `controller` context; exactly
  one valid directive is accepted, zero is rejected (`no_valid_directive`),
  and more than one is rejected (`ambiguous_json_output`); copied
  request/config objects are ignored only because they fail directive
  validation, never through heuristic key stripping; wrong envelopes,
  unknown fields, and malformed arguments are never normalized. Requests
  without `directive_schema` keep the historical single-object extraction.
- Correction feedback: rejected directives return a provider-completed
  `directive_error` response with one compact machine-generated correction
  message (precise failure, the required `kind in [...]` envelope for the
  current allowed kinds, "return one JSON object only", no tools/code
  fence/explanation, never the previous response, within the accepted
  200-character rejection-detail bound); the adapter converts it into the
  accepted `LiveModelAdapterError` rejection so the existing bounded
  directive-feedback cycle carries the exact correction to the model (retry,
  directive-feedback, PDB, and patch budgets unchanged).
- Command/audit contract: preflight and effective-command validation enforce
  the new inline contract (single non-empty positional message, no trailing
  positionals, no `--file`, no shell, no repository working directory, no
  read/bash/edit/write tools); audit evidence records only request hash and
  byte count. The synthetic executable recovers the request from the inline
  message and gained `state-legal`, `copied-request-plus-valid`, and
  `tool-call-text` scenarios.

Focused tests prove: inline message content between delimiters; the real
command has no `--file` and one positional message; Read/Bash stay denied;
one valid directive surrounded by prose and copied non-directive JSON is
accepted; two valid directives are ambiguous; zero valid directives are
rejected; alternate envelopes (`action`, `params`/`payload`, protocol/version
wrappers) are rejected; malformed action arguments are rejected; bounded
correction feedback contains the exact failure without the full prior
response; every frozen controller state receives its legal
action/transition/hypothesis directive through the real wrapper plus
synthetic provider output (Reproduce action, Understand add_hypothesis,
RuntimeEvidence revise_hypothesis); wrapper preflight still creates no
provider inference; legacy behavior is unchanged. The `705aa047...` attempt
is classified as provider-connected but protocol-invalid (not a valid
static-versus-PDB experiment); the Authorized Six-Case Live Campaign TODO
remains open. No test/build/lint/compile/validation was run (FirstMate owns
it); no real OpenCode command, catalog, provider, or paid endpoint ran; no
commit/stage/push was made.


### OpenCode Go native-executable directive transport repair v2 (2026-08-03, transport-only)

Replay against the provider-connected attempt `705aa047...` proved the
previous inline-message design still blocked the campaign: 27 unique public
requests (canonical 4515-8661 bytes), only 14 fit the 7800-byte message
ceiling, 13 failed closed before provider execution, and every frozen case's
Understand-stage request was too large (complete inline messages
9189-9752 bytes). The public-evidence contract permits 20000 bytes; the
cmd.exe batch-shim line limit (~8191 characters), not the protocol budget,
was the blocker. Bounded transport-only repair (campaign, controller, case
runner, PDB gates, facts provider, verifier, authorization, and route
identity unchanged):

- Native executable execution: model execution invokes the native
  `opencode.exe` directly (batch-shim bypass). The wrapper begins from the
  independently verified `opencode.cmd` launcher path, resolves the native
  `opencode.exe` through the trusted npm package root
  (`<launcher-dir>\node_modules\opencode-ai`; explicit allowlist of
  package-managed relative locations, including the established
  `node_modules\opencode-windows-x64\bin\opencode.exe`, the baseline x64
  platform package, and the direct package `bin`; hard-linked copies of the
  single platform binary count as one; exactly one unique native binary must
  remain), requires it to be a regular executable file contained in the
  trusted root (no symlink/reparse escape) and to report the exact same
  OpenCode version as the launcher (same-installation proof; OpenCode Go
  mode additionally requires the exact authorization-bound version), fails
  closed otherwise (zero, multiple distinct, and path-escape candidates),
  uses the absolute native path as argv[0] with `shell=False`, keeps the
  isolated `--dir` and every permission denial, retains the exact
  model/variant/route binding, and never falls back silently to the batch
  shim, PATH lookup, environment-supplied executable paths, PowerShell,
  shell interpolation, or another
  executable. Short non-model inspection commands may continue through the
  launcher. Only bounded launcher/native identity evidence is recorded.
- Restored public-evidence budget: the 7800-byte message ceiling was
  removed; the 20,000-byte public-evidence limit applies to the canonical
  public request serialization, not to the complete user message (canonical
  up to and including `MAX_PUBLIC_EVIDENCE_BYTES = 20000` accepted, message
  constructed unchanged); the fully constructed
  native command is checked against `MAX_NATIVE_COMMAND_LINE_CHARS = 30000`
  (`subprocess.list2cmdline`, below the CreateProcess maximum) and fails
  closed before process creation. No batch shim, response file, shell, or
  model-readable attachment. The 8661-byte canonical Understand request and
  its complete inline scaffolding (9752 bytes) construct successfully.
- Strict top-level directive fields: the schema-aware validator rejects
  unknown top-level fields per kind (action/transition/add_hypothesis/
  revise_hypothesis/set_hypothesis_status field sets); missing and
  additional fields are rejected, never normalized or stripped;
  action-argument contract validation unchanged.
- Precise bounded correction feedback: the correction message carries the
  actual candidate-validation reason (e.g. `unknown argument field 'extra'`,
  `missing required argument 'path'`, `action 'x' is not allowed in state
  'Understand'`); single invalid candidate -> exact bounded reason; multiple
  candidates with none valid -> deterministic bounded reason without full
  model output; more than one valid -> ambiguous reason. <= 200 characters;
  precise reason, legal `kind: [...]` envelope, "one JSON object only", no
  tools/code fence/explanation; never the prior response; malformed alternate
  envelopes are never converted.
- Preserved diagnostic classifications: empty output, text without a
  protocol directive, no JSON object, zero valid directives, and multiple
  valid directives remain distinct; only directly affected stale test
  expectations updated.

Focused tests: frozen request-size range (>= 8661-byte canonical, > 9000-byte
message, native command construction, no `.cmd`/`--file`/shell/truncation);
> 20000-byte requests fail closed; native command-line bound enforced;
native `opencode.exe` resolution same-directory/version-bound/fail-closed;
extra top-level fields rejected per kind; precise candidate reason reaches
bounded correction feedback; one valid directive among copied non-directive
JSON accepted; two valid directives ambiguous; Read/Bash/edit/write denied;
wrapper preflight zero provider inference; legacy unchanged. Deterministic
synthetic fixtures only (a compiled fake native `opencode.exe` forwarder
plus the fake launcher shim); no real OpenCode or provider call. Attempt
`705aa047...` remains classified as provider-connected but protocol-invalid;
the Authorized Six-Case Live Campaign TODO stays open pending FirstMate
review and a fresh real attempt. No test/build/lint/compile/validation was
run (FirstMate owns it); no real OpenCode command, catalog, provider, or
paid endpoint ran; nothing was committed.

### OpenCode Go npm-native + full public-evidence budget repair v3 (2026-08-03, transport-only)

FirstMate material review found two remaining transport-contract gaps and
three stale focused-test assertions. Bounded transport-only repair
(campaign, controller, case runner, PDB gates, facts provider, verifier,
authorization, route identity, and isolation unchanged):

- Trusted npm-native resolution: the same-directory-only assumption was
  replaced with a deterministic fail-closed npm-installation resolution
  contract. The wrapper begins only from the independently verified
  `opencode.cmd` launcher path, defines the trusted npm package root as
  `<launcher-dir>\node_modules\opencode-ai`, and resolves the native
  executable exclusively from an explicit allowlist of package-managed
  relative locations under that root — the established Windows x64
  platform-package path `node_modules\opencode-windows-x64\bin\opencode.exe`,
  the baseline x64 platform package, and the direct package `bin` (the npm
  shim's own target). The genuine npm layout hard-links the single platform
  binary into these locations, so candidates sharing one file identity count
  as one; exactly one unique native binary must remain. Every candidate must
  resolve to an absolute path inside the trusted root (no symlink/reparse
  escape) and exist as a regular executable file; zero, multiple distinct,
  and path-escape candidates fail closed. The resolved native must report
  the exact same version as the launcher (and, in Go mode, the exact
  authorization-bound version) and is used as argv[0] with `shell=False`;
  arbitrary recursive searches, PATH lookup, environment-supplied executable
  paths, shell interpolation, PowerShell execution, parsing an unrestricted
  command from the batch file, and fallback to `opencode.cmd` are rejected
  by construction. Evidence records only the resolution strategy
  (`npm-package-layout`), the bounded package-relative native path, and the
  regular-file/root-containment/version-match flags. Real machine inspection
  confirmed the established npm layout (launcher
  `C:\Users\benya\AppData\Roaming\npm\opencode.cmd`; native
  `...\node_modules\opencode-ai\bin\opencode.exe` plus the two platform
  packages, all hard-links of the single 174 MB binary; no sibling exe). All
  synthetic fixtures mirror the production layout (native under
  `node_modules\opencode-ai\node_modules\opencode-windows-x64\bin\`); a
  sibling-only `opencode.exe` is never trusted.
- Full 20 KB public-evidence support: the 20,000-byte public-evidence limit
  applies to `canonical_public_request(request).encode("utf-8")`, not to the
  complete user message. Canonical requests up to and including 20000 bytes
  are accepted (FirstMate reproduced: canonical 18914 bytes, complete
  message 20005 bytes — previously rejected), canonical requests above
  20000 bytes fail closed, the canonical request is never truncated,
  reduced, summarized, split, or mutated, the complete message is
  constructed unchanged, and the fully constructed native command remains
  independently bounded by `MAX_NATIVE_COMMAND_LINE_CHARS = 30000`
  (`subprocess.list2cmdline`) failing before process creation.
- Stale focused-test corrections (no runtime weakening): the inline message
  assertion compares lowercase to lowercase; pure prose preserves the
  established `no_json_object` classification (not `no_valid_directive`);
  the route-capture inspection inventory includes the native executable's
  `--version` proof while still proving no command uses the `run`
  subcommand.

Focused tests: nested npm x64 native binary resolves; resolved native
remains under the trusted `opencode-ai` root; zero/multiple-distinct/
path-escape candidates fail closed; sibling `opencode.exe` not implicitly
trusted; native version bound to launcher and authorization; route capture
and wrapper share the same resolved native identity; route capture never
invokes `opencode run`; real model execution uses the nested native
executable directly (no `.cmd`, shell, PowerShell, response file, or
`--file`); canonical 20000-byte boundary; frozen 8661-byte request and
>9000-byte message still construct; Read/Bash/edit/write and all isolation
denials intact; strict top-level fields and precise bounded correction
feedback unchanged. Attempt `705aa047...` remains classified as
provider-connected but protocol-invalid; the Authorized Six-Case Live
Campaign TODO stays open pending FirstMate review and a fresh real attempt.
No test/build/lint/compile/validation was run (FirstMate owns it); no real
OpenCode command, catalog, provider, or paid endpoint ran; nothing was
committed.
### BugsInPy licensing and metadata preflight

- [x] Licensing gate completed at `da39c55`.
- [x] Metadata-only BugsInPy preflight is the active task (`bugsinpy-metadata-preflight-v1`).
- [ ] BugsInPy source acquisition and execution remain unauthorized; no containment implementation or benchmark execution is approved.

Current state (2026-08-02):

- The paired-pilot v2 contract and the operational routing authority are the
  current project state: DeepSeek V4 Flash through the operator's OpenCode Go
  subscription is the default implementation route when a task explicitly
  authorizes model use; GPT-5.6 High in a separate ChatGPT conversation owns
  literature review and deep-research work; research outputs are
  non-authoritative until reviewed and incorporated into tracked project
  artifacts. See `CURRENT_AGENT_ROSTER.md`.
- The QuixBugs paired-pilot v2 planning manifest is frozen
  (`research/quixbugs/PAIRED_PILOT_V2.json`); live execution remains
  unavailable and fail-closed until a separate implementation task supplies an
  explicit authorization artifact.
- The accepted v1 paired-pilot files and historical results are unchanged and
  remain the retained v1 authority. The earlier OpenCode Zen matrix claims
  remain historical, descriptive-only records (kept as [historical] entries
  below); they were not rewritten as OpenCode Go claims.
- BugsInPy execution remains BLOCKED and is out of scope for the paired pilot.
- No new live/model run or dataset execution is authorized or scheduled.

Current state (2026-07-31):

- QuixBugs static live feasibility is complete and accepted: Resource-Limited
  QuixBugs Fallback Real Smoke v1 and QuixBugs Eight-Task Gold Baseline v1 both
  passed on the pinned QuixBugs revision, no-model, infrastructure validation
  only.
- The contained PDB runtime is complete and accepted (Task 4 family, Task 9
  demonstration).
- The earlier four-case curated-fixture OpenCode Zen matrix is a historical,
  descriptive-only record (kept as separate [historical] entries in the log
  and notes below; it is not the protocol-1.3 probe). Static policy resolved
  2/2 cases; PDB-on-uncertainty opened PDB 0/2 times and both PDB cases
  terminated with underlying reason `invalid_model_response`, so that matrix
  supports no PDB-effectiveness claim.
- The later protocol-1.3 QuixBugs `gcd` live-model PDB reachability probe
  (accepted at a143e62d54a7cf25f56ba743a020cc19b472c762) terminated with
  underlying reason `invalid_model_response`: the returned malformed
  provider-completed objects received bounded adapter feedback
  (`malformed_directive`), there was no controller-accepted directive in the
  final accepted case, baseline reproduction was not reached in that final
  case, the PDB gate never opened, and no PDB-effectiveness claim is
  supported.
- The BugsInPy eligibility manifest and the BugsInPy adapter already exist and
  are not future work: `research/bugsinpy/PILOT_ELIGIBILITY_MANIFEST_V1.json`
  and `agentic_debugger/bugsinpy/{adapter,wsl,wsl_preparation,smoke}.py` are
  tracked and tested.
- The BugsInPy licensing and redistribution gate v1 is now the current BLOCKED
  authority (`docs/datasets/bugsinpy/license-gate.md` and
  `research/bugsinpy/BUGSINPY_LICENSE_GATE_V1.json`): dataset verdict BLOCKED,
  formal license status UNKNOWN, redistribution BLOCKED, private local
  research-use UNKNOWN, operational execution gate BLOCKED, all eight task
  verdicts BLOCKED, overall pilot verdict BLOCKED. The offline validator fails
  closed on exact dataset authority identity, project repository identity,
  task project verdict equality, and exact buggy/fixed license-record
  coverage.
- No BugsInPy acquisition, dependency preparation, containment execution, or
  benchmark execution is authorized.
- The next allowable engineering task is metadata-only containment/preflight
  enforcement, not adapter design or source execution: the preflight must
  fail closed while the dataset verdict is BLOCKED and refuse source
  acquisition and execution.
- No new live/model run or dataset execution is authorized or scheduled.
  Dataset and Evaluation Decision v1 and Model/RAG/SFT/DPO Decision Gate v1
  remain the documentation-only decisions; the accepted QuixBugs campaigns
  were not rerun.

### Historical log (preserved for the record; not current state)

- [historical] Task 10B-R4 is complete: the offline PDB-policy directive-path
  audit identified concrete contract/gating defects without making any live
  provider, model, OpenCode, or network call.
- [historical] Task 10B-R5 is complete and accepted: Policy-Scoped Live
  Contract Repair v1, source/merge commit
  `63fa27cc4d30490b9770ead3ce14b4b6d3ddf222`, current protocol version `1.3`.
- [historical] This decision branch started from accepted baseline
  `51e7dc0faabe84a36d60486c420de9ba0af95878`; its documentation changes were
  intentionally not source changes.
- [historical] R5 final validation collected 2,110 tests: 2,108 passed and 2
  skipped. The final immutable audit ZIP SHA-256 is
  `6f65acf77a43b1f44897e2bd3b846a47d63114ec9b59c7b9a38e341a8e0a2e82`.
- [historical] The accepted four-case Zen matrix was descriptive only. Its
  PDB-on-uncertainty cases opened PDB 0/2 times, so it still supports no
  causal PDB-effectiveness or policy-superiority claim.
- [historical] Dataset and Evaluation Decision v1 selected BugsInPy as
  primary, QuixBugs Python as fallback, and the five current curated fixtures
  as an architecture smoke gate; RAG was NO-GO-FOR-NOW for a research
  comparison, SFT was DEFER, and DPO/preference optimization was
  NO-GO-FOR-NOW. The eligibility manifest and adapter have since been
  implemented and the licensing gate is now the current authority.
- [historical] Post-MVP research, containment, dataset execution, broader
  evaluation, model training, and final-report work remained active or
  deferred as indicated by the phase checkboxes.
- [historical] 2026-07-31: Model, RAG, Fine-Tuning and DPO Decision Gate v1
  and Final Technical Report and Demo Package v1 were complete and accepted,
  documentation-only, baseline `2236775`. Decision Gate v1
  (`docs/evaluation/model-rag-sft-dpo.md`) reaffirmed RAG NO-GO-FOR-NOW,
  SFT DEFER, and DPO NO-GO-FOR-NOW from Dataset and Evaluation Decision v1,
  added PROCEED (narrow) on future model-access strategy (one real-dataset
  single-task static-baseline live case as the smallest credible next
  experiment, on the existing free-tier route, before any paid/multi-model
  expansion), and recorded that the eight-task QuixBugs gold baseline is
  sufficient for infrastructure validation only — not model selection,
  training, or generalization claims. Final Technical Report v1
  (`docs/archive/reports/final-report-v1.md`) and Demo Guide v1
  (`docs/demo/guide.md`) synthesized the full project to date; the Demo
  Guide reused only existing entry points (no parallel demo framework) and its
  Task 9 command was re-verified live on this checkout. No model, RAG,
  training, PDB, or paid API ran during that campaign; the accepted QuixBugs
  benchmark campaigns were not rerun.

### Historical notes (preserved for the record; not current state)

- [historical] Task 4A complete.
- [historical] Task 4B complete.
- [historical] Task 4C complete.
- [historical] Task 4D complete.
- [historical] parent Task 4 complete.
- [historical] Task 5 complete.
- [historical] Task 5 was fast-forward merged into `main` at `43d00c8`.
- [historical] Task 6 complete: five curated pytest-compatible bug fixtures were reviewed, repaired, merged and pushed.
- [historical] Task 6 was fast-forward merged into main at eedcccb.
- [historical] Task 7 complete: Verifier and Evaluation Runner v1.
- [historical] Task 7 was fast-forward merged into main at 1b0af78.
- [historical] Task 7 provides authoritative DebugTask loading and validation, disposable workspace preparation, canonical fixture immutability checks, baseline reproduction, F2P/P2P execution, candidate unified-diff application, syntax validation, post-patch reproduction, exact test-node collection, full-suite consistency checks, bounded typed result records, deterministic JSON-compatible mappings, workspace-relative path normalization, cleanup lifecycle reporting, verifier command accounting separate from controller max_test_runs, and trusted-local execution-boundary disclosure.
- [historical] Task 7 evaluates trusted local benchmark fixtures and benign candidate patches. It is not an OS-level hostile-code security sandbox.
- [historical] Task 8 complete: Golden Trajectories v1 (commit ab9b8b7). At the Task 8 implementation closeout point, main and origin/main point to ab9b8b7.
- [historical] Task 8 provides immutable record/replay architecture, RunEvent sequence validation, controller state transition reconstruction, action/observation linkage, semantic trajectory projection and first-mismatch reporting, scripted model sequences with exact model-call accounting (rejecting exhausted/unused outputs), static/PDB-gated/deterministic-rejection trajectories, verifier integration, provider/network attempt guards, portable disposable workspace handling, and exception-safe cleanup across success/rejection/exhaustion/PDB/tool/evaluator/cleanup-error paths.
- [historical] Task 8 is not an OS-level hostile-code sandbox and does not claim causal PDB efficacy proof for agentic debugging.
- [historical] Task 9 complete and accepted: First End-to-End Demonstration, implementation commit `e7031fa796a738fc80de4c673607eee72254ce56`.
- [historical] Task 9 integrated the real controller, tool registry, workspace, test runner, source-skill, PatchManager, PDB session, event replay, and Task 7 verifier paths into an offline, deterministic demonstration over five curated tasks and two policies. The implementation scope was 19 changed files, 6709 insertions, and 75 deletions; no external model-provider execution was used.
- [historical] The accepted demonstration covered 5 curated tasks × 2 policies = 10 cases: controller Done 10/10, verifier COMPLETED / RESOLVED 10/10, fail-to-pass 10/10, pass-to-pass 22/22, localization `CORRECT_TARGET_SYMBOL` in all 10 cases, full suite passed for every case, canonical fixtures unchanged 10/10, disposable workspaces cleaned 10/10, provider attempts 0, and network attempts 0.
- [historical] Static policy covered 5/5 verifier COMPLETED, 5/5 RESOLVED, 5/5 fail-to-pass, 11/11 pass-to-pass, and 0 PDB observations. PDB-on-uncertainty covered the same 5/5, 5/5, 5/5, and 11/11 results with 21 successful PDB observations.
- [historical] The two clean strict demonstration executions produced identical deterministic views: 10 semantic trajectories compared and 0 semantic differences; the generated source-tree digest matched the accepted live tree and no stale summary placeholder values remained.
- [historical] Task 9 validation passed: focused Task 9 suite 177 tests; relevant controller/PDB/replay/golden/evaluator regression suite 1229 passed with 2 warnings; full repository suite 2020 passed, 2 skipped, and 5 warnings; compile validation and whitespace validation passed. The skips and warnings were pre-existing. One managed-sandbox `.pytest_cache` permission warning occurred during evidence inventory generation and was not a product defect.
- [historical] Static-versus-PDB parity is structural because both policies use the same deterministic offline catalog repair. The demonstration does not establish causal PDB superiority. Provider/network guards measure in-process attempts and are not an operating-system-level network sandbox.
- [historical] Task 10A complete and accepted: Real-Model Evaluation Harness v1, implementation commit `14a0287a763553038549eb8d84d6d9f8a432f44a`.
- [historical] Task 10A delivers an explicitly authorized, offline-by-default real-model evaluation harness over the existing integrated runtime. It provides dual explicit live-access authorization before configuration is read, credential-free configuration, credential-shaped configuration and argv rejection, secret-safe events, diagnostics, JSON reports, and human reports, UUID-based evaluation identities, unique namespaces for reports, cases, runs, trajectories, and requests, duplicate task and policy rejection, stable credential-free configuration fingerprinting, full controller/tool-registry/policy/PDB/patch-lifecycle/RunEvent/localization/verifier/cleanup integration, accepted-patch-only verifier submission, static-policy PDB prohibition, positive PDB-enabled live-path validation, bounded model requests/retries/stdin/stdout-stderr/request-timeouts/model-transport timing, explicit unknown provider token fields, non-destructive workspace ownership and cleanup, versioned machine-readable reports, human-readable reports, authoritative report-schema validation before configured CLI output, coherent resolved/unresolved/rejected/failed/cleanup-failed/interrupted/partial semantics, deterministic local fake and fault-injection validation, and no external provider execution during Task 10A.
- [historical] Task 10A does not claim that a real model solved any task, does not claim PDB improves model performance, and does not claim a provider-specific integration has been validated.
- [historical] Task 10B-R1 complete and accepted: Live Protocol and Accounting Repair v1, accepted implementation/merge commit `2996f16f7c95baf0860d0736d8ab67d13af60b9e`. It exposed truthful state-specific action and transition contracts and preserved unique transport-attempt identities, bounded rejection diagnostics, and usage accounting for provider-completed invalid model responses. The live wire protocol version became `1.1`.
- [historical] The private Task 10B live runner remains operator tooling outside this repository. The original controlled live baseline evidence package (SHA-256 `87ac568c74aaa4b6d2e726003a5a1cafd238215411f691dd3aaa7d46e135db08`) received verdict `ACCEPT`; the baseline received verdict `ACCEPT_WITH_LIMITATION`.
- [historical] In that original baseline, the static policy result was `RESOLVED`. The PDB policy terminated with underlying reason `invalid_model_response`; the case-status layer reported `PROVIDER_ERROR`, which is not evidence of a provider outage. The model repeated the illegal action `extract_failing_test`, and PDB was never opened.
- [historical] Task 10B-R3 complete and accepted: Invalid Directive Retry Feedback v1, accepted implementation/merge commit `1bb1d5251cc732f331ce2f5fdd163d9e46309d29`. It added bounded, redacted, structured `directive_feedback` after provider-completed invalid directives while preserving retry identity, accounting, and transport-failure semantics. The live wire protocol version became `1.2`.
- [historical] Task 10B-R3 evidence was archived outside the repository with SHA-256 `4b32ec09a2f6bae58c63c42123bbfd9323711f2c07d4ecc6024c97aaed360b5c`.
- [historical] A minimal retry-recovery diagnostic then ran through the private runner. Its evidence package SHA-256 is `4681de9c02ca8f222cf6067293e59a8dd3c1eb605d4ee4be245ddf13e9cea88a`. The diagnostic directly observed one legal recovery after feedback and one later failed recovery in the same case; the case still terminated with `invalid_model_response`, did not attempt a patch, and never opened PDB.
- [historical] Private-runner follow-up work added protocol-1.2 compatibility, direct sanitized feedback evidence, episode classification, a locked small repeated matrix profile, per-case stop gates, aggregate budget enforcement, infrastructure exception closure, redaction hardening, and telemetry fail-closed behavior. This tooling remains outside the repository and is not part of the source commit history.
- [historical] The final locked matrix used OpenCode Zen provider ID `opencode`, model ID `deepseek-v4-flash-free`, variant `max`, fixture `curated-none-handling-001`, policies `static-baseline` and `pdb-on-uncertainty`, two repetitions per policy, four total cases, and concurrency 1.
- [historical] The matrix evidence package SHA-256 is `96675c3995683169c440411deef84429277bcf5289c03375863f6bc65b3ac43d`; the evidence package and matrix execution received verdict `ACCEPT`, while experimental interpretation remains limited.
- [historical] Static policy resolved 2/2 cases and produced 2/2 accepted patches. PDB-on-uncertainty resolved 0/2 cases; both terminated with underlying reason `invalid_model_response`, no patch or verifier phase was reached, and PDB openings were 0/2.
- [historical] Across all four cases, there were 31 logical model calls, 37 transport attempts, 226,385 provider-reported total tokens, provider-reported cost metadata of 0, and approximately 396.5 seconds wall-clock duration. Provider-reported cost metadata is descriptive and is not proof of actual billing.
- [historical] Six corrective-feedback episodes were observed: 4 `RECOVERED_AFTER_FEEDBACK`, 2 `INVALID_AFTER_FEEDBACK`, and 0 `INTERRUPTED_AFTER_FEEDBACK`. This 4/6 descriptive recovery fraction is not a causal estimate or generalized reliability claim.
- [historical] The historical OpenCode Go baseline and the OpenCode Zen free-model matrix use different provider routes and must not be pooled as one provider population.
- [historical] Because neither PDB-enabled matrix case opened PDB, the matrix still does not measure PDB effectiveness. It supports no claim that static debugging is superior, that PDB is harmful, or that protocol 1.2 caused a higher success rate.
- [historical] Task 10B-R4 offline audit completed. It found that the live PDB policy did not fully machine-enforce `decide_pdb_access`, advertised actions outside the exact state/registry/policy/lifecycle/budget intersection, exposed lifecycle-invalid PDB actions, and allowed some state-illegal hypothesis directives to bypass protocol-1.2 corrective feedback.
- [historical] Task 10B-R5 repaired the live boundary in four bounded stages: policy-scoped transition/action enforcement; total directive-kind parsing and validator-contract parity; protocol `1.3` plus deep contract detachment; and mandatory exact-registry plus PDB-observation-budget filtering.
- [historical] Protocol `1.3` now has one authoritative nested validator-derived action-contract shape. `LiveModelAdapter` fails closed without an exact `ToolRegistry`; no manually maintained flat fallback remains.
- [historical] Effective PDB actions are filtered by authoritative budget classification. At zero remaining PDB observations, observation-consuming actions disappear; an active session retains `stop_pdb_session` for cleanup, and hidden exhausted actions receive bounded `illegal_action` feedback before controller execution.
- [historical] R5 changed exactly seven tracked files and was accepted after final focused, unit/golden, integration, collection, manifest, hash, CRC, secret-scan, and Git-state review. No live/model/network/OpenCode call occurred.
- [historical] Dataset and Evaluation Decision v1 is the current documentation-only decision. Any later dataset or real-model validation requires separate explicit authorization and must remain narrow; the previous matrix must not be reused as evidence of PDB effectiveness.
- [historical] The accepted ten-task implementation sequence (Tasks 1–9 plus Task 10A) is complete. Dataset inventory and primary/fallback selection are now documented in Dataset and Evaluation Decision v1; external dataset execution, training-data work, fine-tuning, RAG beyond the implemented tool foundations, DPO/RLHF, broad benchmarking, and later technical evaluation work remain deferred, partial, or not started where indicated by the phase checkboxes.
- [historical] Hostile-code filesystem, process and network containment remains deferred.
- [historical] Adaptive PDB gating and Tier 3/supporting-paper reading remain deferred. BugsInPy is selected as the primary external target. The BugsInPy licensing and redistribution gate is complete and remains BLOCKED at the dataset level; the BugsInPy adapter and eligibility manifest already exist; containment enforcement and execution remain future work.
- [historical] Planned decomposition (all completed):
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
- [x] Resource-Limited QuixBugs Fallback Real Smoke v1 (baseline `96526fc`; branch `feature/quixbugs-resource-limited-smoke-v1`)
- [x] QuixBugs Eight-Task Gold Baseline v1 (baseline `4063fa4`; branch `feature/quixbugs-eight-task-baseline-v1`)
- [x] Model, RAG, Fine-Tuning and DPO Decision Gate v1 (baseline `2236775`; branch `feature/model-decision-final-report-v1`)
- [x] Final Technical Report and Demo Package v1 (baseline `2236775`; branch `feature/model-decision-final-report-v1`)

### QuixBugs Eight-Task Gold Baseline v1

Expands the accepted single-task `gcd` smoke into a reproducible eight-task
no-model baseline on the same pinned revision
`4257f44b0ff1181dedaedee6a447e133219fcebf`, reusing the adapter, WSL runner,
resource profile, environment, source checkout, patch lifecycle, and
`EvaluationVerifier` unmodified in behavior. Validates dataset eligibility,
gold patches, verifier behavior, runtime stability, and evidence quality —
does not evaluate a model or PDB.

- Generalization: `agentic_debugger/quixbugs/adapter.py`'s manifest validation
  and `to_debug_task()` were narrowly generalized from a literal `"gcd"` pin
  to a derived `quixbugs-<algorithm>-smoke-v1` identity with fail-closed
  path-naming consistency checks and a new required per-task `oracle` manifest
  section. All 50 original adapter tests plus 13 new generalization tests
  pass (63 total); the final full unit suite passed with 1952 passed /
  2 skipped.
- Selection: deterministic alphabetical order over the pinned repository's
  `json_testcases`-backed test files (excluding `gcd`), then formal execution
  through the resource-limited runner. 11 unique candidates executed through
  the resource-limited runner (8 selected reaching the `EvaluationVerifier`
  + 3 excluded). Excluded:
  `bitcount` and `find_first_in_sorted` (discovery-stage — buggy baseline
  never terminates for at least one case — safely killed by the enforced
  CPU-time/wall-clock limits, not a hang on the host; tests ran through the
  resource-limited runner but did not reach the `EvaluationVerifier`) and
  `get_factors` (pre-verifier schema-construction stage — 11 collected nodes
  push `verifier_command_count` to 26, past
  `task_schema.Constraints.max_test_runs`'s `[1, 20]` range — a
  schema-representability limit, not weakened; preflight/discovery/oracle/
  gold-patch all succeeded through the resource-limited runner, but
  `DebugTask` construction raised `SchemaValidationError` before the
  `EvaluationVerifier` could run, so `get_factors` did not reach the
  `EvaluationVerifier`; replaced by `kth`). Historical compliance with the
  12-unique-candidate cap is **unproven** (the exploratory unsandboxed
  triage inventory cannot be reconstructed from surviving evidence); the
  prior "11 of 12" claim has been removed. For future runs only static
  file/metadata inspection may occur outside the resource-limited runner,
  and the 12-unique-attempted-algorithm cap is enforced in the orchestration
  path.
- Selected 8: `gcd`, `bucketsort`, `find_in_sorted`, `flatten`, `kth`,
  `hanoi`, `is_valid_parenthesization`, `kheapsort`. Every gold patch touched
  exactly one file (1-2 hunks); every task reached `COMPLETED`/`RESOLVED`
  with F2P/P2P/full-suite all passing, canonical fixture hash unchanged, and
  workspace lifecycle `CLEANED`. 49/49 total collected nodes passed
  post-patch across all eight tasks (100% solved rate, 100% full-suite pass
  rate). Pinned source re-verified clean (exact pin, no modifications) after
  the full run; WSL `runs/` directory holds only the persistent `selftest/`
  scaffold, confirming every disposable per-task workspace was removed.
- Final verdict: **ACCEPT CANDIDATE — EIGHT-TASK BASELINE COMPLETE**. See
  `docs/datasets/quixbugs/baseline-8-task.md` and
  `_ai-review/quixbugs-eight-task-baseline-v1/` for full evidence.

### Resource-Limited QuixBugs Fallback Real Smoke v1

BugsInPy execution remains license-gated and its WSL real-smoke was also
fail-closed on CPU/memory/process-count enforcement (see the accepted
`bugsinpy-wsl-real-smoke-v1` evidence package: `IMPLEMENTED — REAL SMOKE
BLOCKED`). This narrow follow-on unblocks the resource-limit gate and uses it
to complete one genuine, real, no-model smoke against QuixBugs Python `gcd`
instead — infrastructure validation only, no model/PDB/broader campaign.

- Mechanism: live-tested `prlimit` (not cgroup v2/`systemd-run`) composed
  inside the existing `bwrap --unshare-all` sandbox. `agentic_debugger/bugsinpy/wsl.py`
  gained `ResourceLimits`, `build_prlimit_argv`, `self_test_resource_limits`,
  and a fail-closed `prepare_resource_isolation` gate; `create_verified_context`
  gained an optional `runner=` parameter. All prior BugsInPy tests kept
  passing unmodified (default no-`runner` path is unchanged).
- Repository: `https://github.com/jkoppel/QuixBugs`, default branch `master`,
  pinned revision `4257f44b0ff1181dedaedee6a447e133219fcebf`. License MIT;
  `legal_notes.txt` documents explicit creator consent (Liron Shapira).
  Supports local, non-redistributed research execution.
- Environment: system `/usr/bin/python3` 3.10.12 in the approved
  Ubuntu-22.04 WSL2 distro; task-local venv built with `--copies` (required —
  the default symlink venv is invisible through the `\\wsl.localhost\` Windows
  bridge because its final symlink hop is an absolute host path); pip
  bootstrapped via `get-pip.py`; `pytest==7.4.4` pinned.
- Live self-tests: all 7 existing Bubblewrap checks passed, plus 3 new
  resource checks — CPU-time (`--cpu=5`, killed exit 137), address-space
  (`--as=256MiB`, clean `MemoryError`), process-count (`--nproc=8`, blocked
  after exceeding the cap). The gate only opens after this live evidence.
- Real dataset finding: the buggy `gcd(a % b, b)` never advances `b`, so every
  case but the trivial `b == 0` one recurses to `RecursionError`. Of 6 official
  parametrized cases, exactly 1 passes on the buggy baseline and 5 fail. This
  required lowering `DebugTask.tests.pass_to_pass`'s minimum from 2 to 1 entries
  in `agentic_debugger/evaluation/task_schema.py` (backward compatible; every
  existing curated/BugsInPy task already supplies ≥2) rather than fabricating a
  second passing node, per explicit user confirmation during planning.
- Storage: WSL-owned root under `~/.local/share/agentic-debugging-internship/quixbugs-smoke-v1/`
  (outside `/mnt/c`) with `sources/`, `python-env/`, `cache/` persistent and
  only `runs/<uuid>/` disposable. `QuixBugsSmokeRunner.ensure_source()` acquires
  the pin once and only re-verifies (never re-clones) on later runs.
- Result: pytest collection/baseline/oracle discovery matched the predicted
  5-fail/1-pass split exactly; gold patch generated via `difflib` and hashed;
  `EvaluationVerifier.evaluate()` returned `COMPLETED`/`RESOLVED` with F2P 1/1,
  P2P 1/1, full suite 2/2, canonical fixture unchanged, workspace `CLEANED`.
  Disposable workspace removed; persistent source/venv/cache retained.
- Final verdict: **ACCEPT CANDIDATE — REAL SMOKE PASSED**. See
  `docs/datasets/quixbugs/smoke-guide.md` and
  `_ai-review/quixbugs-resource-limited-smoke-v1/` for full evidence.

### Model, RAG, Fine-Tuning and DPO Decision Gate v1 + Final Technical Report and Demo Package v1

Documentation-only campaign, baseline `2236775`, branch
`feature/model-decision-final-report-v1`. Produces three new documents and
updates README/TODO/this tracker/diary; adds no runtime source code and
runs no model, provider, OpenCode, RAG, training, PDB, or paid API.

- `docs/evaluation/model-rag-sft-dpo.md`: explicit PROCEED/DEFER/
  NO-GO-FOR-NOW verdicts for future model-access strategy (PROCEED, narrow),
  repository RAG (NO-GO-FOR-NOW), SFT (DEFER), DPO/preference optimization
  (NO-GO-FOR-NOW), and whether the eight QuixBugs tasks are sufficient for
  infrastructure validation (yes), model selection (no), training (no), and
  generalization claims (no). Names the smallest credible next experiment
  (one QuixBugs task, static-baseline policy, free-tier model, through the
  protocol-1.3 harness) and trigger conditions for each decision, without
  authorizing that experiment to run in this campaign.
- `docs/archive/reports/final-report-v1.md`: a stand-alone technical report
  covering the research question, architecture/execution lifecycle,
  dataset/provenance decisions, sandbox/resource/Git/credential/fail-closed
  boundaries, BugsInPy license-block findings, the QuixBugs fallback and
  eight-task methodology, exact results and their explicit non-claims,
  model/RAG/SFT/DPO decisions, limitations, validity threats,
  reproducibility, future work, and final contribution.
- `docs/demo/guide.md`: reuses only existing entry points — the Task 9
  offline demo (`python -m agentic_debugger.demo`), the one-task QuixBugs
  smoke (`scripts/quixbugs_live_smoke.py`), and the eight-task baseline
  (`scripts/quixbugs_eight_task_baseline.py --skip-excluded`). No parallel
  demo framework was created. While preparing this guide, the Task 9 demo
  was re-run live on this checkout (`--output-dir ... --strict`, exit `0`,
  10 cases) and a documentation gap was found and fixed: `--list-tasks`
  also requires `--output-dir`, which the guide's first draft omitted. The
  QuixBugs WSL entry points were verified by source/CLI-help inspection
  only (`-h` output matches documented flags) — they were **not**
  re-executed, per the instruction not to rerun accepted benchmark
  campaigns.
- README.md, TODO.md, and this tracker were updated to mark both remaining
  tasks (the Decision Gate and the Final Report/Demo Package) complete, and
  the diary gained a 2026-07-31 entry.
- Independent review: one read-only Explore-agent review pass over the new
  documents and the diff, verifying internal consistency, evidence backing,
  and that no claim overstates the underlying accepted evidence.
- Validation performed: targeted demo re-run (above), `python -m compileall`
  over the repository, JSON manifest re-validation (`json.load` over every
  tracked `research/**/*.json` manifest), and `git diff --check`. No
  accepted benchmark campaign and no known-hanging test path were rerun.
- Review package: `_ai-review/model-decision-final-report-v1/` (local,
  gitignored via `.git/info/exclude`, uncommitted) — campaign brief,
  decision report, final technical report and demo guide copies, review
  findings, validation output, a diff against `2236775`, direct copies of
  every changed/new tracked file, and exact `git status`.

### BugsInPy Licensing and Redistribution Gate v1

The documentation and manifest gate is complete against accepted baseline
a143e62d54a7cf25f56ba743a020cc19b472c762. The exact BugsInPy authority
revision is 11c5f1eea954a42132cfd06bf257766a7963e0fd. Exact buggy and fixed
project revisions were checked for FastAPI, HTTPie, tqdm, and thefuck using
bounded public GitHub tree metadata and individual license/notice files.

- Dataset verdict: BLOCKED for redistribution. Formal BugsInPy license status is
  UNKNOWN: the complete non-truncated recursive tree response matched no
  conventional license/notice filename pattern, and the README has no explicit
  permission for metadata, isolated patches, scripts, tests, or repository
  structure.
- Private local research-use status is UNKNOWN. The exact README expressly
  instructs users to clone, configure, checkout, compile, and test for
  reproducible research; that is intended-use evidence, not a blanket license
  or redistribution grant. The operational execution gate remains BLOCKED by
  current project policy, because the ambiguity is unresolved, Onur has not
  approved proceeding, and containment/dependency gates are incomplete. This
  is a fail-closed project decision, not a legal conclusion that local use is
  prohibited.
- Canonical machine-readable record: research/bugsinpy/BUGSINPY_LICENSE_GATE_V1.json.
  The _ai-review matrix is a direct consistency copy only; clean checkouts must
  resolve the manifest record IDs and run the validator without _ai-review.
- Project verdicts: FastAPI CLEAR_WITH_CONDITIONS (MIT); HTTPie
  CLEAR_WITH_CONDITIONS (BSD-3-Clause plus AUTHORS.rst); tqdm
  CLEAR_WITH_CONDITIONS (file-scoped MIT/MPL-2.0); thefuck
  CLEAR_WITH_CONDITIONS (MIT).
- All eight task verdicts and the overall pilot verdict are BLOCKED because
  each task materially depends on the unresolved BugsInPy metadata/patch
  terms.
- The tracked repository and _ai-review/ may contain only sanitized URLs,
  exact revisions, paths, SHA-256 hashes, gate records, bounded retrieval
  metadata, and aggregate results. Do not add upstream source, BugsInPy
  patches, tests, environments, caches, raw logs, credentials, or model
  candidate diffs.
- No benchmark, dependency preparation, model, OpenCode, containment, or
  upstream execution occurred. The next containment task must refuse source
  acquisition while the operational gate is BLOCKED and require both resolved
  private-use/redistribution terms and explicit Onur approval before a future
  gate change.
- Full report: docs/datasets/bugsinpy/license-gate.md. Evidence package:
  _ai-review/bugsinpy-license-gate-v1/.
- A bounded material repair closed the validator's fail-open gaps without
  changing any verdict: the offline validator now requires manifest dataset
  verdict equality, exact task project-verdict equality, exact buggy/fixed
  license-record coverage derived from repository and revisions, a locked
  BugsInPy authority revision chain, dataset/tree identity and evidence
  hashes, and per-project repository identity on every file record. All
  accepted verdicts are unchanged: BugsInPy BLOCKED, projects
  CLEAR_WITH_CONDITIONS, all eight tasks BLOCKED, overall pilot BLOCKED.
- A second bounded material repair made expected evidence contract-derived
  rather than mutable-record-derived: one project-level artifact contract now
  pins each project's repository, required artifact paths, record kinds, and
  SPDX metadata (FastAPI LICENSE/MIT; HTTPie LICENSE/BSD-3-Clause plus
  AUTHORS.rst; tqdm LICENCE with file-scoped MIT/MPL-2.0 and a non-empty
  project scope note; thefuck LICENSE.md/MIT). Every file record must carry a
  canonical lowercase 40-hex revision, exact source_url and revision_url,
  matching path/kind/SPDX metadata, and a unique repository/revision/path
  identity; project coverage must match the selected revisions exactly
  (rejecting missing artifacts, extra unselected revisions, and unused
  records), and each task's reviewed IDs must be unique and exactly equal the
  required artifact records for its own buggy/fixed revisions. All accepted
  verdicts remain unchanged.

### QuixBugs Paired Pilot Planning and Qualification v1

The QuixBugs paired-pilot plan and no-model qualification are the active task.
The frozen manifest is research/quixbugs/PAIRED_PILOT_V1.json; it selects
three tasks and six future static-baseline/PDB-policy cases by deterministic
hash order. The harness defaults to validation and provides model-free
validate, plan, dry-run, and qualify modes. No live campaign has been
authorized or run, and no provider/model contact occurred for this task.

BugsInPy execution remains BLOCKED and is out of scope for this pilot.

### QuixBugs Paired Pilot Route v2 and Research Ownership

Contract and project-state update against accepted baseline
`18e067f24c337e7215139373edc699a347cf2127` on branch
`feature/quixbugs-paired-pilot-route-v2`. No model was run, no pilot provider
was contacted, no nested OpenCode process was started, no live catalog was
queried, no QuixBugs benchmark was executed, and no system-level change was
made.

- v2 is a derived paired-pilot contract, not a rewrite of v1:
  `research/quixbugs/PAIRED_PILOT_V2.json` (campaign-manifest SHA-256
  `bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171`)
  freezes the same three selected tasks, the same six-case order (task/policy
  sequence carried over from the accepted v1 manifest; case IDs re-stamped
  with the `quixbugs-paired-pilot-v2` prefix), the same controller budgets,
  protocol 1.3, the same qualification contract
  (`7246d289fcc689e93d93385751cbae5fa75a3c52e3c04e001f2c977a1990c52d`), the
  same source-integrity authority
  (`a3ccf9d083f3405f0811b66c69a5e93d8a347d77b5f8ccb9d168d93102bd1977`), the
  same public/private boundary, containment requirements, and no-rerun rules.
- The route changed: the future Zen/free-tier route is replaced by the
  operator-selected OpenCode Go subscription running DeepSeek V4 Flash. The
  old zero input/output price eligibility rule is replaced by a fail-closed
  subscription-route contract: authorized route is the OpenCode Go
  subscription only; no Zen route, free-tier substitution, Ollama route,
  alternate provider, model substitution, metered fallback, paid overage
  route, or per-call billing fallback; if subscription entitlement or
  billing-route evidence cannot be established before contact, the campaign
  blocks before the first provider call. No exact catalog identifier, OpenCode
  version, catalog fingerprint, account status, entitlement, or pricing
  observation was invented; the exact runtime model/catalog identity remains
  intentionally authorization-bound.
- Provider-reported token and cost metadata remain truthful: v2 results do not
  force reported cost to zero merely because access is subscription-based.
  Authorization, route-observation, preflight-failure, result-validation, and
  stop-rule contracts now represent subscription billing explicitly and
  validate fail-closed (new preflight failure categories:
  `SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED`, `ZEN_ROUTE_OBSERVED`,
  `FREE_TIER_SUBSTITUTION`, `OLLAMA_ROUTE_OBSERVED`,
  `MODEL_SUBSTITUTION_OBSERVED`, `RUNTIME_MODEL_ID_MISMATCH`,
  `METERED_FALLBACK_REQUIRED`, `PAID_OVERAGE_REQUIRED`,
  `PER_CALL_BILLING_FALLBACK`; new authorization failure categories:
  `SUBSCRIPTION_ROUTE_REQUIRED`, `BILLING_ROUTE_MISMATCH`,
  `RUNTIME_MODEL_ID_BINDING_MISSING`, `ENTITLEMENT_EVIDENCE_MISSING`,
  `ZERO_PRICING_RULE_CONTRADICTION`).
- `CURRENT_AGENT_ROSTER.md` is now the operational routing authority: DeepSeek
  V4 Flash through OpenCode Go is the default implementation route when a task
  explicitly authorizes model use; GPT-5.6 High in a separate ChatGPT
  conversation owns literature review and deep-research work; research outputs
  are non-authoritative until reviewed and incorporated into tracked project
  artifacts; every task still requires explicit authorization for
  provider/model execution; coding agents must not launch additional models,
  research agents, MCP, benchmarks, or paid services unless the current task
  explicitly authorizes them.
- The validator entry point (`scripts/validate_quixbugs_paired_pilot.py`) now
  validates every tracked supported manifest version (v1 and v2). The v1
  files and historical results are preserved unchanged; the earlier OpenCode
  Zen matrix claims in the README, diary, tracker, and v1 documents remain
  labeled historical and were not rewritten as OpenCode Go claims.
- Validation performed: paired-pilot validator for v1 and v2 (both valid),
  full v1 paired-pilot unit suite (179 passed), new v2 paired-pilot unit
  suite (88 passed), `python -m py_compile` on changed Python files, and
  `git diff --check`. No live pilot and no accepted benchmark campaign was
  run. Review package: `_ai-review/quixbugs-paired-pilot-route-v2/`.
- A bounded material repair then made the v2 derivation fail closed: the
  validator now requires and exactly validates `derived_from`
  (`manifest_path` = `research/quixbugs/PAIRED_PILOT_V1.json`,
  `manifest_sha256` = `5d84ea22820ca38ce80dd90a5d36e6f80160220178496950f9b45be41fae19ce`,
  exactly the accepted contract fields), re-validates the tracked v1 manifest
  to the accepted canonical hash, freezes the tracked v1 campaign
  identity/version, and verifies that every v1-retained contract area
  (qualification contract, qualification evidence binding, source-integrity
  authority, selected tasks, frozen v1 selection ranking, six-case
  task/policy order, budgets, public/private boundary, containment contract,
  no-rerun rule) stays consistent with the accepted v1 authority. The
  `MODEL_SUBSTITUTION_OBSERVED` evidence is now bound to the validated
  authorization artifact: `evidence.expected_runtime_model_id` must equal the
  authorization-bound value and `evidence.observed_runtime_model_id` must
  equal the route observation, so evidence cannot rewrite both identities to
  the observed value. Adversarial tests cover missing/wrong `derived_from`,
  missing/drifted referenced v1 file, wrong v1 identity, v1-retained contract
  drift, and the model-substitution forgery case. Post-repair counts: v2
  suite 88 passed, combined paired-pilot suite 267 passed.

## Last Updated

2026-08-13 (R1-R6 documentation closeout: current status reconciled through
the R1-R6 phase — real-model debugger milestones, R5 clean base-14B holdout,
R6 fine-tuned 8/8 disjoint validation, professor structured traces complete,
R6 final five-task holdout INCOMPLETE_HARDWARE_STOP; canonical current status
document `docs/project-closeout.md`; 2026-08-11 closeout archived at
`docs/archive/status/project-closeout-2026-08-11.md`)

2026-08-11 (S9 final closeout: reproducibility audit, status/TODO
reconciliation, bounded deterministic validation, canonical closeout artifact
`Agentic_Debugging_Project_Closeout_2026-08-11.md` [archived:
`docs/archive/status/project-closeout-2026-08-11.md`]; project closes on the
accepted bounded-negative path — **historical snapshot; superseded by the
R1-R6 phase**)

2026-08-07 (repository reconciliation, root-cause explanation metric,
post-mortem trajectory persistence, and full-suite forwarder-cache repair)

The current branch reconciles instructor/TODO/tracker claims against reachable
history, adds strict independent `root-cause-assessment-v1` scoring, persists
bounded post-mortem PDB evidence through the accepted controller/event/replay
path, and repairs the historical synthetic OpenCode wrapper failure family.
The latter was an order-dependent test-fixture bug: distinct
`(interpreter, target_script)` cache keys compiled to one shared per-PID
assembly path. Unique target-specific build directories now make disk and
cache identities agree; the production wrapper and provider gates are
unchanged. Validation: affected surface 156 passed; post-fix full suite 3733
passed / 3 skipped; the sole tuple-return collection warning was subsequently
removed and final collection is 3735 tests without warnings. See
`docs/archive/status/repo-reconciliation-2026-08-07.md`,
`docs/architecture/root-cause-metric.md`,
`docs/architecture/pdb-trajectory.md`, and
`docs/architecture/verifier-cache.md`.

2026-08-06 (Friday main-repo completion hardening: ledger time provenance, transport teardown race, known wrapper/transport test failures, post-mortem PDB entry; final bounds-v2 marker-reservation repair and durable Friday documentation wording)

This pass is accepted and integrated on `main` at `62deca4` (the
`fix/post-mortem-pdb-bounds-v2` branch), built on top of the accepted Friday
delivery bundle commit `ab464dd` (`456f0e9` is the earlier presentation
plan/deck/cue commit; the original delivery bundle is accepted and integrated
on `main` at `ab464dd`). The bounded post-mortem evidence layer
(exception-argument work bounds, huge-int fail-closed handling, the
no-overread local scan, and the omission marker reserved inside the byte
budget) is part of the accepted presentation state on `main`; the exact
presentation-day tip is recorded by the preflight `git rev-parse HEAD` check,
and presentation runs from clean `main == origin/main`:

- Campaign ledger time provenance (`scripts/quixbugs_live_runner_v2.py`): the
  terminal ledger `updated_at`, the create-once `terminal-commit.json`
  `created_at`, the post-campaign authority `observed_at`, and the post-case
  authority-invalidated `observed_at` now reflect the actual finalization /
  detection time rather than reusing the campaign-start `reference_time`. The
  ledger `created_at` (genuine claim time) and every pre-campaign / in-loop
  authority gate keep using `reference_time` (those gates are evaluated
  against the campaign's frozen start identity). Deterministic clock
  injection is preserved. 6 focused tests added.
- Repaired (deterministic) — OpenCode request-thread teardown race
  (`scripts/quixbugs_opencode_go_adapter.py`): the `process.stdin is not None`
  background-thread assertion that surfaced as
  `PytestUnhandledThreadExceptionWarning` and cascaded under full-suite
  ordering is replaced with a teardown-aware writer guard. The writer
  captures any failure into `write_error` and joins before process
  termination; a benign `BrokenPipeError` on a process that exited 0 with a
  valid response is no longer misclassified as a transport failure. 3
  deterministic regression tests added.
- Repaired (deterministic) — known wrapper/transport test failures (4 test
  defects + 2 env-gated): zero-price catalog fingerprint binding; `message_is_single_positional`
  run-path/preflight contract (the run-path `transport_preflight` record now
  carries the same contract fields as the `--preflight` CLI record); sibling
  `opencode.exe` resolver test set up the trusted npm layout so the intended
  error path fires; the two env-gated real-wrapper preflight tests are now
  hermetic (fake profile + fake npm-layout native + synthetic auth), so they
  pass without a real OpenCode install.
- Post-mortem PDB entry (TODO 6.1.3): `run_post_mortem` PDB
  protocol/worker/session operation that runs a Python script and captures
  bounded structured traceback evidence on unhandled exception. Reuses the
  existing PDB protocol, worker channel, and session lifecycle; the response
  is deterministic, bounded, JSON-serializable protocol evidence. The
  historical limitation recorded here was closed on 2026-08-07 through the
  existing `get_failure_trace` controller/tool/event/replay path; see
  `docs/architecture/pdb-trajectory.md`. Offline-capable; no provider/network;
  successful exit produces no post-mortem; tracebackless failure fails closed
  through the real worker branch (authoritative `PdbResponse`, success=false,
  empty result, bounded error, lifecycle `failed`); one-execution-per-session
  invariant preserved. Evidence capture never invokes target presentation
  code: exception summarization uses exact descriptors only, traceback frames
  come from one bounded walk (hard scan ceiling, no source-line loading,
  explicit truncation, fail-closed bounded error evidence), and locals are
  collected with a hard inspection ceiling and fail-closed mutation handling;
  all text fields are UTF-8-byte-bounded with the truncation marker inside
  the limit. 107 unique focused tests (no shadowed definitions). The
  tracked TODO 6.1.3 subtask status is determined by its actual acceptance
  contract (see the tracker 6.x log).
- Bounds-v2 evidence layer (`agentic_debugger/runtime/pdb_worker.py` +
  `tests/unit/test_pdb_post_mortem.py`, part of the accepted presentation
  state on `main`):
  exception-argument summarization is now work-bounded as well as
  byte-bounded — at most `_POST_MORTEM_EXC_ARGS_MAX_SCAN` (64) arguments are
  inspected, a remaining UTF-8 byte budget (`_POST_MORTEM_MAX_EXC_MESSAGE_UTF8`,
  1024) is reduced while processing with separators and the omission/truncation
  marker inside the same budget, huge exact `str` arguments are only previewed
  (never copied in full), huge exact `bytes` arguments are only decoded from a
  bounded prefix (never decoded completely), and huge exact `int` arguments
  are decimalized only below the safe bit ceiling
  (`_MAX_SERIALIZED_INT_BITS` = 4096) and otherwise rendered as stable
  `<int bits=N>` metadata — Python's integer-to-string conversion digit limit
  previously raised `ValueError` from `str(huge_int)` inside the helper and
  escaped into evidence capture; `_post_mortem_bounded_text` applies the same
  bit rule and fails closed on any exact-built-in conversion failure. The
  omission marker is reserved inside the budget: whenever any argument or
  argument tail is unrepresented, the final message ends with exactly one
  marker; marker decisions use the renderer's explicit truncation metadata,
  never the rendered text suffix, so a real argument value that ends with the
  marker character is never misread. The
  bounded-local scan checks the scan budget before every iterator advance:
  actual successful advances never exceed the declared ceiling, returned
  `inspected` equals successful advances, no `next()` probe occurs after
  budget exhaustion, and the exact mapping length decides unseen entries
  without peeking one extra item (one additional advance is only required to
  discover a further accepted entry when the length is unavailable). New
  instrumented tests count iterator advances directly (`_CountingIterator`)
  for the ceiling, ceiling−1, exactly-at-ceiling, 32-accepted ± remainder,
  and unavailable-length boundary cases; the final marker-reservation pass
  added the exact-full-budget counterexample, the reservation-boundary
  below/above/multi-byte cases, the literal-ellipsis false-positive pair, the
  below-ceiling byte-exhaustion case, and the no-retrieval proof; the final
  narrow pass added the separator-only empty-argument boundary cases (empty
  exact str/bytes fully represented at exactly 1024 bytes, empty argument
  with a later omission, and zero-argument-byte non-empty omission). Test
  count progression (all unique): 70 → 90 (2026-08-06 bounds pass) → 103
  (final marker-reservation pass) → 107 (empty-argument boundary pass).

Accepted validation: focused suites (live-runner 286; wrapper+transport 100
in isolation; transport-factory+case-runner 55 in both orders; V4
budget/verifier+replay; post-mortem 107 + PDB protocol/session/integration 964
(1071 PDB-surface tests); compileall exit 0; deterministic demo exit 0, 2/2
RESOLVED, F2P 2/2, P2P 4/4,
0 provider/0 network, replay-valid 2/2, CLEANED 2/2). Recorded full suite on
the integrated checkpoint (fresh process, content of `62deca4`):
**3448 passed, 3 skipped, 32 failed** — the suite is NOT green, and the
remaining 32-node family is NOT repaired (no repair is claimed). The 32
failures are all in the pre-existing wrapper-preflight subprocess-chain
family (`tests/unit/test_opencode_go_transport_factory.py` 16,
`tests/unit/test_opencode_go_wrapper_repair.py` 13,
`tests/unit/test_opencode_go_case_runner.py` 3) with
`LiveTransportError(kind="process_error"): provider process exited nonzero`
from the synthetic wrapper chain; every one of them passes in isolation
(85/85 across the three files) and none references the changed PDB code
paths. The same wrapper-preflight subprocess-chain family is documented as a
pre-existing full-suite resource-pressure flake (reproduced on the clean
`ab464dd` baseline) and previously surfaced as
`test_selftest_mode_is_synthetic_only`; in that run the selftest node passed
and the pressure surfaced in sibling wrapper-chain nodes instead. Bounds-v2
A/B classification (2026-08-06, fresh processes, same machine and
environment): L1 isolation 85/85 and L2 heavy wrapper subset 395/395 passed
on both the clean `62deca4` checkpoint and the candidate; the L3 36-file
prefix-load sequence reproduced the family on BOTH trees with the identical
32-node failure set (32 failed / 1463 passed / 3 skipped, ~830 s each) and
the same `LiveTransportError(kind="process_error")` signature — the family
reproduces on the exact clean checkpoint without any candidate change, is
consistent with cumulative OS resource pressure on the synthetic wrapper
subprocess chain, and is not implicated in or amplified by the candidate.
The previous order-dependent failure family (the transport/case-runner
cascades under full-suite ordering) is repaired; the race/drain regression
tests are unit-level and do not amplify OS resource pressure. No instructor
TODO status is promoted by this pass; no provider, live campaign, WSL,
BugsInPy, QLoRA, or held-out execution occurred; no commit/merge/push was
made during the coding-agent build phase.

This 2026-08-06 diagnosis is historical. The 2026-08-07 follow-up proved the
shared per-PID compiled-forwarder output path was the actual order-dependent
cause, repaired it without changing production transport gates, and produced
a green full suite. See `docs/architecture/verifier-cache.md`.

Earlier history:

2026-08-05 (Friday professor delivery bundle; campaign infrastructure accepted on main; V4 attempt record; QLoRA implementation)

The Friday professor delivery bundle is committed and integrated on `main`
at `ab464dd` (the earlier presentation plan/deck/cue delivery commit is
`456f0e9`; campaign infrastructure accepted through `0abb588`; V4 identity
correction accepted through `fc7c85b`; QLoRA implementation accepted at
`3f0d3e7` on the unmerged experiment branch; external AI audit 39 ACCEPT / 36
REJECT; final training authorized 2026-08-05 with no accepted artifact; held-out
generation unauthorized). The delivery bundle adds the offline Friday
professor package (`docs/FRIDAY_DELIVERY_MANIFEST_V1.md`,
`docs/FRIDAY_PREFLIGHT_CHECKLIST_V1.md`, `docs/FRIDAY_STATUS_HANDOFF_V1.md`,
plan/deck/cue sheet v1.2) and a fresh single-task demo rehearsal (exit 0, 2
cases, 2/2 RESOLVED, F2P 2/2, P2P 4/4, 0 provider/0 network, workspaces
CLEANED; evidence preserved under the ignored `_ai-review/` location). No
instructor TODO status is promoted; no provider, campaign, WSL, training, or
benchmark execution occurred.

2026-08-03 (OpenCode Go npm-native + full public-evidence budget repair v3,
transport-only)

The directive transport now resolves the native `opencode.exe` through the
trusted npm package root (`<launcher-dir>\node_modules\opencode-ai`; explicit
allowlist including the established
`node_modules\opencode-windows-x64\bin\opencode.exe`; hard-linked copies of
the single platform binary count as one; exactly one unique binary; root
containment, regular file, launcher/authorization version equality; zero,
multiple distinct, and path-escape candidates fail closed) and invokes it
directly for `opencode run` (`shell=False`, never a silent fallback), and the
20,000-byte public-evidence limit applies to the canonical public request
serialization (not the complete user message) with the fully constructed
native command independently bounded by
`MAX_NATIVE_COMMAND_LINE_CHARS = 30000`. The three stale focused-test
assertions were corrected without runtime weakening. Attempt `705aa047...`
remains classified as provider-connected but protocol-invalid; the
Authorized Six-Case Live Campaign TODO stays open. No validation was run
(FirstMate owns it); no real provider/catalog/OpenCode command ran; nothing
was committed.

Earlier history:

2026-08-03 (OpenCode Go native-executable directive transport repair v2,
transport-only)

The directive transport now executes the model through the native
`opencode.exe` (resolved from the verified `opencode.cmd` launcher, same
directory, regular file, exact same version, fail closed), bypassing the
cmd.exe batch-shim line limit, so the inline user message supports the full
frozen public-evidence budget (`MAX_PUBLIC_EVIDENCE_BYTES = 20000`) with the
canonical request never reduced or truncated; the fully constructed native
command is bounded by `MAX_NATIVE_COMMAND_LINE_CHARS = 30000`
(`subprocess.list2cmdline`) and fails closed before process creation. The
schema-aware validator rejects unknown top-level directive fields, the
bounded correction feedback carries the precise candidate-validation reason
(never the prior response), and the established diagnostic classifications
are preserved. Attempt `705aa047...` remains classified as provider-connected
but protocol-invalid; the Authorized Six-Case Live Campaign TODO stays open.
No validation was run (FirstMate owns it); no real provider/catalog/OpenCode
command ran; nothing was committed.

Earlier history:

2026-08-03 (OpenCode Go directive transport repair v1, transport-only)

The OpenCode Go directive transport was repaired for the first
provider-connected six-case attempt (`705aa047...`): the sanitized public
request now travels inline inside the single OpenCode user message between
explicit delimiters (`--file` removed from the real `opencode run` command;
Read/Bash/edit/write stay denied), extraction is schema-aware (every JSON
candidate is validated against the request's embedded directive schema,
action contracts, and controller context; exactly one valid directive is
accepted, zero/ambiguous rejected), and rejected directives return one
compact machine-generated correction message that flows through the existing
bounded directive-feedback cycle (never the previous model response).
Preflight/effective-command validation and audit evidence (request hash +
byte count only) follow the inline contract; legacy extraction without
`directive_schema` is unchanged. Attempt `705aa047...` is classified as
provider-connected but protocol-invalid, not a valid static-versus-PDB
experiment; the Authorized Six-Case Live Campaign TODO stays open. No
validation was run (FirstMate owns it); no real provider/catalog/OpenCode
command ran; nothing was committed.

Earlier history:

2026-08-03 (QuixBugs OpenCode Go execution adapter v1 task, adapter-only)

The OpenCode Go execution-adapter wiring for the paired-pilot v2 live runner
is implemented and validated with zero provider contact. Added:

- Strict versioned adapter configuration contract
  (`quixbugs-opencode-go-execution-adapter-v1`) in
  `scripts/quixbugs_opencode_go_adapter.py`; tracked non-executable template
  `research/quixbugs/OPENCODE_GO_EXECUTION_ADAPTER_TEMPLATE.json` (rejected as
  an active configuration); active configurations live outside tracked source
  in the ignored `operator/` location. Rejections: unknown/missing fields,
  wrong types, string shell commands, empty argv elements, relative/ambiguous
  executables, shell metacharacters, out-of-boundary executables/working
  directories, hidden environment inheritance, credential-shaped content,
  authorization/manifest/protocol/commit/route/catalog/model mismatches,
  budget/timeout contradictions, and the historical
  `opencode/deepseek-v4-flash-free` Zen identity as an execution identity.
- Runtime identity binding derived from validated authorization + route
  evidence; agreement enforced across configuration, authorization, route
  observation, and transport invocation; alias/catalog/version/variant/
  route-class/billing-route drift and any observed Zen/free-tier/Ollama/
  alternate-provider/fallback state rejected via typed RouteDriftError that
  maps to the accepted TRANSPORT_EVIDENCE_LOSS infrastructure stop contract;
  independent identity observations recorded in bounded redacted evidence.
- Explicit transport factory: structured argv only, explicit cwd, bounded
  environment allowlist, bounded stdout/stderr/diagnostics, process-group-
  aware timeout and tree cleanup, zero automatic retries/fallback/catalog
  queries, no global-model or session-state reliance; construction requires
  validated authorization/execution commit/route observation/configuration/
  binding; per-case prepare() verifies the output/attempt ownership gates on
  disk; the binding and gates are revalidated before every provider process
  attempt. Provider-reported token/cost metadata propagates truthfully;
  non-finite metadata is rejected; subscription access never forces cost to
  zero.
- Case-runner binding reusing `run_live_quixbugs_case` (bounded
  backward-compatible extension of `agentic_debugger/evaluation/live_quixbugs.py`:
  explicit `pdb_identity_binding` replacing the historical Zen PDB identity
  only when supplied, and bounded PDB-gate/rejection evidence for every
  policy; default behavior unchanged): one fresh transport/session/workspace
  per frozen case, no shared conversation, static-baseline PDB prohibition,
  PDB-on-uncertainty through the accepted controller gate and budgets, and
  full reconciliation with the live runner's ledger, terminal commitment,
  authority checks, stop rules, and result validator.
- CLI surface: `adapter-template`, `adapter-validate` (structural or
  authorization+route bound), `route-preflight-only` (zero provider
  processes), `selftest` (synthetic only), and `live-wire` (requires explicit
  authorization, route evidence, adapter config, output root, operator
  confirmation, QuixBugs environment artifact, and a resolvable facts
  provider; unusable without an actively validated configuration and an
  explicitly constructed transport factory).
- Deterministic network-incapable synthetic executable
  (`scripts/opencode_go_synthetic_executable.py`) covering: valid response,
  malformed-then-valid recovery, malformed exhaustion, startup failure,
  timeout, oversized output, non-zero exit, identity/model/route drift,
  missing usage, finite metadata, non-finite metadata, credential output, and
  child-process cleanup. Zero real OpenCode/provider/catalog/account calls,
  exact process-attempt/logical-call accounting, fresh per-case boundaries,
  and correct cleanup proven by tests and the self-test mode.
- Validation: new unit suites 76 passed (configuration 40, transport 24,
  case-runner 12), new CLI integration suite 10 passed; existing live-runner
  266, paired-pilot 267, live-quixbugs/opencode-transport/live-evaluation/
  model-adapter/controller/controller-policy/quixbugs-adapter/verifier 456
  passed; full unit suite 2783 passed (3 skipped); integration suite 357
  passed; golden-trajectory suite 11 passed; v1 and v2 paired-pilot validators
  pass; `python -m py_compile` on all changed Python files passes; `git diff
  --check` clean. No live campaign, benchmark, model, provider, catalog, or
  paid endpoint was contacted; no real OpenCode binary was executed.

Not marked complete and not started: operator authorization, real route
preflight, real OpenCode Go execution, the six-case live campaign, empirical
evaluation, model performance, PDB effectiveness, RAG, SFT, and DPO.
Historical OpenCode Zen records remain historical and unchanged.

2026-08-02 (QuixBugs paired-pilot v2 live-runner infrastructure task)
- Final (third) material repair round: (1) crash-safe terminal package
  commitment — terminalization is a three-step durable protocol
  (campaign.json PREPARED payload, ledger terminalization, create-once
  terminal-commit.json written last binding attempt identity, authorization
  hash, execution commit, status, campaign SHA-256, exact terminal ledger
  entry SHA-256, manifest hash, and case inventory); a standalone
  campaign.json is never accepted without the commitment; verify_attempt_package
  and all loaders reject uncommitted/interrupted packages
  (TERMINAL_COMMIT_MISSING); fault injection covers every step including a
  BaseException simulated process death; interrupted attempts are never
  silently resumed. (2) Authority-invalidated cases — post-case drift
  invalidates the affected case (lifecycle authority-invalidated, excluded
  from completed_case_count, counted in invalidated_case_count, preserved
  only as quarantined evidence with the authority record hash and
  provider-contact flag); reconciliation is now
  completed + blocked + aborted + invalidated + unstarted == 6; final-case
  drift yields PARTIAL with completed 5 / invalidated 1 / unstarted 0 and the
  affected final case ID; pre-terminal drift is a separate campaign-level
  failure with affected case ID null. Post-repair counts: live-runner suite
  266 passed; paired-pilot suites 267 passed.

2026-08-03 (OpenCode Go execution adapter v1 wrapper repair, adapter-only)

Bounded surgical repair of the OpenCode Go execution adapter. (1) The active
adapter command now explicitly launches the accepted protocol wrapper
(scripts/opencode_protocol_transport.py) with the exact authorization-bound
model identity, variant, and --route-mode opencode-go plus the route-binding
flags (expected OpenCode version, catalog fingerprint, runtime model id,
account status, billing route); --evidence-file is appended only because the
wrapper owns that argument. Direct OpenCode CLI commands that bypass the
wrapper are rejected (DIRECT_OPENCODE_COMMAND_REJECTED / WRAPPER_NOT_BOUND);
the tracked template shows the wrapper form with placeholders only. (2) The
wrapper gained explicit route modes: legacy (default; historical OpenCode Zen
zero-price behavior preserved unchanged, all existing wrapper tests pass) and
opencode-go (catalog prices preserved as observed and never required to be
zero; launcher version must equal the expected version exactly; the
outer-validated model/fingerprint/account/billing-route evidence is required
and recorded; no hidden fallback, model selection, or Zen/free-tier
inference). (3) The case execution cost is the aggregate of the finite
monetary costs explicitly reported by each provider response; absent cost
metadata stays absent (schema zero is the absence representation, never a
fabricated reported zero), explicit zero stays zero, subscription access never
implies zero, and the preflight route-observation cost is never used as the
case execution cost. The frozen v2 case validator cost-equality check was
relaxed to the truthful non-negative-finite contract (directly affected
compatibility fix; paired-pilot v2 suite 88 passed). (4) Synthetic validation
runs the fake OpenCode CLI through the real wrapper (request via stdin,
bounded opencode run command, response reaching the model-adapter boundary),
covering absent/zero/positive cost propagation, drift through wrapper
telemetry, credential redaction, and child cleanup; zero real provider calls.
Focused checks: wrapper repair 12, configuration 45, transport factory 24,
case runner 13, CLI integration 10, wrapper transport 30, paired-pilot v2 88,
cost-focused live-runner/paired-pilot 7 — all passed; py_compile and git diff
--check clean. No live campaign, benchmark, model, provider, catalog, or paid
endpoint contacted; no commit/stage/push. Not marked complete: operator
authorization, real route preflight, real OpenCode Go execution, six-case
live campaign, empirical evaluation, model performance, PDB effectiveness,
RAG, SFT, DPO.

2026-08-03 (Operator Authorization and Real Route Preflight v1, operator preparation; open)

Implemented and packaged the operator preparation flow; no real OpenCode
inspection command was executed by the implementation agent, and validation
was intentionally not run (validation belongs to FirstMate). Two focused
operator-facing modes were added to scripts/quixbugs_opencode_go_adapter.py:

- route-capture: read-only; runs only local/non-model OpenCode inspection
  commands (opencode.cmd --version and opencode.cmd models opencode-go
  --verbose --pure); never invokes opencode run; requires the exact
  operator-selected runtime model ID (rejects the historical
  opencode/deepseek-v4-flash-free Zen identity and every non-opencode-go/
  provider) and variant; locates exactly one active catalog entry and
  records observed status, variant availability, and finite pricing metadata;
  requires explicit operator-supplied account status, subscription
  entitlement confirmation/reference, and billing-route assertion; records
  every denial/fallback observation explicitly; writes a strict
  quixbugs-route-evidence-v1 artifact (accepted by the existing live-runner
  validator, schema validated through the new public
  runner.validate_raw_route_evidence wrapper) with create-once semantics into
  the ignored operator/ storage; contains no credentials or raw private
  account data.
- operator-bundle: consumes the accepted route-evidence file and materializes
  the real quixbugs-paired-pilot-authorization-v1 artifact and the real
  quixbugs-opencode-go-execution-adapter-v1 configuration, bound to the
  actual clean Git HEAD observed (read-only) when the operator runs the
  command after the task has been accepted and merged (never a
  caller-supplied commit; the task baseline
  618c33ff186493892665ca1233c3edd8b2eec13f is retained only as a minimum
  lineage prerequisite), the frozen manifest hash
  bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171, the exact
  six frozen case IDs in order, protocol 1.3, the exact observed OpenCode
  version, runtime model ID, variant, and catalog fingerprint, the account
  status and subscription billing route, one operator authorization ID, one
  fresh attempt identity and output root, an explicit bounded validity
  period, and the operator-resolved Python executable, repository wrapper
  path, working directory, and operator boundary root. Rejects dirty/staged
  source, drift, occupied targets, template values, route drift, unknown
  fields, malformed paths, and contradictory subscription/fallback
  assertions; active operator artifacts are never committed.

A deterministic catalog-entry fingerprint contract is implemented once in
scripts/opencode_protocol_transport.py (parse the exact selected entry,
serialize with the project's canonical JSON rules, SHA-256; shared
select_catalog_entry and catalog_entry_facts parsing) and is used identically
in route evidence, authorization, adapter configuration, and wrapper
verification; the wrapper's OpenCode Go preflight independently recomputes
the selected entry fingerprint and compares it with the authorization-bound
expected fingerprint before any model process may run. The materialized
artifacts work with the existing zero-provider-process route-preflight-only
command (PowerShell example in
docs/datasets/quixbugs/opencode-adapter.md).

Tests added: deterministic catalog-entry fingerprinting; exact selected-entry
matching; malformed, duplicate, inactive, missing-variant, and historical
free-route rejection; route evidence schema production; authorization/config
cross-binding; dirty-Git and occupied-target rejection; execution-commit
binding to a clean descendant HEAD different from the task baseline;
rejection of nonexistent, non-descendant, dirty, staged, and drifting HEADs
(including drift between observation and the pre-materialization recheck);
wrapper fingerprint mismatch rejection; and proof that capture never
constructs or invokes opencode run (verified against the actual command
inventory). Existing adapter/wrapper/transport fixtures were updated so
every wrapper OpenCode Go preflight carries the exact synthetic catalog-entry
fingerprint the wrapper recomputes.

Real operator preflight remains pending FirstMate review and Onur's manual
execution (TODO item kept open). Not marked complete: real operator
authorization execution, real route preflight, real OpenCode Go execution,
six-case live campaign, empirical evaluation, model performance, PDB
effectiveness, RAG, SFT, DPO.

2026-08-03 (Operator Authorization and Real Route Preflight v1 - execution-commit repair)

Confirmed blocker repair: CAMPAIGN_EXECUTION_COMMIT was hardcoded to the task
baseline 618c33ff..., so operator-bundle rejected the dirty pre-commit tree
and would reject the post-commit HEAD. The bundle now resolves the execution
commit through read-only Git inspection at materialization time:

- CAMPAIGN_EXECUTION_COMMIT was renamed to TASK_BASELINE and is retained only
  as the minimum accepted lineage/task baseline - it is never used as the
  generated authorization's accepted_campaign_commit.
- observe_bundle_execution_head resolves the actual Git HEAD via read-only
  git commands (rev-parse, cat-file -e, merge-base --is-ancestor against the
  accepted project baseline and the task baseline, status --porcelain, and
  check-ignore) and requires: a valid existing commit; descent from both the
  accepted project baseline and the task lineage baseline; a clean tracked
  working tree; a clean real index; and no non-ignored untracked files.
- No caller-supplied execution commit is accepted; route capture remains
  independent of Git commit binding.
- The same independently observed HEAD is used in authorization
  accepted_campaign_commit, adapter configuration execution_commit, the
  route-preflight execution binding, the runtime identity binding, and the
  returned operator-bundle record (which also reports task_baseline).
- HEAD and repository cleanliness are re-checked immediately before the
  authorization and configuration files are created; any drift between
  observation and materialization fails closed and creates neither active
  artifact.

Test-source repair: a clean descendant HEAD different from 618c33f... is
accepted and becomes the exact generated execution commit; mismatched
(drifting), nonexistent, non-descendant, dirty, staged, and non-ignored
untracked HEADs are rejected; authorization, adapter configuration, route
preflight, and the returned bundle record contain the same independently
observed HEAD; the task baseline is retained only as a lineage requirement
(the bundle rejects a HEAD that descends from the project baseline but not
from the task baseline). The route-capture assertion test no longer passes
account_status twice; the no-opencode-run proof inspects the actual command
inventory instead of substring presence across fields such as runtime_model_id
or run_invoked; expected catalog fingerprints are derived from the exact
catalog fixture used by each test. No validation run (FirstMate owns
validation). Real operator preflight remains pending FirstMate review and
Onur's manual execution; operator-bundle binds artifacts to the clean current
HEAD present after Git closeout.

2026-08-03 (OpenCode Go catalog provider selection repair, adapter-only)

Real Windows inspection proved that Go mode queried
`opencode.cmd models opencode --verbose --pure` and therefore observed the
historical Zen/free identity `opencode/deepseek-v4-flash-free`. Repair of the
route-capture and protocol-wrapper paths:

- OpenCode Go mode now queries exactly `models opencode-go --verbose --pure`;
  `scripts/opencode_protocol_transport.py` resolves the catalog command by
  route mode (legacy mode keeps `models opencode` unchanged), and the
  operator `route-capture` uses the Go provider exclusively (the route-mode
  `_catalog_command` shared with the wrapper).
- Go runtime identities must use the `opencode-go/` provider prefix
  (`GO_RUNTIME_ID_PREFIX` in the adapter; `OPENCODE_GO_RUNTIME_ID_PREFIX` in
  the wrapper): `opencode/`, the historical
  `opencode/deepseek-v4-flash-free` identity, and any other provider are
  rejected before model execution by the wrapper's OpenCode Go preflight
  (`_require_go_runtime_identity`, before any catalog query or `opencode
  run`), by the operator `route-capture`, by the `operator-bundle` route-
  evidence gate, and by the strict adapter-configuration validator
  (`PROVIDER_MISMATCH`).
- The selected `opencode-go/<model>` catalog entry remains fingerprinted with
  the deterministic contract, and the wrapper's OpenCode Go preflight still
  independently recomputes and verifies that fingerprint against the
  authorization-bound expected fingerprint before any model process; the
  wrapper evidence records the queried `catalog_provider`.
- Route capture still never constructs or runs `opencode run` (command-
  inventory proof retained). The operator PowerShell example now uses
  `--runtime-model-id opencode-go/deepseek-v4-flash`; no model variant is
  invented before the real Go catalog is inspected.
- Directly affected tests were updated (route-capture, operator-bundle,
  wrapper-repair, operator route-preflight CLI integration), plus a focused
  Go-mode provider-rejection wrapper test; the synthetic executable contract
  now documents `models opencode-go --verbose --pure`.

The existing real-operator-preflight TODO item stays open pending the
repeated Windows route capture. No validation was run (FirstMate owns
validation); no real OpenCode command, catalog, provider, or paid endpoint
was contacted; no commit/stage/push.

2026-08-03 (QuixBugs multi-task PDB live-wire repair, adapter + live path)

Repair of the final live-wire integration blocker: the frozen six-case
campaign starts with a pdb-on-uncertainty case
(quixbugs-find-in-sorted-smoke-v1), but the established live path locked PDB
to the historical quixbugs-gcd-smoke-v1 task, always prepared the gcd probe,
could not execute the reviewed task-local probes frozen in
PAIRED_PILOT_V2.json, and called one zero-argument generic facts provider per
task while the QuixBugs dependency gate requires DependencyPreparation bound
to the exact task manifest/fingerprint/algorithm/revision — so live-wire
aborted before the six-case comparison. Bounded repair of the established
live path (no parallel campaign runner):

- Task-local PDB probe: run_live_quixbugs_case now takes an explicit
  task-local RuntimeProbe for pdb-on-uncertainty; static-baseline accepts no
  probe and keeps zero PDB access; PDB requires the selected task's own
  reviewed probe validated against the selected task ID (the default gcd
  probe keeps its gcd lock), buggy module path, corrected-source/test/support
  exclusion, reviewed target symbol, source containment, and a resolvable
  breakpoint anchor (validate_quixbugs_runtime_probe_identity is now public
  in agentic_debugger/quixbugs/contained_pdb.py, with the corrected-source
  exclusion check reachable ahead of the buggy-path match); probe preparation
  uses prepare_quixbugs_pdb_probe; the historical standalone GCD APIs
  (prepare_quixbugs_gcd_pdb_probe, the run_live_quixbugs_evaluation gcd PDB
  lock, and the default GCD runtime probe) remain unchanged, and the
  contained-PDB, resource, cleanup, and identity gates are not weakened.
- Adapter case binding: OpenCodeGoCaseRunner resolves the exact inventory
  entry per frozen case (missing/duplicate entries rejected at construction
  for all six cases and re-validated per case), builds each PDB case's probe
  only from the entry's frozen runtime_probe fields (module_path,
  focus_function, call_expression, breakpoint_anchor, inspect_names; never
  derived from corrected source, tests, model output, or runtime guesses),
  rejects missing/malformed/mismatched/duplicate probe metadata before any
  provider interaction, and passes the probe only for pdb-on-uncertainty. The
  three selected PDB tasks are quixbugs-find-in-sorted-smoke-v1,
  quixbugs-is-valid-parenthesization-smoke-v1, and quixbugs-hanoi-smoke-v1.
- Task-bound facts provider: the contract is now
  provide(manifest_path: str) -> QuixBugsPreflightFacts; the case runner
  calls the provider separately for every frozen case with the exact manifest
  path, requires an exact QuixBugsPreflightFacts result, and enforces that its
  dependency preparation matches the selected task manifest (pilot_task_id,
  manifest_fingerprint, authority_revision, bug_id); zero-argument generic
  facts providers (rejected at live-wire resolution and wrapped at call
  time), wrong-task facts, and malformed results fail before provider
  execution; --facts-provider module:callable operator selection is
  preserved.
- Operator facts provider module (scripts/quixbugs_live_wire_environment.py):
  reuses the accepted read-only WSL/Bubblewrap readiness verification
  (_verify_environment_ready); never installs, clones, resets, cleans, or
  downloads; creates task-bound verified facts from the selected manifest;
  exposes describe_environment() returning the existing repository root and
  sources parent needed to materialize quixbugs-environment.json. The WSL
  execution architecture is not duplicated.
- Tests (focused; no unrelated cleanup): each of the three selected PDB cases
  receives its own exact reviewed probe; static cases receive no probe and
  retain zero PDB access; non-GCD PDB cases are no longer rejected merely
  because they are non-GCD (full contained-PDB pipeline on
  find-in-sorted with its reviewed probe); missing/mismatched/duplicate probe
  metadata fails before provider execution; GCD-only legacy/default APIs
  remain unchanged; facts are requested separately with the exact manifest
  path; wrong-task dependency facts are rejected before the executor; a
  zero-argument generic facts provider is rejected; the six-case runner
  enters all six case bindings with synthetic transport and no real provider;
  the live-wire CLI integration fixtures were updated to the task-bound
  contract and a zero-argument-provider rejection test was added.

Validation was intentionally not run (FirstMate owns validation); no real
OpenCode command, catalog, provider, or paid endpoint was contacted; no
commit/stage/push. The live campaign TODO stays open pending FirstMate
review and real operator execution; not marked complete: real operator
authorization execution, real route preflight, real OpenCode Go execution,
the six-case live campaign, empirical evaluation, model performance, PDB
effectiveness, RAG, SFT, DPO.

2026-08-03 (OpenCode Go isolated route-capture environment repair, wrapper + route-capture)

Fresh attempt quixbugs-paired-pilot-v2-attempt-4c7fc4445de54c8d9a33f8ab9a23fd97 reached
all six case bindings but all 18 transport attempts failed before model inference with
catalog fingerprint drift: the wrapper independently recomputed a fingerprint under its
deterministic isolated OpenCode configuration that did not equal the authorization-bound
expected fingerprint recorded by operator route capture under the ambient user OpenCode
configuration (b3b63d9c... != b68d7e09...); all six cases ended PROVIDER_ERROR with zero
directives/patches/verifier runs/tokens. The exact catalog-entry fingerprint contract was
not weakened; route capture now observes the catalog under the SAME deterministic isolation
environment and effective configuration contract the wrapper uses, through one shared
isolated catalog-observation path:

- scripts/opencode_protocol_transport.py: observe_isolated_catalog(...) is the single
  explicit isolated catalog-observation path used by both operator route capture and wrapper
  catalog verification. It creates a temporary deterministic isolation root (helper-owned
  when isolation_root is None; wrapper-owned for the run phase), prepares it with
  route_mode="opencode-go", requires the exact effective configuration (enabled providers
  exactly ["opencode-go"] plus the existing permission/MCP/plugin/instruction/sharing/
  autoupdate denials), runs only the local/non-model inspection commands under the isolated
  environment (opencode.cmd --version and opencode.cmd models opencode-go --verbose --pure),
  selects the exact opencode-go/deepseek-v4-flash entry through the shared
  select/facts/fingerprint path, computes the existing exact-entry canonical JSON SHA-256
  fingerprint, and always removes the helper-owned temporary isolation root (success or
  failure); opencode run is never constructed or executed. The wrapper (_preflight and main)
  passes its own root plus the authorization-bound expected fingerprint/version/runtime
  identity so the independent fingerprint comparison happens inside the shared path; route
  capture passes no expected fingerprint (pure observation). Catalog command/parse/route
  checks were consolidated into _catalog_entry_observation and _enforce_catalog_route_checks
  (legacy zero-cost gate and Go drift messages preserved byte-for-byte).
- scripts/quixbugs_opencode_go_adapter.py: run_route_capture now calls
  transport.observe_isolated_catalog(runtime_model_id, variant, route_mode="opencode-go");
  the strict quixbugs-route-evidence-v1 schema, create-once semantics and operator-storage
  boundary are unchanged; the companion capture record carries a bounded observation_mode
  block (mode isolated-opencode-go, effective provider allowlist, isolation/config validation
  passed, temporary isolation cleaned, run_invoked false, model_requests 0). No auth
  contents, copied auth data, credentials, environment dumps, or unrestricted catalog output
  are recorded. Ambient _run_catalog_inspection/_resolve_catalog_command and the 1 MB capture
  bound were removed (the shared path is the only catalog source).
- Legacy behavior unchanged: wrapper provider remains opencode, historical zero-cost checks
  remain, no new legacy route-capture behavior.

Focused tests: ambient and isolated catalog entries may differ and capture fingerprints the
isolated entry (never the ambient entry, every inspection under the isolated environment);
the capture fingerprint exactly equals the wrapper independent isolated recomputation (a
wrapper preflight bound to the captured fingerprint passes); Go capture effective config
allows exactly ["opencode-go"]; route capture never constructs/runs opencode run; temporary
isolation cleanup on success and failure; catalog/version failures stay typed
(catalog_command_failed) and bounded; shared helper keeps caller-owned-root cleanup to the
caller, compares expected fingerprints when supplied, and preserves the legacy zero-cost
gate; CLI integration fake shim now serves debug config with a fixture auth.

Validation was intentionally not run (FirstMate owns validation); no real OpenCode command,
catalog, provider, or paid endpoint was contacted; no commit/stage/push. The Authorized
Six-Case Live Campaign TODO stays open pending a fresh attempt after this repair; both
previous attempts remain classified as infrastructure-failed attempts, not valid
experiments. Not marked complete: real operator authorization execution, real route
preflight, real OpenCode Go execution, the six-case live campaign, empirical evaluation,
model performance, PDB effectiveness, RAG, SFT, DPO.

2026-08-04 (Case-level public-evidence terminal completion and paired-pilot v3)

- The original `8890ed...` provider-connected case exceeded the frozen public
  evidence budget (`21949 > 20000`) after useful controller progress; the old
  runner aborted before materializing the case and omitted its completed
  resource accounting from campaign aggregates. Expected, internally
  consistent public-evidence exhaustion is now a typed case-level terminal
  evaluated only after every other budget and relational invariant.
- Supported shapes preserve calls, attempts, directives, tokens, provider-
  reported cost, hypotheses, controller states, PDB/patch/verifier activity,
  timing, transport evidence, cleanup/restoration evidence, and the exact
  observed byte count in the termination detail. The public counter is clamped
  to the frozen 20,000-byte report limit, the validated case is written, and
  the campaign continues. Corrupt or unsupported shapes still abort.
- Existing v2 terminals cover no-contact, pre-PDB, completed unresolved, and
  completed resolved shapes. Paired-pilot v3 (manifest SHA-256
  `f5f513a16008ce807b4ed248e0310958940aefd348199e77dc0bbabc9a9e45cf`)
  preserves the v2 route, tasks, order, budgets, qualification contract, and
  source authority while adding `VALIDATION_NOT_REACHED` and candidate
  provenance for the observed static pre-Validate candidate-applied shape.
- The next live attempt is v3. Every operator command must pass
  `--manifest research/quixbugs/PAIRED_PILOT_V3.json`; the v2 CLI default is
  retained only for compatibility. Fresh v3 route evidence, authorization,
  adapter configuration, attempt identity, bundle, and output root must be
  created against the clean accepted execution HEAD.
- Campaign reconciliation remains six lifecycle slots. The descriptive
  denominator is six total cases and three per policy; budget-terminal cases
  remain completed, authority-invalidated cases are excluded from evaluation
  but retained in resource accounting, and blocked/aborted/unstarted cases
  remain visible. No real campaign or empirical/PDB-effectiveness result is
  marked complete.

2026-08-04 (Paired-pilot v3 live attempt fddf1e39... and v4 preregistration)

- The v3 live attempt `fddf1e39...` (case 1, find-in-sorted / pdb-on-uncertainty)
  progressed through the full pre-verifier lifecycle and beyond: baseline
  reproduction (attempt 1 accepted and dispatched), transition to Understand,
  three source reads, an add_hypothesis, transition to Patch, apply_patch,
  transition to Validate, a post-patch reproduction and a regression-test run,
  and a transition to Done (attempt 13) with the independent verifier executed.
  Accounting observed: 12 logical model calls, 13 provider process attempts
  (12 completed responses + 1 bounded retry after an attempt-10 no_text_event
  stream), 12 valid directives, 1 retry, 1 applied candidate, 0 PDB
  observations, 33,685 cumulative public evidence bytes, provider-reported
  cost 0.010565556 (aggregate of 12 reported costs).
- The campaign aborted honestly as ABORTED / BUDGET_EXCEEDED: the frozen v3
  terminal matrix has no representation for provider contact + applied
  candidate + Validate visited + verifier executed + public-evidence
  exhaustion after the completed lifecycle (`_budget_exhausted_outcome`
  returned None). This is the preregistered v3 contract working as designed;
  it is not an extraction, validation, or acceptance failure. The case-level
  truth is preserved in the private transport evidence under
  `operator/attempts/quixbugs-paired-pilot-v3-attempt-fddf1e39.../private/`;
  campaign-level `counts` are zero except provider_process_attempts because
  aborted cases materialize no case record.
- Preregistered `research/quixbugs/PAIRED_PILOT_V4.json` (canonical SHA-256
  `020dfc1f7b8f23aa96a4d7c7942429e306cc290906abfed5ce96cde22b90354d`),
  derived from the frozen v3 manifest, adds: (a) v4-only verifier-authoritative
  classification (`_finalize_live_case(campaign_version=4)`: a case whose
  verifier executed is classified by the verifier semantic outcome before the
  PDB_NOT_REACHED rule; v2/v3 behavior unchanged); (b) the budget-terminal
  matrix for the observed completed post-apply shape (RESOLVED / UNRESOLVED
  with all accounting preserved and the exact observed byte count in the
  termination detail, counter clamped to the frozen 20,000 limit);
  (c) VALIDATION_NOT_REACHED extended to pdb-on-uncertainty and
  Validate-visited stops (verifier still NOT_RUN); (d) post-contact
  controller/cleanup/evidence-packaging INFRASTRUCTURE_ERROR budget
  terminalization. Unsupported or contradictory shapes still abort; the
  20,000-byte budget is unchanged.
- Sanitized deterministic replay coverage
  (`tests/fixtures/quixbugs_v4_replay_fixture.json` +
  `tests/unit/test_quixbugs_v4_live_attempt_replay.py`) replays the preserved
  evidence through extraction, parse, controller acceptance, no_text_event
  classification, bounded retry accounting, and the observed-shape v4
  terminalization without contacting any provider.
- Ledger timestamp follow-up (nonblocking): campaign `created_at`/`updated_at`
  use the campaign-start `reference_time`, so ledger timestamps do not reflect
  campaign end; fix in a separate task.
- No route capture, authorization, preflight, or live campaign executed in
  this task; v4 requires fresh operator artifacts against the clean accepted
  execution HEAD.

2026-08-05 (Campaign infrastructure accepted on main; V4 attempt record; QLoRA implementation)

- The campaign infrastructure and paired-pilot v4 terminal contract are
  accepted on main through commit `0abb588`: `eb63c76` hardened the campaign
  budget and verifier path, `9f53df7` added the actual V4 interrupted budget
  terminal, and `0abb588` added the terminal, exact-identity validation, and
  fail-closed budget-exhaustion provenance infrastructure (run persistence,
  campaign-record validation, and attempt-package verification).
- Accepted campaign validation: the focused campaign integration suite passed
  389 tests; the bounded full suite produced 3394 passed, 3 skipped, and the
  same six pre-existing OpenCode wrapper/transport failures (no new failure).
- Recorded V4 attempt `quixbugs-paired-pilot-v4-attempt-3b5d7488...`
  (preserved under ignored `operator/attempts/`): exact case boundaries are
  Case 1 = `find-in-sorted` / `pdb-on-uncertainty` (order 1): 10 provider
  processes, 9 logical calls, 1 retry, 26,139 public-evidence bytes, malformed
  unified-diff (hunk-header) rejection, no candidate, 0 verifier runs,
  `$0.007378`, `INFRASTRUCTURE_ERROR`; Case 2 = `find-in-sorted` /
  `static-baseline` (order 2): 15 provider processes, 14 logical calls, 1
  retry, 38,534 bytes, patch applied with Validate visited, 0 verifier runs,
  interrupted, `$0.012323`; the original campaign aborted
  `ABORTED/BUDGET_EXCEEDED`, now representable as schema-valid
  `INFRASTRUCTURE_ERROR` / `ABORTED/INTERRUPTED` terminals with exact
  observed byte counts in `budget_exhaustion` provenance and counters clamped
  to the frozen 20,000-byte limit.
- The fixture identity correction (2026-08-05): the sanitized attempt fixture
  and replay assertions accepted at `0abb588` associated the two observed
  shapes with the wrong frozen cases; that fixture/test identity mapping was
  corrected per the preserved campaign record and private transport (the
  26,139-byte malformed shape to `find-in-sorted` / `pdb-on-uncertainty`
  order 1, the 38,534-byte applied-patch interrupted shape to
  `find-in-sorted` / `static-baseline` order 2) and is accepted on `main` at
  `fc7c85b` — it is no longer a pending Friday-readiness candidate.
  Production budgets, manifest, route, provider, authorization, and
  controller behavior are unchanged.
- This repair establishes no verifier-confirmed live repair, demonstrates no
  live PDB benefit, and is not a post-repair provider campaign. The Authorized
  Six-Case Live Campaign remains open and unauthorized; the next authorized
  attempt must use `research/quixbugs/PAIRED_PILOT_V4.json` explicitly with
  fresh operator artifacts.
- QLoRA: the experiment implementation (including the tracked `independent_ai`
  audit contract and run-provenance) is accepted at commit `3f0d3e7` on the
  unmerged branch `experiment/qlora-patch-pilot-v1` (FirstMate implementation
  review passed). Owner suite review: 3457 passed, 3 skipped, 36 unrelated
  pre-existing OpenCode transport/wrapper failures, no QLoRA-focused failure.
  The owner-delegated independent FirstMate AI audit of the 75 frozen corpus
  rows is complete externally (39 ACCEPT / 36 REJECT, disclosed AI reviewer
  identity; an AI audit, not a human audit); fail-closed validation and final
  corpus acceptance remain pending. Final QLoRA training was externally
  authorized by FirstMate on 2026-08-05; no accepted final-training artifact
  exists yet and results are pending FirstMate artifact review. Held-out
  generation and base-versus-tuned comparison remain unauthorized. The
  tracked freeze record at `3f0d3e7` (still carrying `final_training_authorized:
  false` and `held_out_generation_authorized: false`) is the historical
  branch-bound freeze record, not evidence about the current external
  authorization. No instructor TODO status is promoted by this entry.
