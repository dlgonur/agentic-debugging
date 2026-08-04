# AGENTS.md

## 1. Project

This repository is a Python/PDB-first research prototype for debugger-assisted software repair.

Its accepted core is a single controller agent with typed deterministic tools, bounded PDB interaction, disposable workspaces, unified-diff patching, event trajectories, and an independent verifier. The project also contains dataset adapters, QuixBugs evaluation infrastructure, live-model transport and campaign tooling, research material, internship documentation, and future model-training work.

The active roadmap lives in `TODO.md` and `docs/PROJECT_TRACKER.md`. This file defines stable repository rules; it does not freeze the current task priority.

## 2. Roles and handoff

- Onur is the project owner, final product decision-maker, and final Git operator.
- ChatGPT FirstMate reviews plans, candidate changes, validation evidence, and `_ai-review` packages.
- The coding agent owns repository investigation, technical planning, implementation, targeted validation, and the task handoff.
- Resolve routine technical decisions from the repository without asking Onur.
- In PLAN, report repository evidence that contradicts supplied assumptions.
- In BUILD, follow verified source, tests, MCP, and runtime evidence; record material differences in `_ai-review`.

## 3. Instruction use

The current task prompt defines the requested outcome, scope, and any authorization for external execution. This root `AGENTS.md` defines repository-wide rules. A nearer nested `AGENTS.md`, if one is added later, may add subtree-specific rules without repeating this file.

Use current source, tests, runtime behavior, and Git state as technical reality. Treat TODOs, trackers, reports, and agent summaries as context and claims that may need verification.

Do not follow a technically false assumption merely because it appears in a prompt or old document. Implement what the live repository evidence supports and explain any material difference. Explicit authorization gates for providers, credentials, external benchmarks, destructive operations, and Git remain binding.

## 4. Read first

Before planning or editing, read:

1. this `AGENTS.md`;
2. the current task prompt;
3. the source and tests directly related to the task;
4. `README.md` for project status and entry points;
5. `TODO.md` and `docs/PROJECT_TRACKER.md` when the task concerns roadmap or project status;
6. `CURRENT_AGENT_ROSTER.md` and the relevant operator contract before any provider or model execution;
7. the relevant design or operator guide under `docs/` for dataset, live-runner, transport, containment, or experiment work.

Read additional files when they help recover the real implementation path. Do not spend time rereading unrelated historical material.

## 5. Repository map

- `agentic_debugger/agent/` — controller, state and budget policy, model directives, tool registry, and trajectory projection.
- `agentic_debugger/runtime/` — command execution, disposable workspaces, patch lifecycle, test execution, PDB protocol/session/worker, and execution contracts.
- `agentic_debugger/evaluation/` — task schema, independent verifier, result taxonomy, trusted-local evaluation, and opt-in live-model evaluation.
- `agentic_debugger/events/` — event schemas, logging, replay, and golden trajectory contracts.
- `agentic_debugger/demo/` — deterministic offline end-to-end demonstration over the curated fixtures.
- `agentic_debugger/datasets/curated/` — five small in-repo Python/pytest architecture fixtures.
- `agentic_debugger/quixbugs/` — QuixBugs manifests, adapter, containment, and PDB preparation.
- `agentic_debugger/bugsinpy/` — BugsInPy metadata, license/preflight, WSL, and adapter work.
- `scripts/` — operator CLIs, live-runner orchestration, OpenCode transport/adapter, and benchmark utilities.
- `tests/unit/`, `tests/integration/`, `tests/golden_trajectories/` — automated validation surfaces.
- `docs/` — accepted designs, operator guides, decisions, tracker, demo guide, and technical report.
- `research/` — reviewed notes, manifests, source records, and synthesis artifacts.
- `diary/` — internship diary material.
- `operator/`, `runs/`, `outputs/`, `artifacts/`, `checkpoints/`, `models/`, and `_ai-review/` — local or review output; do not commit.

## 6. Environment and standard commands

Run commands from the repository root. The package requires Python 3.11 or newer.

Install the project and test dependency:

```powershell
python -m pip install -e .[test]
```

Collect tests without executing them:

```powershell
python -m pytest --collect-only -q
```

Run focused tests:

```powershell
python -m pytest <test-path-or-node> -q
```

Compile relevant Python modules when syntax/import coverage adds value:

```powershell
python -m compileall agentic_debugger scripts
```

Run the deterministic offline demo when the task affects the controller, tools, workspace, PDB, patch, or verifier path:

```powershell
python -m agentic_debugger.demo --output-dir demo-out --task-id curated-off-by-one-002
```

