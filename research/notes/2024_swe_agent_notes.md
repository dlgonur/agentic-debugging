# Paper Notes — SWE-agent / Agent-Computer Interfaces

## Bibliography

- Title: SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering
- Authors: John Yang, Carlos E. Jimenez, Alexander Wettig, Kilian Lieret, Shunyu Yao, Karthik Narasimhan, Ofir Press
- Affiliation: Princeton Language and Intelligence, Princeton University
- Venue: NeurIPS 2024
- arXiv: 2405.15793v3
- Date in PDF: 11 Nov 2024
- Local PDF path: research/papers/tier2_core_sections/2024_swe_agent_agent_computer_interfaces.pdf
- Access level: CORE_AND_RELEVANT_APPENDIX_SECTIONS_READ
- Focus of these notes: Agent-Computer Interface (ACI), command design, file/search/edit/context interface, evaluation results, trajectory/failure analysis, and implications for the Python/PDB MVP.

## Why this paper matters

SWE-agent is important because it reframes software-engineering agents as an interface-design problem.

The key argument is:

> LMs are a new kind of computer user. They should not simply be thrown into human-oriented interfaces like a raw Linux shell. They need an Agent-Computer Interface (ACI) designed around LM strengths and weaknesses.

For our project, SWE-agent is not a debugger paper and does not provide a first-class PDB/debugger adapter. Its value is in the design of a usable tool interface:

- simple commands,
- compact outputs,
- guardrails,
- file viewer,
- file editor,
- search tools,
- context/history processing,
- syntax-checking edit validation,
- execution feedback loops.

This directly applies to our future PDB tool interface.

## Core contribution

SWE-agent introduces:

1. The concept of an Agent-Computer Interface (ACI).
2. SWE-agent, an LM + ACI system for repository-level software engineering.
3. A custom LM-friendly interface for:
   - navigating repositories,
   - viewing files,
   - searching code,
   - editing files,
   - executing tests/programs,
   - managing interaction history.
4. Empirical evidence that ACI design significantly changes agent performance.

The central result is that better interfaces improve performance without changing model weights.

## Problem

A raw Linux shell is too general and too granular for LMs.

Common raw-shell problems:

- inefficient `cd`, `ls`, `cat`, `grep` exploration,
- context flooding from large file outputs,
- no consistent feedback when edits silently succeed/fail,
- fragile `sed`/redirection editing,
- poor recovery from syntax errors,
- too many low-level actions needed for high-level operations.

SWE-agent’s thesis is that these are interface failures, not just model failures.

## Agent-Computer Interface definition

An ACI is the interface through which an LM agent interacts with a computer.

It specifies:

- available commands,
- command documentation,
- action format,
- environment feedback format,
- history and context management,
- guardrails,
- command outputs shown to the LM.

The ACI is analogous to an IDE for humans, but designed for LM agents instead of people.

## ACI design principles

The paper identifies four recurring design principles.

### 1. Actions should be simple and easy to understand

Commands should have:

- few options,
- concise documentation,
- predictable behavior,
- no large option surface like full Bash/Git/Python CLIs.

Implication for PDB MVP:

Do not expose raw PDB with all commands initially. Expose simple typed commands:

```text
get_stack()
get_locals(frame)
get_source_window(frame)
safe_eval(frame, expression)
```

### 2. Actions should be compact and efficient

A single command should accomplish a useful higher-level operation.

Bad:

```text
cat file
calculate line numbers
sed edit
cat again
run linter
```

Good:

```text
edit <start>:<end> <replacement>
```

Implication for PDB MVP:

Do not require the model to manually step through trivial debugger mechanics when a higher-level command can return the useful state directly.

### 3. Feedback should be informative but concise

Every action should return enough information to understand:

- what changed,
- what the current state is,
- whether there was an error,
- how to recover.

But it should avoid flooding the context window.

Implication for PDB MVP:

A debugger observation should include:

```text
frame/function/file/line
source window
selected locals
exception
concise next possible actions
```

Not full stack dumps, entire files, or huge object reprs by default.

### 4. Guardrails reduce error propagation

SWE-agent uses guardrails such as syntax/lint checking after edits. Invalid edits are rejected and the model receives feedback.

Implication for PDB MVP:

Guardrails should exist for:

- unsafe eval expressions,
- repeated failed commands,
- large outputs,
- invalid frame indexes,
- risky patch edits,
- failing syntax/lint checks.

## SWE-agent interface components

SWE-agent’s ACI includes:

1. Search/navigation
2. File viewer
3. File editor
4. Context management
5. Execution through shell/Python/pytest

