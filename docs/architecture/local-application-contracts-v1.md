# Local Application V1 — Task 1 Application Contracts

**Document type:** Accepted Task-1 contract reference (stable)  
**Status:** Accepted implementation candidate for Task 1 of the V1 roadmap  
**Authority:** `docs/architecture/local-application-v1.md` (phased plan); this
document records the accepted Task-1 contract details only and does not
replace or duplicate it.  
**Scope:** UI-independent `agentic_debugger.application` package.

## 1. Purpose and boundary

Task 1 establishes the application/session boundary over the existing
scientific core.  The package:

- defines session specification, validated identity, lifecycle/status/
  termination contracts;
- defines the separate versioned application-owned `SessionEvent` model and
  its safe-data rules;
- defines execution-source (live versus replay) contracts;
- defines immutable presentation state and a pure event reducer;
- is independent of Textual and never executes or mutates controller, PDB,
  patch, verifier, demo, live-model, GPU, or experiment behavior.

Canonical `RunEvent` 1.0 semantics, trajectory ordering/IDs,
`project_controller_run()`, replay rules, golden trajectories, and semantic
projections are untouched.  There is no incremental canonical-RunEvent work
in Task 1.  Durable journaling, worker supervision, manifests/history, and
the UI are later roadmap tasks; only the minimum source/sink protocols are
fixed here.

## 2. Module map and dependency order

```text
agentic_debugger.application
    events.py         vocabulary + SessionEvent model (bottom layer)
    sources.py        execution-source contracts
    session.py        session specification/identity/status/result contracts
    presentation.py   immutable view state + pure reducer
```

Dependency rule: `events.py` imports only lightweight existing enums and
taxonomies for validation (`ControllerState`, `ObservationStatus`,
`SemanticOutcome`, `EvaluationStatus`).  No module imports controller,
verifier, PDB, patch, demo, live-model, GPU, or experiment execution code.
`events.py` owns the versioned lifecycle vocabulary because it is the event
payload vocabulary and the single-source payload validators; `session.py`
re-exports it for the session contracts.

## 3. Versioned vocabulary

### 3.1 Source kinds (`SourceKind`)

`offline_demo`, `configured_model` — live-startable.  `session_bundle`,
`canonical_trajectory`, `experiment_evidence` — recorded/replay-only
(`recorded == True`).  `can_start_new_session(kind)` returns False for
recorded kinds: replay material never enters the live-start workflow.

### 3.2 Session lifecycle (`SessionStatus`)

```text
CREATED -> STARTING -> RUNNING -> SUCCEEDED | UNRESOLVED | FAILED
                                | CANCELLED | TIMED_OUT | INTERRUPTED
                                | CLEANUP_FAILED
```

- `SUCCEEDED`: orderly end-to-end completion (cleanup verified); the
  verifier may still report an unsuccessful repair.
- `UNRESOLVED`: orderly completion without a resolved repair.
- `FAILED`: unexpected execution/harness/journal failure.
- `CANCELLED`, `TIMED_OUT`, `INTERRUPTED`, `CLEANUP_FAILED`: distinct honest
  terminals (cancel with verified cleanup; timeout; crash/abrupt journal
  end; leftover-state diagnostic).

`RUNNING` substate: `SessionPhase` ∈ {`waiting_model`, `executing_tool`,
`pdb_paused`, `verifying`, `cleaning`}.  Phases are only valid while
`RUNNING`.

### 3.3 Termination taxonomy (`SessionTerminationReason`)

`done`, `unresolved`, `model_error`, `directive_exhausted`,
`controller_failed`, `pdb_error`, `subprocess_error`, `verifier_error`,
`journal_error`, `timeout`, `cancelled`, `interrupted`, `cleanup_failed`.

Rules: verifier errors never infer a correctness verdict; journal failures
preserve already-produced scientific artifacts; cleanup failure is its own
terminal.  `compatible_reasons(status)` and `terminal_status_for(reason)`
pin the exact status↔reason mapping.  The transition map
(`allowed_transitions()`, `can_transition()`) allows exactly one terminal
event per complete stream.

## 4. `SessionEvent` model

Schema version: `session-event-v1` (namespaced; distinct from `RunEvent`
1.0).  Envelope fields:

```text
schema_version, session_id, task_id, run_id, sequence,
timestamp_utc, source_kind, event_kind, controller_phase, payload
```

- `sequence`: authoritative ordering, contiguous from 0.
- `timestamp_utc`: informational; strict ISO-8601 UTC (Z or +00:00).
- `run_id`: null until `session.started` binds the underlying run and
  therefore is the accepted started indicator for cleanup semantics.
