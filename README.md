# Agentic Debugging Internship

Python/PDB-first research prototype for debugger-assisted software repair.

A single controller agent uses typed deterministic tools, bounded PDB
interaction, disposable workspaces, unified-diff patching, event trajectories,
and an independent verifier. Local Application V1 is a local product surface
over that scientific system. The project investigates the path from
traditional debugging, fault localization, automated program repair,
LLM-based debugging, and repository-level software-engineering agents toward
interactive debugger-assisted agents.

## Current status

**Authority:** [`docs/project-closeout.md`](docs/project-closeout.md) for the
accepted project closeout. Level-32 artifact-boundary work is recorded as a
new treatment and does not rewrite historical runs. Roadmap notes live in
[`TODO.md`](TODO.md); the execution tracker is [`docs/project-tracker.md`](docs/project-tracker.md).

- **Local Application V1** (2026-08-16): COMPLETE — Tasks 1–8 accepted
  ([`docs/architecture/local-application-v1.md`](docs/architecture/local-application-v1.md)).
- **Real Ollama Cloud product proof** (2026-08-17): COMPLETE via
  `gpt-oss:20b-cloud`. Session `sess-20260817-103258-3d1193` on
  `curated-none-handling-001` / `pdb-on-uncertainty`: SUCCEEDED; independent
  verifier **RESOLVED**; fail-to-pass **1/1**; pass-to-pass **2/2**; PDB
  **NOT EXERCISED** (not PASS, not a failure). Product success: YES;
  debugging success: YES. The R1–R3 PDB scientific milestones are unchanged.
- **Exact-PDB single-task live repair proof** (2026-08-21): COMPLETE via
  `gpt-oss:20b-cloud` on `pdb-required-boundary-006`. The model followed the
  bounded source → hypothesis → PDB start/stack/locals/next/stop → evidence-bound
  diagnosis → unified-diff patch lifecycle. The independent verifier reported
  **RESOLVED** (F2P **1/1**, P2P **1/1**), cleanup and canonical immutability
  were true, and event replay ended in `Done`. The run used 21 logical calls,
  21 transport attempts, zero retries, and zero provider errors. This is a
  one-task lowest-rung capability proof, not a multi-task performance claim or
  a causal PDB-versus-static comparison.
- **Exact-PDB capability ladder, 12/100 rung** (2026-08-21): COMPLETE via the
  same `gpt-oss:20b-cloud` model on `pdb-required-caller-callee-007`. With
  high-thinking streaming and an activity-resetting idle watchdog, the model
  completed the caller/callee unit-contract repair in 22 calls/attempts, zero
  retries/provider errors. Verifier: **RESOLVED**, F2P **1/1**, P2P **2/2**,
  private checks true, cleanup/immutability true, replay `Done`. The tracked
  raw evidence and model patch are in
  [`experiments/pdb_capability_ladder/`](experiments/pdb_capability_ladder/README.md).
  This advanced one ordinal rung only; the later frozen 32/100 run located the
  first valid failure boundary for this treatment.
- **Exact-PDB capability ladder, 18/100 rung** (2026-08-21): COMPLETE on
  `pdb-required-multistage-units-008`. GPT-OSS used the real
  start/stack/locals/next/stop path, observed the converted intermediate value,
  and repaired the stale raw value crossing a three-function deadline pipeline.
  Verifier: **RESOLVED**, F2P **1/1**, P2P **2/2**, private checks true;
  21 calls/attempts, zero retries/provider errors, cleanup/immutability true,
  replay `Done`. Frozen evidence is in the same ladder directory.
- **Level-32 candidate-artifact boundary** (2026-08-23): COMPLETE as a
  provider-free harness repair. The new
  `workspace-derived-official-git-diff-v1` treatment preserves raw model
  patches and derives `candidate-official.patch` from the accepted workspace,
  then proves strict Git application and byte equality before Docker. Replay
  covered 18 retained candidates: 16 reached official tests, two failed closed
  during raw materialization, and none reached authoritative 5/5 F2P plus 0
  P2P failures. The historical V3 `0/5, 9/9` result is no longer treated as a
  clean semantic model failure without proven test execution; its original
  artifact remains unchanged. Durable replay evidence is in
  `analysis/level32_candidate_artifact_replay_20260823.md`; the local review
  package is `_ai-review/L32-ARTIFACT-01/`.
- **Level-32 repaired authoritative treatment** (2026-08-24): COMPLETE / RESOLVED.
  Fresh GLM 5.2 V11 under
  `workspace-derived-official-git-diff-v1` completed the exact PDB proof,
  preserved the raw patch, passed canonical semantic equivalence and official
  application, and reached `official_test_execution_proven=true` with F2P
  **5/5** and P2P failed **0/9**. V10 remains a separate preserved
  `INFRASTRUCTURE_BLOCKED` attempt; it never started provider/model execution.
  Durable V11 evidence is in
  `analysis/level32_glm52_v11_repaired_treatment_20260824.md`.
