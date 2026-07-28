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
workflow. Task 10A added a real-model evaluation harness v1, and Task 10B-R1
repaired the live protocol contracts and attempt accounting (protocol version
`1.1`). A controlled live baseline run has since been accepted with
limitation (`ACCEPT_WITH_LIMITATION`); see `docs/PROJECT_TRACKER.md` for the
full evidence record. That baseline's PDB-enabled case never opened PDB, so
it supports no claim about PDB effectiveness.

The current source priority is Task 10B-R3 — Invalid Directive Retry
Feedback v1.

Dataset expansion, broader evaluation, fine-tuning, RAG expansion beyond the
implemented tool foundations, preference optimization (DPO/RLHF), containment
hardening, and final technical reporting remain future work — see `TODO.md`
for the phase-level roadmap. This status does not represent completion of the
broader internship or research project.
