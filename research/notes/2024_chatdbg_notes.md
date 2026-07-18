# Paper Notes — ChatDBG

## Bibliography

- Primary title: ChatDBG: Augmenting Debugging with Large Language Models
- arXiv title / earlier title: ChatDBG: An AI-Powered Debugging Assistant
- Authors: Kyla H. Levin, Nicolas van Kempen, Emery D. Berger, Stephen N. Freund
- Year: 2024 arXiv preprint; 2025 PACMSE/FSE publication
- Venue: Proceedings of the ACM on Software Engineering, FSE 2025
- DOI: 10.1145/3729355
- arXiv: 2403.16354
- Local PDF path: research/papers/tier1_must_read/2024_chatdbg_ai_powered_debugging_assistant.pdf
- Access level: FULL_TEXT_READ

## Why this paper matters

ChatDBG is the closest direct prior art for this project. It is not merely a repository-level repair agent. It integrates an LLM into real debuggers and lets the model inspect runtime state, navigate stack frames, query variables, retrieve source/documentation, and answer natural-language debugging questions.

For our project, ChatDBG provides the strongest justification for a Python/PDB-first debugger-assisted agent. It also gives concrete design ideas for prompt construction, debugger command wrappers, enriched stack traces, command sanitization, and evaluation.

## Problem

Traditional debuggers expose useful runtime state, but the programmer must still decide which stack frame, variable, object, condition, or source location to inspect. Debugging remains expensive because the root cause may be distant from the visible failure and because stack traces, heap state, and source context can be large.

LLM code repair can help with reasoning, but ordinary LLM repair usually sees only static code, error text, and test output. ChatDBG addresses this missing evidence channel by connecting the LLM to the debugger itself.

## Core idea

ChatDBG lets the user ask natural-language questions inside a debugger session, for example:

- Why did this crash?
- Why is this variable null?
- Why is this value different than expected?
- Why did this assertion fail?

The LLM can then "take the wheel" and issue debugger commands through controlled function calls. The debugger executes the command, returns the output, and the LLM uses that observation to continue reasoning. After answering, control returns to the programmer.

## System / architecture

ChatDBG sits between three actors:

1. User
2. Existing debugger such as Pdb, LLDB, or GDB
3. LLM

Command flow:

1. If the user enters a normal debugger command, ChatDBG dispatches it directly to the underlying debugger.
2. ChatDBG saves the command and output in a history buffer.
3. If the user enters free-form natural language, ChatDBG builds a prompt.
4. For the first chat step, the prompt includes:
   - instructions,
   - enriched stack trace,
   - program inputs when available,
   - error description,
   - previous debugger command history,
   - user question.
5. For later chat steps, the prompt includes the new history since the last chat step plus the new user message.
6. The LLM response stream may contain prose and function calls.
7. When the LLM calls `debug(command)`, ChatDBG executes the command in the debugger, prints it to the user, and sends the output back to the LLM.
8. Once the response is complete, ChatDBG returns control to the user.

Important architecture point for our project:

- ChatDBG is not fully autonomous patching. It is an interactive assistant. It diagnoses and recommends fixes, but does not directly edit the code.

## Debugger support

Confirmed support:

- Pdb for Python
- IPython/Jupyter via Python debugging workflows
- LLDB for C/C++ native code
- GDB support
- subset/ported features for WinDBG
- Rust native debugging through LLDB-style native setup, with extra steps in the paper example

Python setup is minimal:

- install `chatdbg`
- run `chatdbg target.py`
- for IPython/Jupyter use `%pdb` / `--pdb` style flows
- Python does not need debug symbols because source/runtime state are available through the managed runtime

Native setup is heavier:

- use LLDB
- compile with `-g`
- use unstripped binary
- use DWARF debug information
- for the paper's C/C++ evaluation: Clang + LLDB 17 with `-g -Og -fno-omit-frame-pointer`

## Take-the-wheel mechanism

