# Agentic Debugging Internship — Final Technical Report

**Date:** 2026-08-13 (v2 historical snapshot 2026-08-11; this revision
reconciles the current R1-R6 state through 2026-08-13)
**Branch:** `docs/r1-r6-current-closeout-v1`
**Baseline HEAD:** `34cce329b5e6e7cf42531d8e609774c7608b67cb` (docs structure
baseline; the R1-R6 milestones and their commits are listed in §23)
**Author's role:** internship project — single-controller-agent architecture,
Python/PDB-first prototype

This report is the current technical report. It preserves the accepted
historical engineering and S8/S9 narrative (sections 1–21 are the
2026-08-11 report content, qualified where they were true only at that
snapshot) and adds the post-S9 R1-R6 phase (sections 22–23). The 2026-08-11
S8/S9 snapshot is archived verbatim at
`docs/archive/reports/final-report-2026-08-11.md`; the 2026-07-31 v1
snapshot remains at `docs/archive/reports/final-report-v1.md`.

Every material experimental claim traces to a frozen run artifact, a tracked
contract, or a committed canonical carrier; external literature claims cite
the S7 closeout with evidence tiers marked. Provenance tiers are defined and
applied in §21–22; local/untracked sources are never silently promoted to
clean-checkout reproducibility.

**Current status (2026-08-13):** the R1-R6 phase superseded the earlier
bounded-negative overall conclusion. A real model now participates in a
multi-turn runtime-debugging loop under the repaired interface (R1-R2),
debugger-informed repair reaches the independent verifier (R3), a
model-generated regression test is verified (R4), the BASE-14B clean holdout
is 5/5 (R5.9), and the project-fine-tuned 7B debugger is 8/8 on the frozen
task-disjoint QuixBugs validation (R6). The stronger R6 final five-task
curated holdout is **INCOMPLETE_HARDWARE_STOP** (hardware power-off; not
claimed complete, not counted as a score). The professor-facing structured
JSON trace deliverable is complete. See §22 for the full R1-R6 record and
§21 for the historical S8/S9 closeout preserved as of 2026-08-11.

---

## 1. Problem and research goals

