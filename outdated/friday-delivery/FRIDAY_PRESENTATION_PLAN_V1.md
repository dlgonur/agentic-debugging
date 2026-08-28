# Friday Presentation Plan v1

**Document version:** 1.2
**Snapshot date:** 2026-08-05
**Source baseline:** `456f0e9a6576aab912f5af5980d756ff4e1e9dc3` is the accepted presentation plan/deck/cue delivery commit and the source baseline for this task's final-delivery candidate; campaign infrastructure accepted through `0abb588df46605a9a754c051e71ebe17692c09db`; V4 identity correction accepted through `fc7c85b9858eba993f6bacc8ea9b4f805873f1a5`. Version 1.1 of this document was prepared from `fc7c85b`; version 1.2 updates the baseline identity and links the delivery bundle (`docs/FRIDAY_DELIVERY_MANIFEST_V1.md`, `docs/FRIDAY_PREFLIGHT_CHECKLIST_V1.md`, `docs/FRIDAY_STATUS_HANDOFF_V1.md`) — an uncommitted candidate built on top of `456f0e9` during review, since integrated on `main` at `ab464dd`.
**Base commit:** `456f0e9a6576aab912f5af5980d756ff4e1e9dc3` (accepted presentation delivery commit; `main` head on 2026-08-05); campaign infrastructure accepted on `main` through `0abb588df46605a9a754c051e71ebe17692c09db`; V4 identity correction accepted through `fc7c85b9858eba993f6bacc8ea9b4f805873f1a5`. On presentation day, run from clean `main` matching `origin/main`, containing the delivery bundle files and descending from `456f0e9`.
**Presentation duration target:** 20–25 minutes talk plus demo and Q&A (primary), with a 10–12-minute shortened track
**Scope boundary:** This is a professor-facing presentation and demo runbook only. It is not a slide deck, not the final technical report, not a claim that the project is a finished automated repair system, not an authorization to run another provider campaign, and not an authorization for final QLoRA training or held-out generation. It changes no code and authorizes no execution.

**Instructor status snapshot** (from `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md`, snapshot base `4087aa0`, 2026-08-05):

COMPLETED 7 / PARTIAL 10 / IN PROGRESS 3 / NOT STARTED 7 / BLOCKED 0 — total 27.

---

## 1. Presentation objective

Present one honest, evidence-backed picture of the internship:

- the instructor's original 27-item scope and its current status;
- a working single-controller agentic debugging architecture with typed tools, a real PDB path, and an independent verifier;
- real execution evidence: the deterministic demo, the QuixBugs infrastructure baselines, and the first real live-model observations;
- explicit limitations: no verifier-confirmed live repair yet, zero live PDB observations, no valid static-versus-PDB comparison;
- the QLoRA fine-tuning experiment with its verified state and its still-pending final results;
- measurable next steps tied to the instructor list.

The talk must make clear that the project delivers infrastructure, evaluation methodology, and honest first findings — not a finished general-purpose automated repair system.

The primary live demonstration is the deterministic offline demo (Section 5). The QuixBugs v4 provider result is presented as a recorded experiment, never as the live demo.

Language: the narrative is suitable for Turkish delivery while retaining the established English technical terminology (controller, verifier, policy, PDB, unified diff, fail-to-pass, pass-to-pass, QLoRA, adapter, held-out).

## 2. Recommended narrative order

| # | Segment | Time | One-line message |
|---|---|---|---|
| 1 | Instructor scope and honest status map | 3 min | The 27-item list is the contract; the status map reports each item with evidence. |
| 2 | Agentic-debugging architecture | 4 min | One controller, typed deterministic tools, a real PDB path, and an independent verifier that does not trust the model. |
| 3 | Dataset and model selection | 3 min | BugsInPy primary but license-blocked; QuixBugs licensed fallback; Qwen2.5-Coder-7B-Instruct frozen. |
| 4 | Deterministic working-system evidence | 3 min | The demo runs end-to-end: 10/10 verifier RESOLVED, F2P 10/10, P2P 22/22. |
| 5 | Real live-model findings | 4 min | Real provider interaction happened; the model diagnosed and proposed the correct fix; zero verifier-confirmed repairs and zero PDB observations. |
| 6 | Honest limitations | 2 min | Infrastructure works; live-model repair and PDB effectiveness are not yet demonstrated. |
| 7 | QLoRA experiment and current results | 3 min | Frozen methodology and a leakage-checked real corpus; final training and comparison pending. |
| 8 | Demo, roadmap and questions | 5–7 min | Deterministic demo; roadmap mapped to the instructor items; open questions. |

## 3. Section-by-section speaking outline

For each segment: objective, concise speaking points, evidence to show, one sentence that may be said, one overclaim that must not be said, and the transition.

### 3.1 Instructor scope and honest status map (3 min)

- Objective: show the professor that the internship is tracked against the original 27-item checklist with an honest, evidence-based status.
- Speaking points:
  - The original instructor TODO is 27 items, preserved byte-identical (`docs/INSTRUCTOR_AGENTIC_DEBUGGING_TODO.md`).
  - The status map assigns exactly one status per item: 7 COMPLETED, 10 PARTIAL, 3 IN PROGRESS, 7 NOT STARTED, 0 BLOCKED.
  - Completed items are limited to what evidence supports: five named system reviews, dataset research and comparison, tool development, the agent itself, model selection (branch-bound), and the working demo + report.
  - In-progress items are the corpus/audit, instruction-response transformation, and QLoRA fine-tuning.
- Evidence to show: the summary table of `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md`; the legend and evidence-layer rules of the same document.
- May be said: "The instructor's 27-item list is our contract; every item is mapped to repository evidence, and nothing is marked complete without that evidence."
- Must not be said: "Most of the 27 items are essentially done, including fine-tuning and evaluation."
- Transition: "Now I will show the architecture this evidence is built on."

### 3.2 Agentic-debugging architecture (4 min)

