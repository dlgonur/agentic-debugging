# S1 Source Audit

## Audited baseline

- Branch: `experiment/debugger-interaction-v2`
- HEAD: `1ff3571ae55fb6fb1bb9f30150cd3bfde997d9f8`
- S0 closeout commit (also `main` tip).

## Minimal delta

S1 is entirely experiment-local. It adds files under
`experiments/debugger_interaction_v2/` and new test files under
`tests/unit/` and `tests/integration/`. It imports from the production
`agentic_debugger` package but does not modify any production file.

## What is new

| File | New logic |
|---|---|
| `bridge.py` | Deterministic parser (~250 lines): line-oriented tokenizer, state-specific command filtering, frame_id/pause_generation derivation from real stack observations, `diagnosis` self-transition. Pure functions, no I/O. |
| `adapter.py` | `DebuggerBridgeAdapter` (~350 lines): `ModelAdapter` Protocol impl, state-specific prompt rendering, transport call, raw-text capture before parsing, telemetry with provenance binding, retry on parse rejection, `ModelAdapterError` on exhaustion (no fabricated directive). `ScriptedBridgeAdapter` for offline tests. |
| `transport.py` | `LocalRawQwenTransport` (~120 lines): RAW base only (no PEFT), raw text always retained. Based on v1 `LocalQwenPeftTransport` pattern with the parse-then-discard failure fixed. |
| `runner.py` | Experiment orchestrator (~300 lines): task → workspace → probe → registry → adapter → controller → trajectory → verifier → evidence → cleanup. `--validate-only` and `--run` modes. |
| `experiment_contract.json` | Frozen contract with explicit treatment differences from v1. |
| `README.md`, `SOURCE_AUDIT.md` | Documentation. |

## What is reused unchanged (production core, imported not modified)

- `agentic_debugger/agent/controller.py` — `DeterministicController`, `ControllerRunConfig`
- `agentic_debugger/agent/model_adapter.py` — `ModelAdapter` Protocol, `ControllerSnapshot`, `ActionDirective`, `TransitionDirective`, `ModelDirective` union, `ModelAdapterError`
- `agentic_debugger/agent/controller_policy.py` — `ActionName`, `ControllerBudgetLimits`, `ControllerBudgetState`, `HypothesisLedger`, `PdbPolicy`, `allowed_actions_for_state`, `is_action_allowed`
- `agentic_debugger/agent/state_machine.py` — `ControllerState`, `TRANSITION_GRAPH`, `is_transition_allowed`
- `agentic_debugger/agent/tool_registry.py` — `ToolRegistry`, `ToolSpec`, dispatch
- `agentic_debugger/agent/trajectory.py` — `project_controller_run`
- `agentic_debugger/demo/tools.py` — `build_registry`, `DemoToolContext`, `prepare_pdb_probe`, `PdbProbe`
- `agentic_debugger/demo/catalog.py` — `DemoScenario`, `RuntimeProbe`, frozen scenario for `curated-off-by-one-002`
- `agentic_debugger/runtime/pdb_session.py` — `PdbSession`
- `agentic_debugger/evaluation/verifier.py` — `EvaluationVerifier`
- `agentic_debugger/evaluation/task_schema.py` — `DebugTask`, `load_task`
- `agentic_debugger/events/logger.py` — `JsonlEventLogger`
- `agentic_debugger/events/replay.py` — `replay_events`

## What is frozen (must not change)

- `experiments/tuned_debugger_pilot_v1/` (entire directory — frozen v1 experiment)
- `experiments/raw-pilot-v1.1/` (frozen RAW baseline artifacts + model revision authority)
- `tests/unit/test_tuned_debugger_pilot.py` (v1 freeze guards including SYSTEM_PROMPT pin)
- `tests/integration/test_pdb_interactive_controls.py` (PDB runtime proof)
- `agentic_debugger/agent/controller.py` (controller — frozen contract)
- `agentic_debugger/agent/model_adapter.py` (ModelDirective union — frozen contract)
- `agentic_debugger/runtime/pdb_session.py`, `pdb_worker.py`, `pdb_protocol.py` (PDB backend — frozen)
- `agentic_debugger/evaluation/verifier.py` (verifier — correctness authority)
- `agentic_debugger/evaluation/live.py` (live runner — not used by S1)

## Treatment differences from v1 (explicit)

| Aspect | v1 | S1 |
|---|---|---|
| Model-facing interface | Full typed JSON directive protocol (26 actions, 5 directive kinds, per-action JSON-schema arguments, state allowlists, transition graph, hypothesis ledger ops) | Simplified line-oriented command grammar (17 commands) with state-specific command visibility |
| Debugger policy | `PdbPolicy.ON_UNCERTAINTY` (hypothesis-gated, enforced in `LiveModelAdapter`) | `PdbPolicy.ALWAYS_ON` (directly available after baseline reproduction, not hypothesis-gated) |
| Raw text retention | Not retained on parse failure (discarded as local variable in transport) | Always retained in telemetry, even on parse failure |
| Adapter | `LiveModelAdapter` (production, uses full JSON protocol) | `DebuggerBridgeAdapter` (experiment-local, uses bridge parser) |
| Transport parse | Transport performs `json.loads(text)` → `LiveTransportError` on failure | Transport returns raw text always; adapter/bridge performs parsing |

## Repair Pass 1 corrections (experiment-local)

Two pre-live qualification blockers were corrected entirely within the S1
experiment-local layer. Production core and v1 are unchanged.

1. **Deterministic baseline reproduction gate** (`bridge.py`): the
   `REPRODUCE → UNDERSTAND` transition is now gated on a real
   `run_reproduction` observation with `payload.failure_reproduced is True`.
   Before reproduction, `render_prompt` hides `understand` and `parse`
   rejects an emitted `understand` with `ILLEGAL_TRANSITION`. This enforces
   the frozen claim that debugger access is directly available only after
   required baseline failure reproduction. No new policy framework — it
   reuses the existing `last_observation` plumbing and the real
   `handle_run_reproduction` payload.
2. **Source provenance** (`runner.py`): `run_identity` now records
   `source_commit_sha` (runtime `git rev-parse HEAD`, not hardcoded) and
   `experiment_contract_sha256`, so the live run binds to its exact
   committed source tree and exact contract. `--validate-only` reports both
   and fails closed if HEAD is unresolvable. `source_baseline.audited_main_commit`
   in the contract remains the audited pre-S1 baseline, intentionally distinct
   from this live-run source identity.

## Scientific confound acknowledgment

Because the debugger policy differs (`ALWAYS_ON` vs `ON_UNCERTAINTY`) and the
adapter differs (`DebuggerBridgeAdapter` vs `LiveModelAdapter`), S1 cannot be
described as a strict causal ablation where interface is the only changed
variable. S1 is explicitly a mechanism/interface feasibility treatment. The
two treatment differences are recorded in the contract, README, and evidence
metadata.