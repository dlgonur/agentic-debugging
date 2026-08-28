# Agentic Debugging Internship — Final Technical Report v2

**Date:** 2026-08-11
**Branch:** `docs/s8-final-report-diary-v1`
**Baseline HEAD:** `677992f` (S7 literature closeout)
**Author's role:** internship project — single-controller-agent architecture,
Python/PDB-first prototype

This report synthesizes the project's accepted evidence through S7 into a
final academic narrative. It supersedes `docs/archive/reports/final-report-v1.md` (the
2026-07-31 QuixBugs gold-baseline snapshot, preserved as a historical
document). Every material experimental claim traces to a frozen run artifact
or to the S5 canonical comparison; external literature claims cite the S7
closeout with evidence tiers marked.

No new model run, retraining, debugger experiment, RAG change, or web
research was performed for this report. S8 is synthesis of accepted evidence.

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
reaches the independent verifier. **The project did not achieve that positive
behavior** — neither the RAW nor the cp118 model condition produced a
successful non-error debugger observation or a debugger-informed patch
(Section 13).

The accepted final completion criterion permits scientifically honest closeout
with the bounded, well-instrumented negative result, provided professor
deliverables are reproducible and all claims are evidence-backed. The project
therefore closes out not on the positive trajectory — which did not occur —
but on an honest bounded-negative debugger result, a verifier-backed
evaluation platform, a definitive RAW-vs-cp118 transfer comparison, and a
literature-aligned architecture defense. The central contribution includes
infrastructure, evaluation methodology, and the bounded-negative debugger
result (Section 20).

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
(Sections 6–13). Question 5 is answered by the S7 literature closeout
(Sections 3, 17, 19).

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

A central distinction must be maintained: **SWE-rebench V2 is the
training/post-training dataset; QuixBugs and the curated fixtures are the
evaluation/experiment data.** The model was not trained on the QuixBugs
evaluation cohort.

### 5.1 Training data — SWE-rebench V2

The primary SFT dataset is **SWE-rebench V2**. The accepted filtered corpus
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
comparison, and the S4 RAG treatment. It is not part of the training split.

The five in-repo curated pytest fixtures (`agentic_debugger/datasets/curated/`)
are architecture smoke gates used by the demo, golden trajectories, and the
real-model debugger experiments (D1 used `curated-off-by-one-002`). They are
synthetic and small; they are not external-benchmark evidence and were never
used for training.

BugsInPy was selected as the primary external dataset by research merit but
remains **license-blocked** for execution (Section 18). SWE-bench Lite/
Verified was deferred for harness cost (Docker, ~120 GB storage, 16 GB RAM,
8 CPUs per the official guide). Defects4J is out of scope (Java/JVM, outside
the Python/PDB track).

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
  §2.5 and the tracked `Agentic_Debugging_Master_Execution_Plan_2026-08-10.md`
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

## 13. Real-model debugger experiments

Two real-model debugger experiments were run (S5 axis 4): D1 (RAW) and S2
(cp118), both on the same frozen D1 runtime-entry treatment using
`curated-off-by-one-002`.

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
This is a **bounded negative result**, not "no evidence." The infrastructure
can return debugger observations to the model, but real-model interpretation
of a successful debugger observation has not been demonstrated.

---

## 14. Professor TODO #23–25 assessment

The professor's requirements map to three debugger-capability questions. The
S5 coverage matrix
(`analysis/s5_final_controlled_comparison/s5_professor_todo_coverage_matrix.md`)
assesses each against accepted evidence with eight columns kept separate.

### Summary verdict

| TODO | Plumbing/backend | Deterministic evidence | Positive real-model success | Bounded negative result |
|---|---|---|---|---|
| #23 — Fine-tuned model generates debugger commands and interprets output | YES | YES | **NO** | YES |
| #24 — Breakpoint / variables / stack / step interaction | YES | YES | **NO** | YES |
| #25 — Debugger → patch → tests/verifier | YES | YES | **NO** | YES |

**Engineering capability:** all three TODOs have backend + deterministic
plumbing. The PDB backend supports the full grammar; deterministic/scripted
trajectories exercise it; the golden reachability capture records 2 successful
PDB observations.