## Search/navigation

Special commands:

```text
find_file <file_name> [<dir>]
search_file <search_term> [<file>]
search_dir <search_term> [<dir>]
```

Behavior:

- `find_file` searches for filenames.
- `search_dir` searches for strings in files under a directory.
- `search_file` searches inside current/specified file.
- Search results are summarized.
- If results exceed 50, the system does not dump all results; it asks the agent to refine the query.

Why this matters:

- raw `grep` can flood context,
- raw filesystem exploration burns turns,
- summarized search helps the model zoom in faster.

Project implication:

Our baseline and PDB MVP need explicit search tools:

```text
find_file
search_code
search_file
find_symbol
```

The PDB layer cannot replace repository search.

## File viewer

Commands:

```text
open <path> [<line_number>]
goto <line_number>
scroll_down
scroll_up
```

The viewer shows:

- file path,
- total number of lines,
- current window,
- line numbers,
- omitted lines above/below.

Default window size used in the strongest configuration:

```text
100 lines
```

Ablation:

```text
30 lines: 14.3% on SWE-bench Lite
100 lines: 18.0%
Full file: 12.7%
```

Lesson:

- too little context harms localization,
- too much context distracts the model,
- fixed windows with line numbers help precise edits.

Project implication:

PDB source windows should be bounded. A good first default:

```text
radius 40-60 lines around failing line
or
~80-120 total lines maximum
```

## File editor

Command:

```text
edit <n>:<m>
<replacement_text>
end_of_edit
```

Meaning:

- replace lines n through m in the currently open file,
- show updated file viewer after edit,
- run syntax/lint check for Python files,
- if major syntax error appears, reject edit.

The editor integrates with the file viewer; after edit, the agent immediately sees updated content.

Important result:

```text
SWE-agent with edit + linting: 18.0% SWE-bench Lite
edit without linting: 15.0%
no edit interface: 10.3%
```

Lesson:

- editing interface quality has large impact,
- silent or low-level shell editing is bad for agents,
- lint guardrails prevent cascading edit failures.

Project implication:

For MVP, avoid asking the model to rewrite whole files or use arbitrary shell editing. Use one deterministic patch tool.

Candidate options:

```text
Search/Replace
JSON edit operations
unified diff with strict parser
```

## Context management

SWE-agent manages context by:

- using prompt templates,
- formatting environment responses,
- giving demonstrations optionally,
- collapsing older observations,
- omitting repeated error messages,
- maintaining enough history without flooding the model.

Specific design:

- observations preceding the last 5 are collapsed into a single line,
- full history performs worse than keeping last 5 observations.

Ablation:

```text
Last 5 observations: 18.0%
Full history: 15.0%
without demonstration: 16.3%
```

Project implication:

Our PDB agent should keep:

```text
last 5 observations full
older observations summarized
current hypothesis
current patch attempts
key runtime evidence
```

Do not feed the entire debugger trajectory every time.

## Command table

SWE-agent specialized commands from Table 4:

```text
File viewer:
  open <path> [<line_number>]
  goto <line_number>
  scroll_down
  scroll_up

Search:
  search_file <search_term> [<file>]
  search_dir <search_term> [<dir>]
  find_file <file_name> [<dir>]

File editing:
  edit <n>:<m> <replacement_text> end_of_edit
  create <filename>

Task:
  submit
```

These commands sit on top of Linux shell access.

Project implication:

Our PDB MVP command set should be similarly small and documented.

## Configuration and extensibility

SWE-agent is configured via YAML and has three major modules:

1. environment,
2. agent,
3. logging.

Configuration specifies:

- prompt templates,
- command files,
- control-flow logic,
- environment variables,
- parse function,
- history processor.

Commands can be added via Bash/Python function files with:

- signature,
- docstring,
- typed arguments,
- implementation.

This is directly relevant to our implementation:

- PDB tools should be individually documented,
- command schemas should be generated into the system prompt,
- command outputs should be structured and compact,
- trajectories and patches should be logged.

## Experimental setup

Datasets:

- SWE-bench full: 2,294 instances,
- SWE-bench Lite: 300 instances,
- HumanEvalFix for short-form editing.

Models:

- GPT-4 Turbo,
- Claude 3 Opus.

Baselines:

- RAG baseline from SWE-bench,
- Shell-only interactive agent.

Metric:

```text
% Resolved / pass@1
```

Budget:

```text
$4 per instance
```

## Main results

SWE-bench full:

```text
SWE-agent + GPT-4 Turbo: 12.47% resolved, $1.59 avg cost
SWE-agent + Claude 3 Opus: 10.46% resolved, $2.59 avg cost
RAG + GPT-4 Turbo: 1.31%
RAG + Claude 3 Opus: 3.79%
```

SWE-bench Lite:

```text
Shell-only GPT-4 Turbo: 11.00%
Shell-only without demonstration: 7.33%
SWE-agent GPT-4 Turbo: 18.00%
SWE-agent Claude 3 Opus: 13.00%
RAG GPT-4 Turbo: 2.67%
RAG Claude 3 Opus: 4.33%
```

HumanEvalFix:

```text
SWE-agent + GPT-4 Turbo:
  Python: 87.7
  JS: 89.7
  Java: 87.9
```

Interpretation:

- interactive ACI beats non-interactive RAG for repository repair,
- ACI beats shell-only,
- interface quality matters substantially.

## ACI ablation summary

SWE-bench Lite with GPT-4 Turbo:

```text
Editor:
  edit action: 15.0
  edit + linting: 18.0
  no edit: 10.3

Search:
  summarized search: 18.0
  iterative search: 12.0
  no search: 15.7

File viewer:
  30 lines: 14.3
  100 lines: 18.0
  full file: 12.7

Context:
  last 5 observations: 18.0
  full history: 15.0
  no demonstration: 16.3
```

Project implications:

1. Linting/guardrails matter.
2. Summarized search beats iterative search.
3. Full files are worse than windowed source.
4. Full history is worse than processed history.
5. Demonstrations help but are less critical than interface design.

## Agent behavior patterns

SWE-agent commonly begins with:

1. Reproduction
2. Localization

Common early pattern:

```text
create -> edit -> python
```

Meaning:

- create a reproduction file,
- write reproduction code,
- run it.

Alternative early pattern:

```text
search_dir/find_file -> open -> search_file/goto
```

Meaning:

- localize relevant files/lines first.

After turn 5, trajectories mostly become:

```text
edit -> python/pytest -> edit -> python/pytest
```

The agent iterates through patch/test loops, with occasional extra localization.

Project implication:

Our PDB MVP loop should similarly be:

```text
reproduce
localize
inspect runtime
patch
run tests
retry or submit
```

## Editing remains difficult

Important findings:

- 51.7% of SWE-agent GPT-4 Turbo trajectories have at least one failed edit.
- 31.5% of resolved trajectories have at least one failed edit.
- any edit attempt has a 90.5% chance of eventually being successful.
- after a single failed edit, success probability drops to 57.2%.

Lesson:

- one failed edit can send agents into recovery problems,
- strict patch tooling and edit rejection are crucial.

Project implication:

Patch application should be deterministic and validated before tests:

```text
parse patch
check target lines/snippets
apply in temp copy
run syntax check
run tests
revert on failure
```

## Success and failure timing

SWE-agent succeeds quickly and fails slowly.

Reported observation:

- resolved instances tend to submit earlier and cheaper,
- resolved GPT-4 runs median cost: $1.21 and 12 steps,
- unsuccessful runs mean cost: $2.52 and 21 steps,
- increasing budget likely gives diminishing returns.

Project implication:

Do not give the PDB agent huge budgets initially.

Suggested v1 budget:

```text
max total cycles: 15
max patch attempts: 3
max PDB inspection rounds: 2
max debugger observations: 10-20
```

## Failure modes

Unresolved SWE-agent trajectories were categorized into failure modes.

Main categories:

- Failed to Reproduce
- Failed to Find Relevant File
- Failed to Find Edit Location
- Overly Specific Implementation
- Incorrect Implementation
- Ran Out of Budget
- Failed Edit Recovery
- Gave Up Prematurely

Major finding:

```text
~52.0% unresolved = Incorrect Implementation or Overly Specific Implementation
23.4% = Failed Edit Recovery
```

Project implication:

Our evaluation should track not just pass/fail, but why an agent failed:

```text
failed reproduction
failed localization
bad runtime interpretation
incorrect patch
overfit patch
edit failure
budget exhausted
regression introduced
```

## What applies to our project

Strongly reusable:

1. ACI concept.
2. Small LM-friendly command set.
3. Bounded file viewer/source windows.
4. Search tools with summarized output.
5. Editing command with guardrails.
6. Immediate feedback after edits.
7. Context/history processing.
8. Demonstrations of correct tool use.
9. Configuration-driven tools/prompts.
10. Logging trajectories and generated patches.
11. Failure-mode taxonomy.
12. Budget discipline.
13. “Reproduce/localize first, then edit/test loop” trajectory model.

## What does not apply directly

