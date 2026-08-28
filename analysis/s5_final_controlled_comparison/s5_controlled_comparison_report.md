# S5 Final Controlled Comparison Report

**Schema:** `s5-controlled-comparison-v1` (human-readable companion to `s5_comparison_ledger.json`)
**Baseline HEAD:** `acfe131a0a99b994fd3d34e520d0022191246025`
**Branch:** `analysis/s5-final-controlled-comparison-v1`
**Authoritative plan:** `Agentic_Debugging_Master_Execution_Plan_2026-08-11_S5_CURRENT.md`
**Stale plan (ignored):** `Agentic_Debugging_Master_Execution_Plan_2026-08-10_S4_CURRENT.md`

This is the **canonical controlled comparison of the project's accepted evidence**. It is synthesis, not another model campaign. No model was run, S4 was not resumed, RAG was not changed, no historical evidence was modified, no debugger/controller interface was changed, and no retraining occurred.

---

## Scientific interpretation rules (binding on this report)

- Keep the eight axes separate: localized executable repair, fine-tuning transfer, RAG treatment, debugger interaction, model-generated test capability, serialization sensitivity, static verifier success, local inference engineering.
- Do **not** combine them into one score.
- Do **not** use the S1-P normalized patch as if the original model output had applied.
- Do **not** use deterministic PDB backend tests as evidence the model used the debugger successfully.
- Do **not** treat one accepted debugger command as successful interaction when the backend observation was an error/rejection.
- Do **not** infer debugger-command inability from S1 alone (S1 had an administrative phase-navigation failure); D1/S2 provide the stronger bounded runtime-entry evidence.
- Do **not** claim RAG success or failure from the S4 partial condition.
- Do **not** claim cp118 is universally worse than RAW; do **not** claim fine-tuning is generally harmful.
- Do **not** extrapolate 5/10 S4 truncation to the full 40-task cohort.
- Missing values use `NOT_RECORDED` / `NOT_EVALUATED` / `NOT_APPLICABLE` — never zero-substitution.

---

## Conditions reconciled

| ID | Condition | Provenance tier | In-repo? |
|---|---|---|---|
| A | RAW Qwen2.5 frozen repair baseline (C9 / Protocol v1.2.1) | frozen_in_repo | yes |
| B | cp118 definitive tuned repair (RAG-OFF) | aggregate_external_per_task (Drive-hosted D7 bundle; aggregates in master plan) | no per-task |
| C | cp118 + frozen repository-RAG (S4 partial) | frozen_in_repo (10/40 partial) | yes |
| D | RAW real-model debugger interaction (S1/D1) | frozen_in_repo (D1; S1 raw-run missing) | yes (D1) |
| E | cp118 real-model debugger interaction (S2) | master_plan_prose_only | no |

Auxiliary conditions (kept separate, NOT collapsed into the primary repair score): historical controlled DPO; S1-P original live; S1-P post-hoc serialization; static QuixBugs gcd verifier; backend/PDB engineering; Efficient SDPA (appendix only).

---

## Axis 1 — Localized executable repair (A vs B vs C)

