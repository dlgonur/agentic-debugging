# Model, RAG, Fine-Tuning and DPO Decision Gate v1

**Decision date:** 2026-07-31
**Branch:** `feature/model-decision-final-report-v1`
**Baseline:** `2236775` (feat: add eight-task QuixBugs gold baseline)
**Scope:** documentation and decision-making only. No model, provider, OpenCode,
RAG, training, PDB, or paid API was run to produce this gate. No dataset was
downloaded or executed.

## How this gate was produced

This document is engineering judgment applied to live repository evidence
(source, tests, docs, manifests, diary, `docs/project-tracker.md`, and
`docs/datasets/selection.md`), not new experimentation. It
supersedes none of the accepted prior evidence; it re-reads that evidence in
light of the eight-task QuixBugs gold baseline (`2236775`) that did not exist
when Dataset and Evaluation Decision v1 was written, and adds the
model-access-strategy decision and the eight-task-sufficiency decision that
were not yet in scope on 2026-07-30. Where a claim rests on external published
work rather than this repository's own evidence, it is marked **[external]**
with its source; every other claim traces to a cited file, commit, or accepted
document in this repository. No new external research was judged materially
necessary for these six decisions: they are sequencing/readiness calls fully
grounded in already-recorded internal evidence (the same posture the accepted
Dataset and Evaluation Decision v1 took for its RAG/SFT/DPO lines).

## Executive decision table

| Decision | Verdict | One-line reason |
|---|---|---|
| Future model-access strategy | **PROCEED (narrow)** | Stay on the already-validated free-tier route one bounded step further (a single real-dataset live case) before any paid or multi-model expansion. |
| Repository RAG | **NO-GO-FOR-NOW** | No real-model baseline exists yet for retrieval to be compared against; unchanged from Dataset and Evaluation Decision v1. |
| Supervised fine-tuning (SFT) | **DEFER** | No trajectory corpus of adequate size or quality exists; unchanged from Dataset and Evaluation Decision v1. |
| DPO / preference training | **NO-GO-FOR-NOW** | No paired preference data and no SFT baseline; unchanged from Dataset and Evaluation Decision v1. |
| Eight QuixBugs tasks: sufficient for what? | **Infra validation: yes. Model selection, training, generalization: no.** | Every "patch" was the literal upstream diff; no model was in the loop. |
| Smallest credible next experiment | **One real-dataset, static-baseline, single-task live case** | Establishes whether the protocol-1.3 harness can carry a real model through a real (non-curated) task before any larger campaign. |

---

## 1. Future model-access strategy

**Verdict: PROCEED, narrowly.**

### What exists today

- Two independent model paths exist in this repository's history:
  1. An **offline scripted stand-in** (`agentic_debugger/demo/model.py`), used
     in the accepted Task 9 demonstration. It is not a model at all — it
     returns catalog-fixed outputs — so it cannot inform model-access
     strategy.
  2. A **real-model live harness**, Task 10A (`docs/evaluation/real-model-eval.md`)
     plus Task 10B-R1/R3/R5 contract repairs, culminating in wire protocol
     `1.3` at commit `63fa27cc4d30490b9770ead3ce14b4b6d3ddf222`. This harness
     is offline-by-default and requires dual explicit live-access
     authorization; it has never been executed from inside this repository's
     automated tests or CI — only via private operator tooling outside the
     tracked source (`docs/project-tracker.md`, "Current Focus" section).
- The only live evidence of route 2 actually talking to a model is the
  four-case OpenCode Zen descriptive matrix: provider `opencode`, model
  `deepseek-v4-flash-free`, variant `max`, fixture
  `curated-none-handling-001`. Static policy resolved 2/2 cases;
  PDB-on-uncertainty resolved 0/2, and **PDB opened in 0 of 2 PDB-enabled
  cases** (`docs/project-tracker.md`). That matrix predates protocol `1.3`
  (Task 10B-R5) and used a free, non-paid provider route.
