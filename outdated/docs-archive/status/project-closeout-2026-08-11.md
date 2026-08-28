# Agentic Debugging — Project Closeout (2026-08-11)

**Project:** Academic Agentic Debugging Internship Project
**Owner:** Onur
**Execution control:** Main FirstMate
**CURRENT_STAGE:** `S9 — Final Reproducibility / Git / Project Closeout`
**PROJECT STATUS:** **COMPLETE** — closes on the accepted bounded-negative path
**Closeout branch:** `closeout/s9-final-reproducibility-v1`
**Accepted S8 baseline (HEAD):** `fbc2479dfefca6c8d51a21b789d485042688143f`
**Final closeout commit:** the Git commit containing this canonical closeout
document (exact S9 SHA is reported by the final Git operator after Main
FirstMate acceptance; no SHA is invented here).

This is the single authoritative final project-status and fresh-reviewer
handoff artifact. It supersedes the untracked working copies
`Agentic_Debugging_Master_Execution_Plan_2026-08-11_S5_CURRENT.md`,
`Agentic_Debugging_Master_Execution_Plan_2026-08-10_S4_CURRENT.md`, and
`REPO_STATE_2026-08-10.txt` — those remain on disk, are marked **SUPERSEDED**,
and are not authoritative. The tracked historical
`Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` and
`docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` are historical snapshots and
remain unchanged.

---

## A. Project Goal

Build and evaluate a systematic, verifier-backed, single-controller agentic
debugging system in which a real code-capable LLM reproduces a failure,
localizes code, forms a root-cause hypothesis, uses real PDB runtime tools
(breakpoint/stack/locals/step/continue), updates its diagnosis from runtime
evidence, produces a patch, and reaches an independent verifier with F2P/P2P/
RESOLVED — with reproducible evidence for the professor, technical report, and
final demo. The final system must separate model capability, retrieval,
tool/interface behavior, controller behavior, debugger backend, patch
serialization, and executable verification.

## B. Final Stage Table

| Stage | Status | Final result |
|---|---|---|
| S0 — Branch/evidence closeout | DONE | Provenance-clean baseline for the debugger-interaction campaign; 204 focused tests passed. |
| S1 — Debugger interaction v2 (RAW feasibility) | DONE — NEGATIVE | RAW authored one PDB attempt (`break 20`), backend returned an error; 0 successful non-error observations; Gate B/C FAIL. STOP rule applied; no interface hardening campaign. |
| S1-P — Model-generated regression-test probe | DONE — AUXILIARY | Frozen RAW generated an executable test that failed on the buggy code; post-hoc serialization normalization (no semantic change) then passed test + verifier (F2P 1/1, P2P 2/2, RESOLVED). Original live patch itself did not apply; both results always reported separately. |
| S2 — cp118 on the frozen D1 runtime-entry treatment | DONE — NEGATIVE | cp118 authored one PDB command (`continue`), backend rejected it (no active session); 0 successful observations; Gate B/C FAIL. |
| S3 — Conditional debugger-oriented post-training | SKIPPED — TRIGGER NOT MET | Trigger 1 false (RAW never demonstrated a working debugger trajectory); no new training run justified. |
| S4 — Definitive cp118 + frozen RAG treatment | DONE — PARTIAL / COMPUTE-CONSTRAINED | 10/40 valid frozen pairs (first 10 in manifest order); campaign stopped for compute feasibility; primary C9 correctness **NOT_EVALUATED**; no RAG success/failure claim. |
| S5 — Final controlled comparison | DONE | Canonical 8-axis ledger from accepted frozen evidence only; explicit `NOT_RECORDED` / `NOT_EVALUATED` missingness; no fabricated four-way correctness matrix. |
| S6 — Real-model debugging evidence presentation | DONE — BOUNDED-NEGATIVE PRESENTATION | Self-contained static HTML; `presentation_reproducible = YES`, `positive_real_model_dynamic_debugger_demo = NO`, `bounded_negative_real_model_evidence_presented = YES`. |
| S7 — Focused literature closeout | DONE | 20 works reviewed across debugger-aware systems, tool-using SWE agents, runtime evidence, multi-agent debugging, tool/trajectory post-training; provenance tiers preserved. |
| S8 — Final technical report + internship diary | DONE | `docs/FINAL_TECHNICAL_REPORT_V2.md` (evidence-backed, forbidden-claim audit passed); `diary/diary.md` complete through 2026-08-11, non-fabricated chronology. |
| S9 — Final reproducibility / Git / project closeout | FINAL CLOSEOUT | This document; bounded deterministic validation; status/TODO reconciliation; Git integration prepared. |

