# Agentic Debugging Local Application V1 — Architecture and Implementation Plan

**Document type:** Active architecture and phased implementation plan  
**Status:** V1 COMPLETE; Tasks 1–8 accepted (2026-08-16)
**Repository:** `agentic-debugging-internship`  
**Repository state inspected:** `main` at `e5ebe2680238624d7020ac9270918b4601848a83`, clean working tree and empty stash before this document was added  
**Scope:** A professional local application surface over the existing Agentic Debugging system  

## 1. Executive recommendation

Proceed with a **modern Textual terminal application backed by a new UI-independent application/session layer**.

The repository already contains the core scientific execution capabilities required by the product:

- bounded controller/state-machine execution;
- typed tool dispatch;
- real structured PDB interaction;
- source and execution-location awareness;
- patch parsing and application in disposable workspaces;
- an independent verifier;
- canonical event trajectories and strict replay;
- richer crash-durable lifecycle logging in the R5/R6 experiment path.

The missing capability is not another debugger, controller, or verifier. It is a stable application boundary that can:

- start a supported execution source;
- observe execution incrementally;
- expose immutable presentation state;
- request cancellation and confirm cleanup;
- persist application-owned session history;
- replay recorded sessions through the same presentation reducer used for live execution.

The proposed `DebugSession` hypothesis is directionally correct, but it should be split into narrower contracts:

1. `SessionController`: lifecycle commands such as start and cancel.
2. `SessionEventSource`: an ordered live stream or replay cursor.
3. `SessionSnapshot` and `SessionResult`: immutable application-facing state.

The presentation layer must never apply patches, manipulate controller state, issue hidden debugger actions, run verification independently, or classify correctness.

Recommended top-level architecture:

```text
Textual presentation
        |
        | commands + immutable view state
        v
Application/session layer
        |
        | process protocol + ordered events
        v
Dedicated session worker
        |
        v
Existing controller + typed tools + PDB + PatchManager
        |
        v
Existing independent EvaluationVerifier
        |
        v
Application-owned journal, manifest, and existing scientific artifacts
```

This is a **GO** recommendation, subject to the compatibility, cleanup, dependency, and data-safety gates in this plan.

## 2. Repository findings that determine the design

### 2.1 The package controller is synchronous

`agentic_debugger/agent/controller.py` contains `DeterministicController.run(initial_snapshot)`. It:

- creates and advances `ControllerSnapshot` state;
- synchronously calls the configured model adapter;
- validates proposed directives;
- dispatches typed tools through `ToolRegistry`;
- appends immutable `ControllerStepResult` records;
- manages phase transitions, hypothesis lifecycle, budgets, and termination;
- returns a completed `ControllerRunResult`.

There is no general observer, subscriber, async iterator, or session cancellation input. Model and tool failures are converted into bounded controller outcomes, but external application cancellation is not part of the controller contract.

### 2.2 Canonical RunEvents are currently post-run projections

`agentic_debugger/agent/trajectory.py` explicitly models the controller as returning immutable step records rather than accepting an event sink. `project_controller_run()` walks a completed result and emits canonical events in this order:

- `DECISION` for each controller step;
- `ACTION` when an action was attempted;
- `OBSERVATION` for the resulting tool observation;
- `TRANSITION` when controller state changes;
- one terminal `FINAL` event.

The canonical events are therefore not currently available as a live stream during the normal controller run.

### 2.3 The canonical schema and replay behavior are strict contracts

`agentic_debugger/events/schema.py` defines strict `RunEvent` schema version 1.0 with the event types:

- message;
- action;
- observation;
- decision;
- transition;
- final.

Unknown fields are rejected. Event ordering uses deterministic sequences and deterministic event IDs.

`agentic_debugger/events/logger.py` can append and flush sequential `RunEvent` records in real time if a producer supplies them. It checks run/task identity and contiguous sequence numbers. The current controller path simply does not feed it incrementally.

`agentic_debugger/events/replay.py` validates:

- one consistent run/task identity;
- contiguous deterministic ordering;
- action-to-observation linkage;
- legal controller transitions;
- exactly one final event in terminal position.

It never silently reorders malformed data. These behaviors and existing golden trajectories must remain unchanged.

### 2.4 R5/R6 already prove a useful live-observation seam

The accepted real-debugger experiment path is primarily represented by:

- `experiments/debugger_interaction_v2_r5/r5_runner.py`;
- `experiments/debugger_interaction_v2_r6/evaluate_debugger.py`.

The R5 runner supports an optional lifecycle callback and emits bounded events around:

- run start;
- probe materialization;
- task workspace creation;
- controller start and completion;
- model request start and completion;
- rejected and accepted directives;
- tool errors;
- final verifier start/completion/error/skipped;
- cleanup start/completion;
- evidence writing.

R6 adds `CrashDurableLifecycleLog`, which:

- assigns one ordered sequence;
- records wall and monotonic timing;
- records process identity;
- serializes writes under a lock;
- flushes and calls `fsync` for every event.

Its PDB lifecycle adapter also emits worker construction, readiness, target start/result/error, and stop status. Existing R6 lifecycle files demonstrate that live durable observation is feasible.

This seam is nevertheless experiment-specific and coarse. It does not contain all source, stack, locals, patch, tool-observation, or presentation information. The application should generalize its mechanics without making the production UI depend directly on experiment runners.

### 2.5 PDB data is already structured

`agentic_debugger/demo/tools.py` delegates debugger tools to the real PDB implementation. The PDB layer already returns structured results for:

- starting a paused target;
- current script, line, and function;
- pause generation;
- stack summary;
- frame identity and current-frame marker;
- bounded locals summaries;
- safe expression evaluation;
- continue, step, and next operations;
- target exit and debugger lifecycle.

The UI should never duplicate raw PDB-text parsing.

`PdbSession.get_frame()` exists in the lower-level API but is not exposed through the current demo tool registry. V1 can show the recorded stack and recorded locals without adding UI-driven arbitrary frame inspection.

### 2.6 Patch lifecycle exists but is distributed

`DemoToolContext` owns mutable task/workspace state, the current accepted candidate patch, patch manager, PDB session, tool history, test status, changed files, localization, and verifier-feedback history.

When a new accepted patch is applied:

- a previous accepted patch may be reverted;
- the new patch is applied through the real `PatchManager`;
- changed files, hunk counts, hashes, adjustments, and revert state are recorded;
- optional independent verifier feedback may run;
- the accepted candidate replaces `context.candidate_patch`.

Failed and rejected attempts can be found across rejected directives, tool observations, and live-adapter telemetry. They are not represented as one normalized patch-attempt lifecycle suitable for a UI.

