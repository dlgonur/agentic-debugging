# Paper Notes — OpenHands

## Bibliography

- Title: OpenHands: An Open Platform for AI Software Developers as Generalist Agents
- Former name: OpenDevin
- Venue/status in PDF: Published as a conference paper at ICLR 2025
- arXiv: 2407.16741v3
- Date in PDF: 18 Apr 2025
- Local PDF path: research/papers/tier2_core_sections/2024_openhands_generalist_agents.pdf
- Access level: CORE_AND_RELEVANT_APPENDIX_SECTIONS_READ
- Focus of these notes: platform architecture, runtime/sandbox, event stream, AgentSkills/tool design, multi-agent delegation, evaluation framework, quality-control tests, and implications for a Python/PDB debugger-agent controller.

## Why this paper matters

OpenHands is not primarily a debugging paper. It is a general platform paper for building AI software developers as agents.

Its value for our project is architectural:

1. It shows a general event-stream abstraction for actions and observations.
2. It separates agent logic from runtime execution.
3. It uses a sandboxed Docker runtime for code execution.
4. It exposes software-engineering tools through a reusable skills library.
5. It supports multiple agents and delegation.
6. It includes benchmark integration and quality-control tests for agent systems.
7. It treats software-agent development itself as a software-engineering problem.

For our Python/PDB project, OpenHands is useful as a platform reference, not as the immediate implementation target. The MVP should borrow:

- event-stream logging,
- action -> observation runtime model,
- sandbox discipline,
- skill/tool library concept,
- deterministic integration tests with mocked LLM outputs,
- human interrupt/feedback idea.

But the MVP should not try to become a full OpenHands-like generalist platform.

## Core contribution

OpenHands introduces a community-driven platform for generalist and specialist AI agents that interact with the world through software.

The paper describes agents that can:

- write code,
- interact with a command line,
- browse the web,
- run Python/IPython,
- operate in a sandboxed runtime,
- use reusable skills/tools,
- be evaluated across many benchmarks,
- delegate subtasks to other agents.

The platform provides:

1. Agent abstraction.
2. Event stream architecture.
3. Docker-based runtime.
4. AgentSkills library.
5. Multi-agent delegation.
6. Evaluation framework.
7. GUI / CLI / IDE-style interfaces.
8. Integration tests and quality-control machinery.

## System overview

OpenHands has three main components:

```text
Agent
  reads state/event history
  produces next action

Event Stream
  stores user messages, agent actions, observations, metadata

Runtime
  executes actions in sandbox
  returns observations
```

The figure on page 3 illustrates this as:

```text
User Interface / CLI / IDE plugins
        |
      Agent
        |
   Action -> Event Stream -> Runtime
        |
   Observation -> Event Stream -> Agent
```

The event stream is the central abstraction: every action and observation is recorded.

## Agent abstraction

An agent in OpenHands:

```text
state/event history -> next action
```

The agent sees:

- prior actions,
- observations,
- user messages,
- cost metadata,
- delegation metadata,
- execution parameters.

It then outputs one of several action types, such as:

```text
CmdRunAction
IPythonRunCellAction
BrowseInteractiveAction
MessageAction
AgentFinishAction
```

The minimal agent example in the paper:

1. builds messages from system prompt + event history,
2. calls an LLM,
3. parses the response,
4. returns a typed action.

Project implication:

Our PDB MVP should use the same separation:

```text
Controller/Agent:
  decide next typed action

Runtime:
  execute action safely

Event log:
  persist actions, observations, tests, patches, and final result
```

Do not mix LLM prompting, PDB subprocess control, patching, and test execution into one unstructured script.

## Event stream

The event stream is a chronological collection of:

- user messages,
- agent actions,
- observations,
- feedback,
- metadata.

This matters because agent debugging requires reconstructing a trajectory:

```text
what did the agent see?
what did it decide?
what did the tool return?
why did it patch this?
why did validation fail?
```

Project implication:

Every PDB MVP run should save a trajectory:

```json
[
  {"type": "user_task", "content": "..."},
  {"type": "run_tests_action", "command": "..."},
  {"type": "run_tests_observation", "status": "failed", "trace": "..."},
  {"type": "get_frame_locals_action", "frame": 0},
  {"type": "get_frame_locals_observation", "locals": {...}},
  {"type": "patch_action", "patch": "..."},
  {"type": "test_observation", "outcome": "..."}
]
```

