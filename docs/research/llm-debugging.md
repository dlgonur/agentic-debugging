# LLM-Based Debugging Literature Review v1

Status: repository-grounded partial review for instructor-item review. This
document does not claim that the named literature set is complete.

## 1. Scope and inclusion rules

This review covers tracked repository evidence about language-model systems
that diagnose, explain, evaluate, or repair program failures. A study is
included when the repository contains a primary-paper manual note or a tracked
manifest/synthesis that identifies the source. The review distinguishes four
roles:

- **debugging**: reasoning about why observed behavior fails, including runtime
  or debugger interaction;
- **repair**: generating or applying a code change intended to correct behavior;
- **evaluation**: measuring a model/system on debugging or software-engineering
  tasks;
- **benchmark construction**: defining tasks, test oracles, and evaluation
  protocols rather than providing a debugging agent.

The review uses only tracked repository material. It does not use web research,
untracked PDF text as a separate citation, or general memory to reconstruct an
absent study. The local paper manifests say that PDFs under the Tier 1 and Tier
2 paper directories are intentionally ignored by Git; therefore the durable
evidence used below is the corresponding tracked manual notes and manifests.

The inclusion rule is deliberately conservative. A paper can be relevant to
LLM-based debugging without being a debugger, and a successful test result is
reported as the study's evaluation signal rather than upgraded to proof of
semantic correctness.

## 2. Evidence availability map

| Named work | Tracked durable evidence | Role supported by available evidence | Availability decision |
|---|---|---|---|
| ChatDBG | `research/notes/2024_chatdbg_notes.md`; Tier 1 manifest | Interactive LLM debugger assistant; diagnosis and fix recommendation | Included in study review |
| debug-gym | `research/notes/2025_debug_gym_notes.md`; Tier 1 manifest | Interactive debugging environment and agent evaluation | Included in study review |
| LDB / Debug Like a Human | `research/notes/2024_ldb_notes.md`; Tier 2 manifest | Runtime-execution-guided LLM debugging and regeneration | Included in study review |
| SWE-bench | `research/notes/2023_swe_bench_notes.md`; Tier 1 manifest | Repository-level issue-to-patch benchmark and evaluation | Included as benchmark evidence, not a debugger |
| Self-Debugging | No tracked primary-paper note, source manifest entry, or paper path found; the tracker marks it unread | Cannot establish method, results, or limitation from the repository | Missing; no factual study claims made |
| DebugBench | No tracked primary-paper note, source manifest entry, or paper path found; the tracker marks it unread | Cannot establish method, results, or limitation from the repository | Missing; no factual study claims made |

The absence decisions are consistent with `docs/project-tracker.md` Phase 1.2
and the corresponding item discussion in
`docs/archive/status/instructor-status-map.md`. They are not claims that
the studies do not exist outside this repository. They mean only that this
review cannot responsibly summarize them from the permitted local evidence.

Other tracked notes—Agentless, SWE-Agent, AutoCodeRover, RepairAgent, OpenHands—
are used for architectural context where relevant, but the instructor-named
LLM-debugging review centers on the four available primary notes above.

## 3. Study-by-study review

### 3.1 ChatDBG

**Source and role.** The tracked note identifies ChatDBG as “ChatDBG:
Augmenting Debugging with Large Language Models,” with the earlier arXiv title
“ChatDBG: An AI-Powered Debugging Assistant.” It is an LLM-powered interactive
debugger assistant, not a benchmark-only paper and not a complete autonomous
repair/verifier pipeline. Evidence: `research/notes/2024_chatdbg_notes.md`.

**Interaction model and context.** A user asks a natural-language question in a
debugger session. ChatDBG builds an initial prompt from instructions, an
enriched stack trace, inputs when available, error description, prior debugger
history, and the question. Subsequent turns include new history and the new
user message. The model can return prose and controlled debugger function calls.

