# S5 Provenance / Source Map

**Schema:** `s5-controlled-comparison-v1` provenance companion
**Baseline HEAD:** `acfe131a0a99b994fd3d34e520d0022191246025`
**Authoritative plan:** `Agentic_Debugging_Master_Execution_Plan_2026-08-11_S5_CURRENT.md`
**Stale plan (ignored):** `Agentic_Debugging_Master_Execution_Plan_2026-08-10_S4_CURRENT.md`

This map binds every cell of `s5_comparison_ledger.json` to a concrete in-repo path or master-plan section, assigns a provenance tier, and surfaces the known gaps. It is a navigation aid; scientific authority remains the frozen run directories and committed source identity (per `AGENTS.md` §14 and the S5 task prompt, `_ai-review` / `AI_REVIEW` are review-convenience layers, not scientific authority).

---

## 1. Provenance tiers

| Tier | Meaning |
|---|---|
| `frozen_in_repo` | Frozen run-result artifact physically present in the repository. |
| `aggregate_external_per_task` | Accepted aggregate in the master plan / contract cross-ref; per-task raw evidence is external (Drive-hosted D7 bundle), not in the current repo. |
| `master_plan_prose_only` | Recorded only as narrative in the master execution plan; no frozen run-result file in the repo. Some details may be unsourcable. |
| `review_navigation_only` | `_ai-review` / `AI_REVIEW` convenience copy; navigation aid only, not scientific authority. |

A metric is never silently promoted from `master_plan_prose_only` or `aggregate_external_per_task` to `frozen_in_repo`.

---

## 2. Authoritative refs (all verified `git cat-file -t` = `commit`)

| Ref | Role | Verified |
|---|---|---|
| `2d4bc14c16d1a7eb3e7fa72c8fbd23259cb5cc4f` | S1 accepted parent commit (D1 source ancestry) | ✅ |
| `7bda64d04a6165eb47bfb232094976e82e1155ed` | D1 source commit (RAW debugger) | ✅ |
| `c47be60e6919626b6f431cd337d1d847a97f0722` | S1-P source commit (model-generated test probe) | ✅ |
| `9e1b9dc9224c76df574346f93854d1097de3792e` | S1-P post-hoc serialization diagnostic | ✅ (ref valid; no in-tree result artifact) |
| `1bd90a2dc76f88307e3387fbf556b0d1ff4dee49` | S2 (cp118 debugger) | ✅ |
| `acfe131a0a99b994fd3d34e520d0022191246025` | S4 source commit = current baseline HEAD | ✅ |
| `10bdfa91c55bc829b9d20f7c92ea098e437f1f91` | Efficient SDPA engineering | ✅ |

---

## 3. Per-condition provenance

### A — RAW Qwen2.5 frozen repair baseline (C9 / Protocol v1.2.1)

| Field | Path | Tier |
|---|---|---|
| Strict-track per-task + aggregate | `experiments/raw-pilot-v1.1/results/results_final.csv`, `experiments/raw-pilot-v1.1/results/metrics_summary.csv`, `experiments/raw-pilot-v1.1/results/failure_taxonomy_counts.csv` | frozen_in_repo |
| Per-task generation telemetry | `experiments/raw-pilot-v1.1/work/gpu-full-extracted/meta/*.json` | frozen_in_repo |
| 40-task cohort manifest | `experiments/raw-pilot-v1.1/state/quix40-v1/pilot_manifest_frozen_v1.jsonl` (sha256 `572082482a64...`) | frozen_in_repo |
| Preflight / model revisions | `experiments/raw-pilot-v1.1/state/quix40-v1/PREFLIGHT_COMPLETE.json`, `model_revisions_v1.json` | frozen_in_repo |
| Frozen protocol definition | `experiments/raw-pilot-v1.1/docs/RAW_BASELINE_PROTOCOL_v1_2_1_FROZEN.md` (Track A strict / Track B semantic) | frozen_in_repo |
| Track B semantic aggregates (40/40 extracted, 20/40 apply, 5/40 RESOLVED) | `Agentic_Debugging_Master_Execution_Plan_2026-08-11_S5_CURRENT.md#2.5`; cross-ref `experiments/cp118_rag_definitive/s4_contract.json` line 83, `validation-evidence.json` line 152 | master_plan_prose_only |

