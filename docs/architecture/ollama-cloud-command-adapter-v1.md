# Local Application V1 — Ollama Cloud Command Adapter

**Status:** bounded implementation contract for one shared Ollama Cloud
adapter with accepted model profiles

**Accepted profiles (17 selectable aliases; 15 live-verified, 1 profile-declared, 1 catalog):**

| Local Application / Ollama CLI alias | Upstream (`/api/show` parent / chat `model`) | `/api/tags` remote_model | Readiness |
| --- | --- | --- | --- |
| `gpt-oss:20b-cloud` | `gpt-oss:20b` | `gpt-oss:20b` | live_verified — historically qualified streaming `think: high` |
| `gpt-oss:120b-cloud` | `gpt-oss:120b` | `gpt-oss:120b` | live_verified — retained bounded qualification, `think: high` |
| `nemotron-3-nano:30b-cloud` | `nemotron-3-nano:30b` | `nemotron-3-nano:30b` | live_verified — prior streamed qualification (task success not required) |
| `glm-5.1:cloud` | `glm-5.1` | `glm-5.1` | live_verified — retained bounded generic-stream qualification; Level-32 matrix authoritative RESOLVED (2026-08-24) |
| `glm-5.2:cloud` | `glm-5.2` | `glm-5.2` | live_verified — accepted Transport Qualification V2; no-thinking 20/60 profile; Level-32 repaired treatment authoritative RESOLVED (2026-08-24) |
| `deepseek-v4-flash:cloud` | `deepseek-v4-flash` | `deepseek-v4-flash:0731` | live_verified — retained bounded generic-stream qualification |
| `deepseek-v4-pro:cloud` | `deepseek-v4-pro` | `deepseek-v4-pro:0813` | live_verified — retained bounded generic-stream qualification |
| `kimi-k2.6:cloud` | `kimi-k2.6` | `kimi-k2.6` | live_verified — retained bounded qualification |
| `kimi-k2.7-code:cloud` | `kimi-k2.7-code` | `kimi-k2.7-code` | profile_declared — generic streaming qualification pending, not Level-32 eligible |
| `kimi-k3:cloud` | `kimi-k3` | `kimi-k3` | catalog — selectable, not Level-32 eligible |
| `minimax-m2.7:cloud` | `minimax-m2.7` | `minimax-m2.7` | live_verified — retained bounded generic-stream qualification |
| `minimax-m3:cloud` | `minimax-m3` | `minimax-m3` | live_verified — retained bounded generic-stream qualification |
| `nemotron-3-super:cloud` | `nemotron-3-super` | `nemotron-3-super` | live_verified — retained bounded generic-stream qualification |
| `nemotron-3-ultra:cloud` | `nemotron-3-ultra` | `nemotron-3-ultra` | live_verified — retained bounded generic-stream qualification |
| `qwen3.5:cloud` | `qwen3.5` | `qwen3.5:397b` | live_verified — retained bounded qualification |
| `gemma4:31b-cloud` | `gemma4:31b` | `gemma4:31b` | live_verified — retained bounded qualification |
| `mistral-large-3:675b-cloud` | `mistral-large-3:675b` | `mistral-large-3:675b` | live_verified — retained bounded no-thinking stream qualification |

`--model` selects one accepted alias; unknown aliases fail closed (`configuration`). The default remains `gpt-oss:20b-cloud`. The OpenCode display name `ollama-cloud/nemotron-3-nano:30b` is not an Ollama CLI identifier and is rejected. `--list-models` / `--list-models --json` project the canonical adapter registry (including `readiness` and `transport_config_fingerprint`) without adding a second source of truth. All 17 share host `https://ollama.com`; versioned `remote_model` divergences are explicit and asserted separately against `/api/tags` vs `/api/show`/chat.

Readiness is three-state: `catalog` (selectable + provenance-validated, no transport profile), `profile_declared` (same-family streaming intent carried, live exercise pending), `live_verified` (bounded live `/api/chat` streaming qualification recorded). `live_verified` historically means V1 stream transport qualification; it is not by itself a claim of directive-protocol compatibility. The Level-32 operator (`run_cookiecutter_967_pdb_proof.py`) fails closed before any preflight/Docker work unless `treatment_eligible` (`live_verified`) holds. There is no `--force` bypass; the intended sequencing is: (1) run the repo-aware `python -m agentic_debugger.evaluation.transport_qualification --endpoint http://127.0.0.1:11434/api --model gpt-oss:120b-cloud --confirm-live --json`, which first launches the standalone adapter with `--preflight` and then launches it once for the synthetic `/api/chat`, retaining the canonical preflight record, (2) review/promote `transport_verified`, (3) only then run Level-32. The adapter itself emits only the provider-completion envelope and bounded stream telemetry; it has no runtime dependency on the repository package. Qualification V2 distinguishes `preflight_ok`, `stream_transport_ok`, and `directive_protocol_ok` without mass-demoting existing profiles. A completed measurement returns process exit code 0 even when `directive_protocol_ok` is false; promotion must inspect the typed result field rather than exit code alone.

