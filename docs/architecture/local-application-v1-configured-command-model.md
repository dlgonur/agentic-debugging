# Local Application V1 — Configured Command-Model Execution (Task 8)

**Document type:** Accepted Task-8 implementation contract (active)
**Status:** Implemented; Task 8 is the final Local Application V1 milestone
**Authority:** `docs/architecture/local-application-v1.md` (phased plan);
this document records the Task-8 contract details and the finished V1
behavior.  It does not replace or duplicate the phased plan.

## 1. Purpose and boundary

Local Application V1 supports two genuine live execution modes through the
**same accepted application architecture**:

```text
Textual
   ↓
SessionSpec / application service
   ↓
SessionWorkerProcess
   ↓
production execution source
   ↓
DeterministicController
   ↓
model adapter
   ├── deterministic policy (DemoPolicyModel)
   └── configured command-model transport (LiveModelAdapter + JSON-lines)
   ↓
real tools / PDB / PatchManager
   ↓
independent verifier
   ↓
SessionEventEmitter → journal → live SessionViewState → HistoryStore → replay
```

1. **Deterministic / Offline** — the Task-7 production deterministic source
   (unchanged behavior).
2. **Configured Command Model** — a user-defined local command model
   executed through the **existing** JSON-lines command transport
   (``evaluation.live.JsonlCommandTransport`` protocol) and the accepted
   ``LiveModelAdapter`` controller contract.

The application remains a debugging-session application: no provider
marketplace, no remote model discovery, no account management, no API-key
vault, no arbitrary plugin framework, no shell terminal, no database.

## 2. The configured command-model path

Both sources run the **same shared execution pipeline**
(``application/local_source.run_local_session``): real controller, tool
registry, PDB, PatchManager, disposable task workspace, and independent
verifier, inside the accepted Task-3 worker process with one shared
``SessionEventEmitter`` (one contiguous durable journal).  The meaningful
difference between the two modes is **model construction**:

- the validated app-owned profile becomes a ``LiveModelConfig``;
- ``CancellableJsonlCommandTransport`` (``application/command_transport``)
  is the application variant of the accepted transport: identical wire
  protocol and error vocabulary, plus cooperative cancellation polling,
  explicit bounded environment/cwd, and tree-wide termination;
- ``LiveModelAdapter`` drives the same controller contract as every other
  supported model: directive validation and tool policy remain
  controller-owned, malformed model output is never reinterpreted as a
  valid directive, and the transport never mutates PatchManager/PDB/
  verifier directly.

The configured source adds two honest semantics on top of the pipeline:

- the independent verifier evaluates the candidate the configured model
  **actually applied**, and only when the controller completed with an
  applied patch; otherwise verification is skipped (nothing to verify
  honestly);
- a controller run that did not complete is a genuine session failure
  (``FAILED`` with ``model_error`` / ``directive_exhausted`` /
  ``controller_failed``), never an orderly completion.

## 3. Configuration contract

One bounded app-owned configuration location:
``<app-root>/config/command-models.json`` (the app root defaults to
``%LOCALAPPDATA%\AgenticDebugger`` on Windows, ``~/AgenticDebugger``
elsewhere; ``--root DIR`` overrides it).

```json
{
  "schema_version": "command-models-v1",
  "profiles": [
    {
      "profile_id": "local-dummy",
      "display_name": "Dummy command model",
      "executable": "C:\\path\\to\\python.exe",
      "argv": ["C:\\path\\to\\model.py", "--flag"],
      "cwd": null,
      "request_timeout_seconds": 60,
      "environment": {"MY_VAR": "value"},
      "protocol_version": "1.3",
      "tool_version": "live-command-v1"
    }
  ]
}
```

- ``profile_id`` — stable id ``[a-z0-9][a-z0-9._-]{0,63}`` (unique).
- ``display_name`` — safe non-empty label (bounded, credential-shaped
  values rejected).
