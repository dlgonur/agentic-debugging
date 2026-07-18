# Tier 2 Synthesis — MVP Architecture Update v1

Date: 2026-07-18

This synthesis updates the Python/PDB debugger-agent MVP after the Tier 2 reading block:

1. LDB / Debug Like a Human
2. RepairAgent
3. SWE-Agent
4. AutoCodeRover
5. OpenHands

Tier 1 established the research direction:

```text
Python/PDB-first debugger-assisted repair agent
compared against an Agentless-style static/test-feedback baseline
evaluated with SWE-bench-inspired patch/test outcome metrics
```

Tier 2 refines the engineering shape of that system.

---

## 1. Updated Executive Decision

The MVP should be a small, testable, Python-only debugging-agent platform:

```text
agent/controller
  -> typed action
  -> sandboxed/runtime execution
  -> structured observation
  -> event log
  -> next action
```

The MVP should not be a full generalist platform.

The updated system target is:

```text
Python/PDB-first
single-controller
state-machine guided
typed tool/skill interface
event-stream logged
sandboxed execution
AST/symbol/source retrieval
runtime-state observation
deterministic patch application
test/verifier loop
golden trajectory tests
```

Still out of scope:

```text
multi-agent orchestration
web/browser tools
GUI
full OpenHands reimplementation
GDB/LLDB
fine-tuning
DPO/RLHF
full SWE-bench leaderboard runs
```

---

## 2. What Each Tier 2 Paper Added

### 2.1 LDB

LDB strengthens the runtime-state argument.

Key lesson:

```text
Runtime execution information beats pure self-reflection when debugging semantic bugs.
```

Reusable idea:

```text
code region + before/after state + task expectation -> local correctness verdict
```

MVP adaptation:

```text
Full CFG/basic-block instrumentation is too heavy for v1.

Use a frame/window-level approximation:
  failing test
  -> traceback
  -> failing frame
  -> source window
  -> locals
  -> caller frame if needed
  -> root-cause verdict
```

### 2.2 RepairAgent

RepairAgent gives the repair-agent control model.

Key lesson:

```text
Autonomy needs a finite state machine, memory, hypothesis management, and tool budgets.
```

Reusable ideas:

```text
dynamic prompt
state-guided available tools
express_hypothesis / discard_hypothesis
patch/test/revert loop
repetition detection
invalid tool-call handling
cycle budget
```

MVP adaptation:

```text
Do not let the LLM freely call every tool.
Use states:
  reproduce
  understand
  gather runtime evidence
  patch
  validate
  done
```

### 2.3 SWE-Agent

SWE-Agent gives the Agent-Computer Interface design principles.

Key lesson:

```text
LM performance depends heavily on the shape of the interface.
Raw shell/raw files/raw history are bad defaults.
```

Reusable ideas:

```text
small command set
bounded file viewer
search tools
edit guardrails
history compression
clear action docs
immediate feedback
failure-mode logging
```

MVP adaptation:

```text
Do not expose raw PDB terminal.
Expose a PDB ACI:
  get_stack_summary
  get_frame_locals
  get_source_window
  safe_eval_expression
```

### 2.4 AutoCodeRover

AutoCodeRover gives program-structure-aware retrieval.

Key lesson:

```text
Runtime state does not replace retrieval. It should augment AST/symbol/method-level retrieval.
```

Reusable ideas:

```text
search_class
search_method
search_code
stratified retrieval
method-level context
SBFL as hint, not authority
wrong-patch vs wrong-location taxonomy
```

MVP adaptation:

```text
Start with simple Python AST/symbol search:
  find_function
  find_class
  search_code
  get_function_source

Then combine with PDB:
  structural retrieval -> runtime evidence -> root-cause -> patch
```

### 2.5 OpenHands

OpenHands gives platform/runtime architecture.

Key lesson:

```text
Agent systems should be built as action-observation runtimes with logs, skills, sandboxing, and tests.
```

Reusable ideas:

```text
event stream
action -> observation abstraction
runtime boundary
sandbox execution
AgentSkills library
integration tests with mocked LLMs
runtime/environment reproducibility
```

MVP adaptation:

```text
Build a small PDB-debugging platform, not a one-off script.
Every tool call and observation should be logged.
```

---

## 3. Updated MVP Architecture

Recommended repo architecture for implementation:

```text
agentic_debugger/
  agent/
    controller.py
    prompts.py
    state_machine.py
    policy.py

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

  datasets/
    curated/
    bugsinpy_subset/

tests/
  unit/
  integration/
  golden_trajectories/
```

---

## 4. Controller State Machine v1