The take-the-wheel mechanism uses LLM function calling.

Main function:

```text
debug(command)
```

For Python, this calls Pdb command processing, e.g.:

```text
debug("p len(stats)")
```

and captures the output.

For LLDB, it uses LLVM command interpreter functionality.

The LLM is instructed to use debugger commands to inspect variables, stack frames, source, and expressions. It does not need special fine-tuning in the paper; the authors argue that general LLM debugger knowledge is enough for common operations such as stack navigation, variable printing, expression evaluation, and heap inspection.

Important for our MVP:

- We can implement a smaller deterministic PDB command schema instead of exposing raw PDB.
- The minimum useful set is likely:
  - `bt`
  - `up`
  - `down`
  - `p expression`
  - `list`
  - `info symbol`
  - possibly `where`
  - possibly controlled `step`, `next`, `continue` later

## Initial prompts and enriched stack traces

The initial prompt includes instructions and an enriched stack trace.

The instructions tell the LLM to:

- answer root-cause questions,
- focus on user code,
- explain variable values that contribute to the error,
- continue explanation until root cause,
- end with either a fix or debugging suggestions.

The enriched stack trace includes:

- function/file/line information,
- larger source windows, default at least 10 lines,
- local variables,
- global variables when relevant,
- variable types,
- abbreviated variable values,
- hidden/elided library frames.

Python implementation details:

- uses Pdb internal data structures,
- uses string conversion / `__repr__`,
- recursively serializes aggregate data structures,
- limits structure depth to avoid huge prompts,
- elides large structures with ellipses.

Our design implication:

- Do not simply pass raw traceback text.
- Build a structured enriched failure context:
  - stack frames,
  - locals,
  - source windows,
  - exception,
  - prior commands,
  - test/assertion context.

## Code navigation commands

ChatDBG adds commands to help the LLM inspect code beyond the current stack.

Important commands:

| Command | Debugger | Meaning |
|---|---|---|
| `info symbol` | Pdb | source code and/or docstring for function, method, field, class, package |
| `slice symbol` | Pdb/IPython/Notebook | backwards slice for notebook/global symbol |
| `code loc` | LLDB | source around filename:line |
| `definition loc symbol` | LLDB | declaration/definition for a symbol at a source location |

For Python, `info` is implemented with `inspect` and `pydoc`.

For LLDB, `code` and `definition` use clangd / language-server functionality because native debuggers do not expose Python-style introspection.

Our design implication:

- For MVP, implement `info_symbol` for Python first.
- Use `inspect.getsource`, `inspect.getdoc`, and source file reading.
- Notebook slicing is useful but should not be in the first MVP unless notebook support becomes required.

## Security and risks

The paper explicitly recognizes that LLM-issued debugger commands can run arbitrary code.

Security mitigation:

- Python: sanitize LLM-generated debugger commands.
- Python sanitizer: only allow function calls from a user-configurable whitelist.
- Native code: provenance is harder, so commands calling functions are rejected.
- `--unsafe` exists for isolated environments where sanitization can be disabled.
- ChatDBG does not directly apply code fixes. It presents recommendations to the user, who vets them.

Our design implication:

- Never expose raw `p <arbitrary Python expression>` without policy.
- Prefer a controlled expression evaluator or narrow PDB command schema.
- Run experiments in an isolated environment.
- Patch application must be deterministic and verifier-gated.
- LLM should propose, not directly mutate without controller approval.

## Evaluation

### Python evaluation

Dataset:

- 22 unpublished Python programs from two introductory CS courses.
- c1-c8: non-interactive command-line scripts.
- s1-s14: Jupyter notebooks.
- Bugs include crashes and semantic assertion failures.
- Programs are unpublished, reducing training-data leakage risk.
- Correctness criteria are clear through assertions / assignment requirements.
- Bugs were real human mistakes, not synthetic mutations.

Configurations:

1. Default Stack
   - standard stack trace
   - no take-the-wheel
   - initial prompt: `why?`

