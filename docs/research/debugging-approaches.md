# Debugging Approach Comparison v1

Status: research synthesis for review; not an empirical ranking of approach families.

## 1. Scope and terminology

This document compares four related families of debugging practice and systems:

1. **Traditional/manual debugging**: a human inspects source, tests, failures,
   traces, and runtime state, then chooses tools and edits.
2. **Automated debugging and automated program repair (APR)**: deterministic or
   search-based procedures automate one or more analysis, localization, patch
   generation, or test-validation steps. The family includes fixed pipelines as
   well as patch-search systems; it does not imply the use of an LLM.
3. **LLM-based debugging**: a language model explains a failure, proposes a
   repair, or iteratively reasons from supplied execution feedback. The model
   may be interactive, but the model alone is not necessarily an autonomous
   agent.
4. **Agentic/tool-using debugging**: a controller gives a model a bounded action
   space and lets it select sequences of repository, test, runtime, debugger, or
   patch tools. The system owns state, budgets, observations, and termination.

The boundaries overlap. An agentic system can contain APR components; an LLM
debugger can be used as a human assistant; and a human debugger can operate
automated analyzers. The comparison therefore describes the dominant control
model and evidence flow, not mutually exclusive software categories.

The repository's tracked notes define debugging as finding, understanding, and
fixing a behavior failure; automated debugging as automating parts of that
process; fault localization (FL) as estimating where suspicious code lies; and
program repair as generating a patch. See `research/literature_notes_01.md`.

## 2. Taxonomy of the four families

### 2.1 Traditional/manual debugging

The human owns the hypothesis, decides what evidence to collect, interprets
the evidence, edits the program, and decides whether the result is acceptable.
The workflow can be disciplined and reproducible when commands, inputs, and
edits are recorded, but the reasoning and stopping criteria are normally human
judgments. Interactive debuggers are especially valuable when the failure
depends on call-stack state, local values, control flow, or an exception state.

### 2.2 Automated debugging and APR

Automation decomposes debugging into procedures such as suspicious-location
ranking, trace analysis, mutation or template-based patch generation, patch
application, and test execution. Agentless is the repository's clearest
non-debugger example: its notes describe hierarchical localization, structured
repository representations, patch generation, reproduction tests, regression
filtering, and patch validation as a fixed staged pipeline. AutoCodeRover notes
add structure-aware context retrieval and optional spectrum-based fault
localization (SBFL). See `research/notes/2024_agentless_notes.md` and
`research/notes/2024_autocoderover_notes.md`.

Automation can be repeatable and cheap per run, but its behavior is determined
by the available analyses, templates, search space, tests, and heuristics. A
plausible patch that passes a weak test suite is not thereby a semantically
correct patch; the repository's APR discussion makes this distinction explicit
in `research/synthesis/pdb_debugger_agent_mvp_rationale.md`.

### 2.3 LLM-based debugging

An LLM can transform issue text, source, stack traces, tests, or runtime
observations into a diagnosis and patch proposal. ChatDBG is the direct tracked
example: the model receives enriched debugger context, can issue controlled
debugger commands through a take-the-wheel mechanism, and returns control to a
programmer. It is an interactive assistant rather than a complete autonomous
patch-and-verifier system. LDB is another LLM debugging pattern: it collects
execution states around basic blocks, asks the model for stepwise correctness
verdicts, and regenerates code from those verdicts. See
`research/notes/2024_chatdbg_notes.md` and `research/notes/2024_ldb_notes.md`.

LLM-based debugging is not the same as static code completion. Its defining
property is evidence-conditioned diagnosis or repair, often across multiple
steps. However, a one-shot prompt containing only source is still static
completion in this taxonomy, even if the requested output is called a fix.

### 2.4 Agentic/tool-using debugging

An agentic debugger makes the interaction loop explicit. The controller chooses
typed actions, receives structured observations, updates state, and enforces
budgets and policy. debug-gym models this as a partially observable sequential
decision process with a repository, tools, textual observations, actions,
rewrite and interaction budgets, and a test-based reward. The repository's
target is narrower: a Python/PDB-first single controller with deterministic
tools, event trajectories, disposable workspaces, patching, and an independent
verifier. See `research/notes/2025_debug_gym_notes.md`,
`research/synthesis/tier2_mvp_architecture_update.md`, and
`outdated/docs-archive/reports/final-report-v1.md`.

The extra capability is not simply “more autonomy.” It is explicit control over
what the model may do, what evidence it sees, when it may patch, and how a
candidate becomes accepted or rejected.

## 3. Comparison matrix

