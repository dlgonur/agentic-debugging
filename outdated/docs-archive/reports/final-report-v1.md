# Agentic Debugging Internship — Final Technical Report v1

**Date:** 2026-07-31
**Branch:** `feature/model-decision-final-report-v1`
**Baseline:** `2236775`
**Author's role:** internship project, single-controller-agent architecture,
Python/PDB-first prototype

This report is a snapshot of the project through the eight-task QuixBugs gold
baseline (`2236775`) and the accompanying Model/RAG/SFT/DPO Decision Gate v1
(`docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md`). It is written to stand alone,
but every material claim traces to a specific file, test, commit, or accepted
document in this repository; external claims are marked **[external]** with
their source. No model, provider, RAG, training run, or paid API contributed
to any result described here.

---

## 1. Goal and research question

The project's stated goal (`README.md`) is to investigate the path from
traditional debugging, fault localization, and automated program repair,
through LLM-based debugging, toward **interactive debugger-assisted agents**:
systems that do not just propose a patch from static context, but can pause a
failing program, inspect its live state through a real debugger (initially
Python's `pdb`), and use that runtime evidence to localize and fix the defect.

The research question this motivates is: **does giving an LLM-based
debugging agent controlled, budgeted access to a real debugger (breakpoints,
stack/frame inspection, safe expression evaluation) change its ability to
localize and repair bugs, compared to a static/test-feedback-only baseline —
and can that comparison be made honestly, with a verifier that does not
just trust the model's own claim of success?**

The internship's scope, by design, is narrower than a full research program:
build a correct, honest, verifier-backed evaluation platform first (Tasks
1–9), add a real-model live path second (Task 10A/10B), and only then run
comparative experiments and decide on RAG/fine-tuning/preference-optimization
investment. This report documents completion of the first two phases and the
decision gate for the third; it is not itself a report of comparative
PDB-effectiveness results, because no such comparison with adequate PDB reach
has yet been run (Section 7).

---

## 2. Architecture and execution lifecycle

### 2.1 Component map

The system (`agentic_debugger/`) is a single controller agent — not a
multi-agent system — operating over typed, deterministic tools:

| Layer | Module(s) | Responsibility |
|---|---|---|
| Task contract | `evaluation/task_schema.py` | `DebugTask`: language, fixture path, reproduction command, exact F2P/P2P test vectors, constraints, evaluator-only `Oracle`. `agent_visible_mapping()` strips the oracle before the model ever sees it. |
| Controller | `agent/controller.py`, `agent/controller_policy.py` | State machine: reproduce → understand → (gate) → optional runtime evidence → patch → validate. Enforces the action allowlist, transition graph, and budgets per state. |
| Tool boundary | `agent/tool_registry.py` | The single dispatch surface between controller directives and effects; argument validation, state allowlists, denied paths. |
| Runtime | `runtime/workspace.py`, `runtime/test_runner.py`, `runtime/command_runner.py`, `runtime/patcher.py` | Disposable per-case workspaces, test execution, unified-diff patch application with syntax validation and allow/deny path enforcement. |
| Debugger | `runtime/pdb_session.py` | A real `pdb` session over a worker subprocess: breakpoints, stack/frame/locals inspection, safe AST-allowlisted expression evaluation, stepping/continue. |
| Events | `events/logger.py`, `events/replay.py` | Immutable `RunEvent` trajectories, replay-verified state reconstruction, semantic projection for stable comparison. |
| Verifier | `evaluation/verifier.py`, `evaluation/outcome_taxonomy.py` | The **authoritative** correctness oracle: baseline reproduction, syntax check, post-patch F2P/P2P/full-suite execution, canonical-fixture immutability, cleanup — run from an independent clean baseline, not trusted from the controller's own claim. |
| Live model path | `evaluation/live.py`, `evaluation/live_cli.py` | Explicitly-authorized, offline-by-default real-model evaluation harness; wire protocol currently version `1.3`. |
| Golden trajectories | `agent/trajectory.py` + `tests/golden_trajectories/` | Immutable record/replay fixtures used to detect any drift in controller behavior. |
| Demo | `demo/` | The Task 9 offline, no-model, five-curated-task × two-policy end-to-end demonstration. |
| External adapters | `bugsinpy/`, `quixbugs/` | Dataset-specific manifest validation, source acquisition, and containment layers mapping external benchmarks into the `DebugTask` contract without weakening it. |

### 2.2 Execution lifecycle (one case)

1. Load and validate a `DebugTask` from a manifest (curated fixture,
   BugsInPy, or QuixBugs adapter) through the authoritative schema loader.
2. Create a disposable workspace; copy the canonical fixture/source into it —
   the canonical source is **never** written to directly.
3. Run the baseline reproduction command; confirm a genuine pre-existing
   failure (not a fabricated one).
4. Drive the controller loop under a tool policy (static, or PDB-gated by
   `decide_pdb_access`) to localization, optional runtime evidence, and a
   candidate patch.
5. Apply the candidate as a unified diff; syntax-check; re-run F2P, P2P, and
   the full declared suite.
6. Classify the outcome (`RESOLVED`, `BREAKING_RESOLVED`,
   `PARTIALLY_RESOLVED`, `WORK_IN_PROGRESS`, `NO_OP`, `REGRESSION`).
7. Project the run into a replay-verified event trajectory.
8. Clean up every workspace, subprocess, and debugger session — on both
   success and failure paths.

This lifecycle is identical whether the "model" is the offline scripted
stand-in (Task 9 demo), a real live model (Task 10A/10B), or absent entirely
(the QuixBugs gold-patch baselines, where the "candidate" is the literal
upstream fix). That reuse is deliberate: it is what lets an infra-only run
(gold patch, no model) and a model-in-the-loop run share one verifier and one
notion of "passing."

---

## 3. Dataset and provenance decisions

Recorded in full in `docs/DATASET_EVALUATION_DECISION_V1.md` (2026-07-30);
summarized and reaffirmed here.

| Dataset | Role | Status |
|---|---|---|
| Five curated pytest fixtures (in-repo) | Architecture smoke gate | Accepted; used by Tasks 6–9 and the golden trajectories. Synthetic, not external-benchmark evidence. |
| **BugsInPy** | Primary external dataset | Adapter and preflight built (`agentic_debugger/bugsinpy/`); execution **license-blocked** (Section 5). |
| **QuixBugs Python** | External fallback | License-cleared (MIT + explicit creator consent); adapter built and **infra-validated** end-to-end on 1, then 8, real tasks (Section 6). No model has been run against it. |
| SWE-bench Lite/Verified | Later repository-scale validation | DEFER — harness cost (Docker, ~120 GB storage, 16 GB RAM, 8 CPUs per the official guide) is out of scope for this internship stage **[external: SWE-bench official dataset guide, https://www.swebench.com/SWE-bench/guides/datasets/]**. |
| Defects4J | — | NO-GO-FOR-NOW — Java/JVM, outside the Python/PDB track. |

Provenance discipline applied to every external artifact used: pinned
revision, recorded license text, environment fingerprint (Python version +
sorted dependency list hash), and a fail-closed preflight that blocks
execution on any unresolved fact (license, platform, containment, dependency,
target annotation). No candidate dataset's gold patch or hidden-test metadata
is ever exposed to the agent-visible task; `agent_visible_mapping()` removes
the `Oracle` before any model request is constructed.

---

## 4. Sandbox, resource, Git, credential and fail-closed boundaries

### 4.1 Execution boundary honesty

The repository is explicit that its containment is **trusted-local**, not an
OS-level hostile-code sandbox, for the curated fixtures and the Task 7/9
verifier path (`docs/PROJECT_TRACKER.md`: "Task 7 evaluates trusted local
benchmark fixtures and benign candidate patches. It is not an OS-level
hostile-code security sandbox."). For external datasets, a stronger boundary
was built and live-tested specifically because that trust assumption does
not extend to third-party benchmark code:

- **WSL2 (Ubuntu-22.04) + Bubblewrap (`bwrap --unshare-all`)**:
  network/namespace isolation, hidden Windows mounts (`/mnt/c` absent),
  hidden unrelated WSL home, read-only runtime mounts, single owned
  bind-mount for writes, child-process isolation (`getppid() == 1`) — all
  live self-tested, 7/7 checks passed (`docs/QUIXBUGS_SMOKE_USAGE_V1.md`).
- **Resource limits via `prlimit`** (composed inside the Bubblewrap sandbox,
  not cgroup v2/`systemd-run`, because only `prlimit` directly expresses a
  CPU-seconds total): CPU-time cap (killed, exit 137/SIGKILL),
  address-space cap (clean `MemoryError`), process-count cap (blocked before
  a 64-fork attempt could succeed) — all live self-tested before the gate
  (`prepare_resource_isolation`) is allowed to open. The gate is fail-closed:
  it raises `ResourceIsolationUnavailable` unless all three checks report
  `passed: true`.
- **Storage separation**: an immutable pinned source and a persistent
  Python venv live outside `/mnt/c` and are never auto-deleted; only the
  per-run disposable workspace under `runs/<uuid>/` is created and removed
  per execution.

### 4.2 Git boundary

No commit, push, merge, rebase, tag, branch deletion, reset, or clean has
occurred in this campaign. `docs/PROJECT_TRACKER.md` records that prior
accepted campaigns (e.g., Task 10B-R5) went through explicit commit/merge/
push steps only after acceptance — that pattern is preserved here: this
report and its sibling documents remain as uncommitted working-tree changes
for the user to review before any Git state change (Section 11 of this
report and the review package's exact Git status).

### 4.3 Credential boundary

The live evaluation harness accepts **no credential field** in its external
configuration file (`docs/REAL_MODEL_EVALUATION_TASK10A.md`): the config
carries only schema version, model name, argv command, request timeout, and
tool version. Common credential-bearing argv shapes (credential-named flags,
key/value assignments, Bearer/Basic values) are rejected before any command
is launched. This is explicitly scoped as *not* a universal secret detector
or an OS sandbox — a trusted local wrapper may hold credentials outside the
harness's reach.

### 4.4 Fail-closed pattern, repeated deliberately

The same fail-closed shape recurs across every boundary in this project and
is treated as a house style, not a one-off:

- BugsInPy adapter preflight: "Missing or unknown facts block execution"
  (`docs/BUGSINPY_ADAPTER_USAGE_V1.md`).
- QuixBugs resource isolation: opens only after live self-test evidence,
  never by configuration assumption.
- Live model harness: both live selection *and* explicit confirmation are
  mandatory, or the CLI writes a rejected report without reading the config.
- Live PDB action contract (protocol 1.3): effective actions are the
  intersection of controller-state allowlist, actual tool registry, policy,
  and PDB lifecycle/budget — with no registry-less fallback
  (`docs/PROJECT_TRACKER.md`, Task 10B-R5).

---

## 5. BugsInPy findings and license block

BugsInPy remains the primary external dataset by research merit (real
project bugs, strong F2P/P2P oracle structure, Python-native), but **real
execution remains license-blocked**, unchanged since Dataset and Evaluation
Decision v1:

- An eight-task, four-project, seven-family pilot manifest exists and is
  **metadata-verified** (`docs/BUGSINPY_PILOT_READINESS_V1.md`): every
  `project.info`, `bug.info`, `run_test.sh`, and isolated source patch is
  present in the pinned official snapshot (revision
  `11c5f1eea954a42132cfd06bf257766a7963e0fd`).
- No root `LICENSE` file was found in the inspected official BugsInPy
  snapshot, and the underlying per-project licenses were not independently
  verified. The verdict is explicit: **"Licensing does not currently clear
  the intended workflow"** — this does not prove prohibition, but it blocks
  redistribution and execution until BugsInPy's terms and every selected
  project's terms are reviewed and recorded.
- The adapter and preflight code (`agentic_debugger/bugsinpy/`) are built and
  tested (fail-closed on unknown license/source/platform/setup/command
  fields), but the operator entry point
  (`python -m agentic_debugger.bugsinpy.smoke`) is **metadata-preflight-only
  by construction** — it does not accept an authorization path that could
  synthesize a concrete execution context from plain JSON, precisely so a
  license or containment gap cannot be papered over. It returns
  `REAL_SMOKE_BLOCKED` (`docs/BUGSINPY_ADAPTER_USAGE_V1.md`).
- This block is a pause, not a rejection: none of BugsInPy's 493 bugs across
  17 projects has been ruled out; the next unblocking step is a licensing
  review plus the containment upgrade named in Section 7 of the Decision
  Gate document.

---

## 6. QuixBugs fallback and eight-task methodology

### 6.1 Why QuixBugs, and what one task proved first

With BugsInPy blocked, a single real, no-model smoke against QuixBugs Python
`gcd` (MIT-licensed, explicit creator consent recorded in `legal_notes.txt`,
pinned revision `4257f44b0ff1181dedaedee6a447e133219fcebf`) was used to prove
the WSL/Bubblewrap sandbox could be extended with live-tested resource limits
(`docs/QUIXBUGS_SMOKE_USAGE_V1.md`). The real (not synthesized) `gcd` bug —
`gcd(a % b, b)` never advances `b`, so 5 of 6 official parametrized cases
`RecursionError` on the buggy baseline — required a narrow, backward-compatible
schema relaxation (`pass_to_pass` minimum lowered from 2 to 1) rather than
fabricating a second passing test node, made only after explicit user
confirmation.

### 6.2 Generalizing to eight tasks

`agentic_debugger/quixbugs/adapter.py` was generalized from a literal
`"gcd"` pin to a derived `quixbugs-<algorithm>-smoke-v1` identity with
fail-closed path-naming checks and a new required `oracle` manifest section
(`docs/QUIXBUGS_EIGHT_TASK_BASELINE_V1.md`). All 63 adapter tests (50
original + 13 new) pass; the full unit suite passed 1952/2-skipped at that
point.

Task selection was deterministic (alphabetical over the pinned repository's
`json_testcases`-backed test files, excluding the already-accepted `gcd`),
then **formally executed** one at a time through the resource-limited
pipeline. Eleven unique candidates were executed; eight were eligible and
solved, three were excluded with recorded, specific reasons:

- `bitcount`, `find_first_in_sorted` — non-terminating buggy baselines,
  safely killed by the enforced CPU-time limit (exit 137) at the discovery
  stage, never reaching the verifier.
- `get_factors` — preflight, discovery, oracle, and gold-patch all
  succeeded, but `DebugTask` construction raised `SchemaValidationError`
  because 11 collected nodes pushed `verifier_command_count` past the
  schema's `[1, 20]` bound — a schema-representability limit, not a
  reproducibility or resource failure. Replaced by `kth`.

The exploratory triage that preceded formal execution used unsandboxed
`pytest` runs under a shell timeout — test execution outside the
resource-limited runner. Its exact inventory cannot be reconstructed, so
**historical compliance with the 12-unique-candidate cap is explicitly
recorded as unproven**, not asserted. Future runs enforce that cap in the
orchestration path itself (`scripts/quixbugs_eight_task_baseline.py`,
`enforce_candidate_cap`).

### 6.3 What "no model" means concretely

Every candidate patch in both the single-task smoke and the eight-task
baseline is generated via `difflib.unified_diff` between the pinned buggy and
corrected upstream files — the literal upstream fix, not a generated one.
This is stated in the accepted verdict language itself and repeated here
because it is the single most important caveat for interpreting Section 7's
numbers.

---

## 7. Exact results and what they do not prove

### 7.1 Single-task QuixBugs smoke (`gcd`)

- Verdict: **`ACCEPT CANDIDATE — REAL SMOKE PASSED`**.
- Collected 6 nodes: baseline 5 F2P / 1 P2P (matches manual analysis
  exactly); independent `--correct` oracle run 6/6 passed.
- Post-patch: F2P 1/1, P2P 1/1, full suite 2/2, canonical fixture hash
  unchanged, workspace `CLEANED`.

### 7.2 Eight-task QuixBugs gold baseline

- **8/8 selected tasks solved** (`gcd`, `bucketsort`, `find_in_sorted`,
  `flatten`, `kth`, `hanoi`, `is_valid_parenthesization`, `kheapsort`).
- **49/49 total collected nodes passed post-patch** across all eight tasks
  — 100% solved rate, 100% full-suite pass rate, 0 failures/errors/skips/
  xfails/xpasses anywhere.
- Every gold patch touched exactly the manifest's `buggy_path` and no other
  file; every canonical fixture hash was identical before/after; every
  disposable workspace was `CLEANED` (verified: only the persistent
  `selftest/` scaffold remained in `runs/` afterward).
- Total measured task runtime ≈436s across the 8 selected tasks.
- Verdict: **`ACCEPT CANDIDATE — EIGHT-TASK BASELINE COMPLETE`**.

### 7.3 What these results prove

They prove the QuixBugs adapter, the resource-limited WSL/Bubblewrap
sandbox, the patch/test/verifier lifecycle, and the evidence-generation
pipeline all function correctly against eight real, licensed, external
Python tasks, end to end, with clean isolation and cleanup.

### 7.4 What these results explicitly do not prove

- **Not model quality.** No model — free, paid, fine-tuned, or otherwise —
  was run against any of the eight tasks. There is no "model resolved 8/8"
  claim anywhere in this project's accepted evidence, and this report makes
  none.
- **Not PDB effectiveness.** No PDB session was opened during either
  QuixBugs campaign; that is out of scope for a gold-patch baseline by
  construction.
- **Not repository-scale generalization.** All eight tasks are small,
  single-file, single-defect algorithm programs from one dataset family.
- **Not evidence about the live model harness.** The eight-task baseline
  uses none of `evaluation/live.py`; it is a separate, older
  patch-lifecycle path shared with the demo, not the Task 10A/10B live
  adapter.
- **Not a resolved question about the 12-unique-candidate cap's historical
  compliance,** which is recorded as unproven rather than asserted true.

### 7.5 The separate, smaller, real-model live evidence

The only evidence anywhere in this project of a real model driving the
controller is the four-case OpenCode Zen matrix (fixture
`curated-none-handling-001`, free-tier `deepseek-v4-flash-free`, protocol
1.2, predating the eight-task baseline and predating protocol 1.3):

- Static policy: 2/2 resolved.
- PDB-on-uncertainty: 0/2 resolved; both terminated
  `invalid_model_response` before PDB opened; PDB opened 0/2 times.
- 4 of 6 observed corrective-feedback episodes recovered after a directive
  rejection; 2 did not.
- This is explicitly recorded as small, fixture-specific,
  provider-route-specific, and not a causal PDB-effectiveness or
  general-reliability claim (`docs/PROJECT_TRACKER.md`).

This evidence and the QuixBugs gold baselines must never be pooled: one
measures whether a real model can complete a curated task under two
policies; the other measures whether the infrastructure can process a real
dataset with no model at all. Conflating them would misstate both.

---

## 8. Model, RAG, SFT, and DPO decisions

Full reasoning and trigger conditions are in
`docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md`. Summary:

| Workstream | Decision |
|---|---|
| Future model-access strategy | **PROCEED (narrow)** — one real-dataset, single-task, static-baseline live case on the existing free-tier route before any paid/multi-model expansion. |
| Repository RAG | **NO-GO-FOR-NOW** for a research comparison; existing deterministic file/code-search tools are sufficient for now. |
| Supervised fine-tuning | **DEFER** — no adequate real-model trajectory corpus exists. |
| DPO / preference optimization | **NO-GO-FOR-NOW** — no SFT baseline, no paired preference dataset. |
| Eight QuixBugs tasks sufficiency | Sufficient for infrastructure validation only; not for model selection, training, or generalization claims. |

---

## 9. Limitations

1. **Trusted-local, not hostile-code-sandboxed**, for the curated-fixture
   and BugsInPy/QuixBugs adapter paths beyond the specific WSL/Bubblewrap/
   `prlimit` boundary built for QuixBugs execution.
2. **No real model has been run against any external dataset** (BugsInPy or
   QuixBugs). All external-dataset results are gold-patch, no-model runs.
3. **PDB has never opened in a live (real-model) case.** The only causal
   evidence about PDB access gating is the Task 9 offline demonstration,
   which is structural (both policies receive the same fixed candidate
   repair) and explicitly not a causal PDB-effectiveness result.
4. **The 12-unique-candidate historical cap compliance is unproven** for the
   pre-formal-execution triage stage of the eight-task baseline.
5. **BugsInPy license status is unresolved**, not disproven; it is a
   pending review item, not a permanent block.
6. **Localization is file/symbol-level**, not statement-level; root-cause
   scoring has no validated semantic metric yet.
7. **Cost/token accounting is honest-missing, not fabricated** — the harness
   never invents provider-reported fields, so cross-run cost comparisons are
   only as good as what each provider actually reports.
8. **Windows-host/WSL2-guest environment specifics** (the `--copies` venv
   requirement, the `\\wsl.localhost\` path-visibility issue) are
   host-specific findings that a Linux-native environment would not need to
   rediscover, but this project's containment evidence was gathered on this
   exact combination.

## 10. Threats to validity

- **Construct validity:** "solved" here means "verifier reproduced F2P/P2P/
  full-suite pass after the candidate patch." For the QuixBugs baselines the
  candidate is definitionally the correct fix, so "solved" measures pipeline
  correctness, not debugging capability — repeated deliberately throughout
  this report to prevent the metric from being over-read.
- **Internal validity:** the four-case live matrix's small sample (2
  repetitions per policy) cannot separate model variance from policy effect;
  its own accepted documentation already states this.
- **External validity:** QuixBugs's toy-scale, one-line-defect design is
  known (from the original repair-overfitting study) to be vulnerable to
  overfit "fixes" that pass tests without being genuinely correct
  **[external: Ye, Martinez, Durieux, and Monperrus, "A Comprehensive Study
  of Automatic Program Repair on the QuixBugs Benchmark," 2018/2019,
  https://arxiv.org/abs/1805.03454, reports 53.3% of studied plausible
  patches were overfitting patches; cited in
  `docs/DATASET_EVALUATION_DECISION_V1.md` Section 3]** — irrelevant here
  because every QuixBugs patch used is the literal upstream fix, not a
  generated candidate, but directly relevant to any *future* model-generated
  QuixBugs run.
- **Historical/audit validity:** the unsandboxed pre-formal-execution triage
  for the eight-task baseline is explicitly flagged as not fully
  reconstructable evidence, rather than silently assumed compliant.

## 11. Reproducibility

- Every accepted result cites a pinned Git commit or repository revision, an
  environment fingerprint, and (for QuixBugs) a `prlimit` resource profile.
- The demo guide (`docs/DEMO_GUIDE_V1.md`) gives the exact, unmodified entry
  points for the one-task smoke, the eight-task baseline, and the in-repo
  offline Task 9 demonstration, plus their expected outputs and evidence
  locations.
- Local working evidence for each accepted campaign (full diffs, manifests,
  exclusion evidence, per-task JSON, hashes) is kept outside Git tracking
  under `_ai-review/<campaign>/`, consistent with this report's own review
  package (Section 13).

## 12. Future work

In priority order, consistent with the Decision Gate:

1. Run the smallest credible next live experiment (Decision Gate Section 6):
   one QuixBugs task, static-baseline policy, free-tier model, through the
   protocol-1.3 harness.
2. If that resolves, attempt the same task under `pdb-on-uncertainty` to
   test whether protocol 1.3's contract repairs let PDB actually open live.
3. Resolve BugsInPy's licensing review and build the OS/container-level
   containment upgrade required before any BugsInPy execution.
4. Only after 1–3: consider a larger paired static/PDB matrix, RAG, SFT, or
   DPO, per the trigger conditions in the Decision Gate.
5. Continue the deferred literature-review items in `TODO.md` Phase 1
   (Self-Debugging, DebugBench, Debug2Fix, FramePilot/ADI, EnIGMA,
   SWE-Doctor) and the root-cause/statement-level localization metrics named
   as gaps in Dataset and Evaluation Decision v1.

## 13. Final contribution

This project delivers a verifier-backed, fail-closed, single-controller
agentic debugging platform with a real PDB integration, a replay-verified
event/trajectory system, an explicitly-authorized real-model live evaluation
harness (now at wire protocol 1.3 after three contract-repair rounds), and a
licensed, infra-validated external-dataset path (QuixBugs, eight tasks, 100%
gold-patch pass rate) alongside a fully-specified but license-blocked primary
external dataset (BugsInPy). Its central contribution to date is
**infrastructure and evaluation methodology**, built and demonstrated to a
standard where the next real-model experiment is well-defined and cheap, not
a claim about debugging performance, PDB effectiveness, or model quality —
those claims are explicitly deferred to the future work this report and the
accompanying Decision Gate lay out.

## 14. Revision note (2026-08-05) — state through accepted campaign infrastructure

The body of this report is a point-in-time snapshot through the eight-task
QuixBugs gold baseline (`2236775`, 2026-07-31). The following 2026-08-05
revision records the accepted state since then; it does not alter the
historical body above.

- **Campaign infrastructure accepted on `main` through `0abb588`.**
  `eb63c76` hardened the campaign budget and verifier path, `9f53df7` added
  the actual V4 interrupted budget terminal, and `0abb588` added the
  terminal, exact-identity validation, and fail-closed budget-exhaustion
  provenance infrastructure (run persistence, campaign-record validation,
  attempt-package verification). Accepted campaign validation: focused
  campaign integration suite 389 passed; bounded full suite 3394 passed, 3
  skipped, same six known OpenCode wrapper/transport failures.
- **Recorded-case identity correction.** The
  sanitized attempt fixture and replay assertions accepted at `0abb588`
  associated the two observed shapes with the wrong frozen cases; that
  fixture/test identity mapping was corrected using the preserved campaign
  record, private transport evidence, cost sums, and the frozen v4 case
  order, and is accepted on `main` at `fc7c85b` — it is no longer a pending
  Friday-readiness candidate. Production budgets,
  the frozen manifest, route, provider, authorization, and controller
  behavior are unchanged.
- **Recorded V4 attempt (`3b5d7488…`).** V4 Case 1 (`find_in_sorted` /
  `pdb-on-uncertainty`, order 1): 10 provider processes, 9 logical calls, 1
  retry, 26,139 public-evidence bytes, malformed hunk-header patch
  rejection, no candidate, zero verifier runs, `$0.007378`,
  `INFRASTRUCTURE_ERROR`. V4 Case 2 (`find_in_sorted` / `static-baseline`,
  order 2): 15 provider processes, 14 logical calls, 1 retry, 38,534 bytes,
  patch applied with Validate visited, interrupted, zero verifier runs,
  `$0.012323`; the original campaign aborted `ABORTED / BUDGET_EXCEEDED`.
  No verifier-confirmed external live repair exists; no live PDB benefit is
  demonstrated; no post-repair provider campaign was run. The next
  authorized attempt must use `research/quixbugs/PAIRED_PILOT_V4.json`
  explicitly.
- **QLoRA experiment.** The implementation (including the tracked
  `independent_ai` audit contract and run-provenance) is accepted at commit
  `3f0d3e7` on the unmerged branch `experiment/qlora-patch-pilot-v1`
  (FirstMate implementation review passed; owner suite review 3457 passed, 3
  skipped, 36 unrelated pre-existing OpenCode transport/wrapper failures, no
  QLoRA-focused failure). The owner-delegated independent FirstMate AI audit
  of the 75 frozen corpus rows is complete externally (39 ACCEPT / 36
  REJECT, disclosed AI reviewer identity; not a human audit). Final QLoRA
  training was externally authorized by FirstMate on 2026-08-05; no accepted
  final-training artifact or result exists yet, and final-training results
  are pending FirstMate artifact review. Held-out generation and the
  base-versus-tuned comparison remain unauthorized; final corpus acceptance
  and the remaining fail-closed audit/corpus-quality decisions remain
  pending. The tracked freeze record at `3f0d3e7` (which still carries
  `final_training_authorized: false`) is the historical branch-bound freeze
  record, not evidence about the current external authorization. No
  predicted training values are implied.
- **Current operating facts** (see `README.md` "Current status
  (2026-08-05)", `TODO.md`, `docs/PROJECT_TRACKER.md` 2026-08-05 entry, and
  `docs/FRIDAY_PRESENTATION_PLAN_V1.md` for the full evidence paths).
