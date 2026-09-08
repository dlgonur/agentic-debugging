# Session Token Usage Telemetry v1

Accepted capability record (Task 34, COMPLETE 2026-09-08 on
`feat/session-token-usage-v1` at Candidate 38; additive on top of the post-V2 closeout
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
zero, guessed, or locally synthesized.

When a logical call spans multiple provider attempts:
- Each attempt is evaluated under its own canonical truth via the shared
  `_compute_attempt_usage` authority:
  1. If Input and Output are both known, exact attempt Total = Input + Output
     (overriding any conflicting raw provider total).
  2. Else if a valid provider Total is reported (and >= known component bounds),
     exact attempt Total = reported Total.
  3. Else exact Total is unknown, preserving any component lower bounds.
- When an attempt reports only `cached_input_tokens = 40` (and `input_tokens = None`),
  because Cached is a subset of Input, this proves `Input >= 40` and `Total >= 40`
  without estimation. Complete Input already includes cache contribution (never double-counted).
- Multi-attempt aggregation accumulates known bounds:
  Attempt 1 (100/20/120) + Attempt 2 (Cached 40 only) proves `Input 140+ · Cached 40+ · Output 20+ · Total 160+`.
- An exact provider Total can coexist with partial Input/Output subtotals:
  if Attempt 1 reports 100/20/120 and Attempt 2 reports only Total 150,
  the aggregate Total is exact and complete (270, coverage `True`),
  while Input (100) and Output (20) are partial lower bounds (coverage `False`).
- Exact totals contradicting component lower bounds (`total_tokens < min_components` with exact total)
  fail closed in `canonical` and drop telemetry at `ControllerSessionEventAdapter` while preserving lifecycle events.
- Coverage contradictions (`Input complete + Output complete + Total reported + Total coverage partial`)
  are rejected as impossible at the coverage/event boundary.

Coverage is tracked per-dimension across the session:
- A dimension is complete only when reported and complete on every completed request.
- Subtotals for partially reported dimensions survive as truthful lower bounds
  and render with a trailing `+` indicator (e.g., `In 150+ · Cache 40+ · Out 30`).
- Session-level total completeness requires all completed requests to carry
  complete total coverage; when complete, the header shows `Tokens 270` without
  `(partial)`. If any request lacked complete total coverage, the header is
  truthfully marked `Tokens 31.8k (partial)`.

## Value contract

`agentic_debugger/agent/token_usage.py` owns the single typed, SAFE
value: non-negative bounded integer counts only
(`input/output/cached/total`, plus an internal `cache_write` dimension
retained by the evaluation metrics). It can never carry prompts,
completions, credentials, headers, endpoints, or request/response bodies;
unknown provider payload is ignored at ingestion and rejected at the
durable-event boundary, which also fails closed on non-integer/negative
counts and unknown fields.

Cross-field arithmetic is coverage-aware:
- Historical events (coverage is None) and requests where both Input and
  Output are complete enforce strict equality (`total == input + output`)
  and containment (`cached <= input`).
- Requests with partial component coverage allow `total >= input + output`
  (exact total coexisting with partial component lower bounds) and allow
  `cached > input` (cached tokens from one attempt exceeding a partial input
  lower bound). Total is never permitted to be less than the sum of known
  component lower bounds.
- An accompanying `TokenUsageCoverage` value tracks per-dimension boolean
  completeness flags (`input_tokens`, `output_tokens`, `cached_input_tokens`,
  `total_tokens`).

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
→ `LiveModelMetrics.usage()` and `_LogicalRequestUsage` consume the shared
  `_compute_attempt_usage` per-attempt authority, ensuring complete parity
  between evaluation metrics and durable logical-request token usage.

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