**Execution and debugger feedback.** The model can “take the wheel” by issuing
debugger commands through the integration. The underlying debugger executes the
command, returns output, and the model reasons from it. The note records support
for Pdb, LLDB, and GDB, with security controls such as sanitization/whitelisting
and restrictions on native function calls. This is direct debugger feedback,
not merely a post-hoc stack trace pasted into a prompt.

**Localization and root-cause reasoning.** The user question, enriched stack,
and interactive commands support causal diagnosis. The note emphasizes that
ChatDBG can explain crashes and recommend fixes, but root-cause and proximate
crash fixes must be distinguished. It does not supply this project's structured,
independent RCA metric.

**Repair loop and tools.** The model may investigate through debugger commands
and recommend a code fix. The tracked note explicitly characterizes ChatDBG as
not directly editing code or autonomously applying and validating patches. Its
tool is therefore a debugger-interaction interface inside an assistant loop,
not the repository's complete patch lifecycle.

**Evaluation and reported contribution.** The note describes a Python study of
22 unpublished student programs and a C/C++ study of eight real-world native
bugs. For Python, the configurations vary stack enrichment, take-the-wheel,
targeted questions, and one follow-up dialog; success is a manual judgment that
the answer explains the error and gives an actionable fix. The note reports
57% for a simple “why?” prompt, 67% with a targeted question, and 85% with one
additional dialog step. It reports 0–12 debugger commands per run and a
then-current interaction estimate of about 10,000 tokens, 25 seconds, and
$0.12 for the targeted question. These are study-specific reported figures,
not current prices or a cross-paper ranking.

For C/C++, the note reports manual judgments of 36% root-cause fixes and an
additional 55% proximate-cause fixes. This distinction is useful for the
project because stopping a crash is not equivalent to repairing the deeper
semantic cause.

**Limitations relevant here.** The Python tasks are student programs rather
than large professional repositories. Manual explanation/fix judgment is
subjective even with predefined criteria. The note says ChatDBG does not
directly compare with Agentless, SWE-Agent, or AutoCodeRover on a common
benchmark and does not apply/validate patches through tests. Prompt length,
model choice, leakage for some native cases, and unintended new bugs remain
risks.

### 3.2 debug-gym

**Source and role.** The tracked note describes debug-gym as “A Text-Based
Environment for Interactive Debugging.” It is an interactive environment and
evaluation framework for debugging agents, rather than a single fixed repair
agent. Evidence: `research/notes/2025_debug_gym_notes.md`.

**Interaction model and context.** The environment contains a repository,
terminal, working directory, modular toolbox, and optional tests. At each step,
an agent receives a textual observation and emits one text action. The formal
framing is a partially observable sequential decision process: repository and
runtime state are not fully visible, and actions reveal observations.

**Execution, debugger, and tool feedback.** The recorded tool set includes
`eval`, `view`, `pdb`, `rewrite`, and `listdir`, with custom tools possible. A
typical loop resets the environment, prompts a model, parses one action, calls
the environment, and feeds back the next observation and reward. PDB is a
first-class tool, but access is still mediated by the environment and action
protocol.

**Localization and root-cause reasoning.** The environment supports actions
that can reveal source, tests, and runtime state; it does not make a separate
formal RCA score the primary contract in the tracked note. The reward is 1 when
rewritten code passes tests and 0 otherwise, so passing tests are an outcome
signal rather than proof of explanation correctness.

**Repair loop and budgets.** The note records a 50-step interaction budget and
10 rewrite attempts, with three runs per experiment and average/standard
deviation reporting. The environment terminates on success, interaction-budget
exhaustion, or rewrite-budget exhaustion. This explicit budget is relevant to
the repository's controller and PDB policy.

**Benchmark/evaluation setup and reported contribution.** The note covers Aider
(133 Python Exercism-style tasks), Mini-nightmare (10 hand-crafted buggy Python
examples), and SWE-bench-Lite (300 curated repository-level tasks). The primary
metric is test-based success and the efficiency measure is rewrite count. It
records a qualitative pattern: PDB adds less value to simple Aider tasks, while
selected Mini-nightmare and SWE-bench-Lite configurations benefit from
interactive debugging, especially with capable models and delayed debugger
access. The note reports the listed SWE-bench-Lite table, including a 52.1%
debug(5) result for Claude 3.7 Sonnet, but stresses that these are minimal
baseline agents rather than a universal performance claim.

