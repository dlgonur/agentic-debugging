# Automated Debugging Literature Survey v1

Status: tracked-repository foundations survey for review. This is a
consolidated, evidence-backed starting point, not a claim that the literature
or the repository's verification list is complete.

## 1. Scope and terminology

This survey uses the repository's tracked concept notes and reviewed manual
paper notes to connect six related areas:

- **debugging**: finding, understanding, and correcting a behavior failure;
- **automated debugging**: automating one or more diagnosis, localization,
  execution, explanation, or repair steps;
- **fault localization (FL)**: estimating the suspicious file, function, line,
  variable flow, or program region associated with a failure;
- **root-cause analysis (RCA)**: explaining the causal chain that produced the
  observed failure and why a change addresses it;
- **automated program repair (APR)**: producing or selecting a candidate patch,
  often using a fault model, search space, and test oracle;
- **agentic debugging**: a controller lets a model choose bounded sequences of
  repository, test, runtime, debugger, and patch actions.

These concepts overlap but are not interchangeable. FL can identify a useful
location without explaining the cause. A patch can be plausible without being
correct. An LLM can explain a failure without having authority to apply a patch.
An agent can use APR and a debugger without making the model an independent
correctness authority. The working definitions and initial project mapping are
recorded in `research/literature_notes_01.md`.

The evidence boundary is tracked repository content: manual notes, manifests,
design syntheses, claims-to-verify material, and project reports. Raw AI
research reports are secondary discovery material, not primary factual
authority. No web research or new dataset acquisition is used in this survey.

## 2. End-to-end debugging and repair pipeline

An end-to-end system can be represented as an evidence and decision pipeline:

```text
task / issue / failing test / crash
        |
        v
baseline reproduction and environment check
        |
        v
static evidence -> initial fault localization -> causal hypothesis
        |                                      |
        +---------- dynamic evidence ----------+
                   (trace, tests, runtime state, debugger)
        |
        v
candidate patch generation and constrained application
        |
        v
fail-to-pass + pass-to-pass + full-suite checks + evidence review
        |
        v
resolved / regression / invalid / unresolved outcome
```

The pipeline is a conceptual map, not a claim that every paper implements all
stages. Traditional debugging may keep every decision with a human. APR may
automate localization, patch search, and test filtering. LLM systems may
generate hypotheses or patches. Agentic systems make action selection and
observation management explicit. The repository's product path adds a strict
independent verifier after the model proposal.

Each stage has a distinct failure surface:

1. **Task and baseline**: the failure may not reproduce, or the environment may
   be unstable.
2. **Localization**: retrieval or suspiciousness may point to the wrong region.
3. **RCA**: the system may describe a symptom rather than the causal chain.
4. **Patch generation/application**: the patch may be malformed, out of scope,
   a no-op, or syntactically invalid.
5. **Validation**: visible tests may be weak, incomplete, or misclassified.
6. **Acceptance**: a controller or model claim may be mistaken for independent
   evidence.

Separating these stages supports the repository's fail-closed outcome taxonomy
and avoids treating an attractive explanation or a passing single test as a
complete repair result.

## 3. Taxonomy of major method families

### 3.1 Human/manual debugging

The human chooses hypotheses, searches source, runs tests, sets breakpoints,
inspects frames and locals, edits code, and judges the result. Its principal
strength is flexible causal reasoning across heterogeneous evidence. Its costs
are human time, subjective stopping criteria, limited scalability, and weak
reproducibility when commands and environment state are not recorded.

### 3.2 Static analysis and retrieval

Static methods use source, syntax, symbols, call relationships, repository
structure, issue text, or retrieved context without direct runtime state.
Agentless notes describe tree/skeleton representations, hierarchical
localization, retrieval, and fixed control flow. AutoCodeRover notes describe
AST/program-structure search and iterative context retrieval. Static evidence
is scalable and inexpensive, but it can miss state-dependent behavior and can
be distracted by broad context.

### 3.3 Dynamic analysis and execution-guided diagnosis

