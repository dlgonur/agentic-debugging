# Paper Notes — Agentless

## Bibliography

- Title: AGENTLESS: Demystifying LLM-based Software Engineering Agents
- Authors: Chunqiu Steven Xia, Yinlin Deng, Soren Dunn, Lingming Zhang
- Affiliation: University of Illinois Urbana-Champaign
- Year: 2024
- arXiv: 2407.01489v2
- Local PDF path: research/papers/tier1_must_read/2024_agentless_demystifying_llm_se_agents.pdf
- Access level: FULL_TEXT_READ

## Why this paper matters

Agentless is one of the strongest non-debugger baselines for this project.

The paper directly challenges the assumption that complex autonomous software-engineering agents are required for repository-level issue resolution. Instead, it shows that a simpler fixed pipeline — localization, repair, and patch validation — can be highly competitive on SWE-bench Lite at low cost.

For our project, Agentless is important because it defines the baseline that a debugger-assisted agent must beat. If a Python/PDB agent is more complex than Agentless but does not outperform it or provide better root-cause explanations, then the additional debugger machinery is not justified.

## Central question

The paper asks:

> Do we really have to employ complex autonomous software agents?

Its answer is cautious and empirical: not always. A simple non-agentic pipeline can outperform or match many open-source agentic systems on SWE-bench Lite.

## Core thesis

Complex tool-using agents have several weaknesses:

1. Complex tool abstractions can be hard for LLMs to use correctly.
2. Letting the LLM autonomously decide future actions can produce long, hard-to-debug trajectories.
3. Incorrect intermediate actions can compound over many turns.
4. LLMs have limited self-reflection and may amplify bad feedback.

Agentless avoids this by using a staged pipeline with fixed control flow:

```text
issue + repository
  -> localization
  -> repair
  -> patch validation
  -> final patch
```

The LLM is used inside individual subtasks, but it is not allowed to autonomously plan, choose arbitrary tools, or decide the next action in an open-ended environment.

## System overview

Agentless has three phases:

1. Localization
2. Repair
3. Patch validation

The figure on page 5 shows the full flow: issue + project codebase enter a hierarchical localization pipeline, patches are generated from localized edit locations, reproduction tests are generated, and patch validation filters/ranks candidate patches before choosing a final submitted patch.

## Phase 1 — Localization

Agentless uses hierarchical localization:

1. Localize to suspicious files.
2. Localize suspicious files to related classes, functions, and variables.
3. Localize to concrete edit locations.

### 1. File-level localization

Inputs:

- repository structure,
- issue description.

Agentless first constructs a concise tree-like repository representation, similar to the Linux `tree` command. It asks the LLM to identify suspicious files.

It also uses embedding-based retrieval:

- asks the LLM to identify irrelevant folders,
- filters those folders out,
- chunks remaining files,
- embeds chunks and issue description,
- retrieves relevant files,
- combines prompting-based and embedding-based localization.

Ablation result:

- prompting-only file localization: 78.67% contains GT location,
- embedding-only without irrelevant filtering: 67.67%,
- embedding with irrelevant filtering: 70.33%,
- combined: 81.67%.

Interpretation:

- LLM file localization and embedding retrieval are complementary.
- Filtering irrelevant folders improves cost and performance.
- File localization is a strong deterministic/retrieval-heavy pattern that can be reused.

### 2. Related-element localization

After selecting suspicious files, Agentless builds a compressed file skeleton:

- class headers,
- function headers,
- variable declarations,
- class fields,
- method signatures,
- class/module comments.

It does not pass the entire file if not needed. This reduces context and makes the prompt more focused.

Ablation result:

- complete file context: 53.67% contains GT location, average cost $0.15,
- skeleton format: 58.33% contains GT location, average cost $0.02.

Interpretation:

- compressed structural representations can outperform full-file context.
- too much context can confuse the model and increase cost.
- our RAG/indexer should support skeleton-style representations.

### 3. Edit-location localization

Agentless then provides selected code content and asks the LLM to identify concrete edit locations:

- lines,
- functions,
- classes.

Ablation result:

- greedy edit location: 50.67% contains GT location,
- direct from file-level: 47.00%,
- multi-samples merged: 56.33%,
- separate multi-samples: used as default for better downstream repair.

Interpretation:

- hierarchical localization is superior to jumping directly from file-level to edit locations.
- sampling different edit-location sets improves downstream patch diversity.
- separate sampled locations can be better than merging everything into a huge context.

## Phase 2 — Repair

Agentless generates patches from localized edit locations.

Instead of asking the model to rewrite an entire file, it uses a Search/Replace edit format:

```text
<<<<<<< SEARCH
old code
=======
new code
>>>>>>> REPLACE
```

The patch is applied by matching the search snippet in the original file and replacing it with the replacement snippet.

Important design rationale:

- avoids rewriting whole files,
- focuses the model on small edits,
- reduces hallucination,
- lowers cost,
- makes patch application deterministic.

Default repair setup:

- four sampled edit-location sets,
- ten patches per location set,
- total 40 candidate patches per issue.

Ablation result:

- greedy location, 40 samples: 88 fixes (29.33%),
- multi-samples merged, 40 samples: 85 fixes (28.33%),
- separate multi-samples, 4 x 10 samples: 96 fixes (32.00%).

Figure 6 shows that increasing patch samples improves performance up to around 40 samples, after which performance plateaus. Considering all samples as an oracle upper bound, Agentless could potentially solve 126 issues (42.0%), which suggests patch ranking/selection is a major bottleneck.

## Phase 3 — Patch validation

Agentless uses generated reproduction tests and existing regression tests to choose the final patch.

### Reproduction test generation

In SWE-bench, the original repository usually has regression tests but no bug-triggering reproduction test, because the issue was newly raised.

Agentless uses the LLM to generate reproduction tests that should print:

- `Issue reproduced` on the original buggy repository,
- `Issue resolved` after a correct patch,
- `Other issues` if the test fails for unrelated reasons.

For each issue, Agentless samples 40 reproduction tests. It executes them on the original repository and keeps only those that reproduce the issue.

Observed quality:

- out of 300 SWE-bench Lite problems, Agentless generated 213 tests that output the required reproduction message on the original repo,
- only 94 also output `Issue resolved` after applying the ground-truth patch.

Interpretation:

- reproduction test generation is useful but noisy.
- issue descriptions often lack enough information to generate fully correct tests.
- reproduction tests must be combined with regression tests and fallbacks.

### Patch selection

Patch selection process:

1. Run existing tests on the original repo.
2. Determine passing tests to use as regression tests.
3. Ask LLM to filter out tests that should not be treated as regression tests.
4. Run regression tests on all candidate patches.
5. Keep patches with the lowest regression failures.
6. Run selected reproduction test.
7. Keep patches that output `Issue resolved`.
8. If no patch passes reproduction test, fall back to regression tests.
9. Normalize patches.
10. Majority-vote over normalized patches.

Patch validation ablation:

- majority voting only: 77 fixes (25.67%),
- + regression tests: 81 fixes (27.00%),
- + reproduction tests: 96 fixes (32.00%).

Interpretation:

- generated reproduction tests are the biggest patch-selection boost.
- validation is not merely a cleanup step; it is central to performance.
- our debugger-agent should also generate/execute reproduction evidence, not only rely on model reasoning.

## Experimental setup

Dataset:

- SWE-bench Lite,
- 300 repository-level software engineering problems.

Implementation:

- LLM: GPT-4o (`gpt-4o-2024-05-13`),
- greedy decoding by default,
- sampling temperature 0.8 for sampled outputs,
- embedding model: OpenAI `text-embedding-3-small`,
- chunk size: 512,
- chunk overlap: 0,
- top 3 suspicious files at file localization,
- 4 edit-location samples,
- 10 patches per location set,
- 40 candidate patches per issue,
- 40 reproduction test samples per issue.

Metrics:

- `% Resolved`,
- average dollar cost,
- average input/output tokens,
- correct location at line/function/file granularity.

## Main results on SWE-bench Lite

Agentless solves:

- 96 / 300 issues,
- 32.00% resolved,
- average cost $0.70,
- average tokens 78,166,
- correct location: 35.3% line, 52.0% function, 69.7% file.

The paper claims this is the highest performance among open-source approaches at the time of the evaluated leaderboard snapshot, although some closed-source/commercial systems have higher solve rates.

Important comparison points from Table 1:

- SWE-agent with Claude 3.5 Sonnet: 69 / 300 (23.00%), $1.62.
- SWE-agent with GPT-4o: 55 / 300 (18.33%), $2.53.
- AutoCodeRover GPT-4: 57 / 300 (19.00%), $0.45.
- AutoCodeRover-v2 GPT-4o: 92 / 300 (30.67%).
- OpenDevin + CodeAct v1.8 Claude 3.5 Sonnet: 80 / 300 (26.67%), $1.14.
- RAG baselines are far lower.

Interpretation for our project:

- Agentless is not a weak baseline.
- A debugger-assisted agent must be compared against this kind of staged baseline.
- Complexity must earn its keep.

## SWE-bench Lite issue-quality analysis

The authors manually classify SWE-bench Lite issues.

### Description quality

Breakdown:

- contains reproducible example: 54.3%,
- contains partially reproducible example: 8.7%,
- enough information in natural language: 27.0%,
- not enough information: 10.0%.

### Solution in description

Breakdown:

- no solution: 73.3%,
- some steps in natural language: 7.7%,
- complete steps in natural language: 9.7%,
- exact patch: 4.3%,
- misleading: 5.0%.

### Location information

The paper reports that few issues provide exact line-level locations, but around half provide file-level location information in the description. This matters because localization difficulty varies heavily depending on how much location information is already leaked by the issue.

Important benchmark warning:

- Some SWE-bench Lite tasks are under-specified.
- Some contain exact or near-exact solution steps.
- Some contain misleading solution suggestions.
- Raw leaderboard scores may not purely measure agent capability.

## SWE-bench Lite-S

Agentless constructs SWE-bench Lite-S by removing:

- issues that contain exact patches,
- issues with misleading solutions,
- issues without enough information.

Resulting subset:

- 249 problems.

Agentless performance on SWE-bench Lite-S:

- 84 / 249,
- 33.73% resolved,
- rank 9 in the listed table.

Interpretation:

- filtering changes the dataset quality and slightly changes rankings.
- evaluation should distinguish benchmark performance from actual debugging ability.
- for our work, we should avoid using unfiltered benchmark results as sole evidence.

## SWE-bench Verified

The paper also reports results on SWE-bench Verified:

- Agentless with GPT-4o: 194 / 500,
- 38.80% resolved.

The paper states Agentless performs strongly among open-source approaches and best among GPT-4o-based approaches in the listed table.

Note:

- Because SWE-bench leaderboards and model results evolve quickly, these numbers should be treated as paper-snapshot results, not permanent current leaderboard facts.

## Threats to validity

Internal threats:

- GPT-4o may have seen ground-truth developer patches during training.
- Because GPT-4o is closed-source, this cannot be fully ruled out.
- Prior work often uses similar closed-source models, so the comparison is not unique to Agentless.

External threats:

- evaluation is mostly on SWE-bench Lite,
- performance may not generalize to other datasets,
- the authors argue OpenAI's SWE-bench Verified evaluation gives additional support.

## What applies to our project

Strongly reusable:

1. Hierarchical localization:
   - file-level,
   - class/function skeleton,
   - edit location.

2. Skeleton representation:
   - useful for RAG/indexing,
   - better than dumping full files.

3. Search/Replace patch format:
   - deterministic,
   - low-cost,
   - safer than whole-file rewriting.

4. Multiple patch sampling:
   - useful for candidate diversity.

5. Reproduction test generation:
   - critical for patch ranking,
   - must be combined with regression tests.

6. Patch normalization and majority voting:
   - useful as a verifier/reranker baseline.

7. Dataset-quality warnings:
   - exact patch leakage,
   - under-specified issues,
   - misleading issue descriptions,
   - location leakage.

## What does not apply directly

