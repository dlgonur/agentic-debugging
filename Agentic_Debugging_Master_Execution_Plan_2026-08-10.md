# Agentic Debugging — Master Execution Plan

**Project:** Academic Agentic Debugging Internship Project  
**Owner:** Onur  
**Execution control:** Main FirstMate  
**Date:** 2026-08-10  
**Status:** ACTIVE  
**Current stage:** `S0 — Current branch/evidence closeout before Debugger Interaction v2`

---

# 0. How to Use This File

This file is the **shared execution map** for the project.

Every new coding-agent session, research chat, audit session, or major project handoff should read this file first.

It answers four questions:

1. **What is the project trying to achieve?**
2. **What has already been done?**
3. **What is still missing?**
4. **What exact stage are we currently executing?**

This file is an execution guide, not a replacement for frozen experimental evidence.

When this file and an accepted experiment artifact disagree, the experiment artifact / repository evidence wins and this file must be updated.

## Required operating rule

At the start of any new work session:

> Read this file first. Identify `CURRENT_STAGE`. Work only on the current authorized stage unless Main FirstMate explicitly changes it.

Do not reopen completed branches merely because another approach is possible.

Do not continue a failed engineering direction indefinitely. Every experiment must have a bounded acceptance gate and a STOP gate.

---

# 1. Project Goal

The project goal is to build and evaluate a **systematic agentic debugging system** in which a real code-capable LLM can:

1. reproduce or understand a software failure;
2. localize the relevant code;
3. form a root-cause hypothesis;
4. use debugging/runtime tools when useful;
5. inspect breakpoint, stack, locals and execution state;
6. update its diagnosis from runtime evidence;
7. produce a patch;
8. run independent verification;
9. report F2P, P2P and final RESOLVED status;
10. produce reproducible evidence suitable for the professor, technical report and final demo.

The final system must separate:

- model capability;
- retrieval;
- tool/interface behavior;
- controller behavior;
- debugger backend;
- patch serialization/application;
- executable verification.

The final project is not considered complete merely because the infrastructure exists.  
A **real model must successfully participate in the dynamic debugging loop**.

---

# 2. What Has Already Been Done

## 2.1 Research

Completed:

- classical debugging / fault localization / automated program repair review;
- LLM-based debugging review;
- SWE-Agent, OpenHands, AutoCodeRover, Agentless, ChatDBG and related system review;
- open-weight code-model landscape;
- debugging / repair dataset landscape;
- SWE-rebench V2 feasibility study;
- fine-tuning / post-training evidence study;
- debugger experiment bootstrap research.

Still incomplete:

- dedicated focused closeout on **multi-agent debugging / tool-using debugging / debugger-aware agentic systems**.

This remaining literature task is academic closeout work, not justification for building a multi-agent system.

---

## 2.2 RAW Model Evaluation

Frozen model cohort:

- Qwen2.5-Coder-7B-Instruct
- Seed-Coder-8B-Instruct
- Granite-4.1-8B
- Ministral-3-8B-Instruct
- Qwen3.5-9B

Accepted corrected C9 / Protocol v1.2.1 result:

| Model | Apply | RESOLVED |
|---|---:|---:|
| Qwen2.5-Coder-7B | 20/40 | 5/40 |
| Seed-Coder-8B | 11/40 | 2/40 |
| Granite-4.1-8B | 8/40 | 1/40 |
| Ministral-3-8B | 4/40 | 1/40 |
| Qwen3.5-9B | 9/40 | 5/40 |

Qwen2.5 was selected as the primary base because of the overall extraction/apply/truncation/compute tradeoff, not because it had a uniquely higher RESOLVED count.

The 40-task cohort is a controlled screening/evaluation cohort, not a universal model leaderboard.

---

## 2.3 Training Dataset

Primary SFT dataset:

**SWE-rebench V2**

Accepted filtered corpus:

- 1,594 eligible authentic Python repair tasks;
- 347 repositories.

Frozen split:

- 1,000 train tasks / 307 repos;
- 150 validation tasks / 40 repos;
- 444 unused;
- train/validation repository overlap = 0;
- protected evaluation repository overlap = 0.

Final no-truncation <=32K training view:

- 940 train;
- 135 validation.

Training formulation:

**Input**
- problem statement;
- oracle-file-localized exact pre-fix production source.

**Target**
- `PATCH`;
- exact stored gold repair diff.

The training does **not** teach end-to-end localization or debugger tool use.

Correct interpretation:

> localized repair / repair-after-localization SFT.

---

## 2.4 QLoRA

Base:

`Qwen/Qwen2.5-Coder-7B-Instruct`

Exact frozen revision:

`c03e6d358207e414f1eca0bb1891e29f1db0e242`

Training completed successfully.

Definitive surviving checkpoint:

**cp118**

Checkpoint-selection note:

- global best observed validation occurred near step 105 but was not saved;
- cp118 is the best surviving saved checkpoint under the original validation-only rule.

No new checkpoint sweep is currently justified.

---

## 2.5 RAW vs Tuned Result

Definitive comparison:

**RAW**
- 40/40 extracted;
- 20/40 applied;
- 5/40 RESOLVED.

**cp118**
- 40/40 extracted;
- 0/40 applied;
- 0/40 RESOLVED.

Important cp118 behavior:

- target file present 40/40;
- multi-diff 40/40;
- extra-file/scope violation 39/40;
- truncation 19/40;
- very large output expansion.

Accepted interpretation:

> Strong formulation-specific negative executable-repair transfer dominated by output-policy degeneration, over-generation, scope explosion and serialization mismatch.

Do **not** generalize this into:

> fine-tuning is bad.

---

## 2.6 Preference / DPO

Historical controlled DPO work exists.

Accepted historical result:

- B1 baseline: 27/30 RESOLVED;
- matched SFT: 27/30;
- DPO: 21/30.

Authentic preference closeout later found insufficient clean homogeneous data to justify a new DPO campaign.

Current decision:

**AUTHENTIC NEW DPO: CLOSED / NOT JUSTIFIED**

Do not reopen unless Main FirstMate explicitly authorizes it based on new evidence.

---

## 2.7 RAG

Implemented:

- deterministic bounded lexical repository retrieval;
- source/test/safe issue/failure provenance;
- answer-bearing/oracle exclusions;
- bounded context budgets;
- model-adapter integration;
- default OFF behavior.

Not yet completed:

**Definitive cp118 + frozen RAG treatment.**

No claim currently exists that RAG rescues cp118.

---

## 2.8 Agent / Tooling / Verifier

Implemented:

- controller/state machine;
- real-model transport;
- file reading;
- source/code search;
- reproduction/test execution;
- patch application;
- independent verifier;
- F2P;
- P2P;
- RESOLVED;
- evidence logging;
- replay;
- cleanup;
- root-cause assessment;
- comparison infrastructure;
- preference exporter.

A real-provider static QuixBugs path has reached verifier-confirmed RESOLVED.

Therefore:

> real model → patch → verifier is demonstrated for a static path.

This does **not** demonstrate debugger use.

---

## 2.9 PDB / Dynamic Debugger Infrastructure

Engineering backend supports:

- model-selectable breakpoint;
- continue;
- step;
- next;
- stack;
- frame locals;
- safe expression evaluation;
- cleanup;
- replay;
- bounded post-mortem behavior.

Deterministic/scripted PDB trajectories work.

Therefore:

> PDB engineering backend is demonstrated.

But:

> real-model PDB capability is NOT demonstrated.

---

## 2.10 Real-Model Debugger Status

Definitive current result:

### cp118 debugger pilot

- structured responses produced;
- some non-action transitions accepted;
- executable debugger action: 0;
- debugger exposure: 0.

### RAW same-base control

- strict JSON transport rejected responses;
- executable debugger action: 0;
- debugger exposure: 0.

