# PDB Debugger Agent MVP Rationale v1

Date: 2026-07-18

This synthesis is based on the first Tier 1 reading block:

1. ChatDBG
2. debug-gym
3. Agentless
4. SWE-bench

Purpose: define why the first implementation should be a Python/PDB-first debugger-assisted agent, what it should compare against, what should be out of scope, and how it should be evaluated.

---

## 1. Executive Decision

The first credible MVP should be:

```text
Python/PDB-first
single-controller agent
deterministic tool wrappers
runtime-state inspection
patch generation
patch/test verifier
Agentless-style static baseline comparison
```

The MVP should not start with:

```text
GDB/LLDB support
multi-agent orchestration
fine-tuning
DPO/RLHF
full SWE-bench leaderboard evaluation
large-scale local model training
```

The central research question is:

> Does controlled PDB/runtime-state evidence improve localization, root-cause explanation, and patch correctness over a strong static/test-feedback baseline?

---

## 2. Why PDB First?

### 2.1 Python has the lowest debugger integration cost

Python/PDB gives immediate access to:

- traceback,
- current frame,
- stack frames,
- local variables,
- source windows,
- expression evaluation,
- exception state.

Unlike native debugging, Python does not require:

- debug symbols,
- DWARF interpretation,
- compiler flags,
- address-to-source mapping,
- memory inspection safety policies at the same level as C/C++.

Therefore, PDB is the fastest route to proving or falsifying the project idea.

### 2.2 ChatDBG directly validates debugger-assisted LLM interaction

ChatDBG shows that an LLM can be integrated into real debuggers and can use debugger commands to inspect program state. Its core value for us is not the exact implementation, but the architectural claim:

> LLM debugging becomes more grounded when the model can query runtime state instead of only reading static code and error text.

Reusable ideas from ChatDBG:

- enriched stack traces,
- model-visible debugger observations,
- controlled debugger command interface,
- `info symbol` style source/doc lookup,
- root-cause-oriented prompts,
- command sanitization,
- user/model/debugger history as evidence.

However, ChatDBG is still primarily an assistant, not a full autonomous repair system. Our MVP must add deterministic patch application and test validation.

### 2.3 debug-gym suggests debugger access should be controller-gated

debug-gym is important because it tests interactive debugging behavior with PDB-like tools. Its most important lesson is not simply "PDB helps"; the stronger lesson is:

> PDB helps when used by capable models and when access is introduced at the right point in the repair loop.

Therefore, our controller should not expose PDB blindly from step zero in every case. Better candidates:

- open PDB after failed static patch attempts,
- open PDB when localization confidence is low,
- open PDB when the failure is runtime-state-dependent,
- skip PDB when the issue is simple and static.

---

## 3. Why Agentless Is the Baseline to Beat

Agentless shows that many benefits attributed to complex autonomous agents can be recovered with a simple staged pipeline:

```text
issue + repository
  -> localization
  -> repair
  -> patch validation
```

Important reusable ideas:

- hierarchical localization,
- file tree / skeleton representations,
- Search/Replace patch generation,
- candidate patch sampling,
- generated reproduction tests,
- regression test filtering,
- majority-vote / patch normalization,
- low-cost fixed control flow.

For our project, Agentless is the correct non-debugger baseline because it is:

- simpler than open-ended agents,
- strong on SWE-bench Lite,
- cheaper than many autonomous systems,
- easier to reproduce conceptually,
- directly focused on patching and validation.

The debugger-assisted system must justify its extra complexity against this style of baseline.

---

## 4. Why SWE-bench Is Not the First MVP Target

SWE-bench is the benchmark vocabulary and future extension target, but full SWE-bench should not be the first MVP benchmark.

Reasons:

1. Full repository environments are expensive to set up.
2. Many tasks require broad repository navigation, not necessarily debugger interaction.
3. Some tasks are feature requests or behavioral changes rather than crash/debugger-suitable bugs.
4. Full SWE-bench adds engineering burden before the debugger hypothesis is tested.
5. The project should first isolate the variable of interest: runtime state.

However, SWE-bench gives us important evaluation concepts:

- issue + base commit + patch task formulation,
- patch application,
- fail-to-pass tests,
- pass-to-pass tests,
- regression preservation,
- execution-based validation,
- failure outcome taxonomy.

Therefore:

```text
First MVP benchmark: BugsInPy or curated Python/PDB bug set
Future benchmark: SWE-bench Lite / SWE-bench Verified subset
Evaluation language: SWE-bench-inspired F2P/P2P verifier
```

---