The project investigates the path from traditional debugging, fault
localization, and automated program repair, through LLM-based debugging,
toward **interactive debugger-assisted agents**: systems that do not merely
propose a patch from static context, but can pause a failing program, inspect
its live state through a real debugger (Python's `pdb`), and use that runtime
evidence to localize and repair the defect.

The overarching goal (master plan §1) is to build and evaluate a system in
which a real code-capable LLM can: reproduce a failure, localize the relevant
code, form a root-cause hypothesis, use debugging tools when useful, inspect
breakpoint/stack/locals/execution state, update its diagnosis from runtime
evidence, produce a patch, run independent verification, and report F2P/P2P/
RESOLVED status with reproducible evidence.

The final system must separate model capability, retrieval, tool/interface
behavior, controller behavior, debugger backend, patch serialization, and
executable verification.

The original research objective sought successful real-model participation in
a dynamic debugger loop: a real model obtaining a successful non-error PDB
observation, interpreting it, and producing a debugger-informed patch that
reaches the independent verifier. **At the 2026-08-11 S8/S9 snapshot the
project had not achieved that positive behavior** — neither the RAW nor the
cp118 model condition produced a successful non-error debugger observation or
a debugger-informed patch (Section 13). That bounded-negative conclusion is
**historical**: the later R1-R6 phase (sections 22–23), executed under the
repaired debugger interface, achieved exactly this chain (R1 breakpoint/stack
→ R2 multi-turn loop → R3 debugger-informed repair reaching the independent
verifier → R4 model-generated regression test, and beyond to the R5/R6
holdouts).

The accepted S8/S9 completion criterion permitted scientifically honest
closeout with the bounded, well-instrumented negative result, provided
professor deliverables are reproducible and all claims are evidence-backed.
The project therefore closed the S8/S9 stage not on the positive trajectory —
which had not occurred by then — but on an honest bounded-negative debugger
result, a verifier-backed evaluation platform, a definitive RAW-vs-cp118
transfer comparison, and a literature-aligned architecture defense. The
central contribution was then infrastructure, evaluation methodology, and
the bounded-negative debugger result (Section 20); the current contribution
extends to positive real-model dynamic debugging and project-fine-tuned
validation (Section 22).

---

## 2. Research questions

1. **Does giving an LLM-based debugging agent controlled, budgeted access to
   a real debugger change its ability to localize and repair bugs, compared
   to a static/test-feedback-only baseline?** And can that comparison be made
   honestly with an independent verifier?

2. **Does localized-repair fine-tuning (QLoRA SFT) transfer to executable
   repair on a held-out evaluation cohort, and does it transfer to interactive
   debugger use?**

3. **Can repository RAG augment a fine-tuned model's repair performance on
   held-out tasks?**

4. **Is preference optimization (DPO) justified given the available data?**

5. **What does the current literature say about tool-using, runtime-aware, and
   multi-agent debugging systems, and does it support this project's
   single-agent + deterministic-controller design?**

Questions 1–4 are answered by the project's accepted experimental evidence
(Sections 6–13, 22). Question 5 is answered by the S7 literature closeout
(Sections 3, 17, 19).

At the S8/S9 snapshot, the accepted answer to Q1 was a bounded negative for
the then-tested interface (Section 13). The R1-R6 phase (Section 22) answers
Q1 positively under the repaired debugger interface, and answers Q2's
debugger side with the debugger-oriented R6 fine-tuning campaign — while
keeping the historical cp118 localized-repair negative transfer (Section 8)
and the "no matched-base causal fine-tuning claim" boundary explicit.

---

## 3. Literature context

The S7 focused literature closeout
(`research/literature/agentic_debugging_literature_closeout_2026-08-11.md`)
reviewed 20 works across debugger-aware systems, tool-using SWE agents,
dynamic/runtime evidence, multi-agent debugging, and tool-use/trajectory
post-training. Its executive conclusion is preserved here.

### Key findings

**Runtime evidence can improve debugging and repair.** ChatDBG (FSE 2025,
peer-reviewed) demonstrated that an LLM can actively query conventional
debuggers. LDB (Findings ACL 2024, peer-reviewed) and NExT (ICML 2024,
peer-reviewed) showed gains from concrete execution-state information.
InspectCoder (PACMPL/OOPSLA1 2026, peer-reviewed) and ADI/FramePilot (FSE
2026, peer-reviewed) provide stronger recent evidence that runtime inspection
can improve repair.

**Raw debugger access is not equivalent to useful debugger capability.** The
ADI ablation is unusually clear: simply adding conventional PDB moved Claude
Sonnet 3.7 from 55.0% to only 55.8% on SWE-bench Verified, hurt GPT-4o from
32.6% to 31.2%, and hurt Qwen3 from 29.2% to 28.4%. The agent-oriented
FramePilot interface instead reached 63.8%, 36.2%, and 31.4%, respectively.
InspectCoder reports that direct stateful debugger interaction caused invalid
commands, corrupted sessions, and error loops; its middleware explicitly
tracks legal state transitions. Debug2Fix (2026, preprint) found that
directly exposing debugger tools was nearly neutral or harmful, whereas a
more strongly scaffolded interface helped.

**Ordinary code-repair fine-tuning should not be expected to produce debugger
competence automatically.** SWE-Gym (ICML 2025, peer-reviewed) shows that
actual agent trajectories can substantially train tool-using SWE behavior.
Open-SWE-Traces (2026, preprint) shows measurable cross-harness degradation
because agents overfit interaction patterns, action spaces, and observation
formats. There is no good empirical basis for assuming that a QLoRA checkpoint
trained primarily for localized repair will spontaneously learn PDB session
semantics.

**Multi-agent systems are now empirically credible, but are not a universal
architectural requirement.** BOAD (ICLR 2026, peer-reviewed) is the strongest
counterargument: under essentially unchanged total-token use on SWE-bench
Verified, its learned hierarchy improved Seed-OSS-36B from 49.8% to 53.2%.
Yet its manually designed sub-agent version scored only 47.4%, and adding
more sub-agents was non-monotonic. Other multi-agent results (AgentForge,
DeLM) frequently confound role decomposition with extra inference, better
tools, or greater token expenditure.

### Evidence tiers

The S7 evidence table distinguishes peer-reviewed publications (FSE, ICML,
ICLR, NeurIPS, ASE, TOSEM, PACMPL/OOPSLA), preprints, and technical reports.
Preprints (debug-gym, Debug2Fix, DebugHarness, Open-SWE-Traces, DeLM,
AgentForge, SWE-Master, SWE-TRACE) are marked as such throughout this report
and are not treated at the same evidentiary level as peer-reviewed work.

The full evidence table and 20 citations are in the S7 closeout document
(§§11–12).

---

## 4. Model landscape and model choice

### Screening pilot

A frozen five-model RAW comparison was run on the 40-task QuixBugs evaluation
cohort (Protocol v1.2.1, Track B semantic extraction). The accepted corrected
result (master plan §2.2):

| Model | Apply | RESOLVED |
|---|---:|---:|
| Qwen2.5-Coder-7B-Instruct | 20/40 | 5/40 |
| Seed-Coder-8B-Instruct | 11/40 | 2/40 |
| Granite-4.1-8B | 8/40 | 1/40 |
| Ministral-3-8B-Instruct | 4/40 | 1/40 |
| Qwen3.5-9B | 9/40 | 5/40 |

### Selection rationale

**Qwen2.5-Coder-7B-Instruct was selected as the primary base model because of
the overall extraction/apply/truncation/compute trade-off, not because it
uniquely had the best RESOLVED result.** Qwen3.5-9B matched the RESOLVED count
(5/40) but had a lower apply rate (9/40 vs 20/40), zero strict extraction, and
higher compute cost. The 40-task cohort is a controlled screening/evaluation
cohort, not a universal model leaderboard.

The frozen model revision is
`Qwen/Qwen2.5-Coder-7B-Instruct` @
`c03e6d358207e414f1eca0bb1891e29f1db0e242`.

---

## 5. Dataset selection — training vs evaluation

A central distinction must be maintained across the project's history:

- **SWE-rebench V2** is the training/post-training dataset for the historical
  cp118 localized-repair SFT campaign.
- **QuixBugs (pinned `4257f44b0ff1181dedaedee6a447e133219fcebf`)** is the
  evaluation/experiment cohort for the RAW/cp118/R5/R6 evaluation work, and —
  from 2026-08-12 — the **training source for the R6 debugger-oriented SFT
  campaign** (a frozen QuixBugs-derived 21-train / 8-validation split). The
  earlier global statement "QuixBugs was never used for training" was true
  at the 2026-08-11 S8/S9 snapshot (cp118 was SWE-rebench V2 only) and is no
  longer globally true after R6; the corrected distinction is:
  - historical cp118 localized-repair SFT: **SWE-rebench V2**;
  - R6 debugger-oriented trajectory SFT: **frozen QuixBugs-derived 21-train /
    8-validation split**;
  - the five curated final holdout tasks: **protected from R6
    training/validation** (structurally excluded from the split).
- The five in-repo curated pytest fixtures remain the architecture smoke
  gate and the R5/R6 final-holdout task set; they were never used for
  training.

### 5.1 Training data — SWE-rebench V2 (historical cp118 campaign)

The primary SFT dataset for the historical cp118 campaign is **SWE-rebench
V2**. The accepted filtered corpus
(master plan §2.3; `experiments/swe_rebench_v2_corpus/b14_package_v2/
B14_PACKAGE_MANIFEST.json`):

- **1,594 eligible authentic Python repair tasks** across 347 repositories.
- **Frozen split** (seed `20260808`):
  - 1,000 train tasks / 307 repos;
  - 150 validation tasks / 40 repos;
  - 444 unused;
  - train/validation repository overlap = 0;
  - protected evaluation repository overlap = 0.
- Final no-truncation ≤32K training view: 940 train, 135 validation.
- Dataset revision: `475dd5e8703bb5fb22dd3c60b5d038b019eba1e0`.

### 5.2 Evaluation data — QuixBugs and curated fixtures

The 40-task QuixBugs Python cohort (`experiments/raw-pilot-v1.1/state/
quix40-v1/pilot_manifest_frozen_v1.jsonl`) is the controlled
screening/evaluation cohort used for the RAW baseline, the cp118 definitive
comparison, and the S4 RAG treatment. It is not part of the cp118 training
split.

The five in-repo curated pytest fixtures (`agentic_debugger/datasets/curated/`)
are architecture smoke gates used by the demo, golden trajectories, the
real-model debugger experiments (D1 used `curated-off-by-one-002`), and the
R5/R6 holdouts. They are synthetic and small; they are not external-benchmark
evidence and were never used for training.

BugsInPy was selected as the primary external dataset by research merit but
remains **license-blocked** for execution (Section 18). SWE-bench Lite/
Verified was deferred for harness cost (Docker, ~120 GB storage, 16 GB RAM,
8 CPUs per the official guide). Defects4J is out of scope (Java/JVM, outside
the Python/PDB track).

### 5.3 R6 training data — debugger-oriented trajectory SFT (2026-08-12)

The R6 campaign built a debugger-trajectory SFT dataset from the pinned
QuixBugs revision `4257f44b0ff1181dedaedee6a447e133219fcebf`
(`experiments/r6_debugger_training/`; split manifest, build summary, SFT
manifest and JSONL files tracked). Accepted fixture construction: **29 / 40
usable debugger-training fixtures** (11 tasks failed construction and are
listed in `build_summary.json`). Frozen split: **21 TRAIN / 8 VALIDATION**
(`split_manifest.json`). SFT pairs: **164 train / 61 validation**
(`sft/sft_manifest.json`). Token statistics (measured SFT distribution,
`training_provenance.json`): p50 ≈ 832, p75 ≈ 1073, p90 ≈ 1607, p95 ≈ 1761,
max 2415. The five curated final holdout tasks
(`curated-none-handling-001`, `curated-off-by-one-002`,
`curated-wrong-branch-003`, `curated-mutation-alias-004`,
`curated-caller-callee-005`) are structurally excluded from the split
(`holdout_excluded`) and were protected from R6 training and checkpoint
selection.

Provenance discipline (pinned revision, recorded license, environment
fingerprint, fail-closed preflight) was applied to every external artifact.
No candidate dataset's gold patch or hidden-test metadata was ever exposed to
the agent-visible task; `agent_visible_mapping()` removes the `Oracle` before
any model request is constructed.

---

## 6. RAW baseline protocol and results

### Protocol

The RAW baseline was a frozen, no-adapter, no-RAG generation run over the
40-task QuixBugs cohort using Qwen2.5-Coder-7B-Instruct at the pinned
revision. The protocol is documented in `experiments/raw-pilot-v1.1/docs/
RAW_BASELINE_PROTOCOL_v1_2_1_FROZEN.md`.

Two extraction tracks are recorded as **distinct metrics** and must not be
combined:

- **Track A (strict)** — in-repo CSV (`experiments/raw-pilot-v1.1/results/
  results_final.csv`): strict parse, file localization, patch apply,
  supplied-oracle resolve.
- **Track B (semantic)** — master-plan §2.5 prose, cross-referenced in
  `experiments/cp118_rag_definitive/s4_contract.json`: semantic extraction,
  semantic apply, resolve.

### Results

| Metric | Track A (strict, CSV) | Track B (semantic, master plan) |
|---|---|---|
| Extraction | 33/40 (0.825) | 40/40 |
| Patch apply | 14/40 (0.35) | 20/40 (0.5) |
| RESOLVED | 5/40 (0.125) | 5/40 |
| Target-file localization | 33/40 (FILE only) | NOT_RECORDED |
| Symbol localization | NOT_RECORDED (no column) | NOT_RECORDED |

The RESOLVED count is the same (5/40) under both tracks; the extraction and
apply counts differ because the tracks use different extractors. Both are
preserved to avoid internal contradiction.

The failure-stage distribution (Track A): `patch_apply_failed`=19,
`designated_test_failed`=9, `strict_parse_failed`=7,
`resolved_supplied_oracle`=5.

**Provenance:** `local_untracked_accepted` — per-task CSV + telemetry in
`experiments/raw-pilot-v1.1/` (local, not Git-tracked). The accepted values
are reproducibly carried by the tracked S5 canonical comparison
(`s5_comparison_ledger.json`, `s5_provenance_source_map.md`).

---

## 7. QLoRA / SFT formulation

### Training setup

- **Base model:** `Qwen/Qwen2.5-Coder-7B-Instruct` @ `c03e6d35...`.
- **Method:** QLoRA (parameter-efficient fine-tuning).
- **Dataset:** SWE-rebench V2 (Section 5.1).
- **Definitive surviving checkpoint:** **cp118** (step 118; eval_loss
  0.45070546; adapter tree `65b5ed9a...`, safetensors `59398e32...`).

A note on checkpoint selection: the global best observed validation occurred
near step 105 but was not saved. cp118 is the best surviving saved checkpoint
under the original validation-only rule. No new checkpoint sweep is currently
justified.

### SFT formulation boundary

The accepted SFT formulation is explicit and must be stated precisely:

**Input:**
- problem statement;
- oracle-file-localized exact pre-fix production source.

**Target:**
- `PATCH`;
- exact stored gold repair diff.

This trained a **localized repair mapping** — repair-after-localization SFT.

It did **not** directly supervise:

- repository exploration;
- debugger action selection;
- PDB session/state transitions;
- tool-use trajectories;
- runtime observation interpretation;
- iterative agent control policy.

This boundary is essential for interpreting both the cp118 executable-repair
transfer result (Section 8) and the cp118 debugger behavior (Section 13).
The training objective did not teach end-to-end debugging.

---

## 8. Definitive RAW vs cp118 comparison

This is the project's primary fine-tuning-transfer result: the definitive
comparison of RAW Qwen2.5-Coder-7B-Instruct against the cp118 QLoRA
checkpoint, both RAG-OFF, on the frozen 40-task QuixBugs cohort
(S5 axis 1, conditions A vs B).

| Metric | RAW (Track B semantic) | cp118 (RAG-OFF) |
|---|---|---|
| Semantic extraction | 40/40 | 40/40 |
| Patch apply | 20/40 (0.5) | 0/40 |
| RESOLVED | 5/40 | 0/40 |
| Target-file localization | NOT_RECORDED | 40/40 |
| Multi-diff | NOT_RECORDED | 40/40 |
| Extra-file/scope violation | NOT_RECORDED | 39/40 |
| Truncation | NOT_RECORDED | 19/40 |
| Output behavior | normal | very large output expansion |

### Interpretation

The accepted interpretation (S5, preserved verbatim):

> Strong formulation-specific negative executable-repair transfer dominated by
> output-policy degeneration, over-generation, scope explosion and
> serialization mismatch.

cp118 extracted all 40 tasks and localized the target file in all 40, but
produced 0/40 applicable patches and 0/40 resolved tasks. The dominant failure
mode was output-policy degeneration: very large output expansion, multi-diff
generation (40/40), extra-file scope violations (39/40), and truncation
(19/40 hit the output cap).

This is **not** "fine-tuning is bad" and **not** "cp118 is universally worse
than RAW." cp118 was trained on a localized-repair formulation (Section 7)
that did not supervise the output policy, serialization discipline, or scope
control required for clean executable patch production on this cohort. The
result reflects a formulation-specific transfer failure, not a general claim
about fine-tuning.

### Provenance

- RAW Track A: `local_untracked_accepted` (`experiments/raw-pilot-v1.1/results/`,
  local, not Git-tracked). Reproducibility carrier: tracked S5 ledger +
  provenance map.
- RAW Track B: `master_plan_prose_only` (values in the untracked S5 master plan
  §2.5 and the tracked `docs/archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md`
  §2.5; cross-referenced in tracked `s4_contract.json`). Reproducibility
  carrier: tracked S5 ledger + tracked 2026-08-10 master plan.
- cp118 aggregates: `master_plan_prose_only` / `aggregate_external_per_task`
  (aggregates in the tracked 2026-08-10 master plan §2.5 and S5 ledger;
  per-task raw evidence is a Drive-hosted D7 bundle, not in the repo).
  cp118 adapter identity: `frozen_in_repo`
  (`experiments/cp118_rag_definitive/s4_contract.json`, tracked).

---

## 9. Preference / DPO investigation

A historical controlled DPO experiment exists (S5 axis 2, auxiliary; master
plan §2.6; `docs/evaluation/model-rag-sft-dpo.md`).

| Condition | RESOLVED |
|---|---|
| B1 baseline | 27/30 |
| Matched SFT | 27/30 |
| DPO | 21/30 |

DPO underperformed both the B1 baseline and the matched SFT control. A
subsequent authentic preference closeout found insufficient clean homogeneous
data to justify a new DPO campaign.

**Decision: AUTHENTIC NEW DPO — CLOSED / NOT JUSTIFIED.**

This result is auxiliary and is not collapsed into the primary repair score.
It is `master_plan_prose_only` in provenance — the exact 27/30, 27/30, 21/30
values are not in the tracked `docs/evaluation/model-rag-sft-dpo.md` but are
carried by the tracked S5 ledger and the tracked 2026-08-10 master plan §2.6.
No in-repo frozen result file exists. It is not reopened unless Main FirstMate
explicitly authorizes it based on new evidence.

---

## 10. RAG design and partial S4 result

### RAG architecture

The repository RAG system (master plan §2.7; `docs/architecture/repository-rag.md`)
implements: deterministic bounded lexical repository retrieval;
source/test/safe issue/failure provenance; answer-bearing/oracle exclusions;
bounded context budgets; model-adapter integration; default-OFF behavior.

### S4 — Definitive cp118 + frozen RAG treatment

The frozen cp118 + repository-RAG treatment was source-frozen and launched on
the full 40-task quix40 cohort, but the live generation campaign was
terminated after 10 valid tasks for compute/runtime feasibility (S5 axis 3,
condition C; master plan §S4).

**Frozen identity:**
- source commit: `acfe131a...`;
- contract SHA256: `966c2aab...`;
- run identity SHA256: `072f1d69...`;
- cp118 adapter tree: `65b5ed9a...`.

**Accepted partial result:**
- 10/40 tasks produced valid immutable raw/meta/retrieval pairs;
- the 10 tasks are the **first 10 in frozen manifest order** — not random, not
  representative;
- 5/10 generations reached the frozen 4096-token output cap (descriptive only;
  **do not extrapolate to 40**);
- the campaign was stopped because observed local runtime implied ~30–45
  additional hours, after two machine shutdown interruptions;
- stopping was based on compute/runtime feasibility, not answer quality;
- task 11 was interrupted and discarded atomically; no partial pair retained;
- `S4_GENERATION_COMPLETE.json` was correctly not written.

**Missingness (explicit):**
- Primary frozen C9 evaluation: **NOT_EVALUATED** (the evaluator requires the
  full 40-task completion marker and fails closed otherwise).
- Patch apply: NOT_EVALUATED.
- RESOLVED: NOT_EVALUATED.
- P2P: NOT_RECORDED.

**No RAG success/failure claim is made from the S4 partial condition.** The
canonical comparison closes out the cp118+RAG axis with primary correctness
recorded as NOT_EVALUATED and descriptive 10/10 evidence recorded as
descriptive-only with no extrapolation to 40.

### GPU-memory semantics

Per-task `peak_allocated_gib` (9.46–20.05 GiB) is **torch/CUDA allocator
peak**, not physical/resident VRAM. It overcounts under Windows WDDM; several
values exceed the 12,227 MiB device capacity (RTX 5070 Ti Laptop). This is
recorded as `torch_cuda_peak_allocated` (untrusted descriptive allocator
instrumentation). `physical_resident_vram_usage` = NOT_RECORDED. The 12,227
MiB figure is GPU capacity, not workload usage.

### S4 optimized-rerun recommendation

The S5 remaining-gaps decision
(`analysis/s5_final_controlled_comparison/s5_remaining_gaps_next_action.md`)
records:
- `scientific_necessity`: **NO-GO** — no load-bearing final claim depends on
  knowing the cp118+RAG correctness result.
- `current_execution_authorization`: **NOT_AUTHORIZED** — no model run in S5.

The Efficient SDPA capability (Section 19) would remove the dominant compute
constraint, but it is engineering evidence, not a retroactive change to S4.

---

## 11. Agent architecture

The system (`agentic_debugger/`) is a **single controller agent** — not a
multi-agent system — operating over typed, deterministic tools.

| Layer | Module(s) | Responsibility |
|---|---|---|
| Task contract | `evaluation/task_schema.py` | `DebugTask`: language, fixture path, reproduction command, F2P/P2P vectors, constraints, evaluator-only `Oracle`. `agent_visible_mapping()` strips the oracle before the model sees it. |
| Controller | `agent/controller.py`, `agent/controller_policy.py` | State machine: reproduce → understand → (gate) → optional runtime evidence → patch → validate. Enforces action allowlist, transition graph, budgets. |
| Tool boundary | `agent/tool_registry.py` | Single dispatch surface; argument validation, state allowlists, denied paths. |
| Runtime | `runtime/workspace.py`, `runtime/test_runner.py`, `runtime/command_runner.py`, `runtime/patcher.py` | Disposable per-case workspaces, test execution, unified-diff patch application with syntax validation and path enforcement. |
| Debugger | `runtime/pdb_session.py` | Real `pdb` session over worker subprocess: breakpoints, stack/frame/locals, safe AST-allowlisted expression evaluation, stepping/continue. |
| Events | `events/logger.py`, `events/replay.py` | Immutable `RunEvent` trajectories, replay-verified state reconstruction. |
| Verifier | `evaluation/verifier.py`, `evaluation/outcome_taxonomy.py` | **Authoritative** correctness oracle: baseline reproduction, syntax check, post-patch F2P/P2P/full-suite, canonical-fixture immutability, cleanup — run from an independent clean baseline. |
| Live model path | `evaluation/live.py`, `evaluation/live_cli.py` | Explicitly-authorized, offline-by-default real-model evaluation harness. |
| Golden trajectories | `agent/trajectory.py` + `tests/golden_trajectories/` | Immutable record/replay fixtures for controller drift detection. |
| Demo | `demo/` | Offline, no-model, five-curated-task end-to-end demonstration. |
| Adapters | `bugsinpy/`, `quixbugs/` | Dataset-specific manifest validation, source acquisition, containment. |

The controller state machine enforces the lifecycle: load task → create
disposable workspace → reproduce baseline failure → drive controller loop →
apply candidate patch → syntax-check → run F2P/P2P/full-suite → classify
outcome → project to event trajectory → clean up. This lifecycle is identical
whether the "model" is the offline scripted stand-in, a real live model, or
absent (gold-patch baselines). That reuse is what lets an infra-only run and a
model-in-the-loop run share one verifier and one notion of "passing."

---

## 12. PDB / debugger architecture

The PDB backend (`agentic_debugger/runtime/pdb_*`) supports the full debugger
grammar: model-selectable breakpoint, continue, step, next, stack summary,
frame locals, safe expression evaluation (AST-allowlisted), cleanup, replay,
and bounded post-mortem behavior.

Key design properties:

- The model never touches the raw PDB terminal directly. It communicates
  through typed controller directives that are translated to backend actions.
- Effective actions are the intersection of controller-state allowlist, actual
  tool registry, policy, and PDB lifecycle/budget — with no
  registry-less fallback.
- The PDB session runs over a worker subprocess in a disposable workspace;
  the canonical source is never written to directly.
- Safe expression evaluation uses an AST allowlist to prevent arbitrary code
  execution through the debugger.

### Deterministic engineering evidence

Deterministic/scripted PDB trajectories exercise the full grammar and pass
(`tests/`, `tests/golden_trajectories/`). The golden reachability capture
(`tests/golden_trajectories/data/quixbugs-gcd-pdb-reachability-captured-
result.json`) records `REACHABILITY_CASE_PASSED` with 2 successful PDB
observations. Patch verification was explicitly out of scope for that capture.

This proves the **backend and deterministic controller path** work. It does
not prove the **model** can use the debugger — that is a separate question
(Section 13).

---

## 13. Real-model debugger experiments (historical D1/S2, superseded)

Two real-model debugger experiments were run (S5 axis 4): D1 (RAW) and S2
(cp118), both on the same frozen D1 runtime-entry treatment using
`curated-off-by-one-002`. **These results are the historical 2026-08-10/11
state under the then-current interface. The repaired-interface R1-R6
experiments (Section 22) superseded this negative outcome; this section is
preserved because the earlier failures must not be erased.**

### 13.1 D1 — RAW debugger interaction

**Setup:** Frozen RAW Qwen2.5-Coder-7B-Instruct, RAG OFF, task
`curated-off-by-one-002`. The D1 treatment deterministically performs the
administrative controller phase transitions (REPRODUCE→UNDERSTAND→
RUNTIME_EVIDENCE) after verified reproduction, then calls the RAW model using
the existing RUNTIME_EVIDENCE state-specific command surface. From that point,
model debugger/action choices remain model-authored.

**What happened (from `experiments/debugger_interaction_v2_d1/runs/
run-1-live-2026-08-10/evidence.json`, verified):**

1. The model authored the PDB command `break 20` (raw_response_text, parse
   status `accepted`, normalized_command `break 20`).
2. The controller accepted it and translated it to the internal
   `start_pdb_session` directive (action_name `start_pdb_session`,
   breakpoint_line 20).
3. The backend dispatched `start_pdb_session`.
4. The backend returned `tool_error` — the requested breakpoint line 20 was
   outside the 19-line probe.

**Layer order (important):** The model authored `break 20`. The model did
**not** author the literal `start_pdb_session` action — that is the
controller-translated internal action / backend dispatch. The `break 20`
command **reached the real backend** and ended in `tool_error`. It was not
rejected before reaching the backend.

**Counts:**
- Model-authored PDB commands: 1 (`break 20`).
- Controller-accepted PDB directives: 1.
- Successful non-error PDB observations: **0** (`get_source_window`
  observations are source reads, not PDB state observations).
- Successful iterative debugger turns: **0**.
- Post-debug diagnoses: `[]` (none).
- Candidate patch: `null` (none).
- Verifier executed: false.
- Gate B: **FAIL** (need ≥2 accepted PDB commands, got 1).
- Gate C: **FAIL** (has_pdb_evidence true, has_diagnosis false, has_patch
  false, resolved false).
- Controller: final_state `Failed`, stop_reason `budget_exhausted`,
  17 model calls, 17 steps.
- Tokens: 17,686 total (17,506 prompt + 180 completion).
- Run duration: 34,125 ms.

### 13.2 S2 — cp118 debugger interaction

**Setup:** Definitive cp118 checkpoint, exact same D1 runtime-entry treatment,
RAG OFF. The interface was not changed between RAW and cp118 (to avoid
confounding the comparison).

**What happened (master plan §S2):**

1. Reproduction succeeded; identical D1 administrative runtime-entry reached
   RuntimeEvidence.
2. cp118 authored one PDB command: `continue`.
3. The real PDB backend rejected it because no active PDB session existed.
4. No second PDB command was issued.

**Counts:**
- Model-authored PDB commands: 1 (`continue`).
- Successful non-error PDB observations: **0**.
- Successful iterative debugger turns: **0**.
- Post-debug diagnoses: none. Patch: none. Verifier: not executed.
- Gate B (legacy and strict): **FAIL**.
- Gate C: **FAIL**.

**Provenance note:** The S2 negative *outcome* is sourceable via master-plan
§S2 prose. The historical statement "5 model calls / 3226 tokens" appears in
master-plan prose but is **not sourceable to any frozen in-repo artifact** —
repo-wide search returns zero hits. No frozen S2 run-result file exists in the
repository. The load-bearing S2 facts are the command (`continue`), the
backend rejection, zero observations, and Gate B/C failure. The call/token
figure is not used as a scientific metric in this report.

### 13.3 Interpretation

The accepted interpretation (S5, preserved verbatim):

> Under the frozen D1 treatment, neither RAW nor cp118 demonstrated a
> successful debugger loop on the single curated task. This does not support a
> broad claim that fine-tuning harms debugger use; the training formulation
> contained no debugger/tool supervision and both model conditions failed the
> strict loop criterion.

Both conditions each produced exactly one model-authored PDB command. Neither
obtained a successful non-error observation nor a second accepted command.
This was, at the time, a **bounded negative result**, not "no evidence." The
infrastructure can return debugger observations to the model; at the S8/S9
snapshot, real-model interpretation of a successful debugger observation had
not been demonstrated.

**Superseded:** the R1-R6 phase (Section 22) demonstrated exactly that chain
under the repaired interface — the interface confounds identified from
D1/S2 (command affordances, staged PAUSED progression, diagnosis retention,
bounded patch checkpoint, verifier authority) were removed, after which a
real model obtained successful non-error PDB observations (R1), completed a
multi-turn loop with diagnosis (R2), and reached verifier RESOLVED via a
debugger-informed semantic patch (R3). D1/S2 remain frozen historical
evidence and are not rewritten.

---

## 14. Professor TODO #23–25 assessment

The professor's requirements map to three debugger-capability questions. The
S5 coverage matrix
(`analysis/s5_final_controlled_comparison/s5_professor_todo_coverage_matrix.md`)
assesses each against accepted evidence with eight columns kept separate.

### Summary verdict

| TODO | Plumbing/backend | Deterministic evidence | Positive real-model success | Bounded negative result |
|---|---|---|---|---|
| #23 — Fine-tuned model generates debugger commands and interprets output | YES | YES | **YES (R1-R6, §22)** | YES (historical D1/S2) |
| #24 — Breakpoint / variables / stack / step interaction | YES | YES | **YES (R2 multi-turn loop, §22)** | YES (historical D1/S2) |
| #25 — Debugger → patch → tests/verifier | YES | YES | **YES (R3/R5/R6 verifier RESOLVED, §22)** | YES (historical D1/S2) |

**Engineering capability:** all three TODOs have backend + deterministic
plumbing. The PDB backend supports the full grammar; deterministic/scripted
trajectories exercise it; the golden reachability capture records 2 successful
PDB observations.

**Positive real-model end-to-end: achieved in the R1-R6 phase (§22).** Under
the repaired interface, a real model authored a valid breakpoint that paused
a real PDB session (R1), completed a multi-turn breakpoint→stack→locals→
step/next→post-step stack→diagnosis chain (R2), produced a debugger-informed
semantic patch that reached the independent verifier (R3, with the
serialization-normalization qualifier), authored a regression test verified
against buggy and fixed workspaces (R4), resolved the clean base-14B holdout
5/5 (R5), and the project-fine-tuned 7B model resolved 8/8 on the disjoint
validation (R6). TODO #23–25 are therefore positive at the current date; the
S8/S9 snapshot verdict (NO for positive real-model success) is historical.

**Bounded negative (historical):** D1 (RAW) and S2 (cp118) each produced one
model-authored PDB command and zero successful observations under the old
interface. Preserved as frozen evidence; superseded by R1-R6.

**Boundary clauses (apply to every row):**
- Deterministic PDB backend tests are not evidence the model used the
  debugger successfully.
- One accepted debugger command is not successful interaction when the backend
  observation was an error/rejection.
- The static QuixBugs gcd verifier RESOLVED does not demonstrate debugger use.
- "Engineering capability exists" must not be read as positive model-behavior
  evidence.
- No negative finding is converted into success; historical negatives stay
  negative.

### Static real-provider success (not debugger success)

A real-provider static QuixBugs gcd path reached verifier-confirmed RESOLVED
(F2P 5/5, P2P 1/1, full suite 6/6; `docs/datasets/quixbugs/baseline-8-task.md`,
`research/quixbugs/GCD_SMOKE_MANIFEST_V1.json`). This demonstrates the
**real model → patch → verifier** static path. It does **not** demonstrate
debugger use — the debugger was not used in that path. It is kept as a
separate `static_verifier_success` axis (S5 axis 7).

---

## 15. S5 controlled comparison

The S5 final controlled comparison
(`analysis/s5_final_controlled_comparison/`) is the canonical synthesis of
the project's accepted evidence. It is not another model campaign — no model
was run, S4 was not resumed, no historical evidence was modified.

### Eight axes kept separate

| Axis | Question | Key result |
|---|---|---|
| 1. Localized executable repair | A vs B vs C | RAW 5/40 RESOLVED; cp118 0/40; S4 NOT_EVALUATED |
| 2. Fine-tuning transfer (DPO) | Auxiliary | B1 27/30, SFT 27/30, DPO 21/30; CLOSED |
| 3. RAG treatment | C | 10/40 PARTIAL; NOT_EVALUATED; no success/failure claim |
| 4. Debugger interaction | D, E | D1 0 observations; S2 0 observations; both Gate B/C FAIL |
| 5. Model-generated test capability | S1-P original live | Generated test exposed bug; original patch did NOT apply |
| 6. Serialization sensitivity | S1-P post-hoc | Normalized patch RESOLVED; kept separate from original |
| 7. Static verifier success | QuixBugs gcd | F2P 5/5, P2P 1/1, RESOLVED; NOT debugger use |
| 8. Local inference engineering | Efficient SDPA | ~84.6× matched speedup; engineering only |

These axes are not combined into one score.

### S1-P — Model-generated test probe (auxiliary)

**Original live:** Frozen RAW Qwen generated an executable regression test on
the first attempt from an explicit public behavior specification (task
`curated-none-handling-001`; tokens 957 = 776 prompt + 181 completion). The
generated test failed on the buggy implementation as required. The one-shot
model repair contained the correct semantic `None` guard, but the raw patch
was rejected (`PatchValidationError: Git metadata lines are not supported`).
The original live result was **NOT RESOLVED**.

**Post-hoc serialization diagnostic:** A separate deterministic
normalization made no semantic body-line changes (semantic-hunk hash
preserved), normalized only serialization metadata/hunk-header defects. Under
that diagnostic, the same frozen test passed and the verifier reported F2P
1/1, P2P 2/2, **RESOLVED**.

The original live result and the post-hoc diagnostic are **always reported
separately**. The normalized patch must not be used as if the original model
output had applied.

**Professor-facing claim:** Given an explicit expected-behavior specification,
the frozen RAW model generated a test that exposed the bug. Its separately
model-produced semantic repair satisfied the same frozen test and independent
verifier only after deterministic post-hoc serialization normalization; the
original raw live patch itself did not apply.

### Efficient SDPA (engineering appendix)

The accepted performance evidence is two distinct blocks:

**A. Matched-prompt controlled diagnostic** (6079 input + 1 output, identical
model/config, only attention path differs):

| Path | Total elapsed | torch peak allocated |
|---|---|---|
| Stock (MATH-SDPA) | 301.399 s | 15,350.3 MiB |
| Efficient (repeat_kv) | 3.562 s | 7,371.0 MiB |
| **Speedup** | **~84.6×** | — |

**B. Reusable harness reproduction** (optimized only, no matched stock
counterpart): 6113+1 at 4.982 s, 6113+256 at 63.806 s, torch allocator peak
~7,380 MiB.

Real-model parity: same top token, cosine 0.99995, max-abs 0.125, mean-abs
0.014.

All MiB/GiB figures are **torch/CUDA allocator peak, not physical/resident
VRAM** (overcounts under Windows WDDM). The 12,227 MiB is GPU capacity, not
workload residency. `physical_resident_vram_usage` = NOT_RECORDED.

This is engineering evidence, not a retroactive change to S4, and is not
collapsed into any repair score.

---

## 16. S6 evidence presentation

The S6 professor-facing evidence presentation
(`presentation/s6-real-debugging-evidence/`) is a self-contained static HTML
that opens locally with no external URLs, CDNs, scripts, or stylesheets. Every
load-bearing fact carries a `data-claim-id` bound to an entry in
`s6_presentation_manifest.json`.

**Status semantics:**
- `presentation_reproducible` = **YES**
- `positive_real_model_dynamic_debugger_demo` = **NO**
- `bounded_negative_real_model_evidence_presented` = **YES**

The presentation is **not** a successful debugger demo. It is a reproducible
bounded-negative evidence presentation that distinguishes model decisions from
deterministic tooling. It presents the D1 trace (`break 20` → tool_error, 0
observations) and the S2 trace (`continue` → rejected, 0 observations) with
explicit provenance tiers, and the static gcd success boundary with the
explicit caveat that it does not demonstrate debugger use.

The manifest distinguishes **scientific provenance** (where a fact comes from)
from **reproducibility carrier** (the tracked clean-checkout artifact that
reproduces the displayed value when the scientific source is untracked or
prose-only). An untracked or prose-only source is never promoted to
clean-checkout reproducibility. D1's underlying scientific source is
`local_untracked_accepted` (frozen evidence.json with recorded SHA256, not
tracked in Git); its reproducibility carrier is the tracked S5 coverage
matrix. S2's underlying scientific source is `master_plan_prose_only`; its
reproducibility carrier is likewise the tracked S5 coverage matrix. Underlying
provenance is never promoted.

---

## 17. Discussion

### 17.1 Interpreting the negative debugger result (historical)

At the S8/S9 snapshot, the project's two tested model policies (RAW and
cp118) had not demonstrated the learned interaction competence required to
reach usable runtime evidence **under the then-current interface**. This
outcome was consistent with the current literature (S7 §8):

