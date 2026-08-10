# S2 — cp118 on the frozen D1 runtime-entry treatment

## Status

**BUILD (offline validated) — the single authorized live cp118 diagnostic.**

S2 is the next professor-critical experiment after D1: exactly ONE real
tuned-model debugger diagnostic with the definitive cp118 checkpoint under
the EXACT frozen D1 treatment.  The ONLY material model-condition change
relative to D1 is:

```
RAW Qwen2.5-Coder-7B-Instruct  →  definitive cp118 tuned checkpoint
```

## Scientific question

> Under the frozen D1 treatment (deterministic administrative phase
> navigation REPRODUCE → UNDERSTAND → RUNTIME_EVIDENCE after a real
> `failure_reproduced == true` observation, then the unchanged S1
> RUNTIME_EVIDENCE model-facing surface), can the definitive cp118 tuned
> checkpoint enter a real PDB interaction loop on `curated-off-by-one-002`?

## Frozen treatment (reused unchanged from D1/S1)

- task: `curated-off-by-one-002`;
- D1 administrative navigation (the harness may automate ONLY
  `REPRODUCE -> UNDERSTAND -> RUNTIME_EVIDENCE` after real
  `failure_reproduced == true`; it must not choose any debugger command or
  breakpoint);
- successful baseline reproduction requirement;
- existing S1 `RuntimeEvidence` model-facing grammar/prompt
  (`system_prompt_sha256` identical to D1/S1);
- controller/state machine;
- `ToolRegistry`;
- `PdbPolicy.ALWAYS_ON`;
- PDB backend (`PdbSession`/`PdbWorker`);
- verifier (`EvaluationVerifier`);
- RAG OFF;
- tokenizer/base revision `c03e6d358207e414f1eca0bb1891e29f1db0e242`;
- generation configuration (`do_sample=false`, `max_new_tokens=1024`,
  `max_input_tokens=32768`, 4-bit NF4 double quantization);
- budgets/timeouts (identical `V1_BUDGETS`);
- evidence/provenance architecture (`project_controller_run` +
  `JsonlEventLogger` + `replay_events`).

## Model condition (the ONLY change)

- Base: `Qwen/Qwen2.5-Coder-7B-Instruct` @
  `c03e6d358207e414f1eca0bb1891e29f1db0e242` (identical to D1).
- Checkpoint: **cp118** — the definitive surviving checkpoint selected
  under the accepted validation-only checkpoint-selection process
  (checkpoint-118; best surviving saved checkpoint by held-out SWE-rebench
  validation eval_loss only, `0.45070546865463257`; QuixBugs not used for
  selection).
- Loading: the established cp118 PEFT/QLoRA mechanism from the accepted
  tuned experiment (`PeftModel.from_pretrained(base, adapter_path,
  is_trainable=False)` on the identical 4-bit NF4 base load).
- The adapter directory is verified byte-exact (per-file SHA-256 + size,
  tree identity SHA-256) against the frozen `s2_contract.json` identity —
  fail closed.  If the definitive cp118 checkpoint cannot be located or
  verified exactly, STOP and report rather than substituting another
  checkpoint.
- Do NOT retrain, sweep checkpoints, change adapter weights, merge
  adapters, introduce RAG, or use another model.

The verified checkpoint used by this experiment:

- path: `C:\Users\benya\Downloads\selected-adapter-corrected-cp118-20260809T193500Z-1-001\selected-adapter-corrected-cp118`
- tree identity SHA-256: `65b5ed9a354d4b2c03ba86e2b8065118e11abab9c439cb481b5739f1b86e7c00`
- `adapter_model.safetensors` SHA-256:
  `59398e322efc9de8ba4b8952b1a06405913438314fd8dd7e5c0c0227ed535533`
- `adapter_config.json` SHA-256:
  `e90c81572a360622003e5971c8c27ac989f6f0807e24a01d7a99478467ae1c62`

This exact tree identity was already recorded in the accepted prior cp118
pilot (`run-cp118-001` run identity) — the established local checkpoint
location and load path.

## Source provenance

- S2 is based on the accepted D1 source commit
  `7bda64d04a6165eb47bfb232094976e82e1155ed`, which descends from the
  accepted S1 source commit
  `2d4bc14c16d1a7eb3e7fa72c8fbd23259cb5cc4f` (both recorded in
  `source_baseline`).
