# S5 Requirement #23 / #24 / #25 Coverage Matrix

**Schema:** `s5-requirement-coverage-matrix-v1` (Markdown twin of `s5_requirement_coverage_matrix.json`)
**Baseline HEAD:** `acfe131a0a99b994fd3d34e520d0022191246025`
**Authoritative plan:** `Agentic_Debugging_Master_Execution_Plan_2026-08-11_S5_CURRENT.md`

This matrix assesses the project's debugger requirements against accepted evidence. It keeps eight columns clearly separated so that engineering capability is never read as positive model-behavior evidence, and no negative finding is converted into success.

## Boundary clauses (apply to every row)

- Deterministic PDB backend tests are **NOT** evidence the model used the debugger successfully.
- One accepted debugger command is **NOT** successful debugger interaction when the backend observation was an error/rejection.
- Debugger-command inability must **NOT** be inferred from S1 alone (S1 had an administrative phase-navigation failure); D1/S2 provide the stronger bounded runtime-entry evidence.
- The static QuixBugs gcd verifier RESOLVED does **NOT** demonstrate debugger use.
- "Engineering capability exists" must not be read as positive model-behavior evidence.
- No negative finding is converted into success.

---

## Requirement #23 — Fine-tuned model generates debugger commands and interprets debugger output

| Column | Assessment | Evidence |
|---|---|---|
| Plumbing/backend capability exists? | **YES** | PDB backend supports breakpoint/continue/step/next/stack/locals/safe-expr; controller emits typed directives; transport parses model text into commands. `agentic_debugger/runtime/pdb_*`; `agentic_debugger/agent/` |
| Deterministic engineering evidence? | **YES** | Deterministic/scripted PDB trajectories pass; golden reachability capture `REACHABILITY_CASE_PASSED` with 2 successful PDB observations. `tests/`; `tests/golden_trajectories/data/quixbugs-gcd-pdb-reachability-captured-result.json` |
| Real RAW model evidence | D1: model-authored PDB command `break 20`; controller accepted it and translated it to the internal `start_pdb_session` directive; backend dispatched `start_pdb_session`. Backend reached: **YES**. Result: **tool_error** because the requested breakpoint line was outside the 19-line probe. Successful non-error PDB observations: **0**. No second successful model-authored debugger interaction. (The model authored `break 20`; the model did NOT author the literal `start_pdb_session` — that is the controller-translated internal action / backend dispatch.) | `experiments/debugger_interaction_v2_d1/runs/run-1-live-2026-08-10/evidence.json` |
| Real cp118 evidence | S2: cp118 authored one PDB command `continue` (model-authored text). Backend result: **rejected** because no active PDB session existed. Successful non-error PDB observations: **0**. No second PDB command. (The internal dispatch action for `continue` is NOT_RECORDED in-repo.) | Master plan §S2 (lines 876–902); no in-repo frozen run-result |
| Positive real-model end-to-end success? | **NO** | The infrastructure can return debugger observations to the model, but real model interpretation of a successful debugger observation has NOT been demonstrated because neither RAW nor cp118 obtained a successful non-error observation. |
| Bounded negative experimental result? | **YES** | Both RAW (D1) and cp118 (S2) each emitted exactly one model-authored PDB command; neither produced a successful non-error observation nor a second accepted command. Gate B strict FAIL for both. This is a bounded negative result, not "no evidence". |
| Honest evidence claim | Engineering supports debugger-command generation and backend observation return. Both the RAW and the fine-tuned (cp118) models each authored one real PDB command that reached the real backend, but neither obtained a successful non-error debugger observation, so real model interpretation of runtime evidence is not demonstrated. | — |
| Remaining positive-demo gap | A working multi-turn debugger loop with ≥2 accepted model-authored PDB commands and ≥1 successful non-error observation from a real model, followed by model interpretation of that observation. | — |

---

## Requirement #24 — Breakpoint / variables / stack / step interaction