### 2.7 The independent verifier is synchronous

`agentic_debugger/evaluation/verifier.py` keeps meaningful internal stages, including:

1. task and patch validation;
2. canonical-source hashing;
3. clean evaluation workspace preparation;
4. baseline and reproduction checks;
5. pre-patch fail-to-pass/pass-to-pass checks;
6. patch application;
7. syntax checks;
8. post-patch reproduction and targeted checks;
9. broader suite execution;
10. final classification;
11. cleanup and canonical integrity confirmation.

These stages are not currently surfaced through a generic progress observer. Consumers receive the final `EvaluationResult` after completion.

Progress events can be added around existing stages, but the final `EvaluationResult` must remain the only correctness authority.

### 2.8 Supported live-model execution is narrower than a generic provider system

`agentic_debugger/evaluation/live.py` already defines:

- `LiveModelConfig`;
- `LiveRunLimits`;
- `JsonlCommandTransport`;
- `LiveModelAdapter`;
- curated live execution and finalization paths.

The model configuration contains a model label, command, request timeout, and tool version. It validates commands and configuration fingerprints and is not intended to contain credentials.

`JsonlCommandTransport` launches a configured command per model request, sends a bounded JSONL request, limits stdout/stderr, waits with a timeout, and terminates/kills the subprocess on timeout. It has no external cancellation API.

The live adapter retains request history, directive attempts, acceptance/rejection, retry, and metrics in memory and final evidence. These are not currently exposed as a generic application stream.

The application should reuse this explicit configured-command capability. It should not claim generic provider support or become a provider-management product.

### 2.9 Local GPU and evaluator paths are not a V1 prerequisite

The R6 evaluator uses in-process local model transports and blocking generation calls. A blocking `model.generate()` call cannot be assumed to stop cooperatively because a TUI worker was cancelled.

Process-isolating an entire session provides a hard termination boundary, but GPU teardown, dependency loading, WSL/operator constraints, and campaign reproducibility deserve separate review.

Therefore:

- deterministic/scripted paths should drive application development and validation;
- configured command-model execution can be supported after cleanup is proven;
- in-process GPU execution remains an optional advanced source;
- QuixBugs/WSL campaign execution is not a normal V1 application mode;
- existing campaign evidence may be opened read-only.

### 2.10 History exists as heterogeneous artifacts, not an application index

The repository contains multiple artifact families:

- canonical JSONL trajectories;
- demo results and semantic projections;
- live evaluation reports;
- R5/R6 `evidence.json` and lifecycle journals;
- training/evaluator telemetry;
- operator attempts and bundles;
- professor-safe traces.

There is no single uniform run-history index. The application must not scan every `runs/` or operator directory and pretend the schemas are equivalent.

Professor traces use their own stable schema and intentionally preserve absent fields as null or not recorded. They generally contain hashes and structured evidence rather than full source and patch bodies. They should be adapted to a read-only presentation timeline without reconstruction or mutation.

## 3. Current execution paths

### 3.1 Deterministic offline demo

```text
python -m agentic_debugger.demo
  -> run_demo()
  -> run_demo_case()
  -> curated task + disposable workspaces/probe
  -> DemoToolContext + DemoPolicyModel + ToolRegistry
  -> DeterministicController.run()
  -> completed ControllerRunResult
  -> project_controller_run()
  -> canonical RunEvents + semantic projection
  -> EvaluationVerifier.evaluate()
  -> cleanup
  -> results, reports, trajectories
```

This is the best first live application source because it exercises real controller, PDB, patch, verifier, and cleanup logic without an external model or GPU.

### 3.2 Configured command-model run

```text
explicit live CLI confirmation
  -> LiveModelConfig + LiveRunLimits
  -> disposable workspace/probe
  -> JsonlCommandTransport
  -> LiveModelAdapter
  -> DeterministicController.run()
  -> optional final EvaluationVerifier
  -> post-run trajectory projection
  -> cleanup and report
```

This can become the supported real-model V1 source after session cancellation and subprocess cleanup are proven.

### 3.3 Accepted R5/R6 debugger path

```text
R5/R6 evaluator/experiment runner
  -> curated task and strict preflight
  -> disposable probe/workspace
  -> DemoToolContext + PDB + registry
  -> R5 bridge/navigation adapter
  -> DeterministicController.run()
  -> optional per-patch verifier feedback
  -> post-run canonical trajectory
  -> final independent verifier
  -> cleanup
  -> lifecycle/evidence/report artifacts
```

This path contains the richest current lifecycle instrumentation but remains scientific experiment infrastructure rather than the application boundary.

## 4. Real-time observability matrix

| Information | In memory during execution | Real-time structured emission today | Persisted today | V1 action |
|---|---|---|---|---|
| Controller phase/state | Yes | Partial R5/R6 lifecycle | RunEvents/evidence after completion | Add per-step observer |
| Model request start/end | Yes | R5/R6 lifecycle | Adapter telemetry/evidence | Promote safe generic events |
| Raw model response | Temporarily | Not canonical | Some experiment telemetry | Restricted; not shown by default |
| Accepted/rejected directive | Yes | R5/R6 lifecycle | Controller/evidence | Generic event projection |
| Tool invocation/result | Yes | Not normal package-wide | Canonical events after completion | Incremental projection |
| PDB lifecycle | Yes | R6 lifecycle | Lifecycle/evidence | Promote lifecycle adapter |
| Current source location | Yes after PDB actions | Not uniform | Observation payload | Normalize location events |
| Active breakpoint | Yes | Not uniform | PDB start/control observation | Normalize view state |
| Stack | When tool is called | No | Tool observation | Stream existing safe result |
| Locals | When tool is called | No | Tool observation | Stream bounded summaries |
| Source context | When source tool is called | No | Sometimes observation | Snapshot for new runs |
| Diagnosis | Adapter/controller memory | Partial | R5/evidence artifacts | Normalize diagnosis event |
| Candidate patch | Yes | Not generally | Controller/evidence | Patch-attempt event |
| Patch applied/reverted | Yes | Not generally | Observation/evidence | Normalize lifecycle |
| Verifier stages | Internal only | Coarse start/end | Final result | Optional stage observer |
| Final classification | After completion | Some final lifecycle | `EvaluationResult` | Reuse unchanged |
| Budgets/status | Snapshot/adapter metrics | Partial | Reports | Status projection |
| Cleanup | Finally blocks | R5/R6 lifecycle | Evidence | Required session terminal evidence |

## 5. UI technology decision

### 5.1 Recommendation: Textual TUI

Textual best matches the current repository and V1 product because it provides:

- direct Python integration;
- full-screen panels, tabs, tables, scrolling, keyboard navigation, and status bars;
- async/thread workers for consuming an application stream;
- Rich/Pygments code and diff rendering;
- headless application tests through `App.run_test()` and Pilot;
- conventional Python console-entry-point packaging;
- Windows and Windows Terminal support;
- no HTTP server, browser process, frontend serialization layer, or JavaScript toolchain.

Windows does not support Textual inline mode, so V1 should explicitly be a full-screen application.

If approved during implementation, Textual should be an optional application dependency rather than a dependency of the scientific core, conceptually:

```toml
[project.optional-dependencies]
app = ["textual>=8,<9"]
```

The exact range is an owner/dependency decision for the implementation task. Use base Textual plus Rich/Pygments. Avoid `textual[syntax]` initially because its tree-sitter language packages are disproportionate for read-only V1 source/diff display.

### 5.2 Local browser GUI

Benefits:

- excellent source and diff presentation;
- familiar mouse interactions;
- a natural path to broader visualization later.

Costs in this repository:

- local HTTP server and port lifecycle;
- SSE/WebSocket/polling protocol;
- browser launching and localhost security decisions;
- frontend serialization and state synchronization;
- a new frontend language/toolchain or a constrained server-rendered approach;
- separate backend/frontend packaging and testing.

This is disproportionate before the session/event contracts are established.

### 5.3 Desktop UI

Tkinter would minimize dependencies but is a poor fit for a modern multi-panel code/debugger application. PySide/Qt would provide a stronger interface but introduces a large runtime and demanding Windows packaging. No current requirement justifies that cost.

### 5.4 Decision

Build a full-screen Textual TUI for V1. Keep the application/session boundary independent of Textual so a later browser or desktop client can reuse it if product evidence justifies one.

## 6. Precise V1 product definition

### 6.1 Home screen

Actions:

- New Session;
- Run History;
- Open Recorded Run/Trace;
- configuration and application diagnostics.

### 6.2 New Session workflow

Available executable sources:

1. Offline deterministic demo — default.
2. Configured command-model execution — explicitly confirmed.

Recorded material is not presented as a new live session. App-owned sessions, canonical trajectories, and supported frozen evidence traces are opened through **Run History** or **Open Recorded Run/Trace** and are always labeled as replay/recorded sources.

Configuration fields:

- curated task;
- source mode;
- policy when applicable;
- model configuration file/profile for command-model runs;
- visible controller/model/time budgets;
- artifact destination.

For a configured model, display model label, command/configuration fingerprint, tool version, and limits. Do not edit credentials or present provider/account management.

### 6.3 Live session workspace

```text
+ Session/task/source | LIVE | phase | substate | budgets | elapsed | Cancel +
+----------------------------------------+----------------------------------+
| Source                                 | Debugger                         |
| current line and breakpoint markers    | Stack / Locals / Breakpoints    |
| before/after/diff toggle               | safe recorded observations      |
+----------------------------------------+----------------------------------+
| Activity timeline                      | Diagnosis / Patch / Verifier     |
| controller/model/tool/error/cleanup    | attempts, state, stage progress |
+ keyboard shortcuts | filters | artifact path | cleanup status -----------+
```

Activity filters:

- controller;
- model;
- tools;
- debugger;
- patch;
- verifier;
- lifecycle/errors.

### 6.4 Replay workflow

Replay uses the same reducer and panels as live execution and is visibly marked `REPLAY` or `RECORDED`.

Controls:

- previous/next event;
- previous/next controller phase;
- beginning/end;
- play/pause presentation;
- jump to sequence;
- timeline filters.

Replay never invokes tools, PDB, patch application, model calls, or verification.

### 6.5 Run history

Show:

- session ID;
- original run ID where applicable;
- task;
- source kind;
- start/end time;
- application terminal status;
- verifier outcome if recorded;
- interrupted or cleanup-failed status;
- provenance/artifact path.

### 6.6 Controls

V1 controls are intentionally narrow:

- start session;
- cancel running session with confirmation;
- navigate views/tabs;
- filter activity;
- select a recorded stack frame for presentation;
- copy/export visible evidence or open its artifact location;
- replay navigation.

There is no separate Apply button, manual controller transition, manual verifier command, or arbitrary PDB command.

## 7. Proposed application architecture

### 7.1 Components

```text
agentic_debugger.ui
    Textual App, screens, widgets, keybindings
                    |
                    v
agentic_debugger.application
    SessionService
    SessionController protocol
    SessionEventSource
    SessionViewReducer
    HistoryStore / ReplaySource
                    |
             process protocol
                    v
Dedicated session worker
    execution-source adapters
                    |
                    v
Existing controller / typed tools / PDB / PatchManager
                    |
                    v
Existing independent EvaluationVerifier
                    |
                    v
App-owned manifest + journal + canonical scientific artifacts
```

### 7.2 Proposed new modules

`agentic_debugger/application/session.py`

- `SessionSpec`;
- `SessionId`;
- `SessionStatus`;
- `SessionSnapshot`;
- `SessionResult`;
- `SessionController` protocol;
- `SessionService`.

`agentic_debugger/application/events.py`

- versioned `SessionEvent`;
- event kinds and bounded payloads;
- crash-durable journal writer/reader.

`agentic_debugger/application/presentation.py`

- pure `reduce_event(view_state, event)`;
- immutable `SessionViewState`;
- no subprocess, controller, or file mutation.

`agentic_debugger/application/worker_protocol.py`

- start/cancel messages;
- worker status/event notifications;
- process-lifecycle and error envelopes.

`agentic_debugger/application/sources.py`

- offline deterministic source;
- configured command-model source;
- replay-source contracts.

`agentic_debugger/application/history.py`

- owned manifest validation/indexing;
- journal discovery;
- historical read-only adapters.

`agentic_debugger/ui/`

- Textual application;
- home/history/session screens;
- panels and styles;
- application entry point.

### 7.3 Existing bounded changes

- Controller: optional observer and cancellation checkpoint between steps; observer output is adapted into application-owned `SessionEvent` records.
- Canonical trajectory projection: no live-path change is required for V1; the existing post-run `project_controller_run()` path remains the compatibility authority and continues to produce canonical `RunEvent` 1.0 trajectories after completion.
- Verifier: optional progress observer and cancellation checks between stages.
- Command/PDB execution: cooperative application cancellation hooks.
- Demo/live/R5 integration: observer/cancellation plumbing with unchanged defaults.
- Packaging: optional application dependency and entry point.

### 7.4 Mutable state ownership

