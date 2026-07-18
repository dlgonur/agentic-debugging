# Paper Notes — LDB / Debug Like a Human

## Bibliography

- Title: Debug like a Human: A Large Language Model Debugger via Verifying Runtime Execution Step by Step
- Short name: LDB
- Authors: Li Zhong, Zilong Wang, Jingbo Shang
- Affiliation: University of California, San Diego
- arXiv: 2402.16906v6
- Date in PDF: 6 Jun 2024
- Local PDF path: research/papers/tier2_core_sections/2024_ldb_debug_like_a_human.pdf
- Access level: FULL_TEXT_READ

## Why this paper matters

LDB is important because it gives a direct runtime-state argument for our PDB/debugger-agent project.

The paper argues that many LLM debugging/refinement methods treat the generated program as an indivisible unit and only use post-execution feedback such as test failure messages. LDB instead imitates human debugging: break the program into smaller execution units, inspect intermediate variable values, verify each unit against the task description, and use the diagnosis to regenerate the program.

For our project, LDB is not a repository-level debugging agent and not a PDB adapter, but it gives a strong design principle:

> Runtime execution information is often more useful than self-reflection or dry-run reasoning alone.

## Core problem

Existing LLM code-refinement methods often use:

- failed unit tests,
- final outputs,
- error messages,
- self-generated explanations,
- dry-run traces.

The paper argues this is insufficient because it treats the program as a black box or indivisible entity. Human developers usually inspect runtime execution:

- execution path,
- intermediate variable values,
- breakpoints,
- state before/after relevant code regions.

This is especially important for complex control flow and data operations.

## Core idea

LDB collects runtime execution information from failed visible test cases and asks an LLM to verify program correctness step by step.

The LDB loop:

```text
Step 0: generate seed program with an LLM
Step 1: build CFG and decompose program into basic blocks
Step 2: run failed visible test case and collect execution trace
Step 3: ask LLM to judge each block using code + before/after states + task description
Step 4: regenerate/refine program using debugging verdicts
Repeat until visible tests pass or iteration budget is exhausted
```

## Main workflow

### 1. Seed generation

A code generator receives task description and visible tests and generates an initial program.

If the seed fails visible tests, LDB begins debugging.

### 2. Profiling

LDB collects runtime execution information by executing the generated program on failed visible test cases.

Profiling includes:

- building or using a control-flow graph,
- segmenting execution into basic blocks,
- collecting execution trace,
- collecting intermediate variable values after each basic block.

A basic block is a straight-line code sequence with one entry and one exit. In LDB, each runtime observation is represented as:

```text
(V_{i-1}, B_i, V_i)
```

Where:

- `V_{i-1}` = variable state before block,
- `B_i` = current basic block,
- `V_i` = variable state after block.

### 3. Debugging verdicts

For each selected block, LDB asks the LLM to produce:

```text
block:
correct: True / False
explanation:
```

The LLM compares:

- task description,
- current block code,
- before-state,
- after-state,
- actual failed test behavior.

This converts a vague test failure into block-level diagnostic feedback.

### 4. Regeneration

LDB feeds the task description plus debugging verdicts/explanations back to the LLM and asks it to regenerate a corrected program.

The process repeats until:

- all visible tests pass, or
- the maximum debugging iteration count is reached.

## Selective debugging

Loops and recursion can produce long traces. LDB therefore samples a bounded number of blocks rather than sending the entire trace.

Default implementation detail:

- sample threshold for blocks: 10,
- input token threshold: 3,097,
- for block-level debugging: first 5 blocks + last 5 blocks when trace is longer than 10 blocks.

Project implication:

- Our PDB agent should not dump an entire debugger session into the model.
- Use a bounded evidence window:
  - first/last trace slices,
  - top suspicious frames,
  - local variables around failure,
  - selected source windows.

## Batch debugging

Instead of asking the LLM once per block, LDB batches selected block states into one prompt and asks for verdicts over all blocks.

This improves token efficiency:

```text
Without batching: O(B^2 * N)
With batching:    O(B * N)
```

Where:

- `B` = number of blocks,
- `N` = average tokens per block.

Project implication:

- Our agent should batch debugger observations when possible.
- Avoid repeated prompts that resend the same long context.
- A structured JSON observation bundle is preferable.

## Decomposition level

The paper compares three granularity levels:

1. line-level,
2. block-level,
3. function-level.

Result:

- block-level gives the best performance and lowest/favorable token cost.
- line-level can be too fine-grained and semantically incomplete.
- function-level can be too coarse and lacks detailed runtime evidence.

HumanEval with GPT-3.5:

- no debugger: 73.8
- LDB line-level: 80.5
- LDB block-level: 82.9
- LDB function-level: 79.9

HumanEval with CodeLlama:

- no debugger: 49.4
- LDB line-level: 53.7
- LDB block-level: 55.5
- LDB function-level: 53.7

Project implication:

- For PDB, the equivalent of a “block” should probably be:
  - current failing frame,
  - source window around exception,
  - selected executed branch/state checkpoints,
  - not every line,
  - not entire function.

## Results

Benchmarks:

- HumanEval,
- MBPP,
- TransCoder.

Backbones:

- GPT-3.5,
- CodeLlama 34B,
- StarCoder 15B.

Main result:

- LDB consistently improves baseline performance by up to 9.8%.
- It beats Self-Debugging variants that use self-explanation or dry-run traces.

Selected results from Table 1:

```text
GPT-3.5:
  HumanEval baseline: 73.8
  LDB: 82.9 (+9.1)
  TransCoder baseline: 82.3
  LDB: 87.7 (+5.4)
  MBPP baseline: 67.6
  LDB: 76.0 (+8.4)

CodeLlama 34B:
  HumanEval baseline: 49.4
  LDB: 55.5 (+6.1)
  TransCoder baseline: 69.8
  LDB: 79.6 (+9.8)
  MBPP baseline: 51.2
  LDB: 57.4 (+6.2)

StarCoder:
  HumanEval baseline: 39.0
  LDB: 39.6 (+0.6)
  TransCoder baseline: 61.8
  LDB: 69.8 (+8.0)
  MBPP baseline: 51.6
  LDB: 55.4 (+3.8)
```

The paper attributes the gain to actual runtime information grounding the model better than self-generated explanation or imagined execution.

## Advanced generator result

LDB can improve already strong seed generators.

HumanEval example:

```text
Reflexion seed: 91.5
+ LDB GPT-3.5: 95.1
+ LDB GPT-4: 96.9
+ LDB GPT-4o: 98.2
```

Project implication:

- Debugging can be orthogonal to generation quality.
- Even a strong model may benefit from runtime evidence.
- A weaker controller/debugger model may still improve outputs from a stronger generator, though this must be tested.

## Iteration behavior

LDB improves over multiple debugging iterations and continues improving beyond where Self-Debugging plateaus.

Important observation:

- Self-Debugging improves early then converges.
- LDB continues to improve because each iteration adds concrete runtime evidence.

The paper reports:

- HumanEval GPT-3.5 baseline: 73.8,
- LDB after 10 iterations: 82.9,
- LDB after 20 iterations: 84.1.

Project implication:

- Runtime evidence can provide new information across retries.
- But iteration budget must be bounded.
- For MVP, use small fixed retry counts.

Recommended initial budget:

```text
static patch attempt: 1
PDB-assisted attempts: 1-2
max total patch attempts: 3
max debugger actions: 10-20
```

## Case study

Task:

- determine if a list is sorted,
- allow one duplicate of the same number,
- return False only if a number appears more than twice.

Seed bug:

```python
return not any(lst.count(x) > 1 for x in lst)
```

For input:

```python
[1, 2, 2, 3, 3, 4]
```

Expected:

```text
True
```

Actual:

```text
False
```

LDB inspects blocks and marks the final block incorrect because the condition should be:

```python
lst.count(x) > 2
```

This is exactly the kind of runtime-state diagnosis that motivates our PDB design.

## Error analysis

LDB evaluates bug localization accuracy on successfully debugged cases using GPT-4 as evaluator.

Reported localization/debug correctness:

- HumanEval: 93.7%
- MBPP: 95.3%
- TransCoder: 86.7%

Detected error types:

- semantic errors around 76.8%–81.2%,
- syntax errors around 18.8%–23.2%.

Interpretation:

- LDB is mainly useful for semantic debugging.
- Runtime state is especially useful when code runs but produces wrong values.
- This aligns strongly with our project: PDB should target semantic/runtime failures, not just syntax errors.

## Overhead

LDB overhead is reported as comparable to Self-Debugging.

HumanEval GPT-3.5 timing:

```text
LDB total: 17.23s
Self-Debugging total: 17.08s
Profiling overhead: 0.09s average
```

Project implication:

- Structured runtime-state collection can be cheap relative to LLM calls.
- Tool overhead is less important than prompt/API overhead.
- Our PDB controller should optimize for fewer, richer observations.

## Limitations

LDB limitations:

1. Requires correct visible test cases.
2. Evaluates generated short programs, not repository-scale bugs.
3. Not a real interactive debugger adapter.
4. Uses generated programs rather than existing mature repositories.
5. Requires runtime execution to be available.
6. Test-case-free debugging remains open.
7. Python implementation, though authors argue the idea is language-adaptable.

Important limitation for our project:

- LDB does not solve repository-scale issue localization.
- It does not apply patches inside large repos.
- It does not manage pass-to-pass regression preservation like SWE-bench.
- It does not use PDB commands directly.

## What applies to our project

Strongly reusable:

1. Runtime state is valuable evidence.
2. Step-wise verification beats pure self-reflection.
3. Intermediate values should be shown to the model.
4. Decomposition granularity matters.
5. Too fine or too coarse observations are worse than semantically meaningful blocks.
6. Batch observations improve token efficiency.
7. Debugging should be iterative but budgeted.
8. Runtime information is most useful for semantic bugs.
9. Test cases are required to ground debugging.
10. Visible vs hidden tests map well to reproduction vs regression/evaluation tests.

## What does not apply directly

Not directly reusable:

- HumanEval/MBPP style full-program regeneration,
- replacing whole generated program,
- short standalone-program assumption,
- lack of repository-level context,
- lack of patch application,
- lack of PDB-specific command control,
- no F2P/P2P split.

## Relation to Tier 1 papers

### Compared with ChatDBG

ChatDBG connects LLMs to debugger command interfaces. LDB provides a cleaner conceptual model for why runtime state helps:

```text
ChatDBG: LLM asks debugger questions.
LDB: system structures runtime states into verifiable steps.
```

Our MVP should combine both:

```text
PDB observations from ChatDBG-style tooling
+
structured block/state verification from LDB
```

### Compared with debug-gym

debug-gym evaluates interactive PDB use. LDB suggests a more structured alternative:

```text
Instead of free-form PDB wandering, collect bounded state snapshots and ask for block/frame verdicts.
```

This supports controller-gated PDB.

### Compared with Agentless

Agentless is static/test-feedback repository repair. LDB explains why adding runtime state may improve over such static baselines, especially for semantic bugs.

### Compared with SWE-bench

SWE-bench gives patch/test evaluation. LDB gives runtime diagnosis. Our system needs both:

```text
LDB-style runtime-state diagnosis
+
SWE-bench-style patch/test verifier
```

## Design implications for PDB MVP

The first PDB MVP should not expose raw PDB as a free-form terminal. It should expose structured observation tools:

```text
get_current_frame()
get_stack_summary()
get_locals(frame)
get_source_window(frame)
get_expression_value(frame, safe_expression)
get_recent_trace_or_branch_points()
```

Then the model should produce structured verdicts:

```json
{
  "localized_region": "...",
  "runtime_observations": [
    {
      "frame": "...",
      "variable": "...",
      "observed_value": "...",
      "expected_value": "...",
      "why_it_matters": "..."
    }
  ],
  "root_cause": "...",
  "patch_plan": "..."
}
```

## Suggested MVP adaptation of LDB

Because repository code is larger than HumanEval programs, exact CFG/basic-block instrumentation is probably too much for v1.

Use a simpler approximation:

```text
1. Reproduce failing test.
2. Capture traceback.
3. Identify failing frame.
4. Collect locals in failing frame.
5. Collect source window around failing line.
6. Optionally inspect caller frame.
7. Ask model for frame-level verdict.
8. Patch.
9. Run tests.
```

This is “frame/window-level LDB” rather than full CFG basic-block LDB.

Later extensions:

```text
branch trace
coverage trace
line-level trace for selected function
AST basic-block extraction
instrumented before/after variable snapshots
```

## Candidate LDB-inspired prompt shape

```text
You are debugging a Python failure.

Task / issue:
...

Failing test:
...

Observed failure:
...

Runtime evidence:
[FRAME 0]
function:
file:
line:
source:
locals:
expected vs actual:

[FRAME 1]
...

For each frame/source block:
- state whether the block is correct or suspicious,
- identify variables whose values contradict the intended behavior,
- explain the causal chain,
- propose the smallest safe patch.
```

## Project decisions after reading

- [x] Keep LDB as runtime-state reasoning reference.
- [x] Do not attempt full CFG/basic-block instrumentation in MVP.
- [x] Implement a simpler PDB frame/source/locals evidence collector first.
- [x] Use structured verdicts rather than free-form “debug this”.
- [x] Keep debugger context bounded and batched.
- [x] Evaluate whether runtime evidence improves semantic bug repair.
- [x] Prioritize bugs with failing tests and observable wrong intermediate state.

## One-paragraph Turkish explanation for my own understanding

LDB, LLM debugging’de runtime execution bilgisinin neden önemli olduğunu gösteren paper’dır. Mevcut self-debugging yaklaşımları çoğunlukla sadece test sonucu veya error message üzerinden modeli tekrar düşündürürken, LDB programı basic block’lara böler, failing visible test ile çalıştırır, her bloktan sonra değişken değerlerini toplar ve LLM’den her bloğun task description’a göre doğru olup olmadığına karar vermesini ister. Sonra bu verdict/explanation’ları kullanarak programı yeniden üretir. Bizim proje için ana ders şu: PDB agent, modele raw terminal vermek yerine stack frame, locals, source window ve selected expression gibi runtime evidence’ı yapılandırılmış şekilde sunmalı; model de root cause’u bu gözlenen state üzerinden açıklamalı. LDB repository-scale patch agent değil, ama PDB/runtime-state tarafının bilimsel gerekçesini güçlendiriyor.