- Objective: explain what the system actually is — a single controller agent over typed, deterministic tools with a debugger path and a verifier.
- Speaking points:
  - Single controller, not multi-agent (AGENTS.md accepted scope; `agentic_debugger/agent/controller.py`, `state_machine.py`, `controller_policy.py`, `tool_registry.py`).
  - State machine: reproduce → understand → (PDB gate) → patch → validate.
  - Typed deterministic tools: file read, code search, test run, patch apply (unified diff), plus a real PDB session over a worker subprocess (breakpoints, stack/frame/locals inspection, safe AST-allowlisted evaluation; `runtime/pdb_session.py`, `pdb_worker.py`, `pdb_protocol.py`).
  - Independent verifier as the correctness authority: baseline reproduction, syntax check, F2P/P2P/full suite, canonical-fixture immutability, workspace cleanup — run from a clean baseline (`evaluation/verifier.py`, `outcome_taxonomy.py`).
  - Disposable per-case workspaces and event trajectories with replay verification (`runtime/workspace.py`, `events/logger.py`, `events/replay.py`).
  - The lifecycle is identical for the offline scripted stand-in, a live model, and gold-patch baselines — the verifier stays authoritative in all three.
- Evidence to show: `docs/FINAL_TECHNICAL_REPORT_V1.md` Section 2 component map; a demo trajectory event file (Section 5).
- May be said: "The model proposes; the independent verifier decides — a model claim is never treated as proof of success."
- Must not be said: "The architecture proved that debugger-assisted agents outperform static repair."
- Transition: "Now: why these datasets, and why this model."

### 3.3 Dataset and model selection (3 min)

- Objective: justify dataset and model choices from the recorded decision documents.
- Speaking points:
  - Evaluation: BugsInPy primary (real Python project bugs) but execution remains license-blocked; QuixBugs Python is the licensed fallback; the five curated fixtures are the architecture smoke gate; SWE-bench deferred; Defects4J no-go (`docs/DATASET_EVALUATION_DECISION_V1.md`).
  - Fine-tuning: CommitPackFT Python corpus selected with a source-license allowlist; five curated tasks held out (`experiments/qlora_patch_pilot_v1/freeze_record.json` on the experiment branch).
  - Model: Qwen/Qwen2.5-Coder-7B-Instruct, exact revision pinned, Apache-2.0.
- Evidence to show: the executive decision tables in `docs/DATASET_EVALUATION_DECISION_V1.md` and `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md`; the freeze record on the experiment branch.
- May be said: "Dataset choice followed licensing, Python/PDB fit, and oracle quality — BugsInPy was selected first but could not be executed under its license gate."
- Must not be said: "We evaluated our system on BugsInPy."
- Transition: "Next, what actually runs today, deterministically."

### 3.4 Deterministic working-system evidence (3 min)

- Objective: show that the full stack — controller, tools, workspace, PDB path, patcher, verifier, cleanup — works end-to-end.
- Speaking points:
  - Task 9 demo: 5 curated tasks × 2 policies = 10 cases; all verifier `COMPLETED`/`RESOLVED`; F2P 10/10, P2P 22/22; localization `CORRECT_TARGET_SYMBOL` 10/10; every workspace cleaned (`docs/DEMO_TASK9.md`, `docs/DEMO_GUIDE_V1.md` Section 2).
  - The demo model is an offline scripted stand-in — it is a plumbing demonstration, not a model-quality result.
  - The PDB-enabled policy path recorded 21 scripted PDB observations; golden trajectory `tests/golden_trajectories/data/pdb-gated-successful-repair.json`.
  - QuixBugs infrastructure baselines: one-task `gcd` real smoke and the eight-task gold baseline (8/8 tasks, 49/49 nodes) with the literal upstream diffs — infrastructure only (`docs/QUIXBUGS_SMOKE_USAGE_V1.md`, `docs/QUIXBUGS_EIGHT_TASK_BASELINE_V1.md`).
- Evidence to show: recorded `results.json` / `technical-evaluation-summary.md` from the demo output directory (produced per Section 5 runbook).
- May be said: "The platform can load a task, sandbox it, apply a patch, and verify the result deterministically — this is evaluation infrastructure that works."
- Must not be said: "Our system repairs bugs with 100% success."
- Transition: "Now the first real-model evidence, and what it honestly shows."

### 3.5 Real live-model findings (4 min)

- Objective: present the QuixBugs v4 attempt as a recorded experiment with precise facts.
- Speaking points (all tracked evidence; see Sections 4 and 6):
  - The campaign infrastructure that can now persist and verify these terminals is accepted on `main` through `0abb588` (terminal, exact-identity validation, and budget-exhaustion provenance, fail-closed through run persistence, campaign-record validation, and attempt-package verification). The sanitized attempt fixture accepted at `0abb588` associated the two recorded shapes with the wrong frozen cases; the accepted 2026-08-05 readiness integration at `fc7c85b` corrects that fixture/replay identity mapping from the preserved campaign record and private transport. Accepted campaign validation: focused suite 389 passed, bounded full suite 3394 passed / 3 skipped with the same six known OpenCode wrapper/transport failures.
  - Real OpenCode Go / DeepSeek V4 Flash interaction occurred (protocol 1.3, subscription route, no fallback).
  - Recorded V4 Case 1 (`find_in_sorted`, PDB policy, order 1): the model correctly diagnosed the semantic defect and proposed the correct one-line semantic change as a unified diff; the patch was rejected by strict hunk-header validation (hunk declared `old_count=7` with a 6-line body) — an honest protocol/format failure, not a semantic one. 10 provider processes, 9 logical calls, 26,139 public-evidence bytes, no candidate applied, zero verifier runs, cost `$0.007378`.
  - Recorded V4 Case 2 (`find_in_sorted`, static-baseline policy, order 2): a patch was applied and Validate was visited, but the campaign exhausted its frozen public-evidence budget (38,534 observed bytes, clamped to 20,000) before verifier execution and the run was interrupted; 15 provider processes, 14 logical calls, cost `$0.012323`. The original campaign aborted `ABORTED / BUDGET_EXCEEDED`; the accepted repair materializes both shapes as schema-valid terminals.
  - Zero live-model repairs reached verifier-confirmed RESOLVED; zero live PDB observations occurred; no valid static-versus-PDB paired comparison was completed.
  - The campaign ended honestly: `ABORTED` / `BUDGET_EXCEEDED`, cases 3–6 unstarted; provider-reported costs preserved (Case 1 `$0.007378`, Case 2 `$0.012323`).
  - Earlier attempts (`705aa047…` protocol-invalid, `81f2e5d8…`/`4c7fc444…` infrastructure-failed, `fddf1e39…` v3 budget-exhausted) are not valid experiments and must not be described as model results.
