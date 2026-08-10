# S1 — Debugger Interaction v2: RAW Feasibility

## Status

**BUILD (Gate A offline) — not yet authorized for live model execution.**

## Scientific Question

> After baseline failure reproduction, when bounded debugger access is directly
> available, can frozen RAW Qwen2.5-Coder-7B-Instruct use a simplified state-specific
> command interface to enter and sustain a real PDB interaction loop?

## Claims Boundary

This is a **mechanism/interface feasibility experiment**. It is NOT a strict causal
ablation against v1 where interface is claimed to be the only changed variable.

Treatment differences from v1:
1. **Simplified state-specific model-facing command interface** — a small
   line-oriented command grammar (17 commands) with state-specific command
   visibility, replacing the full typed JSON directive protocol (26 actions,
   5 directive kinds, per-action JSON-schema arguments, state allowlists,
   transition graph, hypothesis ledger operations).
2. **Debugger availability policy = `PdbPolicy.ALWAYS_ON`** instead of the
   historical `PDB_ON_UNCERTAINTY` gate. The uncertainty/hypothesis gate was
   enforced primarily in the existing `LiveModelAdapter` path; since S1 uses a
   new experiment-local `DebuggerBridgeAdapter` through the `ModelAdapter` seam,
   claiming to preserve `PDB_ON_UNCERTAINTY` would be incorrect and could create
   a hidden scientific confound. `ALWAYS_ON` means debugger access is not
   hypothesis-gated; it does not bypass baseline failure reproduction or budgets.

A successful S1 supports only: "RAW Qwen can perform bounded real debugger
interaction under the S1 simplified/direct-access treatment."

## Frozen Variables

- Model: `Qwen/Qwen2.5-Coder-7B-Instruct @ c03e6d358207e414f1eca0bb1891e29f1db0e242`
- Condition: RAW base only (no PEFT adapter)
- RAG: OFF
- Task: `curated-off-by-one-002`
- Controller: `DeterministicController` (unchanged)
- PDB backend: `PdbSession`/`PdbWorker` (unchanged)
- Verifier: `EvaluationVerifier` (unchanged)
- Evidence architecture: `project_controller_run` + `JsonlEventLogger` (unchanged)
- Generation: `do_sample=False`, `max_new_tokens=1024`, `max_input_tokens=32768`
- Quantization: 4-bit NF4 with double quantization (same as v1)
- All v1 budgets reused unchanged

### Deterministic baseline reproduction requirement

The frozen S1 claim states debugger access is directly available **after
required baseline failure reproduction**. The bridge (`bridge.py`)
deterministically enforces this: in `REPRODUCE`, the `understand`
transition is only accepted after a real `run_reproduction` observation
whose payload records `failure_reproduced is True`. Before that, the
model-facing prompt hides `understand`, and the parser rejects an emitted
`understand` with `ILLEGAL_TRANSITION`. This is an experiment-local bridge
guard; production core is unchanged.

### Source provenance

The live run binds to its exact committed source tree via `source_commit_sha`
(runtime `git rev-parse HEAD`, captured at run time — not hardcoded) and to
the exact experiment contract via `experiment_contract_sha256`, both recorded
in `run_identity` (and therefore in `evidence.json`). `--validate-only`
reports both fields and fails closed (`status: FAIL`, exit 1) if the Git HEAD
cannot be resolved. `source_baseline.audited_main_commit` in the contract
remains the audited pre-S1 baseline and is intentionally distinct from this
live-run source identity. The Git commit SHA is the immutable source-tree
identity; the owner commits the accepted S1 files before authorizing live
inference.

## Architecture

```
MODEL (next_directive)
  ↓
DebuggerBridgeAdapter (experiment-local, ModelAdapter Protocol)
  ↓ formats state-specific prompt → calls transport → captures raw text →
  ↓ parses through bridge → records telemetry with provenance → returns ModelDirective
  ↓
DeterministicController (production, unchanged)
  ↓ dispatches typed ActionDirective/TransitionDirective
  ↓
ToolRegistry (production, unchanged) → build_registry with ALWAYS_ON + interactive controls
  ↓
PdbSession / PdbWorker (production, unchanged)
  ↓
Observation → returned to controller → next snapshot.last_observation
  ↓
DebuggerBridgeAdapter renders observation into next model request (provenance-bound)
```

## Files

| File | Purpose |
|---|---|
| `bridge.py` | Deterministic parser + state-specific grammar + frame derivation + diagnosis |
| `adapter.py` | `DebuggerBridgeAdapter` (ModelAdapter impl) + telemetry + provenance |
| `transport.py` | `LocalRawQwenTransport` (RAW base, raw text always retained) |
| `runner.py` | Experiment orchestrator (`--validate-only` + `--run`) |
| `experiment_contract.json` | Frozen contract |
| `SOURCE_AUDIT.md` | Minimal-delta audit |

## Invocation

```bash
# Validate contract/identity (no model load)
python experiments/debugger_interaction_v2/runner.py --validate-only

# Live run (requires GPU + authorization; NOT run in BUILD)
python experiments/debugger_interaction_v2/runner.py --run --output-dir <dir>
```

## Gates

### Gate A — Engineering correctness (offline)
- v1 frozen tests pass (v1 + PDB backend untouched).
- Bridge parser/bridge tests pass.
- Evidence retention demonstrated (raw text on parse failure; NOT_AVAILABLE on
  transport failure; NOT_RECORDED for missing usage).
- PDB integration through controller works (real PDB observation → provenance
  binding → next request).
- Bridge is deterministic/fail-closed/non-oracular.

### Gate B — Interface feasibility (interaction loop)
1. RAW model emits a valid debugger command accepted by the controller that
   reaches the real PDB backend.
2. A real PDB observation is produced and bound into the next model request via
   `prior_observation_id` + `rendered_observation_sha256` provenance.
3. After receiving that request, the model emits a second accepted debugger
   command.
4. That second command also reaches the real PDB backend and produces another
   real observation.

### Gate C — Full dynamic trajectory (preferred)
Runtime evidence → post-debug diagnosis → patch → verifier.
RESOLVED is ideal but not required for Gate B.

## Repair Budget

- Initial implementation + offline tests (Gate A).
- One live model run.
- At most two material deterministic repair passes if engineering defects are
  demonstrated.
- STOP if bridge is correct but live run yields zero accepted debugger actions
  or zero debugger exposure.
- Do NOT open v2.1/v2.2/v2.3 protocol-hardening campaigns.