# Paper Notes — SWE-bench

## Bibliography

- Title: SWE-bench: Can Language Models Resolve Real-World GitHub Issues?
- Authors: Carlos E. Jimenez, John Yang, Alexander Wettig, Shunyu Yao, Kexin Pei, Ofir Press, Karthik Narasimhan
- Affiliation: Princeton University, Princeton Language and Intelligence, University of Chicago
- Venue: ICLR 2024
- arXiv: 2310.06770v3
- Year: 2024 conference version
- Local PDF path: research/papers/tier1_must_read/2023_swe_bench_can_lms_resolve_github_issues.pdf
- Access level: FULL_TEXT_READ

## Why this paper matters

SWE-bench is the main repository-level benchmark underlying many modern software engineering agent papers. It defines the evaluation world that Agentless, SWE-agent, AutoCodeRover, OpenHands, and many other systems compete on.

For this project, SWE-bench is not the first implementation target, but it is essential background because it defines:

- real GitHub issue resolution as a benchmark task,
- patch-based evaluation,
- fail-to-pass and pass-to-pass tests,
- repository-scale context difficulty,
- benchmark construction from real PRs,
- the difference between “patch applies” and “issue resolved.”

Our MVP should not chase full SWE-bench immediately, but SWE-bench gives the correct evaluation vocabulary.

## Core contribution

SWE-bench introduces an evaluation framework of 2,294 software engineering problems drawn from real GitHub issues and corresponding pull requests across 12 popular Python repositories.

Each task provides:

- an issue description,
- a snapshot of the codebase at the base commit,
- hidden or benchmark-side tests derived from the PR,
- a gold/reference patch,
- executable infrastructure to apply a model-generated patch and run tests.

The model’s job is to generate a patch that resolves the issue.

## Main problem addressed

Existing coding benchmarks such as HumanEval mostly use short, self-contained tasks. SWE-bench argues that real software engineering requires:

- navigating large repositories,
- understanding issue descriptions,
- identifying relevant files,
- coordinating edits across functions/classes/files,
- using tests and execution environments,
- producing valid patch files,
- preserving existing behavior.

This makes SWE-bench much harder and more realistic than function-level code generation.

## Dataset construction

SWE-bench uses a three-stage construction pipeline.

### Stage 1 — Repository selection and PR scraping

The authors start from popular Python repositories. The paper uses 12 popular open-source Python repositories:

- astropy
- django
- flask
- matplotlib
- pylint
- pytest
- requests
- scikit-learn
- seaborn
- sphinx
- sympy
- xarray

They crawl about 90,000 PRs from these repositories.

### Stage 2 — Attribute filtering

A candidate PR must:

1. be merged,
2. resolve one or more GitHub issues,
3. introduce one or more tests.

This is intended to identify PRs where a developer fixed or implemented something and added tests that can later verify the solution.

### Stage 3 — Execution-based filtering

For each candidate, SWE-bench:

1. checks out the base commit,
2. installs the repository in an execution environment,
3. applies the test patch,
4. runs tests before the solution patch,
5. applies the solution patch,
6. runs tests after the solution patch,
7. keeps only tasks where at least one test changes from fail to pass.

This produces 2,294 final task instances.

## Task formulation

Input to model:

- issue text,
- codebase snapshot,
- sometimes retrieved code files depending on the evaluation setup.

Output from model:

- a patch file.

A generated solution is considered resolved if:

1. the patch applies successfully,
2. all fail-to-pass tests pass,
3. all pass-to-pass tests remain passing.

Important distinction:

- “Apply rate” only means the patch could be applied and tested.
- “Resolved” means the patch passes the relevant behavioral tests.

## SWE-bench instance fields

Important fields in a SWE-bench task instance:

- `base_commit`: commit ID for the pre-fix codebase,
- `created_at`: original PR creation timestamp,
- `hints_text`: optional comments/suggestions before PR creation,
- `instance_id`: unique repo/pull-number ID,
- `issue_numbers`: resolved issues,
- `patch`: reference/gold patch,
- `problem_statement`: issue title/body/comments,
- `pull_number`: original PR,
- `test_patch`: tests introduced by the PR,
- `version`: repository release version,
- `repo`: source repository,
- `FAIL_TO_PASS`: tests that fail before and pass after the gold patch,
- `PASS_TO_PASS`: tests that pass before and after the gold patch,
- `env_install_commit`: commit used for dependency installation.

