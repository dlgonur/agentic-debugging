# Dataset and Evaluation Decision v1

**Decision date:** 2026-07-30  
**Branch:** `research/dataset-evaluation-decision-v1`  
**Accepted starting baseline:** `51e7dc0faabe84a36d60486c420de9ba0af95878`  
**Scope:** research and documentation only; no dataset was downloaded or executed.

## Executive decision

| Decision | Selection | Meaning now |
|---|---|---|
| Primary external dataset | **BugsInPy** | The first external dataset to adapt and evaluate, after a bounded eligibility screen. |
| External fallback | **QuixBugs Python** | A low-cost fallback for validating the adapter and paired PDB/static evaluation loop if BugsInPy setup or licensing cannot be cleared. |
| Architecture smoke gate | **Current five curated fixtures** | Continue using the repository’s existing deterministic fixtures to validate adapter/verifier plumbing; do not present them as external-benchmark evidence. |
| SWE-bench Lite/Verified | **DEFER** | Later repository-scale validation, not the first PDB pilot. |
| Defects4J | **NO-GO-FOR-NOW** | Java/Perl/JVM infrastructure is outside the Python/PDB decision. |

The immediate research choice is therefore **BugsInPy first, QuixBugs fallback, with the current curated set as a preflight gate**. This separates scientific validity from engineering readiness: the curated fixtures are the cheapest way to prove the existing contracts still work, while BugsInPy supplies the first realistic Python bug population.

## 1. Repository baseline and evidence method

The live checkout was clean at the requested branch and baseline. The source of truth for the architecture is the live repository, tests, and Git state. Codebase Memory MCP was used only as an auxiliary navigation aid:

- project-specific index: `agentic-debugging-internship-dataset-evaluation-v1`
- indexed path: `<repository-root>`
- mode: fast
- persistence: `false`
- automatic indexing/watch: not enabled
- production architecture scope: `agentic_debugger/`
- graph artifact: none created or committed

CBM identified the high-fan-in `DebugTask.from_mapping`, `load_task`, `EvaluationVerifier.evaluate`, `validate_live_report`, and `PdbSession.start_paused_target` paths. Each material conclusion below was checked against live source and tests. The index excluded `docs/` and some integration tests, so graph absence was not used as evidence of absence.

Relevant local evidence:

- [`task_schema.py`](../agentic_debugger/evaluation/task_schema.py) defines schema version `1.0`, Python-only language validation, relative fixture paths, reproduction commands, exact F2P/P2P test vectors, constraints, and an evaluator-only oracle. `agent_visible_mapping()` removes the oracle.
- [`runner.py`](../agentic_debugger/evaluation/runner.py) routes mappings, manifest paths, and `DebugTask` objects through the authoritative schema loader.
- [`verifier.py`](../agentic_debugger/evaluation/verifier.py) checks the baseline, reproduction, selected tests, candidate patch application, syntax, post-patch reproduction, F2P/P2P results, full-suite consistency, cleanup, and canonical fixture integrity.
- [`outcome_taxonomy.py`](../agentic_debugger/evaluation/outcome_taxonomy.py) classifies `RESOLVED`, `BREAKING_RESOLVED`, `PARTIALLY_RESOLVED`, `WORK_IN_PROGRESS`, `NO_OP`, and `REGRESSION`.
- [`live.py`](../agentic_debugger/evaluation/live.py) records case identity, controller/verifier state, localization, measurements, token-usage availability, PDB observation counts, events, cleanup, and complete/partial/interrupted/rejected report semantics.
- [`tests/integration/test_evaluation_verifier.py`](../tests/integration/test_evaluation_verifier.py), [`tests/integration/test_demo_end_to_end.py`](../tests/integration/test_demo_end_to_end.py), and the golden-trajectory tests verify the relevant contracts, including F2P/P2P/full-suite outcome handling, localization, PDB evidence, replay, and cleanup.

