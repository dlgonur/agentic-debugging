# Paper Notes — RepairAgent

## Bibliography

- Title: RepairAgent: An Autonomous, LLM-Based Agent for Program Repair
- Authors: Islem Bouzenia, Premkumar Devanbu, Michael Pradel
- Affiliations: University of Stuttgart, UC Davis
- arXiv: 2403.17134v2
- Date in PDF: 28 Oct 2024
- Local PDF path: research/papers/tier2_core_sections/2024_repairagent_autonomous_llm_program_repair.pdf
- Access level: FULL_TEXT_READ

## Why this paper matters

RepairAgent is important because it is a program-repair-specific autonomous agent paper. It is not debugger-centered, and it targets Java/Defects4J, but it provides several design components that are directly useful for our Python/PDB MVP:

1. dynamic prompt/state memory,
2. repair-specific tool set,
3. finite-state guidance,
4. hypothesis control tools,
5. patch application with automatic test validation and revert,
6. ablations showing that search tools, state machine, and long-term memory matter.

For our project, RepairAgent is a reference for how to structure the agent loop around tools and repair attempts. It is also a warning: unconstrained autonomy needs guardrails, memory, state transitions, and budget limits.

## Core problem

Prior LLM-based automated program repair approaches usually fall into two categories:

1. one-shot repair:
   - prompt model once with buggy code,
   - receive fixed code.

2. hard-coded iterative repair:
   - try patch,
   - run tests,
   - feed error/test output back,
   - repeat in a fixed loop.

RepairAgent argues these loops are too rigid because real developers interleave:

- understanding the bug,
- searching for relevant code,
- gathering repair ingredients,
- forming hypotheses,
- applying patches,
- validating fixes.

The key move is to treat the LLM as an autonomous tool-using agent instead of just a patch generator.

## Core thesis

An LLM agent can repair programs more effectively when it can autonomously choose between developer-like tools:

```text
read code
search codebase
run tests
run fault localization
state hypothesis
discard hypothesis
generate/apply patch
validate patch
collect more information
declare done
```

The agent is not forced into a single fixed sequence. However, RepairAgent does not leave it completely unconstrained: it uses a finite state machine to guide which tools are available in each phase.

## System overview

RepairAgent has three main components:

1. LLM agent
2. Tool set
3. Middleware

Loop:

```text
dynamic prompt -> LLM chooses command -> middleware parses command -> tool executes -> output added to prompt -> repeat
```

The loop continues until:

- agent calls `goal_accomplished`, or
- cycle budget is exhausted.

Default cycle budget:

```text
40 cycles
```

## Dynamic prompt

The prompt consists of static and dynamic sections.

Static sections:

- role,
- goals,
- guidelines,
- output format.

Dynamic sections:

- state description,
- available tools,
- gathered information,
- last executed command/result.

Important design point:

The prompt is not a fixed “here is code, fix it” prompt. It is an evolving memory and control surface for the agent.

## Goals given to the agent

RepairAgent gives the agent five high-level goals:

1. Locate the bug.
2. Gather information about the bug.
3. Suggest simple fixes.
4. Suggest complex fixes if simple fixes fail.
5. Iterate over previous goals until a fix is found.

Project implication:

Our PDB agent should also have explicit phase goals. A good minimal phase model:

```text
reproduce failure
localize suspicious region
inspect runtime state
form root-cause hypothesis
patch
validate
retry or stop
```

## Finite state machine

RepairAgent uses a state machine because early experiments found that LLM agents can get lost in aimless exploration.

States:

1. Understand the bug
2. Collect information to fix the bug
3. Try to fix the bug
4. Done

The state machine constrains available tools, but does not enforce a strict linear path. The agent can move between states using control tools.

This is important for our project:

- We should not let the model freely call any tool at any time.
- We should not hard-code a single rigid loop either.
- Use state-guided autonomy.

## Tool set

RepairAgent provides 14 tools.

### Read and extract code