## C. Final Scientific Conclusions

1. **RAW baseline (QuixBugs quix40 cohort).** Track A (strict;
   local-untracked accepted CSV evidence): 33/40 strict extraction, 14/40
   apply, 5/40 RESOLVED. Track B (semantic, master-plan aggregates): 40/40
   extraction, 20/40 apply, 5/40 RESOLVED. The tracks are deliberately kept
   separate. Track A provenance: scientific source
   `experiments/raw-pilot-v1.1/results/{results_final,metrics_summary}.csv`;
   scientific provenance `local_untracked_accepted`; tracked in final HEAD:
   NO; clean checkout available: NO; reproducibility carrier: the tracked S5
   canonical comparison/provenance artifacts
   (`analysis/s5_final_controlled_comparison/s5_comparison_ledger.json`,
   condition `A_raw_frozen_repair`, and `s5_controlled_comparison_report.md`),
   which bind the accepted 33/40 / 14/40 / 5/40 values to those local CSVs.
2. **Localized-repair QLoRA SFT did not transfer to executable repair.**
   cp118 (`Qwen/Qwen2.5-Coder-7B-Instruct` @
   `c03e6d358207e414f1eca0bb1891e29f1db0e242`) produced 0/40 applicable
   patches and 0/40 RESOLVED (vs RAW 20/40 apply, 5/40 resolved), dominated
   by output-policy degeneration, over-generation, scope explosion (39/40
   extra-file scope violations) and truncation (19/40). This is a
   formulation-specific negative transfer result — **not** "fine-tuning is
   bad" and **not** "cp118 is universally worse".
3. **DPO is closed.** Historical controlled result: B1 baseline 27/30,
   matched SFT 27/30, DPO 21/30. Insufficient clean homogeneous authentic
   data; new authentic DPO campaign **CLOSED / NOT JUSTIFIED**.
4. **RAG infrastructure is done; the cp118+RAG treatment is partial.**
   Deterministic lexical repository RAG implemented and accepted. The frozen
   cp118+RAG treatment produced 10/40 valid pairs (compute-constrained);
   primary correctness **NOT_EVALUATED**; truncation 5/10 is descriptive
   only and is not extrapolated to 40.
5. **The real-model dynamic debugger loop is a bounded, well-instrumented
   negative result.** D1 (RAW): one model-authored `break 20` reached the
   real backend and returned a tool error; 0 successful observations; no
   second command. S2 (cp118): one `continue` rejected; 0 observations.
   Gate B strict FAIL for both. Debugger effectiveness was therefore never
   positively measured.
6. **Static real-model repair is demonstrated, debugger-informed repair is
   not.** A real-provider static path reached verifier RESOLVED on QuixBugs
   gcd (F2P 5/5, P2P 1/1) — this is a separate static axis and does NOT
   demonstrate debugger use.
7. **S1-P auxiliary evidence.** Given an explicit expected-behavior
   specification, frozen RAW generated an executable regression test exposing
   the bug; the separately model-produced semantic repair satisfied the
   frozen test and the independent verifier only after deterministic
   post-hoc serialization normalization (original raw live patch did not
   apply). Original and post-hoc results are always reported separately.
8. **Literature (S7).** Runtime evidence can materially improve debugging and
   repair; raw debugger exposure alone does not reliably cause competent use;
   ordinary localized-repair SFT does not automatically teach debugger
   competence; multi-agent designs are credible but not a universal
   prerequisite. The single-agent + deterministic-controller + typed-tool
   design was defensible.
9. **Central contribution.** The project delivers infrastructure, evaluation
   methodology, and an honestly established bounded-negative experimental
   result — not a claim of debugging performance or PDB effectiveness.

## D. Professor TODO Reconciliation

