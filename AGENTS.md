# Agentic Debugger repository contract

## Scope and source of truth

This is a Python 3.11+ research product for evidence-driven repair of Python
projects. The repository contains the terminal application, repair runtime,
evaluation infrastructure, frozen experiments, and research evidence.

Use current source, tests, runtime behavior, and Git state as implementation
truth. Treat roadmap, status, report, and historical material as claims to
verify when they affect the task. Detailed product and operator contracts live
under `docs/`; load only the documents relevant to the work.

## Core architecture and correctness

- Preserve the Python/PDB-first, single-controller architecture. Do not add a
  second controller, multi-agent repair path, another debugger/runtime, or a
  parallel workspace, patch, verifier, or event system unless the task
  explicitly changes that architecture.
- Keep directives, controller state/actions, budgets, tools, and execution
  results typed and deterministic. Strict schemas and fail-closed boundaries
  are intentional: unknown, malformed, stale, contradictory, or unauthorized
  inputs and evidence must not be inferred into success.
- PDB interaction is structured and bounded by the established protocol,
  command, observation, lifecycle, and budget limits. Do not expose an
  unrestricted debugger shell or silently normalize invalid protocol data.
- Execute repairs in disposable workspaces. Canonical curated fixtures,
  external pinned sources, and frozen evidence are immutable inputs, not repair
  workspaces. Preserve project-source isolation and clean task-owned processes,
  PDB workers, workspaces, and temporary state on both success and failure.
- Model-authored patches pass through the established unified-diff parser,
  allowed-path policy, application checks, and workspace boundary. Preserve raw
  model artifacts separately when an evidence contract requires them; do not
  misrepresent a normalized or derived patch as the original model output.
- Keep failure classes honest. A model-correctable action failure may produce
  bounded, sanitized feedback only where the controller contract declares it
  recoverable. Protocol, transport, containment, verifier, cleanup, and other
  infrastructure failures must remain distinguishable and must never become a
  model success or verified repair.
- The independent verifier is the correctness authority. Controller completion,
  patch application, provider success, or model confidence is not proof. Repair
  evaluation must preserve clean-baseline reproduction, syntax/application
  checks, declared fail-to-pass and pass-to-pass tests, outcome classification,
  and cleanup evidence.

## Events and evidence

- Preserve append-only event order, schema validation, identity, deterministic
  JSON-compatible serialization, replay semantics, and chain-of-custody fields.
  The durable journal is authoritative for application history; projections,
  manifests, reports, and UI summaries must not invent or upgrade evidence.
- Do not weaken path, timeout, containment, regression, leakage, or cleanup
  checks to obtain a passing case. Never expose gold patches, hidden oracle
  fields, corrected source, or held-out test answers to an evaluated model.
- Keep infrastructure results, model-repair results, debugger-use results, and
  scientific claims distinct. Gold patches, scripted transports, provider exit
  code zero, and successful route connection validate only the boundary they
  actually exercised.
- Preserve dataset provenance, pinned revisions, licenses, task/split identity,
  prompt and configuration identity, raw-result lineage, and mandatory result
  qualifiers. Frozen experiment evidence is immutable; superseded material
  remains historical rather than current authority.
- Fine-tuning claims require a real weight update and leakage-safe held-out
  evaluation of base and tuned models under the same prompt, task, verifier,
  and metric contract. Label small studies as pilot or descriptive unless the
  evidence supports a broader claim.

## External execution, security, and data

- Live models/providers, external benchmark campaigns, and license-gated data
  acquisition or execution require explicit authorization in the current task.
  Before an authorized run, read `CURRENT_AGENT_ROSTER.md` and the exact route,
  adapter, containment, dataset, and campaign contract. Synthetic execution
  does not grant live-execution authority.
- Never substitute a provider, model, route, account, billing mode, executable,
  protocol, or fallback. Do not infer entitlement, price, or zero cost.
- Preserve the documented containment gates for external commands and datasets.
  Do not rerun accepted external campaigns merely as general regression tests.
  BugsInPy remains acquisition-, redistribution-, and execution-gated by its
  tracked license decision.
- Credentials and private data must not enter tracked files, command arguments,
  prompts, journals, events, logs, review transport, or evidence. Provider CLIs
  may read their own operator auth stores through the established adapters;
  availability probes must remain offline and presence-only. Redact secrets and
  sensitive machine paths from diagnostics.
- The repository currently grants no open-source license. Do not publish or
  redistribute repository content, external datasets, or frozen artifacts
  without applicable owner and license authority.

## Validation

Choose the smallest validation surface that can falsify the change and cover
its affected regression boundary. Broaden only when impact or observed evidence
requires it; a full suite is not the default. Diagnose observed lack of progress
before classifying a long-running test or process as stalled.

Useful repository-specific entry points:

```powershell
python -m pytest <affected-test-path-or-node> -q
python -m compileall agentic_debugger scripts
python -m agentic_debugger.demo --output-dir demo-out --task-id curated-off-by-one-002
python scripts/verify_public_evidence.py --output public-evidence-attestation.json
```

Use the deterministic demo when controller, tool, workspace, PDB, patch, or
verifier behavior needs end-to-end proof. Use the public-evidence gate when a
change affects public evidence or its release claims. Remove generated smoke
output after recording useful results. Do not contact providers or execute WSL
or external-dataset campaigns as ordinary validation.

## Git candidates and review transport

- Work on a task branch or isolated worktree when needed to protect unrelated
  user state. Local implementation and repair commits are allowed using the
  existing valid Git identity, with no AI/model/vendor attribution.
- A repair is a new commit on top of the candidate. Do not amend, reset, rebase,
  or otherwise rewrite candidate history. Do not push, merge, tag, release, or
  rewrite protected/public history.
- Candidate commits include all legitimate task-owned tracked files and exclude
  unrelated user changes, `_ai-review`, generated runs/output, caches,
  credentials, external data, environments, and model artifacts.
- `_ai-review/<task>/review.md` and `candidate.patch` are transient, ignored
  review transport. The patch must represent the complete accepted-baseline to
  candidate delta and be reversible from the candidate. Add unique supporting
  evidence only when it materially affects review.

## Instruction maintenance

Keep this file concise, durable, actionable, and repository-specific. Update it
only when instruction maintenance is explicitly in scope or Onur/FirstMate has
authorized a consequential durable rule change. Do not add task state, SHAs,
campaign snapshots, fixed test counts, machine-local paths, generic workflow
policy, or rules better enforced by code or tests.
