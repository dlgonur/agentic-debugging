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
- `Output` — provider-reported output/completion tokens (including
  generation-side reasoning tokens when reported separately by the
  provider, e.g. OpenCode reasoning or Gemini thinking).
- `Total` — `Input + Output` (never `Input + Cached + Output`).

Example: Input 10,000 · Cached 7,000 · Output 2,000 → Total 12,000.

## Provider-reported only and lower-bound truth

No local token estimation exists or is authorized for this feature. If a
route does not report a dimension, that dimension stays unknown
(`None` end-to-end, `—` in the UI); it is never silently converted to
zero.

When a logical call spans multiple provider attempts (e.g., attempt 1
reports token usage but a directive is rejected, and attempt 2 repairs the
directive but lacks a usage block), known provider-reported consumption is
never discarded. Instead, it is preserved as a truthful lower bound
(`Input 100+ · Cached 40+ · Output 20+ · Total 120+`) and accompanied by a
typed `token_usage_coverage` block specifying which dimensions are complete
versus lower bounds.

Coverage is tracked per-dimension across the session:
- A dimension is complete only when reported and complete on every completed request.
- Subtotals for partially reported dimensions survive as truthful lower bounds
  and render with a trailing `+` indicator (e.g., `In 150+ · Cache 40+ · Out 30`).
- Session-level total completeness requires all completed requests to carry
  complete total coverage; otherwise, the session summary is truthfully marked
  `Tokens 31.8k (partial)`.

## Value contract

`agentic_debugger/agent/token_usage.py` owns the single typed, SAFE
value: non-negative bounded integer counts only
(`input/output/cached/total`, plus an internal `cache_write` dimension
retained by the evaluation metrics). It can never carry prompts,
completions, credentials, headers, endpoints, or request/response bodies;
unknown provider payload is ignored at ingestion and rejected at the
durable-event boundary, which also fails closed on non-integer/negative
counts, unknown fields, `total != input + output`, and
`cached > input`. An accompanying `TokenUsageCoverage` value tracks
per-dimension boolean completeness flags.

## Flow (one chain, no second telemetry system)

provider adapter normalization (transport `usage` mapping)
→ `LiveModelAdapter` per-logical-call aggregation
  (`last_request_token_usage()` and `last_request_token_coverage()`; every
  provider-completed attempt accumulates known consumption; partial logical
  calls preserve lower bounds rather than discarding tokens; transport failures
  without a provider response contribute nothing and are never fabricated)
→ `ControllerObservation.token_usage` and `.token_usage_coverage` (optional
  adapter seam; scripted adapters legitimately report none)
→ `ControllerSessionEventAdapter` → `model.request_completed` payload
  (`token_usage` and `token_usage_coverage` blocks, additive; historical events
  without coverage default to complete for reported dimensions)
→ shared `reduce_event` reducer (`SessionViewState.token_usage`,
  cumulative counts + dimension-specific completeness) → header/Live-pane rendering.
  Live and replay use the same reducer, so they cannot diverge.
→ `LiveModelMetrics.usage()` consumes canonical token semantics (`usage.canonical()`),
  ensuring `Total = Input + Output` whenever Input and Output are known.

## Provider normalization notes

| Route | Cached reported? | Notes |
|---|---|---|
| Direct API `chat_completions` / `responses` | yes — `prompt_tokens_details` / `input_tokens_details` `cached_tokens` (subset of the reported prompt/input count) | |
| Direct API `messages` (Anthropic) | yes — `cache_read_input_tokens` | Canonical input = base + cache read + cache creation (the provider's disjoint input buckets); cache-write retained internally. |
| OpenCode protocol / Go command / legacy CLI | yes — `part.tokens.cache.read` | Anthropic-style disjoint buckets: canonical input = base + read + write; reasoning tokens are generation-side and fold into canonical Output (output + reasoning). |
| AGY Gemini command | yes — `cache_read_tokens` (Gemini semantics: input already includes cached content) | `thinking_tokens` are generation-side and fold into Output so the canonical total matches the provider's own total. |
| CommandCode GOAT (legacy CLI) | when the CLI reports `cachedInputTokens` | camelCase usage is normalized to the canonical transport keys. |
| Ollama Cloud | no cache dimension | prompt/eval counts only; no cached claim. |

Routes that report no usage at all remain truthfully unavailable.

## Out of scope (deliberately)

Money/cost calculations, pricing tables, context-window percentages,
token budgets/enforcement, pre-call token prediction, billing or account
usage APIs, and historical account spend. Pricing changes over time and
belongs to a separate capability.
