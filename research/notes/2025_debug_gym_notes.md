# Paper Notes — debug-gym

## Bibliography

- Title: debug-gym: A Text-Based Environment for Interactive Debugging
- Authors: Xingdi Yuan, Morgane M. Moss, Charbel El Feghali, Chinmay Singh, Darya Moldavskaya, Drew MacPhee, Lucas Caccia, Matheus Pereira, Minseon Kim, Alessandro Sordoni, Marc-Alexandre Côté
- Year: 2025
- Institution / affiliation: Microsoft Research Montréal, Microsoft Research NYC, McGill University, Mila
- arXiv: 2503.21557
- Local PDF path: research/papers/tier1_must_read/2025_debug_gym_text_based_interactive_debugging.pdf
- Access level: FULL_TEXT_READ

## Why this paper matters

debug-gym is directly relevant because it reframes debugging as an interactive tool-use environment for LLM agents. Unlike ChatDBG, which is primarily an interactive assistant inside a debugger, debug-gym is closer to an experimental platform: it defines an environment, toolbox, action syntax, observations, rewards, budgets, benchmarks, and agent baselines.

For this project, debug-gym is important because it provides a concrete blueprint for building and evaluating a Python/PDB-first debugging agent. It also gives a warning: merely giving an LLM access to PDB is not enough. Tool availability improves performance only when the agent/model can decide when and how to use tools effectively.

## Problem

Most LLM code-repair systems use a simple loop:

1. run code or tests,
2. observe an error message,
3. rewrite code,
4. repeat.

This can fail when the visible error message is nested, non-crashing, distant from the root cause, or insufficient for understanding variable values and control flow.

debug-gym addresses this limitation by giving the agent tools for active information seeking, especially PDB, so the agent can inspect hidden runtime semantics: variable values, breakpoints, stack state, execution flow, and file contents.

## Core idea

debug-gym is a text-based interactive environment for debugging agents. It exposes a code repository, terminal, and modular toolbox. At each step, the agent receives an observation and emits one text action. The action can inspect files, run tests, use PDB, patch code, list directories, or call a custom tool.

The central research question is:

> To what degree can LLMs use interactive debugging tools such as PDB?

## Formal setup

The paper formalizes interactive debugging as a partially observable sequential decision process. The agent cannot see the full true state of the codebase and runtime. It must choose actions that reveal useful observations.

Main components:

- State: repository files, directory tree, tests, runtime variables, breakpoints, and tool-specific state.
- Observation: partial textual view returned by environment/tool calls.
- Action: a text command invoking a tool.
- Reward: 1 if rewritten code passes tests, 0 otherwise.
- Episode termination: success, interaction-budget exhaustion, or rewrite-budget exhaustion.

For our project, this framing is useful because it separates the debugging system into:

- environment,
- action space,
- observation space,
- controller policy,
- verifier/reward.

## System / architecture

A debug-gym environment contains:

1. repository,
2. interactive terminal,
3. toolbox,
4. optional test suite,
5. working directory,
6. Docker or local execution backend.

The API resembles Gym/Gymnasium:

```text
env.reset() -> initial observation
env.step(action) -> next observation, reward
```

A typical agent loop:

1. reset environment,
2. build prompt from observation,
3. call LLM,
4. parse one action,
5. call `env.step(action)`,
6. receive observation and reward,
7. repeat until success or budget exhaustion.

## Safety / isolation

debug-gym copies the target repository into a temporary working directory. By default, that directory is mapped into a Docker container.

Safety features:

- Docker isolation is recommended to prevent model-generated actions from modifying the host filesystem.
- `.debugignore` removes files/directories from the environment scope.
- `.debugreadonly` marks files/directories read-only.
- Tests can be made read-only to reduce cheating, such as editing tests instead of fixing code.

This is highly relevant for our project. We should not run an LLM debugger directly against the user’s real repository without isolation.

## Tool action syntax

All actions are text commands wrapped in triple backticks.

Examples:

```text
```pdb b 10```
```pdb p df.columns```
```rewrite code/utils.py 4:6 <c> print('bonjour')</c>```
```

The environment parses the tool name and passes the inner command to that tool.

## Built-in tools

### eval

Runs the repository entrypoint or test command.

Examples:

- `python -m pytest -sv .`
- `python main.py`

If tests pass, reward is 1 and the episode terminates.

### view

Opens a file and shows its contents with line numbers. It also sets the current working file.

### pdb

Direct wrapper around Python PDB.

Capabilities include:

- breakpoints,
- clear breakpoint,
- continue,
- step,
- next,
- print variable/expression,
- inspect local variables,
- work inside a persistent debugging shell.

The PDB tool instruction used in the paper includes examples:

```text
```pdb p x```
```pdb b 42```
```pdb cl src/code.py:26```
```pdb c```
```

### rewrite

Edits part of a file. It accepts:

- file path,
- start/end line range,
- replacement code block.

This avoids rewriting entire files in realistic codebases.

### listdir