- Runtime information is valuable, but stateful debugger use is a distinct
  agent capability shaped by model strength, interface abstraction, and
  trajectory-specific training.
- The strongest 2026 evidence (ADI, InspectCoder, Debug2Fix) finds that raw
  debugger exposure is not reliably beneficial; agent-oriented state
  abstractions and middleware-enforced legal transitions are more effective.
- Ordinary localized-repair SFT should not be assumed to produce debugger
  competence automatically (SWE-Gym, Open-SWE-Traces).

The RAW/cp118 result was **negative evidence about emergent debugger
interaction under the tested model/checkpoint/scaffold combination**, not
negative evidence about the value of debugging information itself. This
interpretation is now historically bounded: the repaired-interface R1-R6
phase (Section 22) demonstrated that the interaction failure was
interface/scaffold-dependent and that interface repairs plus
debugger-oriented trajectory supervision (R6) close the loop that D1/S2 could
not. The literature predictions that interface abstraction and
trajectory-specific training matter (rather than raw debugger exposure) are
corroborated by that outcome.

### 17.2 Architecture defense

The single-agent + deterministic-controller + typed-tool architecture was a
defensible design choice (S7 §7, §10). The architecture deliberately separates
probabilistic reasoning from deterministic execution control. Current evidence
supports this separation: recent debugger-agent systems report substantial
difficulty with low-level stateful debugger protocols and achieve better
results through agent-oriented state abstractions and middleware.