```text
read_range
get_classes_and_methods
extract_method
extract_tests
```

Purpose:

- inspect relevant file ranges,
- understand file structure,
- extract method implementations,
- inspect failing tests.

### Search and generate code

```text
search_code_base
find_similar_api_calls
generate_method_body
```

Purpose:

- search for relevant identifiers/snippets,
- find similar API usage,
- gather repair ingredients,
- generate candidate method bodies.

Important observation:

`find_similar_api_calls` helps avoid hallucinated method calls by grounding the model in existing codebase usage.

### Testing and patching

```text
run_tests
run_fault_localization
write_fix
```

Purpose:

- run test suite,
- retrieve fault localization information,
- apply patch and validate with tests.

Important `write_fix` behavior:

- applies patch,
- runs tests,
- reverts changes if tests fail,
- samples up to 30 variants of a suggested fix,
- removes duplicates,
- tests each variant.

### Control tools

```text
express_hypothesis
collect_more_information
discard_hypothesis
goal_accomplished
```

Purpose:

- manage state transitions,
- force hypothesis formulation,
- discard wrong hypotheses,
- return to information gathering,
- terminate when fixed.

Project implication:

Our PDB MVP should include explicit hypothesis tools or structured outputs:

```text
express_root_cause_hypothesis
discard_hypothesis
request_more_runtime_evidence
propose_patch
```

## Patch format

RepairAgent uses a JSON patch format rather than free-form diffs.

Patch operations include:

```text
insertions
deletions
modifications
```

For each file:

```json
{
  "file_path": "...",
  "insertions": [
    {
      "line_number": 175,
      "new_lines": ["..."]
    }
  ],
  "deletions": [179, 183],
  "modifications": [
    {
      "line_number": 179,
      "modified_line": "..."
    }
  ]
}
```

Project implication:

We need deterministic patch application. Options:

1. unified diff,
2. Agentless-style Search/Replace,
3. RepairAgent-style JSON edit operations.

For MVP, Search/Replace or JSON edits are probably easier to validate than whole-file rewrites.

## Middleware

The middleware is central. It:

1. queries the LLM,
2. parses the response,
3. maps imperfect tool names/argument names to valid tools,
4. rejects or reports invalid arguments,
5. detects repeated identical tool calls,
6. executes tool commands in isolated environments,
7. updates dynamic prompt sections.

Important implementation detail:

RepairAgent heuristically repairs malformed LLM tool outputs:

- maps predicted tool name to actual tool name via substring or Levenshtein distance,
- maps argument names similarly,
- maps invalid argument values when possible,
- if ambiguous, reports the issue back to the model.

Project implication:

Our MVP should not assume perfect JSON/tool calls. It needs:

- schema validation,
- command correction only when safe,
- repetition detection,
- invalid command feedback,
- strict budget.

## Implementation

Implementation details:

- Python 3.10,
- Docker for command isolation,
- built on AutoGPT framework,
- GPT-3.5-0125,
- Java parsing via ANTLR.

Project implication:

For us:

- Python project controller can be implemented in Python.
- Sandbox/isolation matters.
- PDB commands and test commands should execute in an isolated working directory.
- We should avoid AutoGPT-style broad autonomy; use smaller custom controller.

## Evaluation setup

Datasets:

1. Defects4J
   - 835 real-world Java bugs,
   - 17 Java projects,
   - 395 bugs from Defects4J v1.2,
   - 440 bugs from Defects4J v2.0.

2. GitBug-Java
   - 199 bugs from 55 projects,
   - discovered/fixed in 2023,
   - used to assess generalization/data leakage,
   - paper samples 100 bugs due to budget.

Baselines:

- ChatRepair,
- ITER,
- SelfAPR.

Metrics:

- plausible fixes,
- correct fixes.

Definitions:

- plausible fix: passes all tests,
- correct fix: syntactically matches developer patch or manually judged semantically consistent with developer patch.

This reinforces the APR distinction:

```text
plausible != correct
```

## Main results