**Positive real-model end-to-end: NOT achieved for any of #23/#24/#25.** No
real model interpreted a successful non-error PDB observation. No
real-model successful breakpoint→state→step/locals/stack sequence occurred.
No debugger-informed real-model patch reached the verifier.

**Bounded negative: present for all three.** Both D1 (RAW) and S2 (cp118)
each produced one model-authored PDB command and zero successful observations.

**Boundary clauses (apply to every row):**
- Deterministic PDB backend tests are not evidence the model used the
  debugger successfully.
- One accepted debugger command is not successful interaction when the backend
  observation was an error/rejection.
- The static QuixBugs gcd verifier RESOLVED does not demonstrate debugger use.
- "Engineering capability exists" must not be read as positive model-behavior
  evidence.
- No negative finding is converted into success.

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

### 17.1 Interpreting the negative debugger result

The project's two tested model policies (RAW and cp118) did not demonstrate
the learned interaction competence required to reach usable runtime evidence.
This outcome is consistent with the current literature (S7 §8):

- Runtime information is valuable, but stateful debugger use is a distinct
  agent capability shaped by model strength, interface abstraction, and
  trajectory-specific training.
- The strongest 2026 evidence (ADI, InspectCoder, Debug2Fix) finds that raw
  debugger exposure is not reliably beneficial; agent-oriented state
  abstractions and middleware-enforced legal transitions are more effective.
- Ordinary localized-repair SFT should not be assumed to produce debugger
  competence automatically (SWE-Gym, Open-SWE-Traces).

The RAW/cp118 result should be treated as **negative evidence about emergent
debugger interaction under the tested model/checkpoint/scaffold combination**,
not negative evidence about the value of debugging information itself.

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

The accepted closeout statement (S7 bottom line):

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

---

## 18. Limitations

1. **No real model successfully used the debugger.** Neither RAW nor cp118
   obtained a successful non-error PDB observation or completed an iterative
   debugger loop. Debugger effectiveness was not measured.

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

Ranked by evidence backing (S7 §9, S5 remaining-gaps §2):

1. **Debugger-specific trajectory post-training — highest priority.** SWE-Gym
   and Open-SWE-Traces show that actual agent trajectories can train
   tool-using behavior; NExT required explicit execution-state training. A
   debugger-specific trajectory corpus (breakpoint → observation →
   step/locals → updated diagnosis → patch) is the most direct path to the
   missing competence.

2. **Stronger base-model rerun under the identical controller — high
   priority.** The current base (Qwen2.5-Coder-7B) is a 7B-class model. A
   stronger base model under the same deterministic controller and typed-tool
   interface would test whether the interaction failure is model-capacity-
   dependent.

3. **Higher-level/state-aware debugger interface ablation — high priority.**
   ADI/FramePilot's agent-oriented dynamic interface outperformed
   conventional PDB. A state-aware interface abstraction (modeled legal
   transitions, middleware-enforced validity) on top of the existing PDB
   backend is directly evidence-backed.

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
harness, a licensed infra-validated external-dataset path (QuixBugs, eight
tasks, 100% gold-patch pass rate), a QLoRA SFT pipeline trained on SWE-rebench
V2, a definitive RAW-vs-cp118 executable-repair comparison, a controlled DPO
investigation, a partial cp118+RAG treatment, a canonical eight-axis
controlled comparison (S5), a professor-facing evidence presentation (S6),
and a focused literature closeout (S7).

The primary scientific findings are:

1. **Localized-repair QLoRA SFT did not transfer to executable repair on the
   held-out QuixBugs cohort.** cp118 produced 0/40 applicable patches and
   0/40 resolved tasks, vs RAW's 20/40 apply and 5/40 resolved. This is a
   formulation-specific negative transfer (output-policy degeneration,
   over-generation, scope explosion), not a general claim that fine-tuning is
   harmful.

2. **Neither RAW nor cp118 demonstrated a successful debugger loop.** Both
   authored one PDB command that ended in error/rejection with zero successful
   observations. This is a bounded negative result, consistent with the
   literature finding that stateful debugger use is a distinct agent
   capability not produced automatically by localized-repair SFT.

3. **DPO is not justified** given the available data (DPO 21/30 vs baseline
   27/30 and matched SFT 27/30).

4. **RAG treatment remains NOT_EVALUATED** (10/40 partial, compute-constrained,
   no success/failure claim).