- Controller state, PDB, disposable workspaces, patch state, and verifier execution belong to the worker.
- Session service owns only application lifecycle, worker supervision, and artifact registration.
- TUI owns only presentation selection, scroll/filter state, and replay cursor.
- Presentation state is derived from immutable events.
- Replay owns no executable resources.

### 7.5 Threading and process model

- Textual runs its normal async UI loop.
- A lightweight async worker consumes event notifications and journal offsets.
- Every live debugging session runs in a dedicated child process.
- A single worker-side writer owns event sequence assignment and durable journal writes.
- The UI never runs controller, PDB, model, or verifier operations on its event loop.
- Queue messages may notify the UI, but the durable journal is authoritative and supports catch-up.

## 8. Event and observability design

### 8.1 Do not mutate RunEvent 1.0

Introduce a separate application-owned `SessionEvent` schema. It may contain:

```text
schema_version
session_id
run_id
task_id
sequence
timestamp_utc
source_kind
event_kind
controller_phase
payload
```

Suggested event kinds:

- `session.created`;
- `session.started`;
- `session.status_changed`;
- `session.cancel_requested`;
- `session.completed`;
- `session.failed`;
- `session.cancelled`;
- `controller.step`;
- `model.request_started`;
- `model.request_completed`;
- `model.directive_accepted`;
- `model.directive_rejected`;
- `tool.started`;
- `tool.completed`;
- `debugger.started`;
- `debugger.location_changed`;
- `debugger.stack_observed`;
- `debugger.locals_observed`;
- `patch.proposed`;
- `patch.rejected`;
- `patch.applied`;
- `patch.reverted`;
- `verifier.started`;
- `verifier.stage_started`;
- `verifier.stage_completed`;
- `verifier.completed`;
- `cleanup.started`;
- `cleanup.completed`;
- `artifact.written`.

### 8.2 Ordering and durability

- One worker-side writer assigns contiguous sequence numbers.
- Sequence is authoritative; timestamps are informational.
- Every durable record is flushed using the proven R6 lifecycle pattern.
- UI notifications contain sequence/offset information.
- A slow or disconnected UI catches up from disk rather than dropping evidence.
- Presentation failure cannot change controller decisions.

### 8.3 Canonical trajectory compatibility boundary

Do **not** make incremental canonical `RunEvent` emission a V1 requirement. Live application observability is carried by the separate application-owned `SessionEvent` stream. After a controller run completes, the existing `project_controller_run()` path produces canonical `RunEvent` 1.0 output exactly as it does today.

Recorded canonical trajectories are adapted read-only into the application presentation model for replay; they are not rewritten into the live journal and the canonical projector remains independent of UI delivery. Existing event IDs, sequences, payloads, transition behavior, semantic projections, replay rules, and golden trajectories therefore remain the compatibility authority.

If implementation evidence later shows that a required V1 behavior cannot be supported with this separation, incremental canonical projection may be reconsidered only as a separate reviewed compatibility task.

### 8.4 Safe-data boundary

Application events may contain only bounded data already safe for model/tool observation or explicitly approved application source snapshots.

Do not stream by default:

- credentials or environment secrets;
- hidden tests;
- evaluator oracle information;
- unbounded stdout/stderr;
- complete raw prompts or raw model output;
- paths or environment metadata current evidence intentionally redacts.

## 9. Source and debugger synchronization

Use the existing structured PDB values:

- script;
- line;
- function;
- debugger state;
- pause generation;
- stack frames;
- current-frame marker;
- bounded locals.

`pause_generation` should prevent stale stack/locals observations from replacing information for a newer pause.

Current source location comes from the latest location-bearing debugger event. Active breakpoint comes from structured PDB start/control payloads.

The UI-selected stack frame is presentation-only in V1. It displays already recorded data and does not silently issue a PDB frame-change command.

### 9.1 Source snapshots for new sessions

Capture application-safe source snapshots when:

- task source is initially loaded;
- a new patch candidate is accepted;
- a patch is applied;
- a patch is reverted.

Store:

- logical file identity;
- workspace/repository-relative path;
- content hash;
- bounded source text;
- line/provenance metadata.

This is necessary because disposable workspaces are removed after the run.

Historical evidence without source text must display `NOT RECORDED`. It must not be reconstructed from the current checkout and presented as historical fact.

## 10. Patch and verifier presentation

Normalize patch attempts into application view states:

```text
proposed -> rejected
proposed -> apply failed
proposed -> applied -> reverted
proposed -> applied -> independently verified
```

The application should retain bounded patch text for new app-owned sessions where safe. Existing evidence containing only hashes remains hash-only.

Patch application means only that the workspace mutation succeeded. It never means the repair is correct.

Expose verifier stages as progress information:

- preparing clean workspace;
- baseline/reproduction;
- pre-patch targeted checks;
- applying candidate;
- syntax validation;
- post-patch reproduction;
- fail-to-pass/pass-to-pass checks;
- broader suite;
- classification;
- cleanup/integrity.

Stage status may be running, completed, failed, skipped, or cancelled. Final semantic outcome and classification remain unavailable until the verifier returns its normal `EvaluationResult`.

## 11. Live execution sources

### 11.1 V1 supported

**Executable session sources:**

1. **Offline deterministic demo** — first-class default and validation source.
2. **Configured command-model live run** — explicit opt-in and existing limits/configuration.

**Recorded/replay sources:**

3. **App-owned session bundle.**
4. **Canonical trajectory.**
5. **Read-only supported R5/R6 evidence and frozen professor-trace format.**

Recorded/replay sources are always labeled as such and never enter the live-start workflow. An R5-style scripted transport may be used as a development/test source without becoming a product-facing source mode.

### 11.2 Deferred

- in-process local GPU model execution;
- WSL/QuixBugs evaluation campaigns;
- arbitrary provider configuration;
- arbitrary repository/task ingestion.

### 11.3 Model configuration UX

The application selects a validated JSON configuration or named local profile and displays:

- model name/label;
- tool version;
- configuration fingerprint;
- request and controller limits;
- live/offline status.

It does not store credentials, edit provider accounts, download models, or install runtimes.

## 12. Run history and replay

No database is justified for V1.

Use an application-owned root such as:

```text
%LOCALAPPDATA%\AgenticDebugger\runs\<session-id>\
    manifest.json
    session.events.jsonl
    controller.events.jsonl
    result.json
    candidate.patch
    evaluation.json
    source\
```

Only applicable artifacts should be created.

`manifest.json` should include:

- manifest schema version;
- application session ID;
- original run/trajectory/evaluation ID where applicable;
- task and source kind;
- start/end time;
- terminal status;
- configuration fingerprint;
- relative artifact paths and hashes;
- provenance;
- cleanup state.

