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

## Frozen Level-32 escalation evidence (2026-08-22)

The explicitly authorized escalation used the existing one-task operator on
`audreyr__cookiecutter-967` with the same public contract and exact-PDB proof
requirements for four distinct Ollama Cloud aliases. GPT-OSS 120B completed
the product path but failed the official Docker authority (F2P 0/5; P2P 9/9).
Kimi K2.6 exposed two bounded transport defects before a final invalid-
directive incompatibility; Qwen3.5 exhausted the frozen two-patch budget and
failed official F2P 0/5. Gemma4 31B V2 completed the required PDB observations
but its otherwise-valid `revise_hypothesis` object was rejected because the
provider wrapped it in one exact Markdown JSON fence. A later forensic review
therefore preserves runtime `MODEL_DIRECTIVE_REJECTED` while scientifically
reclassifying V2 as `PARSER_COMPATIBILITY_FAILURE`; it is not a clean semantic
model failure. No model had solved this frozen rung at that historical
boundary. This is descriptive one-task evidence, not a model ranking, success
rate, or PDB causal claim. See
`experiments/pdb_capability_ladder/` and the detailed `_ai-review` report.

The provider-neutral follow-up is `directive-normalization-v2` /
`redundant-trailing-brace-v1-or-exact-json-markdown-fence-v1`. Strict JSON is
still first. The added branch accepts only a full lowercase `json`, LF-delimited
fence containing one strict top-level Mapping, with optional outer JSON
whitespace and no prose, second value, semantic repair, composed normalization,
retry, feedback, or reserialization. The same canonical semantic parser then
validates the recovered Mapping. This receiver-policy change alters both the
configured transport fingerprint and Level-32 treatment fingerprint.

The follow-up provider-neutral breakpoint policy is versioned as
`pdb-breakpoint-selection-v1` / `model-selected-runtime-validated-v1`. The
model-visible `start_pdb_session.breakpoint_line` is a positive integer with no
source-window enum or value rewrite. The PDB tool validates the requested line
against the production file and focus function; only a valid pause creates
proof evidence. A runtime-invalid positive line is therefore an ordinary typed
tool outcome, so the controller can issue a corrected start in the same bounded
run. If a later control exits before producing admissible proof, exact-public
mode releases that session and may start a fresh cycle within the same PDB
budget; non-proof interactive pilots retain their one-session limit. The runtime
probe/source-window anchor remains distinct from the model-selected breakpoint,
and the proof contract records the actual validated line.

## Runtime and limits

Each case loads the curated manifest, creates a disposable workspace, runs the
actual controller and registered tools, records a redacted RunEvent trajectory,
and invokes the Task 7 verifier on the candidate patch. Cases are isolated and
cleanup runs on success and failure.

Supported limits are selected fixtures, policies, repetitions, model requests,
controller steps, model-phase seconds, retries, response bytes, and
stop/continue after task failure. `request_timeout_seconds` is an inactivity
watchdog: stdout or stderr activity refreshes it. A streaming adapter can
therefore report progress without granting that progress channel directive
authority. `max-model-phase-seconds` is a broad cumulative emergency guard
checked before starting each request; it does not terminate an actively
progressing response and is not a whole-case deadline.
Tool, PDB, verifier, event, and cleanup phases retain their existing
component-specific bounds and are observed in case_elapsed_duration_ms.
model_phase_elapsed_duration_ms and model_transport_duration_ms accumulate only
the actual transport call wall-clock durations, including retries. Tool/PDB
work between requests and verifier/event/cleanup work after the final request
are excluded from model-phase timing and the max-model-phase-seconds budget.
Requests and retries are counted separately. Model stdout and stderr are
drained through bounded buffers; stdin writing and process wait share the
inactivity watchdog. Command timeout, provider failure, controller rejection,
controller failure, verifier failure, event/reporting failure, interruption,
and cleanup result are retained separately.

