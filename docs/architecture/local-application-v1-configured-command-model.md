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
- ``executable`` — a bare command name (resolved through ``PATH``) or an
  absolute path.  Relative paths with separators are rejected as
  ambiguous.
- ``argv`` — explicit argument list (max 31 entries, bounded); combined
  with the executable the accepted 32-argument command cap applies.
- ``cwd`` — optional absolute working directory.
- ``request_timeout_seconds`` — 1..300.
- ``environment`` — optional bounded explicit overrides (max 8); the
  inherited process environment is never serialized into evidence.
- ``protocol_version`` / ``tool_version`` — bounded version metadata.

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
- history/replay stores configuration **provenance and fingerprint**
  (``model.configured`` event: profile id, safe fingerprint, display
  label, protocol/tool version), never a live executable object;
- stderr/stdout of the command are bounded and never persisted; only
  bounded vocabulary diagnostics (``request_timeout``,
  ``provider_or_transport_error``, ``invalid_model_response``, …) reach
  session diagnostics.

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