- `controller_phase`: nullable validated `ControllerState` value.
- `payload`: bounded per-kind mapping; unknown fields rejected; non-finite
  floats rejected; JSON-compatible detached copies only.

**Construction and immutability (Repair Pass 1):** `SessionEvent` is a
frozen dataclass whose `__post_init__` re-validates every field through the
same strict rules as `from_mapping`, so no public construction path can
produce an invalid event.  The payload is canonicalized into a frozen
nested JSON structure (tuple-backed mapping/sequence with no mutating
protocol, mirroring the `events/replay.py` pattern): caller-owned input
mappings are never shared, `from_mapping()` is strict and detached, and
`to_mapping()` returns fresh plain JSON-compatible data whose mutation
cannot change the event.  `payload` behaves as a read-only mapping
(`Mapping[str, Any]`).

Event kinds (29, architecture §8.1): `session.created`, `session.started`,
`session.status_changed`, `session.cancel_requested`, `session.completed`,
`session.failed`, `session.cancelled`, `controller.step`,
`model.request_started`, `model.request_completed`,
`model.directive_accepted`, `model.directive_rejected`, `tool.started`,
`tool.completed`, `debugger.started`, `debugger.location_changed`,
`debugger.stack_observed`, `debugger.locals_observed`, `patch.proposed`,
`patch.rejected`, `patch.applied`, `patch.reverted`, `verifier.started`,
`verifier.stage_started`, `verifier.stage_completed`, `verifier.completed`,
`cleanup.started`, `cleanup.completed`, `artifact.written`.

Terminal kinds carry the exact terminal status: `completed` ∈
{`succeeded`, `unresolved`}; `failed` ∈ {`failed`, `timed_out`,
`interrupted`, `cleanup_failed`}; `cancelled` = `cancelled`.
`status_changed` carries only `running` + a phase.

### 4.1 Payload contracts (summary)

- lifecycle: created→`spec_fingerprint`; started/cancel_requested/verifier.
  started/cleanup.started→empty; status_changed→`status`+`phase`; terminal
  events→`status`+`termination_reason`; cleanup.completed→`verified`.
- controller.step→`step_index` + nullable `directive_kind`/`stop_reason`.
- model.request_*→`request_index` (+`status` ∈ ok/error/timeout);
  directive_accepted→nullable kind/action/target;
  directive_rejected→nullable kind + `rejection_category`.
- tool.started/completed→`tool_name` (+`status` ∈ ObservationStatus
  vocabulary).
- debugger.started→nullable `script` + bounded `breakpoints`;
  location_changed→nullable script/line/function + `pause_generation`;
  stack_observed/locals_observed→`pause_generation` + bounded
  frames/locals records.
- patch.proposed→`attempt_index`+`patch_sha256`; rejected→+`rejection_reason`;
  applied→+`changed_files`+nullable `syntax_passed`; reverted→`attempt_index`.
- verifier.stage_*→`stage` (+`status` ∈ running/completed/failed/skipped/
  cancelled); verifier.completed→nullable `status` (EvaluationStatus
  vocabulary), `outcome` (SemanticOutcome), F2P/P2P counts,
  `workspace_cleaned`.
- artifact.written→`path`+`sha256`.

### 4.2 Safe-data rules

Event payloads may carry only bounded data already safe for model/tool
observation.  All text fields are UTF-8-byte-bounded with explicit limits;
truncation is producer-side with the marker inside the limit.  Rejected
fail-closed: credential-shaped values (accepted `live.py` key=value/bearer
policy), control characters, oversized values, unknown fields, non-finite
floats, oversized frame/local/breakpoint/file lists.  Raw prompts, raw
model output, unbounded stdout/stderr, hidden tests, oracle information,
and redacted paths are never event payloads.

### 4.3 Complete-stream contract

`validate_session_event_stream(events)` enforces: non-empty; first event
`session.created`; contiguous sequences; constant session/task/source
identity; at most one `session.started` binding a constant `run_id` (none
before); legal lifecycle transitions; at most one `cancel_requested` before
the terminal; exactly one terminal event in terminal position.

**Cleanup lifecycle (Repair Pass 2):** cleanup follows one deterministic
single-cycle lifecycle.  `cleanup.completed` must follow an active
`cleanup.started`; a new `cleanup.started` may not begin while one is
active.  `session.completed`, and `session.cancelled` for a started
session, require the terminal cleanup cycle to be completed with
`verified=True` — an earlier verified cleanup never authorizes completion
while a later cleanup cycle is incomplete.  `cleanup_failed` is an honest
distinct terminal: it requires an attempted cleanup (`cleanup.started`)
that did not end verified.  Incomplete (crash-interrupted) journals are a
later history concern, not this validator.