A nonzero configured-command exit is generic `process_error` unless stderr is
one strict `command-error-v1` JSON object with an accepted closed error kind.
Only that kind is retained in measurements; the message and all arbitrary raw
stderr are discarded. Provider-specific adapters may therefore expose safe
typed failure evidence without making configured-command stderr a report or
prompt data channel.

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
PDB counts, elapsed time, token availability, termination reason, cleanup
evidence, and—when supplied by a streaming adapter—aggregate frame, thinking
byte, and action-content byte counts. Reasoning text is not retained.

An interrupted run is marked interrupted; completed preceding cases and the
interrupted case are retained when possible. Unstarted cases are counted but
not represented as successful. The harness does not infer that PDB improves
performance. Static and PDB-on-uncertainty policies must use the same task,
repetition, and limit configuration before any comparison is attempted.

The model command receives a bounded JSON request containing protocol/version,
evaluation/case/run/trajectory identity, task context, policy, controller
state, effective allowed actions, state-specific action contracts, authoritative legal
transition targets, budget limits/state, hypotheses, last observation, a
`directive_feedback` field, and up to 32 bounded history entries. The current
live wire protocol is version 1.3. Protocol 1.2 introduced the bounded
`directive_feedback` field; historical 1.2 evidence must not be interpreted as
advertising the current 1.3 effective-contract semantics. Each request has a
globally unique request ID plus explicit logical model-call and
transport-attempt indexes. A retry changes the transport-attempt index and
request ID while retaining one bounded history entry for the logical call.
The Reproduce state advertises only `run_reproduction.phase=baseline`, and
Validate advertises only `post_patch`. Enum-backed confidence and status
fields expose their accepted values. Tool rejections retain the
`dispatch_reason` and may include a bounded, redacted diagnostic; arbitrary
exception text is not forwarded.

The effective action contract is authoritative for each request. Every
protocol-1.3 request is built from the exact `ToolRegistry` supplied to both
the live adapter and deterministic controller; there is no registry-less
contract fallback. Its action names are the intersection of the
controller-state allowlist, the actual registered live tool names, policy
availability, and the current PDB session lifecycle. Each registered live tool declares the argument contract used by
its validator, including required/optional fields, exact types, non-empty
string and non-negative integer constraints, enum values, and rejection of
additional properties; unregistered controller actions are not advertised. Before a
PDB session starts, stack/frame/evaluation/stop actions are absent. While a
session is active, `start_pdb_session` is absent and only lifecycle-valid
observations and stop operations are exposed. When the remaining PDB
observation budget is zero, all PDB observation-consuming actions disappear
from the contract; an active session still exposes `stop_pdb_session` for
cleanup. Directive kinds are likewise
state-scoped: hypothesis add/revise/status directives appear only where the
controller can apply them.

The opt-in exact-PDB proof adds a narrower `proof_gate` object only to public
requests in `RuntimeEvidence`; it is absent in other controller states so it
cannot conflict with their legal action surface. Exact `Understand` requires
the bounded full source observation before the runtime hypothesis. After the
transition, `next_required_actions` exposes only the next RuntimeEvidence
surface: start, stack, locals, next, then stop. Exact proof uses `next` rather
than `step` so a call expression cannot descend out of the declared production
frame and invalidate otherwise genuine runtime evidence.
`continue_pdb_session` is not part of this proof.
The exact proof also accepts one unique baseline reproduction: after it is
recorded, reproduction and optional failure-trace actions disappear and
`Understand` is the sole legal transition, preserving test/PDB budget for the
required proof and validation lifecycle.
Within `Understand`, the current request narrows the directive kind by phase:
the pre-PDB hypothesis must set `requires_runtime_evidence=true`; after the
debugger lifecycle, the same active hypothesis is revised with
`requires_runtime_evidence=false` and current observation IDs before the bound
diagnosis action appears. Runtime argument examples are derived only from
already-visible observations: the current stack frame and pause generation for
locals, followed by the exact evidence IDs and one observed-local example for
diagnosis. The source-window line contract is one-based, matching its handler,
and the exact small fixture exposes the complete target function for breakpoint
selection.