**In-repo CSV facts (Qwen2.5-Coder-7B, 40 tasks, strict Track A):**
- `strict_valid=True`: 33/40 (0.825)
- `target_file_localized=True`: 33/40 (0.825) — FILE localization only; no `symbol_localization` column exists
- `patch_apply=True`: 14/40 (0.35)
- `test_pass=True`: 5/40 (0.125)
- `failure_stage` distribution: `patch_apply_failed`=19, `designated_test_failed`=9, `strict_parse_failed`=7, `resolved_supplied_oracle`=5

**Distinct from Track B (master-plan prose, not in CSV):**
- semantic_extraction = 40/40
- patch_apply_semantic_track_b = 20/40
- resolved = 5/40 (same count, different extractor)

The ledger carries **both tracks as distinct metrics** to avoid the internal contradiction flagged in amendment 3.

### B — cp118 definitive tuned repair (RAG-OFF)

| Field | Path | Tier |
|---|---|---|
| Aggregates (40/40 extracted, 0/40 apply, 0/40 RESOLVED, target-file 40/40, multi-diff 40/40, extra-file 39/40, truncation 19/40, "very large output expansion") | `Agentic_Debugging_Master_Execution_Plan_2026-08-11_S5_CURRENT.md#2.5` (lines 196–207); cross-ref `experiments/cp118_rag_definitive/s4_contract.json` line 83 | master_plan_prose_only / aggregate_external_per_task |
| Per-task raw evidence | **Drive-hosted D7 bundle** (per `s4_contract.json` line 94: "cp118 RAG-OFF per-task raw evidence (Drive-hosted D7 bundle; accepted aggregates in the master plan)") — NOT in current repo | aggregate_external_per_task |
| cp118 adapter identity | `experiments/cp118_rag_definitive/s4_contract.json` (adapter tree `65b5ed9a...`, safetensors `59398e32...`, step 118, eval_loss 0.45070546) | frozen_in_repo |

No in-repo frozen JSON/CSV exists for the cp118 RAG-OFF 40-task run. The ledger records strict_extraction / runtime / latency / tokens / VRAM for B as `NOT_RECORDED` (per-task external), and only the master-plan aggregates are recorded with their prose provenance.

### C — cp118 + frozen RAG (S4 partial)

| Field | Path | Tier |
|---|---|---|
| Contract | `experiments/cp118_rag_definitive/s4_contract.json` (schema `s4-cp118-rag-definitive`) | frozen_in_repo |
| Run identity | `experiments/cp118_rag_definitive/runs/run-1-live-2026-08-10/run-identity.json` | frozen_in_repo |
| Partial-run record | `experiments/cp118_rag_definitive/runs/run-1-live-2026-08-10/PARTIAL_RUN_RECORD.json` | frozen_in_repo |
| Validate-stage evidence | `experiments/cp118_rag_definitive/runs/run-1-live-2026-08-10/validation-evidence.json` (embeds full contract) | frozen_in_repo |
| Retrieval index | `experiments/cp118_rag_definitive/runs/run-1-live-2026-08-10/index-v1.json` (240,668 B) | frozen_in_repo |
| 10 valid pairs | `runs/run-1-live-2026-08-10/{raw,meta,retrieval}/Qwen_Qwen2.5-Coder-7B-Instruct_CP118-RAG__<task_id>.{txt,json}` for the first 10 manifest slots (bitcount → get_factors) | frozen_in_repo |
| 40-task cohort manifest | `experiments/raw-pilot-v1.1/state/quix40-v1/pilot_manifest_frozen_v1.jsonl` | frozen_in_repo |

