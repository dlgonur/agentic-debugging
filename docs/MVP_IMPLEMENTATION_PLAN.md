# Agentic Debugging MVP Implementation Plan

## 1. Document Status and Purpose

Status: proposed implementation baseline, pending Onur's acceptance.

This document converts the completed Tier 1 and Tier 2 research into an executable implementation sequence for the first agentic debugging prototype.

It is a planning artifact only. It does not authorize source-code implementation until Onur reviews and accepts it.

Primary source documents:

- `TODO.md`
- `docs/PROJECT_TRACKER.md`
- `research/synthesis/pdb_debugger_agent_mvp_rationale.md`
- `research/synthesis/tier2_mvp_architecture_update.md`

The plan intentionally defers Tier 3 and supporting-paper reading. Those papers may be reopened later only when a concrete report claim, implementation decision, or evaluation ambiguity requires additional evidence.

## 2. Research-to-Implementation Baseline

The first MVP tests one narrow research question:

> Does controlled PDB/runtime-state evidence improve localization, root-cause diagnosis, and patch correctness over a strong static/test-feedback baseline on small Python bugs whose failures expose useful runtime state?

The implementation baseline is:

- Python/PDB-first;
- one controller, not a multi-agent repair team;
- deterministic typed tools instead of raw shell or raw PDB access;
- structural source retrieval before runtime inspection;
- controller-gated PDB access rather than unconditional debugger use;
- deterministic patch application, revert, and test verification;
- action/observation event logging for every run;
- SWE-bench-inspired fail-to-pass and pass-to-pass outcome evaluation;
- a static/test-feedback baseline that can run without PDB;
- five curated Python bugs before any larger dataset integration.

The MVP is not intended to reproduce ChatDBG, SWE-Agent, OpenHands, AutoCodeRover, Agentless, or SWE-bench in full. It combines only the smallest useful architectural ideas required to test the runtime-evidence hypothesis.

## 3. MVP Objective

The MVP must execute a complete, inspectable repair trajectory for a small Python task:

1. reproduce a failing test;
2. collect the failure trace;
3. retrieve relevant source structure;
4. form a root-cause hypothesis;
5. decide whether PDB evidence is required;
6. collect bounded runtime evidence when allowed;
7. apply one minimal patch attempt at a time;
8. run fail-to-pass and pass-to-pass tests;
9. revert unsuccessful or regressing patches;
10. classify the outcome;
11. save an event trajectory and final report.

The first credible milestone is not model quality. It is a deterministic platform where the same task, fixed model outputs, and fixed tool observations produce a stable trajectory and verifiable outcome.

## 4. Locked MVP Decisions

### 4.1 Package and Distribution Names

The Python import package name is:

```text
agentic_debugger
```

The Python distribution/project name is:

```text
agentic-debugger
```

All internal imports must use `agentic_debugger`. The hyphenated distribution name is used only in package metadata and installation commands.

### 4.2 Supported Runtime

The initial supported runtime is:

```text
Python >= 3.11
```

Python 3.11 is the minimum because the MVP benefits from modern typing, dataclasses, enums, `tomllib`, exception groups, and current debugger behavior without requiring the newest interpreter available on every machine.

The implementation must remain pure Python unless a later task explicitly approves a native dependency.

### 4.3 Dependency Policy

Implementation Task 1 must use the Python standard library for runtime code.

The only initially approved development dependency is:

```text
pytest
```

No runtime framework, agent framework, model SDK, schema library, subprocess wrapper, sandbox framework, logging framework, or CLI framework may be added in Task 1.

Any later dependency addition requires:

- a concrete need;
- a built-in/existing-alternative check;
- security and maintenance review;
- explicit scope inclusion in the relevant task.

### 4.4 Initial Execution Model

The initial architecture uses one controller with typed actions and typed observations.

The controller may select tools only from the allowlist for its current state. Tools execute deterministically and return bounded structured observations. The model does not receive unrestricted access to the host shell, filesystem, debugger prompt, process table, environment secrets, or network.

### 4.5 Initial Dataset Strategy

The first dataset is five locally curated, pytest-compatible Python bugs.

Each task must:

- reproduce with one deterministic command;
- have exactly one designated fail-to-pass test at task creation time;
- have at least two designated pass-to-pass regression tests;
- require no network, browser, GUI, database server, container registry, or external service;
- complete within a small local timeout;
- contain an observable runtime-state clue;
- fit in a small source tree that can be inspected manually.

A small BugsInPy subset is a later extension, not part of the first MVP implementation sequence.

### 4.6 Baseline Variants

The evaluation runner must eventually support four policies over the same task interface:

- **Static baseline:** source/search, patch, and tests; PDB disabled.
- **PDB always-on:** runtime evidence collected immediately after reproduction.
- **PDB after failed patch:** one static attempt is allowed before PDB.
- **PDB on uncertainty:** PDB is allowed only when confidence is low or the hypothesis depends on runtime state.

The static baseline is mandatory. A PDB-enabled result has no research value without a comparable non-debugger run.

## 5. Scope

### 5.1 In Scope for the First MVP

- Python-only tasks.
- Pytest-compatible reproduction and regression tests.
- Small isolated workspaces.
- Structured source retrieval by path, symbol, and text query.
- Failure trace extraction.
- PDB stack, frame, locals, source-window, and restricted-expression observations.
- Root-cause hypothesis records with evidence references.
- Deterministic unified-diff patch application and revert.
- Syntax checks and test execution with timeouts.
- Action, observation, decision, and final events.
- Event-log replay sufficient for debugging and golden-trajectory tests.
- Static and PDB policy comparison.
- SWE-bench-inspired behavioral outcome classification.
- Mocked/fixed model outputs in automated tests.
- Manual or opt-in real-model experiments later.