Therefore:

**Debugger effectiveness has NOT been measured.**

The treatment never actually occurred.

Current strongest bottleneck:

> model-facing action/interface/protocol adoption.

Another important evidence gap:

> rejected RAW model text was not retained in the frozen transport.

Future experiments must retain full raw model responses.

---

# 3. What Is Still Missing

The project is not finished because the following remain open.

## Critical

1. A real model must actually enter the debugger loop.
2. A real model must consume runtime evidence.
3. A real model must produce a post-debug diagnosis.
4. A real model must produce a post-debug patch.
5. The patch must reach the verifier.
6. At least one real-model debugger trajectory must be usable as a final demo/evidence trace.

## Experimental

7. Test cp118 on the same debugger interface only after RAW feasibility is demonstrated.
8. Run the definitive frozen cp118 + RAG treatment.
9. Complete the final common comparison matrix.

## Academic / Deliverable

10. Close the remaining focused agentic/multi-agent literature gap.
11. Complete canonical metrics/cost/runtime table.
12. Build a minimal trace/demo UI from real evidence.
13. Complete technical report.
14. Complete internship diary.
15. Final reproducibility/Git/status closeout.

---

# 4. Work That Is Currently Closed

Do not spend project time on these unless Main FirstMate explicitly reopens them.

- new general QLoRA run;
- checkpoint sweep;
- new base-model zoo;
- authentic DPO training;
- new primary training dataset;
- new RAG architecture;
- new PDB backend;
- GDB/LLDB expansion;
- new controller/state machine;
- multi-agent implementation;
- broad new benchmark campaign;
- open-ended protocol hardening.

Negative results do not automatically reopen these branches.

---

# 5. Execution Roles

## Main FirstMate

Responsible for:

- current-state authority;
- choosing the next stage;
- experiment design;
- PLAN review;
- acceptance/rejection;
- STOP decisions;
- reconciliation of evidence;
- updating this execution plan.

No major campaign moves forward without Main FirstMate approval.

---

## Coding Agent

Use for:

- live local repository inspection;
- source changes;
- tests;
- experiment runners;
- local execution;
- local training if explicitly authorized;
- Git-state evidence.

Standard workflow:

`READ PLAN -> PLAN MODE -> FirstMate REVIEW -> BUILD -> TEST/EVIDENCE -> FirstMate ACCEPT/REJECT`

The coding agent does not choose a new project direction on its own.

---

## Research Chat

Use for:

- current literature;
- web research;
- external model/dataset/tool evidence;
- focused academic comparison.

The research chat receives this plan first and is told the exact current stage and research question.

Research chats do not modify the repository.

---

## Direct File Review by Main FirstMate

Use when:

- one or several concrete files must be interpreted;
- live repository context is not required.

Owner uploads the requested file(s) directly.

---

# 6. Stage System

Only one primary execution stage is active at a time.

Parallel documentation/research work may be explicitly marked as `PARALLEL`.

When a stage is accepted:

1. mark it `DONE`;
2. record the result;
3. change `CURRENT_STAGE`;
4. do not keep working on the old stage unless a later dependency explicitly requires it.

---

# S0 — Current Branch / Evidence Closeout

**Status:** CURRENT

## Objective

Close and freeze the current tuned-debugger-pilot / RAW-control branch state before starting the next experimental campaign.

## Required work

- verify live Git branch, HEAD and working tree;
- inspect current branch changes;
- verify existing tuned pilot and RAW-control evidence;
- confirm frozen artifact paths/hashes;
- run the relevant bounded preflight/tests;
- determine what belongs in accepted repository history;
- reconcile only necessary stale state/docs;
- preserve historical v1 experiment behavior unchanged.

## Must NOT happen

- debugger v2 implementation;
- new model run;
- refactor;
- historical result rewrite.

## Exit gate

S0 is DONE when:

- current work is provenance-clean;
- accepted evidence is frozen;
- Git baseline for the next experiment is unambiguous;
- Main FirstMate explicitly authorizes `S1`.

