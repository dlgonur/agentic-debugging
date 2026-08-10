# S2 Source Audit

## Audited baseline

- Branch: `experiment/cp118-debugger-d1` (worktree
  `agentic-debugging-cp118-d1`)
- Accepted D1 source commit (S2 parent): `7bda64d04a6165eb47bfb232094976e82e1155ed`
- Accepted S1 source commit (grandparent): `2d4bc14c16d1a7eb3e7fa72c8fbd23259cb5cc4f`
- Current HEAD (at S2 BUILD): `7bda64d04a6165eb47bfb232094976e82e1155ed`

## Minimal delta

S2 is entirely experiment-local and additive. It adds files under
`experiments/debugger_interaction_v2_s2_cp118/` and one new test file under
`tests/unit/`. It imports from the production `agentic_debugger` package and
the frozen S1/D1 experiments but does not modify any production file, any S1
file, any D1 file, the frozen S1/D1 contracts, or the D1 Live Run evidence.

## What is new

| File | New logic |
|---|---|
| `s2_transport.py` | `LocalCp118QwenTransport(LocalRawQwenTransport)` — subclass whose ONLY change is the model condition: `__init__` verifies the cp118 adapter byte-exact (fail closed, before GPU load) and attaches it via the established `PeftModel.from_pretrained` mechanism; `request()` is inherited byte-identical from the frozen S1 transport (raw-text retention, chat template, generation call, envelope). Pure `verify_adapter_identity`/`compute_adapter_identity` used by validate-only and the transport. |
| `s2_gates.py` | `compute_gate_b_legacy` (frozen `_compute_gate_b` unchanged), `compute_gate_b_strict` (six-condition additive computation using telemetry + real trajectory observation statuses; tool-error observations never satisfy strict), `observation_status_map` (trajectory JSONL → observation statuses). |
| `s2_runner.py` | Orchestrator: `--validate-only` (contract + on-disk cp118 adapter identity + source ancestry + runtime Python; fails closed on any mismatch or unresolvable HEAD) and `--run` (unchanged S1 `run_experiment` with the unchanged D1 phase-navigation wrapper injected; evidence augmented with S2 identity, treatment, admin transitions, Gate B legacy + strict). |
| `s2_contract.json` | Frozen S2 contract: identical task/budgets/generation/interface as D1; model block changes to `adapter_applied=true` with the frozen cp118 adapter identity; D1 treatment; Gate B legacy+strict criteria; patch policy (no normalizer); STOP rule. |
| `README.md`, `SOURCE_AUDIT.md` | Documentation. |
| `tests/unit/test_s2_cp118_condition.py` | Focused offline tests (model-condition-only proof, adapter identity fail-closed, Gate B strict semantics, validate-only behavior). |

## What is reused unchanged (imported, not modified)

- `experiments/debugger_interaction_v2/bridge.py` — bridge grammar, prompt
- `experiments/debugger_interaction_v2/adapter.py` — `DebuggerBridgeAdapter`,
  telemetry/provenance machinery
- `experiments/debugger_interaction_v2/transport.py` —
  `LocalRawQwenTransport` base (model-condition-only subclass),
  `BASE_REPOSITORY`, `BASE_REVISION`, `GENERATION_CONFIG`
- `experiments/debugger_interaction_v2/runner.py` — `run_experiment`,
  `_compute_gate_b`, `_compute_gate_c`, `V1_BUDGETS`, contract helpers
- `experiments/debugger_interaction_v2_d1/d1_adapter.py` —
  `D1PhaseNavigationAdapter` (unchanged D1 harness)
- `agentic_debugger/agent/controller.py` — `DeterministicController`
- `agentic_debugger/demo/tools.py` — `build_registry`, `prepare_pdb_probe`,
  `DemoToolContext`
- `agentic_debugger/runtime/pdb_session.py`, `pdb_worker.py` — PDB backend
- `agentic_debugger/evaluation/verifier.py` — `EvaluationVerifier`
- All v1 budgets, task, generation configuration

## What is frozen (must not change)

- `experiments/debugger_interaction_v2/` (entire S1 experiment — frozen)
- `experiments/debugger_interaction_v2_d1/` (entire D1 experiment + live
  run evidence — frozen; S2 imports from it)
- `experiments/tuned_debugger_pilot_v1/` (v1 — frozen)
- `agentic_debugger/agent/controller.py`, `model_adapter.py` (frozen contracts)
- `agentic_debugger/runtime/pdb_session.py`, `pdb_worker.py`,
  `pdb_protocol.py` (PDB backend — frozen)
- `agentic_debugger/evaluation/verifier.py` (verifier — correctness authority)
- The D1 Live Run evidence (never overwritten or reinterpreted)

## The ONLY material change vs D1

The model condition: RAW Qwen2.5-Coder-7B-Instruct base → the definitive
surviving cp118 tuned checkpoint (PEFT/QLoRA adapter, byte-exact verified
against the frozen contract identity).  Everything else in the D1 treatment
— administrative phase navigation only, S1 interface/prompt, controller,
tools, PDB backend, verifier, task, budgets, generation, RAG OFF, evidence
architecture — is unchanged.

## Explicitly NOT part of S2 (FirstMate amendments)

- The S1-P serialization-normalization diagnostic is NOT imported,
  cherry-picked, or applied.  No patch normalizer.  A malformed/non-applicable
  cp118 patch is preserved exactly as the RAW live outcome.
- No automatic repair or rerun of cp118 after the one live run.