The single-agent component is somewhat less strongly supported than the
deterministic-controller component, because BOAD (ICLR 2026, peer-reviewed)
provides credible evidence that optimized delegation can improve SWE
performance. But that evidence does not establish multi-agent orchestration as
necessary for debugger-informed repair, and comparisons are frequently
confounded by additional inference and scaffolding.

The accepted closeout statement (S7 bottom line, valid at the S8/S9
snapshot):

> The project successfully built the deterministic machinery required for real
> stateful debugging, but its two tested model policies did not demonstrate
> the learned interaction competence required to reach usable runtime
> evidence. That outcome is consistent with the current literature, which
> increasingly finds that runtime information is valuable but stateful
> debugger use is a distinct agent capability shaped by model strength,
> interface abstraction, and trajectory-specific training. The existing
> single-agent + deterministic-controller + typed-tool architecture remains a
> defensible experimental baseline; debugger-specific trajectory training and
> higher-level state-aware tool abstractions are the most directly
> evidence-backed next steps, while multi-agent orchestration is a secondary
> controlled ablation rather than a missing prerequisite.

The R1-R6 phase (Section 22) implemented exactly those evidence-backed next
steps — repaired state-aware interface abstractions and debugger-oriented
trajectory post-training — and the resulting positive R1-R6 results
supersede the "did not demonstrate" clause of the S8/S9 statement without
changing the architecture defense.