- **Level-32 repaired model matrix** (2026-08-24): COMPLETE — all 15 current
  live-verified aliases ran exactly once sequentially under the frozen repaired
  transport. GLM 5.1 and GLM 5.2 independently resolved the official task;
  GPT-OSS 120B was a semantic rejection and 12 models were protocol failures.
  No code or prompt changes were made. Detailed leaderboard:
  [`analysis/level32_repaired_model_matrix_20260824.md`](analysis/level32_repaired_model_matrix_20260824.md).
- **Nemotron 3 Nano capability probe** (2026-08-18): COMPLETE as closed
  evidence. After the multi-model Ollama Cloud generalization, selected
  `nemotron-3-nano:30b-cloud` was tested on the fixed five-task curated
  treatment under Harness V2 (`4f0a748`). All five runs were admissible;
  **1/5 RESOLVED** (V2b `sess-20260818-052524-f0287d` on
  `curated-none-handling-001`; F2P 1/1; P2P 2/2). Four tasks remained
  unresolved. PDB **NOT EXERCISED** on all five. This is not a causal
  model-strength comparison with GPT-OSS, R5, or R6. Evidence:
  [`experiments/nemotron_3_nano_model_capability_probe/`](experiments/nemotron_3_nano_model_capability_probe/README.md).
- **R1–R6 scientific closeout** stands (positive real-model dynamic
  debugging under the repaired interface; R5 clean base-14B holdout 5/5;
  project-fine-tuned 7B debugger 8/8 RESOLVED on the frozen task-disjoint
  QuixBugs validation; professor traces complete).
- Stronger R6 final holdout: **INCOMPLETE_HARDWARE_STOP**.
- Fine-tuned+RAG correctness: **CLOSED — PARTIAL / COMPUTE-CONSTRAINED / NOT_EVALUATED**.
- DPO: **CLOSED / NOT JUSTIFIED**.
- BugsInPy: **BLOCKED / license-gated**.
- Authorized Six-Case Live Campaign: **RETAIN_OPTIONAL / OWNER-AUTHORIZED**.

## Start here

| Question | Where |
| --- | --- |
| What the project does | This page; [`docs/architecture/local-application-v1.md`](docs/architecture/local-application-v1.md) |
| High-level architecture | [`docs/architecture/`](docs/architecture/) (start with Local Application V1 and the MVP plan) |
| Accepted scientific results | [`docs/results-index.md`](docs/results-index.md) |
| Experiments and evidence | [`experiments/README.md`](experiments/README.md), [`analysis/s5_final_controlled_comparison/README.md`](analysis/s5_final_controlled_comparison/README.md), [`docs/professor_traces/`](docs/professor_traces/) |
| Final technical report | [`docs/final-report.md`](docs/final-report.md) |
| Current status | [`docs/project-closeout.md`](docs/project-closeout.md) |
| Historical / archive material | [`docs/archive/`](docs/archive/), [`docs/README.md`](docs/README.md), and the archived historical README log |

Recommended first reading order: this README → `docs/results-index.md` →
`docs/project-closeout.md` → `docs/final-report.md` → the family notes linked
from the results index.

## Architecture (high level)

- `agentic_debugger/agent/` — controller, state and budget policy, model
  directives, tool registry, trajectory projection
- `agentic_debugger/runtime/` — command execution, disposable workspaces,
  patch lifecycle, tests, PDB protocol/session/worker
- `agentic_debugger/evaluation/` — task schema, independent verifier, live
  evaluation (the verifier is the correctness authority)
- `agentic_debugger/application/` — Local Application V1 session, history,
  replay, and configured-command surface
- `agentic_debugger/datasets/curated/` — five in-repo Python/pytest fixtures
- `scripts/` — operator CLIs, live-runner orchestration, transports
- `tests/` — unit, integration, and golden-trajectory suites

Python 3.11+; install with `python -m pip install -e .[test]`. Offline demo:
`python -m agentic_debugger.demo --output-dir demo-out --task-id curated-off-by-one-002`.

## Repository layout

| Path | Role |
| --- | --- |
| `agentic_debugger/` | Production package (controller, runtime, evaluation, application) |
| `tests/` | Automated validation |
| `scripts/` | Operator and benchmark CLIs |
| `docs/` | Current docs, architecture, final report, archive |
| `experiments/` | Frozen experiment families (R1–R6, S4, pilots) |
| `analysis/` | S5 controlled comparison of accepted evidence |
| `research/` | Literature notes, QuixBugs/BugsInPy manifests |
| `presentation/` | S6 static presentation snapshot |
| `docs/diary/` | Internship diary |
| `TODO.md` | Roadmap (not the status authority) |
| `AGENTS.md` | Repository-level agent rules |
| `CURRENT_AGENT_ROSTER.md` | Operational route roles (does not authorize execution) |

Generated or local-only trees (`artifacts/`, `runs/`, `operator/`,
`_ai-review/`, `.pytest_cache/`, `*.egg-info/`, `.opencode/`, `.claude/`,
`.codex/`) are ignored and are not part of the professor-facing checkout.

Historical chronological repository status notes are preserved at
[docs/archive/status/README-historical-status-log-through-2026-08-07.md](docs/archive/status/README-historical-status-log-through-2026-08-07.md).
They are **not** the current status authority — use
[docs/project-closeout.md](docs/project-closeout.md).