The 2026-08-21 authorized single-task run demonstrated this path end to end on
`pdb-required-boundary-006` with Ollama Cloud `gpt-oss:20b-cloud`: 21 logical
calls/transport attempts, zero retries/provider errors, PDB
start/stack/locals/next/stop, an evidence-bound diagnosis, and a one-hunk model
patch. The verifier completed RESOLVED with F2P 1/1 and P2P 1/1; cleanup and
canonical immutability were true; replay ended in Done. This is a bounded
one-task proof, not a success-rate or causal-comparison result.

The next single-task ladder rung used the same model on
`pdb-required-caller-callee-007` under the activity-aware streaming contract.
It completed in 22 logical calls/attempts with zero retries/provider errors,
three successful and zero failed PDB observations, and independent-verifier
`RESOLVED` (F2P 1/1, P2P 2/2, verifier-only private checks true). The 40,168
stream frames and 181,326 thinking bytes were progress telemetry only; no
thinking text entered directive authority or persisted evidence. Frozen raw
evidence is in `experiments/pdb_capability_ladder/level12-gpt-oss-v1/`.
The subsequent 18/100 single-task rung,
`pdb-required-multistage-units-008`, also completed `RESOLVED`: 21 logical
calls/attempts, zero retries/provider errors, three successful and zero failed
PDB observations, F2P 1/1, P2P 2/2, verifier-only private checks true, cleanup
and canonical immutability true, and 54-event replay terminal `Done`. Its
26,821 frames and 121,411 thinking bytes remained non-authoritative activity.
Frozen evidence is in
`experiments/pdb_capability_ladder/level18-gpt-oss-v1/`.

The frozen 32/100 SWE-rebench V2 rung, `audreyr__cookiecutter-967`, produced
the first valid failure boundary. V1 and V2 stopped on provider-context and
proof-binding harness defects and are not model results. V3 completed 24
provider calls with zero retries/errors, the exact PDB lifecycle, an
evidence-bound diagnosis, model patch, controller `Done`, and local verifier
`RESOLVED`. The official pinned Docker evaluator rejected the candidate (F2P
0/5; P2P not passed 9/9). The raw model diff required only terminal-newline
serialization normalization for Git; after that normalization the official
failure was an ordinary candidate failure. Hidden identities and patches were
never model-visible, and the task was not changed after the hidden outcome.
`Understand` remains unavailable until the unique successful pre-diagnosis
observations satisfy the same paused-production-frame checks as the
authoritative patch gate and the session has stopped. A rejected debugger
action is retained in the event stream but is not counted as proof, so a
corrected action can proceed without turning the earlier failure into evidence.
An execution-control observation that pauses outside the declared production
frame is retained but not selected as proof. If execution exits, the event is
retained, the tool session/workspace is released, and the selected runtime
cycle is reset; the next bounded request may start a fresh exact-public PDB
session and recollect stack, locals, and control evidence within the unchanged
controller PDB budget. Non-proof interactive pilots remain one-session cases.

`RuntimeEvidence` is not a model-selected shortcut. The live adapter calls the
accepted `decide_pdb_access` policy function before permitting a transition
into that state. The static policy always denies it. The uncertainty policy
requires a reproduced failure, remaining PDB budget, an active hypothesis,
and either low confidence or `requires_runtime_evidence=true`. The same
decision is used for the request's legal transition targets, so a provider
cannot bypass the gate by returning a hidden transition. Protocol 1.3 is the
R5 wire version: it introduces policy-scoped effective directive contracts,
state-scoped directive schemas, nested validator-derived argument contracts,
and machine-enforced PDB transition availability. It preserves protocol-1.2
request IDs, accounting fields, feedback categories, and other compatible
fields. Historical protocol-1.2 evidence remains historical and is not
relabelled.

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
five rejection categories are `illegal_action` (a recognized action name or
directive kind that is illegal in the current controller state, including
state-illegal `add_hypothesis`, `revise_hypothesis`, or
`set_hypothesis_status` directives), `illegal_transition` (a real target state
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
qualified conclusions are recorded in `docs/project-tracker.md`.