2. Enriched Stack
   - enriched stack trace
   - no take-the-wheel
   - initial prompt: `why?`

3. +Take the Wheel
   - enriched stack
   - LLM can issue debugger commands
   - initial prompt: `why?`

4. +Targeted Question
   - enriched stack
   - take-the-wheel
   - specialized neutral question about expected behavior

5. +Dialog
   - same as targeted question
   - adds one generic follow-up:
     - continue explaining reasoning and give a fix

Model:

- `gpt-4-1106-preview`

Metrics:

- Manual success judgment.
- A response is successful if it includes:
  - accurate explanation of the error,
  - actionable fix,
  - either code or fully explicit prose fix.
- Criteria were defined before examining responses.

Key results:

- Simple `why?`: 57% success.
- Targeted question: 67% success.
- One additional dialog step: 85% success.
- +Take the Wheel used 0-12 debugger commands per run.
- Common commands: `info`, `slice`, `p`.
- `slice` was especially important for notebooks.
- Average targeted-question interaction:
  - about 10,000 tokens,
  - about 25 seconds,
  - about $0.12 USD under then-current OpenAI pricing.

### C/C++ evaluation

Dataset:

- 8 real-world native-code bugs from BugBench and BugsC++.
- Programs include BC, GZIP, NCOM, PEG, POLY, TIFF, YAML1, YAML2.
- Bug types mainly memory safety and crash-related issues:
  - buffer overflow,
  - null dereference,
  - division by zero,
  - stack overflow,
  - assertion failure.

Setup:

- x86 Ubuntu 22.04 server.
- Clang + LLDB 17.
- Compiler flags:
  - `-g`
  - `-Og`
  - `-fno-omit-frame-pointer`
- Model:
  - `gpt-4-1106-preview`
- Some cases used AddressSanitizer to force crash at memory violation.

Metric:

- Manual judgment of whether ChatDBG suggested:
  - a fix for the root cause, or
  - a fix for the proximate crash symptom.

Key results:

- ChatDBG was strong at diagnosing and explaining crashes.
- Root-cause fixes: 36%.
- Additional proximate-cause fixes: 55%.
- Proximate fixes may stop a crash but still fail to address the deeper semantic/design cause.

## Key findings

1. Runtime debugger state improves the evidence available to the LLM.
2. Enriched stack traces help, but take-the-wheel is the major technical contribution.
3. User-provided behavioral context improves success.
4. Multi-step dialog helps; a second query can let the model complete reasoning that did not fit or converge in the first answer.
5. Python/PDB is a strong first target because Python runtime/source introspection is much simpler than native debugging.
6. ChatDBG can recommend fixes, but it is not a full autonomous repair system.
7. For C/C++, ChatDBG often fixes symptoms/proximate causes rather than true root causes.
8. The paper's evaluation validates the direction but does not replace our need for verifier-backed patch validation.

## Limitations

Evaluation limitations:

- Python suite consists of student programs, not large professional repositories.
- Python programs are unpublished, which helps leakage control, but may not represent production code.
- C/C++ programs and fixes may be present on GitHub, creating possible training-data leakage.
- C/C++ suite is dominated by memory errors; other bug classes may differ.
- Manual evaluation of explanations/fixes introduces subjectivity, though criteria were predefined.
- Results may depend on GPT-4 prompt engineering.
- Prompt length can hurt model performance.
- LLM sometimes suggests changes that introduce other bugs.
- ChatDBG does not compare directly against Agentless, SWE-Agent, or AutoCodeRover on a common benchmark.
- ChatDBG does not directly apply and validate patches through tests.
- Root-cause correctness is partly inferred through manual judgment and suggested fixes, not through a formal RCA metric.

## Future work from the paper

The authors identify several directions:

- incorporate existing fault localization methods,
- incorporate delta debugging,
- integrate time-travel debugging,
- explore fine-tuning or additional training so the LLM can use more sophisticated debugging tools effectively.