- Evidence to show: `research/quixbugs/PAIRED_PILOT_V4.json` (frozen contract); the status map's live-campaign boundary section; recorded attempt summary (Section 6). The local preserved review package may be kept available as an optional artifact only.
- May be said: "A real model drove our controller, diagnosed the defect correctly, and proposed the right fix — but no live repair has been verified yet, and PDB has not opened in a live case."
- Must not be said: "The live experiment showed the model repairing QuixBugs tasks."
- Transition: "These findings define our limitations precisely."

### 3.6 Honest limitations (2 min)

- Objective: state the boundaries before any positive framing.
- Speaking points:
  - No verifier-confirmed live repair; zero live PDB observations; no valid paired policy comparison (status map Section 5).
  - QuixBugs baselines are gold-patch (literal upstream diffs), no model; they prove infrastructure only.
  - BugsInPy execution license-blocked; RAG NO-GO-FOR-NOW, DPO NO-GO-FOR-NOW are decisions, not completions.
  - Infrastructure failures and budget-contract limits are reported as such, never as model-performance results.
- Evidence to show: `docs/FINAL_TECHNICAL_REPORT_V1.md` Sections 7.3–7.4 and 9; status map Section 5.
- May be said: "Every number we report is labeled with exactly what it does and does not prove."
- Must not be said: "The remaining work is just polish."
- Transition: "Given the live-model limits, we started the fine-tuning experiment — here is its exact state."

### 3.7 QLoRA experiment and current results (3 min)

- Objective: present verified state vs. pending results, strictly separated (Section 8).
- Speaking points:
  - Frozen: Qwen2.5-Coder-7B-Instruct, exact revision; CommitPackFT Python methodology; train/validation split with zero held-out exact/near leakage and zero repository overlap; minimum-tier real corpus 1,000 train / 150 validation materialized.
  - Implemented and accepted: the QLoRA patch-pilot implementation at commit `3f0d3e7` on branch `experiment/qlora-patch-pilot-v1` (unmerged) passed FirstMate implementation review; this includes the tracked `independent_ai` audit contract integration and complete run-provenance enforcement.
  - Executed: one-step real CUDA QLoRA weight update succeeded; adapter save and reload succeeded (FirstMate-reviewed external evidence, not yet merged or durably tracked on main).
  - External audit: the owner-delegated independent FirstMate AI audit of the 75 frozen corpus rows is complete (39 ACCEPT / 36 REJECT) with a disclosed AI reviewer identity — external evidence, not yet merged or durably tracked on main; final corpus acceptance is still pending.
  - Owner full-suite validation: reviewed at 3457 passed, 3 skipped, 36 unrelated pre-existing OpenCode transport/wrapper failures, no QLoRA-focused failure.
  - Authorized externally by FirstMate on 2026-08-05: final QLoRA training.
  - Pending: final-training results (no accepted final-training artifact exists yet; pending FirstMate artifact review); fail-closed audit validation; corpus acceptance decision; held-out base-versus-tuned generation and comparison (remain unauthorized).
  - Freeze-flag distinction: the tracked freeze record at `3f0d3e7` still carries `final_training_authorized: false`; that is the historical branch-bound freeze record, not evidence about the current external authorization.
- Evidence to show: experiment branch `experiment/qlora-patch-pilot-v1` commit `3f0d3e7`: `freeze_record.json`, `training_config.json`, `transformation_config.json`, `SMOKE_EVIDENCE.md`, `colab/agentic_debugging_qlora_pilot.ipynb`.
- May be said: "The methodology is frozen, the corpus is real and leakage-checked, the implementation passed FirstMate review, the owner-delegated independent FirstMate AI audit is complete externally (39 ACCEPT / 36 REJECT), final training is authorized and its results are pending, and held-out comparison remains unauthorized."
- Must not be said: "Fine-tuning improved the model" (no comparison exists).
- Transition: "Let me show you the deterministic demo, then the roadmap."

### 3.8 Demo, roadmap and questions (5–7 min)

- Objective: run the deterministic demo (Section 5 runbook), then close with the roadmap (Section 12) and Q&A (Section 10).
- Speaking points:
  - Run the demo live per the runbook; narrate task input, controller states, typed tools, PDB-capable path, patch apply, verifier, structured results, cleanup.
  - If the recorded v4 evidence is shown, show it as experimental evidence, not a live demo.
  - Close with the post-Friday roadmap and the three next measurable steps: final-training artifact review and corpus acceptance, held-out comparison, authorized six-case campaign.
- May be said: "Here is the deterministic pipeline running end to end; the live-model findings from earlier remain recorded experiments."
- Must not be said: "Let me show you the model fixing a real bug live."
- Transition: none — end with thanks and questions.

## 4. Evidence path for every material claim

Every material claim in the presentation must trace to tracked repository evidence. The local preserved review package (`_ai-review/…`) may be kept available during the presentation but is never the durable basis for a claim.

