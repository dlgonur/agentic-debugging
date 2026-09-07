# Session Token Usage Telemetry v1

Accepted capability record (Task 34, implemented on
`feat/session-token-usage-v1`; additive on top of the post-V2 closeout
baseline `9e658ea`). This is a product telemetry capability, not a new
architecture campaign.

## What it is

Truthful, provider-reported token usage per live session, visible in the
live/replay UI:

1. per completed logical model request (Live-pane row detail):
   `Input 8,420 · Cached 6,912 · Output 734 · Total 9,154`;
2. cumulative session usage (workspace header `Tokens 31.8k`, with the
   breakdown in the wide-screen run-context rail).

The data rides the durable session event/journal path
(`model.request_completed`), so replay reduces the identical cumulative
state from the journal.

## Semantics (canonical, user-facing)

- `Input` — the complete effective input token count under the provider's
  own usage semantics.
- `Cached` — the cache-read/hit portion of Input when the provider reports
  it. **Cached is a subset of Input.**
- `Output` — provider-reported output/completion tokens.
- `Total` — `Input + Output` (never `Input + Cached + Output`).

Example: Input 10,000 · Cached 7,000 · Output 2,000 → Total 12,000.

## Provider-reported only

No local token estimation exists or is authorized for this feature. If a
route does not report a dimension, that dimension stays unknown
(`None` end-to-end, `—` in the UI); it is never silently converted to
zero. A session where some completed request reported no usable usage is
marked partial (`Tokens 31.8k (partial)`) instead of claiming a complete
total.

## Value contract

`agentic_debugger/agent/token_usage.py` owns the single typed, SAFE
value: non-negative bounded integer counts only
(`input/output/cached/total`, plus an internal `cache_write` dimension
retained by the evaluation metrics). It can never carry prompts,
completions, credentials, headers, endpoints, or request/response bodies;
unknown provider payload is ignored at ingestion and rejected at the
durable-event boundary, which also fails closed on non-integer/negative
counts, unknown fields, `total != input + output`, and
`cached > input`.

## Flow (one chain, no second telemetry system)

provider adapter normalization (transport `usage` mapping)
→ `LiveModelAdapter` per-logical-call aggregation
  (`last_request_token_usage()`; every provider-completed attempt counts,
  including transport retries and directive repairs; transport failures
  without a provider response contribute nothing and are never fabricated)
→ `ControllerObservation.token_usage` (optional adapter seam; scripted
  adapters legitimately report none)
→ `ControllerSessionEventAdapter` → `model.request_completed` payload
  (`token_usage` block, additive; historical events without usage remain
  valid)
→ shared `reduce_event` reducer (`SessionViewState.token_usage`,
  cumulative counts + coverage) → header/Live-pane rendering. Live and
  replay use the same reducer, so they cannot diverge.

## Provider normalization notes

| Route | Cached reported? | Notes |
|---|---|---|
| Direct API `chat_completions` / `responses` | yes — `prompt_tokens_details` / `input_tokens_details` `cached_tokens` (subset of the reported prompt/input count) | |
| Direct API `messages` (Anthropic) | yes — `cache_read_input_tokens` | Canonical input = base + cache read + cache creation (the provider's disjoint input buckets); cache-write retained internally. |
| OpenCode protocol / Go command / legacy CLI | yes — `part.tokens.cache.read` | Anthropic-style disjoint buckets: canonical input = base + read + write. |
| AGY Gemini command | yes — `cache_read_tokens` (Gemini semantics: input already includes cached content) | `thinking_tokens` are generation-side and fold into Output so the canonical total matches the provider's own total. |
| CommandCode GOAT (legacy CLI) | when the CLI reports `cachedInputTokens` | camelCase usage is normalized to the canonical transport keys. |
| Ollama Cloud | no cache dimension | prompt/eval counts only; no cached claim. |

Routes that report no usage at all remain truthfully unavailable.

## Out of scope (deliberately)

Money/cost calculations, pricing tables, context-window percentages,
token budgets/enforcement, pre-call token prediction, billing or account
usage APIs, and historical account spend. Pricing changes over time and
belongs to a separate capability.