---

## 18. Limitations

1. **No real model successfully used the debugger (historical, S8/S9).**
   At the 2026-08-11 snapshot, neither RAW nor cp118 obtained a successful
   non-error PDB observation or completed an iterative debugger loop under
   the then-current interface, so debugger effectiveness was not measured.
   This is superseded by the R1-R6 phase (§22): real models now obtain
   successful debugger observations, complete multi-turn loops, and reach
   verifier RESOLVED under the repaired interface. The current limitations
   that remain are the incomplete R6 final holdout
   (INCOMPLETE_HARDWARE_STOP, §22.6) and the absence of a matched-base R6
   ablation for causal fine-tuning claims.

2. **Bounded negative, not broad failure.** The debugger result is from one
   curated task under one frozen treatment per condition. It does not support
   a broad claim that fine-tuning harms debugger use.

3. **cp118 per-task evidence is external.** The cp118 RAG-OFF 40-task
   per-task raw evidence is a Drive-hosted D7 bundle, not in the current
   repository. Only accepted aggregates are in-repo.

4. **S4 RAG is PARTIAL / NOT_EVALUATED.** 10/40 tasks, first 10 in manifest
   order, not random or representative. No RAG success/failure claim.

5. **S2 frozen run-result not in repo.** The S2 outcome is sourceable via
   master-plan prose; specific call/token figures are not sourceable to any
   frozen artifact.

6. **S1 original raw-run artifact missing from disk.** D1 evidence is intact
   and SHA256-verified and is the authoritative RAW-debugger source.

7. **S1-P post-hoc has no dedicated frozen result artifact.** The result
   exists only as master-plan prose; the source commit ref is valid.

8. **Trusted-local, not hostile-code-sandboxed**, for the curated-fixture
   path. The WSL2/Bubblewrap/`prlimit` boundary was built for QuixBugs
   execution specifically.

9. **BugsInPy license unresolved.** Adapter and preflight are built; execution
   remains license-blocked. This is a pending review item, not a permanent
   block.

10. **CUDA allocator peaks are not physical/resident VRAM.** All GPU-memory
    figures in the project are torch/CUDA allocator peak, which overcounts
    under Windows WDDM. No independent physical/resident measurement was
    recorded.

11. **Cost/token accounting is honest-missing where providers do not report.**
    The harness never invents provider-reported fields.

12. **The 40-task QuixBugs cohort is a controlled screening cohort**, not a
    universal benchmark. QuixBugs is toy-scale, single-file, single-defect;
    known to be vulnerable to overfit "fixes" (Ye et al. 2018/2019,
    peer-reviewed) — irrelevant here because every QuixBugs gold-patch
    baseline uses the literal upstream fix, but relevant to any future
    model-generated run.

---

## 19. Future work

Ranked by evidence backing (S7 §9, S5 remaining-gaps §2); statuses updated
through 2026-08-13:

1. **Debugger-specific trajectory post-training — DONE for the 7B campaign
   (R6, §22.4); the previously open call for this direction was implemented
   and produced the 8/8 disjoint validation.** SWE-Gym and Open-SWE-Traces
   show that actual agent trajectories can train tool-using behavior; NExT
   required explicit execution-state training. R6 built exactly that
   debugger-specific trajectory corpus (breakpoint → observation →
   step/locals → updated diagnosis → patch) from frozen QuixBugs tasks and
   fine-tuned the project 7B model. Remaining: a matched-base R6 ablation is
   still absent, so causal fine-tuning improvement is not claimed.

2. **Stronger base-model rerun under the identical controller — high
   priority.** The current base (Qwen2.5-Coder-7B) is a 7B-class model. A
   stronger base model under the same deterministic controller and typed-tool
   interface would test whether the interaction failure is model-capacity-
   dependent. (R5 already provides the base-14B evidence on the curated
   holdout treatment; a 14B + R6-style debugger SFT combination is a natural
   next step.)

3. **Higher-level/state-aware debugger interface ablation — high priority.**
   ADI/FramePilot's agent-oriented dynamic interface outperformed
   conventional PDB. The R1-R6 repaired interface (staged PAUSED
   progression, production-region filtering, sanitized diagnostics) is the
   project's implementation of this direction; a controlled ablation against
   the raw-interface D1/S2 treatment is the appropriate measurement.

4. **Execution-feedback RL / process training — medium-high priority.**
   SWE-TRACE and SWE-Master support process-reward and execution-feedback
   post-training pipelines.

5. **Multi-agent debugger specialization — medium priority, controlled
   ablation only.** BOAD is credible but not debugger-specific. A
   compute-matched controlled ablation (not a general multi-agent expansion)
   is the appropriate test.

6. **More generic multi-agent expansion — lowest priority.** AgentForge and
   DeLM show gains but are heavily confounded by token cost and extra
   inference.

### S4 optimized-rerun future GO conditions

If a future load-bearing claim comes to depend on the cp118+RAG correctness
result, a future authorized campaign would require: explicit owner +
FirstMate authorization; a fresh frozen contract distinct from `966c2aab...`;
strict separation from the existing 10 stock-generated tasks; a full 40-task
completion marker before the frozen C9 evaluator runs; identical cp118
adapter identity; and GPU-memory telemetry recorded with correct semantics
(allocator peak AND independent device measurement as separate fields). The
Efficient SDPA capability would remove the dominant compute constraint.

### BugsInPy unblocking

Resolve BugsInPy's licensing review and build the OS/container-level
containment upgrade required before any BugsInPy execution.

---

## 20. Conclusion

This project delivers a verifier-backed, fail-closed, single-controller
agentic debugging platform with a real PDB integration, a replay-verified
event/trajectory system, an explicitly-authorized real-model live evaluation
harness, a licensed infra-validated external-dataset path (QuixBugs), a
QLoRA SFT pipeline (historical SWE-rebench V2 localized-repair campaign, and
the R6 debugger-oriented QuixBugs-derived campaign), a definitive RAW-vs-cp118
executable-repair comparison, a controlled DPO investigation, a partial
cp118+RAG treatment, a canonical eight-axis controlled comparison (S5), a
professor-facing evidence presentation (S6), a focused literature closeout
(S7), the repaired-interface R1-R4 real-model debugger milestones, the clean
R5 base-14B holdout, the R6 debugger-oriented fine-tuning campaign with its
8/8 disjoint validation, and the complete professor-facing structured JSON
trace deliverable.

The primary scientific findings are:

1. **Localized-repair QLoRA SFT did not transfer to executable repair on the
   held-out QuixBugs cohort (historical).** cp118 produced 0/40 applicable
   patches and 0/40 resolved tasks, vs RAW's 20/40 apply and 5/40 resolved.
   This is a formulation-specific negative transfer (output-policy
   degeneration, over-generation, scope explosion), not a general claim that
   fine-tuning is harmful. It does not apply to the R6 debugger-oriented
   campaign, which used a different dataset, formulation, and interface.

2. **Neither RAW nor cp118 demonstrated a successful debugger loop under the
   old interface (historical).** Both authored one PDB command that ended in
   error/rejection with zero successful observations. This bounded negative
   was superseded by the repaired-interface R1-R6 phase: a real model paused a
   real PDB session (R1), completed a multi-turn breakpoint→stack→locals→
   step→diagnosis loop (R2), reached the independent verifier RESOLVED with a
   debugger-informed semantic patch (R3, count-only serialization
   normalization qualifier), and authored a regression test verified on buggy
   and fixed workspaces (R4).

3. **R5 — clean generalized base-14B holdout.** Qwen2.5-Coder-14B-Instruct
   BASE resolved 5/5 curated bugs under the final sanitized r5.9 treatment
   with 0 leakage findings across 41 audited prompts. The r5.7 5/5 was
   disqualified for hidden-test leakage in PATCH prompts and is preserved as
   historical upper-bound evidence. R5 does not prove fine-tuning caused an
   improvement.

4. **R6 — debugger-oriented project fine-tuning.** The project-fine-tuned
   Qwen2.5-Coder-7B debugger achieved **8/8 RESOLVED on a frozen,
   task-disjoint QuixBugs validation set** using real debugger/tool execution
   and independent verification (97 model calls, 64,783 tokens, 841,702 ms,
   zero row errors). The stronger five-task curated final holdout is
   **INCOMPLETE_HARDWARE_STOP** — two completed rows (RESOLVED and
   BREAKING_RESOLVED) plus three interrupted/unstarted tasks; it is not a
   completed 5-task result. No matched-base R6 ablation exists, so causal
   fine-tuning improvement is not claimed.

5. **DPO is not justified** given the available data (DPO 21/30 vs baseline
   27/30 and matched SFT 27/30).

6. **RAG treatment remains NOT_EVALUATED** (10/40 partial, compute-constrained,
   no success/failure claim).

7. **The single-agent + deterministic-controller + typed-tool architecture is
   a defensible controlled experimental baseline**, aligned with — not
   contradicted by — the strongest recent literature evidence; the R1-R6
   positive results were achieved without changing that architecture.

The project's central contribution evolved from infrastructure, evaluation
methodology, and an honest bounded-negative experimental result into
**positive real-model dynamic debugging plus project-fine-tuned validation,
with the professor-facing trace deliverable complete**. The final five-task
tuned-model holdout remains incomplete due to local hardware power-offs; the
old negative experiments are preserved, not rewritten; and main integration
of the documentation candidate is the remaining operational step.

---

## 21. Provenance appendix (historical S8/S9 evidence)

This appendix maps each load-bearing S8/S9 conclusion to its scientific source
and provenance tier. The original audit was verified by direct Git audit
(`git ls-files --error-unmatch`) at HEAD `677992f`; the current report's
R1-R6 carriers are listed in §22.9/§23. Provenance tiers:

- `frozen_in_repo` — tracked in Git AND present in HEAD tree AND clean-checkout
  reproducible as a scientific source.
- `aggregate_external_per_task` — accepted aggregate in a tracked canonical
  artifact; per-task raw evidence is external (Drive-hosted D7 bundle), not
  in the repo.
- `master_plan_prose_only` — recorded only as narrative in a master execution
  plan; no frozen run-result file in the repo. The S5 master plan
  (`Agentic_Debugging_Master_Execution_Plan_2026-08-11_S5_CURRENT.md`) is
  **untracked**; the older `docs/archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md`
  is tracked and carries the same dataset/training/metric aggregates.