| Claim | Tracked evidence |
|---|---|
| Instructor scope is 27 items, unchanged | `docs/INSTRUCTOR_AGENTIC_DEBUGGING_TODO.md` |
| Status map 7/10/3/7/0 with per-item evidence | `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` (summary table line 106; per-item sections) |
| Single-controller architecture | `agentic_debugger/agent/controller.py`, `state_machine.py`, `controller_policy.py`, `tool_registry.py`; `docs/FINAL_TECHNICAL_REPORT_V1.md` Section 2 |
| Typed file-read / code-search / test-run / patch-apply tools | `agentic_debugger/skills/file_skills.py`, `search_skills.py`, `runtime/test_runner.py`, `runtime/patcher.py`, `agentic_debugger/agent/tool_registry.py`; status map item 16 |
| Real PDB path exists (session, breakpoints, inspection, stepping) | `agentic_debugger/runtime/pdb_session.py`, `pdb_worker.py`, `pdb_protocol.py`, `quixbugs/contained_pdb.py`; status map item 22/24 |
| Independent verifier is the correctness authority | `agentic_debugger/evaluation/verifier.py`, `outcome_taxonomy.py`, `task_schema.py`; README "Reusing the curated-task correctness authority" |
| Deterministic demo works: 10/10 RESOLVED, F2P 10/10, P2P 22/22, localization 10/10, cleanup | `docs/DEMO_TASK9.md`, `docs/DEMO_GUIDE_V1.md` Section 2; `agentic_debugger/demo/`; recorded `demo-out/results.json` produced per Section 5 |
| Scripted stand-in, 21 scripted PDB observations, golden trajectory | `docs/DEMO_TASK9.md`; `tests/golden_trajectories/data/pdb-gated-successful-repair.json` |
| QuixBugs baselines are gold-patch, infra-only | `docs/QUIXBUGS_SMOKE_USAGE_V1.md`, `docs/QUIXBUGS_EIGHT_TASK_BASELINE_V1.md` (8/8, 49/49, literal upstream diffs) |
| BugsInPy primary but license-blocked | `docs/DATASET_EVALUATION_DECISION_V1.md`; `docs/BUGSINPY_LICENSE_GATE_V1.md`, `docs/BUGSINPY_PILOT_READINESS_V1.md` |
| SWE-bench DEFER, Defects4J NO-GO | `docs/DATASET_EVALUATION_DECISION_V1.md` Section 2 |
| Route: OpenCode Go subscription, DeepSeek V4 Flash, protocol 1.3, v4 manifest | `CURRENT_AGENT_ROSTER.md`; `research/quixbugs/PAIRED_PILOT_V4.json` (canonical SHA-256 `020dfc1f7b8f23aa96a4d7c7942429e306cc290906abfed5ce96cde22b90354d`) |
| Real live interaction occurred; v4 facts (diagnosis, hunk-header rejection, budget exhaustion, costs, zero PDB, zero verifier runs) | `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` (items 18/24/26 and Section 5 boundaries); `research/quixbugs/PAIRED_PILOT_V4.json`; `TODO.md` 2026-08-03/04/05 entries; `docs/PROJECT_TRACKER.md` (v3 attempt and 2026-08-05 entry); preserved attempt `3b5d7488…` campaign record and private transport (ignored `operator/`, not the durable basis alone) |
| V4 recorded case identities and failure boundaries bound to exact frozen cases; budget-exhaustion provenance fail-closed | `tests/fixtures/quixbugs_v4_budget_verifier_attempt_fixture.json`, `tests/unit/test_quixbugs_v4_budget_verifier_path.py` (attempt `3b5d7488…`; Case 1 = `find_in_sorted`/`pdb-on-uncertainty` order 1, Case 2 = `find_in_sorted`/`static-baseline` order 2 — identity mapping corrected and accepted at `fc7c85b`); campaign infrastructure accepted on `main` through `0abb588` |
| Earlier attempts are not valid experiments | `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` Section 5 (attempts `705aa047…`, `81f2e5d8…`, `4c7fc444…`, `fddf1e39…`) |
| Historical Zen matrix is descriptive-only | `docs/PROJECT_TRACKER.md`; README "[Historical]" note; `CURRENT_AGENT_ROSTER.md` |
| Model selection frozen | `experiments/qlora_patch_pilot_v1/freeze_record.json` on branch `experiment/qlora-patch-pilot-v1` commit `3f0d3e7` (Qwen/Qwen2.5-Coder-7B-Instruct, revision `c03e6d358207e414f1eca0bb1891e29f1db0e242`, Apache-2.0) |
| QLoRA methodology frozen; implementation (incl. tracked `independent_ai` audit contract and run-provenance) accepted at `3f0d3e7` (FirstMate implementation review); held-out generation still unauthorized | Historical branch-bound freeze record at `3f0d3e7` (`held_out_generation_authorized: false`; the same record also carries `final_training_authorized: false`, which is historical and not evidence about the 2026-08-05 external authorization); `training_config.json`; `transformation_config.json`; `SMOKE_EVIDENCE.md`; owner suite review 3457 passed / 3 skipped / 36 unrelated pre-existing OpenCode failures |
| Real minimum-tier corpus 1,000/150, zero leakage, zero repository overlap; one-step CUDA update + adapter reload succeeded | FirstMate-reviewed external experimental evidence not yet merged or durably tracked on main (labeled Layer 2 in the status map, items 9/12); status map item 9 and Section 5 QLoRA boundary |
| Owner-delegated independent FirstMate AI audit of the 75 frozen corpus rows complete: 39 ACCEPT / 36 REJECT, reviewer identity disclosed; corpus not modified | Owner-supplied result; FirstMate-reviewed external experimental evidence not yet merged or durably tracked on main. This is an AI audit, not human review. Final QLoRA training was externally authorized by FirstMate on 2026-08-05; no accepted final-training artifact exists yet and results are pending FirstMate artifact review; fail-closed audit validation and the corpus acceptance decision remain pending (Section 8) |
| RAG NO-GO-FOR-NOW, SFT DEFER, DPO NO-GO-FOR-NOW | `docs/DATASET_EVALUATION_DECISION_V1.md` Section 10; `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md` |
| Friday delivery bundle identity and inventory | `docs/FRIDAY_DELIVERY_MANIFEST_V1.md` (bundle inventory with SHA-256, evidence index, exact commands, fallbacks, rehearsal evidence) |
| Final preflight and rehearsal gate | `docs/FRIDAY_PREFLIGHT_CHECKLIST_V1.md` (consolidated checklist; rehearsal evidence from the 2026-08-05 single-task run) |
| Project-status handoff and post-Friday batches | `docs/FRIDAY_STATUS_HANDOFF_V1.md` (completed/partial/pending/blocked; batches B1–B7) |

## 5. Friday demo runbook

### 5.1 Primary demo: deterministic offline demo (recommended)

Commands below are exactly the established entry points documented in `docs/DEMO_GUIDE_V1.md` Section 2 and `agentic_debugger/demo/cli.py`. Nothing here runs a model, WSL, or network.

Prerequisites (complete environment setup before presentation day; `pip install`
may require package-index access or already-cached dependencies — the demo
itself has zero provider/network dependency once the environment is prepared):

```powershell
python -m pip install -e .[test]
```

Full run (10 cases, ~seconds) — always into a fresh unique output directory
(never reuse, delete, or overwrite a prior output):