`transport_config_fingerprint` covers every material execution parameter (alias, upstream, effective tags remote, capabilities, family, parameter count, context length, readiness/profile/verified/thinking, protocol name+version, Ollama version, host, all byte/frame/timeout bounds, retry/fallback counts, stream mode). Any material difference yields a different persisted fingerprint.

The evidence-backed `deepseek-v4-flash:cloud` profile uses a 300-second
stream-inactivity watchdog and a 3,600-second outer request bound. The Level-32
operator passes both values explicitly to the adapter; the inactivity watchdog
refreshes on every valid stream frame, while the outer deadline remains
authoritative for the complete request.

Nemotron 3 Super's first Level-32 treatment produced 7,864 streamed frames,
reached `Validate`, and then lost its seventeenth decision to the inherited
20-second inactivity watchdog. That incomplete treatment is infrastructure
evidence, not a semantic model result. Its fresh treatment profile uses the
same bounded, model-specific 45-second idle and 75-second outer request limits
already established for a continuously streaming Level-32 route; the changed
limits produce a new transport and treatment fingerprint.

Nemotron 3 Ultra's first Level-32 treatment likewise ended at the inherited
20-second inactivity watchdog: 14 valid responses completed exact PDB and an
evidence-bound diagnosis, but the fifteenth request produced no directive
after entering `Patch`. No patch or verifier result exists, so V1 is retained
as incomplete transport evidence rather than a semantic result. Ultra's fresh
V2 uses its own bounded 45-second idle and 75-second outer request limits with
zero retries and unchanged task, action, and decision budgets.

**2026-08-17:** the first real remote product proof through this adapter is
COMPLETE — session `sess-20260817-103258-3d1193` (task
`curated-none-handling-001`, policy `pdb-on-uncertainty`) SUCCEEDED with the
independent verifier RESOLVED (F2P 1/1, P2P 2/2), cleanup verified, and
observed live/replay terminal-state parity. PDB was NOT EXERCISED in that
session. That proof used `gpt-oss:20b-cloud`. See `docs/project-tracker.md`
(Local Application V1 real remote route proof, 2026-08-17) for the full
record. The later multi-model generalization does not replace that proof.

**2026-08-21:** a separate exact-PDB single-task live proof completed on
`pdb-required-boundary-006` with `gpt-oss:20b-cloud` through Ollama 0.32.15.
The model used the real bounded PDB start/stack/locals/next/stop lifecycle,
revised its hypothesis from current observation IDs, recorded a diagnosis
bound to an observed local value, and emitted a one-hunk unified diff. The
independent verifier returned RESOLVED (F2P 1/1, P2P 1/1); cleanup and canonical
immutability passed; replay ended in Done. The run used 21 logical calls and 21
transport attempts with zero retries and zero provider errors. This supplements
the earlier product-route proof by exercising PDB; it remains a one-task
lowest-rung proof rather than a comparative or generalization result.

Later on 2026-08-21, the activity-aware streaming contract completed the next
12/100 ladder rung, `pdb-required-caller-callee-007`: 22 calls/attempts, zero
retries/provider errors, verifier `RESOLVED`, private checks true, and 40,168
stream frames whose thinking text was discarded. Frozen evidence is under
`experiments/pdb_capability_ladder/`.

The same day, the 18/100 `pdb-required-multistage-units-008` rung completed in
21 calls/attempts with zero retries/provider errors. Exact PDB evidence exposed
the converted intermediate value before the stale raw input crossed the later
retry-expansion stage; the model's one-line patch was independently
`RESOLVED`, including verifier-only private checks. The run produced 26,821
activity frames and discarded 121,411 thinking bytes.

The historical frozen 32/100 `audreyr__cookiecutter-967` V3 rung reached a
valid local model/controller/PDB boundary, but its raw candidate serialization
was not proven compatible with the official Git application path. Its original
`0/5, 9/9` summary remains historical and is not a clean semantic rejection.
The repaired Level-32 treatment is separately identified as
`workspace-derived-official-git-diff-v1`: raw `candidate.patch` is preserved,
the accepted workspace delta produces `candidate-official.patch`, and strict
Git application plus byte equality are verified before official evaluation.
This changes candidate transport only; the task, hidden tests, PDB proof, and
official evaluator authority remain unchanged. The durable provider-free
replay summary is tracked in
`../../analysis/level32_candidate_artifact_replay_20260823.md`.

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
daemon. Metadata and chat use the same bounded inactivity setting.

```text
Local Application protocol 1.3
  -> Ollama Cloud command adapter
  -> GET  http://127.0.0.1:11434/api/tags
  -> POST http://127.0.0.1:11434/api/show
  -> POST http://127.0.0.1:11434/api/chat
  -> streamed thinking/activity plus final assistant content
  -> strict JSON and request-bound directive validation
  -> existing LiveModelAdapter/controller/verifier path
```

