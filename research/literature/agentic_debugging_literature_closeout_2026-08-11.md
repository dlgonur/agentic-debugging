# 1. Executive conclusion

**Yes. The project's single-agent + deterministic-controller + typed-tool architecture was a defensible design choice, and the literature through August 11, 2026 does not show that a multi-agent redesign was academically necessary.**

The evidence supports four narrower conclusions.

First, **real runtime evidence can improve debugging and repair**, sometimes substantially. ChatDBG demonstrated that an LLM can actively query conventional debuggers; LDB and NExT showed gains from concrete execution-state information; InspectCoder and FSE 2026's ADI/FramePilot provide stronger recent evidence that runtime inspection can improve repair. ([arxiv.org](https://arxiv.org/abs/2403.16354))

Second, however, **raw debugger access is not equivalent to useful debugger capability**. The strongest 2026 evidence is unusually clear on this point. In the ADI ablation, simply adding conventional PDB moved Claude Sonnet 3.7 from 55.0% to only 55.8% on SWE-bench Verified, hurt GPT-4o from 32.6% to 31.2%, and hurt Qwen3 from 29.2% to 28.4%; the agent-oriented FramePilot interface instead reached 63.8%, 36.2%, and 31.4%, respectively. ([arxiv.org](https://arxiv.org/html/2604.24212v1)) InspectCoder likewise reports that direct stateful debugger interaction caused invalid commands, corrupted sessions, and error loops; its middleware explicitly tracks legal state transitions and blocks state-invalid operations. ([arxiv.org](https://arxiv.org/html/2510.18327v1)) Debug2Fix similarly found that directly exposing debugger tools was nearly neutral or harmful, whereas a more strongly scaffolded debugging interface/subagent helped. ([arxiv.org](https://arxiv.org/html/2602.18571v1))

Third, **ordinary code-repair fine-tuning should not be expected to produce debugger competence automatically**. SWE-Gym shows that actual agent trajectories can substantially train tool-using SWE behavior, while 2026 Open-SWE-Traces shows measurable cross-harness degradation specifically because agents overfit interaction patterns, action spaces, and observation formats. ([proceedings.mlr.press](https://proceedings.mlr.press/v267/pan25g.html)) NExT similarly required explicit execution-state training to improve runtime reasoning. ([proceedings.mlr.press](https://proceedings.mlr.press/v235/ni24a.html)) There is therefore no good empirical basis for assuming that a QLoRA checkpoint trained primarily for localized repair will spontaneously learn PDB session semantics.

Fourth, **multi-agent systems are now empirically credible, but are not a universal architectural requirement**. ICLR 2026 BOAD is the strongest counterargument: under essentially unchanged total-token use on SWE-bench Verified, its learned hierarchy improved Seed-OSS-36B from 49.8% to 53.2%. Yet its manually designed sub-agent version scored only 47.4%, and adding more sub-agents was non-monotonic. Other striking multi-agent results often combine role decomposition with extra inference, better tools, execution loops, or substantially greater token expenditure. AgentForge, for example, uses about **2.7×** the token cost of its single-agent baseline. ([arxiv.org](https://arxiv.org/html/2604.13120v1))

So the defensible closeout claim is:

> **Current evidence supports runtime-aware agentic debugging, but does not support the assumption that merely exposing a conventional debugger causes a code-repair model to use it competently. Effective systems increasingly rely on structured, state-aware interfaces, explicit agentic/trajectory training, strong base models, or specialized orchestration. The project's deterministic controller and typed tool layer are therefore aligned with—not contradicted by—the strongest recent evidence.**

Your RAW/cp118 result should accordingly be treated as **negative evidence about emergent debugger interaction under the tested model/checkpoint/scaffold combination**, not negative evidence about the value of debugging information itself.

---

# 2. Debugger-aware and runtime-aware systems

## ChatDBG: early proof that active debugger access can help

**ChatDBG**, published in FSE 2025, is the clearest landmark preceding the current autonomous-agent wave. It integrates Pdb, GDB, and LLDB and permits an LLM to take control of the debugger, inspect stacks and program state, and return a root-cause explanation/fix. For its Python evaluation, one high-level query produced an actionable fix 67% of the time; allowing one follow-up raised that to 85%. ([arxiv.org](https://arxiv.org/abs/2403.16354))

That establishes **feasibility**, but it is not equivalent to modern autonomous repository repair: ChatDBG is fundamentally a debugging assistant in a programmer-in-the-loop setting, not an autonomous SWE-bench-style repair agent.

## debug-gym: access alone does not solve tool use

**debug-gym** in 2025 moved closer to the present project by exposing PDB within a textual interactive environment for coding agents. Its importance is less a headline SOTA number than the model-dependent behavior it exposed: stronger reasoning models could exploit interactive debugging, while weaker models could fail to benefit or regress. ([arxiv.org](https://arxiv.org/abs/2503.21557))

This is directly relevant to a 7B Qwen experiment. The appropriate inference is not:

> “PDB should have helped Qwen2.5-Coder-7B.”

It is:

> “PDB created a new sequential decision problem that the model additionally had to know how to operate.”

## InspectCoder: direct debugger interaction plus stateful middleware

**InspectCoder**, published in PACMPL/OOPSLA1 2026, is currently among the closest papers to your experiment. It uses actual interactive debugging: breakpoint placement, targeted state inspection, continued execution, and runtime perturbation. Its Program Inspector and Patch Coder form a dual-agent repair loop. ([dl.acm.org](https://dl.acm.org/doi/10.1145/3798238))

On BigCodeBench-R with Qwen2.5-Max, InspectCoder's resolve rate was **67.87%**, compared with 64.58% for its strongest LDB variant and **58.86% when the actual debugger was removed**. ([arxiv.org](https://arxiv.org/html/2510.18327v1))

But its architecture result is more relevant to your project than the score. The authors explicitly report that LLMs:

- lack adequate training data for agentic debugger operation;
- struggle to compose primitive debugger operations;
- issue state-invalid commands;
- corrupt debugger sessions;
- become confused by verbose native debugger output. ([arxiv.org](https://arxiv.org/html/2510.18327v1))

Their solution, InspectWare, models five debugger states, continuously tracks transitions, exposes current session state, **programmatically prevents illegal actions**, rewrites/filter outputs, and returns explicit invalid-action feedback. Its middleware improved results over direct debugger integration by 5.44 and 2.65 percentage points on the two evaluated benchmarks. ([arxiv.org](https://arxiv.org/html/2510.18327v1))

That is remarkably close to the rationale for a deterministic controller plus typed PDB tools.

## ADI / FramePilot: probably the strongest direct architecture evidence

**Empowering Autonomous Debugging Agents with Efficient Dynamic Analysis**, published in FSE 2026, is even more consequential for your architecture.

The authors argue that human-oriented PDB/GDB interfaces expose very low-level sequential operations. They instead construct an **Agent-centric Debugging Interface (ADI)** around Frame Lifetime Traces and explicitly formulate debugging as a **state-transition system with well-defined commands**. ([arxiv.org](https://arxiv.org/html/2604.24212v1))

Its controlled ablation is particularly useful:

- Claude Sonnet 3.7: static BaseAgent **55.0%** → raw PDB **55.8%** → FramePilot **63.8%**
- Claude Sonnet 3.5: **44.4 → 45.0 → 51.2**
- GPT-4o: **32.6 → 31.2 → 36.2**
- Qwen3: **29.2 → 28.4 → 31.4**. ([arxiv.org](https://arxiv.org/html/2604.24212v1))

So **actual conventional PDB access was not reliably better than no PDB at all**. The benefit came from representing dynamic execution in a form adapted to an autonomous agent.

This is arguably the single most useful paper for interpreting your negative result.

## DebugHarness: dynamic debugging for C/C++ security repair — preprint evidence

**DebugHarness**, an April 2026 **preprint** (arXiv:2604.03610), targets autonomous repair of complex real-world C/C++ security vulnerabilities. Rather than relying only on static artifacts, it begins from a reproducible crash, actively queries the live runtime environment, probes program memory states and execution paths, and iteratively validates patches in a closed-loop debugging-and-repair cycle. Its systems-tool suite includes conventional dynamic-debugging facilities such as GDB, debugger augmentation such as pwndbg, and record-and-replay support such as rr. ([arxiv.org](https://arxiv.org/abs/2604.03610))

On the paper's SEC-bench evaluation, the authors report that DebugHarness successfully patches **approximately 90%** of the evaluated vulnerabilities and achieves a **relative improvement of more than 30%** over the compared state-of-the-art baselines. ([arxiv.org](https://arxiv.org/abs/2604.03610))

This strengthens one narrow conclusion of the present review: **dynamic debugging can be materially valuable once an agent reaches and productively uses runtime evidence.** It does **not** show that raw conventional debugger access should automatically help every repair model. DebugHarness addresses C/C++ security and vulnerability repair, uses a substantially different systems-oriented scaffold and tool suite, and is not directly comparable to this project's Python / QuixBugs / PDB experiment.

## Debug2Fix: debugger specialization helps, but attribution is difficult

The 2026 **Debug2Fix** preprint exposes PDB/JDB through a dedicated Debug Subagent. The authors state that early versions which split debugger initialization into several low-level tools suffered failures, races, and timeouts; they therefore collapsed the sequence into an atomic `Debug Start Session` operation and provided a single typed control operation with an enum for continue/step actions. ([arxiv.org](https://arxiv.org/html/2602.18571v1))

Its direct-tool ablation is revealing. On GitBug-Java:

- GPT-5 baseline 60.2%; direct debugger tools 60.8%.
- Claude Haiku 4.5: 71.0 → 70.4%.
- Claude Sonnet 4.5: 75.7 → **64.5%**.

The debugger-subagent configurations did better, reaching as high as 85.5% with enforced debugger use. ([arxiv.org](https://arxiv.org/html/2602.18571v1))

But it is not a clean proof that “multi-agent beats single-agent”: the subagent consumes considerable additional tokens and changes both interface design and debugger-use policy. Its own failure analysis also reports debugger-session failures in 36% of sampled failures. ([arxiv.org](https://arxiv.org/html/2602.18571v1))

### Maturity assessment — Question B

As of **August 11, 2026**, I would classify debugger-aware LLM research as:

**Emerging and now empirically credible, but not yet mature.**

It is no longer accurate to call it a nearly empty niche: ChatDBG, InspectCoder, and ADI now provide **peer-reviewed** evidence, while debug-gym, Debug2Fix, and DebugHarness expand the experimental space as **preprint / environment evidence** rather than peer-reviewed confirmation. ([dl.acm.org](https://dl.acm.org/doi/10.1145/3729355)) ([arxiv.org](https://arxiv.org/abs/2604.03610))

But the evidence remains heterogeneous across self-repair, repository repair, models, debugger abstractions, and human-vs-autonomous settings. More importantly, the field has **not converged on raw PDB/GDB as the correct interface**. The 2026 trend is toward state-aware middleware and semantic debugger abstractions.

---

# 3. Tool-using SWE agents

The broader SWE-agent literature makes a related point: **the interface is part of the agent system, not neutral plumbing.**

SWE-agent's NeurIPS 2024 contribution was explicitly an **Agent-Computer Interface (ACI)** designed around what LMs can reliably manipulate: repository navigation, editing, search, execution, and feedback. Its original evaluations showed large improvements over contemporaneous non-interactive approaches. ([arxiv.org](https://arxiv.org/abs/2405.15793))

Conversely, **Agentless** demonstrated that complex free-form agent loops were not always necessary: a predetermined localization → repair → validation pipeline achieved 32% SWE-bench Lite at low reported cost in its historical evaluation. ([arxiv.org](https://arxiv.org/abs/2407.01489)) That result is no longer performance-frontier evidence in 2026, but its methodological lesson survives: more LLM-controlled orchestration is not automatically better.

## PatchPilot: complementary evidence for structured orchestration

**PatchPilot**, a **peer-reviewed ICML 2025** paper, explicitly contrasts **agent-based planning**, where an LLM determines the patching workflow, with **rule-based planning**, where the workflow is predefined. PatchPilot adopts a structured five-component rule-based workflow: **reproduction → localization → generation → validation → refinement**. The authors motivate this design as a way to balance patching efficacy with greater stability and cost-efficiency; their evaluation reports less than $1 average cost per instance alongside competitive SWE-bench performance. ([PMLR](https://proceedings.mlr.press/v267/li25cf.html))

PatchPilot is therefore complementary architecture evidence for the narrower proposition that a useful SWE system need not delegate every orchestration decision to the language model. It does **not** establish that this project's deterministic debugger controller is optimal; it simply provides peer-reviewed precedent for deliberately retaining rule-based workflow structure around an LLM.

The same principle continues into 2026. SWE-Master is a technical report rather than peer-reviewed evidence, but its framework combines teacher trajectories, long-horizon SFT, execution-feedback RL, and explicit inference-interface design rather than relying on an unstructured shell agent. ([arxiv.org](https://arxiv.org/html/2602.03411v1))

The combined evidence therefore supports **iterative tool use, repository navigation, and execution feedback**, but does not imply that the LLM should control every low-level orchestration decision.

---

# 4. Dynamic/runtime evidence

The literature supports a useful hierarchy:

**test result < execution trace/state < selectively gathered debugger state**, provided the model can consume the richer signal correctly.

### LDB

LDB is not an interactive PDB agent. It instruments execution, partitions code into basic blocks, records intermediate values, and lets an LLM evaluate execution step-by-step. Across HumanEval, MBPP, and TransCoder, it improved baseline debugging by up to **9.8%**. ([arxiv.org](https://arxiv.org/abs/2402.16906))

That establishes a benefit from **real intermediate runtime state**, not from debugger command skill.

### NExT

NExT, ICML 2024, goes further by actually **training** the model to reason from execution traces. It self-generates execution-aware rationales based on variable states. In PaLM 2 program repair experiments, it improved fix rate by **26.1 percentage points on MBPP and 10.3 points on HumanEval**. ([proceedings.mlr.press](https://proceedings.mlr.press/v235/ni24a.html))

This is important because it separates two questions:

1. Is runtime state informative? **Yes.**
2. Does an ordinary code model inherently know how to reason from it? **Not necessarily; explicit training helps.**

### REval

ICSE 2025's REval supports that caution. Across code LLMs, average runtime-behavior-reasoning accuracy was only **44.4%**, with an average incremental-consistency score of **10.3**. ([arxiv.org](https://arxiv.org/abs/2403.16437))

So even before introducing a stateful debugger protocol, reliable reasoning about execution state is itself a nontrivial learned capability.

### Answer to Question A

**Yes, there is credible evidence that explicit runtime information can improve debugging/repair over static context.**

But the stronger statement—

> “Giving an agent a debugger improves repair”

—is **not supported unconditionally**.

The actual evidence says:

> **Usable dynamic evidence helps; low-level debugger exposure by itself often does not.**

That distinction should be explicit in the internship report.

---

# 5. Multi-agent debugging evidence

This is the area where the closeout needs the most restraint.

## BOAD — strongest evidence that coordination itself can matter

**BOAD**, published at ICLR 2026, is the most persuasive result I found because it comes closer than most papers to controlling the compute objection.

With Seed-OSS-36B:

- SWE-agent single agent: **49.8% Verified / 12.3% Live**
- manually designed sub-agent: **47.4 / 14.0**
- evolutionary hierarchy: **46.0 / 17.0**
- BOAD-discovered hierarchy: **53.2 / 20.0**. ([arxiv.org](https://arxiv.org/html/2512.23631v2))

On Verified, total tokens were essentially identical: **0.92M vs 0.93M (+0.7%)**. On Live, BOAD actually used 23.8% fewer total tokens. ([arxiv.org](https://arxiv.org/html/2512.23631v2))

That is genuine evidence that **a well-selected hierarchy can improve SWE performance for reasons beyond simply spending more tokens**.

But it simultaneously demonstrates why “multi-agent is better” is too broad:

- the manual hierarchy was worse than single-agent on Verified;
- Top-2 sub-agents scored 20.0% on Live, whereas Top-5 fell to 13.7%;
- the paper identifies error propagation when the orchestrator accepts incorrect sub-agent outputs. ([arxiv.org](https://arxiv.org/html/2512.23631v2))

So the causal claim is **optimized decomposition can help**, not **multi-agent architecture is intrinsically superior**.

## DeLM — promising, but test-time-scaling evidence

The June 2026 **DeLM** preprint uses decentralized agents sharing verified context. On SWE-bench Verified with Gemini 3 Flash it reports 65.7% Avg.@1, 72.9% Pass@2 and 77.4% Pass@4, outperforming alternative parallel/centralized approaches while reporting lower per-task cost. ([arxiv.org](https://arxiv.org/abs/2606.10662))

This is interesting evidence that shared exploration can make **parallel test-time attempts** more efficient. It is not a direct comparison of your architecture against a single sequential debugging agent.

## AgentForge — strong headline, badly confounded for this question

AgentForge reports **40.0% SWE-bench Lite**, against 14.0% for its one-shot GPT-4o baseline and 12.0% for its ReAct baseline. Removing Tester or Debugger agents hurts performance. ([arxiv.org](https://arxiv.org/html/2604.13120v1))

But its full system uses approximately:

- 13,600 input + 5,100 output tokens/task,
- versus 5,000 + 1,800 for the single-agent baseline,

or **2.7× the cost**. ([arxiv.org](https://arxiv.org/html/2604.13120v1))

Moreover, its “Debugger” consumes test/stderr execution feedback; it is not an interactive PDB-style debugger. ([arxiv.org](https://arxiv.org/html/2604.13120v1))

It therefore supports **structured execution-feedback loops**, not a clean causal claim for multiple agents.

## SGAgent

SGAgent, accepted to TOSEM 2026, separates localization, suggestion, and fixing and reports 51.3% SWE-bench Lite with Claude 3.5. ([arxiv.org](https://arxiv.org/abs/2602.23647))

But a localizer → suggester → fixer decomposition could also be implemented as three deterministic phases calling one model. The evaluation does not isolate **agent identity multiplicity** from **additional intermediate reasoning/scaffolding** well enough to claim that three independent agents are necessary.

## InspectCoder and Debug2Fix

Both are more directly relevant because they couple specialization with debugging.

InspectCoder's Inspector/Coder separation is plausible: runtime diagnosis and patching impose different contexts and objectives. ([arxiv.org](https://arxiv.org/html/2510.18327v1)) Debug2Fix similarly shows that a dedicated debugger context can outperform dumping all debugger tools into the main agent. ([arxiv.org](https://arxiv.org/html/2602.18571v1))

But neither cleanly controls total computation while holding interface/scaffold constant.

### Answer to Question E

**No. The project was not mistaken to omit multi-agent orchestration.**

The defensible academic position is:

- multi-agent SWE has become a **credible promising direction**;
- BOAD demonstrates at least one meaningful controlled gain;
- nevertheless, multi-agent superiority is **not general**, is not specifically established for interactive debugging, and often remains entangled with additional inference, scaffolding, context, and tools.

A multi-agent design is therefore reasonable **future-work ablation**, not a prerequisite for the project's validity.

---

# 6. Tool-use / trajectory post-training evidence

This area gives the clearest answer to Question C.

## SWE-Gym

SWE-Gym, ICML 2025, contains 2,438 executable real-world SWE tasks and explicitly trains software-engineering agents rather than ordinary patch generators. Its trained agents gained as much as **19 percentage points** on SWE-bench Lite/Verified, with additional trajectory-trained verifiers used for inference-time selection. ([proceedings.mlr.press](https://proceedings.mlr.press/v267/pan25g.html))

This demonstrates that long-horizon interaction behavior itself is a trainable distribution.

## Open-SWE-Traces

Open-SWE-Traces is an especially useful 2026 preprint because it examines **harness transfer**. It contains 207,489 trajectories generated under OpenHands and SWE-agent-style harnesses and fine-tunes Qwen3-family models. ([arxiv.org](https://arxiv.org/html/2606.16038v1))

Its cross-harness ablation finds consistent performance drops when a model trained with one harness is moved to the other. The authors attribute the degradation to adaptation to specific **interaction patterns, action spaces, and observation formats**. ([arxiv.org](https://arxiv.org/html/2606.16038v1))

This is almost a direct counterexample to the proposition:

> “If the model learned repair, it should know how to use whatever tool interface we give it.”

It does not.

The same study also found that retaining **unsuccessful trajectories** improved later agent performance compared with training exclusively on successful ones, suggesting that failures, retries, and recovery states can themselves be useful training signals. ([arxiv.org](https://arxiv.org/html/2606.16038v1))

## SWE-Master / SWE-TRACE

The 2026 SWE-Master technical report combines teacher-trajectory synthesis, long-horizon SFT, **RL with real execution feedback**, and scaffold design. ([arxiv.org](https://arxiv.org/html/2602.03411v1))

SWE-TRACE, also a 2026 preprint, explicitly constructs a 60K shortest-path trajectory SFT corpus and then uses a process-reward model/RL pipeline to evaluate and guide intermediate actions. ([arxiv.org](https://arxiv.org/html/2604.14820v1))

These are promising but should be labeled **preprint/technical-report evidence**, not settled peer-reviewed evidence.

### Answers C and D

**C. Does ordinary code-repair SFT provide evidence that debugger skills should emerge automatically? — No.**

Quite the opposite: NExT, SWE-Gym, Open-SWE-Traces, REval, and InspectCoder collectively suggest that execution reasoning, action sequencing, and stateful tool protocols are separate capabilities that benefit from targeted data or scaffolds. ([proceedings.mlr.press](https://proceedings.mlr.press/v235/ni24a.html))

**D. Is there evidence for explicit trajectory/tool-use post-training? — Yes, increasingly strong for SWE agents generally; still comparatively thin for debugger-specific training.**

That distinction matters. The field has substantial evidence for **agent trajectory post-training**, but comparatively little controlled evidence for training specifically on PDB/GDB trajectories. InspectCoder itself explicitly identifies targeted debugger SFT/RL as future work rather than something already solved. ([arxiv.org](https://arxiv.org/html/2510.18327v1))

---

# 7. Architecture implications for THIS project

## Reasons to retain single agent + deterministic controller + typed tools

### 1. It separates model capability from infrastructure behavior

Your controller knows whether a PDB session exists and whether actions are legal. That makes failures attributable:

- malformed/incorrect action chosen by model;
- deterministic tool rejection;
- valid runtime observation;
- subsequent model reasoning.

For academic evaluation, that separation is valuable.

InspectCoder's middleware was introduced for almost exactly this reason: stateful debugger commands depend on previous actions and the model otherwise corrupts the session. ([arxiv.org](https://arxiv.org/html/2510.18327v1))

### 2. A state machine is particularly appropriate for a debugger

Debugging is not a bag of independent tools.

`continue` is meaningful only in an appropriate active session state; breakpoint behavior depends on a target program and execution state; stack/locals/evaluation depend on a paused frame.

ADI independently formalizes its debugger as a **state-transition system**, while InspectWare tracks five explicit modes. ([arxiv.org](https://arxiv.org/html/2604.24212v1))

Thus a deterministic state machine is not an obsolete hand-coded workaround. It is directly aligned with 2026 debugger-agent research.

### 3. Typed tools reduce protocol burden

SWE-agent established the broader value of LM-oriented interfaces. ([arxiv.org](https://arxiv.org/abs/2405.15793)) Debug2Fix goes further: its authors explicitly collapsed fragile multi-step debugger setup into one atomic operation and represented execution control with an enum. ([arxiv.org](https://arxiv.org/html/2602.18571v1))

That favors typed actions over an unrestricted shell for controlled experimentation.

### 4. It improves reproducibility

A deterministic controller reduces stochasticity in orchestration. Given identical action arguments and environment state, controller transitions and errors can be reproduced independently of the LLM.

This is particularly important in a project comparing RAW and tuned models: allowing each model to invent its own orchestration protocol would add an uncontrolled systems-level confound. This is an inference from the state/interface sensitivity documented by ADI, InspectCoder, SWE-agent and Open-SWE-Traces. ([arxiv.org](https://arxiv.org/html/2604.24212v1))

### 5. Single-agent design improves attribution

If localization, debugger control, patch choice, verifier interpretation, and retry decisions are all logged from one policy, you can attribute behavioral differences between RAW and tuned checkpoints more cleanly.

Introducing several independent model instances would make it harder to determine whether a result came from:

- better base-model reasoning,
- role specialization,
- more inference tokens,
- independent sampling,
- context partitioning,
- or actual communication.

For an academic controlled experiment, the simpler architecture can therefore be a feature rather than a deficiency.

---

## Strongest counterarguments — Question G

There are nevertheless real limitations.

**First**, an overly rigid controller can suppress useful adaptive strategies. Determinism should enforce legal transitions and safety, not predetermine every debugging decision.

**Second**, the current PDB action granularity may still be too close to a human debugger. ADI's central result is that conventional PDB is often inferior to a function-level semantic dynamic-analysis interface. ([arxiv.org](https://arxiv.org/html/2604.24212v1))

**Third**, specialized context can help. Debug2Fix suggests that placing debugger interaction in a dedicated context can avoid overloading the primary code-repair agent, although its attribution is confounded by extra computation. ([arxiv.org](https://arxiv.org/html/2602.18571v1))

**Fourth**, BOAD now gives credible evidence that an automatically optimized hierarchy can outperform a single-agent system without simply spending dramatically more tokens. ([arxiv.org](https://arxiv.org/html/2512.23631v2))

**Fifth**, a 7B model may simply be below the capability threshold where raw, stateful PDB interaction becomes reliable. The debugger literature repeatedly shows substantial model dependence; the architecture cannot compensate indefinitely for model-side action-selection limitations. ([arxiv.org](https://arxiv.org/html/2604.24212v1))

These are arguments for carefully scoped future experiments, not reasons to invalidate the existing design.

---

# 8. Interpretation of the project's negative debugger result

This is where wording matters most.

The correct interpretation is **not**:

> “The debugger did not improve repair.”

Nor:

> “PDB is ineffective for Qwen2.5-Coder.”

Neither run ever acquired a valid PDB observation.

The more precise statement is:

> **Under the tested RAW Qwen2.5-Coder-7B-Instruct and cp118 conditions, neither model established a successful stateful debugger interaction loop. Both attempted debugger actions reached the deterministic backend, but failed before producing a usable runtime-state observation. Therefore the experiment provides bounded negative evidence about emergent debugger-action competence, not evidence about the downstream utility of runtime state once successfully acquired.**

The two failures are diagnostically different.

**RAW:** `break 20` reached the backend but referred to a line outside the executable probe. That is principally an **action-argument grounding / target-selection failure**.

**cp118:** `continue` was requested before an active PDB session existed. That is a **state-precondition / action-sequencing failure**.

These are remarkably compatible with InspectCoder's reported direct-debugger failure modes: models issue commands invalid in the current debugger state, which is why InspectWare explicitly exposes session state and rejects invalid transitions. ([arxiv.org](https://arxiv.org/html/2510.18327v1))

They are also compatible with Debug2Fix's finding that naïvely exposed debugger tools may be ignored, misused, or harmful and that debugger startup had to be bundled atomically after separate operations caused failures. ([arxiv.org](https://arxiv.org/html/2602.18571v1))

And they align with ADI's most important controlled result: **raw PDB access did essentially nothing or reduced success for several models**, while a higher-level agent-specific abstraction improved performance consistently. ([arxiv.org](https://arxiv.org/html/2604.24212v1))

### What the cp118 result does and does not tell you

It is particularly important not to overinterpret the tuned checkpoint.

If cp118 was trained primarily on localized repair examples rather than trajectories containing:

`session start → breakpoint → continue → pause → stack/locals/eval → hypothesis update → further debugger action → repair`

then there is no literature-backed reason to expect that training to produce PDB orchestration automatically.

Open-SWE-Traces shows that even agents trained on actual agent trajectories lose performance when the **tool/action harness changes**. ([arxiv.org](https://arxiv.org/html/2606.16038v1))

Therefore:

**the cp118 failure is consistent with a distribution mismatch between localized-repair SFT and interactive-debugger behavior.**

It does **not** by itself show that cp118 is a worse repair model.

Finally, because you observed **zero successful non-error PDB observations**, this experiment cannot answer whether either model could have:

- interpreted locals correctly;
- updated its hypothesis from runtime evidence;
- performed productive step/next sequences;
- used stack information;
- or generated a better patch as a result.

Those downstream capabilities were simply never reached.

That boundary should be stated explicitly.

---

# 9. Future-work recommendations, ranked by evidence

### 1. Debugger-specific trajectory post-training — **highest priority**

Train on trajectories expressed in **the project's existing typed tool/action vocabulary**, including valid session setup, breakpoint selection, state inspection, continuation, hypothesis revision, termination, and recovery from `tool_error`.

This follows the strongest relevant evidence: SWE-Gym demonstrates trajectory training works for agentic SWE; NExT shows execution reasoning is trainable; Open-SWE-Traces shows action-space specificity matters; InspectCoder explicitly identifies debugger-targeted SFT as a next step. ([proceedings.mlr.press](https://proceedings.mlr.press/v267/pan25g.html))

I would include **failure/recovery trajectories**, not only ideal demonstrations, given Open-SWE-Traces' finding that unresolved trajectories can improve later agent behavior. ([arxiv.org](https://arxiv.org/html/2606.16038v1))

### 2. Stronger base-model rerun under the identical controller — **high priority**

This is the cleanest way to test whether the observed failure is largely a model-capacity threshold while preserving experimental comparability.

Do **not** change controller, prompts, debugger backend, and model simultaneously.

ADI and debug-gym both indicate significant model dependence in the ability to exploit debugging tools. ([arxiv.org](https://arxiv.org/html/2604.24212v1))

### 3. Higher-level/state-aware debugger interface ablation — **high priority**

Not a replacement architecture: an ablation.

Compare the existing primitive PDB interface against something like:

- explicit session-state observation;
- legal-action masking;
- atomic start-and-break operation;
- function/frame-level runtime summaries.

This is supported directly by ADI, InspectWare and Debug2Fix. ([arxiv.org](https://arxiv.org/html/2604.24212v1))

The important research question would be:

> Does the model lack dynamic reasoning, or is it losing before dynamic reasoning begins because the interaction protocol is too low-level?

Your current result cannot distinguish those.

### 4. Execution-feedback RL / process training — **medium-high priority**

There is rapidly growing 2026 evidence for execution-verifiable RL and intermediate process supervision in SWE agents, including SWE-Master and SWE-TRACE, but much of it remains technical-report/preprint evidence and is not debugger-specific. ([arxiv.org](https://arxiv.org/html/2602.03411v1))

Thus it is promising after trajectory imitation, not the first missing piece.

### 5. Multi-agent debugger specialization — **medium priority, controlled ablation only**

A dedicated debugger agent could be compared against the current single-agent controller, motivated by InspectCoder, Debug2Fix and BOAD. ([arxiv.org](https://arxiv.org/html/2510.18327v1))

But the experiment should match, as far as possible:

- model family;
- total model-token budget;
- total tool-call budget;
- verifier budget;
- number of independent samples.

Otherwise it would not answer whether **specialization/coordination itself** helped.

### 6. More generic multi-agent expansion — **lowest priority**

The literature does not justify turning the project into a planner/localizer/debugger/coder/tester swarm merely for completeness.

That would substantially expand the experimental question while weakening attribution.

---

# 10. Final answer: Was single-agent + deterministic controller a defensible design choice?

**Yes. Clearly.**

More precisely:

**It was a defensible and methodologically strong choice for the project's stated experimental objective.**

The strongest 2026 debugger evidence does not argue for handing an LLM unrestricted control of a debugger. It increasingly argues for the opposite: **LLM-adapted actions, explicit state tracking, middleware-enforced legal transitions, controlled feedback, and observable trajectories.** ADI formally models debugging as a state-transition system; InspectCoder programmatically prevents state-invalid commands; Debug2Fix found atomic typed debugger operations more reliable than decomposed low-level operations. ([arxiv.org](https://arxiv.org/html/2604.24212v1))

The architecture's **single-agent** component is somewhat less strongly supported than its deterministic-controller component, because 2026 multi-agent work such as BOAD provides credible evidence that optimized delegation can improve SWE performance. But that evidence is not sufficient to say that a project focused on controlled RAW-vs-tuned debugger experiments was required to adopt multiple agents. ([arxiv.org](https://arxiv.org/html/2512.23631v2))

I would therefore state the conclusion in the report approximately as:

**“The architecture deliberately separates probabilistic reasoning from deterministic execution control. Current evidence supports this separation: recent debugger-agent systems report substantial difficulty with low-level stateful debugger protocols and achieve better results through agent-oriented state abstractions and middleware. Although emerging multi-agent systems demonstrate promising gains in broader software-engineering benchmarks, existing evidence does not establish multi-agent orchestration as necessary for debugger-informed repair, and comparisons are frequently confounded by additional inference and scaffolding. A single-agent architecture therefore remains a defensible controlled baseline, while debugger-specific post-training, higher-level dynamic interfaces, stronger base models, and compute-matched multi-agent variants constitute appropriate future work.”**

---

# 11. Evidence table

| Work | Year / status | Task | Dynamic tools/evidence | Actual debugger? | Multi-agent? | Training? | Main evidence | Relevance to this project | Main limitation |
|---|---|---|---|---|---|---|---|---|---|
| **ChatDBG** | 2025, FSE, peer-reviewed | Human-in-loop debugging | Stack/runtime inspection | **Yes: Pdb/GDB/LLDB** | No | No | Python actionable fix 67%, 85% after follow-up ([arxiv.org](https://arxiv.org/abs/2403.16354)) | Proof that active debugger use can work | Assistant setting; limited autonomous repo repair |
| **LDB** | 2024, Findings ACL, peer-reviewed | Code self-repair | Intermediate variable/block traces | No interactive debugger | No | No | Up to +9.8% over baselines ([arxiv.org](https://arxiv.org/abs/2402.16906)) | Dynamic state improves repair | Generated-code benchmarks; passive trace collection |
| **NExT** | 2024, ICML, peer-reviewed | Runtime reasoning/repair | Execution traces | No | No | **Execution-aware self-training** | +26.1pp MBPP, +10.3pp HumanEval repair ([proceedings.mlr.press](https://proceedings.mlr.press/v235/ni24a.html)) | Strong evidence runtime skills benefit from targeted training | Not tool/PDB trajectory training |
| **REval** | 2025, ICSE, peer-reviewed | Runtime-behavior reasoning | Program execution states | No | No | Evaluation only | Avg runtime reasoning 44.4%, IC 10.3 ([arxiv.org](https://arxiv.org/abs/2403.16437)) | Runtime understanding is not automatically reliable | Diagnostic benchmark, not repair system |
| **SWE-agent** | 2024, NeurIPS, peer-reviewed | Repo SWE | Search/edit/test/program execution | No | No | No | Custom ACI improves agent interaction ([arxiv.org](https://arxiv.org/abs/2405.15793)) | Typed/LM-oriented interfaces matter | Historical benchmark numbers |
| **Agentless** | 2024/25, peer-reviewed ASE version | Repo repair | Validation/tests | No | No autonomous agent | No | Simple fixed phases competitive historically ([arxiv.org](https://arxiv.org/abs/2407.01489)) | Deterministic workflow can be effective | Old models/results; candidate sampling |
| **PatchPilot** | 2025, ICML, **peer-reviewed** | Repository-level software repair | Reproduction/tests/validation | No interactive debugger | No | No | Rule-based reproduction → localization → generation → validation → refinement workflow; designed for efficacy, stability, and cost-efficiency ([PMLR](https://proceedings.mlr.press/v267/li25cf.html)) | Complementary evidence that LLMs need not control every orchestration decision | Does not establish optimality of a deterministic debugger controller |
| **debug-gym** | 2025, preprint/environment | Interactive repair | Code execution and state | **PDB** | No | Environment, not principal training result | PDB usefulness highly model/scaffold dependent ([arxiv.org](https://arxiv.org/abs/2503.21557)) | Closest early autonomous PDB environment | Not a conclusive SOTA repair study |
| **InspectCoder** | 2026, PACMPL/OOPSLA1, peer-reviewed | LLM-generated program repair | Breakpoints, state inspection, perturbation | **Yes** | **Dual-agent** | Prompted strategies, no debugger SFT | 67.87% BCB-R vs 58.86% without debugger; middleware critical ([arxiv.org](https://arxiv.org/html/2510.18327v1)) | Direct match to stateful-PDB problem | Self-repair rather than repo-scale SWE |
| **ADI / FramePilot** | 2026, FSE, peer-reviewed | SWE-bench repair | Function-level lifetime traces | PDB baseline + custom dynamic interface | No | No | Claude 3.7: 55.0 static, 55.8 PDB, **63.8 ADI** ([arxiv.org](https://arxiv.org/html/2604.24212v1)) | Strongest evidence for state-aware deterministic abstraction | Custom trace interface ≠ conventional PDB |
| **DebugHarness** | 2026, **preprint** | C/C++ security/vulnerability repair on SEC-bench | Live memory state, execution paths, crash-driven dynamic introspection | **Yes: GDB-oriented dynamic debugging plus pwndbg/rr tooling** | No | No debugger-specific post-training reported | ~90% patch success; >30% relative improvement over compared baselines, according to the paper ([arxiv.org](https://arxiv.org/abs/2604.03610)) | Strengthens evidence that productive runtime debugging can materially improve repair | C/C++ security domain and substantially different scaffold/tooling; not directly comparable to Python/QuixBugs/PDB |
| **Debug2Fix** | 2026, preprint | GitBug-Java/SWE-Bench Live | Interactive state | **JDB/PDB** | Debug subagent | No targeted SFT | Direct tools flat/harmful; scaffolded subagent improves results ([arxiv.org](https://arxiv.org/html/2602.18571v1)) | Shows raw debugger exposure is insufficient | Large token increase; multi-agent/interface confounded |
| **SWE-Gym** | 2025, ICML, peer-reviewed | SWE agent training | Tests/executable repo | No debugger | No requirement | **Trajectory SFT + verifier** | Up to 19pp gains ([proceedings.mlr.press](https://proceedings.mlr.press/v267/pan25g.html)) | Strong evidence tool behavior needs agent training | Not debugger-specific |
| **Open-SWE-Traces** | 2026, preprint | SWE trajectory distillation | Full agent/tool trajectories | No debugger focus | Harness dependent | **SFT/distillation** | 207k trajectories; cross-harness penalties; unsuccessful trajectories useful ([arxiv.org](https://arxiv.org/html/2606.16038v1)) | Strong evidence against automatic action-space transfer | Preprint; 30B-class models |
| **SWE-Master** | 2026, technical report | Repo SWE | Real execution feedback | No debugger focus | No requirement | SFT + RL | Full post-training pipeline includes execution RL ([arxiv.org](https://arxiv.org/html/2602.03411v1)) | Supports explicit tool/process post-training | Package-level result; contribution attribution difficult |
| **BOAD** | 2026, ICLR, peer-reviewed | SWE-bench | Standard SWE tools | No | **Yes** | Agent-design optimization | 49.8→53.2 Verified with ~same tokens; manual hierarchy worse ([arxiv.org](https://arxiv.org/html/2512.23631v2)) | Strongest multi-agent counterargument | Not debugger-specific; architecture searched |
| **SGAgent** | 2026, TOSEM accepted | Repo repair | Repo retrieval/validation | No | **Yes** | No | Claude 3.5 51.3% Lite ([arxiv.org](https://arxiv.org/abs/2602.23647)) | Shows useful role decomposition | No compute-matched equivalent sequential single-agent |
| **AgentForge** | 2026, preprint | SWE-bench Lite | Mandatory tests/execution | No interactive debugger | **Yes, five roles** | No | 40% vs 14% single-agent; but ~2.7× tokens ([arxiv.org](https://arxiv.org/html/2604.13120v1)) | Execution feedback + structured workflow useful | Strong multi-agent/compute confound |
| **DeLM** | 2026, preprint | SWE-bench test-time scaling | Shared verified context | No | **Yes** | No | Up to 10.5pp gain; lower reported cost ([arxiv.org](https://arxiv.org/abs/2606.10662)) | Serious emerging multi-agent evidence | Parallel test-time scaling, not debugger experiment |
| **SWE-TRACE** | 2026, preprint | SWE agents | Execution/process rewards | No | Auxiliary evaluator | **SFT + RL/PRM** | 60K curated trajectories + process-reward pipeline ([arxiv.org](https://arxiv.org/abs/2604.14820)) | Supports trajectory/process post-training future work | Preprint; debugger transfer untested |

---

# 12. Full citations and primary links

1. Levin, K. H., van Kempen, N., Berger, E. D., & Freund, S. N. **ChatDBG: Augmenting Debugging with Large Language Models.** *Proceedings of the ACM on Software Engineering / FSE*, 2025. [DOI](https://doi.org/10.1145/3729355) · [arXiv](https://arxiv.org/abs/2403.16354). ([dl.acm.org](https://dl.acm.org/doi/10.1145/3729355))

2. Zhong, L., Wang, Z., & Shang, J. **Debug Like a Human: A Large Language Model Debugger via Verifying Runtime Execution Step-by-Step.** *Findings of ACL 2024*. [DOI](https://doi.org/10.18653/v1/2024.findings-acl.49) · [arXiv](https://arxiv.org/abs/2402.16906). ([arxiv.org](https://arxiv.org/abs/2402.16906))

3. Ni, A. et al. **NExT: Teaching Large Language Models to Reason about Code Execution.** *ICML 2024*. [PMLR](https://proceedings.mlr.press/v235/ni24a.html). ([proceedings.mlr.press](https://proceedings.mlr.press/v235/ni24a.html))

4. Chen, J. et al. **Reasoning Runtime Behavior of a Program with LLM: How Far Are We?** *ICSE 2025*. [DOI](https://doi.org/10.1109/ICSE55347.2025.00012) · [arXiv](https://arxiv.org/abs/2403.16437). ([dl.acm.org](https://dl.acm.org/doi/10.1109/ICSE55347.2025.00012))

5. Yang, J. et al. **SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering.** *NeurIPS 2024*. [arXiv](https://arxiv.org/abs/2405.15793). ([arxiv.org](https://arxiv.org/abs/2405.15793))

6. Xia, C. S., Deng, Y., Dunn, S., & Zhang, L. **Agentless: Demystifying LLM-based Software Engineering Agents.** [arXiv](https://arxiv.org/abs/2407.01489); subsequently published in ASE. ([arxiv.org](https://arxiv.org/abs/2407.01489))

7. Yuan, X. et al. **debug-gym: A Text-Based Environment for Interactive Debugging.** 2025 preprint. [arXiv](https://arxiv.org/abs/2503.21557). ([arxiv.org](https://arxiv.org/abs/2503.21557))

8. Wang, Y. et al. **InspectCoder: Dynamic Analysis-Driven Self Repair through Interactive LLM-Debugger Collaboration.** *Proceedings of the ACM on Programming Languages 10, OOPSLA1*, 2026. [DOI](https://doi.org/10.1145/3798238) · [arXiv](https://arxiv.org/abs/2510.18327). ([dl.acm.org](https://dl.acm.org/doi/10.1145/3798238))

9. Xiang, J. et al. **Empowering Autonomous Debugging Agents with Efficient Dynamic Analysis.** *Proceedings of the ACM on Software Engineering / FSE*, 2026. [DOI](https://doi.org/10.1145/3797126) · [arXiv](https://arxiv.org/abs/2604.24212). ([dl.acm.org](https://dl.acm.org/doi/10.1145/3797126))

10. Garg, A., & Huang, J. **Debug2Fix: Supercharging Coding Agents with Interactive Debugging Capabilities.** 2026 preprint. [arXiv](https://arxiv.org/abs/2602.18571). ([arxiv.org](https://arxiv.org/html/2602.18571v1))

11. Pan, J. et al. **Training Software Engineering Agents and Verifiers with SWE-Gym.** *ICML 2025*. [PMLR](https://proceedings.mlr.press/v267/pan25g.html). ([proceedings.mlr.press](https://proceedings.mlr.press/v267/pan25g.html))

12. Ahmad, W. U., Ludwig, N., Majumdar, S., & Ginsburg, B. **Open-SWE-Traces: Advancing Dual-Mode Multilingual Distillation for Software Engineering Agents.** 2026 preprint. [arXiv](https://arxiv.org/abs/2606.16038). ([arxiv.org](https://arxiv.org/abs/2606.16038))

13. **SWE-Master: Unleashing the Potential of Software Engineering Agents through Post-Training.** 2026 technical report. [arXiv](https://arxiv.org/abs/2602.03411). ([arxiv.org](https://arxiv.org/html/2602.03411v1))

14. Xu, I. et al. **BOAD: Discovering Hierarchical Software Engineering Agents via Bandit Optimization.** *ICLR 2026*. [OpenReview](https://openreview.net/forum?id=O6stE173BD) · [arXiv](https://arxiv.org/abs/2512.23631). ([openreview.net](https://openreview.net/pdf?id=O6stE173BD))

15. Mao, Y., & Mirhoseini, A. **Decentralized Multi-Agent Systems with Shared Context.** 2026 preprint. [arXiv](https://arxiv.org/abs/2606.10662). ([arxiv.org](https://arxiv.org/abs/2606.10662))

16. Zhang, Q. et al. **SGAgent: Suggestion-Guided LLM-Based Multi-Agent Framework for Repository-Level Software Repair.** Accepted to *ACM TOSEM*, 2026. [arXiv](https://arxiv.org/abs/2602.23647). ([arxiv.org](https://arxiv.org/abs/2602.23647))

17. Kumar, R. et al. **AgentForge: Execution-Grounded Multi-Agent LLM Framework for Autonomous Software Engineering.** 2026 preprint. [arXiv](https://arxiv.org/abs/2604.13120). ([arxiv.org](https://arxiv.org/abs/2604.13120))

18. Han, H. et al. **SWE-TRACE: Optimizing Long-Horizon SWE Agents Through Rubric Process Reward Models and Heuristic Test-Time Scaling.** 2026 preprint. [arXiv](https://arxiv.org/abs/2604.14820). ([arxiv.org](https://arxiv.org/abs/2604.14820))

19. Sun, M., Yang, Y., Liu, X., Zhou, Y., & Xu, B. **DebugHarness: Emulating Human Dynamic Debugging for Autonomous Program Repair.** April 2026 **preprint**, arXiv:2604.03610. [arXiv](https://arxiv.org/abs/2604.03610).

20. Li, H., Tang, Y., Wang, S., & Guo, W. **PatchPilot: A Cost-Efficient Software Engineering Agent with Early Attempts on Formal Verification.** *Proceedings of the 42nd International Conference on Machine Learning (ICML 2025)*, PMLR 267:35922–35941. [PMLR](https://proceedings.mlr.press/v267/li25cf.html) · [OpenReview](https://openreview.net/forum?id=ybODpT8ydV).

## Bottom line for the internship closeout

The literature does **not** require you to retrofit the project into a multi-agent system or claim that the failed RAW/cp118 runs invalidate debugger-aware repair.

It gives you a substantially cleaner conclusion:

**The project successfully built the deterministic machinery required for real stateful debugging, but its two tested model policies did not demonstrate the learned interaction competence required to reach usable runtime evidence. That outcome is consistent with the current literature, which increasingly finds that runtime information is valuable but stateful debugger use is a distinct agent capability shaped by model strength, interface abstraction, and trajectory-specific training. The existing single-agent + deterministic-controller + typed-tool architecture remains a defensible experimental baseline; debugger-specific trajectory training and higher-level state-aware tool abstractions are the most directly evidence-backed next steps, while multi-agent orchestration is a secondary controlled ablation rather than a missing prerequisite.** ([arxiv.org](https://arxiv.org/html/2604.24212v1))