---

# S1 — Debugger Interaction v2: RAW Feasibility

**Status:** NEXT

## Objective

Answer one question:

> Can frozen RAW Qwen2.5 use a small, natural, bounded debugger-facing interface to execute a real PDB interaction?

This is the highest-priority blocker.

## Model

Frozen RAW:

`Qwen/Qwen2.5-Coder-7B-Instruct`

Exact project revision remains fixed.

## Initial task

`curated-off-by-one-002`

Reason:

- deterministic;
- debugger-friendly;
- execution can continue after breakpoint;
- suitable for stack/locals/step/next behavior;
- useful as interface feasibility, not benchmark evidence.

## Frozen variables

- model;
- task;
- PDB backend;
- controller;
- verifier;
- RAG OFF;
- deterministic decoding where supported;
- existing internal typed actions.

## Changed variable

**Model-facing interaction format only.**

The implementation should reuse the existing typed-action controller internally.

The model-facing surface should be much smaller and more natural than the previous strict custom JSON contract.

Illustrative command family:

- reproduce;
- hypothesis;
- break;
- where;
- locals;
- print;
- next;
- step;
- continue;
- patch;
- validate;
- stop.

Exact syntax is an implementation-design question for the coding-agent PLAN and Main FirstMate review.

## Bridge rule

Any model-facing command bridge must be:

- deterministic;
- syntactic;
- fail-closed;
- bounded;
- non-oracular.

It must not infer the correct debugging action for the model.

## Required evidence

Retain:

- full raw model response;
- normalized response;
- parsed command;
- translated internal directive;
- controller accept/reject;
- rejection reason;
- actual debugger command;
- breakpoint;
- stack;
- locals;
- safe expression;
- step/next/continue evidence;
- post-debug model response;
- post-debug diagnosis;
- patch;
- apply;
- F2P;
- P2P;
- RESOLVED;
- latency;
- tokens if available;
- context size;
- cleanup/replay evidence.

## Gate A — Engineering

Must prove:

- old frozen v1 behavior remains unchanged;
- parser/bridge is deterministic;
- backend tests pass;
- raw-response telemetry works;
- no semantic oracle is introduced.

## Gate B — Interface feasibility

Minimum success:

> At least one model-generated debugger command is accepted and results in a real PDB observation returned to the model.

Preferred trajectory:

`break -> stack/locals -> step/next/continue -> updated diagnosis`

## Gate C — Full dynamic demo feasibility

Preferred success:

`debugger observation -> diagnosis -> patch -> verifier`

A RESOLVED patch is ideal.

A wrong patch does not erase successful debugger treatment exposure; semantic repair quality is measured separately.

## Repair budget

At most:

- initial implementation/run;
- two material deterministic repair passes.

Repairs must address demonstrated engineering defects.

Do not keep widening the interface because the model refuses to use it.

## STOP gate

If the deterministic bridge is correct but bounded attempts still produce:

- zero accepted debugger action, or
- zero debugger exposure,

STOP interface engineering.

Run only one bounded harness/interface sanity diagnostic.

Then record the negative result rather than opening an indefinite protocol-hardening campaign.

## Exit

If Gate B passes:

move to `S2`.

If Gate B fails after STOP:

Main FirstMate performs a project-direction decision before any new engineering.

---

# S2 — cp118 on the Frozen Working Debugger Interface

**Status:** BLOCKED BY S1

## Objective

Test the definitive tuned checkpoint under the exact debugger interface demonstrated to work with RAW.

## Critical rule

Do not change the interface between RAW and cp118.

Otherwise the comparison becomes confounded.

## Questions

1. Can cp118 enter the debugger loop?
2. Can it consume runtime evidence?
3. Can it update diagnosis?
4. Can it produce a debugger-informed patch?
5. How does its behavior differ from RAW?

## Important interpretations

### RAW works + cp118 works

Proceed to small matched debugger comparison.

### RAW works + cp118 fails