| Metric | A (RAW, Track A strict / in-repo CSV) | A (RAW, Track B semantic / master plan) | B (cp118 RAG-OFF) | C (cp118+RAG, S4 partial) |
|---|---|---|---|---|
| strict_extraction | 33/40 (0.825) | NOT_RECORDED (separate metric) | NOT_RECORDED (external) | NOT_EVALUATED |
| semantic_extraction | NOT_RECORDED (separate metric) | 40/40 | 40/40 | NOT_EVALUATED |
| target_file_localization | 33/40 (0.825) — FILE only | NOT_RECORDED | 40/40 — FILE only | NOT_EVALUATED |
| symbol_localization | NOT_RECORDED (no column) | NOT_RECORDED | NOT_RECORDED | NOT_EVALUATED |
| patch_apply (strict) | 14/40 (0.35) | NOT_RECORDED | NOT_RECORDED | NOT_EVALUATED |
| patch_apply (semantic Track B) | NOT_RECORDED | 20/40 (0.5) | 0/40 | NOT_EVALUATED |
| f2p | NOT_RECORDED | NOT_RECORDED | NOT_RECORDED | NOT_EVALUATED |
| p2p | NOT_RECORDED | NOT_RECORDED | NOT_RECORDED | NOT_RECORDED |
| resolved | 5/40 (0.125) | 5/40 | 0/40 | NOT_EVALUATED |
| multi_diff | NOT_RECORDED | NOT_RECORDED | 40/40 | NOT_EVALUATED |
| extra_file_scope_violation | NOT_RECORDED | NOT_RECORDED | 39/40 | NOT_EVALUATED |
| truncation | NOT_RECORDED (per-task) | NOT_RECORDED | 19/40 | 5/10 (descriptive only; **do not extrapolate to 40**) |
| output_length | per-task | per-task | "very large output expansion" | per-task output_tokens (5 at 4096 cap) |

**Interpretation (accepted, preserved verbatim):** Strong formulation-specific negative executable-repair transfer dominated by output-policy degeneration, over-generation, scope explosion and serialization mismatch. This is **not** "fine-tuning is bad" and **not** "cp118 is universally worse than RAW". RAG treatment (C) primary correctness is `NOT_EVALUATED`; no RAG success/failure claim.

**Provenance note:** A's in-repo CSV records only strict Track A. The "40/40 extracted / 20/40 applied" Track B figures are master-plan §2.5 prose cross-referenced in `s4_contract.json` line 83; they are not computed in the CSV. Both tracks are recorded as distinct metrics to avoid contradiction (amendment 3). Symbol localization is `NOT_RECORDED` everywhere — never inferred from file localization.

---

## Axis 2 — Fine-tuning transfer (auxiliary DPO)

| Metric | Historical controlled DPO |
|---|---|
| B1 baseline RESOLVED | 27/30 |
| matched SFT RESOLVED | 27/30 |
| DPO RESOLVED | 21/30 |
| Provenance | master-plan §2.6 prose only; `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md` |
| In-repo frozen result file | none (no `qlora*` directory) |
| Status | AUTHENTIC NEW DPO: CLOSED / NOT JUSTIFIED |

Not collapsed into the primary repair score. Not reopened.

---

## Axis 3 — RAG treatment (C)

- Planned 40 tasks; 10 valid generated task pairs completed (first 10 in frozen manifest order; **NOT random / NOT representative**).
- 5/10 reached the 4096-token cap (descriptive only).
- Primary frozen C9 evaluation = **NOT_EVALUATED** (the evaluator fails closed unless `valid_pairs==40`).
- Patch apply = NOT_EVALUATED; RESOLVED = NOT_EVALUATED; P2P = NOT_RECORDED.
- Per-task `peak_allocated_gib` (9.46–20.05 GiB) is torch/CUDA allocator peak, NOT physical/resident VRAM (overcounts under Windows WDDM; several values exceed the 12,227 MiB device capacity). Recorded as `torch_cuda_peak_allocated` (untrusted descriptive allocator instrumentation, NOT a physical-VRAM metric); `gpu_total_vram_capacity` = 12,227 MiB (device capacity, not workload usage); `physical_resident_vram_usage` = **NOT_RECORDED** (no independent physical/resident measurement).
- `S4_GENERATION_COMPLETE.json` correctly NOT written.
- **No RAG success/failure claim.**

---

## Axis 4 — Debugger interaction (D, E)

Distinct metrics are used (amendment 2). Administrative D1 phase-navigation transitions are **not** counted as model-authored debugger actions. Controller-step count is **not** the debugger-turn count.