| Dimension | Traditional/manual | Automated debugging / APR | LLM-based debugging | Agentic/tool-using debugging |
|---|---|---|---|---|
| Task input and context | Human problem report, source, tests, traces, runtime state, domain knowledge | Structured program, tests, traces, static features, fault model, patch space | Prompt-visible issue, source, tests, traces, and optionally runtime observations | Task schema plus policy-filtered repository, test, runtime, and tool observations |
| Fault localization | Human hypothesis refined by inspection and experiments | Ranking, slicing, SBFL, retrieval, templates, or fixed stages | Model inference from text/code/execution evidence; may explain a location without a formal score | Controller/model actions combine static localization, failure path, PDB observations, and declared localization output |
| Root-cause reasoning | Human causal model; can ask targeted “why?” questions | Often implicit in suspiciousness or patch search; may not produce an explanation | Natural-language causal hypothesis; quality depends on evidence and evaluation rubric | Explicit hypothesis/state records can be tied to observations, actions, and later verifier evidence |
| Static evidence | Source, call graph, symbols, types, configuration, tests | Program analysis, retrieval, AST/CFG, SBFL, repository structure | Supplied source and retrieved context | Deterministic source/search tools plus model-directed but bounded retrieval |
| Dynamic evidence | Human runs tests, logs, traces, and debugger commands | Instrumentation, traces, test outcomes, mutation results, execution filters | Stack traces, execution traces, or debugger results if made visible | Structured command/test/PDB observations with budgets and event logging |
| Test/runtime/debugger interaction | Human chooses commands and interprets results | Predetermined or search-driven execution; debugger support is optional | May call a debugger in an interactive assistant, or reason over recorded traces | Controller gates test and PDB actions, records results, and decides whether another action is permitted |
| Tool use | Broad, context-sensitive, human controlled | Fixed APIs, analyzers, runners, patch operators | Tool use may be absent or embedded in a model interface | First-class typed tools, allowlists, validation, timeouts, and action budgets |
| Patch generation | Human edits and reviews | Templates, mutations, search, synthesis, or fixed patch format | Model proposes code or a patch; formatting and scope can fail | Model proposes a constrained diff; deterministic patch lifecycle applies/reverts it |
| Validation authority | Human judgment plus tests and review | Test oracle, static checks, ranking, or human review | Often tests or manual judgment; a passing test may be the only oracle | Independent verifier is authoritative; F2P, P2P, full-suite and patch-integrity evidence are separate outcomes |
| Autonomy and human control | Human retains control | Automation follows its configured procedure | Ranges from assistant to iterative model loop | Bounded autonomy: model proposes actions, controller enforces state, policy, limits, and terminal outcomes |
| Reproducibility | Depends on recorded inputs, environment, commands, and edits | Usually strong when inputs, versions, search seeds, and environment are pinned | Sensitive to model/version, prompt, context, stochasticity, and provider state | Records deterministic boundaries, trajectories, identities, budgets, and provider-field missingness where applicable |
| Computational/provider cost | Human time; machine cost usually modest | Analyzer, instrumentation, and patch-search cost; often predictable per configuration | Token, latency, model, and multi-turn provider cost can dominate | Adds model cost to repeated tool/runtime work; budgets and counts make the trade-off measurable |
| Benchmark suitability | Case studies and expert studies; hard to scale consistently | Good for controlled APR/FL comparisons if oracle and search space are specified | Good for diagnosis/repair studies when prompts, model, context, and rubric are fixed | Good for trajectory, policy, tool-use, verifier, and ablation studies, but requires strict environment and run identity |
| Common failure modes | Confirmation bias, incomplete exploration, missed regressions, undocumented steps | Fault-model mismatch, search explosion, overfitting, weak tests, invalid assumptions about the code | Hallucinated causes, shallow fixes, context distraction, malformed patches, overfitting visible tests | Tool misuse, observation overload, budget exhaustion, unsafe commands, retry loops, stale/contradictory evidence |
| Strengths | Flexible causal reasoning and high-context judgment | Repeatability, scalable search, explicit algorithms, low inference variance | Broad code/language knowledge and concise hypotheses; can synthesize heterogeneous evidence | Closed-loop evidence gathering, adaptive inspection, reproducible policy boundaries, and inspectable trajectories |
| Limitations | Expensive human attention; subjective and difficult to scale | Limited by analysis/search design and test oracle; may not explain runtime cause | Not a correctness authority; quality varies with model/context and may require external tools | More engineering, longer trajectories, safety surface, and need for independent verification |

The matrix is a synthesis rather than a claim that every system in a family
has every listed property. In particular, ChatDBG demonstrates interactive
debugger assistance, while debug-gym demonstrates an agent environment; neither
should be read as evidence that all LLM systems or all agents behave alike.

## 4. Detailed comparative analysis