- ``executable`` — a bare command name (resolved through ``PATH``) or a
  true absolute path (Windows drive-rooted ``C:\...``, UNC ``\\server\...``,
  or POSIX ``/...``).  Drive-relative Windows paths (``C:relative.exe``,
  ``C:..\evil.exe``) and relative paths with separators are rejected as
  ambiguous; the check uses correct ``ntpath``/``PureWindowsPath``
  semantics, not "has a drive letter == absolute".
- ``argv`` — explicit argument list (max 31 entries, bounded); combined
  with the executable the accepted 32-argument command cap applies.
- ``cwd`` — optional absolute working directory.
- ``request_timeout_seconds`` — 1..300.
- ``environment`` — optional bounded explicit overrides (max 8); the
  inherited process environment is never serialized into evidence.
- ``protocol_version`` — an explicit compatibility assertion only.  There
  is exactly one truthful protocol authority: the runtime wire protocol
  ``evaluation.live.LIVE_PROTOCOL_VERSION`` (currently ``1.3``).  A profile
  value that does not equal the actually supported runtime protocol is
  rejected fail-closed before any executable launch, so
  ``model.configured.protocol_version`` always equals the wire protocol
  used.  An omitted field defaults to the runtime constant.
- ``tool_version`` — bounded version metadata.

**Security rules (Task 8 Part A4/A5):**

- loaded with ``json.loads`` only — no YAML constructors, no Python config
  execution, no code evaluation of any kind;
- execution is always explicit ``argv`` with ``shell=False`` — no implicit
  ``cmd /c`` / PowerShell evaluation, no shell metacharacter
  interpretation (metacharacters in argv are inert literal arguments);
- credential-shaped values — argv tokens, environment overrides, display
  names, and paths — are **rejected at validation**, so no secret literal
  can be persisted into history, rendered in the UI, or serialized into a
  configuration fingerprint;