The accepted baseline already records that the prior four-case live matrix was descriptive only: the PDB policy opened PDB in 0/2 cases. That result must not be pooled with the future pilot or treated as evidence of PDB effectiveness.

## 2. Compact dataset inventory

| Candidate | Language / shape | Oracle and evaluation assets | PDB/runtime fit | Setup and security | Decision |
|---|---|---|---|---|---|
| **BugsInPy** | Python; 493 real bugs from 17 open-source projects in the original paper | Buggy/fixed revisions, isolated source patch, and tests that expose the bug; project metadata and test commands | Strong language fit; runtime evidence is not a native benchmark field, so target-symbol and debugger-entry metadata must be derived and reviewed | Medium/high setup cost: project-specific revisions, dependencies, and commands; execute third-party repositories only in a hardened sandbox; dataset README documents Docker use | **PRIMARY** |
| **QuixBugs Python** | Python 3; 40 small programs, one-line defects, 14 defect classes, paired with Java | Passing/failing test cases and corrected Python programs; good binary regression oracle but weak issue/root-cause metadata | Easy to attach PDB to small Python programs, but runtime evidence is easy to overfit and does not represent repository debugging | Low setup cost; MIT repository; still execute only in isolation because benchmark code is executable | **FALLBACK** |
| **SWE-bench Lite / Verified** | Python repository-level issue/patch tasks; official dataset guide currently lists Lite as 534 and Verified as 500 | Issue, base commit, test patch, gold patch, `FAIL_TO_PASS`, `PASS_TO_PASS`; Verified is a solvability-reviewed subset | Python-compatible but PDB is not required; runtime-state fields, target symbols, and root-cause labels are not native task fields | Very high cost: official harness uses Docker and recommends x86_64, 120 GB free storage, 16 GB RAM, and 8 CPUs; large untrusted repository execution surface | **DEFER** |
| **Defects4J** | Java; 854 active bugs plus deprecated metadata in v3.0.1 | Triggering tests, buggy/fixed revisions, fault metadata, and controlled CLI | No PDB/Python compatibility; JVM/Java debugger adapter would be a different project | Java, Perl, Subversion, and external project repositories; substantial setup and licensing review | **NO-GO-FOR-NOW** |
| **Current curated v1** | Five small, single-file Python/pytest fixtures already in this repository | Exact `task.json` oracle, one F2P node, P2P nodes, full suite, allowed/denied paths, root-cause and runtime hints | Best fit: deliberately authored runtime clues, PDB budgets, and existing golden trajectories | Cheap and deterministic, but trusted-local rather than OS-level sandboxed; synthetic and not externally representative | **SMOKE GATE** |

