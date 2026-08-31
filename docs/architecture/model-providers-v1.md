# Model Providers v1

Status: accepted (2026-08-28) · Owner decision: Onur, Goal-Mode authorization
"access the models not only with OLLAMA, but also from the OpenCode Go and
CommandCode GOAT plan"

Extended 2026-08-31: provider connections, live catalog discovery, and the
direct-API general runtime route (see "Provider connections v2" below). The
original v1 decision and its history are preserved unchanged below.

## Provider connections v2 (added 2026-08-31)

### Scope

The two subscription providers gained an application-owned connection
abstraction (`agentic_debugger/application/provider_connections.py`) and a
direct provider-API execution route
(`scripts/provider_direct_api_adapter.py`):

    connect provider -> discover live catalog -> GENERAL MODEL CATALOG
        -> direct API general-runtime execution

This is a product/platform capability only. It deliberately does NOT change
the scientific Capability Ladder: a discovered model never becomes
treatment-eligible, never enters the qualified Ollama roster, and never
appears in the ladder picker (`model_compatibility` keeps the ladder
fail-closed to qualified Ollama Cloud entries; `is_treatment_eligible` is
untouched).

### Provider endpoint facts (verified 2026-08-31 against official docs and
live catalogs)

| Provider | Catalog | Inference families | Protocol metadata in catalog |
|---|---|---|---|
| `opencode_go` | `GET https://opencode.ai/zen/go/v1/models` (Bearer key) | `/chat/completions`, `/responses`, `/messages` | none — ids only |
| `commandcode_goat` | `GET https://api.commandcode.ai/provider/v1/models` | `/chat/completions` (OpenAI/open-source), `/messages` (Anthropic) | none — ids only |

Both hosts sit behind bot protection that rejects the Python stdlib TLS
signature with HTTP 403 `error code: 1010` (verified for both providers).
The OS `curl` client is accepted by the same endpoints, so the bounded HTTP
boundary (`agentic_debugger/application/provider_http.py`) performs a
deterministic pre-request engine selection: providers declaring
`tls_signature_blocked` use `curl` when present, otherwise the stdlib engine
fails closed with the sanitized 1010 diagnostic. Engine selection is never a
retry: one request, one engine, no fallback.

### Protocol resolution — discovery is not permission to guess

- OpenCode Go: an explicit, provider-owned model→protocol table grounded in
  the official Go endpoint table (https://opencode.ai/docs/go/, "Endpoints",
  verified 2026-08-31). The general Zen catalog routes some shared ids
  differently (e.g. MiniMax uses `/chat/completions` on `zen/v1` but
  `/messages` on `zen/go/v1`), so the mapping is per-base and is never
  extended by family heuristics. A model absent from the documented table is
  **discovered but not runnable**: "Protocol not yet resolved for direct API".
- CommandCode: the provider documents exactly two families and validates the
  split server-side. The deterministic routing rule is the documented one:
  Anthropic identities (`claude-*`, `anthropic/` prefix) call `/messages`;
  every other catalog id calls `/chat/completions`.

### Credentials

- Raw keys never enter the repository, `config/command-models.json`, the
  catalog cache, session params, journals, events, provenance, logs, argv,
  or diagnostics.
- Resolution order inside the runtime boundary: process-local session key
  (UI "Connect API key", memory-only) → provider environment variable
  (`CMD_API_KEY` per current CommandCode docs; `OPENCODE_API_KEY`) →
  OpenCode CLI auth store read in place
  (`~/.local/share/opencode/auth.json`, `opencode-go` entry — schema
  verified). The CommandCode CLI auth store is NOT parsed: its schema is not
  reliably established, so the direct route fails closed to the
  environment/session-key sources.
- A pasted API key is process-local only. The UI says so plainly
  ("API key: connected for this app session"). No OS keychain or new secret
  dependency was introduced.

### Catalog discovery and cache

`refresh_provider_catalog` performs one read-only `GET /models` (no
generation credits), normalizes the response deterministically (invalid or
oversized entries ignored, dedupe, deterministic sort, bounded at 256
entries with an explicit `truncated` flag), and persists a versioned,
credential-free cache (`provider-catalog-cache.json` under the platform-local
app-data root; atomic write; malformed/oversized caches fail closed to absent).
A failed refresh never fabricates an empty successful catalog and never
touches the previous cache. `list_provider_models` prefers the most recent
valid discovered catalog and falls back to the curated presentation
defaults when no catalog has been refreshed.

### General runtime route

`resolve_provider_live_config` now makes an explicit, deterministic route
decision recorded in provenance:

- `direct_api` — the model's protocol family is resolved AND a usable
  credential source exists;
- `legacy_cli` — otherwise, when the provider CLI is fully available
  (v1 route preserved as an explicit, provenance-visible path).

There is no runtime fallback between routes. Provenance
(`model.configured`) gained optional additive fields `route`, `api_protocol`,
`provider_model_id`, and `endpoint` (credential-free; historical events replay
unchanged). OpenCode's `opencode-go/<id>` TUI/config namespace is normalized to
the provider API's documented bare `<id>` only at the direct transport
boundary. The
direct-API adapter speaks the identical protocol-1.3 JSONL command contract
as the v1 CLI adapters, performs exactly ONE provider inference per
transport request, adds zero retries and zero fallback, and accepts only an
evaluation-only `--base-url`/`--engine` pair (used by the local fake-server
tests; production never passes it).