Final statuses per professor item (27 items; numbering per
`docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md`). Negative outcomes are not
converted into positive checkmarks; explicit wording carries the semantics.
The S5 coverage matrix (`analysis/s5_final_controlled_comparison/`) is the
per-item evidence authority for #23–25.

| # | Item (short) | Final status |
|---|---|---|
| 1–2, 4–5 | Literature (debugging, LLM-based, comparison, systems) | DONE (2026-08-05 reviewed syntheses, `3c23b6e`) |
| 3 | Agentic / tool-using / multi-agent debugging literature | DONE (S7 closeout, 20 works, `677992f`) |
| 6–8 | Dataset research / comparison / selection | DONE (SWE-rebench V2 selected for SFT; BugsInPy license-gated, QuixBugs used) |
| 9 | Dataset analysis + train/test split | DONE — SWE-rebench V2: 1,594 tasks / 347 repos; split 1,000/150/444 (seed `20260808`, repo-overlap 0); 940/135 no-truncation view |
| 10 | Model selection | DONE — `Qwen/Qwen2.5-Coder-7B-Instruct` @ `c03e6d35…` |
| 11 | Instruction-response conversion | DONE — SFT formulation: problem + oracle-file-localized source → `PATCH` + gold diff (localized-repair formulation) |
| 12 | QLoRA supervised fine-tuning | DONE — cp118 (definitive surviving saved checkpoint) |
| 13 | Pre/post fine-tuning comparison | DONE — RAW vs cp118 (negative transfer; see §C.2) |
| 14 | RAG system | DONE (2026-08-06, deterministic repository-native lexical RAG) |
| 15 | Combine fine-tuned model with RAG | CLOSED — PARTIAL / COMPUTE-CONSTRAINED; primary correctness **NOT_EVALUATED**; no RAG success/failure claim. Not an active future task. |
| 16–17 | File/code-search/test/patch tools; debugging agent | DONE (controller state machine, typed tools, verifier) |
| 18 | Model localization / root-cause / patch | PARTIAL — static repair demonstrated; full dynamic debugger-informed chain NOT achieved (bounded negative) |
| 19 | Preference dataset | DONE as bounded historical controlled preference data + exporter v1; authentic production corpus CLOSED / NOT JUSTIFIED |
| 20 | DPO / RLHF | DONE as bounded historical controlled investigation (27/30 / 27/30 / 21/30) — negative; new authentic campaign CLOSED / NOT JUSTIFIED |
| 21 | Base / tuned / RAG / agentic comparison | DONE through S5 with explicit missingness (`NOT_RECORDED` / `NOT_EVALUATED`); NOT a fabricated complete four-way correctness matrix |
| 22 | Debugger adapter (PDB/GDB/LLDB) | DONE — PDB (the "veya/or" option) |
| 23 | Fine-tuned model generates debugger commands and interprets output | `[ ]` — **CLOSED — BOUNDED NEGATIVE**: engineering capability YES; deterministic engineering evidence YES; positive real-model success NO; bounded-negative evidence YES (D1 `break 20` → tool error; S2 `continue` → rejected; 0 successful observations). Not authorization for more experiments. |
| 24 | Breakpoint / variables / stack / step interaction | `[ ]` — **CLOSED — BOUNDED NEGATIVE**: engineering capability YES; positive real-model sequence NO; bounded-negative evidence YES (no real-model breakpoint→observation→step/locals sequence in either condition). |
| 25 | Debugger → patch → tests/verifier | `[ ]` — **CLOSED — BOUNDED NEGATIVE**: static model→patch→verifier YES (gcd RESOLVED, non-debugger); debugger-informed real-model patch→verifier NO; bounded-negative evidence YES. |
| 26 | Evaluate success/localization/root-cause/cost/runtime | DONE through S5 with `NOT_RECORDED` / `NOT_EVALUATED` where appropriate (8 axes kept separate) |
| 27 | Working agentic debugging demo + technical report | DONE — demo/tooling (deterministic offline demo + S6 presentation) and technical report V1 (2026-07-31) + **V2** (S8, `docs/FINAL_TECHNICAL_REPORT_V2.md`); S6 is a bounded-negative evidence presentation, not a positive debugger demo |

## E. Canonical Evidence / Artifact Paths

