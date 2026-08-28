# Model Providers v1

Status: accepted (2026-08-28) · Owner decision: Onur, Goal-Mode authorization
"access the models not only with OLLAMA, but also from the OpenCode Go and
CommandCode GOAT plan"

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

## Why the CLI routes (not raw HTTP)

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
- Claude models through CommandCode route via the CLI itself; the raw-API
  `/messages` endpoint is not implemented in v1.

## Validation evidence (2026-08-28)

- Real end-to-end adapter calls: CommandCode GOAT (free model
  `poolside/laguna-s-2.1-free`) and OpenCode Go
  (`opencode-go/deepseek-v4-flash`) both returned valid directive JSON +
  usage under the full protocol contract, exit 0.
- `python -m agentic_debugger.ui --doctor` reports all three providers.
- 47 focused unit tests: adapter contracts, registry resolution, Local
  Project provider params, plus 78 regression tests over the Local Project
  and UI surfaces and 243 event-schema tests.