Dynamic methods use tests, logs, traces, instrumented execution, variable
states, or debugger observations. LDB records before/after states around
executed basic blocks. ChatDBG exposes debugger commands to an LLM. debug-gym
offers PDB as one environment tool. Dynamic evidence can reveal values and
control flow that source alone cannot, but it adds runtime cost, noise, safety
constraints, and environmental dependence.

### 3.4 Fault localization

FL ranks or selects suspicious program regions. Inputs may include failing-test
coverage, spectra, stack traces, issue text, retrieval, static analysis, or
runtime frames. Agentless performs hierarchical file-to-element-to-edit
localization. AutoCodeRover's notes describe SBFL-assisted retrieval when test
execution is available. PDB can provide a high-value failure-path signal, but a
stack frame is not automatically the root cause.

### 3.5 Automated program repair

APR generates candidate edits using templates, mutations, search, synthesis,
LLM output, or combinations. It then applies and evaluates candidates against
tests or other oracles. Search spaces and test strength strongly constrain the
result. Agentless uses localized Search/Replace edits and candidate sampling;
AutoCodeRover generates patches from structure-aware context; SWE-bench records
repository-scale patch evaluation vocabulary.

### 3.6 LLM-based diagnosis and repair

LLMs can summarize failures, propose causes, select relevant code, generate
patches, interpret traces, and use tools. LDB and ChatDBG are direct evidence
for runtime-conditioned reasoning. SWE-bench is primarily an issue-to-patch
benchmark, not a debugger. LLM output remains a proposal until execution and
independent validation supply stronger evidence.

### 3.7 Agentic and tool-using systems

An agentic system adds a sequential controller over actions and observations.
debug-gym formalizes this as a partially observable environment with an action
space, observation stream, rewrite budget, and interaction budget. The
repository narrows the design to one controller, typed deterministic tools,
bounded PDB, disposable workspaces, event trajectories, and an independent
verifier. More autonomy increases the available evidence but also adds tool,
state, budget, protocol, safety, and cleanup failure modes.

## 4. Fault localization methods and evidence

### 4.1 Static and structural localization

Static localization can operate from source structure, issue vocabulary,
retrieval, call relationships, ASTs, class/method skeletons, or manually
declared symbols. The Agentless notes describe three stages: suspicious files,
related elements, and concrete edit locations. The hierarchy narrows context
progressively and avoids giving the model an entire repository at once.

AutoCodeRover adds AST-based program representation, class/method/snippet
search, iterative retrieval, and optional SBFL-assisted context retrieval. The
notes record that SBFL can redirect attention away from distracting classes named
in an issue toward a method reached by failing tests. The evidence supports
dynamic analysis as a useful complement to textual hints, not a universal
replacement for static retrieval.

### 4.2 Test and spectrum evidence

Failing and passing tests provide behavioral constraints. Coverage or spectrum
signals can rank statements or methods associated with failing executions.
Generated reproduction tests can make an issue executable when the original
suite does not expose the failure. The Agentless notes report that generated
reproduction tests are useful but noisy and must be combined with regression
tests and fallbacks.

### 4.3 Trace, stack, and debugger evidence

Tracebacks and stacks identify the failure surface. Logs and traces extend the
path beyond the final exception. Runtime locals, selected expressions, and
breakpoint/step observations can test hypotheses about state. PDB is especially
relevant to Python because it can expose traceback, frames, locals, source
windows, expression evaluation, and exception state without native debug-symbol
work.

The repository's PDB rationale cautions that debugger access should be gated:
immediate access can be wasteful for simple static bugs, while state-dependent
bugs may need runtime observations. PDB evidence should therefore be logged as
an observation with a provenance and action, not flattened into an unexplained
model context.

### 4.4 Localization metrics

Localization should be scored separately from patch success. Candidate outcome
categories in the tracked synthesis include correct method/function, wrong
patch but correct method, wrong location in the correct file, wrong file, and
no patch. A future finer-grained protocol may score line/span or AST-node
targets, but the current repository does not claim validated statement-level
labels for external tasks. A passing patch cannot retroactively prove that the
agent's declared location or reasoning was correct.

## 5. Root-cause analysis versus localization

FL asks:

```text
Where should investigation or editing focus?
```

RCA asks:

```text
What causal chain produced the observed failure, and why does the repair fix it?
```

For example, `module.py:42` may be the observed crash location. RCA would need
to connect the relevant input, state transition, control-flow decision, and
incorrect value that made line 42 fail. A downstream exception location may be
useful for navigation while the root cause lies in an earlier state mutation.

The distinction changes what evidence is needed:

| Question | Typical evidence | Failure if treated as the other |
|---|---|---|
| Where is suspicious? | Retrieval, symbols, stack, coverage, SBFL, changed files | Search may be broad or edit the symptom location |
| Why did it fail? | Inputs, trace, locals, states, expected behavior, causal explanation | A plausible location or patch may hide the cause |
| Does the patch work? | Baseline reproduction, F2P, P2P, full-suite and integrity checks | A convincing explanation can still produce regression |

The repository proposes structured outputs containing localization, confidence,
observed runtime state, causal chain, failure explanation, and patch rationale.
RCA quality should be evaluated with a reviewed rubric or annotation; it must
not be inferred solely from a passing test.

## 6. Program-repair method families

### 6.1 Template, mutation, and search-based repair

Classical APR searches a constrained edit space, often guided by failing tests,
coverage, templates, or mutations. Its strengths are explicit search behavior,
repeatability, and a clear separation between candidate generation and oracle
evaluation. Its limitations include search explosion, fault-model mismatch,
patch overfitting, and dependence on test quality.

### 6.2 Fixed LLM-assisted pipelines

Agentless demonstrates a staged alternative to open-ended tool use:

```text
issue + repository
  -> hierarchical localization
  -> localized patch generation
  -> reproduction/regression validation
  -> patch selection
```

The tracked notes describe compact repository representations, Search/Replace
patches, candidate sampling, generated reproduction tests, regression filtering,
normalization, and majority voting. This style is a valuable static/test
feedback baseline because its control flow is comparatively fixed.

### 6.3 Structure-aware and spectrum-assisted repair

AutoCodeRover uses program-structure-aware retrieval and can use SBFL when tests
are available. Its notes distinguish plausible patches that pass tests from
correct patches that are semantically equivalent to the developer fix, using
manual validation in that study. The distinction illustrates why localization
and patch correctness are separate outcomes: a wrong patch can still touch the
correct method.

### 6.4 Runtime-state-guided repair

LDB uses execution states around blocks to produce model verdicts before
regeneration. ChatDBG uses interactive debugger commands to ground an assistant's
diagnosis. A Python/PDB repair agent would combine these lessons with repository
search and deterministic patching, but it should not assume that an observed
runtime value alone proves a generalized fix.

### 6.5 Agentic repair loops

An agentic repair loop lets a controller or model decide whether to inspect more
source, run a test, open PDB, apply a patch, revert, retry, or stop. The action
space must be bounded because incorrect intermediate actions can compound over
long trajectories. The project therefore treats a controller policy, typed tool
contracts, budgets, event records, cleanup, and independent verification as
part of correctness—not optional implementation detail.

## 7. Patch plausibility versus correctness

The repair literature uses several increasingly strong notions:

1. **Applicable**: the patch has a valid format and can be applied.
2. **Executable**: the patched project can be imported or run within limits.
3. **Plausible**: the available triggering/visible tests pass.
4. **Regression-preserving**: selected pass-to-pass tests remain passing.
5. **Consistent**: full-suite or additional independent checks do not contradict
   the claimed fix.
6. **Semantically correct**: the patch addresses the intended behavior without
   unintended changes, usually requiring stronger review or hidden tests.

SWE-bench's tracked formulation operationalizes resolution as patch application
plus F2P and P2P success. AutoCodeRover notes separately report plausible and
manually judged correct patches. Agentless notes show why generated reproduction
tests and regression checks are central but imperfect. The project should adopt
these layers without claiming that test success proves all unseen semantics.

Common overfitting modes include hardcoding visible examples, suppressing an
exception without repairing the cause, changing a test-visible branch while
breaking related cases, and applying a too-small patch that ignores repository
conventions. The verifier should expose these as distinct outcomes where the
available evidence supports the distinction.

## 8. Validation and correctness authorities