| Stage | Path (all tracked) |
|---|---|
| S5 | `analysis/s5_final_controlled_comparison/` — `s5_comparison_ledger.json`, `s5_controlled_comparison_report.md`, `s5_professor_todo_coverage_matrix.{md,json}`, `s5_provenance_source_map.md`, `s5_remaining_gaps_next_action.md` |
| S6 | `presentation/s6-real-debugging-evidence/` — `index.html` (self-contained static HTML), `s6_presentation_manifest.json` |
| S7 | `research/literature/agentic_debugging_literature_closeout_2026-08-11.md` |
| S8 | `docs/FINAL_TECHNICAL_REPORT_V2.md`; `diary/diary.md` (through 2026-08-11, incl. S9 subsection) |
| S9 | `Agentic_Debugging_Project_Closeout_2026-08-11.md` (this document) |

Supporting tracked evidence: `tests/golden_trajectories/data/quixbugs-gcd-pdb-reachability-captured-result.json`, `docs/QUIXBUGS_EIGHT_TASK_BASELINE_V1.md`, `agentic_debugger/runtime/pdb_protocol.py`, `tests/integration/test_pdb_interactive_controls.py`, `experiments/cp118_rag_definitive/` (S4 source/contract), `experiments/tuned_debugger_pilot_v1/`.

## F. Reproducibility Audit

**F.1 Model identity**
- Base: `Qwen/Qwen2.5-Coder-7B-Instruct`, frozen revision `c03e6d358207e414f1eca0bb1891e29f1db0e242`.
- cp118: definitive surviving saved checkpoint (QLoRA PEFT); identity byte-verified at S2 run time; adapter tree `65b5ed9a354d4b2c03ba86e2b8065118e11abab9c439cb481b5739f1b86e7c00`.
- S4 binding: source commit `acfe131a0a99b994fd3d34e520d0022191246025`; contract SHA256 `966c2aaba413d6f688ad9095b47c2c0d3c6936ea67bc95acb52fd9a1df5745bd`; run identity SHA256 `072f1d693cfd07049c47ff6f7826eda17b24a22cd19476e97d50c328e56c72ab`.

**F.2 Dataset identity**
- SWE-rebench V2 accepted corpus: 1,594 eligible authentic Python repair tasks / 347 repos.
- Frozen split: 1,000 train / 150 validation / 444 unused; train↔validation repo overlap = 0; protected evaluation repo overlap = 0; seed `20260808`.
- No-truncation (≤32K) training view: 940 train / 135 validation.
- QuixBugs kept outside SFT training (evaluation-only).

**F.3 Scientific result identity**
- RAW Track A vs Track B kept separate (local-untracked accepted strict CSV evidence vs semantic aggregates; 33/40 vs 40/40 extraction; 14/40 vs 20/40 apply; 5/40 resolved both). Note: the S5 ledger labels Track A's tier `frozen_in_repo` per its own vocabulary for locally-frozen accepted evidence; the RAW CSVs themselves are NOT Git-tracked in the final HEAD (see §C.1 provenance fields).
- cp118 (B, RAG-OFF): 40/40 extracted, 0/40 apply, 0/40 resolved.
- DPO: 27/30 / 27/30 / 21/30 — closed, not justified.
- S4: 10/40 partial, primary metrics `NOT_EVALUATED`.
- D1 (RAW): `break 20` → tool error, 0 successful observations, Gate B/C FAIL. S2 (cp118): `continue` → rejected, 0 observations, Gate B/C FAIL.
- S1-P: original live result (patch did not apply) and post-hoc normalization result (F2P 1/1, P2P 2/2, RESOLVED) always reported separately.
- Static gcd: F2P 5/5, P2P 1/1, verifier RESOLVED — non-debugger static axis.
- S6 status semantics: `presentation_reproducible = YES`; `positive_real_model_dynamic_debugger_demo = NO`; `bounded_negative_real_model_evidence_presented = YES`.