Use `python -m agentic_debugger.demo --output-dir demo-out --strict` only when a broader accepted demo check is justified. Remove task-created output after evidence is captured.

Do not run the full pytest suite by default. Use it only when the affected surface cannot be bounded reliably or the task explicitly concerns broad compatibility, CI, the test runner, or release readiness.

## 7. Architecture and correctness invariants

- The project is Python/PDB-first. Do not add GDB, LLDB, another language runtime, or a multi-agent architecture unless the active task explicitly changes that accepted scope.
- Preserve the single-controller design and the existing typed directive, state, action, budget, and tool contracts unless the task intentionally changes a public contract.
- Repair or extend the established path. Do not create a parallel controller, verifier, patcher, workspace system, live runner, or demo framework when an accepted path already exists.
- The independent verifier is the correctness authority. A controller, model, runner, or agent claim is not proof of success.
- For bug-repair and evaluation cases, verify from a clean baseline: reproduce the original failure, apply the candidate in a disposable workspace, syntax-check, run declared fail-to-pass and pass-to-pass checks, classify the result, and clean all task-owned state.
- Never edit canonical curated fixtures or external pinned sources directly during a case. Work in disposable copies and prove canonical immutability when the path requires it.
- In the debugger/evaluation product path, model-generated candidate patches use the existing unified-diff and allowed-path mechanisms. Do not weaken path, syntax, timeout, cleanup, or regression checks to obtain a pass.
- Strict schemas and fail-closed boundaries are deliberate project style. Unknown, malformed, stale, contradictory, or unauthorized evidence must not be converted into success by defaults or inference.
- Preserve deterministic and JSON-compatible records where existing contracts require them. Do not silently normalize invalid protocol or evidence objects.
- Cleanup is part of correctness. Workspaces, subprocesses, PDB workers, temporary files, and task-touched state must be cleaned on success and failure, or the remaining state must be reported precisely.

## 8. PLAN and BUILD

### PLAN

When asked to plan:

- inspect the live repository before proposing changes;
- use source, tests, bounded diagnostics, and codebase-memory-mcp when useful;
- determine the established path, real failure chain, or remaining TODO delta;
- state evidence that contradicts the task assumptions;
- return a concise implementation and validation plan in the message;
- do not modify tracked files or create `_ai-review`.

Existing targeted tests or non-writing diagnostics may be run when they are needed to establish the actual state.

### BUILD

Continue in the same agent session after plan approval.

- Implement the complete bounded outcome using the technically correct repository path.
- Treat the approved plan as informed direction, not a rigid edit prescription.
- Make ordinary design, file, helper, and test decisions independently.
- If source, tests, MCP, or runtime evidence disproves part of the plan, follow the verified evidence and complete the task.
- Record material divergence and its evidence-based reason in the task report.
- Do not stop for ordinary implementation differences. Stop only when the work cannot be completed honestly without a real owner decision, unavailable credential, prohibited external action, or destructive/production operation.

## 9. Codebase memory

Read-only use of the installed `codebase-memory-mcp` service is pre-approved for repository analysis in this project.

Use it when it improves understanding of:

- symbols, callers, dependencies, and execution paths;
- architectural boundaries and existing implementation locations;
- cross-file or cross-layer change impact;
- likely test and regression surfaces.

Create or refresh the project memory when necessary. Prefer focused questions over generic whole-repository reports. MCP supports codebase understanding; current source, tests, and runtime behavior remain the technical evidence.

## 10. Change discipline

- Keep one coherent task coherent; do not split it into artificial sub-campaigns.
- Change every file genuinely required by the outcome. There is no arbitrary file-count limit.
- Avoid unrelated cleanup, formatting churn, speculative hardening, and broad rewrites.
- Preserve unrelated working-tree changes and report pre-existing dirty state.
- Update tests when the changed behavior requires coverage.
- Update documentation, TODO, tracker, or diary only when the task requires it or the accepted project state materially changes.
- Do not mark a TODO complete because code appears to exist; verify the accepted behavior and evidence.
- Keep prompts, scripts, comments, and reports concise and decision-focused.

## 11. Validation and smoke

Use the smallest validation set that can establish the requested outcome:

1. focused tests for the changed behavior;
2. directly affected subsystem tests when needed;
3. one meaningful failure or negative path when relevant;
4. compile, CLI, runtime, generated-artifact, containment, or verifier smoke when the task depends on it.

Do not repeatedly run unchanged tests without decision value. Do not run accepted external campaigns merely to increase test volume.

This repository is primarily CLI and artifact based, not a GUI application. Use real command output, JSON, Markdown reports, trajectories, logs, generated patches, and verifier records as smoke evidence. Capture screenshots only when the task produces a visual notebook, report, UI, or presentation artifact for which visual inspection adds real value.