### 4.1 Localization is not root-cause analysis

FL narrows the search space: file, function, method, line, or state transition.
Root-cause analysis (RCA) explains the causal chain that makes the observed
failure occur and why the proposed change addresses that chain. The distinction
matters in all four families. A suspicious line can be downstream of the real
state corruption, and a test-passing patch can suppress a symptom without
repairing the cause. The repository's example format therefore separates a
localization record from a root-cause statement and asks the verifier not to
infer RCA correctness merely from test success.

Traditional debugging can perform both activities through experiments. APR
often optimizes localization and patch outcome without producing a durable RCA.
An LLM can articulate RCA, but articulation is not evidence. An agentic system
can preserve the chain—observation, hypothesis, action, new observation,
patch, and verification—so the quality of the explanation can be reviewed
separately.

### 4.2 Static and dynamic evidence are complementary

Static retrieval scales across repositories and is a necessary baseline. The
SWE-bench notes warn that retrieval can miss the relevant file and that larger
contexts can distract the model. Dynamic evidence narrows the actual failure
path and exposes values or control flow unavailable in source alone, but it
costs execution time and may be noisy or unsafe.

LDB's block-level before/after states, ChatDBG's debugger observations, and
debug-gym's PDB tool are three different dynamic-evidence designs. They do not
establish that dynamic evidence always helps: debug-gym's notes report that
immediate PDB access did not clearly help simple Aider tasks and could hurt
weaker agents, while selected harder tasks benefited from interactive use. The
repository consequently treats PDB as a controlled experimental factor, not a
universal default.

### 4.3 Patch plausibility is weaker than correctness

An applied patch is a syntactic or operational event. A plausible patch passes
the available tests. A stronger correctness claim requires the original failure
to pass while regressions remain absent, and still does not prove all unseen
behaviors. SWE-bench's F2P/P2P formulation is useful because it separates the
triggering behavior from regression preservation; the repository adopts this
style in its verifier and reports limitations rather than treating a gold patch
or a model claim as proof.

Automated search can generate many plausible variants. LLMs can generate a
semantically attractive but malformed or overfit patch. An agentic controller
reduces operational ambiguity with constrained unified diffs and deterministic
application, but the independent verifier remains the acceptance authority.

### 4.4 Autonomy changes the error surface

Moving from a human to a fixed automation pipeline changes judgment into
algorithm and search errors. Moving from a model response to an agent adds
action-selection, state-tracking, tool-protocol, timeout, cleanup, and
observation-interpretation errors. More actions can expose more evidence, but
also create more opportunities for an unsafe or unproductive trajectory.

The repository's design response is a single controller, typed directives,
deterministic tools, fixed budgets, disposable workspaces, event trajectories,
and fail-closed verifier outcomes. This preserves a reviewable causal record
without introducing a multi-agent architecture whose benefit is not established
by the tracked evidence.

## 5. Repository-specific design implications

### 5.1 Single controller, not an unbounded agent swarm

The target is a Python/PDB-first single-controller system. The controller owns
the state machine and decides whether to gather source evidence, run tests,
open PDB, propose a patch, retry, or terminate. The model is a proposal and
reasoning component inside that boundary. This follows the MVP rationale and
the Tier 2 architecture update, both of which keep multi-agent orchestration
out of scope.

### 5.2 PDB is a bounded evidence source

PDB should be entered when the failure is state-dependent, localization is
uncertain, or a static attempt has failed—not blindly for every task. The PDB
path should expose structured stack, frame, locals, source-window, and safe
expression observations. Expression evaluation must be restricted; raw shell
execution and unconstrained function calls are not acceptable defaults. These
constraints are documented in `research/synthesis/pdb_debugger_agent_mvp_rationale.md`.

### 5.3 Deterministic tools separate proposal from execution

File reads, code search, test execution, patch application, and PDB interaction
should have typed contracts. The model may request an action, but the runtime
decides whether the request is allowed, executes it in the disposable workspace,
and emits a structured result. This makes malformed patches, timeouts,
forbidden paths, and rejected debugger expressions observable failures rather
than silent success.

### 5.4 The independent verifier is the correctness authority

The controller must not accept a patch because the model says it is fixed or
because one test happened to pass. The verifier should check baseline failure,
patch application and allowed paths, syntax, fail-to-pass behavior,
pass-to-pass regression behavior, full-suite consistency where configured, and
cleanup. It should retain distinct outcome categories so a tool failure is not
misreported as model success or model failure. See
`outdated/docs-archive/reports/final-report-v1.md` and
`docs/datasets/selection.md`.

### 5.5 Evaluation should compare policies, not slogans