```powershell
$fullOut = "demo-out-full-" + (Get-Date -Format "yyyyMMdd-HHmmss")
python -m agentic_debugger.demo --output-dir $fullOut
```

Expected success criteria (documented in `docs/DEMO_TASK9.md` / `DEMO_GUIDE_V1.md`):

- exit code 0;
- `$fullOut/results.json` and `$fullOut/technical-evaluation-summary.md` report 5 curated tasks × 2 policies = 10 cases;
- all controller `Done`; all verifier `COMPLETED`/`RESOLVED`; F2P 10/10; P2P 22/22; localization `CORRECT_TARGET_SYMBOL` 10/10;
- canonical fixtures unchanged; every workspace cleaned;
- per-case trajectories under `$fullOut/trajectories/<case>.events.jsonl` and `.semantic.json`.

Narration order inside the demo (each step maps to a visible artifact):

1. Task/repository input — `--list-tasks` shows the 5 curated task IDs; run one case first for narration: `python -m agentic_debugger.demo --output-dir $demoOut --task-id curated-off-by-one-002` (fresh `$demoOut` per run, as in Section 5.3 of `docs/FRIDAY_PREFLIGHT_CHECKLIST_V1.md`).
2. Controller state transitions — open `<case>.events.jsonl`: reproduce → understand → (PDB gate) → patch → validate.
3. Typed tools — point to the same file: file-read, code-search, test-run, patch-apply directives with validated arguments.
4. PDB-capable path — narrate the `pdb-on-uncertainty` cases and their scripted evidence boundary (offline stand-in, 21 scripted PDB observations; see `docs/DEMO_TASK9.md`). The golden trajectory `tests/golden_trajectories/data/pdb-gated-successful-repair.json` can be shown as the recorded PDB-gated repair path.
5. Patch application in a disposable workspace — unified diff applied to a workspace copy; canonical fixture never modified (`runtime/patcher.py`).
6. Independent verifier — `results.json`: baseline reproduction, syntax check, F2P/P2P/full-suite outcomes.
7. Structured result and metrics — `technical-evaluation-summary.md`.
8. Workspace cleanup — the summary reports every workspace cleaned.

Optional stricter check (only when a clean 10/10 is certain) — fresh unique
output directory:

```powershell
$strictOut = "demo-out-strict-" + (Get-Date -Format "yyyyMMdd-HHmmss")
python -m agentic_debugger.demo --output-dir $strictOut --strict
```

### 5.2 Demo boundary statements

- The demo uses an offline scripted model stand-in — it demonstrates the pipeline, not model debugging performance.
- The PDB-enabled policy attaches to a driver script with a pre-known breakpoint, not the failing test itself (`docs/DEMO_TASK9.md` Section 8).
- Do not weaken verifier gates, patch validation, containment, or budgets to obtain a pass. A failing demo is a regression signal to investigate, not something to force.

### 5.3 Secondary experimental evidence (recorded, not live)

Show the QuixBugs v4 result as a recorded experiment (status map items 18/26 and Section 5 boundary; contract in `research/quixbugs/PAIRED_PILOT_V4.json`). The recorded case identities are bound to the preserved campaign record and private transport for attempt `3b5d7488...`. Precise statements:

- Real OpenCode Go / DeepSeek V4 Flash interaction occurred (subscription route, protocol 1.3, variant `max`, no fallback);
- Case 1 (`find_in_sorted`, PDB policy, order 1): the model correctly diagnosed the semantic defect in `find_in_sorted`;
- it proposed the correct one-line semantic change as a unified diff;
- the Case 1 patch was rejected because of an invalid unified-diff hunk header (strict hunk-header validation: hunk declared `old_count=7` with a 6-line body);
- Case 2 (`find_in_sorted`, static-baseline policy, order 2): a patch was applied and Validate was visited, but the campaign exhausted its public-evidence budget (38,534 observed bytes) before verifier execution and the run was interrupted;
- zero live-model repairs reached verifier-confirmed RESOLVED;
- zero live PDB observations occurred;
- no valid static-versus-PDB paired comparison was completed;
- the campaign terminated honestly as `ABORTED` / `BUDGET_EXCEEDED` with cases 3–6 unstarted; provider-reported costs preserved (Case 1 $0.007378, Case 2 $0.012323).
- The campaign infrastructure that persists and verifies these terminals is accepted on `main` through `0abb588` (terminal and budget-exhaustion provenance, fail-closed); the sanitized fixture's case-identity mapping was corrected and accepted at `fc7c85b`.

The local preserved review package (`_ai-review/quixbugs-v4-live-campaign/`) is an optional artifact to keep available during the presentation; it is not required for any durable claim.

## 6. Claims that may be made

All claims below are supported by tracked evidence (Section 4) or by explicitly labeled FirstMate-reviewed external evidence (QLoRA items).

- The deterministic agentic-debugging demo works end-to-end: 10/10 verifier `RESOLVED`, F2P 10/10, P2P 22/22, localization `CORRECT_TARGET_SYMBOL` 10/10, workspaces cleaned — with the scripted stand-in boundary stated.
- Typed file-read, code-search, test-run, patch-apply tools exist and are exercised.
- A real PDB session path exists (breakpoints, stack/frame/locals inspection, safe evaluation, stepping) and is demonstrated by scripted trajectories.
- An independent verifier exists and is the correctness authority for every lifecycle.
- Real OpenCode Go / DeepSeek V4 Flash provider interaction occurred.
- The live model correctly diagnosed the semantic defect in `find_in_sorted` and proposed the correct one-line semantic change; the patch was rejected by strict hunk-header validation.
- The QuixBugs adapter/sandbox/verifier infrastructure was validated on one then eight real licensed tasks with literal gold diffs (infrastructure only).
- BugsInPy is the primary dataset by research merit but execution remains license-blocked.
- Model selection is frozen: Qwen/Qwen2.5-Coder-7B-Instruct with exact revision.
- QLoRA methodology is frozen; a real leakage-checked minimum-tier corpus (1,000 train / 150 validation) is materialized; a one-step real CUDA QLoRA update and adapter save/reload succeeded (FirstMate-reviewed external evidence).
- Costs and counters are provider-reported and preserved as observed.

