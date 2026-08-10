# D1 Source Audit

## Audited baseline

- Branch: `experiment/debugger-interaction-v2`
- S1 accepted source commit (D1 parent): `2d4bc14c16d1a7eb3e7fa72c8fbd23259cb5cc4f`
- Current HEAD (at D1 BUILD): `2d4bc14c16d1a7eb3e7fa72c8fbd23259cb5cc4f`

## Minimal delta

D1 is entirely experiment-local and additive. It adds files under
`experiments/debugger_interaction_v2_d1/` and one new test file under
`tests/unit/`. It imports from the production `agentic_debugger` package and
the frozen S1 experiment but does not modify any production file, any S1
file, the frozen S1 experiment contract, or the S1 Live Run 1 evidence.

## What is new

| File | New logic |
|---|---|
| `d1_adapter.py` | `D1PhaseNavigationAdapter` (~160 lines): a `ModelAdapter`-Protocol wrapper that performs ONLY the two existing legal administrative transitions `REPRODUCE -> UNDERSTAND -> RUNTIME_EVIDENCE` after a real observation with `failure_reproduced == true`, then delegates everything else to the inner S1 adapter. Administrative transitions are recorded with `d1_authorship="administrative"`, `raw_response_status="administrative_navigation"`, `parse_result.status="administrative"`, and no `action_name` — they cannot be counted as model debugger commands by the existing Gate-B filter. |
| `d1_runner.py` | Experiment orchestrator (~200 lines): `--validate-only` (contract + identity + source ancestry + runtime Python, fails closed on unresolvable HEAD) and `--run` (reuses the S1 `run_experiment` path with the D1 wrapper injected as the adapter, then augments evidence with D1 identity/treatment/admin records). |
| `d1_contract.json` | Frozen D1 contract: `source_baseline.s1_accepted_parent_commit` = the accepted S1 commit; identical model/task/budgets/generation as S1; D1 treatment block; Gate B/C criteria; STOP rule. |
| `README.md`, `SOURCE_AUDIT.md` | Documentation. |
| `tests/unit/test_d1_phase_navigation.py` | Focused offline tests (positive loop, negative no-entry, one-time navigation). |

## What is reused unchanged (imported, not modified)

- `experiments/debugger_interaction_v2/bridge.py` — bridge grammar, prompt,
  `_baseline_reproduction_succeeded` gate
- `experiments/debugger_interaction_v2/adapter.py` — `DebuggerBridgeAdapter`,
  `ScriptedBridgeAdapter`, telemetry/provenance machinery
- `experiments/debugger_interaction_v2/transport.py` — `LocalRawQwenTransport`
  (RAW base, raw text always retained)
- `experiments/debugger_interaction_v2/runner.py` — `run_experiment`,
  `_compute_gate_b`, `_compute_gate_c`, contract helpers
- `agentic_debugger/agent/controller.py` — `DeterministicController`
- `agentic_debugger/agent/state_machine.py` — `TRANSITION_GRAPH`,
  `is_transition_allowed`
- `agentic_debugger/agent/model_adapter.py` — `TransitionDirective`,
  `ControllerSnapshot`, `ModelAdapter` Protocol
- `agentic_debugger/demo/tools.py` — `build_registry`, `prepare_pdb_probe`,
  `DemoToolContext`
- `agentic_debugger/runtime/pdb_session.py`, `pdb_worker.py` — PDB backend
- `agentic_debugger/evaluation/verifier.py` — `EvaluationVerifier`
- All v1 budgets, task, generation configuration

## What is frozen (must not change)

- `experiments/debugger_interaction_v2/` (entire S1 experiment — frozen,
  including `experiment_contract.json` and Live Run 1 evidence)
- `experiments/tuned_debugger_pilot_v1/` (v1 — frozen)
- `experiments/raw-pilot-v1.1/` (RAW baseline artifacts + model revision authority)
- `agentic_debugger/agent/controller.py`, `model_adapter.py` (frozen contracts)
- `agentic_debugger/runtime/pdb_session.py`, `pdb_worker.py`,
  `pdb_protocol.py` (PDB backend — frozen)
- `agentic_debugger/evaluation/verifier.py` (verifier — correctness authority)
- `AI_REVIEW/s1_debugger_interaction_v2_build_2026-08-10/live_raw_run_1/`
  (frozen Live Run 1 evidence)

## The only automated behavior in D1

Two administrative `TransitionDirective`s (REPRODUCE->UNDERSTAND and
UNDERSTAND->RUNTIME_EVIDENCE), emitted deterministically by
`D1PhaseNavigationAdapter` only after a real `run_reproduction` observation
with `failure_reproduced == true`. No debugger command, no breakpoint, no
runtime evidence, no PDB observation is automated, injected, or modified.

## Deviation from S1 (intentional, recorded in contract)

S1 left REPRODUCE->UNDERSTAND->RUNTIME_EVIDENCE to the model, which fixated
on the `reproduce` self-loop (frozen Live Run 1: `reproduce` on all 6 calls,
0 accepted PDB commands, Gate B FAIL). D1 automates ONLY these two
administrative phase transitions after verified reproduction. Everything
else — model, prompt, grammar, controller, tools, PDB, verifier, budgets —
is unchanged.
