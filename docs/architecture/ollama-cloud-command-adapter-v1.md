# Local Application V1 — Ollama Cloud Command Adapter

**Status:** bounded implementation contract for one shared Ollama Cloud
adapter with accepted model profiles

**Accepted profiles:**

| Local Application / Ollama CLI alias | Upstream Cloud provenance | Role |
| --- | --- | --- |
| `gpt-oss:20b-cloud` | `gpt-oss:20b` | accepted product-runtime default |
| `nemotron-3-nano:30b-cloud` | `nemotron-3-nano:30b` | experimental candidate |

`--model` selects one accepted alias. The default remains
`gpt-oss:20b-cloud`. The OpenCode display name
`ollama-cloud/nemotron-3-nano:30b` is not an Ollama CLI identifier and is
rejected. Other Ollama Cloud models visible in OpenCode are not accepted.

**2026-08-17:** the first real remote product proof through this adapter is
COMPLETE — session `sess-20260817-103258-3d1193` (task
`curated-none-handling-001`, policy `pdb-on-uncertainty`) SUCCEEDED with the
independent verifier RESOLVED (F2P 1/1, P2P 2/2), cleanup verified, and
observed live/replay terminal-state parity. PDB was NOT EXERCISED in that
session. That proof used `gpt-oss:20b-cloud`. See `docs/project-tracker.md`
(Local Application V1 real remote route proof, 2026-08-17) for the full
record. The later multi-model generalization does not replace that proof.

**2026-08-18:** the experimental Nemotron profile has a completed
five-task Harness V2 capability probe (**1/5 RESOLVED**). That result is
closed evidence at
`experiments/nemotron_3_nano_model_capability_probe/`. It does not
replace the `gpt-oss:20b-cloud` product-runtime proof and is not a
matched five-task comparison.

This adapter is a provider-specific decision-model command for the existing
Local Application configured-command source. It does not replace or extend
the application controller, tool registry, PDB path, PatchManager, or
independent verifier.

## Runtime boundary

The configured profile launches
`scripts/ollama_cloud_command_adapter.py`. For every protocol-1.3 request,
the adapter establishes Cloud provenance from zero-inference local metadata
and then sends exactly one generation request to the signed-in local Ollama
daemon. Metadata and chat share the same adapter timeout budget.

```text
Local Application protocol 1.3
  -> Ollama Cloud command adapter
  -> GET  http://127.0.0.1:11434/api/tags
  -> POST http://127.0.0.1:11434/api/show
  -> POST http://127.0.0.1:11434/api/chat
  -> final assistant content
  -> strict JSON and request-bound directive validation
  -> existing LiveModelAdapter/controller/verifier path
```

The request uses the selected accepted Cloud alias as `model`, plus
`stream: false` and `think: "low"`; it does not send `format`, `tools`, or
function definitions. Chat messages use a `system` role for the stable
directive-schema contract and a `user` role for the bounded canonical public
request plus request-specific legal shapes. The system prompt states that the
top-level type field is always `kind` and gives the exact action,
transition, and hypothesis objects enforced by the adapter validator. The
user message derives illustrative legal action JSON from the current
`allowed_actions` and `action_contracts`; it does not grant provider tools.
When `apply_patch` is legal, that user guidance also states the exact
unified-diff form accepted by the existing PatchManager: both
`--- a/<path>` and `+++ b/<same-path>` headers; a complete numeric hunk
header `@@ -OLD_START,OLD_COUNT +NEW_START,NEW_COUNT @@` with 1-based
starts; mechanical count formulas
`OLD_COUNT = context(" ") + removed("-")` and
`NEW_COUNT = context(" ") + added("+")`; the rule that context lines
count toward both sides; a pre-output four-number checklist; and a
neutral arithmetic example whose header counts equal its body counts.
Zero-context hunks are valid when they uniquely locate the edit, so the
guidance prefers the smallest valid hunk. A rejected `apply_patch` does
not create an active patch; `revert_patch` and patch-dependent
`syntax_check` apply only after a successful apply. The adapter does
not normalize or repair malformed diffs.
The adapter has zero provider retry and zero fallback, and preserves the
25-logical-call and 25,000-byte public-request bounds.

Malformed provider JSON is not rewritten. Invented aliases such as
top-level `action`, `payload`, or `transition` remain invalid.

The adapter accepts only the intended `http://127.0.0.1/.../api` loopback
endpoint. It never reads, accepts, copies, persists, or emits an Ollama API
key. The local daemon performs signed-in Cloud authentication outside the
application's credential boundary.

## Response boundary