- No real model has ever been evaluated against QuixBugs or BugsInPy. Every
  QuixBugs result to date, including the eight-task baseline, is a
  gold-patch (no-model) run (`docs/datasets/quixbugs/baseline-8-task.md`).

### Reasoning

Two open questions are more urgent than which model or provider to use next:

1. **Does the repaired protocol-1.3 contract actually let PDB open live?**
   The only live PDB-enabled evidence (protocol 1.2, pre-R5) shows 0/2
   opens, both terminating `invalid_model_response` before PDB. R5 fixed
   concrete contract gaps that plausibly caused this (`docs/project-tracker.md`,
   Task 10B-R4/R5 entries), but that fix has **never been observed live**.
2. **Does the harness work end-to-end against a real (non-curated) dataset?**
   Every live case to date used the curated fixture set. QuixBugs is licensed
   and infra-validated but has never received a live model request.

Committing to a broader model-access strategy (more tasks, more models, paid
providers) before answering these two questions would spend budget on
capability the harness has not yet demonstrated it can exercise. The
free-tier OpenCode Zen route already in use is adequate to answer both
questions at near-zero marginal cost, so there is no reason to introduce a
paid provider **[external: reserving frontier/paid model spend until a cheap
proxy validates the harness is a standard cost-control pattern in ML
evaluation pipelines, not a claim specific to this project]** before that.

### Decision

PROCEED, but narrowly: the next authorized live experiment (see Section 6)
should reuse the existing free-tier OpenCode Zen route and the existing
protocol-1.3 harness, targeted at exactly one QuixBugs task, static-baseline
policy first. Do **not** expand to paid providers, multiple models, or a
multi-task live campaign until that experiment's trigger conditions
(Section 7) are met. This decision authorizes nothing to run in the current
campaign — it only orders the next step.

---

## 2. Repository RAG

**Verdict: NO-GO-FOR-NOW for a research comparison; DEFER implementation
beyond the existing deterministic file/code-search tools already in the
controller (`agentic_debugger/skills/search_skills.py`,
`agentic_debugger/skills/file_skills.py`).**

This restates and reaffirms Dataset and Evaluation Decision v1 Section 10
unchanged. Nothing in the eight-task QuixBugs baseline or the diary since
2026-07-30 changes the underlying reasoning: retrieval would change the
information available to the agent before there is any non-RAG real-model
baseline to compare it against. The deterministic file-read and code-search
tools already implemented (Phase 4 of `TODO.md`, marked `[x]`) remain the
retrieval surface the controller uses; that is existing accepted
infrastructure, not new RAG.

**Trigger to revisit:** a real-model, non-RAG baseline exists (from Section 6
or a larger successor) with a stable resolved-rate on at least one dataset
outside the five curated fixtures, **and** the task distribution under
consideration is repository-scale (BugsInPy unblocked, or a SWE-bench-class
target), where retrieval has plausible marginal value over the existing
file/search tools. Revisiting for QuixBugs-scale single-file algorithm tasks
is not worthwhile — the whole buggy program already fits in one file the
controller can read directly.

---

## 3. Supervised fine-tuning (SFT)

**Verdict: DEFER.**

Restates Dataset and Evaluation Decision v1 Section 10 unchanged, now
cross-checked against everything produced since:

- The only model-generated trajectories in existence are the four live
  OpenCode Zen cases — 2 resolved, 2 terminated `invalid_model_response`
  before any patch. That is not a training corpus by any measure (volume,
  diversity, success/failure balance, or protocol currency — those four
  cases predate protocol 1.3).
- The Task 9 "golden trajectories" are scripted, not model-generated
  (`docs/demo/task-9.md` Section 4: "There is no model in the loop"); they
  cannot serve as SFT training examples of model behavior.
