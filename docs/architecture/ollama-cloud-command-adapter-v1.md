# Local Application V1 — Ollama Cloud Command Adapter

**Status:** bounded implementation contract for `gpt-oss:20b-cloud`

This adapter is a provider-specific decision-model command for the existing
Local Application configured-command source. It does not replace or extend
the application controller, tool registry, PDB path, PatchManager, or
independent verifier.

## Runtime boundary

The configured profile launches
`scripts/ollama_cloud_command_adapter.py`. For every protocol-1.3 request,
the adapter sends exactly one request to the signed-in local Ollama daemon:

```text
Local Application protocol 1.3
  -> Ollama Cloud command adapter
  -> http://127.0.0.1:11434/api/chat
  -> final assistant content
  -> strict JSON and request-bound directive validation
  -> existing LiveModelAdapter/controller/verifier path
```

The only accepted model is `gpt-oss:20b-cloud`. The request uses
`stream: false` and `think: "low"`; it does not send `format`, `tools`, or
function definitions. The adapter has zero provider retry and zero fallback,
and preserves the 25-logical-call and 25,000-byte public-request bounds.

The adapter accepts only the intended `http://127.0.0.1/.../api` loopback
endpoint. It never reads, accepts, copies, persists, or emits an Ollama API
key. The local daemon performs signed-in Cloud authentication outside the
application's credential boundary.

## Response boundary

The response must identify the exact configured model or its explicitly
validated Cloud upstream identity, be complete, contain a completed assistant
message, and contain bounded string `message.content`. For the Cloud form,
`model` must be exactly `gpt-oss:20b`, `remote_model` must also be exactly
`gpt-oss:20b`, and `remote_host` must identify exactly `https://ollama.com`
(with only harmless default HTTPS port/trailing-slash representation
differences normalized). The pinned local alias form is accepted only with
the same exact remote provenance. Tool/function-call activity is rejected.
`message.thinking` is discarded and never enters adapter stdout, Local
Application protocol data, events, history, diagnostics, validation evidence,
or review artifacts.

The complete content string must be one JSON object. Markdown fences, prose,
concatenated JSON values, malformed JSON, non-object JSON, ambiguous output,
and directives that do not satisfy the request's embedded action/transition
contracts are rejected. The existing `LiveModelAdapter` validates the
directive again downstream and remains authoritative.

## Zero-inference preflight

`--preflight` performs only local metadata checks: `/api/version`, `/api/tags`,
and `/api/show` for the exact model. It requires the alias metadata to map to
`remote_model: gpt-oss:20b`, `remote_host: https://ollama.com`, and
`details.parent_model: gpt-oss:20b`. It reports local API readiness, expected
version, model availability, readable metadata, the validated provenance, and
`provider_inference_started: false`. It does not call `/api/chat` or
`/api/generate` and does not establish that Cloud inference will succeed.

The verified owner environment for this contract is Ollama `0.32.14` with
`gpt-oss:20b-cloud` available. A version mismatch fails closed.

## Cancellation and ownership

The existing `CancellableJsonlCommandTransport` owns the adapter subprocess
boundary. Cancellation or timeout terminates the adapter and closes its HTTP
client resources promptly. The Ollama daemon is a persistent external service,
not a Local Application child process; Local Application does not kill it.
Closing the client connection therefore bounds the local adapter request but
does not claim to cancel computation already accepted by Ollama Cloud.