### 5.2 Out of Scope for the First MVP

- Full SWE-bench, SWE-bench Lite, or SWE-bench Verified execution.
- Broad BugsInPy ingestion.
- GDB, LLDB, Java, C, C++, JavaScript, or multi-language adapters.
- Multi-agent debugging or repair.
- Fine-tuning, LoRA, QLoRA, DPO, RLHF, or preference optimization.
- Production RAG infrastructure or vector databases.
- Arbitrary shell access.
- Arbitrary Python `eval`.
- Model-generated raw PDB command streams.
- Network access during task execution.
- Browser or GUI automation.
- Docker orchestration in the first implementation tasks.
- Paid API calls in automated tests or CI.
- Autonomous Git commits, pushes, merges, releases, or PR creation.
- Production-grade security isolation claims.

### 5.3 Explicitly Deferred Work

The following decisions remain deferred until the deterministic MVP works:

- exact real-model provider and adapter;
- local model versus subscription-backed model comparison;
- container sandbox selection;
- larger dataset import format;
- BugsInPy task selection;
- full repository indexing or embeddings;
- multi-step debugger commands such as arbitrary breakpoints, stepping, and continue;
- training-data generation from trajectories;
- fine-tuning and preference optimization;
- Tier 3/supporting-paper reading.

## 6. Target Repository Structure

The target structure is directional. Each implementation task may create only the files required for that bounded slice.

```text
pyproject.toml

agentic_debugger/
  __init__.py

  agent/
    __init__.py
    controller.py
    prompts.py
    state_machine.py
    policy.py

  runtime/
    __init__.py
    workspace.py
    command_runner.py
    pdb_session.py
    patcher.py
    test_runner.py

  skills/
    __init__.py
    file_skills.py
    search_skills.py
    pdb_skills.py
    patch_skills.py
    test_skills.py

  events/
    __init__.py
    schema.py
    logger.py
    replay.py

  evaluation/
    __init__.py
    task_schema.py
    runner.py
    metrics.py
    outcome_taxonomy.py

  datasets/
    __init__.py
    curated/
      curated-none-handling-001/
      curated-off-by-one-002/
      curated-wrong-branch-003/
      curated-mutation-alias-004/
      curated-caller-callee-005/
    bugsinpy_subset/

tests/
  unit/
  integration/
  golden_trajectories/
```

Empty placeholder modules and directories should not be created merely to match the final tree. A file is added when its owning task defines behavior or a stable contract for it.

## 7. Initial Task Schema

### 7.1 Schema Format

Curated task manifests use JSON. JSON is selected because it is deterministic, language-neutral, serializable with the standard library, and straightforward to validate in tests.

The initial schema version is:

```text
1.0
```

The canonical in-memory type is `DebugTask` under:

```text
agentic_debugger.evaluation.task_schema
```

### 7.2 Required Top-Level Fields

Each task record must contain:

| Field | Type | Purpose |
|---|---|---|
| `schema_version` | string | Must equal `1.0` in MVP v1. |
| `task_id` | string | Stable lowercase identifier matching `^[a-z0-9][a-z0-9-]{2,63}$`. |
| `title` | string | Human-readable task title. |
| `description` | string | Agent-visible issue/failure description. |
| `language` | string | Must equal `python` in MVP v1. |
| `fixture_path` | string | Repository-relative path to the task fixture directory. |
| `reproduction` | object | Command and timeout used to reproduce the failure. |
| `tests` | object | Fail-to-pass and pass-to-pass test contract. |
| `constraints` | object | Execution and path restrictions. |
| `oracle` | object | Evaluator-only localization and root-cause metadata. |
| `tags` | array[string] | Task grouping and analysis labels. |

### 7.3 Reproduction Object

Required fields:

| Field | Type | Rules |
|---|---|---|
| `argv` | array[string] | Non-empty argument vector; no shell string. |
| `cwd` | string | Relative to the isolated task workspace. |
| `timeout_seconds` | integer | Range `1..60`; initial curated tasks should use `10`. |
| `expected_exit_code` | integer | Initial failing reproduction normally uses `1`. |

Commands are represented as argument vectors to avoid shell interpolation and platform-dependent quoting.

### 7.4 Tests Object

Required fields:

| Field | Type | Rules |
|---|---|---|
| `fail_to_pass` | array[string] | Exactly one pytest node id for the first curated set. |
| `pass_to_pass` | array[string] | At least two pytest node ids. |
| `full_suite_argv` | array[string] | Deterministic full fixture test command. |
| `timeout_seconds` | integer | Range `1..60`; initial value `20`. |

The verifier must distinguish:

- the originally failing test becoming green;
- previously passing regression tests staying green;
- the full task suite outcome.

### 7.5 Constraints Object

Required fields:

| Field | Type | Rules |
|---|---|---|
| `allowed_write_paths` | array[string] | Non-empty repository-relative paths or file paths. |
| `denied_write_paths` | array[string] | Must include tests and task manifest by default. |
| `network_allowed` | boolean | Must be `false` for curated v1 tasks. |
| `external_services_allowed` | boolean | Must be `false`. |
| `max_patch_attempts` | integer | Initial range `1..3`; default `2`. |
| `max_test_runs` | integer | Initial range `1..10`; default `5`. |
| `max_pdb_observations` | integer | Initial range `0..20`; default `8`. |

### 7.6 Oracle Object

The `oracle` object is evaluator-only and must not be exposed to the repair agent during a normal run.

Required fields:

| Field | Type | Purpose |
|---|---|---|
| `bug_category` | string | Controlled category for analysis. |
| `target_files` | array[string] | Files containing the intended defect. |
| `target_symbols` | array[string] | Functions or methods containing the intended defect. |
| `root_cause_summary` | string | Concise reference diagnosis. |
| `runtime_evidence_hint` | string | Why runtime state may help; evaluator-only. |