Shows a directory tree at arbitrary depth. Useful for repository navigation.

## Custom tools

debug-gym is designed to be extensible. A tool:

1. inherits from `EnvironmentTool`,
2. registers with `@Toolbox.register()`,
3. defines a `name`,
4. defines `instructions`, including syntax, description, and examples,
5. implements `use()`.

This maps well to our future tool plan:

- file-read tool,
- code-search tool,
- PDB tool,
- test-run tool,
- patch-apply tool,
- verifier tool,
- maybe root-cause checker.

## Example agents

The paper defines three minimal agents.

### rewrite agent

Tools:

- view,
- rewrite,
- eval,
- sometimes listdir.

This is the non-debugger baseline.

### debug agent

Tools:

- all rewrite-agent tools,
- PDB from the beginning of the episode.

The agent can inspect runtime information before patching.

### debug(5) agent

Intermediate strategy:

- starts like rewrite agent,
- PDB becomes available only after the 5th rewrite attempt.

Motivation:

- early rewrites are often productive,
- later rewrites have diminishing returns,
- delayed PDB may preserve baseline behavior while adding runtime evidence when needed.

This is one of the most important ideas for our project. It suggests that debugger use should be gated rather than always-on.

## Models evaluated

Closed-weight models:

- GPT-4o
- GPT-4o-mini
- o1-preview
- o3-mini
- Claude 3.7 Sonnet

Open-weight models:

- Llama-3.2-3B-Instruct
- Llama-3.3-70B-Instruct
- DeepSeek-R1-Distill-Llama-70B
- DeepSeek-R1-Distill-Qwen-32B

## Benchmarks

### Aider

- 133 Python Exercism-based tasks.
- Mostly function-level code generation.
- Used as a simpler prerequisite task.

### Mini-nightmare

- 10 hand-crafted buggy Python examples.
- Average length around 40 lines.
- Designed so interactive debugging is useful to humans.
- Includes race conditions, unknown/complex data structures, boundary issues, condition coverage, and string management.

### SWE-bench-Lite

- 300 curated tasks from SWE-bench.
- Real GitHub issue / pull request setting.
- Requires repository-level understanding.
- In debug-gym’s default setup, test cases are available read-only to agents, which differs from some standard SWE-bench settings.

## Evaluation metrics

Primary metric:

- success rate: whether final code passes tests.

Efficiency metric:

- number of rewrites.

Budget:

- 50 interaction steps,
- 10 rewrite attempts.

Each experiment is run three times and reported with average and standard deviation.

## Key results

### Aider

PDB access did not clearly improve Aider performance. This makes sense because Aider tasks are simple and mostly function-level. The runtime evidence exposed by PDB often adds little beyond static code and tests.

Important finding:

- weaker/minimal agents may call PDB without a useful strategy.
- additional tools can increase steps without improving correctness.

### Mini-nightmare

Mini-nightmare is where PDB access starts to show value.

Observed pattern:

- weaker models struggle,
- stronger models solve many tasks,
- PDB-equipped agents often outperform rewrite baseline on tasks designed to need runtime investigation.

Important qualitative examples:

- `patcher`: Claude 3.7 Sonnet used `view`, breakpoints, `p` commands, cleared the breakpoint, and then rewrote the buggy code. This resembles a human debugging trace.
- `shopping_cart`: Claude used PDB to test the Python 3 rounding behavior and then patched using `decimal.ROUND_HALF_UP`.

These examples are important because they show runtime interaction can produce information unavailable from error text alone.

### SWE-bench-Lite

Key Table 3 results:

| Backbone | rewrite | debug | debug(5) |
|---|---:|---:|---:|
| GPT-4o | 19.1 ± 2.4 | 17.2 ± 0.8 | 23.6 ± 1.0 |
| GPT-4o-mini | 4.0 ± 0.7 | 3.5 ± 0.7 | 6.2 ± 0.1 |
| o1-preview | 10.7 ± 0.7 | 30.2 ± 1.0 | 30.8 ± 0.9 |
| o3-mini | 8.5 ± 1.0 | 22.1 ± 0.9 | 19.8 ± 1.1 |
| Claude 3.7 Sonnet | 37.2 ± 2.1 | 48.4 ± 1.6 | 52.1 ± 1.6 |
| Llama-3.3-70B-Instruct | 2.4 ± 0.5 | 4.0 ± 1.0 | 4.8 ± 0.4 |

Interpretation:

- Immediate PDB can harm weaker models.
- Stronger models can benefit from PDB.
- Delayed PDB via debug(5) outperforms rewrite in all listed SWE-bench-Lite cases.
- For Claude 3.7 Sonnet, debug(5) achieves the best reported score: 52.1%.

Important caution:

- These are minimal baseline agents and may not represent the maximum possible performance.
- The paper’s point is not that debug-gym has solved debugging, but that it exposes a useful research environment.

## Behavioral analysis

The paper reports that stronger models, especially Claude 3.7 Sonnet, use a more diverse set of tools. Instead of only rewriting, they also inspect files and list directories. This broader exploration may be linked to better performance.