Not directly reusable as final architecture:

- no debugger/PDB runtime inspection,
- no stack-frame or variable-state analysis,
- no autonomous evidence-gathering through runtime tools,
- no causal root-cause explanation requirement,
- depends on GPT-4o and OpenAI embeddings,
- SWE-bench-focused setup may not map cleanly to BugsInPy/PDB-style evaluation.

## Relation to ChatDBG and debug-gym

Compared with ChatDBG:

- ChatDBG gives LLM access to debugger state.
- Agentless avoids open-ended tool use and focuses on fixed localization/repair/validation.
- ChatDBG is closer direct prior art for debugger control.
- Agentless is a stronger baseline for repository-level repair.

Compared with debug-gym:

- debug-gym evaluates interactive PDB-based agent behavior.
- Agentless evaluates whether a non-agentic staged pipeline can beat complex agents.
- debug-gym suggests PDB helps strong models but can harm weaker models if exposed too early.
- Agentless suggests fixed controller logic and staged execution can outperform open-ended autonomy.

## Key project implications

1. Our MVP should not be an unconstrained autonomous agent.
2. We should use fixed phases:
   - reproduce,
   - localize,
   - inspect runtime state,
   - hypothesize,
   - patch,
   - validate.
3. Debugger access should be controller-gated, not always-on.
4. We should keep an Agentless-style baseline:
   - hierarchical localization,
   - Search/Replace patch,
   - regression + reproduction tests,
   - no PDB.
5. The main research question becomes:
   - Does adding PDB/runtime-state evidence improve over an Agentless-style staged baseline?

## Baseline design for our experiments

Recommended baseline:

```text
agentless_static_baseline:
  input: issue/test failure + repository
  steps:
    1. file localization
    2. skeleton-based element localization
    3. edit-location localization
    4. Search/Replace patch generation
    5. reproduction/regression test validation
  no PDB
  no debugger state
```

Recommended debugger-enhanced variant:

```text
pdb_agent_variant:
  same as baseline, plus:
    - when localization is uncertain or patch attempts fail,
      enter PDB
    - inspect stack/local variables/source windows
    - use runtime state as extra evidence before patch generation
```

Ablation target:

```text
baseline without PDB
baseline + PDB always on
baseline + PDB after failed attempts
baseline + PDB only on uncertainty
```

## Claims verified

- Agentless uses a three-phase localization/repair/patch-validation pipeline.
- Agentless avoids autonomous LLM decision-making and complex tool use.
- Agentless performs hierarchical localization from files to elements to edit locations.
- Agentless uses Search/Replace patches rather than full-file rewrites.
- Agentless generates and filters reproduction tests.
- Agentless solves 96/300 SWE-bench Lite issues in the paper.
- Agentless reports $0.70 average cost.
- Agentless constructs SWE-bench Lite-S with 249 filtered problems.
- Agentless reports 194/500 on SWE-bench Verified.
- SWE-bench Lite has benchmark-quality issues: under-specified descriptions, exact patch leakage, misleading issue descriptions, and location clues.

## One-paragraph Turkish explanation for my own understanding

Agentless, karmaşık autonomous software engineering agent’larının gerçekten gerekli olup olmadığını sorgulayan güçlü bir baseline paper’dır. Sistem, LLM’e açık uçlu araç kullanımı veya gelecek aksiyonu seçme yetkisi vermek yerine sabit üç aşamalı bir pipeline kullanır: localization, repair ve patch validation. Localization tarafında önce şüpheli dosyaları, sonra class/function skeleton’larını, en son edit location’ları bulur. Repair tarafında tüm dosyayı yeniden yazmak yerine Search/Replace diff üretir. Validation tarafında ise regression testleri ve LLM-generated reproduction testleriyle candidate patch’leri filtreler. Bizim proje için ana ders şudur: PDB/debugger kullanan sistemimiz, bu kadar basit ve güçlü bir static/test-feedback baseline’a karşı anlamlı fayda göstermelidir; aksi halde ek agentic/debugger karmaşıklığı bilimsel olarak ikna edici olmaz.
