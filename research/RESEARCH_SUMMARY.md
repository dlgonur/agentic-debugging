# Research Synthesis — Agentic Debugging Project

**Purpose:** Consolidated research summary for the `research/` directory.  
**Scope:** Literature notes, local paper archive, multi-model research reports, reviewed syntheses, QuixBugs/BugsInPy research records, and the empirical findings produced by this project itself.

This document is a synthesis and navigation aid. It does **not** replace the canonical scientific evidence in `docs/`, `analysis/`, or `experiments/`. Raw AI-generated research reports under `research/reports/raw/` are treated as discovery material only; reviewed notes, syntheses, primary papers, manifests, and project evidence take precedence.

---

# 1. Executive conclusion

The central research question of the project became:

> **Can a software-repair agent use controlled runtime/debugger evidence to localize, explain, and repair bugs more reliably than a strong static/test-feedback baseline?**

The research archive supports five broad conclusions.

## 1.1 Runtime evidence is genuinely useful, but debugger access alone is not enough

The literature consistently supports the value of execution information. ChatDBG showed that an LLM can actively inspect a conventional debugger. LDB and NExT showed that intermediate execution state can improve debugging and program repair. Later work such as InspectCoder, ADI/FramePilot, Debug2Fix, and related runtime-aware systems strengthened the same direction.

However, the literature also shows that simply exposing PDB/GDB-like commands does not guarantee better repair. Stateful debugger use requires:

- correct command sequencing;
- awareness of session state;
- useful breakpoint selection;
- interpretation of stack/locals/runtime values;
- recovery from invalid actions;
- integration of observed evidence into a causal hypothesis and patch.

This distinction became one of the most important findings of the project.

## 1.2 Interface design and deterministic orchestration matter

The strongest architecture lesson across SWE-agent, RepairAgent, OpenHands, Agentless, debug-gym, AutoCodeRover, InspectCoder, and ADI/FramePilot is that the model should not be forced to control an unconstrained computer interface.

A strong software-engineering agent benefits from:

- a small action vocabulary;
- typed tools;
- concise observations;
- deterministic error handling;
- legal state transitions;
- explicit budgets;
- reproducible runtime isolation;
- patch/test verification independent of the model.

This directly supports the project's single-agent + deterministic-controller + typed-tool design.

## 1.3 Patch success is not equivalent to debugging success

The literature repeatedly distinguishes:

- suspicious-location prediction from root-cause analysis;
- plausible patches from correct patches;
- passing visible tests from general correctness;
- static repair from debugger-grounded repair.

The project therefore adopted an independent verifier as the correctness authority. A patch being produced or applied is never itself a success condition.

## 1.4 Debugger competence is a learned/interaction-specific capability

Ordinary localized code-repair fine-tuning should not be assumed to produce stateful debugger competence automatically.

NExT, SWE-Gym, Open-SWE-Traces, REval, InspectCoder, and the project's own results all point in the same direction: execution reasoning, multi-step action sequencing, and harness-specific tool use are distinct capabilities.

The project's early cp118 experience was consistent with this. A checkpoint trained for localized repair did not automatically become a competent PDB user. Later debugger-trajectory-oriented R6 training produced a much stronger result, although the project does **not** claim a causal fine-tuning improvement because no matched-base ablation was completed.

## 1.5 Multi-agent orchestration is credible future work, not a required redesign

Recent multi-agent SWE systems provide real evidence that specialization and coordination can help. BOAD is especially important because it attempts to control total-token usage; InspectCoder and Debug2Fix also provide a direct motivation for separating debugging/inspection from coding.

But the evidence does not establish that every debugging system should be multi-agent. Multi-agent results often mix together:

- additional model calls;
- more total reasoning;
- better tools;
- specialized prompts;
- broader context;
- explicit verification;
- parallel sampling.

For this project's goal — measuring model behavior under a controlled debugger interface — a single policy behind a deterministic controller improves scientific attribution. Multi-agent debugging remains a useful later ablation, not a missing prerequisite.

---

# 2. How the research process evolved

The research process had three distinct stages.

## 2.1 Phase 1 — Broad discovery with multiple research reports

Three independent long-form AI-assisted research reports were collected:

- `reports/raw/Gemini_Agentic_Debugging_Literature_Review.pdf`
- `reports/raw/ChatGPT_Agentic_Debugging_Literature_Review.pdf`
- `reports/raw/Claude_Agentic_Debugging_Systems_Deep_Research_Literature_Review.pdf`

These reports were **not** treated as authoritative scientific evidence. Their role was source discovery, taxonomy building, and disagreement detection.

The reviewed cross-report synthesis found strong agreement on several points:

- ChatDBG was the closest direct prior art.
- Mainstream SWE agents were mostly repository/test repair agents, not true debugger agents.
- Fault localization is not the same as root-cause analysis.
- Passing tests does not automatically prove semantic correctness.
- Python + PDB was the lowest-risk first debugger target.
- Agentless-style static repair was a strong baseline.
- Multi-agent orchestration and fine-tuning should be deferred until a working baseline existed.