Manifest updates must be atomic. Startup scans one level of application-owned run directories and validates each manifest. Incomplete journals are classified as interrupted, not successful.

### 12.1 Historical evidence adapters

Provide read-only adapters for known formats. Adapters:

- preserve original identifiers/hashes;
- normalize known facts into presentation events;
- mark unavailable information as not recorded;
- never write back to the source folder;
- never reclassify historical results.

Do not recursively index heterogeneous campaign and operator directories as if they shared one schema. Built-in professor traces can be indexed explicitly. Other evidence may be opened by path or imported as an app-owned reference plus provenance hash.

## 13. Session lifecycle and cancellation

Recommended application lifecycle:

```text
CREATED
  -> STARTING
  -> RUNNING
       WAITING_MODEL
       EXECUTING_TOOL
       PDB_PAUSED
       VERIFYING
       CLEANING
  -> SUCCEEDED
   | UNRESOLVED
   | FAILED
   | CANCELLED
   | TIMED_OUT
   | INTERRUPTED
   | CLEANUP_FAILED
```

Application completion and scientific correctness are separate. `SUCCEEDED` means execution completed normally; the verifier may still report an unsuccessful repair.

### 13.1 Cancellation sequence

1. UI sends `cancel(session_id)`.
2. Worker records `session.cancel_requested`.
3. Cooperative cancellation is checked before/after model requests, between controller steps, around tool execution, between verifier stages, and during subprocess polling.
4. Active configured-model subprocess is terminated.
5. PDB receives normal stop/cleanup.
6. Task and PDB workspaces are cleaned.
7. After a bounded grace period, the worker process tree is terminated if still alive.
8. Cleanup is verified and recorded.
9. Only then is `CANCELLED` emitted.

Ctrl+C and terminal shutdown use the same path.

A presentation disconnect does not automatically cancel a session. The worker continues writing its durable journal, and a new UI can reopen it.

### 13.2 Failure boundaries

- Malformed model response: rejected directive/retry event, then bounded failure after exhaustion.
- Controller failure: terminal controller status; verifier skipped unless existing policy permits it.
- PDB failure: typed tool error, stop attempt, cleanup.
- Subprocess failure: bounded exit and stderr diagnostic.
- Verifier failure: verifier error, never an inferred correctness verdict.
- Journal failure: session reporting failure while preserving any already produced scientific artifact.
- Cleanup failure: distinct terminal status and visible leftover diagnostic.

## 14. Compatibility and evidence safety

The implementation must preserve:

- existing CLI behavior and arguments;
- deterministic demo behavior and artifacts;
- `ControllerRunResult` semantics;
- strict `RunEvent` 1.0 schema;
- canonical event ordering and IDs;
- replay validation and rejection rules;
- golden trajectories and semantic projections;
- live configuration/request protocol schemas;
- accepted R5/R6 experiment evidence;
- professor traces and audit hashes;
- verifier taxonomy and sole authority;
- historical evaluation outcomes;
- explicit live-model confirmation;
- offline-default behavior.

The application must not:

- rewrite or append to frozen scientific evidence;
- mix `SessionEvent` records into existing canonical trajectory files;
- reclassify historical outcomes;
- infer absent source or patch information;
- require Textual imports for core modules or existing CLI paths;
- require an application process for existing evaluation workflows.

## 15. Validation strategy

### 15.1 Unit tests

- `SessionEvent` schema, bounds, redaction, and ordering;
- journal append/recovery;
- atomic manifest updates;
- presentation reducer transitions;
- source snapshot hashing/safe paths;
- history adapters and missing-field behavior;
- cancellation lifecycle.

### 15.2 Canonical event parity

For existing demos and golden trajectories:

1. stream controller steps through the incremental projector;
2. project the completed result through the legacy public path;
3. compare exact events, IDs, sequences, payloads, and transitions;
4. compare semantic projections;
5. retain existing adversarial replay tests.

This is the primary compatibility gate.

### 15.3 Session integration tests

Use deterministic/scripted transports to exercise:

- waiting for a model;
- rejected directive and retry;
- real structured PDB pause/stack/location;
- source observation;
- rejected and accepted patch attempts;
- verifier stages and final result;
- cleanup and final artifacts.

### 15.4 Cancellation and error tests

Cancel during:

- a model command;
- a paused PDB session;
- a test subprocess;
- verifier execution;
- cleanup.

Assert:

- process-tree termination;
- PDB worker shutdown;
- disposable workspace removal;
- durable terminal event;
- honest cleanup status.

Windows process-group behavior needs focused validation.

### 15.5 Replay parity

For every event prefix in representative new sessions:

1. reduce the live event into view state;
2. reload the recorded prefix;
3. replay it;
4. assert identical presentation state.

Historical adapters separately verify explicit `NOT RECORDED` behavior.

### 15.6 Textual tests

Use `App.run_test()` and Pilot for:

- keyboard navigation;
- timeline scrolling/filtering;
- replay stepping;
- cancel confirmation;
- live event arrival;
- error and corrupt-history states;
- representative terminal sizes such as 80x24, 120x40, and 160x50.

Optional SVG snapshot testing should be introduced only if it proves maintainable.

### 15.7 Smoke and packaging

- Clean Windows virtual environment.
- Install core only; confirm existing CLI behavior.
- Install optional app extra.
- Launch application and help entry point.
- Complete one deterministic offline session.
- Cancel one delayed scripted session.
- Reopen both through history/replay.

No GPU, model download, or external provider is required for normal application validation.

## 16. Ordered implementation roadmap

### Task 1 — Establish application contracts

**Objective:** Define session lifecycle, application events, immutable presentation state, execution-source contracts, and safe-data policy.

**Why first:** Every UI and execution change depends on stable boundaries.

**Expected areas:** New `agentic_debugger/application/` package and tests.

**Key work:**

- `SessionSpec`, status, result, controller, and event-source protocols;
- versioned `SessionEvent`;
- pure presentation reducer;
- failure taxonomy and data-safety rules;
- accepted architecture documentation.

**Acceptance criteria:**

- The reducer represents every proposed V1 state.
- Live and replay can satisfy the same presentation-input contract.
- No Textual or execution-core mutation is required for the contract tests.

**Validation:** Focused schema/reducer/unit tests.

**Dependencies:** None.

**Non-goals:** UI, controller edits, dependency additions.

### Task 2 — Add incremental controller observability

**Objective:** Publish controller-step activity as application-owned `SessionEvent` records during execution without changing canonical trajectory generation.

**Why second:** This validates the hardest observability boundary before UI investment while keeping the accepted scientific event contract isolated.