- The live run binds to its exact committed source tree via
  `source_commit_sha` (runtime `git rev-parse HEAD`) and to the exact S2
  contract via `experiment_contract_sha256`, both in `run_identity`.
- `--validate-only` reports both and fails closed (`status: FAIL`, exit 1)
  if HEAD cannot be resolved.
- The runtime Python executable + version are recorded in
  `run_identity.runtime_python` at run time.

## Architecture

```
MODEL (next_directive) — cp118 tuned checkpoint
  ↓
D1PhaseNavigationAdapter (unchanged, imported from D1)
  ↓ performs ONLY the two administrative transitions after verified
  ↓ reproduction; delegates everything else
DebuggerBridgeAdapter (S1, unchanged)
  ↓ formats state-specific prompt → calls transport → parses through bridge
LocalCp118QwenTransport (S2, model-condition-only)
  ↓ identical frozen base + verified cp118 PEFT adapter; request() inherited
DeterministicController (production, unchanged)
  ↓ dispatches typed directives
ToolRegistry (production, unchanged) → PdbSession / PdbWorker (unchanged)
  ↓
Observation → controller → D1 wrapper delegates → next model request
```

## Files

| File | Purpose |
|---|---|
| `s2_transport.py` | `LocalCp118QwenTransport` (model-condition-only subclass of the frozen S1 transport) + fail-closed `verify_adapter_identity`. |
| `s2_gates.py` | Gate B legacy (frozen computation) + Gate B strict (six-condition additive computation) + observation-status map. |
| `s2_runner.py` | Experiment orchestrator (`--validate-only` + `--run`). |
| `s2_contract.json` | Frozen S2 contract (D1 treatment + cp118 adapter identity + budgets). |
| `README.md` | This file. |
| `SOURCE_AUDIT.md` | Minimal-delta audit. |

## Invocation

```bash
# Validate contract + on-disk cp118 adapter identity (no model load)
python experiments/debugger_interaction_v2_s2_cp118/s2_runner.py \
  --validate-only \
  --adapter-path "C:\Users\benya\Downloads\selected-adapter-corrected-cp118-20260809T193500Z-1-001\selected-adapter-corrected-cp118"

# ONE live run (requires GPU + authorization; NOT run in BUILD)
python experiments/debugger_interaction_v2_s2_cp118/s2_runner.py \
  --run \
  --adapter-path "C:\Users\benya\Downloads\selected-adapter-corrected-cp118-20260809T193500Z-1-001\selected-adapter-corrected-cp118" \
  --output-dir experiments/debugger_interaction_v2_s2_cp118/runs/run-1-live-2026-08-10
```

Use the interpreter recorded by `--validate-only`
(`run_identity.runtime_python`) for the live run — do not assume an
interpreter path.

## Gates

### Gate B legacy

The repository's existing Gate-B computation, unchanged
(`experiments/debugger_interaction_v2/runner.py:_compute_gate_b`).
Administrative D1 transitions do not count.

### Gate B strict

A real iterative debugger loop requires:

1. first MODEL-AUTHORED accepted PDB command;
2. command reaches real PDB;
3. it produces a SUCCESSFUL NON-ERROR PDB observation/state;
4. exact observation is bound into the next actual model request;
5. model authors a second accepted PDB command;
6. the second command reaches real PDB and also produces a successful
   non-error PDB observation/control result.

Tool-error observations may be retained as real provenance evidence but
MUST NOT satisfy Gate B strict.  Administrative D1 transitions count toward
neither Gate B.

### Gate C

Kept separate: successful runtime evidence → model-authored post-debug
diagnosis → patch → verifier.  Patch apply, F2P, P2P and RESOLVED are
reported honestly.

## Patch policy (FirstMate amendment)

The S1-P serialization-normalization diagnostic is NOT part of the S2
treatment.  No patch normalizer is applied.  If cp118 produces a malformed
or non-applicable patch, the RAW live outcome is preserved exactly; any
serialization analysis would be a separate post-hoc diagnostic.

## Stop rule

After the single S2 live run: no rerun because output is undesirable; no
repair or prompt/interface modification after seeing the result; no further
debugger-interface campaign.  Do not infer broad fine-tuning conclusions
from one task.
