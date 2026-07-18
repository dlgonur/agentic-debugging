# Paper Notes — AutoCodeRover

## Bibliography

- Title: AutoCodeRover: Autonomous Program Improvement
- Authors: Yuntong Zhang, Haifeng Ruan, Zhiyu Fan, Abhik Roychoudhury
- Affiliation: National University of Singapore
- Venue: ISSTA 2024
- DOI: 10.1145/3650212.3680384
- arXiv: 2404.05427v3
- Date in PDF: 25 Jul 2024
- Local PDF path: research/papers/tier2_core_sections/2024_autocoderover_autonomous_program_improvement.pdf
- Access level: FULL_TEXT_READ

## Why this paper matters

AutoCodeRover is important because it is a strong repository-level repair agent that is explicitly grounded in software-engineering structure rather than a generic shell/interface view of a repo.

Its central difference from SWE-Agent:

```text
SWE-Agent:
  codebase as files + shell/file/search/edit ACI

AutoCodeRover:
  codebase as AST/program structure + class/method/code-snippet retrieval APIs
```

For our project, AutoCodeRover gives the retrieval/fault-localization side of the future PDB agent:

- AST-aware class/method search,
- stratified iterative context retrieval,
- method-level retrieval instead of file dumping,
- SBFL as external analysis signal,
- patch generation from localized methods,
- patch validation with tests,
- plausible vs correct patch warning.

The main lesson is:

> Runtime/debugger evidence is useful, but it should be combined with program-structure-aware retrieval. PDB does not replace code search; it should feed into a structured localization/retrieval pipeline.

## Core contribution

AutoCodeRover combines LLM agents with sophisticated code search over program structure.

The paper emphasizes:

- AST-based program representation,
- class/method/snippet search,
- iterative context retrieval,
- SBFL-assisted retrieval when tests are available,
- patch generation from collected context,
- evaluation on SWE-bench Lite and full SWE-bench.

It reports:

- 19% pass@1 on SWE-bench Lite,
- 26% pass@3 on SWE-bench Lite,
- 12.42% pass@1 on full SWE-bench,
- 17.96% pass@3 on full SWE-bench,
- low average cost compared with SWE-Agent.

## Problem

Many agentic software-engineering systems treat repositories as collections of files. AutoCodeRover argues that this is not software-engineering-aware enough.

Real software maintenance requires reasoning over:

- classes,
- methods,
- method signatures,
- code snippets,
- call/semantic relationships,
- test signals,
- program structure.

The paper’s position is that program specifications can be partially gleaned from repository structure.

## Overall workflow

AutoCodeRover has two main stages:

```text
problem statement + codebase
  -> context retrieval
  -> buggy locations
  -> patch generation
  -> final patch
```

More detailed:

```text
1. LLM reads issue description.
2. LLM extracts hints:
   - class names
   - method names
   - file names
   - code snippets
   - domain terms
3. LLM invokes context retrieval APIs.
4. APIs search the local codebase using AST/program structure.
5. LLM analyzes returned context.
6. If context is insufficient, LLM performs another stratum of search.
7. When sufficient, LLM outputs buggy locations.
8. Patch generation agent receives:
   - issue,
   - buggy locations,
   - retrieval history,
   - context analysis.
9. Patch generation agent writes patch.
10. If patch cannot apply or fails syntax/lint validation, retry.
11. If tests are available, validate patch and retry up to a limit.
```

## Motivating example

The paper uses Django issue `django-13933`, where `ModelChoiceField` should include the invalid value in the validation error message.

AutoCodeRover’s retrieval process:

1. searches `ModelChoiceField`,
2. searches `ModelMultipleChoiceField`,
3. searches `clean` methods,
4. discovers `ModelChoiceField` lacks `clean`,
5. searches `validate` and `to_python`,
6. identifies `to_python` as the likely modification location,
7. generates a patch.

Important lesson:

- The correct method was not obvious from the issue alone.
- Earlier retrieval results revealed new method names to inspect.
- Iterative search matters.

## Context Retrieval APIs

AutoCodeRover provides seven retrieval APIs:

```text
search_class(cls)
search_class_in_file(cls, f)
search_method(m)
search_method_in_class(m, cls)
search_method_in_file(m, f)
search_code(c)
search_code_in_file(c, f)
```

Outputs:

```text
search_class -> class signature
search_method -> method implementation
search_code -> +/- 3 lines around snippet
```

Important design:

- class search returns signatures, not full class bodies,
- method search returns implementation,
- snippet search returns small source windows,
- outputs are intentionally concise to avoid distraction.

Project implication:

Our PDB MVP should include AST/symbol search:

```text
find_class(name)
find_function(name)
get_function_source(name)
search_code_snippet(snippet)
get_callers(name)        # later
get_callees(name)        # later
```

The system should avoid file-level dumping.

## Stratified Context Search

AutoCodeRover introduces stratified search.

Core idea:

- Do not run only one retrieval call.
- Do not run every possible retrieval call at once.
- Instead, search in iterative strata.

Per stratum:

```text
current_context = issue + previous retrieved context

LLM chooses necessary API calls
APIs return compact context
LLM analyzes whether context is sufficient

if sufficient:
  output buggy locations
else:
  next stratum
```

Why this matters:

1. A single search may miss the root cause.
2. Running all searches can flood context.
3. Search results reveal new class/method names.
4. New names become arguments for later API calls.
5. The LLM can cross-reference issue hints and retrieved code.

Project implication:

The future PDB agent should use the same pattern:

```text
stratum 1:
  issue + traceback -> initial file/function search

stratum 2:
  inspect source + stack -> method/class/symbol retrieval

stratum 3:
  PDB locals + caller/callee context -> focused patch location
```

## SBFL / Analysis-Augmented Context Retrieval

AutoCodeRover optionally integrates Spectrum-Based Fault Localization (SBFL).

SBFL setting:

- tests are available,
- passing and failing tests are executed,
- method-level suspiciousness is computed,
- top-5 suspicious methods are provided to the LLM before context retrieval.

Important nuance:

AutoCodeRover does not replace context retrieval with SBFL. Instead, SBFL supplies extra hints.

The paper argues this because SBFL accuracy depends on test-suite quality. SBFL is best used as an analysis signal, not as absolute truth.

Project implication:

PDB observations should work similarly:

```text
PDB stack/local variables are not final truth.
They are extra structured hints for retrieval and hypothesis formation.
```

## SBFL results

On SWE-bench Lite:

```text
AutoCodeRover @1:
  57/300 = 19%

ACR-sbfl:
  66/300 = 22%
```

ACR-sbfl uniquely resolved 7 task instances not solved by the other runs.

The case study on `django-13964` shows why. Without SBFL, issue text hints point to distracting reproduction classes such as `Product` and `Order`. With SBFL, the analysis reveals methods such as `_prepare_related_fields_for_save`, which is where the developer actually fixed the bug.

Project implication:

External dynamic analysis can rescue retrieval when issue text is misleading or reproduction examples mention non-root-cause classes.

For our PDB MVP:

```text
traceback + PDB frame + locals
```

should similarly rescue cases where issue text is misleading.

## Patch generation

The patch generation agent receives:

- problem statement,
- buggy locations/methods,
- retrieval history,
- API calls and outputs,
- context analysis.

It first retrieves precise code snippets at buggy locations, then generates patches.

Retry loop:

- if patch format invalid,
- if patch cannot apply,
- if Python syntax/lint errors occur,
- if tests are available and patch fails test-suite,

then retry.

Default retry limit:

```text
3 attempts
```

Project implication:

This aligns with our planned patch verifier:

```text
patch format check
apply patch
syntax/lint check
run reproduction test
run regression tests
retry or stop
```

## Experimental setup

Benchmark:

- SWE-bench full: 2,294 issues,
- SWE-bench Lite: 300 issues.

Model:

- GPT-4 `gpt-4-0125-preview`,
- temperature 0.2,
- max_tokens 1024.

Inputs:

- natural language GitHub issue description,
- local code repository checked out at buggy version.

Evaluation metrics:

- percentage resolved,
- average time,
- average token/cost.

Execution:

- official SWE-bench Docker environment for patch correctness.

Termination:

- patch generated,
- or context retrieval repeats ten times.

## Main results

SWE-bench Lite:

```text
SWE-Agent:
  18.00% (54), 245k tokens, $2.51

AutoCodeRover @1:
  19.00% (57), 195s, 37k tokens, $0.43

AutoCodeRover @3:
  26.00% (78), 520s, 112k tokens, $1.30

ACR-sbfl:
  22.00% (66), 250s, 40k tokens, $0.47
```

Full SWE-bench:

```text
SWE-Agent:
  12.47% (286), 240k tokens, $2.46

AutoCodeRover @1:
  12.42% (285), 248s, 39k tokens, $0.45

AutoCodeRover @3:
  17.96% (422), 701s, 120k tokens, $1.39
```

SWE-bench Devin subset:

```text
SWE-Agent:
  13.51% (77)

Devin:
  13.86% (79), >600s

AutoCodeRover @1:
  12.63% (72), 238s, 37k tokens, $0.42

AutoCodeRover @3:
  18.77% (107), 692s, 117k tokens, $1.36
```

Interpretation:

- AutoCodeRover @1 roughly matches or slightly beats SWE-Agent on SWE-bench Lite at much lower token cost.
- AutoCodeRover @3 improves solve rate but costs more.
- Structured retrieval is token efficient.

## AutoCodeRover vs SWE-Agent

The paper reports complementarity with SWE-Agent on SWE-bench Lite:

```text
common resolved: 31
AutoCodeRover unique: 26
SWE-Agent unique: 23
```

Why AutoCodeRover succeeds uniquely:

- AST-level fine-grained context search,
- precise class/method retrieval,
- better program-structure grounding.

Why AutoCodeRover fails where SWE-Agent succeeds:

- unimplemented search APIs,
- invalid API calls from LLM,
- failure to recover to valid search APIs.

Project implication:

Our system needs both:

```text
SWE-Agent style robust interface/guardrails
+
AutoCodeRover style program-structure-aware retrieval
+
PDB runtime-state evidence
```

## Token efficiency

For resolved tasks, AutoCodeRover is much more token-efficient:

- 19 resolved tasks under 10k tokens,
- 31 resolved tasks between 10k and 30k tokens,
- 66.7% of SWE-Agent resolved tasks required over 100k tokens.

Interpretation:

- compact, targeted retrieval can drastically reduce context cost.
- method-level search is better than broad trajectory/context accumulation.

Project implication:

PDB observations should be similarly compact and targeted.

## Plausible vs correct patches

AutoCodeRover manually validates patch correctness.

Definitions:

```text
plausible patch:
  passes given tests

correct patch:
  semantically equivalent to developer patch
```

AutoCodeRover @3 on SWE-bench Lite:

```text
78 plausible patches
51 correct patches
65.4% correctness rate
```

Important observation:

Most overfitting patches still modified the same methods as developer patches, but their code changes were wrong. Therefore, even wrong patches often helped localization.

Causes of overfitting:

1. LLM capability limits,
2. insufficient context,
3. misleading preliminary patch in issue description,
4. issue description mentions only one case, while developer patch handles broader similar cases.

Project implication:

- PDB/runtime evidence may improve wrong-patch cases if location is already right but semantics are wrong.
- Evaluation should track:
  - correct location / wrong patch,
  - correct file / wrong method,
  - wrong file,
  - no patch.

## Failure taxonomy / challenge taxonomy

AutoCodeRover classifies SWE-bench Lite tasks into:

```text
Success
Wrong patch
Wrong location in correct file
Wrong file
No patch
```

Distribution:

```text
Success: 26.0%
Wrong patch: 29.3%
Wrong location in correct file: 20.0%
Wrong file: 18.0%
No patch: 6.7%
```

Interpretation:

- The largest unresolved category is not localization failure, but wrong patch after correct method-level localization.
- More fine-grained intra-procedural analysis and specification inference are needed.
- For tasks with few issue hints but reproduction examples, generated tests + execution analysis/SBFL may help.
- For tasks with only vague natural language and no reproducible example, human involvement may be needed.

Project implication:

This is a strong argument for PDB:

```text
If the method is already localized but the patch is wrong,
runtime state may help infer the actual semantic fix.
```

## Future directions from the paper

Relevant future directions:

1. Issue reproducer:
   - generate bug reproduction tests from GitHub issue description,
   - use reproduction tests for patch validation/regeneration.

2. Semantic artifacts:
   - static call graph,
   - language server / jump-to-definition,
   - forward data-dependence analysis.

3. Human involvement:
   - LLM alone may not always decide context locations or termination correctly,
   - human involvement criteria are needed.

Project implication:

Potential future extensions after MVP:

```text
issue-to-reproduction-test generation
static call graph
language server indexing
data-dependence trace
human escalation rules
```

## Limitations / threats

Important threats:

1. LLM randomness:
   - mitigated by three experimental repetitions and released replication package.

2. Plausible may not be correct:
   - mitigated by manual semantic validation against developer patches.

3. Tests are incomplete specifications:
   - patches can overfit.

4. Issue descriptions can be incomplete or misleading:
   - preliminary patches or specific cases can mislead the LLM.

5. Search API coverage:
   - missing/unimplemented APIs reduce agent performance.

6. Dependency on GPT-4 and SWE-bench setup:
   - not directly portable to local small models or our first prototype.

## What applies to our project

Strongly reusable:

1. AST/program-structure-aware retrieval.
2. Retrieval APIs for classes/methods/snippets.
3. Stratified iterative search.
4. Concise class signature output.
5. Method implementation retrieval.
6. Search results as context, not raw files.
7. SBFL as hint source, not authority.
8. Patch generation from localized methods and retrieval history.
9. Retry loop with apply/syntax/test validation.
10. Plausible vs correct distinction.
11. Failure taxonomy separating localization vs patch-generation failures.
12. Generated reproducer as future direction.
13. Semantic artifacts: call graph, LSP, data dependence.