### 7.7 Validation Rules

Task loading must reject:

- unknown schema versions;
- missing required fields;
- empty strings where identifiers or commands are required;
- absolute paths;
- paths containing `..` traversal;
- empty command vectors;
- shell command strings in place of `argv`;
- duplicate test node ids;
- the same node id appearing in both fail-to-pass and pass-to-pass lists;
- non-positive or out-of-range timeouts and budgets;
- `network_allowed: true` in curated v1;
- an empty `allowed_write_paths` list;
- a task whose `fixture_path` is outside the curated dataset root.

Unknown additional fields must be rejected in schema v1 so format drift is explicit rather than silent.

### 7.8 Example Task Record

```json
{
  "schema_version": "1.0",
  "task_id": "curated-none-handling-001",
  "title": "Normalize missing display names",
  "description": "A formatting helper crashes when an optional display name is missing.",
  "language": "python",
  "fixture_path": "agentic_debugger/datasets/curated/curated-none-handling-001",
  "reproduction": {
    "argv": ["python", "-m", "pytest", "tests/test_profile.py::test_missing_display_name", "-q"],
    "cwd": ".",
    "timeout_seconds": 10,
    "expected_exit_code": 1
  },
  "tests": {
    "fail_to_pass": ["tests/test_profile.py::test_missing_display_name"],
    "pass_to_pass": [
      "tests/test_profile.py::test_regular_display_name",
      "tests/test_profile.py::test_whitespace_is_normalized"
    ],
    "full_suite_argv": ["python", "-m", "pytest", "-q"],
    "timeout_seconds": 20
  },
  "constraints": {
    "allowed_write_paths": ["profile.py"],
    "denied_write_paths": ["tests", "task.json"],
    "network_allowed": false,
    "external_services_allowed": false,
    "max_patch_attempts": 2,
    "max_test_runs": 5,
    "max_pdb_observations": 8
  },
  "oracle": {
    "bug_category": "none-handling",
    "target_files": ["profile.py"],
    "target_symbols": ["format_display_name"],
    "root_cause_summary": "The helper calls a string method before normalizing an optional None value.",
    "runtime_evidence_hint": "The failing frame shows that the local name value is None while the normal path contains a string."
  },
  "tags": ["curated", "runtime-state", "none-handling"]
}
```

## 8. First Five Curated Benchmark Bugs

The following are behavioral specifications only. Their fixture code is created in a later implementation task.

### 8.1 `curated-none-handling-001`

**Category:** None handling.

**Scenario:** A display-name formatter receives an optional string from a caller. The implementation invokes a string operation before converting a missing value to the intended fallback.

**Design requirements:**

- one public helper function;
- one fail-to-pass test for `None`;
- pass-to-pass tests for a normal name and whitespace normalization;
- defect localized to one function in one source file;
- expected patch must preserve valid-string behavior.

**Runtime-state value:** The failing frame exposes `name is None`, while the code path assumes `str`.

**Expected localization:** Correct helper function.

**Expected root cause:** Optional input normalization occurs after an operation that requires a string.

### 8.2 `curated-off-by-one-002`

**Category:** Boundary/off-by-one.

**Scenario:** A function extracts a fixed-size recent window from a sequence. The failure occurs only when the requested window size exactly equals the sequence length or when the boundary index is reached.

**Design requirements:**

- deterministic list input;
- one fail-to-pass boundary test;
- pass-to-pass tests for a smaller window and an empty/zero-size policy;
- minimal defect in loop bound or slice boundary;
- no randomness or time dependence.

**Runtime-state value:** Frame locals expose sequence length, requested size, and computed start/end index at the failing boundary.

**Expected localization:** Window calculation function.

**Expected root cause:** Inclusive/exclusive boundary is calculated incorrectly.

### 8.3 `curated-wrong-branch-003`

**Category:** Wrong conditional branch.

**Scenario:** A pricing or eligibility function selects the wrong branch when two booleans interact. Static reading is plausible in both directions, but the failing case reveals the actual combination of runtime flags.

**Design requirements:**

- two boolean or enum-like inputs;
- one fail-to-pass test for the ambiguous combination;
- at least two pass-to-pass tests covering the other branches;
- defect must be a wrong condition or branch ordering, not a missing feature.

**Runtime-state value:** Locals show the exact flag combination and the incorrectly selected branch.

**Expected localization:** Branching function.

**Expected root cause:** Condition ordering or boolean operator selects a broader branch before the specific case.

### 8.4 `curated-mutation-alias-004`

**Category:** Incorrect mutation/aliasing.

**Scenario:** A helper is expected to return an updated collection without mutating the caller's original collection. The implementation aliases and modifies the input.

**Design requirements:**

- one fail-to-pass test asserting the original value remains unchanged;
- pass-to-pass tests for returned content and repeated calls;
- defect contained in one helper;
- patch must not modify tests or hide mutation through copying at assertion time.

**Runtime-state value:** Caller and callee frames reveal that input and working collection share identity and that the original has changed.

**Expected localization:** Collection-transform helper.

**Expected root cause:** The working variable references the caller-owned mutable object instead of an independent copy.

### 8.5 `curated-caller-callee-005`

**Category:** Caller/callee contract mismatch.

**Scenario:** A caller provides a value in one unit or representation while the callee interprets it as another. Each function appears locally reasonable; the bug is visible only across the frame boundary.

**Design requirements:**

- one caller and one callee in separate functions;
- one fail-to-pass test for the mismatched representation;
- pass-to-pass tests for zero and a standard non-boundary value;
- intended fix must be localized to the contract boundary;
- patch must avoid compensating in multiple places.