For our project, `FAIL_TO_PASS` and `PASS_TO_PASS` are the most important evaluation concepts.

## Dataset statistics

SWE-bench contains:

- 2,294 task instances,
- 12 repositories,
- average issue text length around 195 words,
- average non-test codebase size around 3,010 files,
- average non-test lines around 438K,
- average gold patch edits:
  - 1.7 files,
  - 3.0 functions,
  - 32.8 lines,
- average tests:
  - 9.1 fail-to-pass,
  - 120.8 total.

The paper emphasizes that codebases are large and the relevant edits are small relative to the full repository.

## SWE-bench Lite

SWE-bench Lite is a 300-instance subset designed for cheaper and faster evaluation.

It focuses more on self-contained functional bug fixes and is intended as a more accessible short-term benchmark. Later work such as Agentless and debug-gym use SWE-bench Lite heavily.

For our project:

- SWE-bench Lite is still probably too heavy for the first PDB MVP.
- But it is useful as a reference benchmark and future extension target.
- Agentless results on SWE-bench Lite are directly relevant as a baseline.

## SWE-Llama

The paper also introduces SWE-Llama:

- CodeLlama-Python 7B and 13B fine-tuned for repository patch generation.
- Training data:
  - 19,000 issue-PR pairs,
  - 37 additional repositories,
  - disjoint from the evaluation benchmark repositories.
- Fine-tuning method:
  - LoRA,
  - attention sublayers,
  - excludes sequences over 30,000 tokens,
  - effective corpus reduced to about 10,000 instances.

Training details from appendix:

- LoRA rank r = 16,
- alpha = 16,
- dropout = 0.05,
- attention query/key/value/output projection matrices,
- learning rate 6e-4,
- batch size 32,
- max 4 epochs,
- SWE-Llama 7B trained in ~20 hours on 4 A100s,
- SWE-Llama 13B trained in ~47 hours on 8 A100s.

Project implication:

- Fine-tuning is expensive and context-distribution-sensitive.
- We should not start with SFT.
- If we later train a local model, we need debugger trajectories, not just issue-to-patch pairs.

## Retrieval setup

Because full repositories are far too large for normal context windows, the paper evaluates retrieval setups.

### BM25 sparse retrieval

BM25 retrieves relevant files from the repository using the issue description.

Important observations:

- Dense retrieval was considered ill-suited due to long code documents and natural-language-to-code retrieval.
- BM25 performance depends heavily on context size.
- Larger retrieved contexts can improve oracle-file recall but still reduce model performance due to distraction.

BM25 recall against oracle files:

- 13k context: average 29.58
- 27k context: average 44.41
- 50k context: average 51.06

But model resolution can drop as context grows.

### Oracle retrieval

Oracle retrieval gives models the files edited by the gold/reference patch.

This is not realistic because real systems do not know the correct files in advance, but it helps analyze whether models can patch correctly when given ideal file-level localization.

Key lesson:

- Localization remains a central bottleneck.
- Even oracle file retrieval does not make SWE-bench easy.
- Correct file context is necessary but not sufficient.

## Main model results

With BM25 retrieval:

- Claude 3 Opus: 3.79% SWE-bench, 4.33% SWE-bench Lite.
- Claude 2: 1.97% SWE-bench, 3.00% SWE-bench Lite.
- ChatGPT-3.5: 0.17% SWE-bench, 0.33% SWE-bench Lite.
- GPT-4-turbo: 1.31% SWE-bench, 2.67% SWE-bench Lite.
- SWE-Llama 7B: 0.70% SWE-bench, 1.33% SWE-bench Lite.
- SWE-Llama 13B: 0.70% SWE-bench, 1.00% SWE-bench Lite.

The original benchmark is extremely difficult for direct retrieval + patch-generation baselines.

With oracle retrieval:

- Claude 2: 4.80%
- ChatGPT-3.5: 0.52%
- GPT-4: 1.74% on 25% subset
- SWE-Llama 7B: 3.01%
- SWE-Llama 13B: 3.97%

With oracle-collapsed retrieval:

- Claude 3 Opus: 9.39%
- Claude 2: 5.93%
- GPT-4: 3.40%
- ChatGPT-3.5: 1.09%

## Findings and observations

### 1. Context length can hurt

Even if larger contexts improve file recall, models can become distracted by additional code. The paper observes that Claude 2 performance drops as total input length increases.

Implication:

- Our RAG system should not dump large raw context.
- Use compact representations:
  - file tree,
  - skeletons,
  - source windows,
  - stack-relevant frames,
  - runtime locals.
- Prefer evidence-rich small context over broad noisy context.

### 2. Localization is hard

SWE-bench requires finding small edits in very large repositories. The paper’s BM25 vs oracle analysis shows that retrieval often misses relevant files, and oracle retrieval still leaves reasoning and patching hard.

Implication:

- Our PDB agent should not rely only on semantic search.
- Runtime stack traces and debugger state can provide high-value localization signals.
- PDB is especially useful when the failure path directly reaches the bug.

### 3. Patch generation is hard

Models struggle to produce correctly formatted patches. Patch application rate and resolution rate are very different.

Implication:

- Use deterministic patch application.
- Keep Search/Replace patches or unified diff patches tightly constrained.
- Validate patch formatting separately from semantic correctness.

### 4. Generated patches are often too short/simple

The paper finds model patches often edit fewer lines and fewer files than gold patches. Models tend to produce primitive direct fixes and may ignore codebase conventions, dependencies, and broader maintainability.

Implication:

- Passing tests is not enough for final quality.
- Add code review/verifier checks later.
- Root-cause explanation should include why the patch is semantically right, not just why it passes the failing test.

### 5. Execution feedback is valuable

The appendix outcome taxonomy shows that many applied patches are No-Op or Regression. The authors note this highlights the value of execution-environment feedback that lets models run fixes and decide whether to continue editing.

Implication:

- Our agent must run tests after patch attempts.
- Debugger observations and tests should be used as a closed-loop process.
- A controller should decide whether to inspect more state, patch, retry, or stop.

### 6. Some tasks need multimodal issue understanding

The paper notes some issue descriptions include embedded images. Examples include matplotlib/seaborn visualization issues.

Implication:

- This is out of scope for the first MVP.
- Later, issue-image handling could matter for UI/plotting libraries.
- For now, choose text-only reproducible bugs.

## Qualitative failure modes

The paper’s qualitative examples show several recurring model failures:

1. Correct location but wrong logic.
2. Primitive fix instead of using existing codebase utilities.
3. Patch passes tests but is stylistically or architecturally inferior.
4. Failure to understand third-party library or internal dependency.
5. Under-generating necessary multi-file or multi-function changes.
6. No-op patches.
7. Regression patches that break pass-to-pass tests.
8. Inability to reason from embedded images.
9. General StackOverflow-like fixes that do not fit the codebase.

For our project, these become verifier and evaluation concerns.

## Evaluation procedure details

Evaluation steps:

1. Reset repository to base commit.
2. Activate correct executable environment.
3. Install codebase.
4. Apply test patch.
5. Apply model prediction patch.
6. Attempt automatic patch repair if patch application fails.
7. Run test script.
8. Parse logs into test-status mappings.
9. Check all `FAIL_TO_PASS` and `PASS_TO_PASS` tests.

A task is solved only if all F2P and P2P tests pass.

Important evaluation categories for applied patches:

| F2P | P2P | Outcome |
|---|---|---|
| All | All | Resolved |
| All | Partial/None | Breaking Resolved |
| Partial | All | Partially Resolved |
| Partial | Partial/None | Work in Progress |
| None | All | No-Op |
| None | Partial/None | Regression |

This taxonomy is useful for our own experiments. We should not only report success/failure; we should classify failure modes.

## Limitations

Paper-level limitations:

- all tasks are Python,
- benchmark may not cover every software-engineering domain,
- direct BM25/retrieval baselines are simple,
- execution-based tests do not guarantee code quality,
- generated solutions may be less comprehensive, efficient, or readable than human solutions,
- models may have been exposed to repository code, though temporal analysis suggests simple memorization is not enough.

Project-level limitation:

- SWE-bench is repository-level, not debugger-centered.
- It does not require an interactive debugger.
- It is too heavy as the first benchmark for our PDB MVP.
- But its F2P/P2P evaluation protocol is valuable.

## What applies to our project

Directly reusable:

1. Patch-based evaluation.
2. Base-commit checkout model.
3. Fail-to-pass and pass-to-pass test split.
4. Patch apply + test execution harness.
5. Failure outcome taxonomy.
6. Repository mirror/base commit concept.
7. Benchmark-construction idea from issue + PR + tests.
8. Temporal leakage analysis principle.
9. Long-context warning.
10. Need for robust execution environments.

## What does not apply directly

Not directly reusable for MVP:

- full SWE-bench scale,
- 12-repository Docker/conda environment complexity,
- leaderboard-driven evaluation,
- BM25-only retrieval,
- oracle retrieval as realistic system setting,
- direct issue-to-patch fine-tuning as first step,
- multimodal issue handling.

## Relation to ChatDBG, debug-gym, and Agentless

### Compared with ChatDBG

- SWE-bench defines repository-level issue resolution.
- ChatDBG defines debugger-mediated runtime diagnosis.
- SWE-bench does not require interactive debugger use.
- ChatDBG does not perform SWE-bench-style patch validation.

Our project should combine:

- ChatDBG-style runtime state,
- SWE-bench-style patch/test validation.

### Compared with debug-gym

- debug-gym adapts repository debugging into a text-based interactive environment with PDB.
- SWE-bench supplies the canonical repository-level task formulation and evaluation style.
- debug-gym’s SWE-bench-Lite experiments are built on this benchmark lineage.

### Compared with Agentless

- Agentless is an approach evaluated on SWE-bench/SWE-bench Lite.
- SWE-bench is the benchmark substrate.
- Agentless improves over simple retrieval baselines by fixed staged localization/repair/validation.
- Our PDB agent should be compared to an Agentless-style staged baseline.

## Project decisions after reading

- [x] Use F2P/P2P terminology in our evaluation design.
- [x] Do not use full SWE-bench as first MVP benchmark.
- [x] Use SWE-bench as future extension / literature benchmark.
- [x] Use BugsInPy or a curated Python bug set first for PDB-controlled experiments.
- [x] Adopt patch-apply + test-run harness as core verifier.
- [x] Track patch outcomes beyond binary resolved/unresolved.
- [x] Avoid broad noisy context; prefer compact source windows and runtime-state evidence.
- [x] Compare against Agentless-style staged baseline.

## Candidate MVP evaluation protocol inspired by SWE-bench

For each Python bug task:

1. Start from buggy repository/script state.
2. Provide issue/failure description and failing test/trace.
3. Agent proposes patch.
4. Apply patch deterministically.
5. Run reproduction test.
6. Run regression/pass-to-pass tests.
7. Categorize outcome:
   - Resolved,
   - Breaking Resolved,
   - Partially Resolved,
   - Work in Progress,
   - No-Op,
   - Regression.
8. Record:
   - localization accuracy,
   - root-cause explanation quality,
   - patch success,
   - number of PDB actions,
   - number of test runs,
   - runtime,
   - model cost/tokens.

## One-paragraph Turkish explanation for my own understanding

SWE-bench, LLM’lerin gerçek GitHub issue’larını çözme kabiliyetini ölçen temel repository-level benchmark’tır. HumanEval gibi kısa fonksiyon yazma benchmark’larından farklı olarak modelden büyük bir Python repository snapshot’ı ve issue açıklaması üzerinden gerçek bir patch üretmesi beklenir. Değerlendirme, patch’in uygulanıp uygulanmadığına ve fail-to-pass ile pass-to-pass testlerin geçip geçmediğine bakar. Paper, 2,294 task instance oluşturur ve ilk basit retrieval + patch generation baseline’larının çok düşük başarı gösterdiğini raporlar. Bizim proje için SWE-bench’in ana değeri, PDB/debugger agent’ın ilk hedefi olmak değil; doğru evaluation dili ve test-based verifier mantığını vermesidir. İlk MVP daha küçük Python/PDB bug setiyle başlamalı, fakat değerlendirme protokolü SWE-bench’ten F2P/P2P, patch apply, regression test ve failure taxonomy kavramlarını almalıdır.