**Limitations relevant here.** Visible tests can be overfit. Success rate does
not capture quality or brittleness, and a reviewer agent is proposed as future
work. The current sequential tool flow may not represent more complex
parallel/DAG flows. The focus is Python/PDB, and current models may lack enough
sequential debugging trajectory data. These limitations support hidden or
independent validation and bounded controller actions in this project.

### 3.3 LDB / Debug Like a Human

**Source and role.** The tracked note describes LDB as “Debug like a Human: A
Large Language Model Debugger via Verifying Runtime Execution Step by Step.” It
is an execution-feedback-guided debugging and regeneration method for generated
programs. Evidence: `research/notes/2024_ldb_notes.md`.

**Interaction model and context.** LDB starts from a task description and
visible tests. An LLM generates a seed program. If it fails, the system builds
or uses a control-flow graph, decomposes execution into basic blocks, and
collects before/after variable states for executed blocks.

**Execution feedback and reasoning.** For each selected block, the model sees
the block code, the state before it, the state after it, the task description,
and the failed-test behavior. It produces a structured verdict—whether the
block is correct and an explanation. This is a runtime trace/state protocol,
but it is not direct interactive PDB command use.

**Localization, repair loop, and tools.** The block decomposition localizes
reasoning at a semantic execution unit rather than a repository file or line.
The verdicts are fed back to regeneration, repeating until visible tests pass or
the debugging budget is exhausted. The loop therefore combines program
generation, runtime profiling, stepwise diagnosis, and regeneration. It does
not solve repository-scale issue localization, patch application in a mature
repository, or pass-to-pass regression management as a primary contract.

**Evaluation and reported contribution.** The tracked note records HumanEval,
MBPP, and TransCoder experiments with GPT-3.5, CodeLlama 34B, and StarCoder.
It reports consistent baseline improvements of up to 9.8% and selected table
differences, while attributing the gain to runtime information rather than
self-generated explanation or imagined execution. The review preserves this
as the note's reported result, not evidence that LDB dominates other systems
under a common task distribution.

**Limitations relevant here.** LDB requires executable visible tests, works on
generated short programs rather than repository-scale bugs, and requires
runtime execution. Test-case-free debugging remains open. The note explicitly
says it is not a real interactive debugger adapter and does not directly use
PDB commands. Its strongest transfer lesson is that semantically meaningful,
bounded runtime observations can be more useful than unconstrained self-talk.

### 3.4 SWE-bench

**Source and role.** SWE-bench is included as benchmark construction and
evaluation evidence, not as a debugger system. The tracked note describes an
issue-plus-repository snapshot input and a patch output evaluated by fail-to-pass
(F2P) and pass-to-pass (P2P) tests. Evidence:
`research/notes/2023_swe_bench_notes.md`.

**Interaction model and context.** The model receives an issue and codebase
context, with retrieval varying by setup, and emits a patch. The benchmark
connects issue reports, repository snapshots, and tests into a reproducible
patch-evaluation task. It does not require an interactive debugger or expose a
PDB trajectory as its core contract.

**Execution feedback and validation.** A patch is “resolved” in the tracked
formulation when it applies, all F2P tests pass, and all P2P tests remain
passing. Apply rate alone is explicitly weaker than resolved status. This
separation is directly relevant to the independent verifier in this project.

**Localization, RCA, repair loop, and tools.** SWE-bench makes localization and
patch generation difficult at repository scale. Retrieval and execution are
evaluation components, but the benchmark does not provide a runtime-debugger
RCA label or require the model to explain the causal chain. The original notes
describe simple retrieval/patch-generation baselines and report that models can
be distracted by long context, miss relevant files, generate short/simple
patches, and benefit from execution feedback.