| Column | Assessment | Evidence |
|---|---|---|
| Plumbing/backend capability exists? | **YES** | Backend supports break, continue, step, next, stack, frame locals, safe expression evaluation, cleanup, replay, bounded post-mortem. `agentic_debugger/runtime/pdb_*` |
| Deterministic engineering evidence? | **YES** | Deterministic/scripted PDB trajectories exercise the full grammar. Golden reachability capture: 2 successful PDB observations (verdict `REACHABILITY_CASE_PASSED`; patch verification explicitly out of scope). `tests/`; `tests/golden_trajectories/data/quixbugs-gcd-pdb-reachability-captured-result.json` |
| Real RAW model evidence | D1: only `break 20` (rejected by backend — line outside 19-line probe). No variables, stack, step, next, or locals reached the backend from the model. `get_source_window` observations are source reads, not PDB state inspection. | `evidence.json` (D1) |
| Real cp118 evidence | S2: only `continue` (rejected — no active PDB session). No variables, stack, step, next, or locals reached the backend from the model. | Master plan §S2 |
| Positive real-model end-to-end success? | **NO** | No real-model successful breakpoint → observation → step/locals/stack sequence occurred in either condition. |
| Bounded negative experimental result? | **YES** | Backend capability and deterministic tests demonstrate the full grammar; real-model positive use was not achieved. Both model conditions' only PDB command was rejected by the backend. |
| Honest evidence claim | The backend and deterministic tests demonstrate the full breakpoint/variables/stack/step grammar. Real-model evidence is limited to one rejected breakpoint (RAW) and one rejected continue (cp118); no real-model variables/stack/step/locals interaction occurred. | — |
| Remaining positive-demo gap | A real-model successful breakpoint followed by an observation, then a step/locals/stack command that produces a successful non-error observation. | — |

---

## Requirement #25 — Debugger → patch → tests/verifier

| Column | Assessment | Evidence |
|---|---|---|
| Plumbing/backend capability exists? | **YES** | Controller path, unified-diff patch lifecycle, independent verifier, F2P/P2P/RESOLVED all implemented. `agentic_debugger/agent/`; `agentic_debugger/runtime/`; `agentic_debugger/evaluation/` |
| Deterministic engineering evidence? | **YES** | Offline deterministic demo, verifier, and golden trajectories run the full path. `agentic_debugger/demo/`; `tests/golden_trajectories/` |
| Real RAW model evidence | D1: no post-debug diagnosis (`post_debug_diagnoses=[]`), no patch (`candidate_patch=null`), verifier not executed (`gate_c.resolved=false`, `verifier_executed=false`). | `evidence.json` (D1) |
| Real cp118 evidence | S2: no diagnosis, no patch, verifier not executed (Gate C FAIL). | Master plan §S2 |
| Positive real-model end-to-end success? | **NO** | No debugger-informed patch reached the verifier from a real model. (A static real-provider model→patch→verifier path reached RESOLVED on QuixBugs gcd F2P 5/5 P2P 1/1, but that path does **NOT** demonstrate debugger use — it is a separate `static_verifier_success` axis.) |
| Bounded negative experimental result? | **YES** | The debugger→patch→verifier path is engineered and is demonstrated by a static (non-debugger) real-provider run (QuixBugs gcd RESOLVED). No debugger-informed patch reached the verifier from a real model in either D1 (RAW) or S2 (cp118). |
| Honest evidence claim | The debugger→patch→verifier path is engineered and demonstrated by a static (non-debugger) real-provider run that reached verifier RESOLVED (QuixBugs gcd, F2P 5/5, P2P 1/1). No debugger-informed patch reached the verifier from a real model. | — |
| Remaining positive-demo gap | A real-model debugger-informed patch that reaches the independent verifier (F2P/P2P/RESOLVED reported). | — |

---

## Summary verdict

- **Engineering capability:** all three requirements have backend + deterministic plumbing.
- **Real-model positive end-to-end:** **NOT achieved** for any of #23/#24/#25.
- **Bounded negative result:** present for all three (D1 RAW + S2 cp118 each produced one rejected PDB command and zero successful observations).
- **If a positive demonstration is required** rather than a negative experimental result, the unfulfilled gaps are the "remaining positive-demo gap" rows above; none can be filled from the accepted frozen evidence in this repository.