Not directly reusable:

- no first-class PDB/debugger integration,
- no structured runtime locals/frames,
- no root-cause evidence requirement,
- no state-machine like RepairAgent,
- no explicit hypothesis/discard mechanism,
- large SWE-bench target is too heavy for first MVP,
- shell access is broader than we want for safe PDB-first experiments.

## Relation to prior notes

### Compared with RepairAgent

RepairAgent gives state machine + dynamic memory + tool-control structure.

SWE-agent gives LM-friendly command design and concrete ACI principles.

Together:

```text
RepairAgent: what states/tools a repair agent needs
SWE-agent: how those tools should be shaped for LMs
```

### Compared with LDB

LDB says runtime state helps.

SWE-agent says interface design determines whether agents can effectively use tools.

Together:

```text
LDB: provide runtime evidence
SWE-agent: provide runtime evidence through compact, guardrailed commands
```

### Compared with ChatDBG/debug-gym

ChatDBG/debug-gym motivate debugger access.

SWE-agent warns that raw interfaces are not ideal for LMs.

So our PDB interface should not be raw PDB; it should be an ACI for debugging.

## PDB ACI design implications

The PDB MVP should expose an LM-friendly debugging ACI.

Candidate command set:

```text
reproduce_failure
get_failure_trace
open_source_at_trace
search_code
get_stack_summary
get_frame_locals
safe_eval_expression
get_source_window
apply_patch
run_tests
revert_patch
submit
```

Command design rules:

1. Few arguments.
2. Typed arguments.
3. Consistent output format.
4. Concise but informative feedback.
5. Guardrails for unsafe actions.
6. Immediate post-action state update.
7. Automatic truncation/summarization.
8. No silent success.
9. No full raw terminal dumps unless explicitly requested.
10. Older observations summarized.

## PDB observation format sketch

```json
{
  "command": "get_frame_locals",
  "status": "ok",
  "frame": {
    "index": 0,
    "function": "parse_item",
    "file": "src/parser.py",
    "line": 84
  },
  "source_window": {
    "start_line": 70,
    "end_line": 100,
    "current_line": 84,
    "content": "..."
  },
  "locals": [
    {
      "name": "token",
      "type": "str",
      "repr": "'EOF'"
    },
    {
      "name": "expected",
      "type": "list",
      "repr": "['IDENT', 'NUMBER']"
    }
  ],
  "hints": [
    "Use safe_eval_expression only for side-effect-free expressions.",
    "Use apply_patch only after stating a root-cause hypothesis."
  ]
}
```

## PDB MVP controller lesson

The first implementation should avoid:

```text
LLM directly controls shell
LLM directly controls raw PDB terminal
LLM edits files with arbitrary shell commands
LLM receives full unprocessed history
```

Use:

```text
small command set
source windows
locals/stack summaries
guardrails
patch validator
test runner
history processor
trajectory logger
```

## Project decisions after reading

- [x] Treat PDB interface as an ACI, not just a debugger adapter.
- [x] Avoid raw PDB terminal in v1.
- [x] Implement LM-friendly structured commands.
- [x] Keep source windows bounded.
- [x] Include guardrails and no-silent-output policy.
- [x] Add history compression.
- [x] Add edit/patch validation before tests.
- [x] Track failure modes in evaluation.
- [x] Keep command docs short and generated from tool schemas.

## One-paragraph Turkish explanation for my own understanding

SWE-agent’in ana fikri, LLM agent’ların bilgisayarı insanlar gibi kullanmadığı ve bu yüzden insanlara göre tasarlanmış raw Linux shell gibi interface’lerin onlar için kötü olduğudur. Paper buna Agent-Computer Interface diyor: modelin hangi komutları kullanabildiği, çıktıları nasıl gördüğü, geçmişin nasıl sıkıştırıldığı, edit/search/file-viewer’ın nasıl tasarlandığı performansı doğrudan etkiliyor. SWE-agent bu yüzden raw shell yerine `open`, `goto`, `scroll`, `search_file`, `search_dir`, `find_file`, `edit`, `create`, `submit` gibi basit ve LM-friendly command’lar veriyor. Edit sonrası file viewer güncelleniyor, lint guardrail syntax hatalı edit’i uygulamıyor, eski context sıkıştırılıyor. Bizim PDB projesi için ana ders şu: PDB’yi raw terminal olarak vermemeliyiz; PDB için de bir ACI tasarlamalıyız. Model stack, locals, source window ve safe expression gibi bilgileri küçük, tipli, güvenli ve kısa çıktılı tool’larla görmeli.