- **safe bounded diagnostics (Repair Pass 2)**: every
  ``CommandConfigError`` diagnostic is a safe structural message bounded to
  an explicit UTF-8 byte limit (``MAX_CONFIG_DIAGNOSTIC_BYTES``, consistent
  with the application's small bounded-diagnostic policy) at construction
  time, in one place.  Raw untrusted config values and keys are never
  echoed into a diagnostic — a malformed config must not leak secrets
  through error text that reaches the Start screen or the worker/session
  result — and a malformed config can never produce an oversized exception
  string.  Known-safe validated identifiers (a profile id that passed its
  own validation contract, safe fingerprints, counts/indexes) may be
  included;
- **authoritative bounded config read (Repair Pass 2)**: the configuration
  file is read through one authoritative bounded read (at most
  ``_MAX_CONFIG_FILE_BYTES + 1`` bytes).  A pre-read ``stat`` is never the
  size authority, so a file that grows between any pre-read observation and
  the read itself can never be read unbounded into memory; the actual bytes
  parsed are exactly the bytes whose size was bounded.  Missing-file
  semantics are unchanged (a file that disappears during load is the empty
  configuration); malformed UTF-8/JSON remains a safe bounded config error;
- **configuration TOCTOU pin**: a Start action pins the selected profile's
  safe configuration fingerprint into the worker launch parameters; the
  worker reloads the profile, recomputes the fingerprint, and fails closed
  (no executable launch, no side effect) when the loaded configuration no
  longer matches the pinned fingerprint.  The mismatch diagnostic carries
  only the safe profile id and the two fingerprints;
- **durable candidate-patch withholding**: the durable app-owned
  ``candidate.patch`` artifact is gated by the same shared
  credential-content policy the application evidence uses.  A patch whose
  body matches the policy is not persisted (never redacted into a fake
  original); truthful patch identity/hash/lifecycle remain available
  through the already-recorded safe patch events, and the history manifest
  only references artifacts that were actually written.  The in-memory
  candidate the independent verifier evaluates is unchanged by this gate;
- history/replay stores configuration **provenance and fingerprint**
  (``model.configured`` event: profile id, safe fingerprint, display
  label, protocol/tool version), never a live executable object;
- stderr/stdout of the command are bounded and never persisted; only
  bounded vocabulary diagnostics (``request_timeout``,
  ``provider_or_transport_error``, ``invalid_model_response``, …) reach
  session diagnostics.

**Network trust boundary (truthful):**

- **Deterministic / Offline** — application-controlled offline
  deterministic execution; no provider/network requirement.
- **Configured Command Model** — the application launches only the
  explicitly configured local command through the validated argv contract
  (``shell=False``); the application itself adds no provider SDK, account
  integration, or API-key vault.  The configured child process is
  **trusted user configuration**: V1 does **not** enforce child-process
  network isolation, and the child's capabilities are those available to
  that executable under the host OS.  The in-process ``OfflineGuard``
  covers only the application process itself.  Users who require network
  isolation for a configured command must provide it externally (OS
  sandbox, firewall, container).

## 4. Protocol expectations

The command model is invoked **once per controller model request** with
one JSON-lines request on stdin and must answer with one JSON object
directive on stdout (the accepted ``agentic-debugging-live-jsonl``
protocol, version ``1.3``; see ``evaluation/live.py``).  The response may
be a bare directive object or a ``{"usage": ..., "directive": ...}``
wrapper.  Bounded failures are handled honestly:

| Condition | Result |
|---|---|
| valid directive | controller validates and executes (or rejects) it |
| malformed JSON / extra noise / empty output | retried, then FAILED/model_error |
| non-zero exit / launch failure | retried, then FAILED/model_error |
| oversized stdout/stderr | FAILED/model_error (bounded) |
| request timeout | command tree terminated; FAILED/model_error (``request_timeout``) |
| cancellation | command tree terminated; CANCELLED after verified cleanup |

The controller remains the authority for directive acceptance and tool
policy; the configured transport cannot bypass either.

## 5. Cancellation, timeout, and process-tree safety

- Session cancellation flows through the accepted Task-3 token into the
  transport's request poll; the active command tree is terminated promptly
  and the session reports ``CANCELLED`` only after verified cleanup.
  Cancellation is never converted into a model error or a timeout.
- Cancellation is honored at **every** wait, including while the request
  writer is blocked on a full stdin pipe (a child that never reads stdin).
  The writer is joined in bounded slices that poll both the cancellation
  check and the request deadline, so an explicit user cancellation can
  never be masked into ``request_timeout`` by a blocked write.  On
  cancellation the command tree is terminated, the blocked writer is
  interrupted safely, and the writer/reader threads are joined boundedly.
- Request timeout terminates the command **tree** (Windows:
  ``taskkill /T /F`` — an explicit standard utility invocation, never a
  shell string; POSIX: the accepted process-group ladder) and is never
  converted into successful empty output.
- The command runs inside the session worker, which is assigned to the
  accepted Windows Job Object (``KILL_ON_JOB_CLOSE``): worker escalation
  and application exit terminate the worker and every command descendant.
- The session deadline (worker token) and the per-request timeout
  (profile) are distinct concepts.

## 6. UI behavior

- The Start screen offers the two modes; configured mode lists validated
  profiles with safe concise information (display name, id, timeout,
  fingerprint prefix).  Start is disabled with a clear reason when no
  valid profile exists; invalid configuration never crashes the TUI.
- The workspace header indicates the mode/source in text and shows the
  recorded model provenance label when present; all panes derive from
  ``SessionViewState`` as in every other mode.
- Expected configured-model failures (profile missing, executable not
  found, malformed protocol, non-zero exit, timeout, cancellation,
  bounded-output violation) are professional application states, never
  tracebacks.

## 7. History and replay

Configured sessions register into app-owned history like every other
session (``configured_model`` source kind, honest classification).
Reopening a configured session from history is pure read-only replay of
the durable journal: **the configured command is never re-run**.  The
journal's ``model.configured`` event lets history identify the selected
profile id, safe configuration fingerprint, display label, and
protocol/tool version without persisting any executable or secret.

## 8. V1 non-goals (unchanged)

Provider SDKs, provider authentication, provider marketplaces, API-key
managers, model download managers, GPU inference, Ollama integration,
arbitrary repository support, arbitrary shell terminals, IDE/editor,
manual PDB consoles, multi-agent support, browser UIs, cloud sync,
telemetry services, and databases.  Task 8 is configured **command-model**
execution, not provider integration.