Source references: [BugsInPy paper](https://arxiv.org/abs/2401.15481) and [BugsInPy repository](https://github.com/soarsmu/BugsInPy); [QuixBugs repository](https://github.com/jkoppel/QuixBugs) and [QuixBugs repair study](https://arxiv.org/abs/1805.03454); [SWE-bench paper](https://arxiv.org/abs/2310.06770), [official SWE-bench repository](https://github.com/SWE-bench/SWE-bench), and [official dataset guide](https://www.swebench.com/SWE-bench/guides/datasets/); [Defects4J repository and README](https://github.com/rjust/defects4j) and [original Defects4J paper record](https://homes.cs.washington.edu/~mernst/pubs/bug-database-issta2014-abstract.html).

Counts and variant descriptions are snapshot claims from the cited sources, not an instruction to fetch current data. A future run must pin a release/commit, dataset manifest, and source URLs before execution.

## 3. Comparison findings by criterion

### Language and PDB compatibility

BugsInPy and QuixBugs are Python candidates. BugsInPy is the better language match for the intended research question because its bugs come from non-trivial Python projects; QuixBugs is a useful protocol fallback because its Python programs are small and easy to instrument. SWE-bench is also Python, but its repository-scale harness and absence of debugger-specific task annotations make it a later extension. Defects4J fails this criterion because it is Java/JVM-focused.

### Task realism

BugsInPy is the best compromise for this prototype: it preserves real project structure and developer fixes while staying within Python. SWE-bench is more realistic at repository/issue scale, but that realism introduces a much larger environment and evaluation problem. QuixBugs is intentionally small algorithm repair, and the current fixtures are intentionally synthetic; neither supports repository-level generalization claims.

### Failing-test and correctness oracle

BugsInPy was constructed around reproducibility, isolated source fixes, and tests that fail on the faulty version and pass on the fixed version. This maps well to the verifier’s baseline and F2P/P2P contracts. QuixBugs has failing/passing tests and corrected programs, but its small tests are more vulnerable to overfitting; the repair study reports that 53.3% of plausible patches it studied were overfitting patches. SWE-bench has a particularly strong patch/test vocabulary through F2P/P2P and full repository execution, but the harness is too heavy for the first pilot. Defects4J has a strong triggering-test oracle but is not executable by this Python evaluator.

No candidate’s gold patch should be exposed to the agent during evaluation. Gold patches are for oracle construction, baseline validation, and post-hoc diagnosis only.

### Localization and root-cause suitability

BugsInPy supplies changed source patches and bug-revealing tests, which support target-file extraction and manual target-symbol annotation. It does not, by itself, provide the current schema’s `target_symbols` or a validated runtime-evidence hint. Those are adapter/annotation work and must be reviewed before a task enters the pilot. QuixBugs has a known defective program and one-line defect shape, so file/symbol localization is cheap, but it is a weak test of repository navigation or multi-frame diagnosis. SWE-bench supplies issue text and changed files through the patch, but localization and root-cause labels are not a native evaluator contract. Defects4J’s fault-localization metadata cannot compensate for the language mismatch.

The pilot must score localization separately from patch success and must not infer root-cause correctness from a passing test. Root-cause scoring needs an explicit rubric or structured annotation; the current runtime has a bounded root-cause statement in the demo catalog, but no validated semantic root-cause metric.

### Environment, reproducibility, and runtime-state usefulness

BugsInPy’s project-specific setup abstraction and Docker instructions are useful, but each selected task still needs a pinned revision, dependency lock/provenance, command, timeout, and rerun check. Its real project state is valuable for PDB research only after the failing test can be attached to a disposable workspace and the debugger can reach a stable pause. QuixBugs is cheaper and deterministic enough for adapter bring-up, but runtime usefulness is confounded by toy program size. SWE-bench’s official Docker harness is the strongest later reproducibility reference but is materially beyond the current internship-scale pilot. Defects4J has a controlled CLI but requires a non-Python toolchain.

### Licensing, redistribution, and hostile-code risk

The SWE-bench, QuixBugs, and Defects4J repositories identify MIT licensing in their official repository pages/README files. BugsInPy’s checked repository README describes the dataset and Docker workflow, but a root-level dataset license was not verified in the accessed repository view. Its underlying project repositories have their own licenses. Therefore, no BugsInPy artifact should be redistributed until the dataset license and every selected project’s license/notice requirements are recorded.

All external candidates contain executable source, tests, build scripts, and dependency installation instructions. The current `TaskWorkspace` and verifier disclose a trusted-local boundary; they are not an OS-level hostile-code sandbox. Before external execution, require process, filesystem, network, resource, dependency, and cleanup containment. Until that exists, perform only metadata/eligibility review and use the existing benign curated fixtures for offline contract checks.

## 4. Primary dataset decision: BugsInPy

BugsInPy is selected because it maximizes the combination of Python compatibility, real-bug realism, reproducible failing tests, isolated source changes, and manageable scale. Its 493-bug inventory is large enough for later stratified sampling without forcing the first campaign to run the full benchmark. The paper explicitly describes buggy/fixed revisions, isolated source patches, tests that expose the bugs, and project-specific build/test abstraction.

The decision is conditional. A BugsInPy task is eligible for this project only if a future offline manifest records:

1. pinned project and buggy revision;
2. exact source/test/license provenance;
3. a reproducible failing test and a passing regression set;
4. a stable Python interpreter and dependency installation recipe;
5. target file and symbol annotations reviewed against the buggy source;
6. a safe PDB entry point, breakpoint plan, and bounded observation budget;
7. no network or external-service requirement for the pilot;
8. clean disposable-workspace and cleanup behavior; and
9. a mapping into `DebugTask` without exposing the oracle to the agent.

This is an evaluation selection, not permission to download or execute BugsInPy in this task.

## 5. Fallback dataset decision: QuixBugs Python

QuixBugs is the fallback because its official repository provides 40 Python/Java counterparts, small programs, failing/passing tests, corrected Python programs, and an MIT license. It can validate the adapter’s basic mapping and the paired static-versus-PDB loop at low setup cost if BugsInPy’s project environments or licensing cannot be cleared.

The fallback has a strict interpretation: success on QuixBugs demonstrates adapter/verifier feasibility, not realistic repository debugging. Its one-line algorithm defects, small contexts, and known corrected programs make it unsuitable as the sole headline result. Use it only with explicit labels such as “QuixBugs feasibility slice,” report overfitting risk, and retain the current curated smoke gate.

The five current curated fixtures remain the preflight gate because they already satisfy the repository’s `DebugTask` schema and PDB/golden-trajectory contracts. They are not substituted for BugsInPy in the research claim and are not counted as external benchmark tasks.

## 6. Minimum pilot evaluation design

### Eligibility and preflight

Before any model evaluation, construct a manifest for **at least 8 BugsInPy tasks from at least 4 projects and at least 4 bug families**. Prefer two tasks per family only after eligibility checks; do not balance by count if that would admit unreproducible environments. Run the existing five curated fixtures as the deterministic smoke gate. The smoke gate must pass schema loading, baseline failure, PDB lifecycle where applicable, patch verification, replay, cleanup, and canonical-fixture immutability before external tasks are considered ready.

The first external slice should be rejected if any task lacks a genuine baseline failure, stable selected-test identity, usable PDB entry plan, or safe offline execution boundary. Replacing an ineligible task must be recorded in the manifest rather than silently changing the sample.

### Paired evaluation matrix

For the eligible 8-task slice, use:

- two policies: the existing static/test-feedback baseline and the PDB-enabled policy;
- the same task, model configuration, controller limits, prompt-visible fields, and patch/verifier contract for both policies;
- two repetitions per task/policy for a minimum of **32 cases**;
- fixed task and policy order, unique evaluation/case/run/trajectory/request identities, and no pooling with the historical OpenCode matrix;
- one pre-registered primary outcome: proportion of cases with verifier outcome `RESOLVED` and a complete, clean report;
- secondary outcomes: F2P pass rate, P2P preservation, full-suite consistency, localization outcome, structured root-cause score, PDB reach/open rate, successful and failed PDB observations, tool/test/patch counts, wall-clock duration, token fields when genuinely provider-reported, and failure taxonomy.

Two repetitions are the minimum useful design for a stochastic model path; this is still a pilot and is not a powered estimate of general performance. The paired task/policy structure is more important than the small absolute sample. The pilot must report per-task results, not only aggregate percentages.

### Success and interpretation gates

Do not interpret a PDB comparison until at least one PDB-enabled case reaches a valid PDB session and records bounded runtime evidence. If all PDB cases fail before PDB, the result is a controller/protocol or model-directive readiness finding, not a PDB-effectiveness result. Do not claim PDB benefit from a difference in `RESOLVED` counts unless baseline validity, model route, repetitions, policy order, and report completeness are all comparable.

Use the verifier as the correctness oracle. Treat patch application, syntax, timeout, baseline invalidity, test execution error, full-suite contradiction, cleanup failure, interrupted execution, and incomplete report as separately reported failure modes rather than silently mapping them to “model failure.”

## 7. Mapping to the current architecture and schema gaps

The natural mapping is:

| External artifact | Current field/path | Gap or required adapter rule |
|---|---|---|
| Bug ID and pinned project revision | `task_id`, `fixture_path`, manifest provenance | Current schema assumes a local fixture path; add an external-task manifest/checkout layer later without weakening path validation. |
| Issue/bug description | `title`, `description` | Preserve source wording and provenance; do not put gold patch or oracle hints in agent-visible fields. |
| Buggy test command | `reproduction.argv`, `reproduction.cwd`, `expected_exit_code` | Normalize project-specific commands and verify the failure is genuine. |
| Triggering tests | `tests.fail_to_pass` | Current schema requires exactly one F2P node; select one stable trigger and record any additional triggers as provenance/secondary checks. |
| Regression tests | `tests.pass_to_pass` | Current schema requires at least two; select stable nodes and retain the full relevant set for later expansion. |
| Full project suite | `tests.full_suite_argv` | Current verifier expects a parseable, bounded pytest-style full suite; non-pytest or huge suites need an explicit runner/adapter decision. |
| Allowed/denied paths and budgets | `constraints` | External projects need generated allowlists, denied test/manifest paths, network false, external services false, and bounded patch/test/PDB budgets. |
| Gold patch and changed files | evaluator-only `oracle` plus provenance | Current `Oracle` needs target files/symbols, root-cause summary, and runtime hint; these require annotation and review, not blind extraction. |
| Runtime/PDB plan | `oracle.runtime_evidence_hint`, controller/PDB records | No external benchmark supplies a validated PDB trajectory. Add a separate reviewed breakpoint/driver plan; never let the model see the hint. |
| Live case and trajectory evidence | `live.py` case/report schema and event replay | Preserve unique IDs, configuration fingerprint, report completion, cleanup, and token-usage missingness. |

The largest gaps are external checkout/environment provenance, multiple F2P support, non-pytest test runners, target-symbol/root-cause annotations, debugger-entry plans, pytest-aware/post-mortem debugging, and real containment. The current PDB path runs a separate script/driver rather than attaching to a failing pytest process. These are adapter/schema design tasks, not reasons to weaken the current verifier.

## 8. Metric coverage and missing requirements

Covered today:

- verifier completion and semantic outcome taxonomy;
- F2P and P2P pass counts;
- full-suite status and contradiction detection;
- patch application, changed files, syntax, patch attempts, and cleanup;
- localization categories based on declared localization and changed files;
- PDB session/action/observation counters and runtime-evidence outcomes;
- controller/tool/test command counts and bounded budgets;
- live model request/retry/provider-error counts, token-usage presence/missing fields, and transport/case timing;
- event/trajectory identity, replay, and deterministic mismatch evidence.

Missing or insufficient for the external pilot:

1. **Root-cause correctness:** add a blinded, rubric-based structured score against reviewed annotations; do not use free-form similarity as the only metric.
2. **Statement-level localization:** add line/span or AST-node target labels where reliable, while retaining file/symbol accuracy as a separate metric.
3. **PDB reachability:** report attempted, opened, usable, and evidence-linked PDB sessions separately.
4. **Environment reproducibility:** record pinned revision, Python version, dependency lock/hash, setup result, test command hash, and rerun consistency.
5. **Cost normalization:** distinguish provider-reported tokens/cost from wall-clock and local command time; missing provider fields must remain missing.
6. **Contamination/leakage:** record benchmark release, model knowledge cutoff where available, and whether issue/patch/test text was visible during any training or retrieval step.
7. **Sample uncertainty:** report per-task paired outcomes and confidence intervals only when the sample and independence assumptions justify them; do not overstate an 8-task pilot.

## 9. Reproducibility and security constraints

No future external run should begin without a pinned dataset release/commit, immutable task manifest, source/license ledger, environment fingerprint, deterministic task ordering, and captured setup/test logs. The manifest must separate evaluator-only oracle data from agent-visible task data. Gold patches must remain unavailable to the model process.

Removing `oracle` from the model request is not sufficient by itself: the current workspace contains `task.json` and tests, and the ordinary file-reading path can inspect workspace files. The external adapter must therefore keep oracle labels, gold patches, and any hidden-test metadata outside the agent-readable workspace or add an explicit hidden-evaluator boundary before execution.

The trusted-local boundary is not sufficient for hostile or merely surprising benchmark code. Before external execution, the next implementation campaign must establish OS/container-level isolation, network denial, process/resource limits, bounded output and disk use, dependency provenance, workspace ownership, cleanup verification, and a policy for setup scripts. The current task intentionally does not implement that hardening.

Do not download or execute any candidate dataset as part of this decision. Do not install dependencies, invoke a provider, run a live model, or use an OpenCode route.

## 10. RAG, SFT, and DPO decisions

| Workstream | Decision | Rationale |
|---|---|---|
| RAG | **NO-GO-FOR-NOW** for a research comparison; **DEFER** implementation beyond existing deterministic file/code-search foundations | Retrieval would change the information available to the agent before the task distribution, adapter, baseline, and localization/root-cause metrics are stable. First establish a non-RAG BugsInPy baseline and record retrieval as a later controlled factor. |
| Supervised fine-tuning | **DEFER** | The primary task format, eligible slice, root-cause/PDB trajectory schema, and real-model baseline are not yet measured. Issue-to-patch data alone does not teach the debugger-state interaction being evaluated. |
| DPO / preference optimization | **NO-GO-FOR-NOW** | There is no approved preference dataset with reliable success/failure labels, paired trajectories, stable evaluator, or SFT baseline. DPO now would confound data quality, model changes, retrieval, and controller readiness. |

These are sequencing decisions, not claims that the methods are generally ineffective. Reopen them only after the primary pilot has stable task manifests, baseline trajectories, metric coverage, and containment.

## 11. Next implementation task

**Implement the BugsInPy eligibility-manifest and adapter design campaign (documentation and tests first).** Its bounded deliverables should be:

1. a pinned, license-reviewed candidate manifest for more than the minimum 8-task screen;
2. a dry-run schema mapping specification, including multiple-trigger handling and target-symbol/root-cause annotation;
3. a containment requirements checklist and execution boundary decision;
4. a deterministic smoke/preflight test plan using the existing curated fixtures; and
5. an explicit go/no-go review before any dataset checkout or execution.

This is the next task, not work performed in Dataset and Evaluation Decision v1. No adapter or runtime source change is authorized by this document.

## 12. Limitations and unresolved questions

- BugsInPy license status for the dataset repository itself was not conclusively verified from the accessed primary repository view; selected project licenses remain a separate obligation.
- No candidate dataset was downloaded, so setup times, task-level reproducibility, and PDB reachability remain unverified for this checkout.
- The official SWE-bench dataset guide is mutable; the recorded Lite/Verified counts must be pinned before later use.
- BugsInPy’s original paper and repository are not a guarantee that every current checkout remains reproducible under today’s Python/dependency versions.
- The current localization implementation is oracle/file/symbol-oriented and does not establish statement-level fault localization.
- Root-cause scoring, contamination accounting, and external-environment fingerprints are not yet sufficient for a broad model claim.
- The 8-task, 32-case pilot is a feasibility and comparative-signal design, not a statistical performance benchmark.
- It remains unresolved whether one stable external test trigger can be selected for every eligible BugsInPy task without biasing the sample.
- It remains unresolved whether the current evaluator should gain a generalized test-runner interface before BugsInPy, or whether the first slice should be restricted to compatible pytest tasks.

## Final decision record

Proceed with **current curated smoke gate → BugsInPy primary pilot → QuixBugs fallback only if necessary**. Keep SWE-bench for later repository-scale validation, exclude Defects4J from the current Python/PDB track, and do not begin RAG, SFT, or DPO/preference optimization until the minimum pilot and missing metric/containment requirements are resolved.