**Identity (verified cross-file):**
- source_commit_sha = `acfe131a0a99b994fd3d34e520d0022191246025`
- contract_sha256 = `966c2aaba413d6f688ad9095b47c2c0d3c6936ea67bc95acb52fd9a1df5745bd`
- run_identity_sha256 = `072f1d693cfd07049c47ff6f7826eda17b24a22cd19476e97d50c328e56c72ab`
- adapter_tree_identity_sha256 = `65b5ed9a354d4b2c03ba86e2b8065118e11abab9c439cb481b5739f1b86e7c00`
- environment.gpu_total_vram_mib = 12227 (RTX 5070 Ti Laptop)

**Missingness (per `PARTIAL_RUN_RECORD.json`):**
- `run_status` = `PARTIAL / COMPUTE-CONSTRAINED`; `completion_fraction` = `10/40`
- `completion_marker_written` = false (`S4_GENERATION_COMPLETE.json` correctly NOT written)
- `primary_evaluation.status` = `NOT_EVALUATED` (strict compliance, recognizable diff, semantic extraction, patch apply, RESOLVED, file/symbol localization, truncation all `NOT_EVALUATED`; no denominator N or 40 claimed)
- `p2p` = `NOT_RECORDED`
- 10 completed tasks = first 10 in frozen manifest order (NOT random / NOT representative)
- `truncated_count_4096` = 5 (descriptive over 10 only; **do not extrapolate to 40**)
- `latency_s` = {min 120.7, median 2170.15, max 5451.0} (per-task `generation_latency_s` aggregated; descriptive over 10)
- **`runtime` = `NOT_RECORDED`** — no campaign-runtime aggregate is defined in `PARTIAL_RUN_RECORD.json` (only per-task `generation_latency_s` and wall-clock timestamps in `stopping_details`: `generation_started_at`, `last_task_completed_at`, `campaign_stopped_at`). No defensible campaign-runtime interval is derived from timestamps. Token counts are recorded only under the `tokens` metric (BLOCKER 1).
- **GPU-memory (BLOCKER 2):** `peak_allocated_gib` {min 9.46, median 14.49, max 20.05} = torch/CUDA allocator peak (NOT physical/resident VRAM; overcounts under Windows WDDM; several exceed the 12,227 MiB capacity) → recorded as `torch_cuda_peak_allocated` (untrusted descriptive allocator telemetry, NOT physical VRAM); `gpu_total_vram_capacity` = 12,227 MiB (device capacity, not workload usage); `physical_resident_vram_usage` = `NOT_RECORDED`. `stopping_details.gpu_evidence.typical_vram_used_mib` (11600–11800) is an operator-observed typical range, not a per-task physical-residency measurement.
- `assembled_prompt_tokens` = null (`assembled_prompt_tokens_NOT_RECORDED: true`) per task
- `temperature`/`top_p` = `NOT_RECORDED` (do_sample=false)

### D — RAW real-model debugger interaction (S1/D1)

| Field | Path | Tier |
|---|---|---|
| Frozen run-result | `experiments/debugger_interaction_v2_d1/runs/run-1-live-2026-08-10/evidence.json` (sha256 `c7a405cc...`) | frozen_in_repo |
| Summary | `experiments/debugger_interaction_v2_d1/runs/run-1-live-2026-08-10/RUN_SUMMARY.md` (sha256 `23545bf5...`) | frozen_in_repo |
| Raw stdout | `experiments/debugger_interaction_v2_d1/runs/run-1-live-2026-08-10/runner_stdout.log` (sha256 `b1e55eba...`) | frozen_in_repo |
| Integrity manifest | `experiments/debugger_interaction_v2_d1/runs/run-1-live-2026-08-10/SHA256SUMS.txt` | frozen_in_repo |
| Build handoff | `AI_REVIEW/s1_debugger_interaction_v2_build_2026-08-10/D1_BUILD_HANDOFF.md` | review_navigation_only |
| S1 raw-run evidence | **MISSING from disk** — `live_raw_run_1/evidence.json`, `LIVE_RAW_RUN_1_HANDOFF.md`, `REPAIR_PASS_1_HANDOFF.md` are referenced by the handoff but not present; `experiments/debugger_interaction_v2/` has only `.pyc` caches | gap (see §4) |

