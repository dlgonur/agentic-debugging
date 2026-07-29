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
transition targets, budget limits/state, hypotheses, last observation, a
`directive_feedback` field, and up to 32 bounded history entries. The live
wire protocol is version 1.2; version 1.1 and earlier must not be interpreted
as having the `directive_feedback` field or its meaning. Each request has a
globally unique request ID plus explicit logical model-call and
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

### Invalid-directive retry feedback (protocol 1.2)

A directive is "provider-completed" when the transport call itself succeeded
(no `LiveTransportError`, no nonzero command exit); the harness then attempts
to interpret whatever the provider returned as a directive. The
`directive_feedback` key is structurally present in every request; on the
first transport attempt for a logical model call its value is `null`, not
absent. If that attempt's directive is rejected, every field the harness uses
to classify it comes from the harness's own closed vocabulary — never from
raw provider text — so a non-null `directive_feedback` on a retry is safe to
forward: `{"category": <one of the five rejection categories below>,
"message": <a bounded, pre-authored string identifying the specific problem>,
"rejected_transport_attempt": <the 1-based attempt that was rejected>}`. The
five rejection categories are `illegal_action` (a real action name not legal
in the current controller state), `illegal_transition` (a real target state
not reachable from the current state), `invalid_argument_value` (a value that
fails a declared argument contract, e.g. an out-of-enum
`run_reproduction.phase` or hypothesis `confidence`/`status`),
`malformed_directive` (an unrecognized or missing `kind`, missing required
fields, or a response body that is not a JSON object), and
`ambiguous_response_envelope` (a response that mixes both wire conventions at
once — a top-level `kind` alongside a nested `directive` — so the harness
cannot tell which one is meant and refuses to guess). The harness never
invents, rewrites, or silently substitutes a legal directive on the model's
behalf; `directive_feedback` only explains the rejection, and the
`allowed_actions`, `legal_transition_targets`, and `action_contracts` already
present in the same request remain the sole authoritative legal-directive
contract. `directive_feedback` reflects only the immediately preceding
attempt: a `LiveTransportError` on any attempt clears pending feedback, so a
transport or provider command/process failure is never described as an
invalid directive, and a directive rejection is never carried across a
transport failure into a later attempt. Repeating a rejected directive after
receiving feedback keeps counting as a normal retry and still terminates as
`invalid_model_response` once `max_retries` is exhausted, exactly as before
this repair.

Directive parsing enforces exactly the fields `LIVE_DIRECTIVE_SCHEMA` already
advertises as required, for every directive kind, including
`add_hypothesis`/`revise_hypothesis`: `evidence_refs` and
`requires_runtime_evidence` must both be present or the directive is rejected
as `malformed_directive`, naming the missing field. `evidence_refs` must be a
genuine JSON array; a string, object, number, boolean, or `null` in that
position is rejected as `malformed_directive` rather than being reinterpreted
— a JSON string is never iterated into single-character references, and a
JSON object is never iterated into its keys. This mirrors the same
never-invent/never-substitute rule the retry-feedback contract itself
follows: the harness explains why a directive was rejected, it does not
silently reshape the directive into something the provider did not actually
send.

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

Task 10B-R1 completed the initial live protocol and accounting repair
(protocol version 1.1; accepted implementation/merge commit
`2996f16f7c95baf0860d0736d8ab67d13af60b9e`). Task 10B-R3 — Invalid Directive
Retry Feedback v1 then added the bounded `directive_feedback` contract
described above (protocol version 1.2; accepted implementation/merge commit
`1bb1d5251cc732f331ce2f5fdd163d9e46309d29`). The R3 implementation campaign
itself used only local deterministic transports and made no live provider
call.

Subsequent live work executed through private operator tooling outside this
repository. A minimal retry-recovery diagnostic directly observed both a
legal recovery after corrective feedback and a later failed recovery in the
same case. The case still terminated with `invalid_model_response`, did not
reach patch verification, and never opened PDB.

A later locked four-case descriptive matrix used fixture
`curated-none-handling-001`, OpenCode Zen provider ID `opencode`, model ID
`deepseek-v4-flash-free`, variant `max`, two repetitions of
`static-baseline`, and two repetitions of `pdb-on-uncertainty`. Static policy
resolved 2/2 cases. PDB-on-uncertainty resolved 0/2; both cases terminated
with underlying reason `invalid_model_response` before PDB opened. Across the
matrix, 4 of 6 observed corrective-feedback episodes produced a legal next
directive, while 2 remained invalid.

These results are small, descriptive, fixture-specific, model-specific, and
provider-route-specific. They do not establish that protocol 1.2 caused a
higher success rate, that corrective feedback is generally reliable, or that
one policy is superior. Because neither PDB-enabled case opened PDB, the
matrix still does not measure PDB effectiveness. The historical OpenCode Go
baseline and the later OpenCode Zen free-model matrix must not be pooled as
one provider population. Exact evidence hashes, accounting totals, and
qualified conclusions are recorded in `docs/PROJECT_TRACKER.md`.
