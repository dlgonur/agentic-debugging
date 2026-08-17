# Experiments Results Summary

This document provides a concise, professor-readable summary of all scientific and engineering experiment families in `experiments/`.

For complete narratives and broader context, see [`docs/project-closeout.md`](../docs/project-closeout.md), [`docs/final-report.md`](../docs/final-report.md), and [`docs/results-index.md`](../docs/results-index.md).

---

## Executive Overview Table

| Family | Experiment ID | Model / Checkpoint | Scope | Status | Main Result | Canonical Evidence Path |
|---|---|---|---|---|---|---|
| `debugger_interaction_v2_r1/` | `debugger-interaction-v2-r1` | Qwen2.5-Coder-7B (RAW) | 1 curated task | COMPLETE / ACCEPTED | Real model authored valid breakpoint; real PDB paused in production code | [`debugger_interaction_v2_r1/README.md`](debugger_interaction_v2_r1/README.md) |
| `debugger_interaction_v2_r2/` | `debugger-interaction-v2-r2` | Qwen2.5-Coder-7B (RAW) | 1 curated task | COMPLETE / ACCEPTED | Full multi-turn loop completed: break → stack → locals → step/next → stack → diagnosis | [`debugger_interaction_v2_r2/README.md`](debugger_interaction_v2_r2/README.md) |
| `debugger_interaction_v2_r3/` | `debugger-interaction-v2-r3` | Qwen2.5-Coder-7B (RAW) | 1 curated task | COMPLETE / ACCEPTED | Debugger evidence → diagnosis → patch → independent verifier **RESOLVED** (count-normalized) | [`debugger_interaction_v2_r3/README.md`](debugger_interaction_v2_r3/README.md) |
| `model_generated_test_probe_r4/` | `model-generated-test-probe-r4` | Qwen2.5-Coder-7B (RAW) | 1 curated task | COMPLETE / ACCEPTED | Model-authored test failed buggy code and passed verified fix on single attempt; verifier **RESOLVED** | [`model_generated_test_probe_r4/README.md`](model_generated_test_probe_r4/README.md) |
| `debugger_interaction_v2_r5/` | `debugger-interaction-v2-r5` | Qwen2.5-Coder-14B (BASE) | 5 curated tasks | COMPLETE / ACCEPTED | **5/5 RESOLVED** under clean treatment r5.9; **0 leakage findings** across 41 audited prompts | [`debugger_interaction_v2_r5/README.md`](debugger_interaction_v2_r5/README.md) |
| `r6_debugger_training/` | `r6-debugger-training` | Qwen2.5-Coder-7B QLoRA SFT (`checkpoint-30`) | QuixBugs (21 train / 8 val) + 5-task holdout | VALIDATION COMPLETE / HOLDOUT STOP | **8/8 RESOLVED** on disjoint QuixBugs validation; stronger 5-task holdout stopped by hardware power-off | [`r6_debugger_training/README.md`](r6_debugger_training/README.md), [`runs/frozen/`](r6_debugger_training/runs/frozen/) |
| `cp118_rag_definitive/` | `s4-cp118-rag-definitive` | Qwen2.5-Coder-7B + cp118 + RAG | 40 QuixBugs tasks | CLOSED / PARTIAL | 10/40 tasks produced; compute-constrained stop; primary correctness **NOT_EVALUATED** | [`cp118_rag_definitive/RESULT.md`](cp118_rag_definitive/RESULT.md) |
| `tuned_debugger_pilot_v1/` | `tuned-debugger-pilot-v1` | Qwen2.5-Coder-7B + early adapter | 5 curated tasks (10 cases) | HISTORICAL / SUPERSEDED | Early pilot protocol/provider failures; contract frozen as historical foundation for R1–R6 | [`tuned_debugger_pilot_v1/README.md`](tuned_debugger_pilot_v1/README.md) |
| `local_inference_perf/` | `local-inference-perf` | Qwen2.5-Coder-7B + cp118 | 50-token parity & throughput | COMPLETE / ENGINEERING ACCEPTED | Windows SDPA speedup (301.4s → 3.56s on 6079+1 tokens) with numerical parity (`cosine=0.9999645`) | [`local_inference_perf/README.md`](local_inference_perf/README.md) |

---

## Detailed Family Summaries

