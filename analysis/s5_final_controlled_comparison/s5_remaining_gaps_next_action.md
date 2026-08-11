# S5 Remaining-Gap / Next-Action Decision

**Schema:** `s5-remaining-gaps-next-action-v1`
**Baseline HEAD:** `acfe131a0a99b994fd3d34e520d0022191246025`
**Authoritative plan:** `Agentic_Debugging_Master_Execution_Plan_2026-08-11_S5_CURRENT.md`

This artifact answers the S5 task's required GO/NO-GO question — "Given the new Efficient SDPA capability, is a fresh full 40-task optimized S4 rerun scientifically necessary before final report closeout?" — with **two independent fields**, separating scientific necessity from execution authorization (per amendment 5).

---

## 1. S4 optimized-rerun decision

### `scientific_necessity`: **NO-GO**

**Decision logic applied:** Scientific necessity answers whether leaving cp118+RAG primary correctness as `NOT_EVALUATED` prevents an honest closeout of a load-bearing project/professor claim.

- If the missing RAG correctness result is required for a load-bearing final claim → `scientific_necessity = GO`
- If the project can honestly close out with RAG explicitly reported as PARTIAL / NOT_EVALUATED and no final claim depends on knowing its correctness → `scientific_necessity = NO-GO`

**Reasoning:**

1. S5's stated purpose (master plan §S5, task prompt) is **synthesis of accepted evidence**, not another model campaign. The task explicitly forbids "creating a new experiment merely to fill a missing metric."
2. S4 is frozen as `PARTIAL / COMPUTE-CONSTRAINED`; the accepted interpretation (master plan §S4) is "measurement, not rescue." The cohort completed 10/40 tasks = first 10 in frozen manifest order (NOT random / NOT representative).
3. The canonical comparison can **honestly close out the cp118+RAG axis** with its primary correctness metrics recorded as `NOT_EVALUATED` and its descriptive 10/10 evidence (5/10 truncation, per-task latency/tokens) recorded as descriptive-only with no extrapolation to 40. No load-bearing project/professor claim depends on knowing the RAG correctness result:
   - The professor-facing TODO #23/#24/#25 coverage matrix does **not** depend on the cp118+RAG repair score; it depends on real-model debugger behavior, which is governed by conditions D (D1 RAW) and E (S2 cp118), not C.
   - The "real model → patch → verifier" static claim is already satisfied by the QuixBugs gcd path (auxiliary `static_verifier_success` axis).
   - The fine-tuning-transfer claim is governed by the RAW-vs-cp118 RAG-OFF comparison (conditions A vs B), which is fully populated; RAG is a separate `rag_treatment` axis.
4. Therefore leaving cp118+RAG primary correctness as `NOT_EVALUATED` does **not** prevent an honest closeout of any load-bearing claim. `scientific_necessity = NO-GO`.

### `current_execution_authorization`: **NOT_AUTHORIZED_IN_S5**

Per the S5 task prompt (lines 36–43): no model run, no cp118 run, no S4 resume, no new experiment. Live provider/model execution is off by default (`AGENTS.md` §12). This authorization state holds **regardless of the scientific recommendation**.

---

## 2. If scientific GO were reached (hypothetical future conditions)

If new evidence later makes `scientific_necessity = GO` (e.g., a load-bearing final claim comes to depend on the cp118+RAG correctness result), a future authorized campaign would require:

1. **Explicit owner (Onur) + Main FirstMate authorization** for live model execution and a new campaign.
2. **A fresh frozen contract** distinct from `966c2aaba413d6f688ad9095b47c2c0d3c6936ea67bc95acb52fd9a1df5745bd` (new contract SHA256, new run identity).
3. **Strict separation from the existing 10 stock-generated tasks** — the old first-10 stock-generated pairs (`runs/run-1-live-2026-08-10/`) must not be mixed with optimized future tasks (per master plan §S4: "Do not mix stock-generated first 10 tasks with hypothetical optimized future tasks").
4. **A full 40-task completion marker** (`S4_GENERATION_COMPLETE.json` with `valid_pairs==40`) before the frozen C9 v1.2.1 CPU evaluator runs (the evaluator fails closed if `len(rows) != len(manifest)`).
5. **Frozen protocol/contract identity** recorded before generation; identical cp118 adapter identity (`65b5ed9a...`); identical frozen 40-task cohort manifest (`572082482a64...`).
6. **GPU-memory telemetry recorded with correct semantics**: allocator peak (`torch.cuda.max_memory_allocated`) AND, if desired/available, an independent device/residency measurement, as **separate** measurements — not a single conflated "physical VRAM" figure. The future run must NOT label allocator peak as `vram_physical` or `physical_resident_vram_usage`; GPU capacity must not be reported as workload usage.

