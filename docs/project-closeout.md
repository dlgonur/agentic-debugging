# Agentic Debugging — Project Closeout (current, 2026-08-13)

**Project:** Academic Agentic Debugging Internship Project
**Owner:** Onur
**Execution control:** Main FirstMate
**Document date:** 2026-08-13
**Docs baseline HEAD:** `34cce329b5e6e7cf42531d8e609774c7608b67cb`
**Supersedes:** `docs/archive/status/project-closeout-2026-08-11.md` (the
2026-08-11 S9 bounded-negative closeout, preserved unchanged as a historical
snapshot) and the earlier `docs/archive/reports/final-report-v1.md`
(2026-07-31).

This is the single current reviewer/handoff status document. The full
technical narrative through 2026-08-13 is `docs/final-report.md`; the
2026-08-11 scientific snapshot is archived verbatim at
`docs/archive/reports/final-report-2026-08-11.md`.

**PROJECT STATUS:**
**POSITIVE REAL-MODEL DYNAMIC DEBUGGING + PROJECT-FINE-TUNED VALIDATION
ESTABLISHED; PROFESSOR TRACE DELIVERABLE COMPLETE; STRONGER R6 FINAL HOLDOUT
INCOMPLETE DUE HARDWARE; MAIN INTEGRATION PENDING**

---

## 1. What was built

A verifier-backed, fail-closed, single-controller agentic debugging prototype
(`agentic_debugging/`): typed deterministic controller/tools, disposable
workspaces, a real PDB session/worker/protocol backend, unified-diff +
whole-file patch serialization with deterministic normalization, an
independent EvaluationVerifier (F2P/P2P/full-suite/syntax/canonical
immutability/cleanup), immutable event trajectories with replay, an
offline-by-default live-model harness, deterministic repository RAG,
comparison/preference infrastructure, dataset adapters (QuixBugs WSL2 +
Bubblewrap containment; BugsInPy license-gated), and professor-facing
structured JSON trace export.

## 2. What was researched

- S7 focused literature closeout (20 works): runtime evidence can help;
  raw debugger exposure alone is not reliably beneficial; ordinary
  localized-repair SFT does not teach debugger competence; single-agent +
  deterministic-controller design is defensible.
- RAW baseline and cp118 localized-repair comparison; DPO investigation;
  RAG treatment; and the later R1-R6 debugger-interaction and
  debugger-oriented fine-tuning campaigns (see sections below).

## 3. What worked (current claims)

- **R1-R4 — repaired-interface real-model debugger milestones (2026-08-11).**
  A real model authored a valid breakpoint; the real PDB session paused and
  returned a production-region observation (R1). A multi-turn dynamic loop
  breakpoint → stack → locals → step/next → post-step stack → diagnosis was
  completed by a real model (R2). Debugger evidence → model diagnosis →
  semantic patch → PatchManager → independent verifier RESOLVED was reached
  (R3; with the mandatory qualifier that the raw patch carried a
  unified-diff hunk-count metadata error corrected by a deterministic
  COUNT-ONLY serialization normalization). A model-authored regression test
  T failed the buggy workspace and passed the accepted fixed workspace with
  the verifier RESOLVED (R4).
- **R5 — clean base-14B generalized holdout (2026-08-12).**
  Qwen2.5-Coder-14B-Instruct BASE (adapter-applied=false) resolved all five
  curated bugs 5/5 under the final sanitized r5.9 treatment, with 0 leakage
  findings across the 41 audited actual prompts. The earlier r5.7 5/5 was
  disqualified because hidden-test content leaked into PATCH prompts and is
  preserved as historical upper-bound evidence that must fail the audit.
  R5 does not claim that fine-tuning caused an improvement.
- **R6 — debugger-oriented project fine-tuning + disjoint validation
  (2026-08-12/13).** A debugger-trajectory SFT dataset was built from the
  pinned QuixBugs revision (`4257f44b0ff1181dedaedee6a447e133219fcebf`):
  29/40 usable fixtures, frozen 21 train / 8 validation split, 164 train /
  61 validation SFT pairs (token statistics: p50 ≈ 832, p90 ≈ 1607,
  p95 ≈ 1761, max 2415). QLoRA SFT on
  Qwen/Qwen2.5-Coder-7B-Instruct (`c03e6d358207e414f1eca0bb1891e29f1db0e242`);
  checkpoint-30 selected from disjoint validation only (adapter model SHA256
  `7ef5d70a…`, config `92ddf91e…`).
  **The project-fine-tuned Qwen2.5-Coder-7B debugger achieved 8/8 RESOLVED
  on a frozen, task-disjoint QuixBugs validation set using real
  debugger/tool execution and independent verification** (97 model calls,
  64,783 tokens, 841,702 ms task runtime, zero row errors).
- **Professor structured JSON traces — complete.** Exactly 10
  `professor_debug_trace_v1` documents (8 successful R6 disjoint-validation
  traces + 2 partial-final-holdout traces) under `docs/professor_traces/`;
  R5 reference removed from the final professor trace set; professor-safe
  audit: 10 documents, 0 findings, passed=true; SHA manifest matches all 10
  traces; deterministic and pristine fresh-checkout regeneration
  demonstrated; hidden tests/oracles/chain-of-thought not exposed.