- The eight-task QuixBugs baseline contains **zero** model trajectories —
  every candidate patch is the literal upstream diff (`docs/datasets/quixbugs/baseline-8-task.md`).
- No instruction-response schema for debugger trajectories has been drafted
  (`TODO.md` Phase 3, all unchecked).

**Trigger to revisit:** a corpus of real-model trajectories — on the order of
tens to low hundreds, spanning both resolved and unresolved outcomes, on a
protocol version that is not superseded — exists and an instruction-response
schema has been drafted and reviewed against it.

---

## 4. DPO / preference optimization

**Verdict: NO-GO-FOR-NOW.**

Restates Dataset and Evaluation Decision v1 Section 10 unchanged. DPO
requires paired chosen/rejected trajectories for the same task under a
stable evaluator and, ordinarily, a measured SFT baseline to compare against
**[external: this is the standard DPO precondition described in the original
DPO formulation, Rafailov et al., 2023, "Direct Preference Optimization:
Your Language Model is Secretly a Reward Model", https://arxiv.org/abs/2305.18290]**.
None of that exists here: there is no SFT baseline (Section 3), and the only
paired-policy live evidence (the four-case matrix) is not a preference
dataset — it is two different policies on the same task with no chosen/
rejected labeling and a sample size of two per policy.

**Trigger to revisit:** an SFT baseline exists (Section 3's trigger met and
executed) and a preference dataset with reliable, reviewed success/failure
labels over paired trajectories exists at meaningful scale (same task, same
protocol version, both a preferred and a dispreferred trajectory).

---

## 5. Are eight QuixBugs tasks sufficient?

This is not a single yes/no — the manifest's own accepted documents already
draw the line, and this section only makes each sub-claim explicit and
traces it to evidence.

| Claim | Verdict | Evidence |
|---|---|---|
| Sufficient to validate dataset adapter, resource-limited sandbox, and verifier infrastructure | **Yes** | 8/8 tasks reached `COMPLETED`/`RESOLVED`; 49/49 collected nodes passed post-patch; every canonical fixture hash was unchanged; every workspace was `CLEANED` (`docs/datasets/quixbugs/baseline-8-task.md`). |
| Sufficient for model selection (choosing which model to use going forward) | **No** | Zero models were run. Every "candidate patch" is the literal upstream gold diff generated via `difflib`, not model output (`docs/datasets/quixbugs/baseline-8-task.md`, "Selected eight tasks"). A model-selection decision requires model output to compare, which does not exist here. |
| Sufficient for training (SFT or DPO) | **No** | Same reason: no model trajectories were produced. Eight gold-patch runs contribute nothing to a training corpus, which needs model attempts, not oracle diffs. |
| Sufficient for a generalization claim about repository-scale debugging | **No** | QuixBugs tasks are single-file, one-line-defect algorithm programs by design (`docs/datasets/selection.md` Section 2: "QuixBugs is intentionally small algorithm repair"). Eight such tasks from one dataset family cannot support a claim about real, multi-file repository debugging; that was BugsInPy's role, and BugsInPy remains license-blocked. |

The accepted verdict wording in `docs/datasets/quixbugs/baseline-8-task.md`
("this campaign validates dataset eligibility, gold patches, verifier
behavior, runtime stability, and evidence quality — it does not evaluate a
model or PDB") already states this. This decision gate's contribution is
making explicit that the same eight tasks also cannot retroactively justify
a training or generalization claim just because more of them were added
since the single-task `gcd` smoke.

**Trigger to expand the QuixBugs task count:** none identified. Expanding
QuixBugs coverage further (e.g., attempting the excluded `bitcount`,
`find_first_in_sorted`, or `get_factors` under a relaxed schema bound) would
still produce infra evidence only, at a cost that competes directly with
Section 6's higher-priority real-model experiment. Do not schedule a wider
QuixBugs no-model campaign; the fallback dataset's job (proving the adapter
and verifier work end-to-end on a real, licensed, external dataset) is
already done.

---

## 6. Smallest credible next experiment

**One real-dataset, single-task, static-baseline-only live case:**

- Dataset: QuixBugs `gcd` (already adapter-mapped and license-cleared;
  `research/quixbugs/GCD_SMOKE_MANIFEST_V1.json`).
- Harness: the existing Task 10A/10B-R5 live evaluation CLI, protocol `1.3`.
- Provider/model: the already-validated free-tier OpenCode Zen route
  (`deepseek-v4-flash-free` or a successor free-tier model on the same
  route), to avoid any paid-service spend.
- Policy: `static-baseline` only. Do not attempt `pdb-on-uncertainty` in the
  same experiment — it has never once opened PDB live and should not be
  combined with a first real-dataset attempt, or a PDB failure would be
  confounded with a dataset-integration failure.
- Repetitions: one. This is a feasibility probe, not a comparison; do not
  interpret a single pass or fail as a rate.

### Why this is the smallest useful step, not the eight-task baseline repeated

The eight-task baseline already proved the QuixBugs adapter, sandbox, and
verifier work end-to-end (Section 5). What it did not touch is the live
model-adapter code path: request construction, the model's ability to return
a legal directive against a *real* dataset's task context (as opposed to the
curated fixtures every live case has used so far), and patch submission
through `LiveModelAdapter`. This experiment isolates exactly that untested
seam with the cheapest possible dataset (a single-file, one-line defect) and
the cheapest possible provider route, changing only one variable
(curated fixture → QuixBugs task) from the already-accepted four-case
matrix.