The reports differed most on:

- whether fine-tuning was required early;
- how strongly to favor multi-agent architectures;
- how confidently to treat very recent frontier systems;
- exact metadata/benchmark claims.

The project resolved these disagreements by moving from report-level claims to local primary papers and manual notes.

Key reviewed files:

- `reports/synthesis/phase1_cross_report_synthesis_v1.md`
- `reports/synthesis/source_consensus_matrix_v1.md`
- `reports/synthesis/claims_to_verify_v1.md`
- `reports/synthesis/pdf_download_manifest_clean_v1.md`
- `reports/synthesis/project_next_steps_v1.md`
- `reports/synthesis/diary_day_02_draft.md`

## 2.2 Phase 2 — Primary-paper reading and manual notes

The first local paper library was split into two reading tiers.

### Tier 1 — must read

1. **ChatDBG**
2. **debug-gym**
3. **Agentless**
4. **SWE-bench**

These papers answered the project's most important initial questions:

- Can an LLM use a debugger at all?
- Does giving a model tools automatically help?
- What does a strong static baseline look like?
- How should repository repair be evaluated?

### Tier 2 — architecture and runtime reasoning

1. **LDB / Debug Like a Human**
2. **RepairAgent**
3. **SWE-agent / Agent-Computer Interfaces**
4. **AutoCodeRover**
5. **OpenHands**

These papers refined the implementation architecture:

- runtime state should be structured rather than dumped raw;
- tool availability should be constrained by state;
- the model/runtime boundary should be explicit;
- source retrieval should use symbols/structure where possible;
- actions and observations should be logged;
- patch application and test execution should be deterministic.

Primary PDFs are stored locally under `research/papers/` and are intentionally not relied upon as Git-tracked evidence. Manual notes preserve the project-relevant conclusions.

## 2.3 Phase 3 — 2026 literature closeout

The final literature closeout expanded the review beyond the original Tier 1/Tier 2 set and explicitly revisited the project's architecture after the real debugger experiments.

The closeout examined approximately twenty works and addressed:

- debugger-aware systems;
- runtime evidence;
- tool-using SWE agents;
- multi-agent debugging;
- trajectory/tool-use post-training;
- debugger-interface abstraction;
- what the project's negative and positive results actually mean.

Canonical file:

`research/literature/agentic_debugging_literature_closeout_2026-08-11.md`

Its main conclusion was that the project's architecture was academically defensible and that the literature did **not** require a multi-agent redesign.

---

# 3. Core concepts established by the research

The early `literature_notes_01.md` established the conceptual boundaries used throughout the project.

## Debugging

Finding, understanding, and correcting the cause of incorrect program behavior.

## Automated debugging

Automating portions of the debugging process, such as localization, test analysis, root-cause inference, evidence collection, or repair.

## Fault localization

Estimating where a defect is likely to be.

Fault localization may identify a suspicious file, method, line, or statement, but it does not necessarily explain **why** the observed behavior occurs.

## Root-cause analysis

Producing a causal explanation grounded in program behavior, state, control flow, data flow, or counterfactual evidence.

The project's scientific interest was closer to root-cause-grounded repair than to simple localization.

## Automated program repair

Generating candidate patches and validating them against some behavioral oracle, usually tests.

A central warning from APR literature is that a patch can be **plausible** under the available tests while still being semantically wrong.

---

# 4. What each core paper contributed

# 4.1 ChatDBG — direct proof that LLMs can interact with real debuggers

**Role in the project:** closest direct prior art.

ChatDBG integrates LLM reasoning with real debugger state through Pdb/GDB/LLDB. Its major contribution is not merely showing an enriched stack trace; the model can "take the wheel" and request additional debugger observations.

Project-relevant lessons:

- runtime state can supply evidence unavailable from static source alone;
- stack traces, frames, locals, source, and debugger queries should be model-visible;
- Python/PDB is a practical first target;
- multi-turn interaction matters;
- model-generated debugger commands require safety controls;
- debugger evidence should support root-cause reasoning rather than become an end in itself.

The manual notes record strong Python results in the original study, but also important limitations:

- assistant/human-in-the-loop setting;
- small/student Python programs rather than repository-scale issue repair;
- no independent patch verifier;
- root-cause quality partly depends on manual judgment.

**Project takeaway:** ChatDBG justified the direction, but the project needed to add autonomous patching, deterministic control, reproducible evaluation, and independent verification.

---

# 4.2 debug-gym — interactive debugging is an environment-design problem

**Role in the project:** closest environment-level reference for text-based PDB agents.

debug-gym frames debugging as repeated text action -> text observation interaction. It provides tools such as evaluation, source view, PDB, rewriting, and directory listing inside a controlled environment.