Strong evidence that the existing PATCH-only adaptation impaired interactive behavior under a working interface.

Do not immediately retrain.

Move to the post-training decision gate.

### Both work

Measure debugger treatment behavior and continue toward final comparison/demo.

## Exit

Evidence is frozen and Main FirstMate decides whether `S3` is necessary.

---

# S3 — Conditional Debugger-Oriented Post-Training Decision

**Status:** CLOSED UNLESS TRIGGERED

This stage is not automatically executed.

## It opens only if all are true

1. RAW has demonstrated a working debugger trajectory.
2. cp118 fails or materially underperforms on the same working interface.
3. The professor deliverable requires a tuned model to demonstrate debugger/tool interaction strongly enough that the existing result is insufficient.

If those conditions are not met:

**skip S3.**

---

## S3A — Focused Research

Open a dedicated ChatGPT research chat.

Question:

> What supervision and post-training methods are supported by current evidence for teaching debugger/tool-use trajectories to code LLMs, and what is the minimum contamination-safe experiment appropriate for this project?

Research focus:

- tool-use trajectory SFT;
- debugger/runtime-state supervision;
- execution-grounded post-training;
- agentic code-model post-training;
- data construction;
- evaluation contamination;
- smallest viable treatment.

No generic fine-tuning landscape repeat.

---

## S3B — Training Feasibility Decision

Only if research supports it.

Define:

- training objective;
- clean training data;
- frozen eval;
- local GPU feasibility;
- maximum training budget;
- acceptance gate;
- STOP gate.

Prefer local execution when technically sensible.

Do not overwrite cp118.

Any new checkpoint becomes a separate experimental condition.

---

# S4 — Definitive cp118 + Frozen RAG Treatment

**Status:** OPEN, LOWER PRIORITY THAN S1/S2

## Objective

Complete the missing tuned+RAG experimental condition.

## Frozen

- cp118;
- RAG architecture;
- retrieval policy;
- provenance exclusions;
- context budget;
- evaluation cohort.

## Not allowed

- modifying RAG until it improves performance;
- new retrieval architecture;
- new embedding campaign unless a separately justified experiment explicitly requires it.

## Result interpretation

Positive, neutral or negative results are all acceptable.

The purpose is measurement, not rescue.

## Exit

Frozen cp118+RAG results enter the common comparison schema.

---

# S5 — Final Controlled Comparison

**Status:** BLOCKED BY REQUIRED TREATMENTS

## Primary conditions

A. RAW base / accepted frozen baseline  
B. cp118 / accepted frozen tuned result  
C. cp118 + frozen RAG  
D. agentic debugger condition

Where scientifically possible, reuse accepted frozen generations rather than rerunning them.

## Metrics

At minimum:

- strict extraction;
- semantic extraction;
- file localization;
- symbol localization;
- root-cause correctness;
- patch apply;
- F2P;
- P2P;
- RESOLVED;
- debugger exposure;
- accepted debugger actions;
- debugger turns;
- debugger-informed patch;
- truncation;
- output length;
- runtime;
- latency;
- tokens;
- VRAM;
- monetary cost when applicable.

Unknown historical values are:

`NOT_RECORDED`

not zero.

## Exit

One canonical comparison table/report with provenance to every treatment.

---

# S6 — Real-Model Dynamic Debugging Demo

**Status:** BLOCKED BY REAL DEBUGGER TRAJECTORY

## Objective

Build the smallest professor-facing demo that shows the real debugging loop.

Required visual/logical sequence:

`Failure -> Model hypothesis -> Breakpoint -> Runtime state -> Updated diagnosis -> Patch -> Verifier`

Preferred debugger evidence:

- breakpoint;
- stack;
- locals;
- step/next/continue.

## UI principle

Minimal.

Do not build a large frontend before a real trajectory exists.

The UI should read real frozen experiment evidence rather than replay a fake scripted success as if it were model behavior.

## Exit