- `local_untracked_accepted` — local accepted evidence on disk, SHA256
  recorded, NOT tracked in Git, NOT clean-checkout reproducible as a
  scientific source.

A source is never silently promoted from `local_untracked_accepted` or
`master_plan_prose_only` to `frozen_in_repo`. Where an original scientific
source is local/untracked but a committed canonical artifact carries the
accepted values, the **reproducibility carrier** is identified separately —
the carrier attests clean-checkout reproducibility of the displayed value,
not the scientific provenance of the original source. A carrier is named only
when the tracked artifact has been verified to contain the exact claim.

### Primary experimental metrics

| Conclusion | Scientific source | Provenance tier | Source tracked? | Source clean-checkout? | Reproducibility carrier | Carrier contains exact claim? |
|---|---|---|---|---|---|---|
| RAW Track A: 33/40 strict, 14/40 apply, 5/40 resolved | `experiments/raw-pilot-v1.1/results/results_final.csv`, `metrics_summary.csv` | local_untracked_accepted | no | no | `s5_comparison_ledger.json` + `s5_provenance_source_map.md` | yes |
| RAW Track B: 40/40 extracted, 20/40 apply, 5/40 resolved | untracked S5 master plan §2.5; tracked `docs/archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` §2.5 | master_plan_prose_only | no (S5 plan); yes (2026-08-10 plan) | no (S5 plan); yes (2026-08-10 plan) | `s5_comparison_ledger.json` + `docs/archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` | yes |
| cp118: 40/40 extracted, 0/40 apply, 0/40 resolved, 19/40 truncation | untracked S5 master plan §2.5; tracked 2026-08-10 master plan §2.5; per-task Drive-hosted D7 bundle | aggregate_external_per_task | aggregates yes (2026-08-10 plan + S5); per-task no | aggregates yes (2026-08-10 plan + S5); per-task no | `s5_comparison_ledger.json` + `docs/archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` | yes |
| DPO: B1 27/30, SFT 27/30, DPO 21/30 | untracked S5 master plan §2.6; tracked 2026-08-10 master plan §2.6 | master_plan_prose_only | no (S5 plan); yes (2026-08-10 plan) | no (S5 plan); yes (2026-08-10 plan) | `s5_comparison_ledger.json` + `docs/archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` | yes (values in both) |
| S4: 10/40, NOT_EVALUATED, 5/10 truncation | `PARTIAL_RUN_RECORD.json`, `run-identity.json` (local); `s4_contract.json` (tracked) | local_untracked_accepted (run records); frozen_in_repo (contract) | run records no; contract yes | run records no; contract yes | `s5_comparison_ledger.json` + `s5_provenance_source_map.md` | yes |
| D1: `break 20` → tool_error, 0 obs, Gate B/C FAIL, 17,686 tokens | `experiments/debugger_interaction_v2_d1/runs/.../evidence.json` (SHA256 `c7a405cc...`) | local_untracked_accepted | no | no | `s5_professor_todo_coverage_matrix.json/.md` + `s5_comparison_ledger.json` | yes |
| S2: `continue` → rejected, 0 obs, Gate B/C FAIL | untracked S5 master plan §S2 | master_plan_prose_only | no | no | `s5_professor_todo_coverage_matrix.json/.md` + `s5_comparison_ledger.json` | yes |
| Static gcd: F2P 5/5, P2P 1/1, RESOLVED | `docs/datasets/quixbugs/baseline-8-task.md`, `research/quixbugs/GCD_SMOKE_MANIFEST_V1.json` | frozen_in_repo | yes | yes | — (source is tracked) | — |
| S1-P original live: 957 tokens, NOT RESOLVED | `AI_REVIEW/s1p_.../live-run-1/evidence.json` (source commit `c47be60e...`) | local_untracked_accepted | no | no | `s5_comparison_ledger.json` + `s5_controlled_comparison_report.md` | yes |
| S1-P post-hoc: RESOLVED F2P 1/1 P2P 2/2 | untracked S5 master plan §S1-P (source commit `9e1b9dc9...`, ref valid) | master_plan_prose_only | no | no | `s5_comparison_ledger.json` + `s5_controlled_comparison_report.md` | yes |
| SDPA: 301.399→3.562s, ~84.6× | `_ai-review/perf-cp118-efficient-sdpa-v1/` (local); source impl `experiments/local_inference_perf/` @ `10bdfa91...` (untracked branch) | local_untracked_accepted | no | no | `s5_comparison_ledger.json` + `s5_controlled_comparison_report.md` | yes |

### Dataset / training facts

| Fact | Scientific source | Provenance tier | Source tracked? | Source clean-checkout? | Reproducibility carrier | Carrier contains exact claim? |
|---|---|---|---|---|---|---|
| SWE-rebench V2: 1,594 eligible, 347 repos | `experiments/swe_rebench_v2_corpus/b14_package_v2/B14_PACKAGE_MANIFEST.json` | local_untracked_accepted | no | no | `docs/archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` §2.3 (tracked) | yes |
| Split: 1000/150/444, overlap 0, seed 20260808 | `B14_PACKAGE_MANIFEST.json` → `frozen_b13_contract` | local_untracked_accepted | no | no | `docs/archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` §2.3 (tracked) | yes |
| SFT formulation: localized repair, oracle-file-localized source → gold diff | untracked S5 master plan §2.3; local `B14_PACKAGE_MANIFEST.json` → `method` | master_plan_prose_only + local_untracked_accepted | no (S5 plan); no (manifest) | no (S5 plan); no (manifest) | `docs/archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` §2.3 (tracked) | yes |
| cp118: step 118, eval_loss 0.45070546, adapter `65b5ed9a...` | `experiments/cp118_rag_definitive/s4_contract.json` | frozen_in_repo | yes | yes | — (source is tracked) | — |
| Qwen2.5 revision: `c03e6d35...` | untracked S5 master plan §2.4 | master_plan_prose_only | no | no | `s5_comparison_ledger.json` (line 47) + `docs/archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` §2.4 (both tracked) | yes |
| QuixBugs 40-task cohort: `pilot_manifest_frozen_v1.jsonl` (SHA256 `57208248...`) | `experiments/raw-pilot-v1.1/state/quix40-v1/` | local_untracked_accepted | no | no | `s5_provenance_source_map.md` (tracked) | yes (SHA256 + path) |

### Literature facts

All literature claims in Section 3 trace to the S7 closeout
(`research/literature/agentic_debugging_literature_closeout_2026-08-11.md`,
`frozen_in_repo`, tracked + clean-checkout available). Evidence tiers
(peer-reviewed / preprint / technical report) are marked per work in the S7
evidence table (§11) and preserved throughout this report.

### Engineering / presentation status

| Fact | Source | Provenance tier | Tracked? | Clean-checkout? |
|---|---|---|---|---|
| S6: reproducible=YES, positive_demo=NO, bounded_negative=YES | `presentation/s6-real-debugging-evidence/s6_presentation_manifest.json` | frozen_in_repo | yes | yes |
| PDB backend + deterministic tests pass | `agentic_debugger/runtime/pdb_*`, `tests/` | frozen_in_repo | yes | yes |
| Golden reachability: 2 successful PDB observations | `tests/golden_trajectories/data/quixbugs-gcd-pdb-reachability-captured-result.json` | frozen_in_repo | yes | yes |

### Git-audited source status summary

The following load-bearing sources were verified by `git ls-files
--error-unmatch` at HEAD `677992f`:

| Source path | Tracked? | Tier applied |
|---|---|---|
| `experiments/raw-pilot-v1.1/results/results_final.csv` | no | local_untracked_accepted |
| `experiments/raw-pilot-v1.1/results/metrics_summary.csv` | no | local_untracked_accepted |
| `experiments/swe_rebench_v2_corpus/b14_package_v2/B14_PACKAGE_MANIFEST.json` | no | local_untracked_accepted |
| `experiments/cp118_rag_definitive/s4_contract.json` | **yes** | frozen_in_repo |
| `experiments/cp118_rag_definitive/runs/.../PARTIAL_RUN_RECORD.json` | no | local_untracked_accepted |
| `experiments/debugger_interaction_v2_d1/runs/.../evidence.json` | no | local_untracked_accepted |
| `AI_REVIEW/s1p_.../live-run-1/evidence.json` | no | local_untracked_accepted |
| `_ai-review/perf-cp118-efficient-sdpa-v1/` | no | local_untracked_accepted |
| `Agentic_Debugging_Master_Execution_Plan_2026-08-11_S5_CURRENT.md` | no | (untracked; not a carrier) |
| `docs/archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` | **yes** | frozen_in_repo (carrier for dataset/training/metric facts) |
| `analysis/s5_final_controlled_comparison/*` (6 files) | **yes** | frozen_in_repo |
| `presentation/s6-real-debugging-evidence/*` (2 files) | **yes** | frozen_in_repo |
| `research/literature/agentic_debugging_literature_closeout_2026-08-11.md` | **yes** | frozen_in_repo |
| `docs/datasets/quixbugs/baseline-8-task.md` | **yes** | frozen_in_repo |
| `research/quixbugs/GCD_SMOKE_MANIFEST_V1.json` | **yes** | frozen_in_repo |
| `tests/golden_trajectories/data/quixbugs-gcd-pdb-reachability-captured-result.json` | **yes** | frozen_in_repo |
| `docs/evaluation/model-rag-sft-dpo.md` | **yes** | frozen_in_repo (does NOT contain DPO 27/30/21/30 values) |

No source is labeled `frozen_in_repo` unless it is tracked in HEAD. No source
with `tracked=no` or `clean_checkout_available=no` is labeled `frozen_in_repo`.
The untracked S5 master plan is not used as a clean-checkout carrier; the
tracked 2026-08-10 master plan and tracked S5 artifacts serve as carriers
where their content has been verified to contain the exact claim.

### Known provenance gaps (surfaced, not smoothed)

1. S1 original raw-run artifact missing from disk; D1 is the authoritative
   RAW-debugger source (local/untracked).
2. S2 "5 calls / 3226 tokens" not sourceable to any frozen in-repo artifact;
   the negative outcome is carried by tracked S5 artifacts.
3. S1-P post-hoc has no dedicated frozen result artifact (source commit valid;
   result carried by tracked S5 ledger + comparison report).
4. cp118 definitive per-task evidence is external (Drive-hosted D7 bundle);
   accepted aggregates are carried by tracked S5 ledger + 2026-08-10 master plan.
5. D1 evidence is local/untracked (SHA256 recorded, not Git-tracked); the
   tracked S5 coverage matrix is its reproducibility carrier.
6. RAW Track A CSVs, SWE-rebench V2 manifests, and S4 run records are
   local/untracked; their accepted values are carried by tracked S5 artifacts
   and the tracked 2026-08-10 master plan.