Cloud provenance is established from `/api/tags` and `/api/show` before any
generation call. Those metadata endpoints must map the selected local alias
to that alias's expected upstream model and Ollama Cloud host
`https://ollama.com`, and `/api/show` must report the same upstream value as
`details.parent_model`. For the accepted product default this is
`gpt-oss:20b-cloud` → `gpt-oss:20b`. For the experimental Nemotron candidate
this is `nemotron-3-nano:30b-cloud` → `nemotron-3-nano:30b`. Harmless default
HTTPS port or trailing slash differences on the metadata host are
normalized. Wrong or missing metadata fails closed and does not call
`/api/chat`.

The `/api/chat` response is not a provenance document. For a selected Cloud
alias it must have `model` exactly equal to that alias's expected upstream
model, be complete, contain a completed assistant message, and contain
bounded string `message.content`. Chat `remote_model` and `remote_host` are
not required and are not used; the installed Ollama 0.32.15 local Cloud
proxy omits them. Returning the local Cloud alias as `model` is
metadata/chat disagreement and fails.
Tool/function-call activity is rejected. `message.thinking` is discarded and
never enters adapter stdout, Local Application protocol data, events, history,
diagnostics, validation evidence, or review artifacts.

The complete content string must be one JSON object. Markdown fences, prose,
concatenated JSON values, malformed JSON, non-object JSON, ambiguous output,
and directives that do not satisfy the request's embedded action/transition
contracts are rejected. The existing `LiveModelAdapter` validates the
directive again downstream and remains authoritative.

## Zero-inference preflight

`--preflight` is an operator diagnostic, not the provenance authority for later
disposable adapter processes. It performs only local metadata checks:
`/api/version`, `/api/tags`, and `/api/show` for the exact model. It requires
the same alias-to-upstream mapping as the normal request path. It reports
local API readiness, expected version, model availability, readable metadata,
the validated provenance, and `provider_inference_started: false`. It does
not call `/api/chat` or `/api/generate` and does not establish that Cloud
inference will succeed. Each later adapter invocation revalidates `/api/tags`
and `/api/show` under the same request timeout before it may call `/api/chat`.

The current verified owner environment for this contract is Ollama `0.32.15`.
The accepted 2026-08-17 product-runtime proof used `0.32.14`; the exact pin is
kept explicit rather than relaxed across versions. The
accepted product default requires `gpt-oss:20b-cloud` available. The
experimental Nemotron profile requires `nemotron-3-nano:30b-cloud`
available. A version mismatch fails closed.

## Typed command failures

Successful adapter stdout remains the one-directive JSON envelope. On a
nonzero adapter exit, stderr contains exactly one bounded `command-error-v1`
JSON object with `schema_version`, a closed `kind`, and a provider-safe
message. The provider-neutral command transport accepts only that exact
schema and closed vocabulary, retains only the kind, and discards the
free-form message. Arbitrary configured-command stderr, malformed envelopes,
and credential-shaped output remain unrecorded and collapse to the generic
`process_error` kind. This preserves enough evidence to distinguish an HTTP
or metadata failure, a bounded request/response failure, and a
provider-completed invalid directive without persisting raw provider content
or thinking.

Local Application selects a model by launching the same adapter through a
`command-models-v1` profile whose `argv` includes `--model <accepted
alias>`. Omitting `--model` keeps the `gpt-oss:20b-cloud` default. Example
profiles:

```json
{
  "schema_version": "command-models-v1",
  "profiles": [
    {
      "profile_id": "ollama-cloud-gpt-oss-20b",
      "display_name": "Ollama Cloud GPT-OSS 20B",
      "executable": "python",
      "argv": [
        "C:\\path\\to\\scripts\\ollama_cloud_command_adapter.py",
        "--model",
        "gpt-oss:20b-cloud"
      ],
      "request_timeout_seconds": 60
    },
    {
      "profile_id": "ollama-cloud-nemotron-3-nano-30b",
      "display_name": "Ollama Cloud Nemotron 3 Nano 30B",
      "executable": "python",
      "argv": [
        "C:\\path\\to\\scripts\\ollama_cloud_command_adapter.py",
        "--model",
        "nemotron-3-nano:30b-cloud"
      ],
      "request_timeout_seconds": 60
    }
  ]
}
```

Model identity stays adapter-owned. The command-model config does not
invent a second provenance schema; it only passes the accepted `--model`
flag through the existing argv contract.

## Cancellation and ownership

The existing `CancellableJsonlCommandTransport` owns the adapter subprocess
boundary. Cancellation or timeout terminates the adapter and closes its HTTP
client resources promptly. The Ollama daemon is a persistent external service,
not a Local Application child process; Local Application does not kill it.
Closing the client connection therefore bounds the local adapter request but
does not claim to cancel computation already accepted by Ollama Cloud.