5. **The single-agent + deterministic-controller + typed-tool architecture is
   a defensible controlled experimental baseline**, aligned with — not
   contradicted by — the strongest recent literature evidence.

The project's central contribution is **infrastructure, evaluation
methodology, and an honest bounded-negative experimental result**, not a
claim about debugging performance or PDB effectiveness. The negative debugger
result is itself scientifically informative: it demonstrates that the gap
between having a debugger backend and having a model that can use it is real,
and it points to the specific future directions (trajectory training,
state-aware interfaces, stronger base models) that the literature identifies
as most promising.

---

## 21. Provenance appendix

This appendix maps each load-bearing conclusion to its scientific source and
provenance tier, verified by direct Git audit (`git ls-files --error-unmatch`)
at HEAD `677992f`. Provenance tiers:

- `frozen_in_repo` — tracked in Git AND present in HEAD tree AND clean-checkout
  reproducible as a scientific source.
- `aggregate_external_per_task` — accepted aggregate in a tracked canonical
  artifact; per-task raw evidence is external (Drive-hosted D7 bundle), not
  in the repo.
- `master_plan_prose_only` — recorded only as narrative in a master execution
  plan; no frozen run-result file in the repo. The S5 master plan
  (`Agentic_Debugging_Master_Execution_Plan_2026-08-11_S5_CURRENT.md`) is
  **untracked**; the older `Agentic_Debugging_Master_Execution_Plan_2026-08-10.md`
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
| RAW Track B: 40/40 extracted, 20/40 apply, 5/40 resolved | untracked S5 master plan §2.5; tracked `Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` §2.5 | master_plan_prose_only | no (S5 plan); yes (2026-08-10 plan) | no (S5 plan); yes (2026-08-10 plan) | `s5_comparison_ledger.json` + `Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` | yes |
| cp118: 40/40 extracted, 0/40 apply, 0/40 resolved, 19/40 truncation | untracked S5 master plan §2.5; tracked 2026-08-10 master plan §2.5; per-task Drive-hosted D7 bundle | aggregate_external_per_task | aggregates yes (2026-08-10 plan + S5); per-task no | aggregates yes (2026-08-10 plan + S5); per-task no | `s5_comparison_ledger.json` + `Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` | yes |
| DPO: B1 27/30, SFT 27/30, DPO 21/30 | untracked S5 master plan §2.6; tracked 2026-08-10 master plan §2.6 | master_plan_prose_only | no (S5 plan); yes (2026-08-10 plan) | no (S5 plan); yes (2026-08-10 plan) | `s5_comparison_ledger.json` + `Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` | yes (values in both) |
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
| SWE-rebench V2: 1,594 eligible, 347 repos | `experiments/swe_rebench_v2_corpus/b14_package_v2/B14_PACKAGE_MANIFEST.json` | local_untracked_accepted | no | no | `Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` §2.3 (tracked) | yes |
| Split: 1000/150/444, overlap 0, seed 20260808 | `B14_PACKAGE_MANIFEST.json` → `frozen_b13_contract` | local_untracked_accepted | no | no | `Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` §2.3 (tracked) | yes |
| SFT formulation: localized repair, oracle-file-localized source → gold diff | untracked S5 master plan §2.3; local `B14_PACKAGE_MANIFEST.json` → `method` | master_plan_prose_only + local_untracked_accepted | no (S5 plan); no (manifest) | no (S5 plan); no (manifest) | `Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` §2.3 (tracked) | yes |
| cp118: step 118, eval_loss 0.45070546, adapter `65b5ed9a...` | `experiments/cp118_rag_definitive/s4_contract.json` | frozen_in_repo | yes | yes | — (source is tracked) | — |
| Qwen2.5 revision: `c03e6d35...` | untracked S5 master plan §2.4 | master_plan_prose_only | no | no | `s5_comparison_ledger.json` (line 47) + `Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` §2.4 (both tracked) | yes |
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
| `Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` | **yes** | frozen_in_repo (carrier for dataset/training/metric facts) |
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

*This report is synthesis of accepted project evidence through S7 (HEAD
`677992f`). No new experiments were run. See `docs/archive/reports/final-report-v1.md`
for the 2026-07-31 QuixBugs gold-baseline snapshot preserved as a historical
document.*