**Evaluation and reported contribution.** The note records the original
BM25/oracle-retrieval results and the benchmark's failure taxonomy. Those
figures demonstrate the difficulty of repository-level issue resolution under
the recorded setups; they are not an LLM-debugger comparison and are not used
here to rank debugging families.

**Limitations relevant here.** Tests are an important but incomplete oracle;
passing tests do not guarantee code quality or unseen correctness. The tasks
are repository-level rather than debugger-centered, the environment is heavy
for the first PDB MVP, and some issue descriptions involve multimodal context.
The project's use of SWE-bench evidence is therefore methodological: base
commit, patch application, F2P/P2P, execution feedback, and failure taxonomy.

### 3.5 Self-Debugging — unavailable evidence

The repository's Phase 1.2 tracker marks Self-Debugging unread, and the tracked
file inventory contains no primary-paper note, local-paper manifest entry, or
source record for it. This review consequently does not state its method,
interaction model, benchmark, results, or limitations. The only safe conclusion
is an evidence gap: a future reviewed note or primary source is required before
including it in the comparison matrix as a study.

### 3.6 DebugBench — unavailable evidence

The repository's Phase 1.2 tracker marks DebugBench unread, and the tracked
file inventory contains no primary-paper note, local-paper manifest entry, or
source record for it. This review consequently does not state its method,
interaction model, benchmark, results, or limitations. The only safe conclusion
is an evidence gap: a future reviewed note or primary source is required before
including it in the comparison matrix as a study.

## 4. Comparison matrix

| Study | Debugging/repair/evaluation role | Interaction model | Repository/file context | Execution feedback | Debugger/runtime feedback | Localization behavior | Root-cause reasoning | Repair loop | Tool use | Validation/oracle | Benchmark/evaluation setup | Contribution and project-relevant limitation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ChatDBG | Debugging assistant; recommends fixes | User ↔ LLM ↔ debugger, with take-the-wheel calls | Debugger session and enriched stack; not a repository repair harness | Stack, inputs, command history, command outputs | Direct Pdb/LLDB/GDB interaction | Interactive evidence can refine suspected location; no structured project FL metric | Natural-language explanation; manual root/proximate distinction | Investigation and recommendation; no autonomous patch/verifier loop | Controlled debugger commands | Manual success judgment; no direct patch/test authority | 22 unpublished Python programs; eight native bugs | Direct prior art for PDB interaction; limited by assistant scope and manual judgment |
| debug-gym | Debugging-agent environment and evaluation | Sequential text actions over observations | Repository, terminal, working directory, tools, optional tests | `eval`, tests, observations, reward | PDB is a first-class tool | Actions can inspect source and runtime; formal RCA score not central | Agent must infer from partial observations; reward is test-based | Rewrites until tests pass or budget ends | Modular built-ins and custom tools | Test-based reward; reviewer/hidden-test gaps remain | Aider, Mini-nightmare, SWE-bench-Lite; fixed budgets and repetitions | Blueprint for controller/tool policy; immediate PDB can be unhelpful for simple tasks |
| LDB | Debugging and regeneration method | Model judges executed blocks and regenerates program | Short generated programs; not mature repositories | Traces and before/after block states | Runtime state, not direct interactive PDB | Block-level execution decomposition | Per-block verdict and explanation | Generate → profile → judge blocks → regenerate | CFG/profiling and model calls | Visible-test pass; no primary P2P repository verifier | HumanEval, MBPP, TransCoder | Runtime grounding lesson; limited repository localization and patch lifecycle |
| SWE-bench | Benchmark construction and repair evaluation | Issue/context → patch → test execution | Repository snapshot/base commit | F2P/P2P and execution outcomes | No required debugger state | Retrieval/localization is a measured difficulty, not a debugger trace | No native RCA label; passing tests do not prove RCA | Model emits patch; evaluator runs it | Retrieval and patch interface | Apply + F2P + P2P resolution contract | Real GitHub issues and repository-level tasks | Supplies verifier vocabulary and difficulty; not an LLM debugger |
| Self-Debugging | Evidence unavailable | Not established from tracked sources | Not established | Not established | Not established | Not established | Not established | Not established | Not established | Not established | Not established | Do not infer from name; source review remains open |
| DebugBench | Evidence unavailable | Not established from tracked sources | Not established | Not established | Not established | Not established | Not established | Not established | Not established | Not established | Not established | Do not infer from name; source review remains open |