### 1. R1 — Repaired Debugger-Interface Breakpoint (`debugger_interaction_v2_r1/`)
- **What it was:** Single-turn PDB breakpoint observation probe under the repaired model-facing interface.
- **Why it existed:** Previous interfaces (D1/S2) failed because models emitted unsupported commands or lacked valid line affordances (e.g., `break 20` → tool error). R1 introduced production-only `compile() + co_lines()` breakpoint affordance rendering and execution visibility.
- **Model / Checkpoint:** `Qwen/Qwen2.5-Coder-7B-Instruct` (RAW base, `adapter_applied=false`, `rag_enabled=false`).
- **Dataset / Task Scope:** Single curated fixture (`curated-off-by-one-002` / `recent_window.py`).
- **Main Result:** The real model authored a valid breakpoint (`break 17`); the live PDB session paused at that breakpoint and returned a production-region observation.
- **Accepted Interpretation:** First accepted positive milestone demonstrating real-model PDB engagement after historical interface failures. It confirms that the repaired interface enables genuine PDB pausing, though it does not by itself evaluate a multi-turn loop or repair correctness.
- **Status:** COMPLETE / ACCEPTED (2026-08-11; commit `c842d69`).
- **Canonical Evidence:** [`experiments/debugger_interaction_v2_r1/README.md`](debugger_interaction_v2_r1/README.md), [`experiments/debugger_interaction_v2_r1/r1_contract.json`](debugger_interaction_v2_r1/r1_contract.json), `docs/project-closeout.md` §3.

### 2. R2 — Staged Multi-Turn Debugger Loop (`debugger_interaction_v2_r2/`)
- **What it was:** Staged multi-turn dynamic debugger interaction experiment.
- **Why it existed:** Evaluated whether a real model could sustain a coherent multi-turn debugging session with staged-legal actions (break → stack → locals/print → step/next → stack → diagnosis) rather than a one-shot probe.
- **Model / Checkpoint:** `Qwen/Qwen2.5-Coder-7B-Instruct` (RAW base).
- **Dataset / Task Scope:** Single curated fixture (`curated-off-by-one-002`).
- **Main Result:** The model completed a full multi-turn observation loop: breakpoint → stack → locals → step/next → post-step stack → diagnosis.
- **Accepted Interpretation:** Proves that a real model can successfully navigate a multi-turn dynamic PDB inspection loop under staged affordances.
- **Status:** COMPLETE / ACCEPTED (2026-08-11; commit `97cc7fe`).
- **Canonical Evidence:** [`experiments/debugger_interaction_v2_r2/README.md`](debugger_interaction_v2_r2/README.md), [`experiments/debugger_interaction_v2_r2/r2_contract.json`](debugger_interaction_v2_r2/r2_contract.json), `docs/project-closeout.md` §3.

### 3. R3 — Debugger Evidence → Diagnosis → Patch → Verifier RESOLVED (`debugger_interaction_v2_r3/`)
- **What it was:** End-to-end debugger-informed repair through the independent verifier.
- **Why it existed:** Connected dynamic debugger observations directly to model-generated patch creation and evaluation against the independent `EvaluationVerifier`.
- **Model / Checkpoint:** `Qwen/Qwen2.5-Coder-7B-Instruct` (RAW base).
- **Dataset / Task Scope:** Single curated fixture (`curated-off-by-one-002`).
- **Main Result:** Debugger evidence → model diagnosis → semantic patch → PatchManager → independent verifier **RESOLVED** (F2P 1/1, P2P 2/2).
- **Accepted Interpretation:** First accepted end-to-end debugger-informed repair confirmed by the independent correctness authority. **Mandatory qualifier:** The raw model patch contained a unified-diff hunk-count metadata error corrected by deterministic **COUNT-ONLY** serialization normalization (no semantic modification to code hunks).
- **Status:** COMPLETE / ACCEPTED (2026-08-11; commit `f2291df`).
- **Canonical Evidence:** [`experiments/debugger_interaction_v2_r3/README.md`](debugger_interaction_v2_r3/README.md), [`experiments/debugger_interaction_v2_r3/r3_contract.json`](debugger_interaction_v2_r3/r3_contract.json), [`experiments/debugger_interaction_v2_r3/serialization.py`](debugger_interaction_v2_r3/serialization.py), `docs/project-closeout.md` §3.