This is needed for evaluation, debugging, and later preference data.

## Runtime / sandbox architecture

OpenHands runtime uses Docker containers.

Main runtime pieces:

```text
Docker sandbox
  Bash shell
  Jupyter/IPython server
  Chromium browser via Playwright
  OpenHands action execution API
```

Workflow:

```text
1. user provides base Docker image
2. OpenHands builds an OH runtime image with runtime API client/server code
3. container launches from that image
4. backend sends actions through REST API
5. runtime client executes actions in sandbox
6. runtime returns observations to event stream
```

Important runtime duties:

- execute shell commands,
- execute Python/IPython code,
- handle file operations,
- manage current working directory,
- manage loaded plugins,
- return consistently formatted observations.

Project implication:

For MVP, we do not need the full OpenHands runtime. But we should adopt the same boundary:

```text
controller process
  -> action execution layer
      -> isolated working copy / subprocess / optional Docker
  <- observation
```

Especially for PDB:

```text
PDB command execution should be behind an action API,
not directly in the LLM prompt.
```

## Runtime image reproducibility

OpenHands uses a dual Docker image tagging scheme:

```text
hash-based tag:
  exact runtime source + Dockerfile content

generic tag:
  stable latest build for a base image + OpenHands version
```

The hash-based tag supports reproducibility; the generic tag supports convenience and caching.

Project implication:

When we later build reproducible experiments, each bug task should record:

```text
Python version
dependency lock / environment hash
dataset task id
base commit
test command
agent version
tool version
model version
trajectory id
```

Without this, experiment results will be hard to reproduce.

## AgentSkills library

OpenHands has an AgentSkills library: a reusable toolbox imported into the IPython environment.

Design philosophy:

- Do not wrap every possible Python package.
- Add a skill only when:
  1. it is not readily achievable for an LLM to write directly, or
  2. it requires external model/tool integration.

Supported skills include:

```text
open_file
goto_line
scroll_down
scroll_up
create_file
edit_file
search_dir
search_file
find_file
parse_pdf
parse_docx
parse_latex
parse_audio
parse_image
parse_video
parse_pptx
```

Project implication:

Our PDB tools should be treated as an AgentSkills-like library:

```text
get_stack_summary
get_frame_locals
safe_eval_expression
get_source_window
set_breakpoint
continue_to_breakpoint
apply_patch
run_tests
revert_patch
```

Only add tools that give the model a reliable higher-level capability. Do not expose every possible PDB command in v1.

## AgentSkills lesson for PDB

Good skill design:

- small number of tools,
- typed arguments,
- clear docstrings,
- stable output,
- narrow purpose,
- safe defaults.

Bad skill design:

- raw shell,
- raw PDB command,
- huge output,
- hidden side effects,
- unconstrained eval,
- ambiguous result formatting.

Therefore, MVP PDB skills should look like:

```python
def get_frame_locals(frame_index: int, max_repr_chars: int = 200) -> Observation:
    ...

def safe_eval_expression(frame_index: int, expression: str) -> Observation:
    # parse AST, reject calls/assignments/imports/attributes if unsafe
    ...
```

## Browser/tool action design

The BrowserGym appendix is less central for our project, but it shows a useful pattern:

- many browser actions are documented as Python-like functions,
- examples are included,
- actions have typed arguments,
- the agent may execute multiple actions in one turn only when no feedback is needed.

Project implication for PDB:

```text
Multiple debugger actions in one turn may be allowed only when they are pure observation actions and do not depend on intermediate results.

Allowed batch:
  get_stack_summary()
  get_frame_locals(0)
  get_source_window(frame=0)

Not allowed batch:
  set_breakpoint(...)
  continue()
  safe_eval_expression(...)
  patch()
```

This avoids long feedback-dependent action chains.

## Multi-agent delegation

OpenHands supports `AgentDelegateAction`, where one agent delegates a subtask to another specialized agent.

Example:

- CodeActAgent delegates web browsing to BrowsingAgent.

Project implication:

Not for MVP.

Our MVP should remain single-controller. Multi-agent delegation can come later only if there is a clear need:

```text
one agent for retrieval
one agent for PDB diagnosis
one agent for patch review
```

But before that, single-controller experiments are more interpretable and easier to evaluate.

## GUI / human feedback

OpenHands includes a GUI where users can:

- view files,
- check executed bash/Python actions,
- observe browser activity,
- interact with the agent,
- interrupt and provide feedback.