## 5. Static vs Dynamic Debugging Taxonomy

### Level 1 — Static code reading

Input:

- issue text,
- files,
- source snippets.

No execution.

Example:

```text
LLM reads code and proposes patch.
```

Strength:

- cheap,
- simple,
- works for obvious bugs.

Weakness:

- can hallucinate runtime behavior,
- may miss state-dependent failures,
- weak causal grounding.

### Level 2 — Static retrieval / RAG

Input:

- issue text,
- retrieved files,
- file tree,
- embeddings/BM25,
- symbols.

No direct runtime state.

Example:

```text
Agentless-style localization with file skeletons.
```

Strength:

- scalable to large repositories,
- good baseline,
- lower risk.

Weakness:

- retrieval may miss the key file,
- large contexts distract the model,
- still lacks runtime evidence.

### Level 3 — Error-message / stack-trace assisted static repair

Input:

- exception traceback,
- failing test output,
- relevant source.

No interactive debugger.

Strength:

- cheap runtime signal,
- stack trace gives good initial localization.

Weakness:

- traceback only shows failure surface,
- not enough for hidden state/control-flow causes.

### Level 4 — Test-feedback iterative repair

Input:

- tests are run after patches,
- model sees pass/fail logs.

Strength:

- validates behavior,
- enables retry loops.

Weakness:

- test output may not reveal why the bug exists,
- can overfit to tests.

### Level 5 — Execution-trace assisted repair

Input:

- logs,
- traces,
- instrumented execution.

Strength:

- more dynamic evidence than tests alone.

Weakness:

- instrumentation complexity,
- traces can be large/noisy.

### Level 6 — Runtime-state assisted debugging

Input:

- stack frames,
- locals,
- source windows,
- selected expressions.

Strength:

- strong evidence for root cause.

Weakness:

- unsafe if arbitrary expression evaluation is allowed,
- requires controller policy.

### Level 7 — Interactive debugger-assisted agent

Input/tooling:

- PDB/LLDB/GDB commands,
- breakpoints,
- stepping,
- variable inspection.

Strength:

- closest to human debugging.

Weakness:

- tool-use errors,
- longer trajectories,
- harder evaluation.

### Level 8 — Autonomous debugger-control repair agent

Input/tooling:

- debugger access,
- repository search,
- patching,
- tests,
- verifier,
- retry policy.

This is the target architecture, but the MVP should implement the smallest useful subset.

---

## 6. Repository Agent vs Debugger Agent

| Dimension | Repository/static agent | Debugger-assisted agent |
|---|---|---|
| Primary evidence | issue text, files, tests | issue text, files, tests, stack, locals, runtime state |
| Tooling | search, edit, run tests | search, edit, run tests, PDB commands |
| Strength | broad repo repair | state-dependent diagnosis |
| Weakness | can guess runtime behavior | tool-use complexity and safety risk |
| Baseline example | Agentless | ChatDBG/debug-gym style systems |
| Best suited for | known code changes, broad issue resolution | crashes, exceptions, failing tests, state bugs |
| Evaluation | patch passes tests | patch passes tests + root-cause evidence quality |

The project contribution should not claim that debugger agents universally dominate repository agents. The claim should be narrower:

> Controlled debugger access improves certain classes of bugs where runtime state is necessary or highly informative.

---

## 7. Fault Localization vs Root-Cause Analysis

Fault localization asks:

```text
Where is the suspicious code?
```

Root-cause analysis asks:

```text
Why did this code produce the failure?
```

Example distinction:

- FL answer: `buggy.py:42 is suspicious`.
- RCA answer: `items is empty because the filter drops every element when threshold is None; line 42 divides by len(items), causing ZeroDivisionError`.

For this project, the agent should output both:

```text
localization:
  file:
  function:
  line/span:
  confidence:

root_cause:
  observed runtime state:
  causal chain:
  why failure occurs:
  why patch fixes it:
```

PDB matters mostly for RCA, not just localization.

---

## 8. APR Patch Plausibility vs Correctness

Automated Program Repair distinguishes:

- plausible patch: passes available tests,
- correct patch: semantically fixes the bug without unintended regressions.

SWE-bench-style tests improve plausibility checking, but tests do not prove correctness.

Therefore, our verifier should combine:

1. reproduction/fail-to-pass tests,
2. regression/pass-to-pass tests,
3. patch application check,
4. root-cause explanation consistency check,
5. optional static/code-style checks later.

Minimum accepted result:

```text
Patch applies.
Original failure is reproduced before patch.
Original failure passes after patch.
Regression tests still pass.
Agent explanation matches observed runtime evidence.
```

