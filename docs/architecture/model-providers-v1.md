# Model Providers v1

Status: accepted (2026-08-28) · Owner decision: Onur, Goal-Mode authorization
"access the models not only with OLLAMA, but also from the OpenCode Go and
CommandCode GOAT plan"

Extended 2026-08-31: provider connections, live catalog discovery, and the
direct-API general runtime route (see "Provider connections v2" below).
Extended 2026-09-02: the registry became fully user-owned — zero configured
providers on a fresh installation, all providers (including the historical
Ollama/OpenCode/CommandCode kinds and arbitrary user-defined direct-API
providers) created explicitly through the Model Providers manager, strict
fail-closed persistence (bounded safe provider-id grammar, no protocol
coercion, no duplicate identities, explicit 64-provider bound), and
endpoint/credential binding safety (a Base URL change with a stored
reusable credential requires key re-entry). The original v1 decision and
its history are preserved unchanged below.

Extended 2026-09-03 (provider-platform integrity): the transport contract
is now explicit and truthful. Provider configuration carries independent
`auth_mode` (bearer / anthropic / none) and `catalog_mode` (openai /
disabled) axes persisted under schema `provider-configurations-v2` (V1
files migrate deterministically to bearer/openai). Supported matrix:
bearer + chat_completions/responses/messages; anthropic + messages only
(native x-api-key + pinned anthropic-version headers, never Bearer);
none + chat_completions only on loopback/self-hosted endpoints with no
credential sent. Unsupported combinations fail before execution with an
actionable message. Ambient environment / CLI-auth credentials are
canonical-endpoint-bound and never resolve against an edited endpoint;
saved/session/forwarded-session sources are provider-identity-bound with
stale-saved-wins ordering. Durable state is strictly validated with exact
types on both read and write (no bool()/str() coercion, no silent model
drops, no unknown-key tolerance); generated IDs always satisfy the bounded
grammar including collision suffixes; ADD rejects existing identities;
the model picker and doctor derive from user-owned registry truth and
surface corruption instead of healthy-empty state. "Save & discover" on a
manual-only provider reports manual-model guidance without a misleading
half-connected state; catalog failures preserve the last known-good
catalog. The UI surface is Model Providers (`m`; hidden `c` binding kept
for compatibility).

Extended 2026-09-04 (transport-profile authority): generic provider
identity and transport semantics are now separate concepts. Every
provider configuration carries an explicit `transport_profile`
(`generic` default, or one historical profile). Historical catalog
paths, inference path sets, TLS behavior, canonical endpoint binding,
provider env-var authority, CLI-auth authority, per-model historical
protocol resolvers, and legacy CLI eligibility all consult the explicit
profile — never the technical ID alone. A generic provider identified
`ollama_cloud`/`opencode_go`/`commandcode_goat` behaves exactly like any
generic provider (`/chat/completions`, no ambient env/CLI, no resolvers,
no CLI fallback). Records predating the profile field migrate
deterministically to generic, except historical-ID records without
durable profile evidence, which fail closed with actionable
`transport_profile` migration guidance instead of guessing. The
effective model protocol (default, manual, discovered, or historical
resolver-derived) is validated against the provider auth matrix and the
profile capability set before persistence (where knowable), picker
availability, doctor status, connection testing, LiveModelConfig
creation, and adapter execution — supported paths only; Ollama exposes
Chat Completions alone. The child adapter receives the non-secret
`--auth-mode` explicitly and agreement-checks auth/endpoint/protocol
against its own configuration view with zero HTTP attempts on any
mismatch (no Bearer guessing). Scientific qualification additionally
requires the absence of a generic squatter on the Ollama identity, so a
generic `ollama_cloud` model can never satisfy Level-32 qualification or
take the canonical ladder operator path.

## Provider connections v2 (added 2026-08-31)

### Scope

The provider registry is an application-owned, user-configured connection
abstraction (`agentic_debugger/application/provider_connections.py`) with a
direct provider-API execution route
(`scripts/provider_direct_api_adapter.py`):

    connect provider -> discover live catalog -> GENERAL MODEL CATALOG
        -> direct API general-runtime execution

This is a product/platform capability only. It deliberately does NOT change
the scientific Capability Ladder: a discovered or user-configured model
never becomes treatment-eligible and never enters the qualified Ollama
roster (`is_treatment_eligible` is untouched). Interactive lower ladder
rungs accept any executable configured provider model; only the frozen
Level-32 treatment remains bound to the qualified Ollama Cloud roster
(`model_compatibility` distinguishes the two, and
`SessionCatalog.ladder_model` binds qualification to provider identity).

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
- Resolution order inside the runtime boundary (updated 2026-09-02 for the
  user-owned registry): forwarded private session environment variable
  (the UI→worker credential hop) → saved OS secure credential
  (Windows Credential Manager, `CRED_PERSIST_USER`; presence-only checks
  elsewhere) → process-local session key → provider environment source
  (`COMMAND_CODE_API_KEY`; optional app-supported `OPENCODE_API_KEY`) →
  OpenCode CLI auth store read in place
  (`~/.local/share/opencode/auth.json`, `opencode-go` entry — schema
  verified). The CommandCode CLI auth store is NOT parsed: its schema is not
  reliably established, so the direct route fails closed to the
  environment/session-key sources.
- An API key saved in the Model Providers manager is stored in the OS
  secure credential store when available (otherwise it remains
  session-only), and the connection status truthfully distinguishes
  `saved`, `session_key`, `environment`, and `cli_auth_store`. Changing a
  provider's Base URL while a reusable credential is stored requires
  re-entering the key: the stored credential is never silently rebound to
  a different endpoint.

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
| `commandcode_goat` | `scripts/commandcode_goat_adapter.py` → `node <command-code>/dist/index.mjs` | `~/.commandcode/auth.json` (read in place by the CLI) or documented `COMMAND_CODE_API_KEY` override |
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