**Identity (verified in `evidence.json`):**
- source_commit_sha = `7bda64d04a6165eb47bfb232094976e82e1155ed`
- experiment_contract_sha256 (D1) = `1d8819cbff46bf74bc03358cdb731d2bfae57c47b4d8f6aabd416c1d89403cf3`
- s1_accepted_parent_commit = `2d4bc14c16d1a7eb3e7fa72c8fbd23259cb5cc4f`
- system_prompt_sha256 = `a6b3d7a3e61a8a47ee4bf8ef5cc0b55926dfd5c71eda317bc9a0fcef69753c42`
- task_id = `curated-off-by-one-002`; model_condition = `RAW_BASE`; adapter_applied = false; rag_enabled = false

**Precise D1 debugger facts (from `evidence.json` telemetry, 31 entries):**
- `d1_authorship` distribution: `administrative`=2, model-authored=29
- Administrative transitions (2): `REPRODUCE->UNDERSTAND`, `UNDERSTAND->RUNTIME_EVIDENCE`; `administrative_transitions_do_not_count_as_debugger_commands: true` — NOT counted as model-authored debugger actions
- Model-authored accepted actions (15): `run_reproduction`=1, `start_pdb_session`=1 (controller-translated directive for the model-authored `break 20`), `get_source_window`=13
- Model-authored rejected (14): all `reproduce` with `command_not_in_state`
- **Layer order (BLOCKER 4):** model-authored PDB command `break 20` (raw_response_text) → controller accepts and translates to internal `start_pdb_session` directive (translated_directive.action_name) → backend dispatches `start_pdb_session` → backend result `tool_error` (observation status=error, dispatch_reason=tool_error, summary="Tool execution failed."; requested line 20 outside the 19-line probe). The model authored `break 20`; the model did NOT author the literal `start_pdb_session` — that is the controller-translated internal action / backend dispatch.
- PDB-specific accepted model-authored commands = 1 (`break 20`); controller-accepted PDB directives = 1
- `start_pdb_session` observation: `status=error`, `dispatch_reason=tool_error`, `summary="Tool execution failed."` (requested line 20 outside the 19-line probe)
- `successful_debugger_observation_count` = 0 (`get_source_window` observations are source reads, not PDB observations)
- `gate_results.gate_b` = {passed: false, reason: "need >=2 accepted PDB commands, got 1", accepted_pdb_count: 1}
- `gate_results.gate_c` = {has_pdb_evidence: true, has_diagnosis: false, has_patch: false, verifier_executed: false, resolved: false, passed: false}
- `controller_result` = {final_state: "Failed", stop_reason: "budget_exhausted", model_calls: 17, steps_count: 17}
- `post_debug_diagnoses` = []; `candidate_patch` = null; `verifier.executed` = false
- tokens: total 17,686 (prompt 17,506 + completion 180); per-call usage `provider_reported: true`; administrative transitions `NOT_RECORDED`
- run `duration_ms` = 34,125; per-call `request_duration_ms` range 578–1735

### E — cp118 real-model debugger interaction (S2)

| Field | Path | Tier |
|---|---|---|
| Aggregates / outcome | `Agentic_Debugging_Master_Execution_Plan_2026-08-11_S5_CURRENT.md#S2` (lines 876–902) | master_plan_prose_only |
| Frozen run-result | **None in repo.** `artifacts/tuned_debugger_pilot_v1/run-cp118-001/` is a DIFFERENT 5-task × 2-condition pilot (contract `47210df7...`, all `PROVIDER_ERROR`/`debugger_turns=0`), NOT the S2 run | gap (see §4) |
| adapter identity | cp118 adapter tree `65b5ed9a...` reused (same as conditions B/C) | frozen_in_repo |

**S2 facts (master-plan prose only):**
- reproduction succeeded; identical D1 administrative runtime-entry reached RuntimeEvidence
- cp118 authored one PDB command: `continue`; real PDB backend rejected it (no active PDB session)
- successful non-error PDB observations = 0; no second PDB command; no post-debug diagnosis; no patch; verifier not executed
- Gate B legacy = FAIL; Gate B strict = FAIL; Gate C = FAIL
- **"5 model calls / 3226 tokens" is NOT sourceable to any frozen in-repo artifact** (repo-wide search returns zero hits); recorded as prose-only and not silently trusted