**F.4 Provenance**
- Scientific source tiers (S5): `frozen_in_repo` / `aggregate_external_per_task` / `master_plan_prose_only` / `review_navigation_only`. Tier is recorded per metric in `s5_comparison_ledger.json`.
- Clean-checkout availability: all canonical artifacts (S5/S6/S7/S8/S9) are tracked; S6 manifest records `tracked_in_git` and `clean_checkout_available` per source with reproducibility carriers.
- Untracked/prose-only sources are NEVER promoted to clean-checkout reproducibility.
- `_ai-review/` packages are review material and are never treated as scientific authority.

**F.5 Report / diary consistency**
- S8 `FINAL_TECHNICAL_REPORT_V2.md` metrics agree with the S5 ledger (RAW/cp118/DPO/S4/D1/S2/gcd/SDPA figures verified — no contradictions).
- S7 evidence tiers (peer-reviewed vs preprint vs technical report) preserved; preprints not silently promoted.
- TODO #23–25 wording matches S5/S6 (CLOSED — BOUNDED NEGATIVE).
- Diary chronology is commit/frozen-run sourced; no invented dates, hours, or SHAs; S9 subsection appended only (S8 prefix byte-identical).

## G. Tracked vs Local / Untracked Provenance

Four distinct concepts are kept separate; historical-branch tracking does NOT
make live evidence available in the final HEAD.

**A. Tracked in the FINAL HEAD / available from a final clean checkout**
Canonical S5/S6/S7/S8/S9 artifacts, project source, tests, golden trajectory
data, and S4 source/contracts (`experiments/cp118_rag_definitive/`). These are
clean-checkout reproducible.

**B. Tracked on a historical experiment branch (Git-reachable, NOT in the
final HEAD)**
Historical implementation/source commits only — e.g. D1 experiment-local
source (changes under `experiments/debugger_interaction_v2_d1` on branch
`experiment/debugger-interaction-v2-d1`), S1-P post-hoc serialization source
(commit `9e1b9dc9…`, branch `experiment/model-generated-test-probe-serialization`),
S2 run sources (branch `experiment/cp118-debugger-d1`). Being Git-reachable on
a historical branch is NOT the same as being final-HEAD clean-checkout
evidence.

**C. Local-untracked live scientific evidence (documented, kept on disk)**
- D1 live-run evidence: `experiments/debugger_interaction_v2_d1/runs/run-1-live-2026-08-10/` — `local_untracked_accepted`, SHA256-verified and bound in the S6 manifest; NOT available from final clean checkout.
- S4 run evidence: `experiments/cp118_rag_definitive/runs/run-1-live-2026-08-10/` — `local_untracked_accepted` (the S4 contract/source is tracked in final HEAD; the raw run pairs are local).
- RAW Track A CSV evidence: `experiments/raw-pilot-v1.1/results/{results_final,metrics_summary,failure_taxonomy_counts}.csv` — `local_untracked_accepted`; tracked in final HEAD: NO; clean checkout available: NO.
- SWE-rebench V2 corpus/split dirs, RAW pilot working dirs, `_ai-review/`, `operator/`.

**D. Tracked canonical reproducibility carriers**
`analysis/s5_final_controlled_comparison/` (`s5_comparison_ledger.json`
condition `A_raw_frozen_repair` carries 33/40 / 14/40 / 5/40 with source refs
to the local CSVs; `s5_controlled_comparison_report.md`;
`s5_professor_todo_coverage_matrix.json`), `s6_presentation_manifest.json`,
`docs/FINAL_TECHNICAL_REPORT_V2.md`. Untracked/prose-only sources are NEVER
promoted to clean-checkout reproducibility; carriers bind them to hashes/tiers.

Consistency with §H: D1 live-run evidence = local/untracked accepted; S1-P
post-hoc result has NO dedicated frozen result artifact in the working tree
(carried by the committed S5/master-plan synthesis where present); RAW Track A
CSVs = local/untracked accepted; S4 run evidence = local/untracked accepted;
canonical S5/S6/S7/S8/S9 artifacts = tracked / clean-checkout available.

## H. Known Provenance Gaps

From `s5_comparison_ledger.json` (`known_provenance_gaps`), unchanged by S9:

1. S1 original raw-run artifact MISSING from disk (live run evidence, handoff files). D1 evidence intact and SHA256-verified.
2. S2 "5 model calls / 3226 tokens" is master-plan prose, NOT sourceable to any frozen in-repo artifact.
3. S1-P post-hoc serialization result: source commit `9e1b9dc9…` + master-plan result, but no dedicated frozen result artifact in the working tree.
4. cp118 definitive (B) per-task evidence is external (Drive-hosted D7 bundle); only accepted aggregates are in the current repo.
5. Master-plan §2.10 claim that "rejected RAW model text was not retained" contradicts on-disk D1 evidence (14 rejected decodes retain `raw_response_text`); resolved in favor of on-disk evidence.
6. S4 per-task `peak_allocated_gib` exceeds the physical 12,227 MiB device cap; treated as `NOT_RECORDED` for physical VRAM, descriptive torch-allocated telemetry only.
7. RAW Track B "20/40 applied / 40/40 extracted" is NOT computed in the
   local-untracked accepted RAW Track A CSV evidence
   (`experiments/raw-pilot-v1.1/results/`); both tracks recorded distinctly.

## I. Explicit Non-Claims

- **No positive real-model dynamic debugger trajectory occurred.** No successful iterative PDB loop, no runtime-evidence-informed diagnosis, no debugger-informed patch reached the verifier from a real model.
- **No RAG success or failure claim** — the cp118+RAG condition is partial with primary correctness `NOT_EVALUATED`.
- **No "fine-tuning is bad" / "cp118 is universally worse" claim.**
- **No DPO benefit claim**, and no claim that DPO is an open task — it is closed with a negative result.
- **No fabricated complete four-way correctness matrix** — S5 carries explicit `NOT_RECORDED` / `NOT_EVALUATED` missingness.
- **No claim that debugger effectiveness was measured** — the treatment never produced successful observations.
- **No claim that the S1-P post-hoc normalized result is the live model result.**
- **No multi-agent implementation, no GDB/LLDB backend, no new benchmark campaign.**
- **`_ai-review/` packages are not scientific evidence.**
- **No claim that deterministic/scripted PDB success equals model success.**
- The closed statuses of TODO #23–25 and #15 are **not authorization for new experiments.**

## J. Final Deterministic Test / Smoke Results (S9 BUILD, 2026-08-11)

| Check | Command | Result |
|---|---|---|
| Compile/import sanity | `python -m compileall agentic_debugger scripts -q` | PASS |
| Golden trajectories | `python -m pytest tests/golden_trajectories -q` | 11 passed |
| Focused deterministic unit subset (controller, controller_policy, tool_registry, state_machine_contract, pdb_protocol, pdb_session, pdb_post_mortem, patcher, workspace, test_runner, evaluation_runner, event_replay, demo_tools) | `python -m pytest tests/unit/… -q` | 1,253 passed (1,264 with golden) |
| Integration (PDB interactive controls, verifier, patch lifecycle, demo e2e, post-mortem trajectory) | `python -m pytest tests/integration -q` | 368 passed |
| Deterministic offline demo smoke | `python -m agentic_debugger.demo --output-dir demo-out --task-id curated-off-by-one-002` | PASS — exit 0, 2 cases, `Model backend: offline-deterministic-demo`, `Network-access policy: blocked-in-process`; `demo-out/` removed after evidence capture |
| S6 browser smoke (headless, installed Chrome) | `chrome --headless=new --dump-dom file:///…/presentation/s6-real-debugging-evidence/index.html` | PASS — exit 0; DOM returned (22,173 bytes); `s6.negative_result_banner` and "This is NOT a successful debugger demo" present |
| S6 browser smoke (headless, installed Edge) | `msedge --headless=new --dump-dom …` | Attempted; exit 0 but empty DOM dump (0 bytes) — recorded honestly; no blocker (structural S6 validation already accepted) |
| Git checks | `git diff --check`, `git status --porcelain`, `git rev-parse HEAD` | Clean diff; candidate status recorded in review package |

The full 3,385-test unit suite was intentionally not re-run (no code changed;
bounded subset is the accepted final validation surface).

## K. Local Untracked-State Classification

All items below are **kept on disk**; nothing was deleted in S9. Full inventory
is in the review package (`_ai-review/s9-final-reproducibility/untracked-inventory.md`).