Our project can directly use these as research gaps.

## What applies to our project

Reusable ideas:

- PDB-first implementation
- enriched stack trace construction
- model-visible debugger command outputs
- function-call tool interface
- `info symbol` style source/doc lookup
- controlled command schema
- root-cause-oriented prompt
- final answer format ending with recommendation/fix
- safety layer before executing model-generated commands
- user/model interaction history as evidence
- ablation study design:
  - stack only,
  - enriched stack,
  - debugger access,
  - targeted question,
  - dialog/follow-up.

## What does not apply directly

Not directly reusable:

- user-in-the-loop assistant workflow as the final system
- relying on proprietary GPT-4 as the only model
- manual-only patch validation
- student-script/notebook evaluation as the only benchmark
- C/C++ memory-error-heavy evaluation as evidence for all bug types
- unbounded raw debugger command execution
- presenting a fix without deterministic test validation
- treating proximate-cause fixes as equivalent to root-cause fixes

## Claims verified

- Official title/version differences verified.
- Official author list verified.
- DOI and FSE/PACMSE metadata verified.
- Debugger support verified: Pdb, LLDB, GDB; subset features ported to WinDBG.
- Python results verified: 57%, 67%, 85%.
- C/C++ results verified: 36% root-cause fix + 55% proximate-cause fix.
- Security mitigation verified: sanitization/whitelist for Python, reject function calls for native, `--unsafe` for isolated environment.
- Implementation suitability: conceptually very suitable for our PDB adapter, but direct implementation should be simplified and verifier-backed.

## Project decisions after reading

- [x] Clone the concept of enriched stack traces.
- [x] Clone the concept of controlled debugger function calls.
- [x] Clone the concept of `info symbol` for Python.
- [x] Clone the root-cause-oriented prompt style.
- [x] Do not clone raw user-in-the-loop workflow as final architecture.
- [x] Do not allow unbounded raw PDB command execution in MVP.
- [x] Do not treat ChatDBG's evaluation as sufficient for our final evaluation.
- [x] Add deterministic verifier and test execution as mandatory MVP components.
- [x] Compare against a static/test-feedback baseline instead of only reporting standalone success.

## Candidate PDB adapter requirements for our MVP

Minimum action schema:

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

Optional later actions:

```text
step()
next()
continue()
set_breakpoint(file_path, line)
clear_breakpoint(id)
slice_symbol(symbol)
```

Safety rules:

1. No arbitrary shell execution from debugger actions.
2. Expression evaluation must be restricted or sandboxed.
3. Function calls inside expressions should be disabled by default.
4. Any unsafe mode must require explicit isolated sandbox.
5. LLM proposes patches; deterministic patch tool applies them.
6. Verifier must run reproduction and regression tests before accepting a patch.

## One-paragraph Turkish explanation for my own understanding

ChatDBG, klasik debuggerların sunduğu stack frame, değişken, source code ve execution state gibi bilgileri LLM tarafından sorgulanabilir hale getiren bir AI debugging assistant sistemidir. Temel farkı, modelin sadece statik kod veya test çıktısına bakmaması; debugger üzerinden canlı program durumunu inceleyebilmesidir. Paper, Pdb/LLDB/GDB entegrasyonu ile LLM'in debugger komutları çalıştırabildiğini, enriched stack trace ve `info` gibi yardımcı komutlarla runtime state + source context alabildiğini gösterir. Python tarafında sonuçlar güçlüdür; targeted question ile 67%, bir follow-up ile 85% başarı raporlanmıştır. Ancak sistem hâlâ kullanıcıya öneri veren bir asistandır; patch’i otomatik uygulayıp test eden bir verifier yoktur. Bu yüzden bizim proje için ChatDBG ana ilham kaynağıdır ama hedefimiz bunu Python/PDB-first, deterministic tool wrapper kullanan, verifier-backed ve baseline karşılaştırmalı bir agentic debugging prototipine dönüştürmektir.