Defects4J total:

```text
Bugs: 835
Plausible fixes: 186
Correct fixes: 164
```

Breakdown:

```text
Defects4J v1.2:
  plausible: 96
  correct: 74

Defects4J v2:
  plausible: 90
  correct: 90
```

Complexity of fixed bugs:

```text
single-line: 115
multi-line single-file: 46
multi-file: 3
```

Comparison:

- ChatRepair: 162 correct fixes,
- ITER: 57,
- SelfAPR: 110,
- RepairAgent: 164.

RepairAgent fixes 39 bugs not fixed by any baseline:

```text
18 single-line
20 multi-line
1 multi-file
```

Interpretation:

RepairAgent is especially useful for multi-line bugs because it can retrieve repair ingredients and edit arbitrary lines/files.

## GitBug-Java results

On 100 sampled recent bugs:

```text
single-line: 19 bugs, 11 plausible, 9 correct
multi-line: 64 bugs, 8 plausible, 4 correct
multi-file: 17 bugs, 0 plausible, 0 correct
total: 100 bugs, 19 plausible, 13 correct
```

Interpretation:

- Generalization exists, but performance drops on harder recent multi-line/multi-file bugs.
- Agent autonomy and tools are not enough to solve complex multi-file repair reliably.
- Multi-file repair remains hard.

## Cost

RepairAgent measures:

- wall-clock time,
- token consumption,
- monetary cost.

Reported median/average-style observations:

- median time per bug: 920 seconds,
- about 99% of time spent in tool execution, mostly running tests,
- median token consumption about 270,000 tokens,
- monetary cost about $0.14 per bug under GPT-3.5 pricing at the time.

Important nuance:

- unfixed bugs consume far more tokens because the agent keeps gathering information until budget exhaustion.
- fixed bugs have much lower token usage.

Project implication:

Unbounded or poorly terminated agent loops are expensive. The controller must detect diminishing returns.

## Ablation study

Ablations on 100 Defects4J bugs:

```text
No search tools:
  plausible 14, correct 11, cost $28

No state machine:
  plausible 18, correct 14, cost $31

Single-cycle memory:
  plausible 9, correct 6, cost $8

Realistic localization:
  plausible 16, correct 16, cost $29

Default RepairAgent:
  plausible 23, correct 21, cost $16
```

Lessons:

1. Search tools are critical.
2. State machine improves both success and cost.
3. Long-term memory matters.
4. Realistic fault localization reduces fixes and increases cost.
5. Perfect fault localization in many APR papers is optimistic.

Project implication:

For our MVP, the PDB tool will not remove the need for search/localization. We still need:

- code search,
- source windows,
- test extraction,
- memory,
- state machine.

## Tool usage analysis

RepairAgent uses:

- around 35 tool invocations per bug on average,
- most frequent tool: `write_fix`,
- fixed bugs average ~6 `write_fix` calls,
- unfixed bugs average ~17 `write_fix` calls,
- `run_tests` is rarely called directly because `write_fix` already runs tests.

Project implication:

Patch attempts dominate. We should set strict budgets:

```text
max patch attempts: 3
max test runs: 5
max PDB observations: 10-20
max total cycles: 15-20
```

## Qualitative insights

Useful information types:

1. failing test code and initial execution result,
2. snippets found via similar-code search,
3. structure of classes/methods,
4. feedback from applying fixes and running tests.

RepairAgent sometimes overcomplicates bugs that need simple changes. The authors suggest initially limiting candidate fix complexity.

Project implication:

Our agent should try simple patches first before complex rewrites.

Recommended patch complexity ladder:

```text
1. single-line condition/value fix
2. small local block edit
3. helper call / existing API usage
4. multi-location patch
5. give up / ask for more evidence
```

## Limitations

RepairAgent limitations:

1. possible data leakage for Defects4J,
2. needs at least one failing test case,
3. fault localization quality matters,
4. LLM non-determinism,
5. expensive loops on unfixed bugs,
6. weak on recent complex multi-line/multi-file GitBug-Java bugs,
7. Java/Defects4J-focused,
8. not debugger/runtime-state focused.