7. SDPA performance measurements and source implementation
   (`experiments/local_inference_perf/` @ `10bdfa91...`) are local/untracked;
   the accepted ~84.6× result is carried by the tracked S5 comparison report.
8. The S5 master plan (`..._S5_CURRENT.md`) is untracked; it is not used as a
   clean-checkout carrier. The tracked 2026-08-10 master plan carries the same
   dataset/training/metric aggregates and serves as the clean-checkout carrier
   for those facts.
9. The tracked `docs/evaluation/model-rag-sft-dpo.md` does NOT contain the
   DPO 27/30/21/30 values; those are carried by the tracked S5 ledger and the
   tracked 2026-08-10 master plan.

### Hash cross-checks performed (S5)

| Identity | Verified match |
|---|---|
| S4 source_commit_sha `acfe131a...` | ✅ `PARTIAL_RUN_RECORD.json` ↔ `run-identity.json` |
| S4 contract_sha256 `966c2aab...` | ✅ `s4_contract.json` ↔ `run-identity.json` |
| S4 run_identity_sha256 `072f1d69...` | ✅ `run-identity.json` ↔ `PARTIAL_RUN_RECORD.json` |
| S4 adapter_tree `65b5ed9a...` | ✅ `s4_contract.json` ↔ `run-identity.json` |
| D1 source_commit_sha `7bda64d...` | ✅ `evidence.json` ↔ `RUN_SUMMARY.md` |
| D1 contract_sha256 `1d8819cb...` | ✅ `evidence.json` |
| S1-P tokens 957 = 776 + 181 | ✅ `live-run-1/evidence.json` |
| RAW strict_valid 33/40 | ✅ `results_final.csv` ↔ `metrics_summary.csv` (0.825) |
| SDPA cosine 0.99995 | ✅ `parity_real_model.json` |

---

*This report is synthesis of accepted project evidence through 2026-08-13.
The 2026-08-11 S8/S9 snapshot is archived verbatim at
`docs/archive/reports/final-report-2026-08-11.md` (blob
`0dcd54773505f9a4797b6bf49ac3780552b85740`); the 2026-07-31 snapshot is at
`docs/archive/reports/final-report-v1.md`. The 2026-08-11 closeout is
archived at `docs/archive/status/project-closeout-2026-08-11.md`; the current
handoff status is `docs/project-closeout.md`.*

---

## 22. Post-S9 phase — R1-R6 real-model debugger milestones, R5 holdout, R6 fine-tuning (2026-08-11 → 2026-08-13)

After the S9 bounded-negative closeout, the project reopened on the two
evidence-backed directions the closeout itself identified: repaired
state-aware debugger interface abstractions and debugger-oriented trajectory
post-training. This section records the R1-R6 phase. Every claim is stated to
the level supported by frozen evidence; the independent verifier is the
correctness authority throughout.

### 22.1 R1 — real debugger entry (commit `c842d69`, 2026-08-11)

Accepted positive capability (frozen evidence
`experiments/debugger_interaction_v2_r1/runs/run-2-live-2026-08-11/evidence.json`):

- the real model authored a valid breakpoint command;
- the real PDB session actually paused (`gate_r1.passed=true`,
  `first_command=start_pdb_session`, observation `observation-000000003`,
  line 2, function `recent_window`);
- subsequent real-model interaction obtained a real stack observation.

This is real-model evidence — it is not collapsed into deterministic/mock
backend evidence. The r1.1 evidence records `gate_r1` passed with the exact
observation bound into the next request.

### 22.2 R2 — multi-turn real-model dynamic debugging (commit `97cc7fe`, 2026-08-11)

Accepted real-model sequence (frozen evidence
`experiments/debugger_interaction_v2_r2/runs/run-r2-live-2026-08-11/evidence.json`,
`gate_r2.passed=true` with observation ids):

1. `break` — accepted, real PDB PAUSED (observation-000000003);
2. `stack` — G1 (observation-000000004);
3. `locals` — frame inspection (observation-000000005);
4. `step`/`next` — progression (observation-000000006);
5. `stack` — post-step G2 > G1 (observation-000000007);
6. `diagnosis` — model-authored diagnosis retained verbatim.

This is positive evidence that a real model could participate in a multi-turn
runtime-debugging loop under the repaired interface/controller (staged PAUSED
progression, production-region filtering). The run itself ended on
`model_call_limit` with no patch — R2 demonstrates the dynamic loop, not the
repair.

### 22.3 R3 — debugger evidence → repair → verifier (commit `f2291df`, 2026-08-11)

Accepted chain (frozen evidence
`experiments/debugger_interaction_v2_r3/runs/run-r3-2-live-2026-08-11/evidence-corrected.json`):

- real debugger evidence (break → stack G1 → locals → next → stack G2);
- model diagnosis (retained verbatim, `diagnosis_provenance` bound to
  observation-000000007);
- model semantic patch (`model_patch_raw`, B, SHA256 `831b1c2b…`);
- PatchManager application;
- independent EvaluationVerifier `COMPLETED / RESOLVED` (F2P 1/1, P2P 2/2,
  full suite + syntax, canonical unchanged, cleaned) on candidate C
  (`8c051faa…`).

**MANDATORY QUALIFIER:** the raw semantic patch contained a unified-diff
hunk-count metadata error (`@@ -7,7 +7,7 @@` declared counts that did not
match the 6-line hunk body). A deterministic COUNT-ONLY serialization
normalization (metadata-only; paths, starts, body lines, ordering, and
`\ No newline` markers preserved; semantic body fingerprint unchanged) was
required before PatchManager/verifier accepted the candidate. The
normalization did not change repair semantics, but the raw serialized patch
did not apply perfectly as authored — this distinction remains explicit
(`model_patch_raw` vs `model_patch_serialization_normalized`, both SHA256
recorded).

### 22.4 R4 — model-generated regression test (commit `372d51f1a35e071c677391c9970f7b552bb276f2`, 2026-08-11)

Accepted result (frozen evidence
`experiments/model_generated_test_probe_r4/runs/run-r4-1-live-2026-08-11/evidence.json`,
`r4_pass=true`):

- the model authored a regression test T on its first and only attempt;
- the exact same frozen T (SHA256 `5503b93e…`, T_raw/T_parsed/T_written
  identities recorded) **FAILS** on the buggy workspace;
- the exact same T **PASSES** on the accepted R3 fixed workspace;
- the independent verifier remains `COMPLETED / RESOLVED` (F2P 1/1, P2P 2/2,
  workspace CLEANED, canonical fixture unchanged).

This is positive model-generated-test evidence. It measures test generation
only; the independent verifier over the frozen F2P/P2P contract remains the
correctness authority.

### 22.5 R5 — clean generalized holdout, BASE 14B (r5.0→r5.9, 2026-08-11/12)

**This distinction is critical: the successful R5 model is
Qwen2.5-Coder-14B-Instruct BASE (adapter_applied=false, base revision
`aedcc2d42b622764e023cf882b6652e646b95671`), not the project fine-tuned 7B.**

- **R5.2/R5.3 (RAW 7B):** the frozen treatment (terminal progression, fence
  unwrap, whole-file repair representation, real verifier-feedback loop,
  context fuzz) was proven mechanically; the RAW 7B model repeatedly authored
  semantically wrong repairs (0/5). Preserved as historical baseline.
- **R5.4-R5.6 (14B):** model-identity escalation to 14B; every authored
  repair was semantically correct; r5.5 fixed fence-in-content unwrap; r5.6
  added verifier-RESOLVED closeout (r5.5 was 4/5 with a post-RESOLVED
  greedy-decoding regression on 005).
- **R5.7 (14B): reached 5/5 but DISQUALIFIED** — hidden-test content leaked
  into PATCH prompts (raw pytest failure output and failing-verifier-record
  tails were forwarded to the model). Preserved as historical upper-bound
  evidence; tracked regression fixture
  `tests/fixtures/old_r57_leakage/` proves the old prompts fail the accepted
  anti-leakage auditor (102 findings across the five task classes).
- **R5.9 (14B BASE, clean holdout):** ONE COMMON DETERMINISTIC SANITIZER
  (`agentic_debugger/demo/sanitize.py`) + truthful production-exception path
  (G2=None when the production frame unwound) + region-filtered observations
  + fail-closed ACTUAL-PROMPT anti-leakage audit.

**Accepted clean result:** 5/5 RESOLVED on the five curated bugs
(`curated-none-handling-001`, `curated-off-by-one-002`,
`curated-wrong-branch-003`, `curated-mutation-alias-004`,
`curated-caller-callee-005`) under the r5.9 treatment
(`experiments/debugger_interaction_v2_r5/runs/R5.9-MATRIX-14B-CLEAN-FINAL-2026-08-12/matrix.json`:
per-row verifier RESOLVED, `end_to_end_resolved=5/5`,
`clean_holdout_prompt_audit_passed=true`, `leakage_findings=0`,
**41 audited prompts, 0 findings**).

R5 proves a clean base-14B debugger/repair holdout result under the final
sanitized treatment. It does **not** prove that fine-tuning caused an
improvement.

### 22.6 R6 — debugger-oriented project fine-tuning (2026-08-12/13)

**Historical cp118 (documented separately, unchanged):** base
Qwen2.5-Coder-7B-Instruct; training on the SWE-rebench V2 localized-repair
formulation; result on Quix40: 0/40 apply, 0/40 RESOLVED; it did not teach
debugger trajectories; a formulation-specific negative transfer result. It is
not erased or reinterpreted.

**R6 is a DIFFERENT debugger-oriented fine-tuning campaign:**

- Training source: QuixBugs pinned revision
  `4257f44b0ff1181dedaedee6a447e133219fcebf`.
- Accepted fixture construction: 29/40 usable debugger-training fixtures
  (`build_summary.json`); frozen split 21 TRAIN / 8 VALIDATION
  (`split_manifest.json`); SFT pairs 164 train / 61 validation
  (`sft/sft_manifest.json`).
- Token statistics: p50 ≈ 832, p90 ≈ 1607, p95 ≈ 1761, max 2415
  (`training_provenance.json`, measured SFT distribution).
- Training: QLoRA, `Qwen/Qwen2.5-Coder-7B-Instruct` base revision
  `c03e6d358207e414f1eca0bb1891e29f1db0e242`, 3 epochs (48 steps in the
  accepted v3 run; checkpoint-30 is the step-30 checkpoint), completion-only
  loss, physical-VRAM-bound STABLE config.
- Validation loss (disjoint SFT validation): cp10 ≈ 0.342
  (0.34222882986068726, v2 run), cp20 ≈ 0.30639
  (0.3063855767250061), cp30 = 0.2981497 (0.2981497347354889) — from the
  trainer_state records of the tracked run directories.
- Selected: **checkpoint-30** — adapter model SHA256
  `7ef5d70ab8691ea02f005ec567901932e08fb94b28ebbfab5b175a94ebb492bd`,
  adapter config SHA256
  `92ddf91e67b116a6730792722d6ee93dffeaac152901cd954389615e50cbd44e`
  (verified on disk and in `runs/frozen/ancillary/checkpoint_selection.json`
  and `docs/professor_traces/source_evidence_manifest.json`).