| Item | Size (approx.) | Class |
|---|---|---|
| `experiments/raw-pilot-v0.1` | 436 K | Accepted local scientific evidence — keep |
| `experiments/raw-pilot-v1.1` | 1.1 G | Accepted local scientific evidence — keep |
| `experiments/swe_rebench_v2_corpus` | 231 M | Accepted local scientific evidence (dataset corpus) — keep |
| `experiments/swe_rebench_v2_golden_verifier` | 52 M | Accepted local scientific evidence — keep |
| `experiments/swe_rebench_v2_census` | 136 K | Accepted local scientific evidence — keep |
| `experiments/swe_rebench_v2_context_ablation` | 56 K | Accepted local scientific evidence — keep |
| `experiments/swe_rebench_v2_split` | 44 K | Accepted local scientific evidence — keep |
| `experiments/swe_rebench_v2_static_pilot` | 64 K | Accepted local scientific evidence — keep |
| `Agentic_Debugging_Master_Execution_Plan_2026-08-11_S5_CURRENT.md` | 36 KB | **SUPERSEDED** historical working copy — left on disk, not authoritative |
| `Agentic_Debugging_Master_Execution_Plan_2026-08-10_S4_CURRENT.md` | 34 KB | **SUPERSEDED** historical working copy — left on disk, not authoritative |
| `REPO_STATE_2026-08-10.txt` | 3 KB | **SUPERSEDED** historical Git snapshot — left on disk |
| `D1_candidate.patch` | 56 KB | Disposable review convenience (D1 delta tracked on its branch) — left on disk |
| `AI_REVIEW/` | — | Disposable S1-era review handoffs — left on disk |
| `.opencode/` | — | Tool-local state (verified: no project source/evidence) — ignored via root `.gitignore` |
| `.claude/`, `.codex/` | — | Tool-local state — already excluded via `.git/info/exclude`, untouched |
| `_ai-review/`, `operator/`, `outputs/`, `artifacts/`, `tmp/`, caches | — | Ignored by design (`.gitignore`) — no action |

## L. Final Git Integration Instructions

To be executed by the final Git operator **after Main FirstMate acceptance** of
the S9 candidate. Verified topology: `origin/main` (`da4df94`) is an ancestor
of local `main` (`1ff3571`), which is an ancestor of the S9 chain
(`acfe131 → c20133a → da2e6dd → 677992f → fbc2479 → S9 closeout commit`), so
the entire integration is a linear fast-forward with no merge conflicts.

1. Accept the S9 candidate (review package `_ai-review/s9-final-reproducibility-FIRSTMATE.zip`).
2. Commit the closeout on `closeout/s9-final-reproducibility-v1` (baseline `fbc2479dfefca6c8d51a21b789d485042688143f`).
3. (Optional) push the S9 branch to origin.
4. Fast-forward local `main` → the S9 commit.
5. Push `main` to origin (fast-forward from `da4df94`).
6. Keep stage/experiment branches as the historical record. Deleting local stale branches is an owner decision, not required for completeness.
7. Record the final S9 commit SHA in the diary S9 subsection only after the commit exists (no SHA is invented beforehand).

## M. Next-Chat / Fresh-Reviewer Entry Point

Start here, in this order:

1. **This document** — final statuses, conclusions, non-claims, evidence paths.
2. `docs/FINAL_TECHNICAL_REPORT_V2.md` — the full technical report (evidence-backed; §20 conclusions).
3. `analysis/s5_final_controlled_comparison/s5_controlled_comparison_report.md` + `s5_comparison_ledger.json` — canonical result data.
4. `presentation/s6-real-debugging-evidence/index.html` + `s6_presentation_manifest.json` — professor-facing bounded-negative presentation with claim-level provenance.
5. `research/literature/agentic_debugging_literature_closeout_2026-08-11.md` — literature closeout.
6. `diary/diary.md` — chronology through 2026-08-11 (S9 subsection appended to the 2026-08-11 entry).

Reproduce the deterministic parts with the §J commands from a clean checkout of
the final commit. Do not reopen closed stages; do not run new model inference;
do not treat review packages as scientific evidence.

---

*Generated by the S9 closeout BUILD. Nothing was staged, committed, pushed, or
merged during this BUILD; the tracked candidate diff is limited to the approved
closeout/status/diary files.*