Its most important lesson for this project is that **PDB availability does not automatically improve every model**. Some models can use debugger state productively; weaker agents may waste actions or reduce performance.

Project-relevant lessons:

- PDB should be a first-class but controlled tool;
- debugger access can be delayed/gated rather than always-on;
- tool budgets matter;
- traces can become future SFT data;
- sandbox/read-only boundaries are important;
- test success alone does not guarantee code quality;
- baseline comparisons should include simpler rewrite/static conditions.

**Project takeaway:** the correct experiment is not "does PDB exist?" but "under what policy, model, and interface does runtime evidence improve repair?"

---

# 4.3 Agentless — the static baseline that complex agents must beat

**Role in the project:** methodological baseline.

Agentless deliberately avoids an open-ended tool-use loop. Its staged pipeline is approximately:

`localization -> repair -> validation`

Key reusable ideas:

- hierarchical localization;
- file/class/function skeletons;
- compact source context;
- deterministic Search/Replace edits;
- multiple patch candidates;
- reproduction-test generation;
- regression-test filtering;
- low-cost fixed control flow.

The deeper lesson is architectural: agentic complexity must earn its cost. A debugger system is scientifically interesting only if runtime evidence adds value beyond a strong static/test-feedback pipeline.

**Project takeaway:** do not compare PDB-assisted repair only against a weak one-shot model.

---

# 4.4 SWE-bench — evaluation discipline rather than the first debugger benchmark

**Role in the project:** verifier/evaluation foundation.

SWE-bench established a repository-level evaluation language built around:

- base-commit checkout;
- patch application;
- fail-to-pass tests;
- pass-to-pass tests;
- execution-based verification;
- task/environment reproducibility.

The project reused this philosophy even when using smaller curated tasks and QuixBugs.

Important limitation for this project:

SWE-bench is not inherently a debugger benchmark. A model can solve it without using runtime-state inspection. It was therefore too large and too confounded for the first PDB MVP.

**Project takeaway:** borrow the verification protocol, not necessarily the full benchmark as the first experimental target.

---

# 4.5 LDB — runtime state can improve reasoning without a real debugger

LDB instruments execution, decomposes programs, records intermediate values, and asks the model to judge execution step-by-step.

It shows that:

- concrete intermediate state can outperform pure self-reflection;
- observation granularity matters;
- runtime reasoning should be iterative but budgeted;
- execution evidence is especially useful for semantic bugs.

LDB is not a repository-scale interactive PDB system, but it strengthened the core scientific rationale for showing structured state to the model.

**Project takeaway:** runtime values should be a compact evidence channel, not an uncontrolled trace dump.

---

# 4.6 RepairAgent — state machines, tool gating, and explicit hypotheses

RepairAgent treats repair as an autonomous tool-use loop but constrains the process through a finite state machine.

Relevant ideas adopted or echoed by the project:

- different tools become legal in different phases;
- maintain gathered information across turns;
- explicitly express or discard hypotheses;
- detect repeated/invalid tool calls;
- apply and revert patches deterministically;
- bound total repair cycles;
- distinguish plausible from correct patches.

**Project takeaway:** debugger tools should be gated by state and mediated by the controller.

---

# 4.7 SWE-agent — Agent-Computer Interface design is part of model capability

SWE-agent's central argument is that model performance depends heavily on the interface through which the model interacts with the environment.

A good Agent-Computer Interface should provide:

- simple actions;
- compact feedback;
- guardrails;
- bounded file views;
- clear edit semantics;
- concise search results;
- immediate post-edit feedback.

For debugging this translates directly to:

- do not expose raw PDB terminal semantics if a smaller typed action is sufficient;
- return bounded stack/locals/source observations;
- tell the model when an action is illegal and why;
- preserve a stable interaction protocol across model conditions.

**Project takeaway:** a bad debugger interface can make a capable model look incapable.

---

# 4.8 AutoCodeRover — structure-aware retrieval before runtime inspection

AutoCodeRover uses program structure and semantic retrieval APIs rather than treating a repository as undifferentiated text.

Relevant ideas:

- AST/symbol-level retrieval;
- class/method search;
- stratified context collection;
- suspicious-location hints;
- compact source windows;
- failure taxonomy separating localization from repair.

**Project takeaway:** runtime evidence should complement good static localization. PDB should not be used as a substitute for finding the relevant code region.

---

# 4.9 OpenHands — runtime separation, event streams, and reproducibility

OpenHands contributed platform-level architecture ideas:

- agent/runtime separation;
- action -> observation events;
- sandbox/runtime abstraction;
- reusable skills/tools;
- trajectory logging;
- cost/usage tracking;
- integration tests;
- optional future human feedback/multi-agent extensions.

The project deliberately adopted the small subset relevant to debugging rather than recreating a full generalist software-agent platform.

**Project takeaway:** the debugger system should be a reproducible experimental platform, not a monolithic script.

