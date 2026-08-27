# Agentic Debugging — Project Closeout (current, 2026-08-27)

**Project:** Academic Agentic Debugging Internship Project
**Owner:** Onur
**Execution control:** Main FirstMate
**Document date:** 2026-08-27 (previous reconciliation: 2026-08-24)
**Accepted docs baseline HEAD before this reconciliation (2026-08-27):**
`8fbea883212d3fe4ad6434a366ca0898fbea68f1` (the accepted PRE-RELEASE-HARDENING-01
commit; earlier historical docs-reconciliation baseline, 2026-08-24:
`70804d226f7e2d014e1edfa0760c2758e92acf94`)
**Supersedes:** `docs/archive/status/project-closeout-2026-08-11.md` (the
2026-08-11 S9 bounded-negative closeout, preserved unchanged as a historical
snapshot) and the earlier `docs/archive/reports/final-report-v1.md`
(2026-07-31).

This is the single current reviewer/handoff status document. The full
technical narrative through 2026-08-13 is `docs/final-report.md`; the
2026-08-11 scientific snapshot is archived verbatim at
`docs/archive/reports/final-report-2026-08-11.md`.

**2026-08-16 status update:** Local Application V1 Tasks 1–8 are COMPLETE /
ACCEPTED (authoritative record `docs/architecture/local-application-v1.md`);
TODO.md and `docs/project-tracker.md` have been reconciled to the post-V1
current status. The R1-R6 documentation candidate was committed, pushed, and
integrated to main by the Final Git operator; at the accepted Local
Application V1 closeout, owner Git evidence showed main == origin/main at
`387a100` (accepted pre-reconciliation baseline; Task-8 branch deleted
locally and remotely). This update does not change any scientific finding,
qualifier, metric, or the INCOMPLETE_HARDWARE_STOP boundary in this document.

