# D1 — Forced-Runtime-Entry Sanity Diagnostic

## Status

**BUILD (offline validated) — not yet authorized for live model execution.**

This is the ONE and ONLY post-S1 STOP-gate harness/interface sanity
diagnostic. It is NOT S1 v2.1, NOT a new protocol campaign, NOT a
prompt-optimization campaign, and NOT a general repair pass.

The current S1 Live RAW Run 1 is frozen negative evidence and remains
unchanged.

## Scientific Question

> After successful baseline failure reproduction, if administrative
> controller phase navigation is handled deterministically and frozen RAW
> Qwen is placed directly at the existing RUNTIME_EVIDENCE interaction
> boundary, can it use the existing simplified debugger command surface to
> enter a real PDB interaction loop?

## D1 Treatment — EXACTLY ONE CHANGE

D1 changes only **administrative phase navigation after verified
reproduction**:

1. Normal RAW model call in `REPRODUCE`.
2. The model must successfully execute the existing `reproduce` action.
3. Confirm from the REAL observation: `failure_reproduced == true`.
4. Only after that verified observation, the experiment-local D1 harness
   deterministically performs the existing legal administrative transitions
   `REPRODUCE -> UNDERSTAND -> RUNTIME_EVIDENCE`.
5. The RAW model is then called using the EXISTING S1 RUNTIME_EVIDENCE
   state-specific command surface.
6. From that point onward, model debugger/action choices remain
   model-authored.

The D1 harness never:

- chooses source / break / stack / locals / print / step / next / continue /
  diagnosis / patch;
- chooses a breakpoint for the model;
- injects runtime evidence;
- modifies PDB observations.

If successful reproduction does not occur: STOP. No forced runtime entry.

## Frozen Variables

Identical to S1 Live Run 1:

- Model: `Qwen/Qwen2.5-Coder-7B-Instruct @ c03e6d358207e414f1eca0bb1891e29f1db0e242`
- Condition: RAW base only (no PEFT adapter), RAG OFF
- Task: `curated-off-by-one-002`
- Controller: `DeterministicController` (unchanged)
- PDB backend: `PdbSession`/`PdbWorker` (unchanged)
- Verifier: `EvaluationVerifier` (unchanged)
- Evidence architecture: `project_controller_run` + `JsonlEventLogger` (unchanged)
- Generation: `do_sample=False`, `max_new_tokens=1024`, `max_input_tokens=32768`
- Quantization: 4-bit NF4 with double quantization (same as v1)
- All v1 budgets reused unchanged
- `PdbPolicy.ALWAYS_ON`, bridge grammar, RuntimeEvidence prompt — unchanged

## Source Provenance

- D1 is based on the accepted S1 source commit
  `2d4bc14c16d1a7eb3e7fa72c8fbd23259cb5cc4f` (recorded as
  `source_baseline.s1_accepted_parent_commit` in the contract).
- The live run binds to its exact committed source tree via
  `source_commit_sha` (runtime `git rev-parse HEAD`) and to the exact D1
  contract via `experiment_contract_sha256`, both in `run_identity`.
- `--validate-only` reports both and fails closed (`status: FAIL`, exit 1)
  if HEAD cannot be resolved.
- The runtime Python executable + version are recorded in
  `run_identity.runtime_python` at run time (not hardcoded).

## Architecture

```
MODEL (next_directive)
  ↓
D1PhaseNavigationAdapter (experiment-local wrapper)
  ↓ performs ONLY the two administrative transitions after verified
  ↓ reproduction; delegates everything else
DebuggerBridgeAdapter (S1, unchanged)
  ↓ formats state-specific prompt → calls transport → parses through bridge
DeterministicController (production, unchanged)
  ↓ dispatches typed directives
ToolRegistry (production, unchanged) → PdbSession / PdbWorker (unchanged)
  ↓
Observation → controller → D1 wrapper delegates → next model request
```

## Files

| File | Purpose |
|---|---|
| `d1_adapter.py` | `D1PhaseNavigationAdapter` — the only automated behavior in D1. |
| `d1_runner.py` | Experiment orchestrator (`--validate-only` + `--run`). |
| `d1_contract.json` | Frozen D1 contract (source ancestry + D1 treatment). |
| `README.md` | This file. |
| `SOURCE_AUDIT.md` | Minimal-delta audit. |

## Invocation

```bash
# Validate contract/identity (no model load)
python experiments/debugger_interaction_v2_d1/d1_runner.py --validate-only

# Live run (requires GPU + authorization; NOT run in BUILD)
python experiments/debugger_interaction_v2_d1/d1_runner.py --run --output-dir <dir>
```

Use the interpreter recorded by `--validate-only` (`run_identity.runtime_python`)
for the live run — do not assume an interpreter path.

## Gates

### Gate B — Interface feasibility (interaction loop)

1. RAW model emits a valid debugger command accepted by the controller that
   reaches the real PDB backend.
2. A real PDB observation is produced and bound into the next model request
   via `prior_observation_id` + `rendered_observation_sha256` provenance.
3. After receiving that request, the model emits a second accepted debugger
   command.
4. That second command also reaches the real PDB backend and produces
   another real observation.

A model-authored `source` command before `break` is allowed. Administrative
transitions inserted by the D1 harness DO NOT count as model debugger
commands (they carry `action_name=None` and `parse_result.status=
"administrative"`, so the existing Gate-B filter cannot count them).

### Gate C — Full dynamic trajectory

Runtime evidence → post-debug diagnosis → patch → verifier. RESOLVED is
ideal but not required for Gate B.

## STOP Rule

After the eventual ONE D1 live run:

- no command hiding campaign;
- no prompt wording campaign;
- no alternative grammar;
- no larger token budget;
- no second curated task;
- no v2.1/v2.2;
- no another debugger-policy variation.

D1 is the final S1 interface sanity diagnostic.