## Original v1 decision (unchanged history)

## Scope

One unified provider registry selects and constructs the live decision-model
transport for Local Project Debug (and any future live surface):

| Kind | Route | Auth (operator-owned) |
|---|---|---|
| `ollama_cloud` | repository Ollama Cloud roster (unchanged accepted route) | Ollama adapter contract |
| `opencode_go` | `scripts/opencode_provider_adapter.py` → verified `opencode` CLI | `~/.local/share/opencode/auth.json` (read in place by the CLI) |
| `commandcode_goat` | `scripts/commandcode_goat_adapter.py` → `node <command-code>/dist/index.mjs` | `~/.commandcode/auth.json` (read in place by the CLI) or `CMD_API_KEY` |
| `configured` | existing `CommandModelConfigStore` profiles (unchanged) | profile command's own contract |

The frozen OpenCode Go campaign adapter (`opencode_go_command_adapter.py`)
is untouched historical evidence; the product adapter imports its machinery
(executable identity, auth store, isolation, bounded capture) and only opens
the model identity to `opencode-go/*` subscription models. Free-tier
`opencode/*` models are excluded so a subscription route is never silently
degraded.

## Registry contract (`agentic_debugger/application/model_providers.py`)

- `provider_availability()` — presence-only, offline probes (auth store
  file exists, CLI found). Never reads credential bytes, never contacts a
  provider, never prints secrets. Feeds `--doctor` and the UI.
- `list_provider_models()` — grouped, availability-annotated model list
  (curated presentation defaults captured from the live catalogs
  2026-08-28; any plan model id remains accepted at the adapter).
- `list_live_models(kind)` — explicit, provider-contacting catalog listing
  (read-only; no generation credits).
- `resolve_provider_live_config(kind, model_id)` — fail-closed
  `(LiveModelConfig, provenance)` construction through each provider's
  canonical builder.

## Adapter wire contract

Both new adapters speak the accepted protocol-1.3 JSONL command contract:

- one JSON request line on stdin (shared prompt shaping with the Ollama
  adapter — imported, not duplicated);
- exactly ONE provider inference per transport request, zero in-adapter
  retries (the accepted `LiveModelAdapter` owns bounded retry attempts
  with directive feedback above this boundary);
- one JSON response line on stdout: `{"provider_completion_schema_version":
  ..., "directive_content": <model text>, "usage": {...}}` — directive-shape
  normalization belongs to the app-side resolver, which accepts both the
  `kind`-tagged and `action`-keyed directive styles;
- failures emit the strict typed stderr envelope (`command-error-v1`,
  closed kind vocabulary) and exit 1.

## Why the CLI routes in v1 (historical decision, 2026-08-28)

The following record is the v1 decision rationale. It is preserved as
history: the Cloudflare-1010 observation remains true for Python-stdlib
TLS clients, but it no longer forces the CLI route — the v2 direct path
uses the OS curl engine (see above). The v1 CLI adapters remain valid,
provenance-visible compatibility routes.

The CommandCode provider endpoint fronts Cloudflare bot protection that
rejects stdlib HTTP TLS fingerprints (observed 403 `error code: 1010`).
Both operators' CLIs are the supported, authenticated entry points —
exactly the pattern the frozen OpenCode Go adapter established.

npm's `.CMD` shims cannot carry model prompts: cmd.exe re-parses the shim's
`%*` and eats `&`, `<`, `>`, `|` characters, silently truncating the prompt
and flags (observed). The CommandCode adapter therefore resolves the
package entry (`node_modules/command-code/dist/index.mjs`) next to the shim
and runs it with the verified `node` runtime directly. The adapter also
passes `--no-auto-update` (a CLI self-update corrupted its own bin links
mid-session once already), `--no-session` (one-shot directive calls must
not persist transcripts), and runs in a fresh empty directory (the CLI
injects directory context into the prompt).

## Session provenance

`model.configured` events now carry an optional `provider` field
(`ollama_cloud` / `opencode_go` / `commandcode_goat` / `configured`), so
replay and history show which provider served a session. The payload
remains credential-free by schema.

## Boundaries

- Live provider execution still requires explicit operator authorization
  per task; the registry's availability probes never execute models.
- Costs: GOAT metering is per-credit; OpenCode Go is subscription-bounded.
  The adapters make no billing assumptions and add no fallback routes.
- Claude models through CommandCode route via the CLI itself in v1; the
  v2 direct path resolves Claude-family models to the documented
  `/messages` endpoint deterministically (see above).
- Scientific qualification for OpenCode Go / CommandCode GOAT models
  (Capability Ladder Levels 6/12/18/32) remains future work and stays
  fail-closed; the v2 connection/catalog layer provides only the
  foundation for that separate, owner-authorized campaign.

## Validation evidence (2026-08-28)

- Real end-to-end adapter calls: CommandCode GOAT (free model
  `poolside/laguna-s-2.1-free`) and OpenCode Go
  (`opencode-go/deepseek-v4-flash`) both returned valid directive JSON +
  usage under the full protocol contract, exit 0.
- `python -m agentic_debugger.ui --doctor` reports all three providers.
- 47 focused unit tests: adapter contracts, registry resolution, Local
  Project provider params, plus 78 regression tests over the Local Project
  and UI surfaces and 243 event-schema tests.