### What this experiment would and would not establish

It would establish whether the harness can carry a real free-tier model
through one real external task to a verifier-graded outcome at all. It would
**not** establish general model quality, PDB effectiveness, dataset-level
success rates, or readiness for a larger campaign — those all require more
tasks, more repetitions, and (for PDB) a case that actually reaches
`RuntimeEvidence`.

This section is a recommendation for the next bounded task, not an
authorization. The current campaign runs no model, per its own constraints.

---

## 7. Trigger conditions summary

| Decision | Escalate when… |
|---|---|
| Model-access strategy | The Section 6 experiment resolves on the free tier **and** a follow-on PDB-enabled single-task case actually reaches `RuntimeEvidence` with recorded observations. Only then consider more tasks, more repetitions, or a paid provider. |
| RAG | A real-model, non-RAG baseline is stable on a repository-scale dataset (BugsInPy unblocked, or equivalent), and the existing file/search tools are shown to be an actual bottleneck. |
| SFT | A real-model trajectory corpus of tens-to-low-hundreds of cases, spanning resolved and unresolved outcomes on a current protocol version, exists and an instruction-response schema is drafted and reviewed. |
| DPO | An SFT baseline exists **and** a reviewed, paired preference dataset (chosen/rejected trajectories, same task, same protocol) exists at meaningful scale. |
| Eight-task QuixBugs sufficiency | Not applicable — no trigger is defined to expand the no-model QuixBugs campaign further; effort should go to Section 6 instead. |
| BugsInPy unblock (context for RAG/generalization) | The dataset root license and every selected project's license/notice terms are reviewed and recorded, **and** an OS/container-level containment boundary beyond the current trusted-local WSL/Bubblewrap+`prlimit` boundary is implemented and self-tested (`docs/datasets/bugsinpy/pilot-readiness.md`, "Checks required before any task execution"). |

## 8. What this gate does not do

It does not run a model, a provider, OpenCode, RAG, training, PDB, or a paid
API. It does not infer model quality from the eight-task gold baseline. It
does not reopen or rerun the accepted QuixBugs campaigns. It does not
authorize the Section 6 experiment to run in this session — it records that
experiment as the recommended next bounded task for a future, separately
authorized session, consistent with how Dataset and Evaluation Decision v1
recorded the BugsInPy adapter-preflight task on 2026-07-30 without running
it.