Validation evidence should be independent of the component being evaluated.
The controller and model can report hypotheses, but they cannot certify their
own success. The repository's independent verifier should own the terminal
behavioral result.

Minimum verifier sequence for a repair case:

```text
clean baseline
  -> reproduce original failure
  -> apply allowed candidate diff
  -> syntax/import check
  -> declared fail-to-pass test
  -> declared pass-to-pass regression tests
  -> full-suite check when configured
  -> cleanup and evidence-integrity check
```

The verifier should distinguish at least resolved, breaking-resolved,
partially-resolved, work-in-progress, no-op, regression, patch-apply failure,
syntax failure, timeout, test execution error, invalid baseline, and cleanup or
evidence-integrity failure as appropriate to the existing contract.

Runtime observations can improve a hypothesis but should not bypass the
verifier. Gold patches and hidden oracle fields must remain evaluator-only; they
are not model-visible evidence. A synthetic transport, scripted stand-in, or
successful infrastructure smoke is not a model repair result.

## 9. Evaluation dimensions and metrics

### 9.1 Behavioral repair outcomes

- resolved proportion under the independent verifier;
- fail-to-pass rate;
- pass-to-pass preservation;
- full-suite consistency;
- patch-application and syntax-failure rates;
- regression, timeout, no-op, and incomplete-evidence rates.

### 9.2 Localization and RCA

- file/function/method localization categories;
- statement or span accuracy only where reviewed labels are reliable;
- root-cause rubric: observed evidence, causal chain, expected behavior, and
  patch rationale;
- distinction between correct location/wrong patch and wrong location;
- whether the explanation cites actual runtime observations rather than guesses.

### 9.3 Runtime and tool behavior

- PDB attempted, opened, usable, and evidence-linked sessions;
- number and type of observations;
- tool calls, test runs, patch attempts, retries, and rejected unsafe actions;
- controller-state transitions and budget exhaustion;
- trajectory replay or deterministic event consistency;
- cleanup success and workspace immutability.

### 9.4 Cost and time

- wall-clock case duration;
- runtime command time;
- model request and retry counts;
- provider-reported token/cost fields only when genuinely observed;
- local computation and provider cost kept separate.

### 9.5 Benchmark and experimental validity

- pinned task/source revision and environment fingerprint;
- fixed model, prompt, policy, and budget configuration;
- per-task paired results, not only aggregates;
- repeated trials where model stochasticity matters;
- hidden or independent checks against visible-test overfitting;
- no leakage of gold patches, hidden tests, or evaluator-only oracle fields.

The dataset/evaluation decision records that the current implementation has
many behavioral, localization, PDB, and cost fields but still lacks sufficient
root-cause scoring, statement-level labels, external-environment fingerprints,
and contamination accounting for broad external model claims.

## 10. Relationship to LLM-based and agentic debugging

LLM-based debugging adds language-mediated hypothesis generation and code
synthesis to static or dynamic evidence. It differs from static completion when
the model must explain an observed failure, revise a hypothesis, or interpret a
runtime observation. LDB's block states and ChatDBG's debugger commands are
examples of evidence-conditioned interaction; SWE-bench is primarily a
repository-level repair/evaluation substrate.

Agentic debugging adds an action-selection layer. The model may decide to call
search, source retrieval, tests, PDB, or patch tools, but a safe implementation
must make the controller and runtime authoritative over action validity, paths,
budgets, timeouts, cleanup, and acceptance. debug-gym supplies the environment
and observation/action framing; the project's design deliberately narrows it to
a single controller and deterministic typed tools.

The relationship can be summarized as:

```text
static completion
  -> supplied source context
one-shot repair
  -> one proposed patch
LLM debugging
  -> failure-conditioned explanation/repair
tool-using agent
  -> sequential bounded evidence/actions
verified debugger agent
  -> all of the above plus independent acceptance
```

This hierarchy describes capability boundaries, not a performance ranking.
Dynamic evidence may help state-dependent bugs and add little to simple static
ones. The tracked research therefore frames PDB as a controlled factor against
a strong static/test-feedback baseline.

## 11. Relevance to the Python/PDB prototype