The paper’s GitBug-Java experiment mitigates, but does not eliminate, data leakage concerns.

## What applies to our project

Strongly reusable:

1. dynamic prompt / state memory,
2. finite-state tool availability,
3. hypothesis expression/discard mechanism,
4. developer-like tool set,
5. deterministic patch application and automatic revert,
6. code search and similar usage retrieval,
7. long-term gathered information section,
8. repetition detection,
9. invalid tool-call repair/feedback,
10. strict cycle budget,
11. plausible vs correct distinction,
12. ablation strategy.

## What does not apply directly

Not directly reusable:

- Java-only tooling,
- Defects4J-specific setup,
- perfect fault localization assumption,
- AutoGPT framework dependency,
- free-form broad autonomy,
- no PDB/runtime state,
- high average cycle count,
- 30 patch variants per write_fix for MVP,
- large-scale test-suite costs.

## Relation to our PDB/debugger-agent project

RepairAgent is not our final architecture, but it helps define the controller.

Our system should combine:

```text
RepairAgent:
  dynamic prompt
  state machine
  tool wrappers
  memory
  patch/test/revert

LDB:
  structured runtime-state diagnosis

ChatDBG/debug-gym:
  PDB/debugger interaction

SWE-bench:
  patch/test evaluation
```

## Proposed PDB MVP state machine

Adapted from RepairAgent:

```text
State 1: Reproduce failure
  tools:
    run_tests
    get_failure_trace
    extract_failing_test

State 2: Understand bug
  tools:
    read_source_window
    search_code
    get_stack_summary
    get_locals
    express_hypothesis

State 3: Gather runtime evidence
  tools:
    get_frame
    get_locals
    safe_eval_expression
    inspect_caller_frame
    discard_hypothesis

State 4: Try fix
  tools:
    apply_patch
    run_tests
    revert_patch
    collect_more_information

State 5: Done
  tools:
    goal_accomplished
```

## Recommended tool set for our MVP

```text
read_range
search_code
extract_failing_test
run_tests
get_failure_trace
get_stack
get_frame
get_locals
safe_eval_expression
express_root_cause_hypothesis
discard_hypothesis
apply_patch
revert_patch
goal_accomplished
```

Optional later:

```text
find_similar_api_calls
get_classes_and_methods
extract_function
coverage_trace
branch_trace
```

## Key caution for our project

RepairAgent demonstrates the value of autonomy, but also the cost of unconstrained autonomy.

The right lesson is not:

```text
Let the LLM decide everything.
```

The right lesson is:

```text
Give the LLM developer-like tools,
but constrain tool availability with states,
validate outputs with middleware,
store memory,
prevent repetition,
budget cycles,
and always verify patches with tests.
```

## One-paragraph Turkish explanation for my own understanding

RepairAgent, LLM’i sadece patch üreten bir model değil, tool kullanan autonomous program repair agent olarak tasarlıyor. Sistem her cycle’da dynamic prompt ile modeli çağırıyor; model JSON formatında bir tool seçiyor; middleware tool’u çalıştırıp sonucu prompt memory’sine ekliyor. Araçlar kod okuma, code search, similar API call bulma, test çalıştırma, fault localization, patch yazma, hypothesis ifade/discard etme gibi developer benzeri aksiyonlardan oluşuyor. En önemli tasarım noktası finite state machine: model tamamen serbest bırakılmıyor; “Understand the bug”, “Collect information”, “Try to fix”, “Done” gibi state’lerde kullanılabilecek tool seti kısıtlanıyor. Bizim PDB agent için bu paper’ın ana dersi şu: PDB/runtime-state tool’ları da böyle bir state-machine ve middleware arkasına konmalı; agent memory, hypothesis tracking, patch/test/revert ve budget olmadan raw autonomy güvenilir olmaz.