**Runtime-state value:** Caller-frame locals and callee arguments expose the representation mismatch.

**Expected localization:** Caller/callee boundary, with one intended target symbol named in the oracle.

**Expected root cause:** The caller passes a value using a different unit or normalization contract than the callee expects.

## 9. Controller State Machine v1

### 9.1 State Set

The locked controller states are:

```text
Reproduce
Understand
RuntimeEvidence
Patch
Validate
Done
Failed
```

`Failed` is an explicit terminal state for non-reproduction, exhausted budgets, unsafe requests, and unrecoverable tool errors.

### 9.2 Allowed State Transitions

```text
Reproduce -> Understand | Failed
Understand -> Understand | RuntimeEvidence | Patch | Failed
RuntimeEvidence -> Understand | RuntimeEvidence | Patch | Failed
Patch -> Patch | Understand | Validate | Failed
Validate -> Done | Understand | RuntimeEvidence | Patch | Failed
Done -> terminal
Failed -> terminal
```

The controller must reject transitions not present in this graph.

### 9.3 Reproduce

Goal: prove the failure is real and capture the initial evidence.

Allowed action names:

```text
run_tests
run_reproduction
get_failure_trace
```

Required exit evidence:

- command invoked;
- exit code;
- timeout status;
- bounded stdout/stderr;
- parsed traceback when available.

### 9.4 Understand

Goal: identify the smallest suspicious source area and create or update a root-cause hypothesis.

Allowed action names:

```text
search_code
find_function
find_class
get_source_window
extract_failing_test
express_root_cause_hypothesis
request_more_evidence
```

The controller may transition directly to Patch when static evidence is sufficient and policy permits it.

### 9.5 RuntimeEvidence

Goal: collect only the runtime evidence required to support or reject the active hypothesis.

Allowed action names:

```text
start_pdb_session
get_stack_summary
get_frame
get_frame_locals
get_source_window
safe_eval_expression
inspect_caller_frame
discard_hypothesis
request_more_evidence
stop_pdb_session
```

Entry requires an explicit policy decision and remaining PDB budget.

### 9.6 Patch

Goal: create and apply one minimal deterministic patch attempt.

Allowed action names:

```text
apply_patch
syntax_check
revert_patch
```

A patch may not edit paths outside the task allowlist. Tests and task manifests are denied by default.

### 9.7 Validate

Goal: determine whether the patch resolves the original failure without breaking designated regression behavior.

Allowed action names:

```text
run_reproduction
run_regression_tests
classify_outcome
revert_patch
```

### 9.8 Done

Required final output:

- task and run identifiers;
- final state;
- localization result;
- root-cause statement;
- evidence references;
- whether and why PDB was used;
- patch summary;
- fail-to-pass result;
- pass-to-pass result;
- full-suite result;
- outcome classification;
- budgets consumed;
- remaining limitations.

### 9.9 Budgets and Retry Limits

The task manifest supplies hard maxima. Initial defaults are:

```text
max patch attempts: 2
max test runs: 5
max PDB observations: 8
max active hypotheses: 3
max source/search observations: 12
```

Exhausting a budget must produce a recorded decision and transition to either a safe fallback path or `Failed`. The controller must not silently exceed a budget.

## 10. Action, Observation, and Event Model

### 10.1 Typed Actions

Every action must contain:

| Field | Type |
|---|---|
| `action_id` | string |
| `run_id` | string |
| `task_id` | string |
| `state` | controller-state enum |
| `name` | action-name enum/string allowlist |
| `arguments` | JSON-compatible mapping |

Action arguments must be validated before tool execution.

### 10.2 Typed Observations

Every observation must contain:

| Field | Type |
|---|---|
| `observation_id` | string |
| `action_id` | string |
| `run_id` | string |
| `task_id` | string |
| `name` | string |
| `status` | `ok`, `error`, `rejected`, or `timeout` |
| `payload` | JSON-compatible mapping |
| `summary` | bounded human-readable string |
| `truncated` | boolean |

Tool-specific observation payloads may add structured fields, but all payload values must remain JSON serializable.

### 10.3 Event Record

The canonical event type is `RunEvent` under:

```text
agentic_debugger.events.schema
```

Required event fields:

| Field | Type | Rule |
|---|---|---|
| `schema_version` | string | Must equal `1.0`. |
| `event_id` | string | Unique within a run. |
| `run_id` | string | Stable for the trajectory. |
| `task_id` | string | Matches the loaded task. |
| `sequence` | integer | Starts at `0`, increments by exactly `1`. |
| `timestamp` | ISO-8601 string | UTC with timezone indicator. |
| `event_type` | enum | `message`, `action`, `observation`, `decision`, `transition`, `final`. |
| `name` | string | Event-specific name. |
| `state` | string or null | Controller state at event creation. |
| `payload` | mapping | JSON-compatible event data. |
| `metadata` | mapping | Timing/model/tool metadata. |

Metadata v1 fields:

```text
duration_ms: integer or null
tool_version: string or null
model: string or null
tokens: integer or null
cost: number or null
```

### 10.4 Event Logger

The initial event logger must:

- accept validated `RunEvent` objects;
- enforce a single `run_id` and `task_id` per log instance;
- assign or validate monotonic sequence numbers;
- write one compact JSON object per line in UTF-8 JSONL format;
- flush deterministically when requested;
- reject non-JSON-compatible payloads;
- avoid logging secrets or environment dumps;
- be testable with an in-memory stream or temporary file;
- not depend on the Python `logging` package configuration of the host application.

### 10.5 Replay Boundary

Full semantic replay is deferred. Task 1 only needs schema and logger contracts that preserve enough information for a later replay module.

A future replay reader must be able to:

- load events in sequence;
- detect missing, duplicate, or out-of-order sequence numbers;
- reconstruct controller-state transitions;
- compare an actual trajectory with a golden expected trajectory.

## 11. PDB Skills v1

PDB skills are future implementation contracts. They are not implemented in Task 1.

### 11.1 Session Lifecycle

```text
start_pdb_session(command_argv, cwd, timeout_seconds)
stop_pdb_session(session_id)
```

Rules:

- no shell command string;
- isolated task workspace only;
- one active PDB session per run in v1;
- hard timeout;
- explicit stop and cleanup;
- session identifier required for every subsequent PDB action.

### 11.2 Stack Inspection

```text
get_stack_summary(session_id)
```

Returns bounded frames containing:

```text
frame_index
file_path
line_number
function_name
source_line
is_project_frame
```

External/library frames may be summarized or filtered, but the raw debugger prompt is never returned to the model.

### 11.3 Frame and Locals Inspection

```text
get_frame(session_id, frame_index)
get_frame_locals(session_id, frame_index, max_items=50, max_repr_chars=200)
```

Rules:

- validate frame index;
- sort local names for deterministic output;
- truncate object representations;
- mark truncation explicitly;
- avoid recursively traversing arbitrary object graphs;
- redact values matching configured secret-key names.

### 11.4 Source Inspection

```text
get_source_window(path, line, radius=50)
```

Rules:

- task workspace paths only;
- normalized repository-relative output path;
- bounded radius;
- include line numbers;
- mark the focal line;
- reject path traversal and denied files.

### 11.5 Safe Expression Evaluation

```text
safe_eval_expression(session_id, frame_index, expression)
```

V1 allowlist intent:

- names;
- constants;
- attribute reads subject to deny rules;
- indexing and slicing;
- comparisons;
- boolean operators;
- arithmetic operators;
- container literals;
- `is` and `is not` checks.

V1 must reject by default:

- function and method calls;
- assignment expressions;
- imports;
- comprehensions;
- lambdas;
- `await`, `yield`, or generator behavior;
- dunder attribute access;
- mutation operations;
- shell/process/network/file operations;
- expressions above configured AST node/depth limits.

The exact AST validator is defined in the PDB implementation task and requires dedicated rejection tests.

### 11.6 Caller-Frame Inspection

```text
inspect_caller_frame(session_id, frame_index)
```

This is a bounded convenience action that returns the immediate caller's frame summary, selected locals, and source window. It must not automatically walk an unbounded stack.

### 11.7 Output Bounds

Initial defaults:

```text
maximum stack frames returned: 20
maximum locals per frame: 50
maximum repr characters per value: 200
maximum source-window radius: 50 lines
maximum observation summary: 4,000 characters
maximum raw stdout/stderr retained per command: 20,000 characters per stream
```

All truncation must be explicit in the observation.

## 12. Static, Patch, and Test Skills v1

### 12.1 Structural Source Retrieval

Initial source tools:

```text
open_file(path, line=None)
get_source_window(path, line, radius=50)
search_code(query, path=None)
find_function(name)
find_class(name)
get_function_source(symbol)
extract_failing_test(test_id_or_path)
```

V1 structural retrieval may use Python's standard-library `ast` module. It does not require embeddings, a vector database, or language-server integration.

### 12.2 Command and Test Execution

Initial test tools:

```text
run_tests(argv, cwd, timeout_seconds)
run_reproduction(argv, cwd, timeout_seconds)
get_failure_trace()
run_regression_tests(argv, cwd, timeout_seconds)
```

The command runner must avoid `shell=True` and return:

```text
argv
cwd
exit_code
timed_out
duration_ms
stdout
stderr
truncation flags
```

### 12.3 Patch Application and Revert

Initial patch tools:

```text
apply_patch(unified_diff)
revert_patch()
syntax_check()
```

Rules:

- unified diff only;
- deterministic path validation;
- no absolute paths or traversal;
- no edits outside `allowed_write_paths`;
- no edits inside `denied_write_paths`;
- record pre-patch hashes;
- reject ambiguous or partially applied patches;
- one active patch snapshot at a time in v1;
- restore exact pre-patch bytes on revert;
- never use Git reset/checkout as the patch-revert mechanism inside a task workspace.

### 12.4 Outcome Classification

