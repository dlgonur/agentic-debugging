# Current Agent Roster

Current operational routing authority for coding-agent sessions and research
work in this repository. This file records who owns which kind of work and
under which authorization. It does not itself authorize any provider/model
execution.

These three route roles stay separate and must not be inferred from each
other:

1. **Coding-agent implementation route** — the model/harness used by the
   local coding agent to inspect and modify the repository.
2. **Product runtime decision-model route** — the model used by Local
   Application through configured-command execution.
3. **Historical Six-Case campaign route** — the frozen OpenCode Go /
   DeepSeek V4 Flash execution path for the optional paired-pilot
   experiment.

**2026-08-17 campaign disposition:** the historical Authorized Six-Case Live
Campaign is **RETAIN_OPTIONAL / OWNER-AUTHORIZED**. It is not required for
Local Application V1 completion and not required for the accepted R1-R6
scientific closeout. The frozen OpenCode Go V4 path remains preserved
evidence, not the current product route and not a coding-agent default; do
not run OpenCode Go merely for checkbox completion. A future
PDB-versus-static comparative experiment using Ollama would be a new
experiment with a new protocol and separate owner authorization, not a
mutation of `research/quixbugs/PAIRED_PILOT_V4.json`.

## Coding-agent implementation route

- The coding-agent implementation route is **TASK-SELECTED /
  OWNER-AUTHORIZED**. This repository does not infer it from the product
  runtime provider.
- It may be Grok, Codex, Gemini, or another explicitly authorized coding
  route, selected by the current task or operator.
- Successful Local Application Ollama Cloud product-runtime proof does
  **not** make Ollama Cloud the repository coding-agent default.
- The frozen OpenCode Go / DeepSeek V4 Flash campaign path is **not** a
  coding-agent default merely because it remains preserved.

## Product runtime decision-model route

- The current accepted **product runtime** route is the Local Application
  configured-command path through Ollama Cloud `gpt-oss:20b-cloud`
  (successful session `sess-20260817-103258-3d1193`). See
  `docs/architecture/ollama-cloud-command-adapter-v1.md` and
  `docs/architecture/local-application-v1.md`.
- This is the model used **by** Local Application. It does not select or
  replace the coding-agent implementation route.

## Historical Six-Case campaign route

- **DeepSeek V4 Flash through the operator's OpenCode Go subscription** is
  only the historical execution route for the optional
  `PAIRED_PILOT_V4` experiment **when a task explicitly authorizes that
  exact frozen OpenCode Go path**. It is not the current product route and
  does not make the six-case campaign required.
- If that optional campaign is ever separately owner-authorized, the frozen
  execution still uses the paired-pilot v4 manifest
  (`research/quixbugs/PAIRED_PILOT_V4.json`, canonical SHA-256
  `020dfc1f7b8f23aa96a4d7c7942429e306cc290906abfed5ce96cde22b90354d`).
  It derives from the v3 route and experiment contract and preserves the same
  tasks, order, budgets, qualification authority, and route: OpenCode Go
  subscription, DeepSeek V4 Flash, protocol
  1.3, no Zen route, no free-tier substitution, no Ollama route, no alternate
  provider, no model substitution, and no metered/paid-overage/per-call
  billing fallback. Subscription entitlement and billing-route evidence must
  be established before the first provider call, or the campaign blocks
  before that call. v4 adds the verifier-authoritative classification and
  the budget-terminal matrix required by the observed v3 completed post-apply
  public-evidence exhaustion shape (attempt `fddf1e39...` aborted honestly
  under the frozen v3 contract). Do not rewrite that frozen contract onto
  Ollama.
- The frozen v2/v3 manifests remain compatibility and derivation authorities;
  they must not be selected if the optional v4 campaign is ever authorized.
  Every v4 operator command must pass
  `--manifest research/quixbugs/PAIRED_PILOT_V4.json` explicitly, because the
  CLI default remains v2.
- The earlier OpenCode Zen free-model matrix
  (`deepseek-v4-flash-free`, variant `max`) is a historical, descriptive-only
  record and is not a current coding-agent, product-runtime, or campaign
  route.

## Research route (literature review and deep research)

- **GPT-5.6 High in a separate ChatGPT conversation** owns literature review,
  deep research, source verification, and broad comparative research. This
  work happens outside coding-agent sessions.
- Coding agents may consume reviewed repository research artifacts
  (e.g. `research/` notes, synthesis packs, decision documents) but must not
  independently broaden implementation tasks into open-ended research
  campaigns.

## Routing rules

- Research outputs are **non-authoritative until reviewed and incorporated
  into tracked project artifacts** (docs, research notes, decision documents).
  Raw research output is not a basis for implementation decisions.
- **Every task still requires explicit authorization for provider/model
  execution.** No provider, model, or benchmark runs without a separate
  explicit authorization for that task.
- **Coding agents must not launch additional models, research agents, MCP
  servers, benchmarks, or paid services** unless the current task explicitly
  authorizes them.
- When a task authorizes provider or model use, use the route that task
  names for that role. The product runtime route does not determine
  coding-agent routing. The frozen OpenCode Go / DeepSeek V4 Flash path
  remains valid only for a separately authorized historical paired-pilot
  execution. Literature review and deep research remain on the research
  route above.

## Reference

- `docs/architecture/local-application-v1.md` — Local Application product
  surface
- `docs/architecture/ollama-cloud-command-adapter-v1.md` — current accepted
  product-runtime adapter
- `research/quixbugs/PAIRED_PILOT_V4.json` — frozen optional OpenCode Go
  six-case live manifest (RETAIN_OPTIONAL; do not mutate onto Ollama)
- `docs/datasets/quixbugs/paired-pilot-v2.md` — retained v2 derivation and route contract
- `docs/datasets/quixbugs/opencode-adapter.md` — v2/v3/v4 operator sequence
- `docs/datasets/quixbugs/paired-pilot-v1.md` — retained v1 authority (historical
  OpenCode Zen zero-price route)
- `docs/evaluation/model-rag-sft-dpo.md` — model-access strategy
  decisions (historical; free-tier PROCEED predates the OpenCode Go
  subscription route)
- `docs/project-tracker.md` — project execution tracker