The request uses the selected accepted Cloud alias as `model`, plus
`stream: true` and `think: "high"`; it does not send `format`, `tools`, or
function definitions. Ollama Cloud does not currently support structured
outputs, so request-bound validation remains in this adapter rather than being
misrepresented as a provider-enforced schema. Chat messages use a `system` role for the stable
directive-schema contract and a `user` role for the bounded canonical public
request plus request-specific legal shapes. The system prompt states that the
top-level type field is always `kind` and gives the exact action,
transition, and hypothesis objects enforced by the adapter validator. The
user message derives illustrative legal action JSON from the current
`allowed_actions` and `action_contracts`; it does not grant provider tools.
For the opt-in exact-PDB proof, the same guidance renders the current
`proof_gate.next_required_actions`. A generated integer in the
`start_pdb_session` shape is explicitly structural, not a suggested
breakpoint: the model must substitute a visible executable statement inside
the target function, never a definition, import, or module-level line.
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
Tool/function-call activity is rejected. Every valid NDJSON frame refreshes
the inactivity watchdog. `message.thinking` is progress telemetry rather than
directive authority: its byte count is accumulated, but its text is discarded
immediately and never enters adapter stdout, Local Application protocol data,
events, history, diagnostics, validation evidence, or review artifacts. Only
the accumulated `message.content` string can become a directive. Its separate
64 KiB bound therefore does not reject a continuously progressing response
merely because the model used a long internal reasoning stream.

The `ollama-content-fragment-observability-v2` diagnostics retain bounded
authorized content fragments, their original and retained UTF-8 lengths and
hashes, frame indices, done flags, and channel-presence metadata. They retain
no thinking text and do not alter the ordered content concatenation. The
adapter fails closed when an unredacted, untruncated diagnostic cannot be
reconciled with its original frame or when the retained fragments cannot
reconstruct the parser-authorized content.

The complete content string normally must be one JSON object. Receiver policy
`directive-normalization-v3` first attempts strict JSON, retains the earlier
one-redundant-trailing-brace and exact lowercase-JSON LF-fence recoveries, and
adds one ordered composition: unwrap that exact whole-response fence, then
remove exactly one redundant trailing closing brace from its inner mapping.
The composed branch records raw, intermediate, and final hashes. It does not
search Markdown, accept prose, accept alternate fences, remove multiple
braces, concatenate objects, reverse or generally chain repairs, infer fields
or values, reserialize bytes, retry, or send feedback. Directives still enter
the unchanged `LiveModelAdapter` semantic parser, which remains authoritative.

## Zero-inference preflight

`--preflight` remains an operator diagnostic and is also the canonical nested
provenance record retained by repo-aware Qualification V2. It performs only
local metadata checks:
`/api/version`, `/api/tags`, and `/api/show` for the exact model. It requires
the same alias-to-upstream mapping as the normal request path. It reports
local API readiness, expected version, model availability, readable metadata,
the validated provenance, and `provider_inference_started: false`. It does
not call `/api/chat` or `/api/generate` and does not establish that Cloud
inference will succeed. Each later adapter invocation revalidates `/api/tags`
and `/api/show` under the same request timeout before it may call `/api/chat`.

Qualification V2 keeps three timeout concepts distinct: the repo-aware
metadata preflight process bound is a bounded 30-second infrastructure limit;
the resolved model profile's `idle_timeout_seconds` is passed to the adapter's
stream inactivity watchdog; and the resolved `request_timeout_seconds` is the
outer completion-process deadline, with only a small shutdown grace. Thus an
active stream may exceed its idle timeout in total elapsed time while each
progress interval remains below the idle timeout, but a silent stream still
fails at the adapter watchdog and any completion exceeding the outer request
deadline fails as an infrastructure measurement error.

The accepted lower-rung application source applies the proven 300-second
activity watchdog explicitly, rather than inheriting a model-profile value
from the frozen Level-32 operator roster. Its outer request deadline is the
remaining rung model-phase guard (600 seconds for Level 6 and 3,600 seconds
for Levels 12 and 18). The controller's cancellation token remains the
prompt-stop path, and lower-rung retries remain zero.

The current verified owner environment for this contract is Ollama `0.32.15`.
The accepted 2026-08-17 product-runtime proof used `0.32.14`; the exact pin is
kept explicit rather than relaxed across versions. The
accepted product default requires `gpt-oss:20b-cloud` available. The
experimental Nemotron profile requires `nemotron-3-nano:30b-cloud`
available. A version mismatch fails closed.

## Typed command failures

Successful adapter stdout contains the one directive plus bounded aggregate
and content-fragment stream activity (`stream_frame_count`, `thinking_bytes`,
content lengths/hashes, and bounded authorized fragments); it never contains
thinking text. On a
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