Demo can be reproduced from accepted evidence and clearly distinguishes model decisions from deterministic tooling.

---

# S7 — Focused Literature Closeout

**Status:** PARALLEL

## Objective

Close the remaining academic literature gap without opening another implementation branch.

Dedicated research question:

> What does the current literature show about tool-using, runtime-aware and multi-agent debugging systems, and does it support this project's single-agent + deterministic-controller design?

Include:

- debugger-aware agents;
- runtime-state reasoning;
- tool-use coding agents;
- multi-agent debugging;
- tool/trajectory post-training where relevant;
- closest successors/alternatives to ChatDBG.

Output:

- concise evidence review;
- citations;
- explicit implications for this project's architecture;
- explicit statement on why multi-agent implementation is or is not justified.

---

# S8 — Technical Report + Internship Diary

**Status:** PARALLEL / FINALIZE AFTER EXPERIMENTS

Do not postpone evidence organization until the last day.

After each accepted stage record:

- question;
- setup;
- frozen variables;
- result;
- interpretation;
- limitations;
- artifact path.

Final report should cover:

1. problem and goals;
2. literature;
3. model landscape;
4. dataset selection;
5. RAW baseline;
6. QLoRA;
7. RAW-vs-cp118 result;
8. preference/DPO;
9. RAG;
10. agent architecture;
11. debugger architecture;
12. interface failure;
13. Debugger Interaction v2;
14. final comparison;
15. demo;
16. limitations and future work.

Do not convert negative findings into success claims.

---

# S9 — Final Reproducibility / Git / Project Closeout

**Status:** FINAL

## Required final checks

- accepted branches reconciled;
- `main` clean;
- stale status documents corrected;
- exact model revisions recorded;
- dataset split hashes/identities recorded;
- frozen artifact hashes recorded;
- experiment commands reproducible;
- evidence paths valid;
- focused tests pass;
- full relevant test suite passes;
- demo works from a clean expected environment;
- final TODO reflects actual status;
- technical report claims map to evidence;
- internship diary complete.

## Final completion gate

The project is complete when:

1. accepted scientific questions have explicit results;
2. a real-model dynamic debugger trajectory exists or a bounded, well-instrumented negative result has been honestly established;
3. required professor deliverables are reproducible;
4. no major report claim depends on missing/unverifiable evidence;
5. repository/project state is clean and understandable to a fresh reviewer.

---

# 7. Planned Order

Primary path:

```text
S0
 ↓
S1
 ↓
S2
 ↓
S3 only if triggered
 ↓
S4
 ↓
S5
 ↓
S6
 ↓
S9
```

Parallel:

```text
S7 — literature closeout
S8 — evidence ledger / report / diary
```

S7 and S8 must not block the critical debugger experiment unless a specific evidence question requires them.

---

# 8. Current Execution Position

## CURRENT_STAGE

`S0 — Current Branch / Evidence Closeout`

## NEXT_STAGE

`S1 — Debugger Interaction v2: RAW Feasibility`

## Current Main FirstMate instruction

Do not start Debugger Interaction v2 implementation yet.

First:

1. close the existing branch/evidence state;
2. establish the clean baseline;
3. review the coding-agent PLAN;
4. only then authorize BUILD.

---

# 9. Stage Update Template

After any accepted stage, Main FirstMate updates this section.

```text
STAGE:
STATUS: DONE / STOPPED / BLOCKED

QUESTION:

RESULT:

EVIDENCE:

INTERPRETATION:

WHAT DID NOT CHANGE:

NEXT_STAGE:

REASON:
```

This prevents the project from drifting into an undefined state.

---

# 10. One-Line Project Status

> The project has completed its research, RAW baseline, authentic localized-repair QLoRA, RAW-vs-tuned comparison, preference closeout, RAG infrastructure, agent/controller/verifier and PDB backend; the current blocker is proving a real-model debugger interaction through a simpler bounded model-facing interface, after which cp118, RAG, final comparison, demo and report closeout can be completed.