## 7. Claims that must not be made

- Any verifier-confirmed live QuixBugs repair ("model resolved case X").
- That PDB improves repair performance (no valid static-versus-PDB comparison exists; zero live PDB observations).
- That the gold-patch baselines are model performance (every candidate is the literal upstream diff).
- Invented final QLoRA or held-out metrics (loss, RESOLVED rates, base-vs-tuned deltas) while the results are pending.
- That BugsInPy evaluation was executed (license-blocked; preflight-only).
- That RAG or DPO/RLHF is implemented (recorded NO-GO-FOR-NOW decisions).
- That the project is a finished general-purpose automated repair system.
- That infrastructure failures or budget-contract limits (attempts `705aa047…`, `81f2e5d8…`, `4c7fc444…`, `fddf1e39…`) are model-performance results.
- Any claim resting on ignored `_ai-review/` or `operator/` files as the durable basis.
- Any subscription/entitlement/pricing inference not explicitly reported by the provider.

## 8. QLoRA result placeholders and replacement instructions

### A. CURRENT VERIFIED STATE

- Qwen/Qwen2.5-Coder-7B-Instruct selected and frozen.
- Exact model revision: `c03e6d358207e414f1eca0bb1891e29f1db0e242` (Apache-2.0) — `experiments/qlora_patch_pilot_v1/freeze_record.json` on branch `experiment/qlora-patch-pilot-v1` commit `3f0d3e7` (branch head; implementation accepted at FirstMate implementation review on 2026-08-05).
- CommitPackFT Python corpus methodology frozen (config `python`, revision `fc56fe33c030c6daa414c2b112c932b8eed085e6`, source-license allowlist) — same freeze record + `transformation_config.json`.
- Real minimum-tier corpus materialized: 1,000 train / 150 validation (from 56,025 input candidates).
- Zero held-out exact/near leakage; zero repository overlap (deterministic dedup + SimHash near-dedup + held-out checks, `transformation_config.json`).
- One-step real CUDA QLoRA update succeeded (LoRA r=16, alpha=32, 4-bit nf4 double quantization, completion-only loss — `training_config.json`).
- Adapter save and reload succeeded.
- **COMPLETED EXTERNAL AUDIT** — the owner-delegated independent FirstMate AI audit; not human review:
  - total frozen rows: 75;
  - accepted-packet rows: 50; rejected-packet rows: 25;
  - independent audit ACCEPT: 39; independent audit REJECT: 36;
  - 11 accepted-packet rows rejected as false positives;
  - 25/25 rejected-packet decisions upheld;
  - sample selection and frozen order unchanged; corpus not modified;
  - `human_*` fields intentionally remain blank.
  - Reviewer: FirstMate / GPT-5.6 Thinking; reviewer type `independent_ai_reviewer`; independent from the DeepSeek coding agent and from the Qwen training model.
  - Research-integrity wording: never call this a human audit; never call it human-reviewed, human-validated, or human sign-off; it is FirstMate-reviewed external evidence not yet merged or durably tracked on main.

The CUDA, corpus, and audit items are labeled: **FirstMate-reviewed external experimental evidence not yet merged or durably tracked on main** (status map Layer 2; supports IN PROGRESS claims only). The QLoRA implementation (including the tracked `independent_ai` audit contract integration and complete run-provenance enforcement) is accepted at commit `3f0d3e7` on the unmerged branch `experiment/qlora-patch-pilot-v1` after FirstMate implementation review; the owner full-suite validation was reviewed at 3457 passed, 3 skipped, 36 unrelated pre-existing OpenCode transport/wrapper failures, with no QLoRA-focused failure.

**STILL PENDING:**
- final QLoRA training results (final training was externally authorized by FirstMate on 2026-08-05; no accepted final-training artifact exists yet and the results are pending FirstMate artifact review — no training value may be predicted);
- fail-closed validation of the completed audit;
- exact interpretation of the 39/50 accepted-packet pass result;
- corpus-quality gate decision;
- final corpus acceptance;
- held-out base-versus-tuned generation and evaluation (still unauthorized; `held_out_generation_authorized` remains `false` at branch head `3f0d3e7`).

These pending items do not change the observed audit result above, and the observed audit result does not constitute corpus acceptance.

### B. PENDING FINAL RESULT

Every field below defaults to `PENDING — DO NOT INFER` and must stay that way until the accepted QLoRA checkpoint is reviewed. Do not fill any field from prediction, analogy, or earlier smoke evidence.

| Field | Status | Replacement source when available |
|---|---|---|
| Independent audit validation and corpus acceptance decision | `PENDING — DO NOT INFER` | Validated `independent_ai` audit record; audit contract validation result; exact corpus-quality acceptance decision |
| Final training runtime and hardware | `PENDING — DO NOT INFER` | Colab notebook training log; training record in the external artifact manifest |
| Training loss summary | `PENDING — DO NOT INFER` | TRL/SFT trainer log from the final run |
| Adapter identity, size and SHA-256 | `PENDING — DO NOT INFER` | `external_artifacts.json` adapter entries + saved adapter reload record |
| Five-task base-model generation results | `PENDING — DO NOT INFER` | Frozen held-out generation records (base condition, same prompt/generation contract) |
| Five-task tuned-model generation results | `PENDING — DO NOT INFER` | Frozen held-out generation records (tuned condition) |
| Strict-parser acceptance counts | `PENDING — DO NOT INFER` | Strict unified-diff parser counts over generated patches |
| Verifier RESOLVED counts | `PENDING — DO NOT INFER` | `EvaluationVerifier` results on generated patches (F2P/P2P/full suite) |
| F2P and P2P results | `PENDING — DO NOT INFER` | Verifier records per held-out task |
| Base-versus-tuned interpretation | `PENDING — DO NOT INFER` | Comparison analysis over the same five tasks and contracts |
| FirstMate approval and experiment-branch merge status | `PENDING — DO NOT INFER` | Acceptance record; merge status of `experiment/qlora-patch-pilot-v1` |