## 5. LLM debugging versus static completion and one-shot repair

Static code completion maps a prompt and source context to a code continuation.
A one-shot repair system asks for one patch and may execute tests afterward. An
LLM-based debugging system, in the stronger sense used here, is conditioned on
an observed failure and attempts to explain or repair it using evidence that
can change across steps.

The differences are operational rather than merely stylistic:

- **Evidence acquisition:** static completion sees only supplied context;
  debugging systems can receive a failing test, stack, trace, state snapshot,
  or debugger output.
- **Hypothesis revision:** one-shot repair commits to one response; an
  interactive loop can inspect a variable, test a hypothesis, and revise the
  diagnosis.
- **Causal target:** completion optimizes plausible code continuation; debugging
  asks where and why an observed behavior fails and whether the change addresses
  that cause.
- **Tool boundary:** completion may have no tools. ChatDBG and debug-gym show
  that model/debugger interaction is a separate capability; the controller
  must constrain it.
- **Validation:** one-shot repair commonly has a final test outcome. A bounded
  debugging system records intermediate evidence and can distinguish no-op,
  patch failure, regression, timeout, and unresolved behavior before deciding.
- **Data needed:** static completion can be trained from text/code pairs.
  Debugging agents additionally need reliable failure contexts, tool traces,
  runtime observations, and verifier-backed outcomes if they are to learn
  sequential debugging behavior.

These distinctions do not imply that every multi-turn LLM is effective. The
tracked debug-gym note explicitly records cases where extra PDB access added
steps without helping, and the ChatDBG note records sensitivity to prompts,
model choice, and manual evaluation. Interaction must therefore be an
experimentally controlled factor.

## 6. Implications for the controller/PDB/verifier architecture

### 6.1 Controller boundary

The project should retain a single controller with typed directives and a
bounded state machine. The model may propose a hypothesis, tool action, or
unified-diff patch, but deterministic policy decides whether that action is
allowed. The controller should record the action, observation, budget effect,
and next state. This separates model capability from execution authority.

The tracked Tier 2 synthesis describes the target as:

```text
agent/controller
  -> typed action
  -> sandboxed/runtime execution
  -> structured observation
  -> event log
  -> next action
```

That boundary captures the useful part of debug-gym without requiring an
unbounded generalist or multi-agent platform.

### 6.2 PDB path

ChatDBG supports the value of model-visible debugger interaction; debug-gym
supports treating PDB as a first-class action/tool; LDB supports bounded
runtime-state observations. Together they motivate a PDB path that exposes
structured stack, frame, local-variable, source-window, stepping, and safe
expression results.

The controller should gate PDB rather than open it blindly. Candidate policies
include PDB after a failed static attempt, PDB when localization confidence is
low, and PDB for failures whose cause depends on values or control flow. The
static/test-feedback policy remains a baseline. PDB expression evaluation must
be restricted, with no raw shell execution and no unconstrained function calls
by default. These are repository design decisions, not claims that every
external paper uses the same safety policy.

### 6.3 Patch and verifier path

SWE-bench supplies the useful F2P/P2P vocabulary. The repository should retain
the stronger separation between:

1. baseline reproduction;
2. allowed-path and unified-diff application;
3. syntax and patch-integrity checks;
4. fail-to-pass behavior;
5. pass-to-pass regression behavior;
6. full-suite consistency where configured;
7. cleanup and evidence completeness.

The independent verifier, not the LLM, should decide the terminal outcome. A
passing test result is evidence of that test behavior; it is not by itself an
RCA certificate. Explanation quality and localization should remain separate
metrics, and missing evidence should remain missing rather than becoming a
default success.

### 6.4 Evaluation design