## What does not apply directly

Not directly reusable for first MVP:

- full SWE-bench target,
- GPT-4-only pipeline,
- no PDB/debugger state,
- no explicit stack/local variable inspection,
- no direct root-cause explanation from runtime evidence,
- reliance on method-level AST retrieval alone,
- pass@3 repeated full runs as default,
- broad GitHub issue feature-addition scope.

## Relation to other papers

### Compared with SWE-Agent

SWE-Agent emphasizes interface design for file/search/edit/shell.

AutoCodeRover emphasizes program-structure-aware retrieval.

Combined lesson:

```text
Use SWE-Agent ACI principles to design robust tools.
Use AutoCodeRover retrieval APIs to make search semantic/structural.
```

### Compared with RepairAgent

RepairAgent gives state machine and repair-specific tool control.

AutoCodeRover gives AST retrieval and stratified context search.

Combined lesson:

```text
State-guided controller
+
stratified retrieval
+
patch/test validation
```

### Compared with LDB

LDB shows runtime state helps semantic debugging.

AutoCodeRover shows method-level localization often succeeds but patch content remains wrong.

Combined lesson:

```text
Runtime state is most useful after method-level localization,
when the system needs intra-procedural semantic guidance.
```

### Compared with ChatDBG/debug-gym

ChatDBG/debug-gym motivate debugger access.

AutoCodeRover shows what debugger evidence should feed into:

```text
not free-form chat,
but structured context retrieval and patch generation.
```

## PDB MVP implications

The PDB MVP should not be just:

```text
stack trace -> ask LLM -> patch
```

It should be:

```text
issue/failure
  -> structured code retrieval
  -> source/method context
  -> PDB runtime evidence
  -> root-cause hypothesis
  -> patch
  -> verifier
```

Recommended combined command set:

```text
find_class(name)
find_function(name)
search_code(snippet)
get_function_source(name)
get_stack_summary()
get_frame_locals(frame)
get_source_window(file, line)
safe_eval_expression(frame, expr)
apply_patch(patch)
run_tests(command)
revert_patch()
```

## Suggested architecture update

```text
Controller states:

1. Reproduce
   - run_tests
   - capture traceback

2. Static/structural retrieval
   - find_class
   - find_function
   - search_code
   - get_function_source

3. Runtime evidence
   - get_stack_summary
   - get_frame_locals
   - safe_eval_expression
   - get_source_window

4. Hypothesis
   - explain root cause using code + runtime evidence

5. Patch
   - apply deterministic patch
   - syntax/lint check

6. Validate
   - reproduction test
   - regression tests
   - classify outcome

7. Retry / stop
```

## Evaluation categories inspired by AutoCodeRover

Track failures as:

```text
success
wrong patch but correct method
wrong location in correct file
wrong file
no patch
syntax/apply failure
regression
```

Add PDB-specific categories:

```text
runtime evidence not collected
runtime evidence misread
correct runtime diagnosis but wrong patch
PDB unnecessary / static baseline sufficient
```

## Project decisions after reading

- [x] Add AST/program-structure-aware retrieval as core future component.
- [x] Treat PDB evidence as an augmentation to structural retrieval, not a replacement.
- [x] Use stratified context retrieval pattern.
- [x] Keep class/method/snippet outputs compact.
- [x] Use SBFL analogy for PDB: dynamic evidence as hints, not absolute truth.
- [x] Evaluate wrong patch vs wrong localization separately.
- [x] Consider generated reproduction tests after MVP.
- [x] Consider LSP/call graph/data-dependence as later semantic retrieval tools.

## One-paragraph Turkish explanation for my own understanding

AutoCodeRover, repo’yu sadece dosya koleksiyonu gibi görmek yerine AST/program structure üzerinden arayan bir autonomous program improvement sistemidir. LLM issue description’dan class/method/snippet hint’leri çıkarır, sonra `search_class`, `search_method_in_class`, `search_code_in_file` gibi API’lerle iteratif ve stratified şekilde context toplar. Context yeterli olunca buggy method/location seçilir ve ayrı bir patch generation agent bu context üzerinden patch üretir. Testler varsa SBFL top suspicious method’lar ek hint olarak verilir ve patch validation loop’a eklenir. Bizim PDB projesi için ana ders şu: PDB runtime state tek başına yeterli değil; önce AST/symbol/method-level retrieval ile doğru code region bulunmalı, sonra PDB stack/locals/source-window bu region’daki semantik hatayı açıklamak ve doğru patch’i seçmek için kullanılmalı.