Project implication:

For our research prototype, GUI is out of scope. But human-interrupt semantics are valuable:

```text
agent can stop and request human clarification if:
  - issue lacks reproduction
  - environment cannot be set up
  - patch requires product decision
  - runtime evidence is ambiguous
```

## Evaluation framework

OpenHands integrates 15 benchmarks across:

- software engineering,
- web browsing,
- miscellaneous assistance.

Software benchmarks include:

```text
SWE-Bench
HumanEvalFix
BIRD
BioCoder
ML-Bench
Gorilla APIBench
ToolQA
```

Web benchmarks include:

```text
WebArena
MiniWoB++
```

Miscellaneous benchmarks include:

```text
GAIA
GPQA
AgentBench
MINT
Entity Deduction Arena
ProofWriter
```

Project implication:

We should not use a broad benchmark suite for MVP. But we should adopt the framework idea:

```text
one runner
multiple task sources
common trajectory format
common result schema
```

First benchmark family:

```text
curated Python/PDB bugs
small BugsInPy subset
HumanEvalFix-style simple bugs for smoke tests
```

Later benchmark family:

```text
SWE-bench Lite subset
debug-gym-style tasks
```

## Software-engineering results

OpenHands reports CodeActAgent v1.8 on SWE-Bench Lite:

```text
gpt-4o-mini-2024-07-18: 7.0%
gpt-4o-2024-05-13: 22.0%
claude-3-5-sonnet@20240620: 26.0%
```

For HumanEvalFix Python:

```text
OH CodeActAgent v1.5 + gpt-4o: 79.3%
```

Interpretation:

- OpenHands is competitive but not necessarily best at SWE-bench.
- Its paper value is platform generality, not state-of-the-art debugging.
- Generalist agents can do software tasks, but specialist interfaces can still matter.

Project implication:

Our system should not compete as a generalist agent. It should be a specialist debugger agent evaluated on bugs where runtime state matters.

## Quality-control / integration tests

This is one of the most important implementation lessons.

OpenHands includes end-to-end agent tests because agent systems are complex and minor changes can degrade behavior.

Testing approach:

- define tasks and expected outputs,
- run through OpenHands,
- compare output against gold files,
- intercept LLM calls,
- provide predefined responses based on exact prompt matches,
- reduce nondeterminism and cost,
- test prompt regression, action execution, sandbox behavior, and message passing.

Project implication:

Our PDB MVP needs tests from day one:

```text
unit tests:
  patch parser
  source window extraction
  safe eval rejection
  event log serialization

integration tests:
  mocked LLM -> fixed sequence of PDB actions
  run against tiny buggy Python script
  verify trajectory and final patch outcome

golden trajectory tests:
  given known model responses,
  ensure controller produces expected actions and observations
```

This is more important than adding features early.

## Ethics / safety

OpenHands states that agents are still research artifacts and may pose security risks as they improve. It mitigates risks by:

- enabling systematic evaluation,
- facilitating human-agent interaction,
- supporting safety research.

Project implication:

The PDB agent must have explicit safety limits:

```text
sandbox execution
no arbitrary host shell access
safe expression evaluation
bounded runtime/test timeout
bounded patch attempts
human escalation for destructive actions
logs for auditability
```

## Limitations and future work

OpenHands future directions:

- enhanced multimodality,
- stronger agents,
- improved long-file editing,
- web browsing improvements,
- automatic workflow generation,
- graph-based agent frameworks.

The most relevant limitation for us:

```text
agent editing still suffers on long files
```

Project implication:

Do not start by editing long files. Use:

```text
small source windows
localized functions
Search/Replace or JSON patch
syntax validation
patch reversion
```

## What applies to our project

Strongly reusable:

1. Event stream architecture.
2. Action -> observation model.
3. Separation of agent/controller and runtime.
4. Docker/sandbox concept.
5. Runtime API boundary.
6. AgentSkills library concept.
7. Small typed tools.
8. Tool docstrings/examples.
9. GUI/user-interrupt as future feature.
10. Multi-agent delegation as later extension.
11. Evaluation framework idea.
12. End-to-end integration tests with mocked LLM.
13. Runtime image/environment reproducibility.
14. Event/trajectory logging.
15. Cost tracking.
16. Agent quality control / prompt regression tests.

## What does not apply directly

Not directly reusable for MVP:

- broad generalist agent platform scope,
- web browser integration,
- multimodal parsing skills,
- large benchmark suite,
- multi-agent delegation,
- arbitrary bash/IPython freedom,
- GUI implementation,
- benchmark leaderboard focus,
- full OpenHands runtime complexity.

## Relation to previous papers

### Compared with SWE-Agent

SWE-Agent gives a narrow ACI for software engineering.

OpenHands generalizes this into a platform:

```text
SWE-Agent:
  specialized ACI for repo editing

OpenHands:
  event stream + runtime + skills + agents + evaluation platform
```

Our PDB agent should borrow SWE-Agent’s focused command design and OpenHands’s event/runtime architecture.

### Compared with RepairAgent

RepairAgent gives repair-specific state machine and hypothesis/control tools.

OpenHands gives platform infrastructure.

Combined lesson:

```text
state-guided repair controller
+
event-stream runtime architecture
+
testable skill library
```

### Compared with AutoCodeRover

AutoCodeRover gives AST/program-structure retrieval.

OpenHands gives how to package reusable tools and runtime execution.

Combined lesson:

```text
AST retrieval tools should be skills/actions
and every retrieval result should be logged as an observation.
```

### Compared with LDB

LDB gives runtime-state reasoning.

OpenHands gives action/observation infrastructure for collecting that runtime state.

Combined lesson:

```text
PDB observations should be first-class event-stream observations.
```

### Compared with ChatDBG/debug-gym

ChatDBG/debug-gym motivate debugger interaction.

OpenHands explains how to build a reusable, sandboxed, observable runtime around such interactions.

## PDB MVP architecture update after OpenHands

The PDB MVP should have these modules:

```text
agent/
  controller.py
  prompts.py
  state_machine.py

runtime/
  workspace.py
  command_runner.py
  pdb_session.py
  patcher.py
  test_runner.py

skills/
  file_skills.py
  search_skills.py
  pdb_skills.py
  patch_skills.py
  test_skills.py

events/
  schema.py
  logger.py
  replay.py

evaluation/
  task_schema.py
  runner.py
  metrics.py
  outcome_taxonomy.py

tests/
  unit/
  integration/
  golden_trajectories/
```

## Proposed event schema

```json
{
  "run_id": "...",
  "task_id": "...",
  "timestamp": "...",
  "type": "action|observation|message|decision",
  "name": "get_frame_locals",
  "payload": {},
  "metadata": {
    "cost": 0.0,
    "duration_ms": 123,
    "tool_version": "..."
  }
}
```

## Proposed action classes

```text
RunTestsAction
GetFailureTraceAction
SearchCodeAction
OpenSourceWindowAction
GetStackSummaryAction
GetFrameLocalsAction
SafeEvalExpressionAction
ApplyPatchAction
RevertPatchAction
FinishAction
```

## Proposed observation classes

```text
TestResultObservation
TracebackObservation
SearchResultsObservation
SourceWindowObservation
StackSummaryObservation
FrameLocalsObservation
SafeEvalObservation
PatchApplyObservation
FinalOutcomeObservation
```

## MVP decision after reading OpenHands

OpenHands does not change the main research direction. It changes the engineering shape:

Before OpenHands:

```text
Build a PDB debugging agent.
```

After OpenHands:

```text
Build a small PDB debugging agent platform:
  typed actions,
  sandbox runtime,
  event stream,
  skills,
  verifier,
  integration tests.
```

Still out of scope:

```text
full generalist platform
web browser
multi-agent
GUI
OpenHands reimplementation
```

## One-paragraph Turkish explanation for my own understanding

OpenHands, tek bir debugging tekniği değil; AI software developer agent’ları için genel bir platform mimarisidir. Ana fikir, agent’ın geçmiş action/observation event stream’ini okuyup yeni action üretmesi, runtime’ın bu action’ı Docker sandbox içinde çalıştırıp observation döndürmesidir. Platform bash, IPython ve browser gibi human developer benzeri çalışma alanları sağlar; ayrıca AgentSkills library ile file edit/search/parse gibi tekrar kullanılabilir araçlar sunar. Bizim PDB projesi için ana ders şu: PDB agent’ı tek parça script gibi yazmak yerine action-observation event stream, sandboxed runtime, küçük typed skills, trajectory logging ve integration tests ile kurmalıyız. Fakat OpenHands gibi genel web/browser/multi-agent platformu yapmaya çalışmamalıyız; sadece PDB-debugging MVP için gerekli küçük mimariyi almalıyız.