---

## 9. System Capability Matrix v1

| System | Repo search | Patch generation | Test validation | Runtime state | Interactive debugger | Full autonomous repair | Role for our project |
|---|---:|---:|---:|---:|---:|---:|---|
| SWE-bench baseline models | Partial | Yes | Evaluated externally | No | No | No | Benchmark substrate |
| Agentless | Yes | Yes | Yes | No | No | Partial/fixed pipeline | Strong non-debugger baseline |
| SWE-agent | Yes | Yes | Yes | Usually no first-class debugger | No/limited | Yes | Agent interface reference |
| AutoCodeRover | Yes | Yes | Yes | SBFL optional | No first-class debugger | Yes | Retrieval/SBFL reference |
| OpenHands | Yes | Yes | Yes | Env/tool dependent | Not debugger-first | Yes | Platform/agent architecture reference |
| ChatDBG | Limited repo focus | Suggests fixes | No autonomous verifier | Yes | Yes | No | Direct debugger-assistant prior art |
| debug-gym | Environment/task dependent | Yes | Yes | Yes | PDB | Agent benchmark environment | PDB experiment blueprint |
| Proposed MVP | Yes, small-scale | Yes | Yes | Yes | PDB only | Bounded | Research prototype |

---

## 10. Proposed MVP Architecture

```text
Input:
  - Python project or script
  - failing test or crash command
  - optional issue text

Controller:
  1. reproduce failure
  2. collect traceback and test output
  3. run static localization
  4. decide whether to enter PDB
  5. collect debugger observations
  6. ask model for root-cause hypothesis
  7. ask model for patch
  8. apply patch deterministically
  9. run reproduction + regression tests
  10. accept, retry, or fail with evidence
```

## 11. Minimum Tool Schema

```text
run_command(command, timeout)
run_tests(command, timeout)
get_failure_trace()
get_stack()
get_frame(frame_index)
get_locals(frame_index)
eval_expression(frame_index, expression)
get_source_window(file_path, line, radius)
info_symbol(symbol)
apply_patch(diff)
revert_patch()
```

## 12. Safety Rules

1. PDB expression evaluation must be restricted.
2. No raw shell execution through the debugger.
3. Function calls inside expressions disabled by default.
4. Patch application is deterministic.
5. Tests run in a bounded subprocess.
6. Retry budget is fixed.
7. Agent must cite observed evidence in its root-cause explanation.
8. Unsafe mode only in isolated disposable environments.

---

## 13. First Experiment Design

### Baseline A — Static/Test Feedback

```text
Input:
  failing test/trace + source snippets

Allowed tools:
  file read
  code search
  run tests
  apply patch

No PDB.
```

### Variant B — PDB Always On

```text
Same as baseline, but PDB state is collected immediately after failure.
```

### Variant C — PDB After Failed Static Attempt

```text
Start as baseline.
If first patch fails, enter PDB and gather runtime evidence.
```

### Variant D — PDB On Uncertainty

```text
Enter PDB only when:
  - localization confidence is low,
  - traceback points to generic/helper code,
  - failure depends on variable values,
  - previous patch fails.
```

Expected initial conclusion:

- PDB should help most on state-dependent bugs.
- PDB may be unnecessary or harmful for simple static bugs.
- Controller gating is likely better than always-on debugging.

---

## 14. Recommended First Dataset

Start with:

```text
Curated Python/PDB bug set
or
Small BugsInPy subset
```

Selection criteria:

- reproducible with one command,
- pytest-compatible if possible,
- clear failing test or crash,
- no external paid services,
- no large datasets,
- no GUI/multimodal requirement,
- bug reachable through runtime execution,
- enough regression tests to detect breakage.

Avoid initially:

- full SWE-bench,
- non-Python bugs,
- flaky tests,
- environment-heavy packages,
- feature requests without failure reproduction,
- issues requiring images.

---

## 15. Immediate Next Tasks

1. Create final Tier 1 synthesis commit.
2. Start `research/synthesis/pdb_debugger_agent_mvp_rationale.md`.
3. Mark tracker items:
   - 1.4.1 static vs dynamic taxonomy
   - 1.4.2 repo-agent vs debugger-agent comparison
   - 1.4.3 FL vs RCA comparison
   - 1.4.4 APR plausible vs correct patch comparison
   - 1.5.6 system capability matrix v1
4. Next reading block after this:
   - LDB
   - RepairAgent
   - AutoCodeRover
   - SWE-agent
   - OpenHands