**Expected areas:** Controller, controller-native observation contract, application event adapter, focused tests.

**Key work:**

- optional typed observer with no-op default and explicit run/task identity;
- controller-native observations at authoritative execution boundaries;
- application-event adapter that emits validated controller-owned `SessionEvent` prefixes;
- unchanged post-run canonical projection through the existing public path.

Cancellation checkpoints are intentionally deferred to Task 3 so cancellation is
introduced together with the worker/process boundary, cooperative token, subprocess
termination, and cleanup verification rather than as a partial controller-only contract.

**Acceptance criteria:**

- Existing controller callers and results remain unchanged.
- Existing canonical trajectories, event IDs, sequences, semantic projections, golden fixtures, and replay behavior remain unchanged.
- Observer/presentation failure cannot alter controller decisions.
- No `RunEvent` 1.0 schema or live-projector requirement is introduced.

**Validation:** Existing controller, trajectory, golden, semantic, and replay tests plus focused observer/`SessionEvent` tests.

**Dependencies:** Task 1.

**Non-goals:** Incremental canonical `RunEvent` emission, verifier streaming, UI, changes to `RunEvent` 1.0.

### Task 3 — Build the cancellable worker boundary

**Objective:** Isolate application execution behind a dependable cancellable worker/process boundary.

**Why third:** Live UI must not run blocking controller/model/PDB operations on its event loop.

**Accepted implementation areas:** Worker protocol, neutral cancellation contract, controller/runtime cancellation checkpoints, crash-durable session journal, process-tree supervision, and workspace cleanup.

**Accepted implementation:**

- dedicated subprocess worker with a strict bounded local JSON-lines protocol;
- neutral cancellation token/error propagated without creating a scientific controller stop reason;
- cancellable `CommandRunner` polling with unchanged no-cancellation behavior;
- worker-authoritative `SessionEvent` journal with flush/fsync durability and validated crash-prefix recovery;
- sequence-only parent notifications; full event bodies remain journal-authoritative;
- worker-owned disposable execution workspace created only after `session.started`;
- honest pre-start cancel/timeout, startup failure, journal-fatal, crash/interruption, cleanup-failure, and cooperative-cancel semantics;
- fail-closed Windows Job Object containment established before the suspended worker executes;
- bounded forced escalation that terminates the worker and descendants, including the real PDB-worker topology;
- production deterministic application-source wiring remains deferred to Task 7; Task 3 uses only a bounded non-product scenario harness to prove infrastructure.

**Acceptance criteria:**

- cooperative cancellation after execution starts reports `CANCELLED` only after verified cleanup;
- pre-start cancellation/timeout leave no disposable work directory and do not fabricate a cleanup cycle;
- forced Windows escalation leaves no Task-3 worker descendant alive and never claims cooperative `CANCELLED`;
- crash, malformed/incomplete journal, and journal-write failure can never classify as successful completion;
- valid Task-1 `SessionEvent` records, including large debugger-local payloads, survive journal persistence and parent catch-up;
- no-cancellation controller/runtime behavior and canonical scientific trajectory semantics remain unchanged.

**Validation:** Windows cancellation, timeout, startup-failure, crash, journal, cleanup, real-descendant process-tree, and compatibility tests.

**Dependencies:** Tasks 1–2.

**Non-goals:** Production deterministic source wiring (Task 7), verifier-stage/debugger/patch observability (Task 4), history/indexing (Task 5), configured-model provider execution (Task 8), or guaranteed cooperative cancellation of in-process GPU inference.

### Task 4 — Expose patch, source, debugger, and verifier progress

**Status:** ACCEPTED (2026-08-15).

**Objective:** Supply truthful structured debugger/source/patch/verifier information to the shared application presentation model.

**Accepted implementation:**

- real structured PDB location, breakpoint, stack, current-frame, locals, and pause-generation projection without reparsing terminal text;
- safe bounded source snapshots with repository-relative logical paths, full-source SHA-256, deterministic UTF-8 truncation, and explicit withholding of credential-shaped content;
- normalized diagnosis and patch lifecycle including proposed, rejected, apply-failed, applied, and reverted states, with optional safe patch body;
- optional verifier stage observer and between-stage operational cancellation while the existing final `EvaluationResult` remains the sole correctness authority;
- shared `SessionEventEmitter` as the session-wide identity/clock/sequence/sink authority used by the worker lifecycle and controller/debugger/source/patch/verifier producers;
- pure presentation-state support for source/current-line, debugger state, diagnosis, patch attempts, verifier progress, and final verifier summary;
- explicit runtime-local credential redaction and bounded safe-data enforcement.

**Acceptance criteria:**

- patch application is never treated as repair correctness;
- stale debugger observations cannot replace newer recorded pause state;
- source snapshots are bounded, path-safe, hashed, and replayable after disposable workspace cleanup;
- verifier observation/cancellation does not change scientific result semantics;
- one live application journal has a contiguous sequence across all producer families;
- no hidden-test/oracle/credential content is intentionally persisted through these producer paths;
- canonical scientific `RunEvent` 1.0 remains unchanged.

**Validation:** Application observability/adversarial tests, real PDB and verifier integration, shared-emitter/journal integration, source/patch safety tests, UTF-8 boundary tests, and directly affected compatibility gates.

**Dependencies:** Tasks 1–3.

**Non-goals:** User-issued PDB commands, manual patch editing, Textual UI, or production deterministic worker-to-demo source wiring.

### Task 5 — Add app-owned history and replay

**Status:** ACCEPTED (2026-08-15).

**Objective:** Persist and reopen app-owned sessions safely and adapt supported recorded evidence read-only through the same presentation model.

**Accepted implementation:**

- filesystem-backed `HistoryStore`; no database;
- one-level app-owned session discovery with resolved-path containment on register, list, and reopen;
- atomic versioned manifests derived from the authoritative Task-3 journal;
- manifest/journal/artifact hash and identity consistency checks before a session can classify as complete;
- honest complete, interrupted, malformed/corrupt, invalid-manifest, and unregistered classifications;
- read-only `SessionReplaySource` navigation over persisted `SessionEvent` streams using the same pure reducer as live execution;
- prefix-by-prefix live/replay presentation parity;
- explicit read-only adapters for canonical trajectories, R5 evidence, and professor-safe traces;
- genuine recorded run identity preserved only when actually present; source commits/experiment ids retained as provenance rather than invented run ids;
- historical absent data remains not recorded and source evidence is never reconstructed from the current checkout.

**Acceptance criteria:**