## 5. Session contracts

- `SessionId`: actually validated immutable value object
  (`[a-z0-9][a-z0-9._-]{0,127}`); construction is the validated factory.
- `SessionSpec`: immutable request — `task_id`, `source`
  (`ExecutionSourceSpec`), `budgets` (`SessionBudgets`: optional positive
  model-call/controller-step/elapsed limits), `artifact_destination`.
  `fingerprint()` is the stable canonical SHA-256.
- `SessionSnapshot`: immutable service-facing state (identity, spec,
  status, phase, run_id, timestamps, sequence, reason); phase only while
  RUNNING; terminal requires a compatible reason.
- `SessionResult`: immutable terminal result — operational completion only,
  never a correctness verdict; status terminal, reason compatible,
  bounded diagnostics.  Cleanup semantics are aligned with the
  complete-stream contract: `run_id` is the accepted started indicator.
  `SUCCEEDED`/`UNRESOLVED` require a `run_id` and `cleanup_verified=True`;
  `CANCELLED` requires `cleanup_verified=True` when the session started and
  represents a pre-start cancel (nothing cleaned) when `run_id` is null;
  `CLEANUP_FAILED` never claims `cleanup_verified=True`;
  `FAILED`/`TIMED_OUT`/`INTERRUPTED` are unconstrained.  These rules never
  infer scientific correctness.
- `SessionController` protocol: `start(spec) -> SessionId`,
  `cancel(session_id)`, `snapshot(session_id) -> SessionSnapshot`.
  The concrete service/worker supervision is a later task.

## 6. Execution-source contracts

- `ExecutionSourceSpec`: `kind` (live-startable only), `task_id`, `policy`,
  `model_config_ref` (required for `configured_model`, forbidden for
  `offline_demo`, credential-shaped values rejected).
- `SessionEventSource` (runtime-checkable protocol): `source_kind`,
  `next_event() -> SessionEvent | None` in contiguous sequence order,
  `close()`.  Live sources are incremental; replay sources are read-only
  and never invoke tools/PDB/patch/model/verifier.
- `SessionEventSink` (runtime-checkable protocol): `append`, `flush`,
  `close`; implementations enforce contiguity and constant identity.
  Durable journal writers/recovery are later tasks.

## 7. Presentation contract

`SessionViewState` is immutable and derived only from events: identity,
status, phase, controller phase, termination reason, debugger view,
normalized patch attempts, verifier stages/summary, cleanup status, bounded
timeline (tail cap 2000 entries, summaries ≤ 240 chars).

**Identity and provenance (Repair Pass 2):** presentation is initialized
with a `PresentationIdentity` (`task_id`, `source_kind`, optional
`session_id`) through the single general initializer
`initial_session_view(identity)`.  The live path derives the identity from
a `SessionSpec` via `presentation_identity(spec)`; recorded/replay paths
derive it from the recorded material, so recorded source kinds have an
explicit supported initialization path while `ExecutionSourceSpec` still
rejects them for new live sessions.  Once identity is bound (session id
binds from the identity or from the first reduced event), any event whose
`task_id`, `source_kind`, or `session_id` mismatches the view fails closed
instead of silently reducing into a wrong-provenance view.

`reduce_event(state, event)` is pure: no I/O, no mutation, fail-closed on
unknown kinds, illegal transitions, and identity mismatches.  Live and
replay feed the same reducer (prefix parity by construction).  Rules:

- stale stack/locals observations cannot replace newer-pause data
  (`pause_generation` guard); location comes from the latest
  location-bearing event;
- patch attempts accumulate across their lifecycle events; an applied
  attempt becomes `verified` only when `verifier.completed` reports
  `COMPLETED` — application ≠ correctness;
- absent historical data stays `None`/empty (the `NOT RECORDED` display
  rule); UI-owned selection/scroll/filter/replay-cursor state is not part
  of view state.

## 8. Compatibility boundary

No existing module is modified by Task 1.  Importing the application
package requires no Textual and loads no controller/verifier/demo
execution path.  Existing CLI behavior, `RunEvent` 1.0, canonical
trajectories, replay, golden fixtures, semantic projections, verifier
taxonomy, and offline-default behavior are unchanged.

## 9. Versioning

`session-event-v1` is the accepted Task-1 schema.  Later tasks may make
bounded additive revisions when real producer data is established; any
revision is a reviewed schema change, never a silent mutation.
