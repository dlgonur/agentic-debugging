# Task 10A — Real-model evaluation harness

Task 10A adds a separate, explicitly authorized live evaluation path. The
normal demo command, imports, tests, and golden trajectories remain offline
and deterministic.

## Live opt-in

The operator path is:

    python -m agentic_debugger.evaluation.live_cli --live --confirm-live-model-access --config C:\private\live-model.json --run-label smoke --task-id curated-none-handling-001 --policy static-baseline --policy pdb-on-uncertainty --repetitions 1 --max-model-requests 64 --max-controller-steps 64 --max-model-phase-seconds 900 --max-retries 2 --max-response-bytes 1048576 --output live-results.json --human-output live-results.txt

The external configuration file contains only schema version, model name,
argv command, request timeout, and tool version. The command receives one JSON
request on stdin and returns one JSON object on stdout. The harness launches it
without a shell and passes a minimal environment. The harness accepts no
credential field. Supported common credential-bearing argv forms, including
credential-named flags, key/value assignments, and Bearer/Basic values, are
rejected. A trusted local wrapper may use an external credential mechanism
outside this harness; the harness is not a universal secret detector or an OS
sandbox.

Both live selection and explicit confirmation are mandatory. Without them, the
CLI writes an attempted-but-rejected report without reading the config. Missing
or invalid configuration is rejected before any command is launched.

## Runtime and limits

Each case loads the curated manifest, creates a disposable workspace, runs the
actual controller and registered tools, records a redacted RunEvent trajectory,
and invokes the Task 7 verifier on the candidate patch. Cases are isolated and
cleanup runs on success and failure.

Supported limits are selected fixtures, policies, repetitions, model requests,
controller steps, model-phase seconds, retries, response bytes, and
stop/continue after task failure. max-model-phase-seconds bounds the model
adapter/transport phase for each case; it is not a whole-case deadline.
Tool, PDB, verifier, event, and cleanup phases retain their existing
component-specific bounds and are observed in case_elapsed_duration_ms.
model_phase_elapsed_duration_ms and model_transport_duration_ms accumulate only
the actual transport call wall-clock durations, including retries. Tool/PDB
work between requests and verifier/event/cleanup work after the final request
are excluded from model-phase timing and the max-model-phase-seconds budget.
Requests and retries are counted separately. Model stdout and stderr are
drained through bounded buffers; stdin writing and process wait share the
transport timeout. Command timeout, provider failure, controller rejection,
controller failure, verifier failure, event/reporting failure, interruption,
and cleanup result are retained separately.

The transport does not fabricate token counts. Prompt, completion, and total
tokens remain null with explicit missing-field markers unless the transport
reports them. Monetary cost is not calculated.

## Reporting semantics

Configured and rejected reports share schema version 1.0 and top-level identity
fields. Configured reports include a unique report/evaluation identity and
credential-free configuration metadata: model name, tool version, stable
configuration fingerprint, request timeout, protocol/schema versions, limits,
and continue-on-failure behavior. Reports include expected, started, completed, incomplete, and
unstarted case counts plus evaluation-root cleanup status. Each case has a
unique case_id, run_id, and trajectory_id derived from evaluation, task,
policy, and repetition. Case statuses distinguish resolved,
unresolved, controller failure or rejection, timeout, provider failure,
verifier failure, event/reporting failure, cleanup failure, harness failure,
and incomplete execution. Reports contain task, policy, repetition, model name,
controller and verifier outcomes, localization, request/response/retry/tool/
PDB counts, elapsed time, token availability, termination reason, and cleanup
evidence.

An interrupted run is marked interrupted; completed preceding cases and the
interrupted case are retained when possible. Unstarted cases are counted but
not represented as successful. The harness does not infer that PDB improves
performance. Static and PDB-on-uncertainty policies must use the same task,
repetition, and limit configuration before any comparison is attempted.

The model command receives a bounded JSON request containing protocol/version,
evaluation/case/run/trajectory identity, task context, policy, controller
state, allowed actions, state-specific action contracts, authoritative legal
transition targets, budget limits/state, hypotheses, last observation, and up
to 32 bounded history entries. The live wire protocol is version 1.1; version
1.0 must not be interpreted as having these fields or meanings. Each request
has a globally unique request ID plus explicit logical model-call and
transport-attempt indexes. A retry changes the transport-attempt index and
request ID while retaining one bounded history entry for the logical call.
The Reproduce state advertises only `run_reproduction.phase=baseline`, and
Validate advertises only `post_patch`. Enum-backed confidence and status
fields expose their accepted values. Tool rejections retain the
`dispatch_reason` and may include a bounded, redacted diagnostic; arbitrary
exception text is not forwarded.

The provider-neutral command convention is: a transport/process/network
failure may use a nonzero command exit; a provider completion with an invalid
directive must instead return exit 0 and a JSON object such as
`{"usage": {...}, "directive": {"kind": "not-a-directive"}}`. The harness
counts usage and the JSON response before parsing, classifies the directive as
`invalid_model_response`, and retries within budget. Nonzero exits remain
transport failures and are not treated as provider completions.

The CLI validates configured reports before writing either JSON or human output.
Duplicate task/policy selections are rejected before any case starts. Exit
status 0 means every
selected case resolved and the report is complete; 1 means a complete report
contains an unresolved or failed case, or any cleanup failure occurred; 2 means
live execution was rejected or configuration was invalid; 3 means the run is
partial or interrupted without cleanup failure. Cleanup failure therefore has
one contract: valid partial/failed report state and CLI exit 1.

## Current limitations

The transport is provider-neutral and command-based. This campaign does not
execute it. PDB-enabled cases prepare the accepted disposable runtime probe
from the curated fixture. Every case directory is created by the harness and
only an owned directory is removed; pre-existing operator directories remain
untouched. Hostile-code containment remains the trusted-local-workspace
boundary accepted by Task 7.

Task 10B-R1 completed the live protocol and accounting repair described above
(protocol version 1.1; accepted implementation/merge commit
`2996f16f7c95baf0860d0736d8ab67d13af60b9e`). A controlled live baseline run has
since executed via the private Task 10B live runner, which remains operator
tooling outside this repository; its evidence package and baseline verdict are
recorded in `docs/PROJECT_TRACKER.md` and are not restated here. In that run
the PDB-enabled case terminated before PDB was opened, so the run does not
measure PDB effectiveness and supports no claim that PDB is better or worse
than the static policy. The next source task is Task 10B-R3 — Invalid
Directive Retry Feedback v1.