**2026-08-17 status update:** Local Application V1 real remote
decision-model product proof is **COMPLETE** via Ollama Cloud
`gpt-oss:20b-cloud`. A real Ollama Cloud configured-command route completed
successfully through the accepted Local Application path (Ollama CLI/API
0.32.14; signed-in local Ollama daemon; requested Cloud alias
`gpt-oss:20b-cloud`; observed upstream chat model `gpt-oss:20b`; provider
route Ollama Cloud; adapter retry 0; fallback 0; no persistent model
conversation; Local Application protocol 1.3). Successful real product
session `sess-20260817-103258-3d1193` (task `curated-none-handling-001`,
policy `pdb-on-uncertainty`): SUCCEEDED (done), phase Done, independent
verifier RESOLVED, fail-to-pass 1/1, pass-to-pass 2/2, cleanup verified,
`candidate.patch` and `evaluation.json` artifacts written. The session is
registered in Local Application history; read-only replay opened it at
120/120 events with the same patched source, the same rejected first patch
and applied second patch, and the same terminal state — observed live/replay
terminal-state parity (replay does not execute Ollama again). Product
success: YES; debugging success: YES. PDB was NOT EXERCISED in this session
(not PASS, not a failure); the R1–R3 PDB scientific milestones in this
document remain unchanged. AGY remains historical/optional. Accepted
adapter lineage on main: `2d256a1`…`cdd9792` (through "Strengthen Ollama
unified diff count guidance") — the accepted main lineage before this
documentation candidate; see
`docs/architecture/ollama-cloud-command-adapter-v1.md` and
`docs/project-tracker.md`. This update does not change any scientific
finding, qualifier, metric, or the INCOMPLETE_HARDWARE_STOP boundary in this
document, and it does not reopen DPO, fine-tuned+RAG correctness, or
BugsInPy execution. The historical Authorized Six-Case Live Campaign is
**RETAIN_OPTIONAL / OWNER-AUTHORIZED**: not required for Local Application
V1 or the accepted R1-R6 closeout; the frozen OpenCode Go V4 path remains
preserved evidence, not the current product route; do not run OpenCode Go
merely for checkbox completion; a future Ollama PDB-versus-static comparison
would be a new experiment, not a mutation of
`research/quixbugs/PAIRED_PILOT_V4.json`.

**2026-08-18 status update:** the Ollama Cloud Nemotron 3 Nano
model-capability probe is **COMPLETE** as closed evidence. After the
multi-model adapter generalization (`756bd2d`) and State-Aware Validate
(`4f0a748`, Harness V2), selected `nemotron-3-nano:30b-cloud` (upstream
`nemotron-3-nano:30b`) was tested on the fixed five-task curated
treatment under policy `pdb-on-uncertainty` with a fresh external
application root per task. All five runs were admissible. One reached
independent-verifier RESOLVED and four remained unresolved, producing
**1/5**. Distinct historical records remain: V1
`sess-20260817-200956-160723` FAILED (classify_outcome before post-patch
F2P; verifier did not run); V2 `sess-20260818-050514-20777e`
infrastructure-invalid `BASELINE_INVALID` (repository-nested verifier
workspace prefixed pytest node IDs; candidate not evaluated); V2b
`sess-20260818-052524-f0287d` COMPLETED / RESOLVED (F2P 1/1, P2P 2/2,
full suite PASS 3/3). PDB was NOT EXERCISED on all five treatment tasks.
This does not establish a causal model-strength comparison and does not
change R1–R6, the 2026-08-17 `gpt-oss:20b-cloud` product proof, the
INCOMPLETE_HARDWARE_STOP boundary, DPO, fine-tuned+RAG correctness, or
BugsInPy execution. Canonical carrier:
`experiments/nemotron_3_nano_model_capability_probe/`.

**2026-08-21 status update:** the exact-PDB capability ladder is accepted
through its 18/100 ordinal rung. First, `gpt-oss:20b-cloud` resolved the
deliberately simple `pdb-required-boundary-006` rung. The next authorized
single task, `pdb-required-caller-callee-007`, added a two-function unit
contract, normalized input, two public P2P checks, and verifier-only private
checks. The activity-aware high-thinking stream completed 22 logical calls /
22 attempts with zero retries/provider errors; real PDB start/stack/locals/
next/stop evidence preceded the model-authored one-hunk patch. Independent
verifier: `COMPLETED/RESOLVED`, F2P 1/1, P2P 2/2, private checks true;
cleanup/canonical immutability true; 57-event replay terminal `Done`. Thinking
text was discarded and only aggregate activity was retained. Canonical tracked
carrier: `experiments/pdb_capability_ladder/`. The third rung,
`pdb-required-multistage-units-008`, raised the task to a three-function
normalize/convert/retry-expand pipeline. GPT-OSS again completed the exact-PDB
path and authored the verified one-line repair: 21 calls/attempts, zero
retries/provider errors, F2P 1/1, P2P 2/2, private checks true, cleanup and
immutability true, 54-event replay `Done`. These are three single-task rung
results, not a success rate, generalization claim, matched model comparison,
or causal PDB-effectiveness result.

The subsequently frozen historical 32/100 rung used SWE-rebench V2
`audreyr__cookiecutter-967`. V1 and V2 are retained infrastructure-invalid
treatments. V3 completed exact PDB, evidence-bound diagnosis, patching,
controller `Done`, local verifier `RESOLVED`, cleanup, and replay in 24
calls/attempts with zero retries/provider errors. The official pinned Docker
verifier then recorded rejection of the raw model patch (F2P 0/5; P2P not
passed 9/9). At that historical point this located a descriptive one-task
boundary between accepted 18/100 and failed 32/100. Later candidate-artifact
forensics established that official test execution was not proven for the raw
serialization, so the `0/5, 9/9` shape is not a clean semantic model failure.
It is not a success rate, generalization claim, matched-model comparison, or
causal PDB result. No hidden-informed task mutation or V4 followed; canonical
evidence is under
`experiments/pdb_capability_ladder/`.

**2026-08-24 status update:** the Level-32 candidate-artifact boundary repair,
historical candidate replay, GLM 5.2 V10/V11 repaired-treatment records, and
the complete repaired model matrix are now closed evidence. Fresh GLM 5.2 V11
authoritatively resolved the task under
`workspace-derived-official-git-diff-v1` (exact PDB proof, canonical semantic
equivalence, official application, proven official execution, F2P 5/5, P2P
failed 0/9). GLM 5.1 independently reached the same authoritative resolution
in the 15-model matrix. Across 15 eligible models run exactly once under one
homogeneous repaired treatment, 14 reached exact-PDB proof, 7 reached proven
official tests, 6 of those 7 reached at least F2P 4/5, 2 resolved, 1 was a
semantic rejection, and 12 were protocol failures. Candidate materialization
was no longer the systemic blocker. Historical raw runs remain unchanged;
their later forensic reclassification and repaired-treatment results are
recorded in the tracked analysis files named by the results index.

The capability-ladder objective is sufficient for the current research cycle;
further difficulty escalation is paused/closed for now. The then-current
"next active project direction = Local Application / UI and UX refinement"
note (pending owner screenshot review) was subsequently superseded — see the
2026-08-27 status update below.

**2026-08-27 status update:** PRE-RELEASE-HARDENING-01 is **ACCEPTED /
FEATURE-FREEZE READY** at `main` `8fbea88` (baseline `5cbe856`). The final
pre-release forensic audit + hardening was independently reviewed by
FirstMate, repaired in two FirstMate rounds (PRH-025..028, PRH-029),
accepted, committed as `8fbea88`, fast-forward merged to `main`, and pushed.
Totals: 9 RED blockers repaired (PRH-001..004, PRH-025..029); 21 ORANGE
findings — 17 fixed, 2 false positives, 2 bounded deferred; PRH-D01..D09
documented as accepted/deferred non-blocking debt. Validation was
deterministic only; external provider/Docker/WSL validations were not rerun
(accepted evidence stands). No known RED release blocker remains. Acceptance
is feature-freeze ready and does **not** itself mean a release tag was
created. Durable record:
`docs/pre-release-hardening-2026-08-27.md`. Current state: capability
research cycle closed/paused; Local Application V1 complete; real Local
Project path accepted; PRE-RELEASE-HARDENING-01 accepted / feature-freeze
ready; no active required engineering campaign remains; next phase is
documentation/release/tag/closure under owner decision. This update does not
change any scientific finding, qualifier, metric, or the
INCOMPLETE_HARDWARE_STOP boundary in this document.

**PROJECT STATUS:**
**POSITIVE REAL-MODEL DYNAMIC DEBUGGING + PROJECT-FINE-TUNED VALIDATION
ESTABLISHED; PROFESSOR TRACE DELIVERABLE COMPLETE; STRONGER R6 FINAL HOLDOUT
INCOMPLETE DUE HARDWARE; R1-R6 DOCS CLOSEOUT COMMITTED, PUSHED, AND
INTEGRATED TO MAIN; LEVEL-32 REPAIRED MATRIX COMPLETE; CAPABILITY ESCALATION
PAUSED; LOCAL APPLICATION V1 + REAL LOCAL PROJECT PATH COMPLETE;
PRE-RELEASE-HARDENING-01 ACCEPTED / FEATURE-FREEZE READY (8fbea88); NO KNOWN
RED RELEASE BLOCKER REMAINS; NO ACTIVE REQUIRED ENGINEERING CAMPAIGN; NEXT
PHASE: DOCUMENTATION / RELEASE / TAG / CLOSURE UNDER OWNER DECISION**

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

- R1-R6 documentation carrier branch used for the 2026-08-13 closeout
  (historical): `docs/r1-r6-current-closeout-v1`.
- Milestones: R1 `c842d69`, R2 `97cc7fe`, R3 `f2291df`, R4
  `372d51f1a35e071c677391c9970f7b552bb276f2`, R5 reproducibility closeout
  `54828db1d5dec4e95105f1c1d07ba5dd7518060c`, R6 preserved implementation/
  evidence `4610785713832daaba6aa133374506a2d200391a`, professor trace
  deliverable `c9afe377db3f53229755532751b485fc2a13a4e7`, docs structure
  historical docs-structure baseline `34cce329b5e6e7cf42531d8e609774c7608b67cb`.
- Frozen R6 evidence capsule:
  `experiments/r6_debugger_training/runs/frozen/` (+
  `capsule_manifest.json`); raw R5.9 evidence directory
  `experiments/debugger_interaction_v2_r5/runs/R5.9-MATRIX-14B-CLEAN-FINAL-2026-08-12/`
  with the tracked contracts and leakage regression fixtures.
- Provenance discipline: local/untracked sources are never silently
  promoted to `frozen_in_repo`; tracked carriers are named per claim in
  `docs/final-report.md` §22.

## 9. Git closeout and what remains

The R1-R6 documentation candidate Git work is **COMPLETE**: FirstMate review
**ACCEPTED** (2026-08-13); commit/push and integration to `main` were
performed by the Final Git operator; at the accepted Local Application V1
closeout, owner Git evidence showed main == origin/main at `387a100`
(accepted pre-reconciliation baseline; Task-8 branch deleted locally and
remotely).

Not in current scope: resuming the R6 final five-task holdout (closed
boundary — INCOMPLETE_HARDWARE_STOP), DPO, RAG correctness campaigns,
BugsInPy execution (license-gated), or further capability-ladder/model
escalation in the current cycle. The
2026-08-18 Nemotron capability probe is completed closed evidence, not an
open campaign.

The historical Authorized Six-Case Live Campaign is **RETAIN_OPTIONAL /
OWNER-AUTHORIZED**. It is not required for Local Application V1 completion
and not required for the accepted R1-R6 scientific closeout. The 2026-08-17
Ollama Cloud product proof (`sess-20260817-103258-3d1193`) did not record
PDB evidence and does not supersede the campaign's original paired
static-versus-PDB question. The frozen OpenCode Go V4 execution path
(`research/quixbugs/PAIRED_PILOT_V4.json`) remains preserved evidence, not
the current product route; do not run OpenCode Go merely for checkbox
completion. A future PDB-versus-static comparative experiment using Ollama
would be a new experiment with a new protocol and separate owner
authorization, not a mutation of `PAIRED_PILOT_V4.json`. The real OpenCode
Go operator preflight remains the same optional owner-authorized action.
There is no active engineering campaign after this disposition.

---

*Historical R1-R6 documentation BUILD note: the candidate BUILD itself
performed no Git mutation; owner Git closeout was later completed as recorded
in §9. The historical 2026-08-11 closeout and report remain archived
verbatim.*