- existing frozen/historical evidence remains byte-for-byte untouched;
- external/symlink-escaped directories cannot be treated as app-owned history;
- stale/tampered manifests or referenced artifacts cannot remain `COMPLETE`;
- missing historical fields are never invented;
- invalid/incomplete journals cannot appear successful;
- replay invokes no controller, model, PDB, patch application, verifier, or cleanup;
- no database is added.

**Validation:** Manifest integrity/containment tests, interrupted/malformed journal tests, historical provenance/absence tests, replay navigation and live/replay parity tests, and read-only evidence mutation guards.

**Dependencies:** Tasks 1–4.

**Non-goals:** Migrating/indexing every campaign folder, background file watching, remote history, or UI implementation.

### Task 6 — Build the replay-first Textual application

**Status:** ACCEPTED (2026-08-15).

**Objective:** Deliver the V1 information architecture as a professional replay-first terminal application.

**Accepted implementation:**

- optional Textual 8 application extra and one documented module launch surface;
- Home/History screen backed by `HistoryStore`, including honest complete,
  interrupted, malformed, invalid-manifest, and empty states;
- shared Session Workspace for replay/live rendering with Source, Debugger,
  Patch, Verifier, Activity, and Timeline panes;
- replay navigation for event, effective-phase, begin/end, and sequence jump;
- all domain content rendered from immutable `SessionViewState`; UI-only focus,
  tab, scroll, and cursor state remains outside the presentation model;
- recorded/evidence content is appended as literal Rich `Text` with styles
  supplied separately, preventing markup interpretation or escaping artifacts;
- successful Start-session navigation replaces the start form so workspace
  back/quit returns directly to Home/History;
- finished/failed live-runner ownership is released without joining the runner
  thread from the Textual event-loop callback, allowing repeated sessions in one
  application lifetime;
- optional Textual dependency does not contaminate the scientific/core import
  surface.

**Acceptance criteria:**

- replay performs no controller/model/PDB/patch/verifier execution;
- Home -> Start -> Workspace -> Home navigation is stable and reusable;
- sequential completed/cancelled/failed sessions do not retain stale execution
  ownership;
- source/debugger/patch/verifier/activity/timeline panes render recorded evidence
  literally and missing information honestly;
- representative terminal sizes and headless interaction remain usable;
- replay continues to use the same pure reducer as persisted/live state.

**Validation:** Headless Textual/Pilot application, navigation, lifecycle,
rendering-safety, replay, adversarial, and resize tests; final Repair Pass 1 UI
surface reported 65/65 passing.

**Dependencies:** Tasks 1 and 5; consumes Task 4 data model.

**Non-goals:** Browser support, code editing, arbitrary PDB console, or provider configuration.

### Task 7 — Wire deterministic live sessions

**Status:** ACCEPTED (2026-08-15).

**Objective:** Run the genuine deterministic offline controller/PDB/patch/verifier path inside the application and persist/replay it through the same presentation model.

**Accepted implementation:**

- production deterministic source distinct from the Task-3 synthetic worker
  scenarios;
- real deterministic controller, tool registry, PDB, PatchManager, disposable
  workspace, and independent verifier composition;
- one worker/session `SessionEventEmitter` shared across lifecycle, controller,
  debugger/source/patch, verifier, cleanup, and terminal events;
- durable journal remains authoritative; lightweight notifications trigger
  parent catch-up, including terminal remainder delivery after process exit;
- `LiveSessionRunner` supervises `SessionWorkerProcess` on a background thread,
  keeping controller/PDB/verifier work off the Textual event loop;
- cooperative live cancellation uses the accepted Task-3 path and terminal UI
  status waits for durable worker evidence;
- app teardown closes/cancels boundedly and cannot intentionally orphan a live
  worker;
- completed/interrupted sessions integrate with app-owned history and can be
  reopened read-only;
- repeated deterministic sessions in one TUI lifetime receive distinct session
  identities and remain independently replayable.

**Acceptance criteria:**

- user can start, observe, cancel, finish, return to history, start another
  deterministic session, and replay prior sessions;
- source/debugger/patch/verifier facts come from real executed operations, not
  UI-only synthetic events;
- cleanup is verified before cooperative terminal cancellation;
- a completed real run's live final `SessionViewState` equals the final replay
  state from its persisted journal;
- UI shutdown leaves no live worker/process descendant under the accepted
  supervision boundary;
- configured/external model execution remains deferred to Task 8.

**Validation:** Real deterministic worker/UI end-to-end tests, sequential-session
tests, cancellation/no-orphan tests, history registration/reopen, journal
catch-up, and live/replay parity. Final captured evidence: 175 events,
`succeeded/done`, cleanup verified, verifier `COMPLETED/RESOLVED` (f2p 1/1,
p2p 2/2), final live/replay state equal.

**Dependencies:** Tasks 1–6.

**Non-goals:** External/configured command-model execution.

### Task 8 — Add configured command-model execution and harden V1

**Status:** ACCEPTED (2026-08-16) — Local Application V1 final milestone.

**Objective:** Support explicitly configured command-model execution through the
accepted application architecture and complete release-quality V1 hardening.

**Accepted implementation:**

- app-owned, versioned `command-models-v1` configuration with explicit argv,
  `shell=False`, bounded direct file reads, safe profile identifiers,
  deterministic safe fingerprints, and structural secret-free diagnostics;
- configured profiles are fingerprint-pinned from UI selection through worker
  consumption, so configuration mutation fails closed before executable launch;
- the existing `JsonlCommandTransport`, `LiveModelAdapter`, `LiveModelConfig`,
  and live protocol remain the command-model authority rather than a parallel
  provider protocol;
- configured execution shares the same local-session pipeline as deterministic
  execution: controller, typed tools, real PDB, PatchManager, independent
  verifier, SessionCoordinator, one `SessionEventEmitter`, durable journal,
  app-owned history, pure presentation reducer, and read-only replay;
- configured-command output, stderr, protocol lines, diagnostics, argv/env
  surfaces, and persisted candidate artifacts are bounded and subjected to the
  accepted safe-data policy;
- explicit cancellation remains operational `CancellationError`; request timeout
  remains a distinct transport failure;
- Windows retains Job Object / `taskkill` tree containment;
- POSIX configured requests own independent process groups and retain ownership
  until final group cleanup has been attempted on every exit path: successful
  response, natural error, cancellation, timeout, bounded transport failure,
  and worker shutdown;
- configured-command subprocesses are documented as trusted user configuration:
  V1 does not falsely claim child-process network isolation and introduces no
  provider SDK/account/key-management layer;
- the existing Textual Start-session flow supports deterministic and configured
  modes using the same Workspace and `SessionViewState`;