### 11.1 Why Python/PDB first

The tracked rationale identifies Python/PDB as the lowest-integration-cost
debugger target. It can expose traceback, current and previous frames, locals,
source windows, expression evaluation, and exception state without the native
debug-symbol and address-mapping requirements of C/C++. This is an engineering
scope decision for testing the runtime-evidence hypothesis, not a claim that
Python debugging generalizes automatically to all languages.

### 11.2 Proposed controlled loop

```text
1. Reproduce the baseline failure.
2. Collect traceback and test output.
3. Perform static source/localization actions.
4. Apply the PDB policy: skip, immediate, delayed, or uncertainty-gated.
5. Collect bounded stack/frame/locals/expression observations.
6. Ask for a structured root-cause hypothesis.
7. Ask for a constrained unified-diff patch.
8. Apply the patch deterministically in a disposable workspace.
9. Run F2P and P2P checks, then configured full-suite checks.
10. Accept, retry within budget, or fail with typed evidence.
```

### 11.3 Minimum tool and safety boundary

The tracked MVP rationale proposes tools for commands/tests, failure traces,
stacks, frames, locals, safe expression evaluation, source windows, symbols,
patch application, and patch reversion. The PDB boundary should reject raw shell
execution through the debugger, restrict expression calls, bound subprocesses,
and preserve disposable workspace cleanup. The model should cite observed
evidence in the RCA record.

### 11.4 Baselines and ablations

The initial comparison should include a static/test-feedback baseline and one or
more PDB policies: always-on, after a failed static attempt, and on uncertainty.
The same task, model, prompt-visible fields, tool budgets, patch contract, and
verifier must be used for a meaningful comparison. Expected outcomes are
hypotheses only: PDB may help state-dependent cases and may be unnecessary or
harmful for simple cases.

### 11.5 Dataset scope

The current rationale recommends a curated Python/PDB bug set or a small,
license-cleared BugsInPy slice for the first experiment, with SWE-bench as a
later repository-scale extension. The dataset decision keeps the existing
curated fixtures as the deterministic smoke gate and treats QuixBugs as an
infrastructure fallback, not evidence of realistic repository generalization.

## 12. Limitations and threats to validity

### 12.1 Literature evidence

- This survey is bounded by tracked notes and does not claim systematic search
  coverage.
- Manual notes and syntheses may preserve interpretation or metadata that still
  needs a primary-source recheck.
- The local paper PDFs are ignored according to the manifests; the durable
  citations in this document point to tracked notes and reports.
- Named studies use incompatible task distributions, models, prompts, oracles,
  and metrics. Their numbers must not be merged into a leaderboard.

### 12.2 Evaluation threats

- Weak or visible tests allow overfitting and can inflate plausibility.
- Gold-patch or training-data leakage can distort model comparisons.
- Manual semantic correctness judgments introduce subjectivity.
- Small task samples and stochastic model runs produce wide uncertainty.
- A model may reach the right file for the wrong causal reason.
- A runtime trace may be noisy, incomplete, or specific to one input.
- A provider's reported tokens or cost may be missing and must not be fabricated.

### 12.3 System threats

- Tool-use errors and long trajectories can compound bad intermediate actions.
- PDB expression evaluation can become an execution/safety boundary.
- Full repository environments add dependency, network, filesystem, resource,
  and cleanup risks.
- The trusted-local prototype boundary is not automatically hostile-code
  containment.
- A single controller limits coordination complexity but does not establish
  that multi-agent designs are inferior.

### 12.4 Claim boundary

The supported project claim is narrow: a controlled Python/PDB-first agent can
be evaluated on whether structured runtime evidence improves selected diagnosis
and repair cases over a static/test-feedback baseline. The survey does not
claim universal dynamic-debugging superiority, model superiority, complete
literature coverage, or completed external model evaluation.

## 13. Appendix: unresolved claims from tracked claims-to-verify material

This appendix preserves unresolved claims rather than converting them into
survey findings. Source: `research/reports/synthesis/claims_to_verify_v1.md`.