## 12. External execution and operator boundaries

### Live models and providers

Live provider/model execution is off by default and requires explicit authorization in the current task. Before such work, read `CURRENT_AGENT_ROSTER.md` and the exact route, adapter, authorization, and campaign documents.

Do not silently substitute a model, provider, route, account, billing mode, executable, protocol, or fallback. Do not infer subscription, entitlement, price, or zero cost. Keep credentials outside tracked source and review packages.

Synthetic transports and test doubles do not authorize real provider contact.

### QuixBugs

The accepted single-task and eight-task QuixBugs gold-patch runs validate infrastructure only. They do not prove model debugging ability or PDB effectiveness.

Do not casually rerun accepted WSL campaigns. Use the recorded evidence unless a task has a clear reason and explicit authorization to execute the real WSL/Bubblewrap path.

Preserve the accepted containment boundary: Windows host, WSL2 `Ubuntu-22.04`, Bubblewrap isolation, `prlimit` resource controls, pinned source and environment outside `/mnt/c`, and disposable owned run workspaces. Do not weaken a failing containment gate.

### BugsInPy

BugsInPy source acquisition and execution remain license-gated. Metadata and preflight work may continue, but do not acquire, redistribute, or execute BugsInPy tasks unless a tracked license/operation authority and the current task explicitly clear that action.

### Destructive and production operations

Do not deploy, modify live data, perform destructive cleanup/migration, install global system components, alter WSL/OS configuration, or access private credentials without explicit owner approval.

## 13. Research and experiment integrity

- Reviewed tracked artifacts are authoritative research inputs; raw AI research is not.
- Do not broaden a coding task into an open-ended literature or benchmark campaign.
- Keep infrastructure results, model results, and scientific claims distinct.
- A gold patch, scripted model stand-in, synthetic transport, successful route connection, or provider exit code is not a successful model repair result.
- Do not expose gold patches, hidden oracle fields, corrected source, or test answers to an evaluation model.
- Preserve dataset provenance, pinned revisions, license status, environment identity, prompt/config identity, and raw result evidence when an experiment depends on them.

For fine-tuning or Colab work:

- perform a real weight update such as LoRA or QLoRA; prompt changes alone are not fine-tuning;
- create leakage-safe train/validation/test splits and keep held-out gold answers unavailable to the model;
- run the base and fine-tuned model with the same prompt contract, held-out tasks, verifier, and metrics;
- retain training configuration, logs, adapter artifacts, dataset transformation records, and evaluation outputs;
- label small experiments as pilot or descriptive and do not claim generalization beyond the evidence;
- integrate results with the existing task schema and verifier path where practical rather than creating a second correctness system.

## 14. Review handoff

After BUILD and useful validation, remove stale output for the same task and create:

```text
_ai-review/<task-id>/
_ai-review/<task-id>-FIRSTMATE.zip
```

A normal package contains only useful review material, typically:

```text
agent-report.md
candidate-state.json
candidate.patch
changed-files/
validation.md          # only when useful
manual-smoke.md        # only when smoke was executed
logs/ or generated-artifacts/  # only when relevant
mcp-impact.md          # only when it adds review value
```

Do not create empty files, empty directories, duplicate reports, stale repair evidence, caches, environments, models, datasets, credentials, or unrelated source copies.

`candidate.patch` is the canonical tracked candidate delta. `changed-files/` is a review convenience. The package does not replace the full repository.

Keep `agent-report.md` short and factual:

- completed outcome;
- changed paths;
- important implementation decisions;
- exact useful validation and smoke results;
- failures or blocked checks;
- material plan divergence and the repository evidence for it;
- known limitations;
- package path and current Git state.

## 15. Git

For normal PLAN, BUILD, and repair work:

- do not commit;
- do not merge;
- do not push;
- leave the candidate available for FirstMate review.

When the current prompt explicitly enables overnight Codex Goal Mode, local commits are allowed at clear TODO/checkpoint boundaries. Merge and push remain prohibited.

Never stage or commit `_ai-review`, review ZIPs, `operator/`, secrets, local environments, caches, model checkpoints, experiment output, external datasets, build output, or unrelated changes.

## 16. Owner decisions

Ask Onur only when the repository and supplied contract cannot resolve a genuine owner boundary, including:

- product behavior or academic scope;
- public compatibility or dataset/experiment claims;
- a new material dependency, paid service, or provider route;
- credentials, private data, or production access;
- licensing or redistribution authority;
- destructive operations, deployment, merge, push, release, or history rewrite.

Do not ask about routine implementation details.