The Efficient SDPA capability (commit `10bdfa91...`) would remove the dominant compute constraint. Per BLOCKER 3 the accepted performance evidence is two distinct blocks: (A) a **matched-prompt controlled diagnostic** at 6079 input + 1 output — stock (MATH-SDPA) 301.399 s / torch peak allocated 15,350.3 MiB vs efficient (repeat_kv) 3.562 s / 7,371.0 MiB, a ~84.6× speedup (matched model/config, only the attention path differs; this is the strongest causal A/B); (B) a **reusable harness reproduction** of the optimized path at 6113+1 (4.982 s) and 6113+256 (63.806 s) with torch allocator peak ~7,380 MiB — this has NO newly-run matching stock 6113-token counterpart and is NOT a matched A/B. Real-model parity: same top token, cosine 0.99995, max-abs 0.125, mean-abs 0.014. Per BLOCKER 2, these MiB/GiB figures are **torch/CUDA allocator peak, NOT physical/resident VRAM** (overcounts under Windows WDDM); the 12,227 MiB figure is GPU capacity, not workload residency; `physical_resident_vram_usage` remains NOT_RECORDED. This is the engineering enabler that would make a future 40-task optimized run feasible in a reasonable wall-clock budget — but it is **engineering evidence, not a retroactive change to S4**, and does not by itself justify a rerun. The ~84.6× speedup references ONLY the matched 6079-token diagnostic; the 6113-token harness measurements are optimized reproduction.

---

## 3. Remaining gaps (all axes)

### Localized executable repair (A vs B)
- **Populated.** RAW 5/40 RESOLVED (supplied-oracle), 20/40 apply (Track B semantic), 33/40 strict (Track A). cp118 0/40 RESOLVED, 0/40 apply, 40/40 semantic extraction, 19/40 truncation. Interpretation: formulation-specific negative executable-repair transfer (output-policy degeneration / over-generation / scope / serialization), NOT "fine-tuning is bad."

### Fine-tuning transfer (auxiliary DPO)
- **Populated as prose-only.** B1 27/30, matched SFT 27/30, DPO 21/30. Authentic new DPO is CLOSED / NOT JUSTIFIED. No in-repo frozen result file.

### RAG treatment (C)
- **Primary correctness NOT_EVALUATED** (10/40 partial). Descriptive 10/10 evidence recorded. No RAG success/failure claim.

### Debugger interaction (D, E)
- **Bounded negative result.** D1 RAW: 1 PDB command (`break 20`), tool_error, 0 successful observations, Gate B/C FAIL. S2 cp118: 1 PDB command (`continue`), rejected, 0 successful observations, Gate B/C FAIL. Neither established a successful iterative debugger loop.

### Model-generated test capability (S1-P original)
- **Populated.** RAW generated an executable failing regression test on first attempt from an explicit behavior spec (tokens 957 = 776+181). Original live patch did NOT apply; original live outcome NOT RESOLVED.

### Serialization sensitivity (S1-P post-hoc)
- **Populated as prose-only.** Post-hoc deterministic serialization normalization (no semantic body-line changes; semantic-hunk hash preserved) → test PASS 1/1, verifier F2P 1/1, P2P 2/2, RESOLVED. Kept strictly separate from original live.

### Static verifier success (QuixBugs gcd)
- **Populated.** Real model → patch → verifier RESOLVED, F2P 5/5, P2P 1/1. Does NOT demonstrate debugger use.

### Local inference engineering (Efficient SDPA)
- **Populated as appendix.** Two-block evidence: (A) matched 6079+1 diagnostic ~84.6× speedup (301.399 s → 3.562 s); (B) optimized harness reproduction at 6113+1/6113+256 (no matched stock counterpart). torch/CUDA allocator peak ~7.4 GiB (NOT physical VRAM); real-model parity cosine 0.99995. Engineering only; not a retroactive change to S4; not collapsed into any repair score.

---

## 4. Next-action decision

1. **Do NOT run a fresh optimized S4 rerun in S5.** (`scientific_necessity = NO-GO`; `current_execution_authorization = NOT_AUTHORIZED_IN_S5`.)
2. **Record the Efficient SDPA capability as a future-work enabler** in the technical report (S8), with the precise future GO conditions in §2 above.
3. **Carry the bounded negative debugger result (D1 + S2) forward as the accepted real-model debugger evidence**; do not open S3 (already skipped: trigger condition 1 false).
4. **The immediate next stage per the master plan is S6 (Real-Model Dynamic Debugging Demo)** — but S6 is `BLOCKED BY REAL DEBUGGER TRAJECTORY`; no real positive trajectory exists in the accepted evidence. The demo must distinguish model decisions from deterministic tooling and must not replay a scripted success as if it were model behavior. Whether S6 can proceed with the bounded negative evidence (an honest "negative-result demo") or remains blocked is a Main FirstMate / owner decision.
5. **No Git commit, push, merge, or stage** in S5. Candidate left for FirstMate review.