- **Checkpoint selection did NOT use the final five-task holdout**
  (`holdout_used_for_checkpoint_selection=false`, frozen selection record).

**R6 disjoint validation — primary tuned-model positive:** frozen
task-disjoint validation (treatment contract SHA256
`5e56165d9b08d24836874711caef306f062f5d36dc4cdbb020d97e7370ca8e78`),
real debugger/tool execution, independent verification, stages:

| Stage | Tasks | Result |
|---|---|---|
| A | quixbugs-depth-first-search | 1/1 RESOLVED |
| B | quixbugs-quicksort, quixbugs-flatten | 2/2 RESOLVED |
| C | quixbugs-find-in-sorted, quixbugs-rpn-eval, quixbugs-shortest-path-length, quixbugs-reverse-linked-list, quixbugs-kth | 5/5 RESOLVED |
| **Aggregate** | **8 tasks** | **8/8 RESOLVED; 97 model calls; 64,783 tokens; 841,702 ms; zero row errors** |

Per-stage aggregates: A 12 calls/7,588 tokens/84,342 ms; B 24 calls/14,830
tokens/203,406 ms; C 61 calls/42,365 tokens/553,954 ms
(`runs/frozen/ancillary/stage_{a,b,c}_report.json`); every task shows
debugger entry, accepted breakpoint, runtime inspection, diagnosis, patch
application, and independent verifier RESOLVED.

**Canonical wording (current claim):** "The project-fine-tuned
Qwen2.5-Coder-7B debugger achieved 8/8 RESOLVED on a frozen, task-disjoint
QuixBugs validation set using real debugger/tool execution and independent
verification."

**Non-claims:** fine-tuning is NOT claimed to have causally improved over a
matched base; there is NO proper matched-base R6 ablation; R6 is NOT directly
comparable to R5 as a causal ablation (different model identity, dataset,
and treatment); R6's final five-task holdout is NOT complete.

**R6 final five-task holdout — INCOMPLETE_HARDWARE_STOP:** the stronger
tuned-model final holdout was interrupted by repeated local hardware hard
power-offs. Completed rows
(`runs/frozen/final_holdout_partial/` + `holdout_report.json`):

- `curated-none-handling-001` — **RESOLVED**, F2P 1/1, P2P 2/2, strict pass;
- `curated-off-by-one-002` — **BREAKING_RESOLVED**, F2P 1/1, P2P 1/2, strict
  failure (the independent verifier rejected an apparently useful repair;
  preserved honestly);
- `curated-wrong-branch-003` — interrupted during a model request;
- `curated-mutation-alias-004` — not started;
- `curated-caller-callee-005` — not started.

**Correct interpretation: INCOMPLETE_HARDWARE_STOP.** Not 2/5, not 1/5, not a
failed 5-task benchmark — three tasks never produced outcomes. Anti-leakage:
0 findings across 18 prompts from the two completed tasks, but final
five-task holdout leakage=0 was NOT established (three tasks never
completed). Hardware: repeated hard power-off / Event 41 style interruption
occurred during sustained local workload; no definitive hardware root cause
was established; no VRAM-exhaustion claim; no additional sustained local GPU
campaign was pursued for closeout.

### 22.7 Professor structured JSON traces — COMPLETE

Professor-facing trace deliverable is complete
(`docs/professor_traces/`; export commit `c9afe377db3f53229755532751b485fc2a13a4e7`):

- exactly **10 professor traces** — 8 successful R6 disjoint-validation
  traces (`r6_validation/`) + 2 partial-final-holdout traces
  (`r6_holdout_partial/`);
- R5 reference removed from the final professor trace set;
- `professor_debug_trace_v1` schema (tracked);
- professor-safe audit: 10 documents, 0 findings, `passed=true`
  (`professor_safe_audit.json`);
- trace SHA manifest matches all 10 traces (`trace_sha_manifest.json`;
  hashes verified against the working tree);
- deterministic regeneration demonstrated
  (`python -m agentic_debugger.evaluation.professor_trace_r6
  --output-dir docs/professor_traces`) and pristine tracked-only /
  fresh-checkout regeneration demonstrated (frozen evidence capsule
  `experiments/r6_debugger_training/runs/frozen/` + `capsule_manifest.json`);
- hidden tests/oracles/chain-of-thought are not exposed in the professor
  traces (fail-closed leakage audit over every exported document).

### 22.8 Claim hierarchy (current)

1. Historical engineering foundation (pre-S5 infrastructure, Tasks 1-10B).
2. Historical cp118 localized-repair negative transfer (§8).
3. Historical D1/S2 real-model debugger failures under the old interface (§13).
4. R1-R4 repaired-interface positive milestones (§22.1-22.4).
5. R5 base-14B clean 5/5 holdout (§22.5).
6. R6 project-fine-tuned 7B 8/8 disjoint validation (§22.6).
7. R6 stronger final five-task holdout incomplete due hardware (§22.6).
8. Professor structured trace deliverable complete (§22.7).

Earlier failures are not erased because later work succeeded; the scientific
story is progression, not revisionism.

### 22.9 R1-R6 provenance summary

| Claim | Scientific source | Tier |
|---|---|---|
| R1 breakpoint → real pause → stack | `experiments/debugger_interaction_v2_r1/runs/run-2-live-2026-08-11/evidence.json` | local_untracked_accepted (evidence.json on disk, SHA256-bound; commit `c842d69` tracked) |
| R2 multi-turn chain + diagnosis | `experiments/debugger_interaction_v2_r2/runs/run-r2-live-2026-08-11/evidence.json` | local_untracked_accepted (commit `97cc7fe` tracked) |
| R3 debugger-informed repair → verifier RESOLVED, B→C normalization | `experiments/debugger_interaction_v2_r3/runs/run-r3-2-live-2026-08-11/evidence-corrected.json` + `model_patch_serialization_normalized.patch` + `tests/fixtures/r31_model_patch_raw.patch` | local_untracked_accepted (run); frozen_in_repo (fixture; commit `f2291df`) |
| R4 generated test T fails buggy / passes fixed / verifier RESOLVED | `experiments/model_generated_test_probe_r4/runs/run-r4-1-live-2026-08-11/evidence.json` + `r4_contract.json` | local_untracked_accepted (run); frozen_in_repo (contract; commit `372d51f`) |
| R5.9 5/5 RESOLVED, 41 prompts, 0 findings | `experiments/debugger_interaction_v2_r5/runs/R5.9-MATRIX-14B-CLEAN-FINAL-2026-08-12/matrix.json` (local untracked run tree) + tracked `r5_contract_14b.json`, `experiments/debugger_interaction_v2_r5/` source, `tests/fixtures/old_r57_leakage/` | local_untracked_accepted (run); frozen_in_repo (contracts/source/fixtures; commits `e568b16`/`eeff17e`/`54828db`) |
| R5.7 disqualified / leakage regression | `tests/fixtures/old_r57_leakage/` + `tests/unit/test_r5_anti_leakage.py` | frozen_in_repo |
| R6 dataset/split/SFT facts | `experiments/r6_debugger_training/{split_manifest,build_summary,sft/sft_manifest}.json` + `training_provenance.json` | frozen_in_repo (commits `10b8028`, `4610785`) |
| R6 training run + losses | `experiments/r6_debugger_training/runs/r6-sft-debugger-v{1,2,3}/trainer/checkpoint-{10,20,30,40,48}/trainer_state.json` + `training_provenance.json` | local_untracked_accepted (run trees gitignored except frozen capsule); values carried by `runs/frozen/ancillary/checkpoint_selection.json` (frozen_in_repo) |
| R6 checkpoint-30 identity | `runs/frozen/ancillary/checkpoint_selection.json`, `docs/professor_traces/source_evidence_manifest.json` | frozen_in_repo (SHA256 verified on disk) |
| R6 8/8 validation aggregates | `runs/frozen/ancillary/stage_{a,b,c}_report.json`, `checkpoint_selection.json` | frozen_in_repo |
| R6 holdout partial rows + 18 prompts/0 findings | `runs/frozen/ancillary/holdout_report.json`, `runs/frozen/final_holdout_partial/*/evidence.json` | frozen_in_repo |
| Professor traces/audit/manifests | `docs/professor_traces/*` | frozen_in_repo (commit `c9afe37`; hashes verified) |

R5/R6 run trees contain large local raw evidence (gitignored by design); the
tracked frozen capsule and manifests are the clean-checkout carriers, and
the accepted aggregates are stated to the level the frozen records support.

---

## 23. Git milestone map (R1-R6) and reference reconciliation

### 23.1 Milestones

| Milestone | Commit | Date |
|---|---|---|
| R1 — real-model PDB breakpoint checkpoint | `c842d69` | 2026-08-11 |
| R2 — multi-turn real-model PDB debugging | `97cc7fe` | 2026-08-11 |
| R3 — debugger-informed real-model repair | `f2291df` | 2026-08-11 |
| R4 — model-generated regression test | `372d51f1a35e071c677391c9970f7b552bb276f2` | 2026-08-11 |
| R5.0-R5.8 — generalized matrix + treatment iterations | `f78d098`…`f8c112f` | 2026-08-11/12 |
| R5.9 clean-holdout treatment + anti-leakage | `e568b16`, `eeff17e` | 2026-08-12 |
| R5 reproducibility closeout | `54828db1d5dec4e95105f1c1d07ba5dd7518060c` | 2026-08-12 |
| R6 matched cp118 matrix harness | `31f8393` | 2026-08-12 |
| R6 SFT pipeline + trace exporter | `10b8028`, `e605aa1` | 2026-08-12 |
| R6 stable/physical-VRAM training + evaluator safety | `a162ccd`…`79c614d` | 2026-08-12 |
| R6 preserved implementation/evidence | `4610785713832daaba6aa133374506a2d200391a` | 2026-08-13 |
| Professor trace deliverable | `c9afe377db3f53229755532751b485fc2a13a4e7` | 2026-08-13 |
| Docs structure baseline | `34cce329b5e6e7cf42531d8e609774c7608b67cb` | 2026-08-13 |

### 23.2 Reference / path reconciliation

The DOCS-STRUCTURE-V1 reorganisation (`34cce32`) renamed many paths. Active
references in this report use current docs paths; the historical
`Agentic_Debugging_Project_Closeout_2026-08-11.md` is archived unchanged at
`docs/archive/status/project-closeout-2026-08-11.md` (git mv; internal
old-path strings are historical statements and were not rewritten). The old
root closeout path and `docs/FINAL_TECHNICAL_REPORT_V2.md` survive only in
frozen snapshots (historical intentional). Current entry points:
`docs/project-closeout.md` (current status), `docs/final-report.md` (this
report), `docs/project-tracker.md` (tracker), `README.md`, `TODO.md`,
`diary/diary.md`.