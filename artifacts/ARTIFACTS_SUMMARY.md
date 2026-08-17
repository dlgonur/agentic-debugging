# Artifact Results Summary

This file summarizes the main findings preserved under `artifacts/`. It is a navigation aid, not a replacement for the original JSON/CSV/report artifacts.

## 1. `tuned_debugger_pilot_v1`

**Purpose:** Early live comparison of RAW Qwen2.5-Coder-7B vs the cp118 PEFT adapter across five curated debugging tasks under `static-baseline` and `pdb-on-uncertainty`.

**Result:** The pilot was **not scientifically evaluable**.

- RAW run: 10/10 cases ended in `PROVIDER_ERROR`.
- cp118 run: 9/10 `PROVIDER_ERROR`, 1/10 `CONTROLLER_FAILED`.
- RESOLVED: 0/10 for both runs.
- Successful PDB observations: 0.
- Independent verifier outcomes: none.
- cp118 produced repeated malformed/invalid directives (21 recorded malformed-directive rejections).

**Interpretation:** These artifacts document an early integration/protocol failure, not evidence that RAW or cp118 was better. They should not be used as a tuned-vs-base performance result.

---

## 2. `swe_rebench_v2_census`

**Purpose:** Determine whether `nebius/SWE-rebench-V2` could supply a sufficiently large, clean Python bug-fixing corpus.

**Dataset:** 32,079 total rows.

**Key findings:**

- Python rows: 7,243.
- Python bug-oriented rows: 4,171.
- Strict primary policy (`16K` metadata proxy): 865 tasks / 260 repos.
- The original `>=1,500` task gate therefore **failed** under the strict policy.
- Sensitivity analysis showed that allowing external URLs and extending the permissive-license allowlist produced **1,594 tasks / 347 repos**.
- Protected named/public evaluation-repository overlap in the final eligible pool: 0.

**Interpretation:** The strict policy was too restrictive for the desired corpus size, but a documented relaxed policy yielded a viable 1,594-task / 347-repository source pool.

---

## 3. `swe_rebench_v2_static_pilot`

**Purpose:** Test whether the adopted 1,594-task pool could be materialized reproducibly before training.

**Pilot:** 30 tasks / 18 repositories.

**Results:**

- Repository clone: 18/18.
- Base commit fetchable: 30/30.
- Static source materialization: 30/30.
- Gold solution patch apply: 30/30.
- Test patch apply: 30/30.
- Combined test -> solution patch application: 30/30.
- Fixed-tree semantics check: 30/30.

**Context-size result using the exact Qwen2.5-Coder-7B tokenizer:**

- 8K: 14/30 fit (46.7%).
- 16K: 21/30 fit (70.0%).
- 32K: 26/30 fit (86.7%).

**Interpretation:** Repository and patch materialization were fully viable, but full changed-file context did not reliably fit the desired sequence budgets. Context construction needed refinement before a training contract could be frozen.

---

## 4. `swe_rebench_v2_context_ablation`

**Purpose:** Reduce context size on the same 30-task pilot without changing the gold repair target.

| Context formulation | <=16K | <=32K |
|---|---:|---:|
| Full changed files | 21/30 (70.0%) | 26/30 (86.7%) |
| Release-note/history exclusions | 23/30 (76.7%) | 29/30 (96.7%) |
| Bounded hunk windows where needed | 30/30 (100%) | 30/30 (100%) |

Bounded windows used a +/-80-line radius on 7/30 tasks.

**Interpretation:** Compact oracle-localized context made all 30 pilot examples fit. This is a **context-feasibility result**, not evidence of repository-level localization ability, because the historical gold repair identified the relevant files/hunks.

---

## 5. `swe_rebench_v2_golden_verifier`

**Purpose:** Check whether the official SWE-rebench verifier/evaluation environment could reproduce the expected golden outcomes on the selected tasks.

**Results:**