The paper also introduces a robustness score: how often debug/debug(5) agents still solve tasks that rewrite solved. Stronger models maintain higher robustness when given new tools, meaning tool access disturbs them less.

## Discussion

The paper’s strongest conclusion is not simply “PDB helps.” The actual conclusion is more conditional:

- PDB can help strong agents.
- PDB can hurt or distract weaker agents.
- Delaying PDB access can be better than exposing it immediately.
- Tool availability is insufficient without agent design and training.
- Debugging is a closed-loop sequential decision problem, not an open-loop reasoning chain.

This matters for our project because we should not expose every tool all the time. The controller should decide when to invoke debugger state.

## Limitations

### Trustworthy agent problem

Agents can cheat by using visible tests too narrowly, for example hardcoding behavior to pass test cases. debug-gym makes tests read-only, but visible tests can still be overfit.

Suggested mitigations:

- hidden tests,
- dynamically generated tests,
- auxiliary test-generation agents,
- periodic test updates.

### Reviewer agent

Success rate alone is insufficient. Code can pass tests but be inefficient, brittle, or low-quality. The paper suggests reviewer/judge agents as future work.

### Linear computation flow

Current debug-gym tools are called sequentially. More complex agents may benefit from DAG-style or parallel tool flows, such as multiple PDB sessions focused on different hypotheses.

### Python-only focus

The paper focuses on Python and PDB. Extending to other languages is nontrivial because debugging logic differs across ecosystems. The authors mention Debug Adapter Protocol (DAP) as a possible path toward cross-language debugger abstraction.

### Training need

The paper argues that current LLMs may lack enough sequential decision-making/debugging trace data. It proposes collecting human or strong-agent debugging trajectories, filtering them with a verifier, and fine-tuning models on those traces.

## What applies to our project

Strongly reusable:

- Gym-like environment framing.
- Text action / text observation interface.
- Tool registry design.
- Docker-isolated repository copy.
- `.debugignore` / `.debugreadonly` equivalents.
- PDB as a first-class tool.
- Partial-line rewrite instead of whole-file rewrite.
- Delayed debugger access strategy.
- Step and rewrite budgets.
- Success-rate plus rewrite-count metrics.
- Trace data as future SFT source.
- Verifier / reviewer concept.

## What does not apply directly

Not directly reusable as-is:

- raw full-suite PDB exposure to the agent,
- success rate as sole evaluation metric,
- visible tests as sufficient validation,
- minimal prompt-only agent architecture,
- reliance on large closed models for best performance,
- immediate PDB access for all tasks.

## Project decisions after reading

- [x] Treat debug-gym as the environment/controller/evaluation blueprint.
- [x] Keep Python/PDB as first debugger target.
- [x] Add delayed-debugger-use as an experimental condition.
- [x] Compare against a rewrite/test-feedback baseline.
- [x] Track both success rate and number of rewrites/tool calls.
- [x] Keep tests read-only.
- [x] Add hidden/augmented tests later to reduce overfitting.
- [x] Do not assume PDB access always helps.
- [x] Defer fine-tuning until we collect useful debugging trajectories.
- [x] Consider DAP only after PDB MVP.

## Candidate MVP design changes from debug-gym

Action categories:

```text
view_file(path)
list_dir(path, depth)
run_tests(command)
start_debug_session(command)
pdb_command(command)
rewrite_file(path, start_line, end_line, replacement)
```

Safer version for our first prototype:

```text
get_stack()
get_frame(index)
get_locals(frame_index)
eval_expression(frame_index, expression)
get_source_window(file_path, line, radius)
info_symbol(symbol)
run_tests(command)
apply_patch(diff)
```

Experimental conditions to copy:

```text
baseline_rewrite_only
pdb_from_start
pdb_after_failed_rewrites
pdb_only_on_uncertainty
```

Metrics:

```text
pass_rate
number_of_rewrites
number_of_debugger_actions
number_of_steps
runtime_seconds
localization_accuracy
root_cause_explanation_quality
patch_correctness
```

## One-paragraph Turkish explanation for my own understanding

debug-gym, LLM tabanlı debugging agent’larını test etmek ve geliştirmek için hazırlanmış text-based bir environment’tır. ChatDBG daha çok debugger içine entegre edilmiş bir kullanıcı asistanı iken, debug-gym bize deney ortamı, tool registry, action/observation formatı, Docker izolasyonu, PDB tool’u, rewrite/eval/view/listdir araçları ve benchmark ölçümü sağlar. En önemli bulgusu şudur: PDB erişimi her zaman otomatik fayda sağlamaz; güçlü modeller PDB’den yararlanabilirken zayıf modeller araçla oyalanıp performans kaybedebilir. Bu yüzden bizim MVP’de debugger erişimi kontrollü/gated olmalı, rewrite-only baseline ile karşılaştırılmalı, PDB’den gelen runtime evidence’ın gerçekten başarıyı artırıp artırmadığı ölçülmelidir.
