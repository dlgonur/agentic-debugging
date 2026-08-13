# Current Agent Roster

Current operational routing authority for coding-agent sessions and research
work in this repository. This file records who owns which kind of work and
under which authorization. It does not itself authorize any provider/model
execution.

## Implementation route (default)

- **DeepSeek V4 Flash through the operator's OpenCode Go subscription** is the
  default implementation route for coding-agent sessions **when a task
  explicitly authorizes model use**. No other provider route is the default.
- The next authorized QuixBugs execution uses the paired-pilot v4 manifest
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
  under the frozen v3 contract).
- The frozen v2/v3 manifests remain compatibility and derivation authorities;
  they must not be selected for the next live attempt. Every v4 operator
  command must pass `--manifest research/quixbugs/PAIRED_PILOT_V4.json`
  explicitly, because the CLI default remains v2.
- The earlier OpenCode Zen free-model matrix
  (`deepseek-v4-flash-free`, variant `max`) is a historical, descriptive-only
  record and is not the current implementation route.

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
- When authorization exists, the implementation route is DeepSeek V4 Flash via
  the operator's OpenCode Go subscription; literature review and deep research
  are routed to GPT-5.6 High in the separate ChatGPT conversation.

## Reference

- `research/quixbugs/PAIRED_PILOT_V4.json` — next six-case live manifest
- `docs/datasets/quixbugs/paired-pilot-v2.md` — retained v2 derivation and route contract
- `docs/datasets/quixbugs/opencode-adapter.md` — v2/v3/v4 operator sequence
- `docs/datasets/quixbugs/paired-pilot-v1.md` — retained v1 authority (historical
  OpenCode Zen zero-price route)
- `docs/evaluation/model-rag-sft-dpo.md` — model-access strategy
  decisions (historical; free-tier PROCEED predates the OpenCode Go
  subscription route)
- `docs/project-tracker.md` — project execution tracker