Replacement instructions: when the QLoRA checkpoint is accepted, replace each `PENDING — DO NOT INFER` cell with the exact observed value and the artifact path/SHA-256 where required, keeping the frozen contract identities (freeze record, prompt contract, generation config) cited in the same row. No field may be filled before FirstMate review of the final training and held-out records. Note: the tracked freeze record at `3f0d3e7` still carries `final_training_authorized: false` and `held_out_generation_authorized: false`; that historical branch-bound record is not evidence about the external authorization of final training granted on 2026-08-05, and it is not evidence that held-out generation is currently authorized (it is not).

Prescribed statement if final training or held-out comparison is incomplete at presentation time:

> "The owner-delegated independent FirstMate AI audit of the frozen corpus is complete externally (39 ACCEPT / 36 REJECT with a disclosed AI reviewer identity), and the QLoRA implementation passed FirstMate implementation review. Final training was externally authorized by FirstMate on 2026-08-05; no accepted final-training artifact exists yet and its results are pending FirstMate artifact review. Contract validation, the corpus acceptance decision, and the frozen base-versus-tuned comparison are still pending, and held-out generation remains unauthorized. The current evidence demonstrates a frozen methodology, a leakage-checked real corpus, and a technically successful CUDA QLoRA update and adapter reload."

## 9. Failure and contingency plan

Never weaken verifier gates, patch validation, containment, dataset leakage checks, frozen prompt/generation contracts, or campaign budgets to make a presentation appear successful.

| Contingency | Concrete fallback procedure |
|---|---|
| Colab unavailable | Present Section 8A as readiness evidence only: frozen methodology, frozen configs, leakage-checked real corpus, one-step CUDA update and adapter reload (labeled external evidence). State the prescribed pending-results sentence; the post-Friday near term is final-training artifact review and corpus acceptance (instructor items 12/13). Do not substitute prompt changes or smoke runs for a training result. |
| Final QLoRA training incomplete | Same as Colab-unavailable branch: all Section 8B fields remain `PENDING — DO NOT INFER`; the segment becomes "methodology and readiness, results pending". |
| Deterministic demo command fails | Re-check `python -m pip install -e .[test]`; then retry the single-task form `python -m agentic_debugger.demo --output-dir demo-out --task-id curated-off-by-one-002`. If `--strict` fails, it is a regression signal per `docs/DEMO_GUIDE_V1.md` Section 6 — do not force it. Last resort: present preserved demo outputs (`results.json`, `technical-evaluation-summary.md`, trajectories) as recorded evidence instead of a live run. |
| Internet unavailable | Safe only when the environment is already prepared (install and import checks done before presentation day). The primary demo itself needs no network and no WSL. Do not attempt any provider interaction. The recorded v4 evidence is local; if its files are not available, rely on the tracked facts in `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` and `research/quixbugs/PAIRED_PILOT_V4.json` (Section 6). |
| Recorded provider evidence unavailable | Present the tracked account of the v4 attempt (status map Section 5 boundary; v4 manifest) without quoting review-package files. No durable claim depends on `_ai-review/` or `operator/`. |
| Presentation time shortened to 10–12 minutes | Use the core arc: scope snapshot (1 min) → architecture (2 min) → deterministic evidence + live findings (3 min) → limitations (1 min) → QLoRA verified state/pending (1 min) → single-task demo (2 min) → roadmap (1 min). Drop the full 10-case demo to the `--task-id curated-off-by-one-002` single case. |

## 10. Expected professor questions and evidence-grounded answers

1. **Why Python and PDB only?** The project is a Python/PDB-first prototype by accepted scope (README; AGENTS.md). Python was chosen for the curated fixtures, the verifier stack, and dataset fit (BugsInPy/QuixBugs are Python); PDB is the first debugger adapter implemented (`runtime/pdb_session.py`, tasks 4A–4D).
2. **Why not GDB/LLDB yet?** The instructor item names "PDB, GDB veya LLDB"; the implemented adapter is PDB only, so the item is honestly PARTIAL (status map item 22). GDB/LLDB are long-term work, partly because the research track is Python-first.
3. **Why QuixBugs instead of BugsInPy?** BugsInPy is the primary choice by research merit (real project bugs) but execution remains license-blocked (no cleared dataset license — `docs/BUGSINPY_LICENSE_GATE_V1.md`). QuixBugs is the licensed (MIT, creator consent), infra-validated fallback (`docs/DATASET_EVALUATION_DECISION_V1.md`; eight-task baseline).
4. **Why Qwen2.5-Coder-7B?** Open-source (Apache-2.0), instruct-tuned code model of feasible size for a QLoRA Colab pilot; selected and frozen with an exact revision in the experiment freeze record (status map item 10; `freeze_record.json`).
5. **Why CommitPackFT?** Large, permissive-license (MIT card), Python-configurable commit dataset; the transformation converts buggy-source + task text + failure output into unified-diff completion instruction-response rows, with a strict source-license allowlist and deterministic train/validation split (status map item 9/11; `transformation_config.json`, `prompt_contract.json`).
6. **What exactly does the deterministic demo prove?** It proves the platform: task loading, controller state machine, typed tools, disposable workspace, patch apply, independent verifier (10/10 RESOLVED, F2P 10/10, P2P 22/22), cleanup, and replay-verified trajectories — with an offline scripted stand-in, so it proves nothing about model quality (`docs/DEMO_TASK9.md`; demo guide Section 5.2 here).
7. **What did the live DeepSeek experiment prove?** It proved the real route works end-to-end (OpenCode Go subscription, protocol 1.3) and gave one strong signal: the model correctly diagnosed `find_in_sorted` and proposed the correct one-line fix, rejected by strict hunk-header validation. It did not prove repair capability: zero verifier-confirmed RESOLVED, zero PDB observations (status map items 18/24/26).
8. **Why were there no verifier-confirmed live repairs?** Budget-contract and protocol realities: the case-1 patch failed strict hunk-header validation; case 2 applied a correct patch but exhausted the frozen 20,000-byte public-evidence budget before verifier execution; the campaign then aborted per the pre-registered stop contract (cases 3–6 unstarted). These are honest terminals, not hidden model failures (`PAIRED_PILOT_V4.json` terminal rules; status map Section 5).
9. **What is the expected contribution of fine-tuning?** The hypothesis is that a QLoRA-tuned patch-completion model produces verifier-accepted patches more reliably on the held-out tasks than the base model. It is a hypothesis; the base-versus-tuned comparison is not yet run (status map items 12/13).
10. **How will base and tuned models be compared fairly?** Identical frozen prompt contract, identical generation config (one candidate, no regeneration), the same five held-out curated tasks, and the same independent verifier for all generated patches (Section 8B; `prompt_contract.json`, `generation_config.json`, freeze record).
11. **Why no RAG or DPO yet?** Recorded decisions: RAG NO-GO-FOR-NOW (no non-RAG real-model baseline to compare against; deterministic file/search tools exist), DPO NO-GO-FOR-NOW (no SFT baseline and no paired preference dataset) — `docs/DATASET_EVALUATION_DECISION_V1.md` Section 10, `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md`.
12. **Which instructor TODO items are still incomplete?** 10 PARTIAL and 7 NOT STARTED of 27 (status map line 106). Material open items: literature-review completion (1–4), corpus audit and acceptance (9, 11), final QLoRA training (12), base-versus-tuned comparison (13), live verifier-confirmed repair and metrics (18, 26), fine-tuned-model debugger interaction (23), and long-term RAG/DPO/GDB-LLDB items (14–15, 19–21, 22).