| Metric | D (RAW, D1) | E (cp118, S2) |
|---|---|---|
| debugger_exposure | 1 (1 model-authored PDB command reached real backend) | 1 |
| model_authored_debugger_command_count | 1 | 1 |
| model_authored_debugger_commands | `break 20` | `continue` |
| controller_accepted_model_debugger_command_count | 1 (controller accepted the model's `break 20` PDB directive) | 1 |
| backend_dispatch_action | `start_pdb_session` (controller-translated internal action dispatched to backend; NOT model-authored text) | NOT_RECORDED in-repo |
| backend_result | tool_error (breakpoint line 20 outside the 19-line probe) | rejected (no active PDB session) |
| administrative_debugger_transition_count | 2 (REPRODUCE→UNDERSTAND, UNDERSTAND→RUNTIME_EVIDENCE; NOT counted as debugger actions) | NOT_RECORDED in-repo |
| successful_debugger_observation_count | 0 | 0 |
| successful_debugger_turn_count | 0 (Gate B strict FAIL: need ≥2, got 1) | 0 (Gate B strict FAIL) |
| controller_step_count | 17 (NOT debugger-turn count) | NOT_RECORDED in-repo |
| debugger_informed_diagnosis | 0 (`post_debug_diagnoses=[]`) | 0 |
| debugger_informed_patch | 0 (`candidate_patch=null`) | 0 |
| f2p / p2p / resolved | NOT_APPLICABLE / NOT_APPLICABLE / false | NOT_APPLICABLE / NOT_APPLICABLE / false |
| tokens | 17,686 total (17,506 prompt + 180 completion) | NOT_RECORDED (S2 "5 calls / 3226 tokens" is prose-only and **not sourceable in-repo**) |
| latency | run duration 34,125 ms; per-call 578–1,735 ms | NOT_RECORDED in-repo |
| gpu_total_vram_capacity | NOT_RECORDED | NOT_RECORDED |
| torch_cuda_peak_allocated | NOT_RECORDED | NOT_RECORDED |
| physical_resident_vram_usage | NOT_RECORDED | NOT_RECORDED |

**Layer order (BLOCKER 4 clarification):** model-authored PDB command `break 20` → controller accepts and translates it to the internal `start_pdb_session` directive → backend dispatches `start_pdb_session` → backend result `tool_error` (breakpoint line 20 outside the 19-line probe). The model authored `break 20`; the model did **not** author the literal `start_pdb_session` action — that is the controller-translated internal action / backend dispatch. Accepted counts are unchanged (1); the D1 scientific result is unchanged (0 successful observations, Gate B/C FAIL).

**Interpretation (accepted, preserved verbatim):** Under the frozen D1 treatment, neither RAW nor cp118 demonstrated a successful debugger loop on the single curated task. This does **not** support a broad claim that fine-tuning harms debugger use; the training formulation contained no debugger/tool supervision and both model conditions failed the strict loop criterion.

---

## Axis 5 — Model-generated test capability (S1-P original live)

| Metric | S1-P original live |
|---|---|
| task | `curated-none-handling-001` |
| frozen test sha256 | `713c2b80...` |
| buggy run | FAIL (`AttributeError: 'NoneType' object has no attribute 'strip'`) |
| model fixed-code patch apply | FAILED (`PatchValidationError: Git metadata lines are not supported`) |
| generated test eval | NOT_RUN (reason: patch_apply_failed) |
| verifier | PATCH_APPLY_FAILED; f2p 0/0; p2p 0/0; resolved false |
| tokens | 957 total (776 prompt + 181 completion) = test-gen 427 + fix-gen 530 |
| outcome | NOT RESOLVED (original live) |
| provenance | `AI_REVIEW/s1p_.../live-run-1/evidence.json` (frozen_in_repo); source `c47be60e...` |

**Professor-facing claim:** Given an explicit expected-behavior specification, the frozen RAW model generated a test that exposed the bug. Its separately model-produced semantic repair satisfied the same frozen test and independent verifier only after deterministic post-hoc serialization normalization; the original raw live patch itself did not apply.

---

## Axis 6 — Serialization sensitivity (S1-P post-hoc)

| Metric | S1-P post-hoc |
|---|---|
| change | no semantic body-line changes; preserved semantic-hunk hash; normalized only serialization metadata / hunk-header defects |
| frozen generated test | PASS 1/1 |
| verifier | F2P 1/1; P2P 2/2; RESOLVED true |
| provenance | source commit `9e1b9dc9...` (ref valid); master-plan §S1-P prose (S5 lines 826–829); **no dedicated frozen result artifact in tree** |
| separation | kept strictly separate from the original live outcome (NOT RESOLVED) |

Do **not** use this normalized patch as if the original model output had applied.

---

## Axis 7 — Static verifier success (QuixBugs gcd)

| Metric | Static real-provider model → patch → verifier |
|---|---|
| program | gcd |
| verifier_resolved | true |
| f2p | 5/5 |
| p2p | 1/1 |
| full suite | 6/6 |
| latency | 62.3 s |
| provenance | `docs/QUIXBUGS_EIGHT_TASK_BASELINE_V1.md` (line 116), `docs/QUIXBUGS_SMOKE_USAGE_V1.md` (line 139), `research/quixbugs/GCD_SMOKE_MANIFEST_V1.json` (frozen_in_repo) |

Demonstrates real model → patch → verifier **static** path. Does **NOT** demonstrate debugger use. (Note: `experiments/swe_rebench_v2_static_pilot/` is a different SWE-rebench V2 30-task materialization pilot and is NOT this condition.)

---

## Axis 8 — Local inference engineering (Efficient SDPA, appendix only)

**BLOCKER 3:** the accepted performance evidence is recorded as TWO distinct blocks. The ~84.6× speedup references ONLY the matched 6079-token diagnostic; the 6113-token harness measurements are optimized reproduction, not a matched stock-vs-efficient A/B.

### A. Matched-prompt controlled diagnostic

6079 input + 1 output, identical model/config; only the attention path differs. This is the strongest causal/backend A/B.

| Path | Total elapsed | torch peak allocated |
|---|---|---|
| stock (MATH-SDPA) | 301.399 s | 15,350.3 MiB |
| efficient (repeat_kv) | 3.562 s | 7,371.0 MiB |
| **speedup** | **~84.6×** | — |

Provenance: `_ai-review/perf-cp118-efficient-sdpa-v1/changed-files/README.md` (6079+1 row). Stock long-gen deliberately not rerun in the committed harness.

### B. Reusable harness reproduction (optimized only; NO matched stock counterpart)

| Case | Total elapsed | torch peak allocated / reserved |
|---|---|---|
| 6113 + 1 | 4.982 s | 7,379.7 / 8,112.0 MiB |
| 6113 + 256 | 63.806 s | 7,379.7 / 8,112.0 MiB |
| 6079 + 1024 (manual stability) | 191.445 s | ~7,371 MiB (narrative) |

Provenance: `_ai-review/perf-cp118-efficient-sdpa-v1/generated-artifacts/index_efficient.json` (6113+1, 6113+256); `changed-files/README.md` (6079+1024). There is no newly-run matching stock 6113-token counterpart; none is invented; stock inference is not rerun. NOT a matched A/B.

### Real-model parity

same top token; cosine 0.9999468444424757; max abs diff 0.125; mean abs diff 0.01402792427688837; policy passed.

### Memory-capture semantics (BLOCKER 2)

- `torch_cuda_peak_allocated` = `torch.cuda.max_memory_allocated()` — **torch/CUDA allocator accounting, NOT physical/resident VRAM** (overcounts under Windows WDDM).
- `torch_cuda_reserved` = `torch.cuda.max_memory_reserved()` — torch caching allocator reserved, NOT physical residency.
- `gpu_total_vram_capacity` = 12,227 MiB — installed device capacity (RTX 5070 Ti Laptop), **NOT workload peak residency**.
- `physical_resident_vram_usage` = **NOT_RECORDED** — no independent physical/resident measurement. The ~7,371 / ~7,380 MiB figures are torch/CUDA allocator peak, **NOT** "7.4 GiB physical VRAM usage."

### Scope

- S4 scientific evidence changed? **NO** (11 changed files all under `experiments/local_inference_perf/`; branch `perf/cp118-efficient-sdpa-v1` @ `10bdfa91...`).

**Engineering evidence, NOT a retroactive change to S4.** Do not rewrite S4 as completed; do not mix stock-generated first 10 tasks with hypothetical optimized future tasks. Appendix only; not collapsed into any repair score. See `s5_remaining_gaps_next_action.md` for the GO/NO-GO recommendation on a fresh optimized full S4 rerun.

---

## Known provenance gaps and conflicts (explicit)

1. **S1 original raw-run artifact MISSING from disk.** D1 evidence intact and SHA256-verified. D1 is the authoritative RAW-debugger source.
2. **S2 "5 model calls / 3226 tokens" is master-plan prose and NOT sourceable** to any frozen in-repo artifact. The negative outcome is sourceable via master-plan §S2; the specific call/token figures are not.
3. **S1-P post-hoc serialization result has no dedicated frozen result artifact** in the working tree (source commit `9e1b9dc9...` ref valid; result is master-plan prose only).
4. **cp118 definitive (B) per-task evidence is external** (Drive-hosted D7 bundle); only accepted aggregates are in the current repo.
5. **D1 RAW-text retention divergence.** Master plan §2.10 states rejected RAW model text was not retained in the frozen transport; D1 `evidence.json` on disk contradicts this — all 14 rejected model-authored decodes retain `raw_response_text` with `raw_response_status='decoded'`; only the 2 administrative transitions carry `NOT_AVAILABLE` (correct by design). Resolved in favor of on-disk evidence (`AGENTS.md` §3, §7). No historical file rewritten; divergence documented in `s5_provenance_source_map.md` §5.
6. **S4 GPU-memory semantics.** `peak_allocated_gib` (9.46–20.05 GiB) is torch/CUDA allocator peak (NOT physical/resident VRAM; overcounts under Windows WDDM; several values exceed the 12,227 MiB device capacity) → recorded as `torch_cuda_peak_allocated` (untrusted descriptive allocator telemetry, NOT physical VRAM); `gpu_total_vram_capacity` = 12,227 MiB (device capacity, not workload usage); `physical_resident_vram_usage` = NOT_RECORDED. No allocator-derived value is labeled `vram_physical` or `physical_resident_vram_usage`; GPU capacity is not reported as workload usage.
7. **RAW Track A vs Track B** — in-repo CSV (strict Track A: 14/40 apply, 33/40 strict) and master-plan (Track B semantic: 20/40 apply, 40/40 extracted) are different extractors, recorded as distinct metrics.

---

## S4 optimized-rerun recommendation (summary)

- `scientific_necessity`: **NO-GO** — the project can honestly close out with RAG reported as PARTIAL / NOT_EVALUATED; no load-bearing final claim depends on knowing the cp118+RAG correctness result.
- `current_execution_authorization`: **NOT_AUTHORIZED_IN_S5** — regardless of the scientific recommendation (no model run / no S4 resume / no new experiment in S5).

Full reasoning and future GO conditions in `s5_remaining_gaps_next_action.md`.

---

## Cross-references

- Machine-readable ledger: `s5_comparison_ledger.json`
- Provenance / source map: `s5_provenance_source_map.md`
- Requirement #23/#24/#25 coverage: `s5_requirement_coverage_matrix.md` (+ `.json`)
- Remaining gaps / next action / S4 GO/NO-GO: `s5_remaining_gaps_next_action.md`