---

# 5. 2025–2026 literature updates and what they changed

The final closeout added several important conclusions not present in the first reading block.

## 5.1 InspectCoder

InspectCoder reports that direct stateful debugger interaction can create:

- invalid commands;
- corrupted sessions;
- repeated error loops.

Its solution uses middleware/state tracking to enforce legal debugger behavior.

**Relevance:** strong independent support for the project's deterministic stateful controller.

## 5.2 ADI / FramePilot

The closeout identifies ADI/FramePilot as probably the strongest direct evidence that **interface abstraction matters more than simply attaching PDB**.

The reported ablations show that conventional debugger access can be almost neutral or even harmful for some models, while an agent-oriented semantic interface performs substantially better.

**Relevance:** future work should compare primitive PDB actions against higher-level state-aware debugger actions rather than assuming human debugger commands are the ideal model interface.

## 5.3 Debug2Fix

Debug2Fix supports debugger specialization but also illustrates an attribution problem: a specialized debugger context, new tools, different prompts, and extra reasoning may all change together.

**Relevance:** debugger specialization is promising, but experiments should separate the effect of role decomposition from extra compute and better interfaces.

## 5.4 PatchPilot

PatchPilot provides peer-reviewed support for rule-based orchestration around LLM patching:

`reproduction -> localization -> generation -> validation -> refinement`

**Relevance:** deterministic workflow structure is not an anti-agent design; it is a legitimate way to improve stability, cost control, and reproducibility.

## 5.5 NExT

NExT explicitly trains models to reason from execution traces.

**Relevance:** runtime reasoning is trainable and should not be expected to emerge automatically from ordinary code SFT.

## 5.6 REval

REval shows that models can be weak and inconsistent at reasoning about runtime behavior even before a stateful debugger protocol is introduced.

**Relevance:** debugger-tool failure may reflect execution-reasoning capability, not only tool syntax.

## 5.7 SWE-Gym

SWE-Gym trains software-engineering agents from executable trajectories and demonstrates substantial gains.

**Relevance:** long-horizon tool behavior itself is a trainable distribution.

## 5.8 Open-SWE-Traces

Open-SWE-Traces provides particularly important evidence that models adapt to specific harness action spaces and observation formats; cross-harness transfer degrades.

**Relevance:** debugger training data should use the same typed action vocabulary and observation schema as the target controller whenever possible.

## 5.9 SWE-Master / SWE-TRACE

These newer systems combine trajectory SFT, process supervision, or execution-feedback reinforcement learning.

**Relevance:** promising second-stage future work after trajectory imitation, but much of this evidence is newer/preprint/technical-report level and not debugger-specific.

---

# 6. Multi-agent research: what is supported and what is not

The final closeout reviewed BOAD, DeLM, AgentForge, SGAgent, InspectCoder, Debug2Fix, and other multi-agent/specialized systems.

## What is supported

- role specialization can be useful;
- multiple agents can broaden exploration;
- learned coordination can outperform a single-agent baseline in some controlled settings;
- inspector/debugger vs coder separation is a plausible decomposition.

## What is not established

The literature does not justify the universal statement:

> "A debugging system should be multi-agent."

Many headline improvements are confounded by:

- increased token budget;
- more attempts;
- different prompts;
- different tools;
- parallel search;
- extra verifier/reviewer stages.

BOAD is the strongest counterexample because it more carefully controls token use, yet even BOAD shows that manually adding more sub-agents is not monotonically beneficial.

**Project conclusion:** single-agent + deterministic controller remained the cleaner experimental architecture. Multi-agent debugger specialization is a later controlled ablation.

---

# 7. Architecture derived from the literature

The Tier 1 and Tier 2 syntheses converged on the following system:

```text
Model / policy
  -> typed action
Deterministic controller/state machine
  -> validated tool call
Sandboxed/disposable runtime
  -> structured observation
Event/history layer
  -> next model action
Patch manager
  -> candidate patch
Independent verifier
  -> final correctness outcome
```

The corresponding debugging flow became:

```text
REPRODUCE
  -> UNDERSTAND
  -> RUNTIME_EVIDENCE (when needed)
  -> PATCH
  -> VALIDATE
  -> DONE / FAIL
```

## Responsibility boundaries

### Model

Responsible for:

- forming hypotheses;
- prioritizing evidence;
- deciding which legal observation to request;
- interpreting runtime state;
- drafting patches;
- explaining root cause.

### Controller

Responsible for:

- state transitions;
- legal-action enforcement;
- budgets;
- retry/stop policy;
- output bounds;
- safety;
- ensuring comparable experimental conditions.

### Runtime/tools

Responsible for deterministic operations such as:

- source retrieval;
- search;
- test execution;
- PDB session control;
- stack/locals extraction;
- safe expression inspection;
- patch application;
- cleanup.

### Verifier

The sole correctness authority.

The verifier decides whether:

- the failure was actually fixed;
- fail-to-pass tests pass;
- pass-to-pass behavior is preserved;
- the outcome is RESOLVED, unresolved, breaking, or otherwise classified.

---

# 8. Dataset and benchmark research

# 8.1 QuixBugs

QuixBugs became the most practical executable external benchmark for the project's controlled debugging experiments.

The research directory preserves:

- per-task smoke manifests;
- source-integrity records;
- an eight-task pilot manifest;
- static-versus-PDB paired-pilot contracts;
- route/authorization templates.

The accepted gold infrastructure baseline covered:

- `gcd`
- `bucketsort`
- `find_in_sorted`
- `flatten`
- `kth`
- `hanoi`
- `is_valid_parenthesization`
- `kheapsort`

On the pinned QuixBugs revision, the literal upstream buggy -> corrected diff solved **8/8** selected gold tasks. This validated:

- source checkout/integrity;
- containment;
- patch application;
- verifier execution.

It did **not** measure model capability.

## Paired static-versus-PDB campaign

The QuixBugs directory also contains V1–V4 frozen campaign contracts.

These were designed to compare static and debugger-assisted conditions under a fixed protocol and route.

Current project disposition:

**RETAIN_OPTIONAL / OWNER-AUTHORIZED**

The campaign is historical/optional and is not required for the accepted Local Application V1 or R1–R6 conclusions.

The V4 OpenCode route contract should not be retrofitted onto Ollama simply because Ollama later became the successful product route; that would change the experimental protocol.

---

# 8.2 BugsInPy

BugsInPy was initially attractive because it offered realistic Python bug-fixing tasks.

The project created:

- `BUGSINPY_LICENSE_GATE_V1.json`
- `PILOT_ELIGIBILITY_MANIFEST_V1.json`

The license/compliance gate concluded:

**BLOCKED**

Therefore BugsInPy execution was not treated as authorized.

This is an important research-process result: dataset availability is not only a technical question. Licensing, redistribution, source provenance, and executable acquisition must be cleared before benchmark use.

**Project consequence:** preserve the selection/gate evidence, but do not claim BugsInPy experimental results.

---

# 8.3 SWE-bench and SWE-rebench influence

SWE-bench supplied the evaluation philosophy:

- base commit;
- reproducible environment;
- patch apply;
- F2P;
- P2P;
- execution-based correctness.

Later project work also investigated SWE-rebench V2 as a training-corpus source. The broader project eventually obtained a viable repository-disjoint corpus/split and verified materialization, but the debugger project's accepted R1–R6 scientific result ultimately relied on the smaller controlled curated/QuixBugs sequence rather than a full SWE-bench leaderboard campaign.

---

# 9. What the project's own experiments taught us

The literature established the hypotheses; the repository experiments then tested the architecture.

## 9.1 Historical D1/S2: raw debugger access was not sufficient

The early real-model debugger attempts produced bounded negative evidence.

Examples included:

- a RAW model emitting a breakpoint request that reached the backend but targeted an invalid line;
- cp118 attempting `continue` without a valid active session;
- no successful iterative debugger loop;
- no debugger-informed patch reaching the verifier in those early attempts.

This result should **not** be summarized as:

> "Debuggers do not help."

The correct interpretation was:

> The tested policies did not yet demonstrate the interaction competence required to obtain and use valid debugger evidence.

This matched the later literature very well.

---

# 9.2 R1 — valid real-model breakpoint/PDB observation

R1 repaired the model-facing affordance/interface problem.

A RAW Qwen2.5-Coder-7B model authored a valid breakpoint against a curated task and the real PDB backend paused in production code.

**Result:** first accepted positive evidence that the model could successfully engage the real debugger after the interface repair.

**What it proved:** valid real-model PDB engagement.

**What it did not prove:** multi-turn debugging or correct repair.

---

# 9.3 R2 — sustained multi-turn debugger interaction

R2 required a staged debugger trajectory:

- breakpoint;
- stack;
- locals/print;
- step/next;
- post-step stack;
- diagnosis.

The real model completed the multi-turn interaction.

**Result:** debugger use was no longer a one-command novelty; the model could navigate a stateful observation loop under controlled affordances.

---

# 9.4 R3 — debugger evidence to verifier-resolved patch

R3 connected runtime evidence to actual repair:

`debugger observation -> diagnosis -> model patch -> PatchManager -> independent verifier`

The verifier returned:

- F2P 1/1;
- P2P 2/2;
- **RESOLVED**.

A mandatory methodological qualifier is preserved: the model's unified diff had a hunk-count metadata error that was corrected by deterministic **count-only** normalization. The semantic patch content was not rewritten.

**Result:** first accepted end-to-end debugger-informed repair.

---

# 9.5 R4 — model-generated regression test

R4 tested whether the model could generate an auxiliary regression test without hidden-test leakage.

The generated test:

- failed the buggy program for the intended reason;
- passed the independently verified fixed program.

The independent verifier remained the final authority.

**Result:** model-generated tests can provide useful auxiliary evidence, but they do not replace frozen verifier contracts.

---

# 9.6 R5 — clean base-14B holdout

R5 tested a larger un-fine-tuned base model across the full five-task curated suite under anti-leakage controls.

Accepted clean treatment:

- **5/5 RESOLVED**
- **0 leakage findings across 41 audited actual prompts**

An earlier 5/5 treatment was disqualified because the prompts contained hidden-test leakage; it was retained only as historical upper-bound evidence.

**Research lesson:** model strength mattered substantially, and strict prompt/evidence hygiene mattered just as much.

This is consistent with debug-gym and ADI-style findings that debugger benefit can be model-dependent.

---

# 9.7 R6 — debugger-trajectory-oriented QLoRA training

R6 moved beyond ordinary localized repair SFT and trained directly on debugger-oriented trajectories.

Dataset split:

- 21 QuixBugs training tasks;
- 8 disjoint QuixBugs validation tasks;
- 5-task curated stronger holdout.

Accepted validation result:

- **8/8 RESOLVED**
- 97 model calls;
- 64,783 tokens;
- approximately 841.7 seconds task runtime;
- zero row-level execution errors in the accepted validation capsule.

The stronger five-task curated holdout was not completed because of repeated laptop hard power-offs.

Observed completed holdout cases:

- `curated-none-handling-001`: RESOLVED;
- `curated-off-by-one-002`: BREAKING_RESOLVED / strict failure;
- remaining tasks were not completed.

Accepted status:

**INCOMPLETE_HARDWARE_STOP**

This is not reported as a 2-task score or as a failed five-task benchmark.

## What R6 supports

- debugger-oriented trajectory post-training can produce a model that successfully operates the project's debugger interface on disjoint validation tasks;
- harness/action-space-specific training is a plausible route to learned debugger competence.

## What R6 does not support

The project does **not** claim:

> "Fine-tuning caused the 8/8 improvement."

There was no matched-base R6 ablation under identical tasks/controller/compute.

This non-claim is important and matches the literature's emphasis on controlled attribution.

---

# 9.8 Earlier RAW / cp118 repair results and negative transfer

The project also observed a strong earlier contrast in the broader repair experiments:

- RAW baseline: 5/40 resolved;
- cp118 checkpoint: 0/40 resolved.

This is evidence of negative transfer in that experiment, not proof that fine-tuning is generally harmful.

It reinforced the decision not to assume that a narrowly tuned patch model would automatically gain general software-debugging or debugger-control competence.

---

# 9.9 cp118 + RAG

The cp118 + frozen repository-RAG condition did not complete the full intended evaluation.

Observed production:

- 10/40 tasks produced valid pairs;
- several outputs hit the token cap;
- correctness was not fully evaluated.

Accepted status:

**CLOSED — PARTIAL / COMPUTE-CONSTRAINED / NOT_EVALUATED**

No positive or negative scientific RAG claim is made from this partial condition.

**Research lesson:** retrieval infrastructure may be useful, but incomplete generation without verifier outcomes cannot establish repair effectiveness.

---

# 9.10 DPO

DPO was considered but ultimately closed as **not justified** under the available evidence/data.

The literature suggested that trajectory/tool-use data and execution-grounded training were a more direct missing capability than preference optimization over an insufficiently grounded repair corpus.

---

# 10. Product/runtime engineering results vs scientific results

The project eventually completed a successful real Ollama Cloud route:

- full session completed;
- patch generated/applied;
- independent verifier returned RESOLVED;
- cleanup and replay were verified.

This is an important **product/runtime success**.

However, the successful Ollama session did **not** exercise PDB. Therefore it does not replace or supersede the scientific question of whether debugger use improves repair.

This distinction is maintained throughout the project:

- product route works;
- debugger architecture works;
- R1–R6 provide the accepted debugger evidence;
- the optional Six-Case static-vs-PDB campaign remains a separate historical protocol.

---

# 11. Final interpretation: what the project actually demonstrated

The complete research record supports a stronger and more precise conclusion than either "debuggers work" or "debuggers fail."

## Demonstrated

1. A deterministic, typed, stateful debugger runtime can be built and validated.
2. Real models can learn to issue valid debugger actions under an appropriate interface.
3. A real model can sustain a multi-turn PDB interaction.
4. Debugger evidence can feed a diagnosis and patch that passes an independent verifier.
5. A model can generate a useful auxiliary regression test without replacing the verifier.
6. Model capacity/interface quality strongly influence tool-use success.
7. Debugger-oriented trajectory training can yield strong disjoint validation performance.
8. Scientific provenance, prompt leakage controls, and verifier independence materially change what results are admissible.

## Not demonstrated