## 11. Final pre-presentation checklist

The consolidated final gate is `docs/FRIDAY_PREFLIGHT_CHECKLIST_V1.md`; this
section remains the plan-scoped subset.

- [ ] Repository baseline noted: accepted source baseline `456f0e9a6576aab912f5af5980d756ff4e1e9dc3` (accepted presentation plan/deck/cue delivery commit); campaign infrastructure accepted through `0abb588df46605a9a754c051e71ebe17692c09db`; V4 identity correction accepted through `fc7c85b9858eba993f6bacc8ea9b4f805873f1a5`; on presentation day, run from clean `main` matching `origin/main`, containing the delivery bundle files and descending from `456f0e9`.
- [ ] Git check: current branch is `main`; local `main` matches `origin/main`; tracked working tree is clean. No requirement that ignored `.opencode/` or `_ai-review/` files be absent.
- [ ] Python environment ready: `python -m pip install -e .[test]` succeeds; `python --version` is 3.11+.
- [ ] Deterministic demo verified once beforehand: `python -m agentic_debugger.demo --output-dir demo-out --task-id curated-off-by-one-002` (single-case rehearsal) and, if desired, the full `--output-dir demo-out` run; expected counters recorded (`docs/DEMO_GUIDE_V1.md` Section 2).
- [ ] Preserved demo outputs available offline: `results.json`, `technical-evaluation-summary.md`, per-case `trajectories/*.events.jsonl` and `.semantic.json`.
- [ ] QLoRA result fields checked: every Section 8B field is `PENDING — DO NOT INFER` unless the accepted checkpoint record exists; the prescribed pending-results sentence is ready.
- [ ] Audit wording checked: the doc states "Owner-delegated independent FirstMate AI audit; not human review" with 75 rows / 39 ACCEPT / 36 REJECT as external evidence; no "human audit / manual audit / human sign-off" phrasing describes the audit; corpus acceptance is shown as pending.
- [ ] Local evidence backups: tracked documents and manifests reachable; optional local review package kept available but not required for any claim.
- [ ] Offline copies of presentation evidence exist (no dependency on internet for demo or evidence).
- [ ] Timing rehearsal done: segments 1–7 ≤ ~20 min; demo within segment 8; 10–12-minute shortened version rehearsed (Section 9).
- [ ] Prohibited-claim review: scan Section 7 list; confirm no slide/sentence claims live repair, PDB benefit, gold-patch model performance, final metrics, BugsInPy execution, or RAG/DPO implementation.
- [ ] Fresh single-task demo rehearsal executed once with a timestamped output directory (checklist Section 3); rehearsal artifacts preserved as local operational fallback only.

## 12. Post-Friday roadmap tied to the instructor's 27-item list

### Post-Friday near term

| Step | Instructor items | Evidence/notes |
|---|---|---|
| Validate the `independent_ai` audit contract and issue the corpus acceptance decision | 9, 11 | Owner-delegated independent FirstMate AI audit already complete externally (75 rows; 39 ACCEPT / 36 REJECT; AI audit, not human review); tracked `independent_ai` contract/provenance implementation accepted at `3f0d3e7`; fail-closed validation and the corpus-quality/corpus-acceptance decisions pending |
| Final QLoRA training on the frozen corpus/config | 12 | Externally authorized by FirstMate on 2026-08-05 (the tracked freeze record's historical `final_training_authorized: false` is not evidence about this authorization); final-training results pending FirstMate artifact review; saved adapter + training record + external artifact manifest |
| Frozen held-out base-versus-tuned comparison with the independent verifier | 13 | Same prompt/generation contracts; five held-out curated tasks; verifier on all generated patches; held-out generation remains unauthorized until a separate authorization exists |
| Fine-tuned model debugger-command generation and interpretation | 23 | Depends on item 12; uses the typed PDB directive contracts |
| Authorized six-case QuixBugs campaign with verifier-authoritative results | 26, 18 | Must use `research/quixbugs/PAIRED_PILOT_V4.json` explicitly, fresh authorization artifacts, real route preflight |
| BugsInPy execution if the license gate clears | 6, 7, 8, 26 | License review + containment upgrade before any execution |

### Long term

| Step | Instructor items |
|---|---|
| Consolidated reviewed literature survey and comparison deliverables | 1, 2, 3, 4 |
| RAG system over repository code/tests/issues/errors and integration with the fine-tuned model | 14, 15 |
| Preference dataset from successful/failed debugging outputs | 19 |
| DPO or appropriate RLHF with an SFT baseline and evaluation | 20, 21 |
| GDB and/or LLDB adapter | 22 |
| Full four-way comparison (base / tuned / RAG / agentic) under one contract | 21 |
| Evaluation across all five metric families on a completed campaign | 26 |

The roadmap does not schedule a wider no-model QuixBugs campaign (the decision gate records the fallback dataset's job as done) and does not authorize any provider, training, or campaign execution by itself.

The grouped post-Friday engineering batches (B1–B7) live in
`docs/FRIDAY_STATUS_HANDOFF_V1.md` Section 5; this section and that table are
the two coordinated views of the same roadmap.