### 4. R4 — Model-Generated Regression Test Probe (`model_generated_test_probe_r4/`)
- **What it was:** Single-attempt model-authored regression test generation capability experiment.
- **Why it existed:** Tested whether a real model, given only buggy code and agent-visible task descriptions (no harness-authored behavioral spec), can author a discriminating regression test in a single try.
- **Model / Checkpoint:** `Qwen/Qwen2.5-Coder-7B-Instruct` (RAW base; 1 model call, no retries).
- **Dataset / Task Scope:** Single curated fixture (`curated-off-by-one-002`).
- **Main Result:** Model-authored regression test $T$ strictly failed the buggy workspace for the intended behavioral reason and passed the independently verified fixed workspace (`R_fix_C`), with the independent verifier confirming RESOLVED.
- **Accepted Interpretation:** Demonstrates that the model can generate a valid, discriminating regression test in one shot without test leakage. The generated test is auxiliary evidence; the independent verifier over frozen F2P/P2P contracts remains the authority.
- **Status:** COMPLETE / ACCEPTED (commit `372d51f1a35e071c677391c9970f7b552bb276f2`).
- **Canonical Evidence:** [`experiments/model_generated_test_probe_r4/README.md`](model_generated_test_probe_r4/README.md), [`experiments/model_generated_test_probe_r4/SOURCE_AUDIT.md`](model_generated_test_probe_r4/SOURCE_AUDIT.md), [`experiments/model_generated_test_probe_r4/r4_contract.json`](model_generated_test_probe_r4/r4_contract.json), `docs/project-closeout.md` §3.

### 5. R5 — Clean Base-14B Generalized Holdout (`debugger_interaction_v2_r5/`)
- **What it was:** 5-task holdout evaluation of base model debugging across diverse bug classes under strict anti-leakage controls.
- **Why it existed:** Tested whether a larger un-fine-tuned model (14B) could complete the debugger repair loop across all five curated bugs without test-oracle leakage into prompts.
- **Model / Checkpoint:** `Qwen/Qwen2.5-Coder-14B-Instruct` (BASE; `adapter_applied=false`, `rag_enabled=false`).
- **Dataset / Task Scope:** Full 5-task curated suite (`curated-none-handling-001` through `curated-caller-callee-005`).
- **Main Result:** BASE 14B resolved **5/5** curated bugs under sanitized treatment r5.9, with **0 leakage findings across all 41 audited actual prompts**. (Earlier r5.7 5/5 was disqualified due to prompt leakage and retained as historical upper-bound evidence).
- **Accepted Interpretation:** Establishes that a clean base-14B model can complete the debugger-informed repair loop without prompt leakage. **Does not claim fine-tuning caused an improvement** (no fine-tuning was used).
- **Status:** COMPLETE / ACCEPTED (2026-08-12; commit `54828db1d5dec4e95105f1c1d07ba5dd7518060c`).
- **Canonical Evidence:** [`experiments/debugger_interaction_v2_r5/README.md`](debugger_interaction_v2_r5/README.md), [`experiments/debugger_interaction_v2_r5/r5_contract_14b.json`](debugger_interaction_v2_r5/r5_contract_14b.json), `docs/project-closeout.md` §3, `docs/final-report.md`.

### 6. R6 — Debugger-Oriented QLoRA Fine-Tuning & Disjoint Validation (`r6_debugger_training/`)
- **What it was:** Trajectory-based QLoRA SFT fine-tuning on QuixBugs debugger trajectories followed by executable validation on disjoint QuixBugs tasks and a 5-task curated holdout.
- **Why it existed:** The first fine-tuning campaign specifically targeting multi-turn debugger interaction (trajectory SFT) rather than single-line localized code edits.
- **Model / Checkpoint:** `Qwen/Qwen2.5-Coder-7B-Instruct` + QLoRA SFT (`checkpoint-30`; adapter SHA-256 `7ef5d70a…`, config SHA-256 `92ddf91e…`).
- **Dataset / Task Scope:** Pinned QuixBugs (`4257f44b`): 21 training tasks (164 pairs), 8 disjoint validation tasks (61 pairs); 5-task curated holdout.
- **Main Result:**
  - **Disjoint Validation:** **8/8 RESOLVED** under real tool/debugger execution and independent verification (97 model calls, 64,783 tokens, 841,702 ms task runtime, zero row errors).
  - **Curated Holdout:** **INCOMPLETE_HARDWARE_STOP** (`curated-none-handling-001` RESOLVED; `curated-off-by-one-002` BREAKING_RESOLVED; remaining 3 tasks unstarted/interrupted due to local hardware power-offs).
