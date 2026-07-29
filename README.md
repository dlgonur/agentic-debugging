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
repaired the initial live protocol and accounting contracts, and Task 10B-R3
added bounded invalid-directive retry feedback in accepted commit
`1bb1d5251cc732f331ce2f5fdd163d9e46309d29` (protocol version `1.2`).

A private-runner, four-case descriptive matrix has also completed on
`curated-none-handling-001` through OpenCode Zen using
`deepseek-v4-flash-free` with variant `max`. Static policy resolved both
repetitions; the PDB-on-uncertainty policy resolved neither repetition and
terminated with underlying reason `invalid_model_response` before PDB opened.
Corrective-feedback recovery occurred in 4 of 6 observed feedback episodes.
This small, fixture-specific matrix does not establish causal PDB
effectiveness or general model reliability; see `docs/PROJECT_TRACKER.md`.

The current engineering priority is an offline audit of why the PDB policy
path continues to produce illegal or malformed directives before reaching
PDB. No larger live policy comparison is justified until a controlled
real-model path actually opens PDB.

Dataset expansion, broader evaluation, fine-tuning, RAG expansion beyond the
implemented tool foundations, preference optimization (DPO/RLHF), containment
hardening, and final technical reporting remain future work — see `TODO.md`
for the phase-level roadmap. This status does not represent completion of the
broader internship or research project.