- First 10 tasks: 9/10 `passed_match` (90%).
- Remaining 20 tasks: 19/20 (95%).
- Full 30-task aggregate: **28/30 (93.3%)**.
- Docker image pulls: 30/30.
- Evaluator-wrapper errors: 0.
- Of the two observed failures:
  - one was confirmed as a repeated deterministic environment failure;
  - one remained an unclassified verifier mismatch.

**Interpretation:** The official verifier path was broadly usable and reproducible, but not perfect. Raw failures were preserved rather than relabeled as passes.

---

## 6. `swe_rebench_v2_split`

**Purpose:** Freeze a repository-disjoint train/validation split from the adopted source pool.

**Source pool:** 1,594 tasks / 347 repos.

**Frozen split:**

- Train: **1,000 tasks / 307 repos**.
- Validation: **150 tasks / 40 repos**.
- Train-validation repository overlap: **0**.
- Protected-evaluation repository overlap: **0**.
- Deterministic seed: `20260808`.

Distribution drift was small:

- Core difficulty/age/patch-bin max drift: 0.151 pp train, 0.646 pp validation.
- Common bug-category max drift: 0.730 pp train, 1.363 pp validation.

**Interpretation:** The project obtained a deterministic, repository-disjoint 1,000/150 split with strong distribution preservation.

---

## 7. `swe_rebench_v2_corpus`

**Purpose:** Materialize the frozen split into authentic instruction/repair training examples and define the final training contract.

### B14 corpus construction

The initial B14 v2 build was blocked because four frozen training examples could not be materialized. B14 v3 repaired the materialization path without replacement or resampling:

- Train: **1,000/1,000** built.
- Validation: **150/150** built.
- Replacement/resampling: none.
- Context truncation: none.
- Oracle localization: gold repair **file paths only**; gold hunk windows were not used.
- Forbidden constructor fields used in input: no.
- Exact forbidden-field overlap-risk records: 0.
- Problem-statement pattern flags: 38.
- Intrinsic problem-to-target added-line overlap records: 126; retained as audit flags rather than silently removed.

Practical size measurements:

- Train raw sequence >16K: 166/1000.
- Train raw sequence >32K: 60/1000.
- Validation raw sequence >16K: 35/150.
- Validation raw sequence >32K: 15/150.

### B15 final training contract

- Canonical train corpus: 1,000.
- Canonical validation corpus: 150.
- <=32K train eligibility: **940/1000**.
- <=32K validation eligibility: **135/150**.
- C/D-clean canonical validation: 142/150.
- C/D-clean and <=32K validation: **128/150**.
- No truncation or redaction.
- Training target: exact stored gold repair patch (`PATCH\n<gold diff>`).
- No synthetic FILES/SYMBOLS/ROOT_CAUSE target was added.
- No `TRAINING_AUTHORIZATION.json` was created and **no training was run by this artifact stage**.

**Interpretation:** The SWE-rebench V2 preparation pipeline successfully progressed from census -> materialization -> context analysis -> verifier check -> repository-disjoint split -> authentic 1,000/150 corpus contract. The final corpus was viable, but sequence-length and leakage-clean masks were preserved explicitly rather than hiding unsuitable examples.

---

## Overall takeaway

These artifact families mainly document the construction and validation of the SWE-rebench V2 training/evaluation pipeline.

The strongest preserved conclusions are:

1. A viable source pool of **1,594 tasks / 347 repos** was obtained after a documented policy adjustment.
2. Static repository and patch materialization worked **30/30** on the pilot.
3. Context engineering could make the pilot fit **30/30 at 16K** when oracle-localized bounded windows were allowed.
4. The official golden verifier reproduced **28/30 (93.3%)** selected cases, with failures preserved honestly.
5. A repository-disjoint **1,000 train / 150 validation** split was frozen with zero repository overlap.
6. The authentic corpus was fully materialized, with **940 train / 135 validation** examples <=32K and explicit clean masks.
7. The separate early `tuned_debugger_pilot_v1` did **not** yield a valid tuned-vs-RAW performance comparison because provider/protocol failures prevented PDB use and verifier evaluation.