```text
State 1: Reproduce
  Goal:
    prove the bug/failure is real

  Allowed actions:
    run_tests
    run_reproduction
    get_failure_trace

  Exit:
    failure reproduced -> Understand
    cannot reproduce -> Fail/NeedHuman

State 2: Understand
  Goal:
    localize initial suspicious area

  Allowed actions:
    search_code
    find_function
    find_class
    get_source_window
    extract_failing_test
    express_hypothesis

  Exit:
    hypothesis formed -> RuntimeEvidence or Patch
    insufficient context -> more retrieval
    budget exceeded -> Fail

State 3: RuntimeEvidence
  Goal:
    collect PDB/runtime evidence relevant to hypothesis

  Allowed actions:
    get_stack_summary
    get_frame_locals
    get_source_window
    safe_eval_expression
    inspect_caller_frame
    discard_hypothesis

  Exit:
    evidence supports/refutes hypothesis -> Patch or Understand
    unsafe/irrelevant -> Understand
    budget exceeded -> Patch or Fail

State 4: Patch
  Goal:
    produce minimal deterministic patch

  Allowed actions:
    apply_patch
    syntax_check
    revert_patch

  Exit:
    patch applied -> Validate
    patch invalid -> Patch retry or Understand

State 5: Validate
  Goal:
    determine behavioral outcome

  Allowed actions:
    run_reproduction
    run_regression_tests
    classify_outcome

  Exit:
    resolved -> Done
    regression -> revert + Understand/Patch
    not fixed -> RuntimeEvidence/Patch retry
    budget exceeded -> Fail

State 6: Done
  Goal:
    final report with evidence

  Required output:
    localization
    root cause
    runtime evidence
    patch summary
    test outcome
```

---

## 5. MVP Tool Set v1

### 5.1 File/source tools

```text
open_file(path, line=None)
get_source_window(path, line, radius=50)
search_code(query, path=None)
find_function(name)
find_class(name)
get_function_source(symbol)
extract_failing_test(test_id_or_path)
```

### 5.2 Test/reproduction tools

```text
run_tests(command, timeout)
run_reproduction(command, timeout)
get_failure_trace()
```

### 5.3 PDB/runtime tools

```text
start_pdb_session(command)
get_stack_summary()
get_frame(frame_index)
get_frame_locals(frame_index, max_repr_chars=200)
safe_eval_expression(frame_index, expression)
inspect_caller_frame(frame_index)
stop_pdb_session()
```

### 5.4 Patch/verifier tools

```text
apply_patch(patch)
revert_patch()
syntax_check()
run_regression_tests(command)
classify_outcome()
```

### 5.5 Control/hypothesis tools

```text
express_root_cause_hypothesis(hypothesis, evidence_refs)
discard_hypothesis(reason)
request_more_evidence(reason)
finish(final_report)
```

---

## 6. PDB ACI Rules

Do not expose raw PDB in v1.

Expose a typed PDB ACI:

```text
get_stack_summary()
get_frame_locals(frame_index)
safe_eval_expression(frame_index, expression)
get_source_window(path, line)
```

Rules:

1. Every command has typed arguments.
2. Every command returns structured JSON-like observations.
3. Outputs are bounded and summarized.
4. Dangerous expression evaluation is rejected.
5. Function calls in expressions are disabled by default.
6. Raw shell execution through PDB is forbidden.
7. Frame indexes are validated.
8. Object repr output is truncated.
9. PDB session has a timeout.
10. PDB access is controller-gated.

---

## 7. Event Stream Schema v1

Every run should produce a trajectory.

Minimal event schema:

```json
{
  "run_id": "string",
  "task_id": "string",
  "timestamp": "iso-8601",
  "type": "message|action|observation|decision|final",
  "name": "string",
  "payload": {},
  "metadata": {
    "duration_ms": 0,
    "tool_version": "string",
    "model": "string",
    "tokens": null,
    "cost": null
  }
}
```

Example event sequence:

```json
[
  {"type": "message", "name": "task", "payload": {"issue": "..."}},
  {"type": "action", "name": "run_tests", "payload": {"command": "pytest tests/test_bug.py"}},
  {"type": "observation", "name": "test_result", "payload": {"status": "failed", "traceback": "..."}},
  {"type": "action", "name": "get_stack_summary", "payload": {}},
  {"type": "observation", "name": "stack_summary", "payload": {"frames": []}},
  {"type": "decision", "name": "root_cause_hypothesis", "payload": {"hypothesis": "..."}},
  {"type": "action", "name": "apply_patch", "payload": {"patch": "..."}},
  {"type": "observation", "name": "patch_apply", "payload": {"status": "ok"}},
  {"type": "final", "name": "outcome", "payload": {"classification": "resolved"}}
]
```

---

## 8. Evaluation Metrics v1

### Behavioral outcome

Use SWE-bench/APR-inspired categories:

```text
Resolved
Breaking Resolved
Partially Resolved
Work in Progress
No-Op
Regression
Patch Apply Failure
Syntax Failure
Timeout
```

### Localization outcome

Use AutoCodeRover-style categories:

```text
correct method/function
wrong patch but correct method
wrong location in correct file
wrong file
no patch
```

### Runtime evidence outcome

PDB-specific categories:

```text
PDB not used
PDB unnecessary / static baseline sufficient
PDB useful / changed diagnosis
PDB misread / wrong conclusion
PDB unsafe command rejected
correct runtime diagnosis but wrong patch
```

### Cost/runtime metrics

```text
number of tool calls
number of PDB observations
number of patch attempts
number of test runs
wall-clock time
token usage if API-backed
```

### Explanation quality

Manual or rubric-based:

```text
root cause cites observed runtime state
causal chain is coherent
patch explanation matches tests
does not rely only on guesswork
```

---

## 9. Baselines and Ablations

Minimum experiment set:

```text
A. static baseline
   file/source/search + patch + tests
   no PDB

B. PDB always-on
   static tools + immediate runtime evidence

C. PDB after failed patch
   first try static, then PDB after failure

D. PDB on uncertainty
   controller opens PDB only when confidence is low or failure is state-dependent
```

Expected result:

```text
PDB should not help every task.
It should help most on semantic/runtime-state bugs.
Controller-gated PDB is likely better than always-on PDB.
```

---

## 10. Dataset Strategy

Start small.

### Smoke dataset

```text
5-10 tiny Python bugs created locally
pytest-compatible
single failing test
simple regression tests
designed to exercise:
  condition bug
  None handling
  off-by-one
  wrong branch
  incorrect data mutation
  caller/callee state mismatch
```

### First real dataset

```text
small BugsInPy subset
or curated Python package bugs
```

Selection rules:

```text
must reproduce with one command
must be Python
must not require paid services
must not require GUI/browser
must have failing test or crash
should have observable runtime state
should have at least minimal regression tests
```

Do not start with full SWE-bench.

---

## 11. Testing Strategy

OpenHands strongly suggests agent systems need integration tests, not only unit tests.

### Unit tests

```text
event schema serialization
patch parser
source window extraction
search output truncation
safe_eval rejection
PDB frame parsing
outcome classifier
```

### Integration tests

```text
mocked LLM response sequence
tiny buggy script
run controller
assert expected event sequence
assert final patch applied
assert tests pass
```

### Golden trajectory tests

```text
given fixed model outputs
controller must produce stable actions/observations
```

### No full LLM in CI

The default tests should not call paid APIs. Real model runs should be manual or opt-in.

---

## 12. Implementation Roadmap After Tier 2

### Phase A — Skeleton

```text
create Python package
define event schema
define task schema
define action/observation classes
define logger
```

### Phase B — Runtime basics

```text
workspace copy
command runner
test runner
patch apply/revert
source window extraction
```

### Phase C — PDB basics

```text
run failing command under PDB or postmortem PDB
capture stack
locals
source window
safe eval
```

### Phase D — Controller

```text
state machine
tool registry
prompt templates
mocked model adapter
real model adapter later
```

### Phase E — Evaluation

```text
tiny curated bugs
static baseline
PDB variant
metrics JSON
summary markdown
```

---

## 13. Updated Research Claim

After Tier 2, the project claim should be phrased as:

> A small Python/PDB-first debugging agent can improve selected semantic bug-repair tasks by combining structured source retrieval, runtime-state evidence, state-machine-guided tool use, and deterministic patch/test validation. The key question is not whether agents should have arbitrary debugger access, but whether controlled runtime observations improve over a strong static/test-feedback baseline.

This claim is narrow enough to test and strong enough to be meaningful.

---

## 14. Updated System Summary

The intended system is:

```text
not ChatDBG clone
not SWE-Agent clone
not OpenHands clone
not full AutoCodeRover clone
not pure Agentless static repair

It is:

a Python/PDB debugging agent prototype
with:
  SWE-Agent-style ACI design
  RepairAgent-style state machine
  AutoCodeRover-style structural retrieval
  LDB-style runtime-state diagnosis
  OpenHands-style event/runtime logging
  SWE-bench-style patch/test evaluation
```

---

## 15. Immediate Next Step

Tier 2 reading is complete after OpenHands.

Next recommended project artifact:

```text
research/synthesis/tier2_mvp_architecture_update.md
```

After that, the next practical research/implementation bridge is:

```text
docs/MVP_IMPLEMENTATION_PLAN.md
```

That plan should define:

- exact Python package name,
- initial task schema,
- first 5 tiny benchmark bugs,
- first controller states,
- first PDB skills,
- first test commands,
- acceptance criteria for implementation task 1.