### Auxiliary provenance

| Auxiliary | Path(s) | Tier |
|---|---|---|
| Historical controlled DPO (B1 27/30, SFT 27/30, DPO 21/30) | master plan §2.6 (lines 219–233); `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md`; `docs/PREFERENCE_EXPORTER_V1.md`; impl `agentic_debugger/preference/`, `agentic_debugger/comparison/` | master_plan_prose_only (no `qlora*` dir, no frozen result file) |
| S1-P original live | `AI_REVIEW/s1p_model_generated_test_probe_2026-08-10/live-run-1/evidence.json`, `AI_REVIEW/s1p_model_generated_test_probe_2026-08-10/LIVE_RUN_1_HANDOFF.md` | frozen_in_repo (source `c47be60e...`, contract `3d7c7e8d...`, task `curated-none-handling-001`) |
| S1-P post-hoc serialization | source commit `9e1b9dc9...` (ref valid); master plan §S1-P (lines 821–833); **NO dedicated frozen result artifact in tree** | master_plan_prose_only |
| Static QuixBugs gcd verifier | `docs/QUIXBUGS_EIGHT_TASK_BASELINE_V1.md` (line 116), `docs/QUIXBUGS_SMOKE_USAGE_V1.md` (line 139), `research/quixbugs/GCD_SMOKE_MANIFEST_V1.json` (revision `4257f44b...`) | frozen_in_repo |
| Backend/PDB engineering | `agentic_debugger/runtime/pdb_*`, `tests/`, `tests/golden_trajectories/data/quixbugs-gcd-pdb-reachability-captured-result.json` | frozen_in_repo |
| Efficient SDPA engineering | `_ai-review/perf-cp118-efficient-sdpa-v1/generated-artifacts/{index_efficient.json, parity_real_model.json}`, `agent-report.md`, `validation.md`, `manual-smoke.md`, `changed-files/README.md`; source `experiments/local_inference_perf/` on branch `perf/cp118-efficient-sdpa-v1` @ `10bdfa91...` | frozen_in_repo (torch/CUDA allocator peak; NOT physical VRAM) |

**Important correction:** `experiments/swe_rebench_v2_static_pilot/` is a SWE-rebench V2 30-task zero-execution materialization pilot, NOT the QuixBugs gcd verifier condition. The provenance map steers the ledger to the correct files (`docs/QUIXBUGS_*`, `research/quixbugs/GCD_SMOKE_MANIFEST_V1.json`).

---

## 4. Known provenance gaps (explicitly surfaced, not smoothed)

1. **S1 original raw-run artifact MISSING.** `AI_REVIEW/s1_debugger_interaction_v2_build_2026-08-10/` contains only `D1_BUILD_HANDOFF.md`. The `live_raw_run_1/evidence.json`, `LIVE_RAW_RUN_1_HANDOFF.md`, `REPAIR_PASS_1_HANDOFF.md` are referenced by the handoff but not present; `experiments/debugger_interaction_v2/` has only `.pyc` caches. **D1 evidence is intact and SHA256-verified** and is the authoritative RAW-debugger source.
2. **S2 "5 model calls / 3226 tokens" is master-plan prose and NOT sourceable.** Repo-wide search for `3226`, `3,226`, `5 model calls`, `model_calls: 5` returns zero hits in frozen artifacts. The S2 negative *outcome* (`continue` rejected, 0 observations, Gate B/C FAIL) is sourceable via master-plan §S2 prose; the specific call/token figures are not.
3. **S1-P post-hoc serialization result has source commit + master-plan result but NO dedicated frozen result artifact.** Ref `9e1b9dc9...` is valid; the result (RESOLVED, F2P 1/1, P2P 2/2) exists only as master-plan prose (S5 lines 826–829).
4. **cp118 definitive (B) per-task evidence is external.** Only accepted aggregates are in the current repo; per-task raw evidence is a Drive-hosted D7 bundle.
5. **D1 RAW-text retention divergence** (see §5).
6. **S4 GPU-memory semantics.** `peak_allocated_gib` (9.46–20.05 GiB) is torch/CUDA allocator peak (NOT physical/resident VRAM; overcounts under Windows WDDM; several exceed the 12,227 MiB device capacity) → recorded as `torch_cuda_peak_allocated` (untrusted descriptive allocator telemetry, NOT physical VRAM); `gpu_total_vram_capacity` = 12,227 MiB (device capacity, not workload usage); `physical_resident_vram_usage` = NOT_RECORDED. No allocator-derived value is labeled `vram_physical` or `physical_resident_vram_usage`; GPU capacity is not reported as workload usage.
7. **RAW Track A vs Track B** — the in-repo CSV records strict Track A (14/40 apply, 33/40 strict/extract); the master-plan "20/40 applied / 40/40 extracted" is Track B (semantic), not computed in the CSV. Both recorded distinctly.