| ID | Unresolved claim/question | Current treatment in this survey |
|---|---|---|
| CTV-001 | ChatDBG author/title metadata required correction against official metadata | Use the corrected metadata in the tracked ChatDBG note; do not extend beyond that note |
| CTV-002 | Exact debug-gym benchmark numbers and delayed-PDB settings require primary-table verification | Report only the numbers recorded in the reviewed local note and label them study-specific |
| CTV-003 | FramePilot/ADI benchmark and debugger-control claims | Excluded from factual method/results claims; primary verification remains open |
| CTV-004 | Debug2Fix debugger subagent, datasets, models, and improvement claims | Excluded from factual method/results claims; primary verification remains open |
| CTV-005 | SWE-Doctor runtime-diagnosis and benchmark claims | Excluded from factual method/results claims; primary verification remains open |
| CTV-006 | EnIGMA interactive-debugger and CTF claims | Excluded from factual method/results claims; domain and tool use require primary verification |
| CTV-007 | SWE-bench leakage, weak-test, and variant-specific percentages | Do not merge numbers; contamination and benchmark-variant verification remains open |
| CTV-008 | Fine-tuning is mandatory for small local models to use debugger tools | Treat as unsupported for the MVP; revisit after trajectories and baselines |
| CTV-009 | Multi-agent systems are superior for debugging | Treat as mixed/unresolved; retain single-controller baseline first |
| CTV-010 | Dynamic debugging is always better | Reject the absolute claim; test task-, model-, and policy-dependent benefit |
| Experiment questions | PDB effect on correct fixes/RCA, useful observations, invocation timing, small-model tool use, and verifier strength | Require controlled experiments; no result is claimed here |

The appendix is intentionally not a completion checklist. It records what
remains uncertain and what evidence or experiment would be needed before a
stronger research claim could be made.

## 14. Tracked repository evidence index

| Path | Role in the survey |
|---|---|
| `research/literature_notes_01.md` | Core definitions and initial mapping to the project |
| `research/synthesis/pdb_debugger_agent_mvp_rationale.md` | Static/dynamic levels, FL/RCA, APR plausibility/correctness, PDB scope, tools, safety, and experiment policies |
| `research/synthesis/tier2_mvp_architecture_update.md` | Updated controller, typed tools, event stream, metrics, verifier, and scope |
| `research/notes/2023_swe_bench_notes.md` | Repository-scale issue/patch task, F2P/P2P, retrieval, execution feedback, and validity limits |
| `research/notes/2024_agentless_notes.md` | Hierarchical localization, fixed repair flow, reproduction tests, regression validation, and threats |
| `research/notes/2024_autocoderover_notes.md` | AST/context retrieval, SBFL, patch evaluation, plausible/correct distinction, and threats |
| `research/notes/2024_chatdbg_notes.md` | LLM/debugger interaction, runtime evidence, evaluation, root/proximate fixes, and limitations |
| `research/notes/2024_ldb_notes.md` | Execution traces, block states, stepwise verdicts, regeneration, results, and limitations |
| `research/notes/2025_debug_gym_notes.md` | Interactive environment, action/observation model, PDB, budgets, benchmarks, and limitations |
| `research/notes/2024_repairagent_notes.md` | Tool-using APR-agent context |
| `research/notes/2024_swe_agent_notes.md` | Agent-computer interface and guardrail context |
| `research/notes/2024_openhands_notes.md` | General tool/event/runtime context; not a scope recommendation |
| `research/papers/TIER1_LOCAL_MANIFEST.md` | Tier 1 source identities and ignored-PDF policy |
| `research/papers/TIER2_LOCAL_MANIFEST.md` | Tier 2 source identities and ignored-PDF policy |
| `research/reports/synthesis/claims_to_verify_v1.md` | Unresolved claims preserved in the appendix |
| `docs/datasets/selection.md` | Evaluation contract, metric gaps, containment and external-task limitations |
| `outdated/docs-archive/reports/final-report-v1.md` | Current architecture, verifier authority, boundaries, and interpretation limits |
| `outdated/docs-archive/status/instructor-status-map.md` | Conservative literature-item status and missing work |

The index identifies the evidence used. It does not assert that all literature
claims have been independently reverified or that the instructor item is fully
complete.
