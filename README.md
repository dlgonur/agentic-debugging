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

## Current status

An MVP agentic debugging implementation is accepted through Task 9: a single
controller agent, typed deterministic tools (file read, code search, test
run, patch apply), PDB session/runtime skills, and a verifier-backed patch
workflow. Task 10A added the real-model evaluation harness. Task 10B-R1
repaired the initial live protocol and accounting contracts, Task 10B-R3
added bounded invalid-directive retry feedback, and Task 10B-R5 completed the
policy-scoped contract repair in accepted source/merge commit
`63fa27cc4d30490b9770ead3ce14b4b6d3ddf222` (protocol version `1.3`).

A private-runner, four-case descriptive matrix has also completed on
`curated-none-handling-001` through OpenCode Zen using
`deepseek-v4-flash-free` with variant `max`. Static policy resolved both
repetitions; the PDB-on-uncertainty policy resolved neither repetition and
terminated with underlying reason `invalid_model_response` before PDB opened.
Corrective-feedback recovery occurred in 4 of 6 observed feedback episodes.
This small, fixture-specific matrix does not establish causal PDB
effectiveness or general model reliability; see `docs/PROJECT_TRACKER.md`.

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
SFT is DEFER, and DPO/preference optimization is NO-GO-FOR-NOW. Dataset
execution, broader evaluation, containment hardening, and final technical
reporting remain future work — see `TODO.md` and
`docs/DATASET_EVALUATION_DECISION_V1.md`.