- **Accepted Interpretation:** Strongest accepted tuned-debugger validation in the repository. Fine-tuning is **not** claimed to have causally improved over a matched base (no matched-base ablation exists). The incomplete holdout is a closed hardware-stop boundary, not a failure score.
- **Status:** VALIDATION COMPLETE (8/8) / HOLDOUT INCOMPLETE_HARDWARE_STOP.
- **Canonical Evidence:** [`experiments/r6_debugger_training/README.md`](r6_debugger_training/README.md), [`experiments/r6_debugger_training/runs/frozen/`](r6_debugger_training/runs/frozen/) (24 tracked JSON files), [`docs/professor_traces/`](../docs/professor_traces/README.md), `docs/project-closeout.md` §3 & §6.

### 7. S4 — cp118 + Frozen Repository RAG (`cp118_rag_definitive/`)
- **What it was:** Controlled evaluation of the cp118 localized-repair checkpoint paired with frozen repository RAG over QuixBugs under one-shot protocol v1.2.1.
- **Why it existed:** Tested whether adding repository RAG to the cp118 localized-repair model improved its 0/40 repair rate.
- **Model / Checkpoint:** cp118 adapter (tree `65b5ed9a…`) on Qwen2.5-Coder-7B base + frozen repo-mode retrieval.
- **Dataset / Task Scope:** Frozen 40-task QuixBugs cohort (`quix40`).
- **Main Result:** 10/40 tasks produced valid pairs before termination due to compute/runtime constraints (5/10 reached 4096-token cap). Primary correctness: **NOT_EVALUATED**; patch apply: **NOT_EVALUATED**; P2P: **NOT_RECORDED**.
- **Accepted Interpretation:** **CLOSED — PARTIAL / COMPUTE-CONSTRAINED / NOT_EVALUATED**. No scientific RAG claim is made from this partial condition.
- **Status:** CLOSED / PARTIAL / NOT_EVALUATED.
- **Canonical Evidence:** [`experiments/cp118_rag_definitive/RESULT.md`](cp118_rag_definitive/RESULT.md), [`experiments/cp118_rag_definitive/s4_contract.json`](cp118_rag_definitive/s4_contract.json), `docs/final-report.md` §10.

### 8. Tuned-Model Interactive Debugger Pilot v1 (`tuned_debugger_pilot_v1/`)
- **What it was:** Earliest frozen infrastructure and contract for a 10-case tuned-vs-RAW interactive debugger pilot (professor TODO #23–25).
- **Why it existed:** Designed to evaluate static baseline vs. PDB-on-uncertainty with early fine-tuned adapters.
- **Model / Checkpoint:** Pinned Qwen2.5-Coder-7B base + supplied PEFT adapter or `--base-only`.
- **Dataset / Task Scope:** 5 curated tasks under static vs. PDB conditions.
- **Main Result:** Early pilot did not yield a valid tuned-vs-RAW comparison because provider/protocol failures prevented meaningful evaluation.
- **Accepted Interpretation:** Preserved as frozen infrastructure and contract provenance; superseded by the accepted R1–R6 sequence.
- **Status:** HISTORICAL / SUPERSEDED.
- **Canonical Evidence:** [`experiments/tuned_debugger_pilot_v1/README.md`](tuned_debugger_pilot_v1/README.md), [`experiments/tuned_debugger_pilot_v1/experiment_contract.json`](tuned_debugger_pilot_v1/experiment_contract.json), `docs/project-closeout.md` §4.

### 9. Local Inference Optimization (`local_inference_perf/`)
- **What it was:** Fail-closed packaging of the Windows PyTorch + Qwen2.5-Coder-7B GQA attention workaround into a benchmark and parity harness.
- **Why it existed:** Resolved severe slow-MATH SDPA fallback on Windows (PyTorch dev build lacking FlashAttention) by expanding KV heads and forcing `SDPBackend.EFFICIENT_ATTENTION`.
- **Model / Checkpoint:** Qwen2.5-Coder-7B base + cp118 adapter.
- **Dataset / Scope:** 50-token numerical parity test and prompt length benchmarks (6079 + 1/256/1024 tokens).
- **Main Result:** Significant speedup on 6079+1 tokens (301.4s / 15.3 GiB → 3.56s / 7.2 GiB) with numerical parity (`cosine=0.9999645`, `same_top_token=True`).
- **Accepted Interpretation:** Engineering packaging milestone enabling feasible local inference; not a software repair result.
- **Status:** COMPLETE / ENGINEERING ACCEPTED.
- **Canonical Evidence:** [`experiments/local_inference_perf/README.md`](local_inference_perf/README.md), [`experiments/local_inference_perf/tests/`](local_inference_perf/tests/).