- completed configured sessions register in history and replay without executing
  their command, with live/replay final-state parity;
- core imports remain Textual-free and the optional application packaging/launch
  path remains supported.

**Final acceptance evidence:**

- command configuration: 59 passed;
- cross-platform configured transport: 22 passed;
- real POSIX request-tree suite under WSL Ubuntu-22.04: 8 passed, including
  success, natural error, cancellation, timeout, and worker-shutdown descendant
  cleanup;
- configured-source integration on Windows: 19 passed;
- configured Textual integration: 15 passed;
- Task-3 worker/process gates: 36 passed;
- deterministic/replay compatibility and command-runner cancellation gates
  remained green;
- compile/import/packaging/whitespace gates remained clean;
- final review package path/count/integrity matched the declared 24-file
  candidate, and FirstMate independently reconstructed the accepted lineage and
  re-ran 59 configuration, 22 transport, and 8 POSIX process-tree tests
  successfully.

**Acceptance criteria satisfied:**

- no generic provider capability is claimed;
- configured mode requires validated explicit configuration;
- command cancellation/timeout and normal request completion do not knowingly
  leave ordinary request-group descendants behind on the validated Windows/POSIX
  paths;
- diagnostics and app-owned evidence remain bounded and secret-safe within the
  accepted policy;
- replay performs no command/model execution;
- deterministic/offline mode remains independent of configured-model
  dependencies;
- verifier correctness authority and canonical scientific `RunEvent` 1.0 remain
  unchanged;
- frozen scientific evidence remains read-only.

**Dependencies:** Tasks 1–7.

**Non-goals:** Provider marketplace/SDK integration, credential vaults, GPU
campaigns/model hosting, arbitrary repository support, QuixBugs campaign
control, browser UI, IDE/editor behavior, or OS-level network sandboxing.

## 17. Principal risks and mitigations

| Risk | Mitigation |
|---|---|
| UI becomes a second controller | UI sends only session start/cancel and presentation-navigation commands. |
| Canonical schema regression | Keep canonical `RunEvent` projection post-run and unchanged; use separate `SessionEvent` records for live application observability and retain existing golden/replay tests as the compatibility gate. |
| Live/replay divergence | One pure reducer and prefix-by-prefix parity tests. |
| Production UI couples to R5/R6 experiments | Promote generic mechanics; retain scientific runners/adapters and provenance. |
| UI thread blocks on model/PDB | Dedicated session process; UI consumes events only. |
| Cancellation strands children/workspaces | Cooperative checkpoints, graceful stop, bounded kill-tree escalation, cleanup verification. |
| Verifier progress is treated as correctness | Progress is informational; only final `EvaluationResult` is authoritative. |
| Historical source is reconstructed incorrectly | Snapshot new sessions; older absent data stays `NOT RECORDED`. |
| Hidden tests/secrets leak into UI | Safe projections, redaction tests, bounded outputs, restricted raw telemetry. |
| Queue backpressure loses evidence | Worker-side durable journal is authoritative; UI catches up by sequence. |
| Large dependency/toolchain expansion | Optional base Textual only; no browser stack or tree-sitter extra in V1. |
| Product implies arbitrary-repository support | V1 exposes only existing curated/supported task sources. |
| GPU becomes a validation prerequisite | Deterministic and scripted sources drive all routine gates. |
| Terminal compatibility is inconsistent | Windows Terminal baseline, representative-size tests, critical-marker fallbacks. |

## 18. Explicit non-goals

- Full IDE or editor.
- Replacement debugger.
- Generic coding assistant.
- Multi-agent platform.
- Arbitrary repository onboarding.
- Browser/desktop parity.
- Remote service or collaboration.
- Manual PDB console.
- Arbitrary user-issued debugger commands.
- Manual patch editor.
- User-controlled verification/classification.
- Provider marketplace or credential management.
- Training/evaluator campaign orchestration.
- Mandatory local GPU support.
- Mutation or reinterpretation of accepted evidence.
- Replacement of existing CLI workflows.
- Database-backed history.

## 19. Stop conditions and owner gates

Do not enable configured live execution if:

- controller observability changes cannot preserve existing canonical trajectory, golden, semantic-projection, and replay behavior unchanged;
- Windows cancellation cannot prove PDB, subprocess, and workspace cleanup;
- safe source/event projection would leak hidden or sensitive evaluator information;
- application event journaling cannot recover an interrupted session honestly.

Do not adopt Textual until:

- the new material dependency is approved;
- a clean Windows environment can install and launch it;
- core-only installation and existing CLI paths remain independent of Textual.

Do not expose local GPU or WSL/campaign execution without a separate explicitly authorized task covering dependencies, cancellation, hardware behavior, and scientific evidence safety.

Replay and deterministic offline sessions remain independently viable if configured-model execution fails its gates.

## 20. Document role and authority

Canonical tracked location:

```text
docs/architecture/local-application-v1.md
```

This file is the **active implementation architecture and phased plan** while Local Application V1 is under construction. After completion, preserve stable architecture decisions and mark the implementation roadmap completed or superseded rather than deleting historical decisions.

Relationship to other planning documents:

- `TODO.md`: concise current application roadmap and milestone status, linking here for the detailed task contracts.
- `docs/project-tracker.md`: current execution status, accepted task results, validation evidence, and blockers as implementation proceeds.
- `docs/architecture/local-application-v1.md`: authoritative V1 boundaries, contracts, phased tasks, compatibility requirements, and non-goals.
- Existing experiment/evidence documents: scientific history and accepted empirical claims; this application plan does not replace or reinterpret them.

The original `_ai-review/LOCAL-APPLICATION-V1-ARCHITECTURE-PLAN.md` copy was the review handoff only and is not the tracked authority.

## 21. Final assessment

**GO:** the architecture is viable and fits the live repository.

The correct V1 is a Textual TUI over a small transport-independent session/application layer, not a web stack and not another scientific execution path.

The repository already demonstrates that real controller/PDB/patch/verifier execution and crash-durable lifecycle observation work. The bounded work is to:

1. make controller and verifier progress observable without changing their authority;
2. isolate live execution behind a cancellable worker process;
3. create a separate application journal and run manifest;
4. normalize live and recorded sources through one presentation reducer;
5. build replay-first and then enable deterministic live execution;
6. add configured command-model execution only after cleanup and compatibility gates pass.

The primary acceptance criterion is not that a TUI launches. It is that a user can observe a genuine bounded debugging session, understand what the controller and debugger are doing, inspect the diagnosis and patch lifecycle, see the independent verifier’s progress and final authority, cancel safely, and reopen the exact recorded session afterward without changing or misrepresenting scientific evidence.