---

## 5. D1 RAW-text retention divergence (amendment 8)

**Master plan §2.10 (S5 line 345–347):** "Another important evidence gap: rejected RAW model text was not retained in the frozen transport. Future experiments must retain full raw model responses."

**On-disk D1 evidence (`evidence.json`):** All 14 rejected model-authored decodes retain `raw_response_text` (e.g. `'reproduce'`), `raw_response_status: 'decoded'`, and `raw_response_bytes`. Only the 2 administrative transitions carry `raw_response_status: 'administrative_navigation'` / `'NOT_AVAILABLE'` (correct by design — no model request was made).

**Resolution:** Per `AGENTS.md` §3 ("Do not follow a technically false assumption merely because it appears in a prompt or old document. Implement what the live repository evidence supports") and §7 ("strict schemas and fail-closed boundaries are deliberate project style"), this divergence is resolved **in favor of the on-disk frozen evidence**: the D1 frozen transport **did** retain rejected RAW model text. The master-plan §2.10 statement is either inaccurate for the D1 transport or refers to a different (now-missing) S1 transport; it cannot be verified against S1 because the S1 raw-run evidence is not on disk (gap §4.1). No historical file is rewritten; the divergence is documented here and in the ledger (`raw_rejected_text_retained: true` with a `notes` field recording the material divergence).

---

## 6. Hash cross-checks performed

| Identity | Ledger value | Source file | Match |
|---|---|---|---|
| S4 source_commit_sha | `acfe131a...` | `PARTIAL_RUN_RECORD.json#frozen_condition.source_commit_sha`, `run-identity.json` | ✅ |
| S4 contract_sha256 | `966c2aab...` | `s4_contract.json` (embedded in `validation-evidence.json` line 215), `run-identity.json` | ✅ |
| S4 run_identity_sha256 | `072f1d69...` | `run-identity.json`, `PARTIAL_RUN_RECORD.json#frozen_condition` | ✅ |
| S4 adapter_tree_identity_sha256 | `65b5ed9a...` | `s4_contract.json`, `run-identity.json`, `validation-evidence.json` | ✅ |
| D1 source_commit_sha | `7bda64d...` | `evidence.json#run_identity.source_commit_sha`, `RUN_SUMMARY.md` | ✅ |
| D1 experiment_contract_sha256 | `1d8819cb...` | `evidence.json#run_identity.experiment_contract_sha256` | ✅ |
| S1-P tokens sum | 957 = 776 + 181 | `AI_REVIEW/s1p_.../live-run-1/evidence.json` (test-gen 427 + fix-gen 530) | ✅ |
| Efficient SDPA cosine | 0.9999468444424757 | `parity_real_model.json` | ✅ |
| Efficient SDPA optimized 6113+1 elapsed | 4.982 s | `index_efficient.json` | ✅ |
| RAW Qwen strict_valid | 33/40 | `results_final.csv` (count) + `metrics_summary.csv` (0.825) | ✅ |