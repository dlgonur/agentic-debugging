# Agentic Debugging Internship

Research and prototype workspace for an agentic debugging system.

The project investigates the path from traditional debugging, fault localization, automated program repair, LLM-based debugging, and repository-level software engineering agents toward interactive debugger-assisted agents.

## Initial research direction

- Python/PDB-first debugging prototype
- Deterministic debugger and test tools
- Single controller agent before multi-agent designs
- Verifier-backed patch validation
- Comparison against non-debugger baselines such as Agentless, SWE-agent, and AutoCodeRover

## Repository structure

- diary/: internship diary notes
- docs/: project documentation
- prompts/: research and agent prompts
- research/reports/raw/: raw AI-generated research reports
- research/reports/synthesis/: cross-report synthesis
- research/notes/: manual paper notes
- research/papers/: local paper archive; PDFs are gitignored
- TODO.md: project TODO list

## Accepted project status (through 2026-07-31)

An MVP agentic debugging implementation is accepted through Task 9: a single
controller agent, typed deterministic tools (file read, code search, test
run, patch apply), PDB session/runtime skills, and a verifier-backed patch
workflow. Task 10A added the real-model evaluation harness. Task 10B-R1
repaired the initial live protocol and accounting contracts, Task 10B-R3
added bounded invalid-directive retry feedback, and Task 10B-R5 completed the
policy-scoped contract repair in accepted source/merge commit
`63fa27cc4d30490b9770ead3ce14b4b6d3ddf222` (protocol version `1.3`).

[Historical] A private-runner, four-case descriptive matrix completed on
`curated-none-handling-001` through OpenCode Zen using
`deepseek-v4-flash-free` with variant `max`. Static policy resolved both
repetitions; the PDB-on-uncertainty policy resolved neither repetition and
terminated with underlying reason `invalid_model_response` before PDB opened.
Corrective-feedback recovery occurred in 4 of 6 observed feedback episodes.
This small, fixture-specific matrix does not establish causal PDB
effectiveness or general model reliability; see `docs/PROJECT_TRACKER.md`.
It is a historical record of the earlier OpenCode Zen free-tier route and is
not the current implementation route.

## Current status (2026-08-02)

The operational routing authority is `CURRENT_AGENT_ROSTER.md`: DeepSeek V4
Flash through the operator's OpenCode Go subscription is the default
implementation route when a task explicitly authorizes model use; GPT-5.6 High
in a separate ChatGPT conversation owns literature review and deep-research
work; research outputs are non-authoritative until reviewed and incorporated
into tracked project artifacts; every task still requires explicit
authorization for provider/model execution; coding agents must not launch
additional models, research agents, MCP, benchmarks, or paid services unless
the current task explicitly authorizes them.

The QuixBugs paired pilot is planned in two versions. v1
(`docs/QUIXBUGS_PAIRED_PILOT_V1.md`,
`research/quixbugs/PAIRED_PILOT_V1.json`) froze the three-task, six-case
static-versus-PDB feasibility design on the historical OpenCode Zen
zero-price route. v2 (`docs/QUIXBUGS_PAIRED_PILOT_V2.md`,
`research/quixbugs/PAIRED_PILOT_V2.json`) is the derived contract against
accepted baseline `18e067f24c337e7215139373edc699a347cf2127`: the same tasks,
six-case order, budgets, protocol 1.3, qualification contract, and
source-integrity authority, with the route replaced by the fail-closed OpenCode
Go subscription contract (DeepSeek V4 Flash; no Zen/free-tier/Ollama/
alternate-provider/model-substitution/metered/paid-overage/per-call fallback;
block before the first provider call if subscription entitlement or
billing-route evidence cannot be established; truthful provider-reported token
and cost metadata preserved). No exact catalog identifier, OpenCode version,
catalog fingerprint, account status, entitlement, or pricing observation is
invented; the exact runtime model/catalog identity remains authorization-bound,
and live execution remains unavailable until a separate implementation task
supplies an explicit authorization artifact.

BugsInPy execution remains blocked by its license gate. A resource-limited
QuixBugs (Python `gcd`) real no-model smoke completed successfully through
the accepted WSL2/Bubblewrap infrastructure, extended with a
live-self-tested `prlimit` CPU/memory/process-count profile: pinned revision
`4257f44b0ff1181dedaedee6a447e133219fcebf`, verdict
`ACCEPT CANDIDATE — REAL SMOKE PASSED`. See
`docs/QUIXBUGS_SMOKE_USAGE_V1.md`. That single-task smoke has since been
expanded into an eight-task no-model gold baseline on the same pinned
revision (`gcd`, `bucketsort`, `find_in_sorted`, `flatten`, `kth`, `hanoi`,
`is_valid_parenthesization`, `kheapsort`), reusing the same adapter, WSL
runner, resource profile, and verifier: 8/8 selected tasks solved (gold
patch verified end-to-end), verdict
`ACCEPT CANDIDATE — EIGHT-TASK BASELINE COMPLETE`. See
`docs/QUIXBUGS_EIGHT_TASK_BASELINE_V1.md`. Both validate infrastructure
only — no model, PDB, or broader benchmark campaign was run; every "patch"
applied is the literal upstream buggy→corrected diff, not a generated one.
No external dataset execution or larger live policy comparison is justified
until containment, task mapping, and a controlled real-model path that
actually opens PDB are ready.

Dataset and Evaluation Decision v1 selects BugsInPy as the primary external
dataset, QuixBugs Python as fallback, and the current five curated fixtures as
the architecture smoke gate. RAG is NO-GO-FOR-NOW for a research comparison,
SFT is DEFER, and DPO/preference optimization is NO-GO-FOR-NOW. See `TODO.md`
and `docs/DATASET_EVALUATION_DECISION_V1.md`.

[Historical] The Model, RAG, Fine-Tuning and DPO Decision Gate v1
(`docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md`) and the Final Technical Report
and Demo Package v1 (`docs/FINAL_TECHNICAL_REPORT_V1.md`,
`docs/DEMO_GUIDE_V1.md`) are complete and accepted as of 2026-07-31,
documentation-only, from baseline `2236775`. The Decision Gate reaffirms RAG
NO-GO-FOR-NOW, SFT DEFER, and DPO NO-GO-FOR-NOW, and at the time added
PROCEED (narrow) on future model-access strategy — the smallest credible next
experiment was one QuixBugs task under the static-baseline policy through the
existing protocol-1.3 live harness on the then-current free-tier route, not a
broader or paid campaign — and records that the eight-task QuixBugs baseline
is sufficient evidence for infrastructure validation only, not for model
selection, training, or generalization claims. That free-tier PROCEED
predates the operator-selected OpenCode Go subscription route recorded in
`CURRENT_AGENT_ROSTER.md` and the paired-pilot v2 contract. The Final
Technical Report synthesizes the architecture, dataset/provenance decisions,
sandbox and containment boundaries, BugsInPy's license block, the QuixBugs
methodology and results (and what they do not prove), and limitations/future
work. The Demo Guide reuses only existing entry points (the Task 9 offline
demo and the QuixBugs WSL smoke/baseline scripts); it adds no parallel demo
framework and states plainly that it validates evaluation infrastructure, not
model debugging performance. No model, RAG, training, PDB, or paid API ran to
produce any of this, and the accepted QuixBugs benchmark campaigns were not
rerun.