## 4. What failed (preserved, not erased)

- **Historical D1/S2 real-model debugger failures (2026-08-10/11).** Under
  the old interface both RAW (`break 20` → tool error) and cp118
  (`continue` → rejected, no session) produced zero successful observations.
  Superseded by the R1-R4 repaired-interface successes; the old runs remain
  frozen historical evidence.
- **cp118 localized-repair negative transfer.** SWE-rebench V2
  localized-repair QLoRA (cp118) on Quix40: 0/40 apply, 0/40 RESOLVED.
  Formulation-specific negative transfer; the later R6 campaign is a
  different debugger-oriented fine-tuning campaign and is not comparable as
  a matched-base ablation.
- **S4 cp118+RAG treatment.** 10/40 partial, compute-constrained, primary
  correctness NOT_EVALUATED; no RAG success/failure claim.
- **R6 final five-task holdout — INCOMPLETE_HARDWARE_STOP.** See §6.

## 5. What was superseded

- The 2026-08-11 S8/S9 bounded-negative conclusion ("the project did not
  achieve a positive real-model debugger trajectory") is now **historical**:
  at that snapshot it was true; the later R1-R6 work superseded the overall
  conclusion while the old experiments and their negative results remain
  preserved.

## 6. Strongest current result and the limitation that remains

- **Strongest result:** the 8/8 task-disjoint R6 validation by the
  project-fine-tuned 7B debugger, and the R5 clean 5/5 base-14B holdout.
- **Remaining limitation:** the stronger tuned-model final five-task curated
  holdout is **INCOMPLETE_HARDWARE_STOP**. Completed rows:
  `curated-none-handling-001` RESOLVED (F2P 1/1, P2P 2/2, strict pass);
  `curated-off-by-one-002` BREAKING_RESOLVED (F2P 1/1, P2P 1/2, strict
  failure); `curated-wrong-branch-003` interrupted during a model request;
  `curated-mutation-alias-004` and `curated-caller-callee-005` never
  started. This is not 2/5, not 1/5, and not a failed 5-task benchmark —
  three tasks never produced outcomes. Holdout leakage=0 was NOT
  established (only the two completed tasks' 18 prompts show 0 findings).
  Repeated local hard power-offs (Event 41 style) interrupted the run; no
  definitive hardware root cause was established; no VRAM-exhaustion claim;
  no sustained local GPU rerun is scheduled in current scope.
- No matched-base R6 ablation exists: fine-tuning is not claimed to have
  causally improved over a matched base.

## 7. Where is the professor evidence

- `docs/professor_traces/` — 10 traces, schema
  `professor_debug_trace_v1`, indexes (`r6_validation_index.json`,
  `r6_holdout_partial_index.json`), manifests
  (`source_evidence_manifest.json`, `trace_sha_manifest.json`,
  `professor_safe_audit.json`), README. Deterministic regeneration:
  `python -m agentic_debugger.evaluation.professor_trace_r6 --output-dir
  docs/professor_traces`.

## 8. Git/evidence carrier

- Current docs branch: `docs/r1-r6-current-closeout-v1`.
- Milestones: R1 `c842d69`, R2 `97cc7fe`, R3 `f2291df`, R4
  `372d51f1a35e071c677391c9970f7b552bb276f2`, R5 reproducibility closeout
  `54828db1d5dec4e95105f1c1d07ba5dd7518060c`, R6 preserved implementation/
  evidence `4610785713832daaba6aa133374506a2d200391a`, professor trace
  deliverable `c9afe377db3f53229755532751b485fc2a13a4e7`, docs structure
  baseline `34cce329b5e6e7cf42531d8e609774c7608b67cb`.
- Frozen R6 evidence capsule:
  `experiments/r6_debugger_training/runs/frozen/` (+
  `capsule_manifest.json`); raw R5.9 evidence directory
  `experiments/debugger_interaction_v2_r5/runs/R5.9-MATRIX-14B-CLEAN-FINAL-2026-08-12/`
  with the tracked contracts and leakage regression fixtures.
- Provenance discipline: local/untracked sources are never silently
  promoted to `frozen_in_repo`; tracked carriers are named per claim in
  `docs/final-report.md` §22.

## 9. What remains operationally before final main integration

1. FirstMate review of this documentation candidate
   (`_ai-review/R1-R6-DOCS-CLOSEOUT-FIRSTMATE.zip`) — **ACCEPTED**
   (2026-08-13).
2. Git commit/push of the documentation candidate (owner/Final Git
   operator; not performed in this BUILD).
3. Integration to `main` (fast-forward or reviewed merge; owner decision).
4. Not in current scope: resuming the R6 final five-task holdout (closed
   boundary — INCOMPLETE_HARDWARE_STOP), DPO, RAG correctness campaigns,
   BugsInPy execution (license-gated), new model experiments.

---

*Current closeout BUILD: no commit, push, or merge was performed during the
BUILD; the historical 2026-08-11 closeout and report remain archived
verbatim.*