1. That PDB always improves repair over static methods.
2. That the R6 fine-tuned model is causally better than a matched base model.
3. That multi-agent architecture is superior.
4. That RAG improved cp118 repair.
5. That BugsInPy was successfully evaluated.
6. That the incomplete R6 curated holdout has a meaningful aggregate success rate.
7. That the successful Ollama product route constitutes debugger-use evidence.

---

# 12. The strongest architecture lessons

## 12.1 Keep the controller deterministic, not the reasoning

The controller should enforce:

- legal transitions;
- safety;
- time/action budgets;
- bounded outputs;
- cleanup;
- reproducibility.

It should **not** predetermine every debugging decision.

The model should remain responsible for adaptive reasoning and evidence selection.

## 12.2 Debugger state should be modeled explicitly

A debugger is not a bag of independent commands.

For example:

- `continue` requires a live session;
- locals require a paused frame;
- stack inspection depends on process state;
- expression evaluation must be scoped and safe.

A state machine is therefore not merely an implementation convenience; it matches the semantics of the environment.

## 12.3 Typed tools reduce protocol burden

Instead of expecting the model to remember raw PDB syntax and session mechanics, expose actions such as:

- start/continue/step/next;
- get stack;
- get frame locals;
- get bounded source;
- safe evaluate;
- stop session.

This reduces invalid-command variance and makes experiments more comparable.

## 12.4 Keep evidence compact

The literature repeatedly warns about excessive context.

Useful model observations should be:

- bounded;
- structured;
- relevant;
- attributable to a tool action.

Large raw terminal dumps are usually a worse interface.

## 12.5 Keep verification independent

The model may:

- propose a diagnosis;
- write a patch;
- generate a test.

It must not be the sole judge of whether the bug is fixed.

---

# 13. Future-work priorities supported by both literature and project evidence

The final literature closeout ranked future work roughly as follows.

## Priority 1 — debugger-specific trajectory post-training

Continue training on trajectories expressed in the same typed action vocabulary used by the controller.

Include:

- successful session setup;
- breakpoint selection;
- stack/locals inspection;
- continuation/stepping;
- hypothesis revision;
- `tool_error` recovery;
- unsuccessful trajectories with useful recovery behavior.

The project already obtained positive R6 evidence for this direction.

## Priority 2 — matched stronger-base comparison

Run a stronger base model under the **same** controller, tasks, prompts, and verifier.

This isolates model capability without changing the entire stack.

## Priority 3 — debugger interface ablation

Compare:

- low-level PDB-like actions;
- state-aware atomic actions;
- higher-level semantic dynamic-analysis operations.

This is strongly motivated by ADI/FramePilot, InspectCoder, Debug2Fix, SWE-agent, and the project's early D1/S2 failures.

## Priority 4 — execution-feedback RL / process supervision

Promising but less mature for debugger-specific work.

Best attempted after a high-quality trajectory dataset and stable controller exist.

## Priority 5 — controlled multi-agent specialization

Compare:

- one model/controller;
- dedicated inspector/debugger + coder;
- possibly reviewer/verifier assistance.

Control total tokens/model family/tools where possible.

## Lowest priority — generic multi-agent swarm expansion

A planner/localizer/debugger/coder/tester swarm would expand the scientific question and weaken attribution unless a specific hypothesis requires it.

---

# 14. Research-file map and what each area represents

## `research/literature/`

### `agentic_debugging_literature_closeout_2026-08-11.md`

The final reviewed literature synthesis. This is the highest-level literature authority in `research/`.

It connects the literature directly to the project's actual R1–R6 evidence and architecture.

---

## `research/notes/`

Manual paper notes:

- `2023_swe_bench_notes.md`
- `2024_agentless_notes.md`
- `2024_autocoderover_notes.md`
- `2024_chatdbg_notes.md`
- `2024_ldb_notes.md`
- `2024_openhands_notes.md`
- `2024_repairagent_notes.md`
- `2024_swe_agent_notes.md`
- `2025_debug_gym_notes.md`
- `paper_notes_template.md`

These preserve the detailed reading process and "what applies to our project" reasoning.

`notes/.gitkeep` is only a historical placeholder and has no research content.

---

## `research/papers/`

Local primary-paper archive.

### Tier 1 PDFs

- SWE-bench
- Agentless
- ChatDBG
- debug-gym

### Tier 2 PDFs

- AutoCodeRover
- LDB
- OpenHands
- RepairAgent
- SWE-agent / Agent-Computer Interfaces

Manifests:

- `TIER1_LOCAL_MANIFEST.md`
- `TIER2_LOCAL_MANIFEST.md`

The PDF files are local reading inputs rather than canonical tracked project evidence.

The various `.gitkeep` files in empty paper subdirectories are structural placeholders, not substantive research.

---

## `research/reports/raw/`

Three initial AI-generated literature reports:

- ChatGPT
- Claude
- Gemini

These are **non-authoritative discovery inputs**.

They are superseded for scientific claims by:

- primary papers;
- manual notes;
- reviewed cross-report synthesis;
- final literature closeout.

---

## `research/reports/synthesis/`

Reviewed research-process artifacts:

- `phase1_cross_report_synthesis_v1.md`
- `source_consensus_matrix_v1.md`
- `claims_to_verify_v1.md`
- `pdf_download_manifest_clean_v1.md`
- `project_next_steps_v1.md`
- `diary_day_02_draft.md`

These document how broad AI-assisted discovery was converted into a controlled primary-source reading process.

---

## `research/synthesis/`

Architecture syntheses derived from the first reading blocks:

### `pdb_debugger_agent_mvp_rationale.md`

Established:

- Python/PDB first;
- single controller;
- deterministic tools;
- verifier-backed repair;
- Agentless-style static baseline;
- runtime-state research question.

### `tier2_mvp_architecture_update.md`

Refined the architecture into:

- state machine;
- typed actions;
- sandbox/runtime boundary;
- event logs;
- AST/symbol retrieval;
- structured PDB observations;
- deterministic patch/test loop.

These files are historical design rationale and explain why the codebase took its current shape.

---

## `research/quixbugs/`

Frozen benchmark/evaluation contracts.

Important categories:

- source-integrity authority;
- per-task smoke manifests;
- eight-task gold pilot;
- paired static-versus-PDB V1–V4 contracts;
- route/authorization templates;
- qualification evidence.

These are protocol/evaluation artifacts, not direct model-performance claims.

The accepted paired-pilot campaign is optional/historical.

`quixbugs/__pycache__/...pyc` is generated Python bytecode and has no research value.

---

## `research/bugsinpy/`

Contains the license gate and pilot-selection authority.

Accepted status:

**BLOCKED / license-gated**

These files explain why BugsInPy was not executed rather than representing an experiment result.

---

## `research/literature_notes_01.md`

Early conceptual notes defining:

- debugging;
- automated debugging;
- fault localization;
- program repair;
- the initial relationship between these concepts and the project.

Historically useful as the starting conceptual frame; later syntheses are more complete.

---

# 15. Recommended authority order when reading the research directory

When two files differ in wording or maturity, use this order:

1. **Current project evidence**
   - `docs/project-closeout.md`
   - `docs/results-index.md`
   - `docs/final-report.md`
   - `experiments/.../README.md` / frozen evidence
2. **Final literature closeout**
   - `research/literature/agentic_debugging_literature_closeout_2026-08-11.md`
3. **Primary papers + manual notes**
   - `research/papers/`
   - `research/notes/`
4. **Reviewed syntheses**
   - `research/synthesis/`
   - `research/reports/synthesis/`
5. **Raw AI research reports**
   - `research/reports/raw/`

Raw reports should never override a verified primary source or accepted project result.

---

# 16. Compact project thesis

The research and implementation together support the following thesis:

> **Runtime state is a valuable debugging evidence channel, but effective use of that evidence is an agent capability rather than a property of the debugger itself. A reliable debugger-assisted repair system therefore needs both a capable policy and a carefully designed interaction layer: deterministic state management, typed debugger actions, bounded observations, explicit hypotheses, reproducible execution, and an independent verifier.**

The project's early failures showed what happens when the policy/interface combination cannot obtain usable state. R1–R4 then demonstrated progressively stronger real-model debugger behavior. R5 showed the importance of model strength and leakage-clean evaluation. R6 demonstrated that debugger-oriented trajectory training can support strong disjoint validation performance, while the incomplete holdout and lack of a matched-base ablation preserve the remaining scientific uncertainty.

That final position is consistent with the broader literature:

- execution state can improve reasoning;
- raw debugger access can be neutral or harmful;
- interface design materially changes agent performance;
- tool/harness behavior can be trained;
- multi-agent specialization is promising but not mandatory;
- correctness requires independent execution-based verification.

---

# 17. Bottom line

The research phase did not merely collect papers. It directly shaped the system:

```text
Literature:
runtime evidence matters
+ raw tool access is fragile
+ static baselines are strong
+ verification must be independent
+ interface/action-space design matters
+ tool behavior can be trained

        ↓

Project architecture:
single policy
+ deterministic state machine
+ typed PDB/tools
+ bounded action/observation loop
+ disposable runtime
+ PatchManager
+ independent verifier
+ structured trajectories

        ↓

Project evidence:
early debugger failures
→ repaired interface
→ valid breakpoint
→ multi-turn PDB
→ debugger-informed verified patch
→ model-generated regression test
→ clean base-14B 5/5
→ debugger-trajectory QLoRA 8/8 disjoint validation
→ stronger holdout left incomplete by hardware
```

The most defensible final conclusion is therefore not that dynamic debugging is universally superior. It is that **debugger-grounded repair is technically viable, scientifically distinct from static repair, and strongly dependent on model competence, interaction design, and trajectory-specific training.**