The most informative first comparison is a paired static/test-feedback policy
versus a delayed or uncertainty-gated PDB policy, holding task, model,
prompt-visible fields, budgets, patch contract, and verifier constant. Useful
outcomes include resolved status, F2P, P2P, full-suite consistency, localization
category, blinded RCA rubric, PDB attempted/opened/usable/evidence-linked
counts, tool and patch counts, wall-clock time, and genuinely reported token
fields. The dataset/evaluation decision records the current gaps in RCA
scoring, statement-level localization, PDB reachability, and environment
fingerprinting; those gaps limit broad claims.

## 7. Limitations and unresolved-source record

### 7.1 Evidence limitations

- This is a consolidated review of tracked manual notes, not a new web search or
  systematic bibliometric survey.
- The local primary PDFs are ignored by Git according to the manifests. The
  review is therefore auditable through tracked notes and manifest records, but
  a future reviewer may need the local paper library to recheck quotations or
  metadata.
- The studies use different tasks, models, prompts, test visibility, metrics,
  and success definitions. Reported percentages are not directly comparable.
- ChatDBG uses manual success judgments; debug-gym and SWE-bench use test-based
  outcomes; LDB reports benchmark pass improvements. None is a complete common
  RCA/correctness protocol.
- The review does not infer a causal benefit from runtime evidence across all
  bugs. The tracked evidence supports a narrower, testable hypothesis.

### 7.2 Missing named sources

Self-Debugging and DebugBench cannot be reviewed honestly from this checkout.
The precise gap is the absence of a tracked primary-paper note or source record,
combined with explicit unread status in `docs/project-tracker.md`. Required
next evidence is a locally recorded, source-grounded note for each study that
separates method, context, execution feedback, validation, reported findings,
and limitations. No tracker or checklist status is changed by this document.

### 7.3 Open claims from the repository

The claims-to-verify material identifies unresolved questions about frontier
debugger-control papers, benchmark validity, multi-agent superiority, dynamic
debugging, and fine-tuning. This review does not silently promote those claims
to findings. See `research/reports/synthesis/claims_to_verify_v1.md`.

## 8. Tracked repository evidence index

| Path | Evidence used |
|---|---|
| `research/notes/2024_chatdbg_notes.md` | ChatDBG bibliography, architecture, debugger interaction, evaluations, findings, and limitations |
| `research/notes/2025_debug_gym_notes.md` | Environment/action/observation model, PDB tool, benchmarks, results, budgets, and limitations |
| `research/notes/2024_ldb_notes.md` | Runtime block states, stepwise verdicts, regeneration loop, results, and limitations |
| `research/notes/2023_swe_bench_notes.md` | Benchmark task formulation, F2P/P2P oracle, retrieval, results, execution feedback, and limits |
| `research/papers/TIER1_LOCAL_MANIFEST.md` | Tier 1 source identities and local-paper availability policy |
| `research/papers/TIER2_LOCAL_MANIFEST.md` | Tier 2 source identities and local-paper availability policy |
| `docs/project-tracker.md` | Explicit read/unread status for LLM-debugging studies, including missing Self-Debugging and DebugBench work |
| `docs/archive/status/instructor-status-map.md` | Conservative instructor-item status and evidence gaps |
| `research/synthesis/pdb_debugger_agent_mvp_rationale.md` | Static/dynamic taxonomy, PDB rationale, APR distinction, and proposed experiment policies |
| `research/synthesis/tier2_mvp_architecture_update.md` | Controller, typed tools, event stream, PDB, verifier, and metric implications |
| `docs/archive/reports/final-report-v1.md` | Current architecture, verifier authority, reproducibility and claim boundaries |
| `docs/datasets/selection.md` | F2P/P2P mapping, localization/RCA metric gaps, and external-evaluation limitations |
| `research/reports/synthesis/claims_to_verify_v1.md` | Claims intentionally left unresolved |

The evidence index is a review aid. It does not claim completion of the
instructor's LLM-literature item, because two named sources remain unavailable
and the tracked status map still treats the item as partial.