The useful comparison is a controlled policy contrast such as static/test
feedback versus delayed PDB access on the same task, model configuration,
budgets, and verifier. Report resolution, F2P/P2P, localization, root-cause
rubric results, PDB reachability, tool counts, wall-clock time, and genuinely
reported provider usage separately. Infrastructure smoke results are not model
performance evidence, and success on small curated fixtures is not a claim of
repository-scale generalization.

## 6. Limitations and unresolved evidence

- The repository contains reviewed manual notes and local-paper manifests, but
  the comparison is a synthesis, not a new systematic search or meta-analysis.
- The tracked notes are not a common benchmark protocol. ChatDBG, LDB,
  debug-gym, SWE-bench, Agentless, and AutoCodeRover differ in tasks, models,
  metrics, test availability, and evaluation criteria; their numbers must not
  be ranked as a single leaderboard.
- Several reported outcomes use manual judgments, visible tests, or benchmark
  success as their primary signal. These do not fully measure semantic RCA or
  unseen correctness.
- Self-Debugging, DebugBench, and several frontier agentic systems remain
  explicitly unresolved in the status map and are not used here as factual
  evidence. See `outdated/docs-archive/status/instructor-status-map.md` and
  `research/reports/synthesis/claims_to_verify_v1.md`.
- The comparison does not prove that PDB improves aggregate repair success.
  The repository has an experimental hypothesis and infrastructure, not a
  completed cross-policy model study.
- Provider prices, current model behavior, and external benchmark revisions are
  outside this document's evidence scope. No web-only source is introduced.

## 7. Conclusion

The four families differ chiefly in who controls the debugging loop and what
evidence is available at each decision point. Manual debugging supplies
flexible causal judgment. Automated debugging and APR supply repeatable
procedures for localization, search, and validation. LLM-based debugging adds
language-mediated synthesis of code and execution evidence. Agentic debugging
adds a bounded sequential policy over tools and observations.

For this project, those distinctions justify a strict separation:

```text
model proposal
    -> deterministic typed tool execution
    -> runtime/PDB evidence
    -> constrained patch lifecycle
    -> independent verifier
```

The model should propose hypotheses, actions, and patches. Deterministic tools
should decide what can execute and produce structured evidence. PDB should add
runtime state where it is likely to resolve uncertainty. The independent
verifier should decide whether the candidate satisfies the behavioral contract.
This architecture tests the narrow research question—whether controlled runtime
evidence improves selected bug-repair cases—without treating autonomy, a
passing claim, or a single benchmark result as proof of correctness.

## 8. Tracked repository evidence index

| Evidence path | Use in this comparison |
|---|---|
| `research/literature_notes_01.md` | Working definitions of debugging, automated debugging, FL, and program repair |
| `research/notes/2024_chatdbg_notes.md` | Interactive LLM/debugger architecture, take-the-wheel interaction, evaluation, and limitations |
| `research/notes/2025_debug_gym_notes.md` | Tool/observation/action framing, PDB experiments, budgets, results, and trustworthy-agent limitations |
| `research/notes/2024_ldb_notes.md` | Runtime trace and stepwise execution-verification pattern |
| `research/notes/2023_swe_bench_notes.md` | Issue-to-patch formulation, F2P/P2P validation, retrieval, execution feedback, and limitations |
| `research/notes/2024_agentless_notes.md` | Fixed localization/repair/validation baseline |
| `research/notes/2024_autocoderover_notes.md` | Structure-aware retrieval, SBFL, patch generation, and plausible/correct distinction |
| `research/notes/2024_repairagent_notes.md` | Tool-using repair-agent state/tool design |
| `research/notes/2024_swe_agent_notes.md` | Agent-computer interface principles and guardrails |
| `research/notes/2024_openhands_notes.md` | General tool/event/runtime architecture context; not a claim that this project should reproduce it |
| `research/synthesis/pdb_debugger_agent_mvp_rationale.md` | Static/dynamic taxonomy, repository-vs-debugger comparison, APR distinction, PDB safety, and MVP hypothesis |
| `research/synthesis/tier2_mvp_architecture_update.md` | Single-controller, typed-tool, event, metric, and verifier-oriented architecture |
| `outdated/docs-archive/reports/final-report-v1.md` | Accepted repository architecture, evidence boundaries, verifier and reproducibility limits |
| `docs/datasets/selection.md` | F2P/P2P, localization/RCA metric gaps, containment and evaluation caveats |
| `outdated/docs-archive/status/instructor-status-map.md` | Current completion boundary and unresolved literature items |
| `research/reports/synthesis/claims_to_verify_v1.md` | Claims deliberately excluded or retained as unresolved |

The index identifies tracked evidence consulted for the synthesis; it does not
claim that every cited study or research question is fully verified.