Initial behavioral outcomes:

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
Infrastructure Failure
```

Minimum classification rules:

- **Resolved:** designated fail-to-pass test passes and all designated pass-to-pass tests pass.
- **Breaking Resolved:** fail-to-pass passes, but one or more pass-to-pass tests fail.
- **Partially Resolved:** the original failure changes meaningfully but the fail-to-pass test remains failing.
- **No-Op:** no effective source change or behavior remains materially unchanged.
- **Regression:** new designated or full-suite failures are introduced without resolving the target.
- **Patch Apply Failure:** proposed patch cannot be applied deterministically.
- **Syntax Failure:** patched source fails syntax/compile validation.
- **Timeout:** required command exceeds its task timeout.
- **Infrastructure Failure:** the task cannot be evaluated because the workspace or runner failed independently of the patch.

## 13. Safety and Sandbox Contract

### 13.1 Prohibited Operations

V1 must not expose or perform:

- raw interactive PDB terminal access for the model;
- raw host shell access;
- `shell=True` execution;
- arbitrary Python `eval` or `exec`;
- unrestricted imports from model-supplied text;
- network access;
- reads or writes outside the isolated task workspace;
- writes to tests or task manifests unless a future task explicitly changes the policy;
- process killing outside a process created by the current runner;
- desktop or operating-system control;
- access to secrets, credentials, SSH keys, browser profiles, or environment dumps;
- Git commit, push, merge, rebase, force-push, tag, release, or PR actions;
- paid model/API calls in default automated tests.

### 13.2 Workspace Boundary

Every repair run must operate on a disposable task workspace copied from an immutable fixture source.

The fixture source is never patched directly. Reverting or starting another variant must produce a clean workspace from the same task baseline.

The first workspace implementation may use local directory copying. Containerization is deferred until the local deterministic path is proven.

### 13.3 Timeout and Resource Policy

Every subprocess and PDB session requires a timeout.

Timeouts are task-configured within schema limits. A timeout produces a structured observation and classification signal; it must not leave a hidden long-running child process under normal operation.

### 13.4 Secret and Cost Policy

Runtime logs must not include:

- full environment-variable dumps;
- credentials;
- API keys;
- tokens;
- private keys;
- unrelated user paths or file contents.

No paid or token-metered service is required for the deterministic MVP, unit tests, integration tests, or golden trajectories.

## 14. Evaluation Design

### 14.1 Experimental Policies

Each curated task should eventually run under the same task schema and budgets with:

```text
A. static baseline
B. PDB always-on
C. PDB after failed patch
D. PDB on uncertainty
```

Model outputs must be controlled when comparing policies. Early automated comparisons should use fixed/mock model action sequences so controller and tool behavior are isolated from model nondeterminism.

### 14.2 Behavioral Metrics

Record:

- final outcome classification;
- fail-to-pass result;
- pass-to-pass result;
- full-suite result;
- patch applied successfully;
- syntax check result;
- number of patch attempts;
- number of test runs;
- timeout or infrastructure failures.

### 14.3 Localization Metrics

Record:

```text
correct target symbol
wrong patch but correct target symbol
wrong location in correct file
wrong file
no localization
no patch
```

Localization scoring uses evaluator-only oracle fields.

### 14.4 Runtime-Evidence Metrics

Record:

```text
PDB not used
PDB unnecessary / static evidence sufficient
PDB useful / changed diagnosis
PDB confirmed existing diagnosis
PDB refuted hypothesis
PDB misread / wrong conclusion
PDB unsafe request rejected
correct runtime diagnosis but wrong patch
```

A `PDB useful` claim must cite event identifiers showing that runtime evidence changed or materially strengthened the diagnosis.

### 14.5 Cost and Runtime Metrics

Record when available:

- total tool calls;
- source/search observations;
- PDB observations;
- hypotheses created and discarded;
- patch attempts;
- test runs;
- wall-clock duration;
- model tokens and cost for opt-in real-model runs.

Token and cost fields remain nullable for deterministic or local runs.

### 14.6 Explanation Quality

Manual review rubric:

- localization cites relevant source or traceback evidence;
- root cause explains the causal chain, not only the failing line;
- runtime-state claims reference actual observations;
- patch explanation matches the changed behavior;
- validation claims match test results;
- uncertainty and limitations are stated honestly.

## 15. Test and Validation Commands

No lint, formatter, type-checker, or coverage command is mandatory until the project explicitly configures that tool. Commands must not be invented merely to look comprehensive.

### 15.1 Implementation Task 1 Targeted Tests

```powershell
python -m pytest tests/unit/test_task_schema.py tests/unit/test_event_schema.py tests/unit/test_event_logger.py tests/unit/test_state_machine_contract.py -q
```

If the final Task 1 test filenames differ for a justified reason, the branch report must list the exact equivalent targeted command.

### 15.2 Implementation Task 1 Full Local Gate

```powershell
python -m compileall -q agentic_debugger tests
python -m pytest -q
git diff --check
```

### 15.3 Later Runtime and Integration Gates

Planned commands after those suites exist:

```powershell
python -m pytest tests/unit -q
python -m pytest tests/integration -q
python -m pytest tests/golden_trajectories -q
python -m pytest -q
```

Default automated tests must not call a real model or paid API.

## 16. Implementation Task Breakdown

Each task uses an isolated branch created from the latest accepted `main`. Tasks are sequential unless a later plan explicitly proves that parallel work is safe.

### Task 1 â€” Foundation Contracts and Event Skeleton

Proposed branch:

```text
feature/mvp-foundation-contracts-v1
```

Deliver package metadata, task schema, event/action/observation contracts, event logger, controller-state contract, and unit tests. No subprocess, PDB, patch, controller loop, dataset fixtures, or real model.

### Task 2 â€” Workspace and Command/Test Runtime

Proposed branch:

```text
feature/mvp-runtime-basics-v1
```

Deliver disposable workspace creation, safe argv-based command runner, bounded outputs, timeout behavior, test-result records, and targeted unit/integration tests.

### Task 3 â€” Source Retrieval and Patch Lifecycle

Proposed branch:

```text
feature/mvp-source-patch-lifecycle-v1
```

Deliver path-safe source windows, text/AST symbol retrieval, deterministic unified-diff validation/application, byte-exact revert, syntax checking, and tests.

### Task 4 â€” PDB Session and Runtime Skills

Proposed branch:

```text
feature/mvp-pdb-runtime-skills-v1
```

Deliver bounded PDB session lifecycle, stack/frame/locals/source observations, safe-expression AST validation, caller-frame inspection, timeout cleanup, and rejection tests.

### Task 5 â€” Controller State Machine and Tool Policy

Proposed branch:

```text
feature/mvp-controller-v1
```

Deliver controller transitions, action allowlists, budgets, hypothesis lifecycle, PDB gating policies, deterministic tool registry, and mocked model adapter.

### Task 6 â€” Curated Benchmark Fixtures

Proposed branch:

```text
feature/mvp-curated-bugs-v1
```

Deliver the five specified fixtures, task manifests, immutable baseline validation, designated fail-to-pass/pass-to-pass tests, and fixture integrity tests.

### Task 7 â€” Verifier, Outcome Taxonomy, and Evaluation Runner

Proposed branch:

```text
feature/mvp-evaluation-runner-v1
```

Deliver policy variants, verifier, outcome classifier, localization/runtime metrics, run summaries, and machine-readable results.

### Task 8 â€” Golden Trajectories

Proposed branch:

```text
feature/mvp-golden-trajectories-v1
```

Deliver fixed model action sequences, stable event expectations, replay validation, patch/test assertions, and no-real-model CI coverage.

### Task 9 â€” First End-to-End Demonstration

Proposed branch:

```text
feature/mvp-end-to-end-demo-v1
```

Deliver a documented local demo over the curated set, static versus PDB policy results, known limitations, and the first technical evaluation summary. Real-model execution remains opt-in and separately approved.

## 17. Implementation Task 1 Specification

### 17.1 Task Name

```text
MVP Foundation Contracts and Event Skeleton v1
```

### 17.2 Goal

Create the smallest installable/testable Python project foundation that locks the MVP's data contracts before runtime behavior is added.

The result must allow later tasks to import, validate, serialize, log, and test:

- task manifests;
- controller states and transition legality;
- typed actions;
- typed observations;
- run events;
- JSONL event logging.

### 17.3 Risk Classification

Expected risk: R1, new non-runtime foundation and unit tests.

The task becomes higher risk and must stop for review if implementation requires:

- a runtime dependency beyond the standard library;
- a public CLI or external API contract;
- subprocess execution;
- persistence beyond append-only event JSONL;
- security-sensitive evaluation code;
- a schema change from this accepted plan;
- source changes outside the approved package foundation.

### 17.4 Expected Baseline and Branch

The task starts only after this implementation plan is accepted and merged to `main`.

Proposed branch:

```text
feature/mvp-foundation-contracts-v1
```

The exact starting commit must be verified from live Git state before work begins.

### 17.5 Allowed Scope

Expected files/categories:

```text
pyproject.toml
agentic_debugger/__init__.py
agentic_debugger/agent/__init__.py
agentic_debugger/agent/state_machine.py
agentic_debugger/events/__init__.py
agentic_debugger/events/schema.py
agentic_debugger/events/logger.py
agentic_debugger/evaluation/__init__.py
agentic_debugger/evaluation/task_schema.py
tests/unit/* directly covering these contracts
```

The coding agent may adjust exact unit-test filenames or add a small shared test helper when justified by the live implementation. It must not create unrelated modules or empty placeholders for later phases.

### 17.6 Required Deliverables

1. `pyproject.toml` with:
   - distribution name `agentic-debugger`;
   - Python requirement `>=3.11`;
   - standard package discovery for `agentic_debugger`;
   - pytest as the only approved initial development/test dependency;
   - no runtime dependencies.

2. Package version constant:

```text
__version__ = "0.1.0"
```

3. `DebugTask` and nested typed records for:
   - reproduction;
   - tests;
   - constraints;
   - oracle.

4. Task loading and validation from a JSON-compatible mapping and JSON file.

5. `ControllerState` enum and a pure transition-validity contract for the state graph in this plan.

6. Typed records for:
   - `Action`;
   - `Observation`;
   - `RunEvent`;
   - event type and observation status enums.

7. Deterministic conversion to and from JSON-compatible mappings where applicable.

8. `JsonlEventLogger` supporting:
   - validated event append;
   - monotonic sequence enforcement;
   - stable UTF-8 JSONL output;
   - explicit flush/close behavior;
   - file path or text-stream use suitable for tests.

9. Focused unit tests for happy paths and rejection paths.

### 17.7 Mandatory Validation Behavior

The implementation must prove at minimum:

#### Task schema

- valid example mapping loads successfully;
- valid task round-trips to a JSON-compatible mapping;
- unknown schema version is rejected;
- missing required fields are rejected;
- unknown fields are rejected;
- invalid task id is rejected;
- absolute and traversal paths are rejected;
- empty argv is rejected;
- shell-string command shape is rejected;
- duplicate or overlapping F2P/P2P node ids are rejected;
- timeout and budget bounds are enforced;
- curated-v1 network access is rejected;
- oracle remains present in the evaluator record but has an explicit agent-visible projection that excludes it.

#### State contract

- every allowed transition in Section 9 passes;
- representative forbidden transitions fail;
- terminal states do not transition;
- transition validation is pure and has no controller side effects.

#### Event/action/observation schema

- valid records serialize to JSON-compatible mappings;
- invalid enum values are rejected;
- sequence values cannot be negative;
- payloads containing non-JSON-compatible values are rejected before logging;
- observation status and truncation are explicit;
- event timestamps are timezone-aware UTC strings or normalized to that representation.

#### Event logger

- multiple events write one JSON object per line;
- sequence numbers are monotonic and contiguous;
- mixed run ids or task ids are rejected;
- duplicate/out-of-order sequence numbers are rejected;
- output is parseable as UTF-8 JSONL;
- flush and close behavior is deterministic;
- logger tests use temporary paths or in-memory streams and leave no repository artifacts.

### 17.8 Acceptance Criteria

Task 1 is accepted only when all of the following are true:

- the import package is exactly `agentic_debugger`;
- the distribution name is exactly `agentic-debugger`;
- Python requirement is `>=3.11`;
- runtime dependency list is empty;
- pytest is the only new approved development dependency;
- schemas and logger use standard-library implementation;
- all contracts in Sections 7, 9, and 10 required by Task 1 are represented;
- task schema rejects unsafe/ambiguous inputs listed above;
- an explicit agent-visible task projection excludes evaluator-only oracle fields;
- controller transition legality is test-covered but no controller loop exists;
- JSONL event output is deterministic and test-covered;
- targeted unit tests pass;
- full pytest suite passes;
- compileall passes;
- `git diff --check` passes;
- the complete diff contains no subprocess runner, PDB integration, patch application, source retrieval, benchmark fixtures, model adapter, network code, CLI, or unrelated documentation rewrite;
- no generated logs, caches, virtual environments, or review artifacts are staged;
- the coding agent does not commit, push, merge, or modify protected/default branches unless Onur separately authorizes an exact exception.

### 17.9 Explicitly Forbidden in Task 1

- `subprocess`-based command execution;
- PDB imports or debugger sessions;
- patch parsing/application/revert;
- workspace copying;
- source search or AST symbol retrieval beyond what is strictly necessary to validate task records;
- controller execution loop;
- prompt templates;
- real or mocked LLM adapter behavior;
- curated bug fixture source code;
- BugsInPy integration;
- Docker or sandbox framework;
- network access;
- secrets/configuration system;
- CLI commands;
- README rewrite;
- paper-note or synthesis edits;
- dependency additions beyond pytest;
- broad repository restructuring.

### 17.10 Task 1 Validation Commands

Targeted:

```powershell
python -m pytest tests/unit/test_task_schema.py tests/unit/test_event_schema.py tests/unit/test_event_logger.py tests/unit/test_state_machine_contract.py -q
```

Full gate:

```powershell
python -m compileall -q agentic_debugger tests
python -m pytest -q
git diff --check
git status --short
```

The coding agent must report the exact commands actually run, their outcomes, changed files, and any deviation from the expected filenames.

### 17.11 Task 1 Evidence and Review

Minimum review evidence:

- starting branch and commit;
- final branch and uncommitted working-tree state;
- exact changed-file list;
- diff stat;
- targeted test result;
- full test result;
- compileall result;
- `git diff --check` result;
- confirmation that no forbidden category was added;
- concise design notes for validation, serialization, and sequence enforcement.

No screenshot, GUI smoke, expensive sandbox run, or real-model run is required.

## 18. Branch and Git Strategy

### 18.1 Branch-per-Task Rule

Every implementation slice uses a dedicated branch from the latest accepted `main`.

Naming pattern:

```text
feature/mvp-<bounded-task>-v1
```

Docs-only planning and hygiene tasks use:

```text
docs/<bounded-task>-v1
```

### 18.2 Authority Boundary

Onur remains:

- final authority;
- product owner;
- manual judge;
- commit/merge/push owner in normal workflow;
- protected/default-branch owner.

Coding agents may inspect, edit, test, review, repair, and report within the approved branch and scope without routine permission prompts. They must stop for real escalation boundaries.

### 18.3 Escalation Gates

Stop and ask Onur when:

- the live baseline or branch is wrong;
- the tracked tree contains unrelated changes;
- requirements conflict in a way that changes the schema or product direction;
- scope must expand materially;
- a new dependency is required;
- security, secrets, paid services, public contracts, packaging, or release behavior enters scope unexpectedly;
- destructive or irreversible action is required;
- a protected/default-branch action would be required;
- safe progress would require desktop, process, or operating-system control outside the task.

### 18.4 Merge Sequence

Normal sequence:

1. Onur creates/verifies the task branch.
2. Coding agent completes the bounded task without committing.
3. ChatGPT reviews the actual diff and validation evidence.
4. Onur runs any required local validation.
5. Onur stages exact files and commits.
6. Onur fast-forward merges into `main` when ancestry permits.
7. Onur pushes `main` and verifies local/remote hashes.
8. Onur deletes the accepted task branch when appropriate.

## 19. Documentation and Evidence Policy

- Live code and terminal output override this plan if they reveal a factual mismatch.
- A mismatch that changes scope or an accepted contract must be escalated, not silently rewritten.
- Docs should be updated after major accepted slices, but implementation tasks must not use documentation updates as permission for broad cleanup.
- `_ai-review/`, generated logs, test caches, virtual environments, benchmark run outputs, and ZIP files must remain untracked unless a later task explicitly changes the policy.
- Evidence must be proportional to risk.
- Automated green tests do not prove research claims; evaluation interpretation remains a separate review step.

## 20. Open Decisions After Task 1

These are intentionally not resolved by Task 1:

1. Exact process-isolation strategy for Task 2.
2. Exact cross-platform timeout and child-process cleanup implementation.
3. Unified-diff parser approach for Task 3.
4. Exact PDB launch mode: post-mortem, `pdb` command loop wrapper, or controlled trace hook.
5. Exact AST allowlist and attribute-deny policy for safe expression evaluation.
6. Confidence representation used by the PDB-on-uncertainty policy.
7. Mocked model adapter protocol.
8. Real model/provider selection.
9. BugsInPy subset selection.
10. Container sandbox timing.

Each decision is resolved in its owning implementation task with a bounded plan and tests.

## 21. Definition of MVP Completion

The first MVP is complete when:

- all five curated tasks have immutable manifests and reproducible baselines;
- the static baseline completes end-to-end;
- at least one controller-gated PDB policy completes end-to-end;
- the controller enforces state/action allowlists and budgets;
- PDB observations are typed, bounded, and safety-filtered;
- patches apply and revert deterministically;
- fail-to-pass and pass-to-pass verification works;
- every run produces a valid event trajectory;
- golden-trajectory tests use fixed model outputs and pass without paid APIs;
- evaluation output records behavioral, localization, runtime-evidence, tool-count, and timing metrics;
- a local demonstration compares static and PDB-enabled behavior on the same curated tasks;
- limitations and unsupported security claims are documented;
- Onur reviews and accepts the demo and evaluation summary.

Completion of this MVP does not imply completion of the full internship TODO. It establishes the deterministic research prototype required before larger datasets, real-model experiments, RAG, fine-tuning, or preference optimization can be evaluated responsibly.
