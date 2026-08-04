# Instructor Agentic Debugging TODO — Status Map v1

## 1. Purpose and snapshot identity

This document is a repository-grounded status mapping of the instructor's
original long-term Agentic Debugging TODO. The original list is authoritative
and remains byte-identical and unchanged at:

- `docs/INSTRUCTOR_AGENTIC_DEBUGGING_TODO.md` (27 items, all unchecked)

This map is a separate derived record. It does not edit the instructor
checklist, `TODO.md`, `README.md`, or `docs/PROJECT_TRACKER.md`.

- Snapshot branch: `docs/instructor-todo-status-map-v1`
- Snapshot base commit: `4087aa056eefa387f006ccaa98138c20ca14d4f0`
- Snapshot date: 2026-08-05
- Git state at writing: clean tracked working tree, no commits made.

Each item below carries:

1. the exact original Turkish wording (line wrapping normalized);
2. one primary status;
3. a current-state explanation;
4. concrete repository evidence;
5. missing work;
6. honest acceptance criteria for COMPLETED;
7. a delivery horizon (FRIDAY PRESENTATION / POST-FRIDAY NEAR TERM /
   LONG TERM).

A FRIDAY horizon means the item may appear in Friday scope as active work or
as an honest limitation; it does not mean the item is already complete.

## 2. Status legend and conservative acceptance rules

Statuses used (exactly one primary status per item):

| Status | Meaning |
|---|---|
| COMPLETED | Full wording of the instructor item is satisfied by repository or accepted experimental evidence, with the evidence-layer rules below. |
| PARTIAL | Material, accepted progress exists, but the full wording is not satisfied. |
| IN PROGRESS | The item is actively being worked; real execution has started or is ongoing, with documented evidence. |
| NOT STARTED | No material evidence of the item's work exists. |
| BLOCKED | No item is currently classified BLOCKED. |

Conservative acceptance rules applied throughout:

- COMPLETED requires evidence that the full wording of the instructor item
  has been satisfied. A prototype, smoke test, or single-fixture
  demonstration is not automatically a completed research item.
- Supervised fine-tuning is not COMPLETED merely because a one-step QLoRA
  smoke succeeded.
- Base-versus-tuned comparison is not COMPLETED before final training and
  the frozen held-out comparison.
- RAG, DPO/RLHF, GDB and LLDB work are not COMPLETED without implementation
  evidence.
- PDB-only implementation counts as PARTIAL for the item worded
  "PDB, GDB veya LLDB".
- A correct model diagnosis or patch proposal without independent verifier
  confirmation is not a successful repair.
- Implemented infrastructure is distinguished from experimentally validated
  model performance.
- No COMPLETED claim is made solely from ignored review packages
  (`_ai-review/`, `operator/`) or from external evidence that is not yet
  merged or durably tracked.
- Evidence is labeled by layer:
  1. **Layer 1 — tracked repository evidence** (source, tests, docs,
     manifests, accepted commits on `main` or the unmerged experiment
     branch).
  2. **Layer 2 — FirstMate-reviewed external experimental evidence not yet
     merged or durably tracked on main** (e.g., the real CUDA QLoRA
     checkpoint and the real CommitPackFT materialization).
  Layer 2 currently supports only IN PROGRESS claims, never COMPLETED.

## 3. Summary table

| № | Status | Item (abridged) | Horizon |
|---|---|---|---|
| 1 | PARTIAL | Debugging / automated debugging / fault localization / program repair literature review | FRIDAY PRESENTATION |
| 2 | PARTIAL | LLM-based debugging studies | FRIDAY PRESENTATION |
| 3 | PARTIAL | Agentic debugging, tool-using agents, multi-agent debugging | FRIDAY PRESENTATION |
| 4 | PARTIAL | Compare traditional / LLM-based / agentic debugging | FRIDAY PRESENTATION |
| 5 | COMPLETED | Study SWE-Agent, OpenHands, AutoCodeRover, Agentless, ChatDBG | FRIDAY PRESENTATION |
| 6 | COMPLETED | Research debugging/bug-fix datasets on HF and open platforms | FRIDAY PRESENTATION |
| 7 | COMPLETED | Compare SWE-bench family, BugsInPy, Defects4J, QuixBugs | FRIDAY PRESENTATION |
| 8 | PARTIAL | Select datasets for fine-tuning, RAG, evaluation | FRIDAY PRESENTATION |
| 9 | IN PROGRESS | Analyze datasets, prepare train/test split | FRIDAY PRESENTATION |
| 10 | COMPLETED | Select open-source code model (branch-bound) | FRIDAY PRESENTATION |
| 11 | IN PROGRESS | Convert dataset to instruction-response format | FRIDAY PRESENTATION |
| 12 | IN PROGRESS | Supervised fine-tuning with LoRA or QLoRA | FRIDAY PRESENTATION |
| 13 | NOT STARTED | Compare pre- and post-fine-tuning model | FRIDAY PRESENTATION |
| 14 | NOT STARTED | Build RAG system | LONG TERM |
| 15 | NOT STARTED | Combine fine-tuned model with RAG | LONG TERM |
| 16 | COMPLETED | File-read / code-search / test-run / patch-apply tools | FRIDAY PRESENTATION |
| 17 | COMPLETED | Create the debugging agent | FRIDAY PRESENTATION |
| 18 | PARTIAL | Model localizes faults, root cause, generates patches | FRIDAY PRESENTATION |
| 19 | NOT STARTED | Create preference dataset | LONG TERM |
| 20 | NOT STARTED | Apply DPO or RLHF | LONG TERM |
| 21 | NOT STARTED | Compare base / fine-tuned / RAG / agentic | LONG TERM |
| 22 | PARTIAL | Develop debugger adapter (PDB done; GDB/LLDB missing) | FRIDAY (PDB) / LONG TERM (GDB/LLDB) |
| 23 | NOT STARTED | Fine-tuned model generates debugger commands | POST-FRIDAY NEAR TERM |
| 24 | PARTIAL | Breakpoint / variable / stack / step debugging | FRIDAY PRESENTATION |
| 25 | PARTIAL | Patch generation + test validation after debugger | FRIDAY PRESENTATION |
| 26 | PARTIAL | Evaluate by success rate, localization, test pass, cost, runtime | FRIDAY PRESENTATION |
| 27 | COMPLETED | Working demo and technical report | FRIDAY PRESENTATION |

Totals: COMPLETED 7 — PARTIAL 10 — IN PROGRESS 3 — NOT STARTED 7 — BLOCKED 0 — TOTAL 27.

## 4. Per-item sections

### Item 1 — PARTIAL — FRIDAY PRESENTATION

**Exact wording:** "Debugging, automated debugging, fault localization ve program repair konularında literatür taraması yap."

**Current state:** A substantial literature-review foundation exists: initial concept notes, three independently generated AI research reports, a cross-report synthesis, a source-consensus matrix, and a claims-to-verify list. Tier 1 papers (ChatDBG, debug-gym, Agentless, SWE-bench) are archived and read; several Tier 2 papers are read with manual notes. The top-level Phase 1 tracker item remains open, with a minority of reading/verification subtasks still unchecked.

**Evidence (Layer 1):**
- `research/literature_notes_01.md` (core concepts)
- `research/reports/raw/` (Gemini / ChatGPT / Claude reports)
- `research/reports/synthesis/phase1_cross_report_synthesis_v1.md`
- `research/reports/synthesis/source_consensus_matrix_v1.md`
- `research/reports/synthesis/claims_to_verify_v1.md` (open items remain)
- `research/papers/tier1_must_read/`, `research/papers/tier2_core_sections/`
- `research/notes/2023_swe_bench_notes.md`, `2024_agentless_notes.md`,
  `2024_chatdbg_notes.md`, `2025_debug_gym_notes.md`
- `docs/PROJECT_TRACKER.md` Phase 1.1 subtasks (1.1.1–1.1.9 checked;
  top-level 1.1 unchecked)

**Missing work:** Closing the claims-to-verify list against primary sources;
finishing remaining Tier 2/3 reading; a consolidated reviewed literature
survey document; tracker Phase 1.1 closure.

**Acceptance criteria for COMPLETED:** A reviewed, evidence-backed literature
survey covering debugging, automated debugging, fault localization, and
program repair is tracked, with the Phase 1.1 tracker item closed and no
open verification claims.

### Item 2 — PARTIAL — FRIDAY PRESENTATION

**Exact wording:** "LLM-based debugging çalışmalarını incele."

**Current state:** LLM-based debugging work is studied in depth for ChatDBG,
debug-gym, LDB, and SWE-bench, with manual notes. Additional named studies
(Self-Debugging, DebugBench) remain unread, and the tracker subtasks 1.2.2 /
1.2.3 are still open.

**Evidence (Layer 1):**
- `research/notes/2024_chatdbg_notes.md`, `2025_debug_gym_notes.md`,
  `2024_ldb_notes.md`, `2023_swe_bench_notes.md`
- `research/synthesis/pdb_debugger_agent_mvp_rationale.md` (Tier 1 reading)
- `docs/PROJECT_TRACKER.md` Phase 1.2 (1.2.0, 1.2.1 checked; 1.2.2–1.2.4 open)

**Missing work:** Read Self-Debugging and DebugBench; write the summary of
how LLM debugging differs from static code repair; tracker Phase 1.2 closure.

**Acceptance criteria for COMPLETED:** Named LLM-debugging studies are read
with notes, and a reviewed synthesis of LLM-based debugging is tracked.

### Item 3 — PARTIAL — FRIDAY PRESENTATION

**Exact wording:** "Agentic debugging, tool-using agents ve multi-agent debugging çalışmalarını incele."

**Current state:** Tool-using agent work is studied (RepairAgent, SWE-Agent,
OpenHands, AutoCodeRover) with manual notes; debug-gym is verified. Frontier
multi-agent / tool-using studies (Debug2Fix, FramePilot/ADI, EnIGMA,
SWE-Doctor) remain unverified, and the tracker subtasks 1.3.3–1.3.7 are open.

**Evidence (Layer 1):**
- `research/notes/2024_repairagent_notes.md`, `2024_swe_agent_notes.md`,
  `2024_openhands_notes.md`, `2024_autocoderover_notes.md`
- `research/reports/synthesis/phase1_cross_report_synthesis_v1.md`
- `docs/PROJECT_TRACKER.md` Phase 1.3 (1.3.1, 1.3.2 checked)

**Missing work:** Verification of the frontier systems; a multi-agent
debugging review; tracker Phase 1.3 closure.

**Acceptance criteria for COMPLETED:** Agentic/tool-using/multi-agent
debugging literature is reviewed with notes, including the named frontier
systems.

### Item 4 — PARTIAL — FRIDAY PRESENTATION

**Exact wording:** "Geleneksel debugging, LLM-based debugging ve agentic debugging yaklaşımlarını karşılaştır."

**Current state:** Comparison work exists as synthesis documents and the
system capability matrix (traditional/LLM/agentic distinctions, static vs
dynamic debugging, fault localization vs root-cause analysis, repository
agents vs debugger agents, APR plausibility vs correctness). The comparison
is embedded in research syntheses rather than a single consolidated reviewed
deliverable, and the top-level tracker item remains open.

**Evidence (Layer 1):**
- `research/synthesis/pdb_debugger_agent_mvp_rationale.md` (§9 System
  Capability Matrix v1)
- `research/reports/synthesis/phase1_cross_report_synthesis_v1.md`
- `docs/FINAL_TECHNICAL_REPORT_V1.md` §1
- `docs/PROJECT_TRACKER.md` Phase 1.4 (1.4.1–1.4.4 checked)

**Missing work:** A consolidated, reviewed comparison deliverable and
tracker Phase 1.4 closure.

**Acceptance criteria for COMPLETED:** A tracked comparison document covers
traditional vs LLM-based vs agentic debugging with evidence-backed
conclusions.

### Item 5 — COMPLETED — FRIDAY PRESENTATION

**Exact wording:** "SWE-Agent, OpenHands, AutoCodeRover, Agentless ve ChatDBG gibi sistemleri incele."

**Current state:** All five named systems are studied with dedicated manual
notes, and the system capability matrix v1 compares them (including
SWE-bench and debug-gym rows). Tracker subtasks 1.5.1–1.5.6 are checked.

**Evidence (Layer 1):**
- `research/notes/2024_swe_agent_notes.md`, `2024_openhands_notes.md`,
  `2024_autocoderover_notes.md`, `2024_agentless_notes.md`,
  `2024_chatdbg_notes.md`
- `research/synthesis/pdb_debugger_agent_mvp_rationale.md` §9
- `docs/PROJECT_TRACKER.md` Phase 1.5

**Missing work:** None for the instructor item's wording; frontier-system
verification belongs to item 3.

**Acceptance criteria for COMPLETED:** All five systems studied and compared;
satisfied (see evidence above).

### Item 6 — COMPLETED — FRIDAY PRESENTATION

**Exact wording:** "Hugging Face ve açık kaynak platformlarda debugging ve bug-fix veri setlerini araştır."

**Current state:** Dataset inventory research over Hugging Face and
open-source platforms is completed and recorded in Dataset and Evaluation
Decision v1 (BugsInPy, QuixBugs, SWE-bench variants, Defects4J, curated
fixtures). Tracker subtask 2.1.1 is checked; `TODO.md` marks this item `[x]`.

**Evidence (Layer 1):**
- `docs/DATASET_EVALUATION_DECISION_V1.md` §2 (dataset inventory)
- `docs/PROJECT_TRACKER.md` Phase 2.1
- `research/bugsinpy/`, `research/quixbugs/` manifests

**Missing work:** None for the research wording.

**Acceptance criteria for COMPLETED:** Dataset inventory over HF/open-source
platforms recorded; satisfied.

### Item 7 — COMPLETED — FRIDAY PRESENTATION

**Exact wording:** "SWE-bench, SWE-bench Lite, SWE-bench Verified, BugsInPy, Defects4J ve QuixBugs veri setlerini karşılaştır."

**Current state:** All six named datasets are compared in Dataset and
Evaluation Decision v1 across language/PDB fit, realism, oracle quality,
localization suitability, environment cost, and licensing. Tracker subtasks
2.2.1–2.2.4 are checked; `TODO.md` marks this item `[x]`.

**Evidence (Layer 1):**
- `docs/DATASET_EVALUATION_DECISION_V1.md` §2–§3
- `docs/PROJECT_TRACKER.md` Phase 2.2
- `research/bugsinpy/BUGSINPY_LICENSE_GATE_V1.json`

**Missing work:** None for the comparison wording.

**Acceptance criteria for COMPLETED:** Six-dataset comparison recorded;
satisfied.

### Item 8 — PARTIAL — FRIDAY PRESENTATION

**Exact wording:** "Fine-tuning, RAG ve değerlendirme için uygun veri setlerini seç."

**Current state:** Evaluation datasets and fine-tuning corpora are selected:
BugsInPy primary / QuixBugs fallback / curated smoke gate for evaluation, and
the CommitPackFT Python corpus plus five curated held-out tasks for
fine-tuning. However, a final RAG dataset/corpus has not been selected and
accepted; the recorded RAG NO-GO / defer decisions do not satisfy the full
wording of the item, which names RAG explicitly.

**Evidence (Layer 1):**
- `docs/DATASET_EVALUATION_DECISION_V1.md` (evaluation selection;
  RAG NO-GO-FOR-NOW)
- `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md` (RAG NO-GO, SFT DEFER)
- `experiments/qlora_patch_pilot_v1/freeze_record.json` (CommitPackFT
  config `python`, pinned revision; five curated held-out tasks) on branch
  `experiment/qlora-patch-pilot-v1` commit `461e1ca`

**Missing work:** Explicit selection and acceptance of a RAG corpus (or an
accepted documented decision that names the RAG corpus requirement and its
trigger), and an explicit acceptance record for the fine-tuning corpus once
the human audit completes.

**Acceptance criteria for COMPLETED:** A tracked selection covers a
fine-tuning corpus, a RAG corpus (or an accepted equivalent decision with
trigger), and an evaluation dataset, each with acceptance evidence.

### Item 9 — IN PROGRESS — FRIDAY PRESENTATION

**Exact wording:** "Veri setlerini analiz et ve eğitim/test ayrımını hazırla."

**Current state:** Dataset analysis and the deterministic train/validation
split design are complete, and the real CommitPackFT minimum-tier corpus has
been materialized externally: 56,025 input candidates → 1,000 training rows /
150 validation rows, zero held-out exact/near matches, zero repository
overlap. The required human manual audit is still in progress; the corpus is
not "not yet built."

**Evidence:**
- Layer 1: `experiments/qlora_patch_pilot_v1/transformation_config.json`
  (deterministic filtering, SimHash near-dedup, held-out checks,
  repository-disjoint split; preferred 1500/200, minimum 1000/150);
  `experiments/qlora_patch_pilot_v1/freeze_record.json`; unit tests
  `tests/unit/test_qlora_patch_pilot.py` (branch `experiment/qlora-patch-pilot-v1`,
  commit `461e1ca`).
- Layer 2 (FirstMate-reviewed external, not yet merged): real CommitPackFT
  materialization executed — 56,025 input candidates, 1,000 train rows,
  150 validation rows, zero held-out matches, zero repository overlap;
  automated 50 accepted + 25 rejected audit packets completed; human audit
  in progress.

**Missing work:** Completion of the human manual audit (≥50 accepted and
≥25 rejected reviewed rows with the fail-closed audit validator); final
corpus acceptance; full analysis write-up.

**Acceptance criteria for COMPLETED:** The minimum-tier real corpus is built
from the frozen revision with verified counts and disjointness, the human
audit is complete and validated, and the corpus is accepted for training.

### Item 10 — COMPLETED (branch-bound qualifier) — FRIDAY PRESENTATION

**Exact wording:** "Seçilen açık kaynak kod modelini belirle."

**Current state:** The open-source code model is selected and frozen:
`Qwen/Qwen2.5-Coder-7B-Instruct`, revision
`c03e6d358207e414f1eca0bb1891e29f1db0e242` (Apache-2.0). The durable
implementation currently exists on the unmerged experiment branch
`experiment/qlora-patch-pilot-v1` at commit
`461e1ca5ec129db326f6e56169e15a9f16f6e58c`. The branch is not yet merged
into `main`; this does not invalidate the model-selection decision itself.

**Evidence (Layer 1):**
- `experiments/qlora_patch_pilot_v1/freeze_record.json` (`model.repository`,
  `model.revision`, `model.license`) on branch
  `experiment/qlora-patch-pilot-v1` commit `461e1ca`
- `experiments/qlora_patch_pilot_v1/README.md` (frozen model condition)

**Missing work:** Merging the experiment branch into `main` (delivery
action, not a selection action); no re-selection is needed.

**Acceptance criteria for COMPLETED:** A model identity with revision and
license is pinned and recorded; satisfied (branch-bound).

### Item 11 — IN PROGRESS — FRIDAY PRESENTATION

**Exact wording:** "Veri seti modele uygun değilse instruction-response formatına dönüştür."

**Current state:** The instruction-response transformation (buggy source +
task text + failure output → unified-diff completion) is implemented,
frozen, and executed on the real CommitPackFT materialization. Its
research-quality acceptance remains dependent on the incomplete human audit.

**Evidence:**
- Layer 1: `experiments/qlora_patch_pilot_v1/prompt_contract.json` and
  `transformation_config.json`; `agentic_debugger/training/patch_pilot.py`;
  `scripts/qlora_patch_pilot.py` (branch `experiment/qlora-patch-pilot-v1`
  commit `461e1ca`).
- Layer 2 (FirstMate-reviewed external, not yet merged): transformation
  executed on the real CommitPackFT materialization (item 9 counts).

**Missing work:** Human audit completion; final transformation acceptance.

**Acceptance criteria for COMPLETED:** The accepted corpus in
instruction-response form is audited and validated against the frozen prompt
and transformation contracts.

### Item 12 — IN PROGRESS — FRIDAY PRESENTATION

**Exact wording:** "LoRA veya QLoRA ile supervised fine-tuning yap."

**Current state:** QLoRA supervised fine-tuning is implemented and frozen,
and a real one-step CUDA QLoRA weight update succeeded, with adapter
save/reload succeeding. Final training has not been run; the freeze gate
(`final_training_authorized: false`) still blocks it; the experiment branch
is not merged into `main`. This is NOT a completed fine-tuning item.

**Evidence:**
- Layer 1: `experiments/qlora_patch_pilot_v1/training_config.json`
  (LoRA r=16, alpha=32, 4-bit nf4 double-quant, completion-only loss,
  one epoch); `freeze_record.json` (`scientific_gate`); `SMOKE_EVIDENCE.md`
  (final training NOT RUN); `colab/agentic_debugging_qlora_pilot.ipynb`
  (one-step weight-update smoke cells; hard gate before final training);
  `agentic_debugger/training/patch_pilot.py` (branch
  `experiment/qlora-patch-pilot-v1` commit `461e1ca`).
- Layer 2 (FirstMate-reviewed external, not yet merged): real one-step CUDA
  QLoRA update succeeded; adapter save/reload succeeded; final training not
  run; held-out generation not run; base-versus-tuned comparison not run.

**Missing work:** Final training run on the frozen corpus/config; saved
adapter artifacts with training logs; FirstMate gate approval; branch merge.

**Acceptance criteria for COMPLETED:** Full QLoRA SFT run on the frozen
corpus with the frozen configuration, saved adapter, training record, and
validation against the accepted gate — not a one-step smoke.

### Item 13 — NOT STARTED — FRIDAY PRESENTATION

**Exact wording:** "Fine-tuning öncesi ve sonrası modeli karşılaştır."

**Current state:** No base-versus-tuned comparison exists. The freeze gate
blocks held-out generation (`held_out_generation_authorized: false`), final
training has not run, and no base or tuned outputs exist for comparison.

**Evidence (Layer 1):**
- `experiments/qlora_patch_pilot_v1/freeze_record.json` (`scientific_gate`)
- `experiments/qlora_patch_pilot_v1/generation_config.json` (frozen
  one-candidate decoding contract, unused)
- `experiments/qlora_patch_pilot_v1/SMOKE_EVIDENCE.md` (held-out generation
  NOT RUN)

**Missing work:** Final training (item 12); frozen held-out generation for
base and tuned models with the same prompt contract; verifier runs on
generated patches; metrics and analysis.

**Acceptance criteria for COMPLETED:** Base and tuned models are run on the
five frozen held-out tasks with identical prompt/generation contracts, and
the independent verifier is applied to all generated patches.

### Item 14 — NOT STARTED — LONG TERM

**Exact wording:** "Repository kodları, testler, issue açıklamaları ve hata mesajları için RAG sistemi kur."

**Current state:** No RAG system is implemented. Repository RAG is recorded
as NO-GO-FOR-NOW / DEFER in both decision documents; the existing
deterministic file-read and code-search tools are explicitly labeled as not
RAG.

**Evidence (Layer 1):**
- `docs/DATASET_EVALUATION_DECISION_V1.md` §10 (RAG NO-GO-FOR-NOW)
- `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md` §2 (RAG NO-GO / DEFER)
- `agentic_debugger/skills/file_skills.py`, `search_skills.py` (deterministic
  tools, not retrieval)

**Missing work:** RAG system over repository code, tests, issue
descriptions, and error messages; retrieval evaluation.

**Acceptance criteria for COMPLETED:** A working RAG system over the four
named content types, evaluated against a non-RAG baseline.

### Item 15 — NOT STARTED — LONG TERM

**Exact wording:** "Fine-tuned modeli RAG sistemiyle birleştir."

**Current state:** No fine-tuned model and no RAG system exist, so no
combination exists.

**Evidence (Layer 1):** Items 12 and 14 are not complete
(`docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md`).

**Missing work:** Depends on items 12 and 14; integration and evaluation.

**Acceptance criteria for COMPLETED:** The fine-tuned model operates over the
RAG system end-to-end with measured retrieval and repair outcomes.

### Item 16 — COMPLETED — FRIDAY PRESENTATION

**Exact wording:** "Modelin kullanacağı dosya okuma, kod arama, test çalıştırma ve patch uygulama araçlarını geliştir."

**Current state:** The four named tools are implemented as typed,
deterministic controller tools and demonstrated end-to-end in the Task 9
demo. Tracker subtasks 4.3.1–4.3.4 are checked; `TODO.md` marks this item
`[x]`.

**Evidence (Layer 1):**
- `agentic_debugger/skills/file_skills.py` (file read)
- `agentic_debugger/skills/search_skills.py` (code search)
- `agentic_debugger/runtime/test_runner.py` (test run)
- `agentic_debugger/runtime/patcher.py` (patch apply)
- `agentic_debugger/agent/tool_registry.py` (typed dispatch)
- `docs/PROJECT_TRACKER.md` Phase 4.3; Task 9 demo

**Missing work:** None for the tool-development wording.

**Acceptance criteria for COMPLETED:** File-read, code-search, test-run, and
patch-apply tools exist and are exercised; satisfied.

### Item 17 — COMPLETED — FRIDAY PRESENTATION

**Exact wording:** "Debugging agentini oluştur."

**Current state:** A single-controller debugging agent exists: controller
state machine, tool registry, typed directives, policies, and the Task 9
end-to-end demonstration (10 cases over 5 curated tasks and 2 policies).
Tracker subtask 4.4.1 is checked; `TODO.md` marks this item `[x]`.

**Evidence (Layer 1):**
- `agentic_debugger/agent/controller.py`, `state_machine.py`,
  `controller_policy.py`, `tool_registry.py`
- `docs/PROJECT_TRACKER.md` Phase 4.4 and Task 9 records
- `agentic_debugger/demo/` (offline end-to-end demonstration)

**Missing work:** None for the agent-creation wording (multi-agent
architecture is out of the accepted single-controller scope and belongs to
item 3's long-term breadth).

**Acceptance criteria for COMPLETED:** A debugging agent exists and
demonstrates the controller path; satisfied.

### Item 18 — PARTIAL — FRIDAY PRESENTATION

**Exact wording:** "Modelin hata konumunu bulmasını, root cause belirlemesini ve patch üretmesini sağla."

**Current state:** Localization, root-cause, and patch outputs are
implemented as controller outputs (tracker 4.5.1–4.5.3) and the deterministic
Task 9 demo achieves `CORRECT_TARGET_SYMBOL` localization 10/10 with the
scripted model. Live-model evidence shows correct diagnosis and patch intent
on one v4 case (`find_in_sorted`: correct root-cause hypothesis and correct
one-line fix submitted as a unified diff) but the patch was rejected by
strict hunk-header validation and zero verifier-confirmed repairs exist. A
correct diagnosis or patch proposal without verifier confirmation is not a
successful repair.

**Evidence (Layer 1):**
- `docs/PROJECT_TRACKER.md` Phase 4.5 subtasks (mechanism) and Task 9 records
- `agentic_debugger/evaluation/outcome_taxonomy.py`, `evaluation/verifier.py`
- v4 live-campaign evidence: attempt `3b5d7488…`, case 1 diagnosis/patch
  intent and hunk-header rejection (Layer 1 manifest:
  `research/quixbugs/PAIRED_PILOT_V4.json`; campaign record in ignored
  `_ai-review/quixbugs-v4-live-campaign/` — used for context only, not as
  sole evidence)

**Missing work:** A live-model verifier-confirmed repair; localization /
root-cause metrics over an external dataset.

**Acceptance criteria for COMPLETED:** At least one live-model case reaches
verifier-confirmed `RESOLVED` with recorded correct localization and
root-cause statements.

### Item 19 — NOT STARTED — LONG TERM

**Exact wording:** "Başarılı ve başarısız debugging çıktılarından preference veri seti oluştur."

**Current state:** No preference dataset exists; creation is explicitly
deferred until enough real/debugger trajectories exist.

**Evidence (Layer 1):**
- `docs/PROJECT_TRACKER.md` Phase 5.1 (defer recorded)
- `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md` (DPO NO-GO-FOR-NOW)

**Missing work:** Preference dataset from successful and failed debugging
outputs with reliable success/failure labels.

**Acceptance criteria for COMPLETED:** A reviewed preference dataset exists
with paired successful/failed trajectories and validated labels.

### Item 20 — NOT STARTED — LONG TERM

**Exact wording:** "DPO veya uygun bir RLHF yöntemi uygula."

**Current state:** No DPO/RLHF implementation or run exists; recorded as
NO-GO-FOR-NOW until a preference dataset and SFT baseline exist.

**Evidence (Layer 1):**
- `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md` (DPO NO-GO)
- `docs/DATASET_EVALUATION_DECISION_V1.md` §10

**Missing work:** Preference dataset (item 19), SFT baseline (item 12), DPO
or RLHF run and evaluation.

**Acceptance criteria for COMPLETED:** DPO or an appropriate RLHF method is
applied on the accepted preference dataset with an SFT baseline and
evaluation.

### Item 21 — NOT STARTED — LONG TERM

**Exact wording:** "Base model, fine-tuned model, RAG destekli model ve agentic sistemi karşılaştır."

**Current state:** No four-way comparison exists; it depends on the
fine-tuned model (item 12), RAG (item 14), and their combination (item 15).

**Evidence (Layer 1):**
- `docs/PROJECT_TRACKER.md` Phase 5.3 (comparison protocol deferred)

**Missing work:** Fine-tuned model, RAG model, agentic system, and a common
comparison protocol with the same prompt contract, verifier, and metrics.

**Acceptance criteria for COMPLETED:** All four conditions are run under a
common contract and compared with the independent verifier.

### Item 22 — PARTIAL — FRIDAY (PDB) / LONG TERM (GDB/LLDB)

**Exact wording:** "PDB, GDB veya LLDB için bir debugger adapter geliştir."

**Current state:** A PDB adapter is complete and accepted (session lifecycle,
breakpoints, execution control, stack/frame/locals inspection, safe
evaluation). GDB and LLDB adapters are not implemented. Because the item
names three debuggers and only PDB exists, the item is PARTIAL (an
alternative "or" reading is noted but not used for a COMPLETED claim).

**Evidence (Layer 1):**
- `agentic_debugger/runtime/pdb_session.py`, `pdb_worker.py`,
  `pdb_protocol.py`
- `agentic_debugger/quixbugs/contained_pdb.py`
- `docs/PROJECT_TRACKER.md` Phase 6.1 and Tasks 4A–4D
  (commits `c8539a4`, `84fe9e2`, `9a921bd`, `24ecc7a`, `17a7ebb`)
- `TODO.md` Phase 6 note: "yalnızca PDB için; GDB/LLDB henüz geliştirilmedi"

**Missing work:** GDB and/or LLDB adapter, or explicit instructor
confirmation that the "veya" reading makes PDB-only sufficient.

**Acceptance criteria for COMPLETED:** PDB plus at least one of GDB/LLDB is
implemented, or the owner confirms PDB-only satisfies the wording.

### Item 23 — NOT STARTED — POST-FRIDAY NEAR TERM

**Exact wording:** "Fine-tuned modelin debugger komutları üretmesini ve çıktıları yorumlamasını sağla."

**Current state:** The repository has typed debugger directives and debugger
output serialization for the base/controller protocol, but the item
explicitly requires the fine-tuned model to generate debugger commands and
interpret their outputs. Final fine-tuning has not occurred, so the
base/controller protocol mechanism is not used as evidence that this item
has started.

**Evidence (Layer 1):**
- `agentic_debugger/runtime/pdb_protocol.py`, `agent/controller_policy.py`
  (typed PDB directives)
- `docs/PROJECT_TRACKER.md` Phase 6.2 (serialization subtask 6.2.1 checked;
  top-level item open)
- `TODO.md` Phase 6 note: fine-tuning not started

**Missing work:** Final fine-tuning (item 12); fine-tuned model generating
protocol-valid debugger directives; interpretation of debugger outputs in a
validated trajectory.

**Acceptance criteria for COMPLETED:** The fine-tuned model produces valid
debugger commands and correctly interprets observed outputs in an accepted
trajectory.

### Item 24 — PARTIAL — FRIDAY PRESENTATION

**Exact wording:** "Modelin breakpoint koymasını, değişkenleri incelemesini, stack trace okumasını ve adım adım debug yapmasını sağla."

**Current state:** The PDB mechanism supports breakpoints, variable
inspection, stack inspection, and step execution, and scripted deterministic
trajectories demonstrate the controller path (Task 9: 21 PDB observations
under the pdb-on-uncertainty policy; golden trajectory
`pdb-gated-successful-repair.json`). However, the real live-model campaigns
produced zero PDB observations, and no external live model has demonstrated
the full breakpoint / variable / stack / step-by-step sequence. Not COMPLETED.

**Evidence (Layer 1):**
- `agentic_debugger/runtime/pdb_session.py` (Tasks 4B–4D)
- `docs/PROJECT_TRACKER.md` Phase 6.3 (6.3.1–6.3.4 checked) and Task 9
  records (21 PDB observations, scripted model)
- `tests/golden_trajectories/data/pdb-gated-successful-repair.json`
- Live campaigns: zero PDB observations (v3 `fddf1e39…`, v4 `3b5d7488…`),
  historical Zen matrix 0/2 (`docs/PROJECT_TRACKER.md` historical log)

**Missing work:** A live-model PDB session recording breakpoint, variable,
stack, and step observations in an accepted campaign.

**Acceptance criteria for COMPLETED:** A live external model opens a PDB
session and completes the breakpoint / variable / stack / step sequence with
recorded observations.

### Item 25 — PARTIAL — FRIDAY PRESENTATION

**Exact wording:** "Modelin debugger etkileşiminden sonra patch üretmesini ve testlerle doğrulamasını sağla."

**Current state:** The architecture supports debugger interaction followed
by patch generation and independent verifier validation, and scripted
fixtures demonstrate the mechanism (Task 9: verifier `RESOLVED` 10/10, F2P
10/10, P2P 22/22). However, no live external model has produced a
verifier-confirmed repair after PDB interaction: the v4 live campaign
produced zero PDB observations and zero verifier runs. Not COMPLETED.

**Evidence (Layer 1):**
- `agentic_debugger/runtime/patcher.py`, `evaluation/verifier.py`
- `docs/PROJECT_TRACKER.md` Phase 6.4 (6.4.1, 6.4.2 checked) and Task 9
  records
- v4 live campaign: zero PDB observations, zero verifier runs (Layer 1
  manifest `research/quixbugs/PAIRED_PILOT_V4.json`; campaign record in
  ignored `_ai-review/` — context only)

**Missing work:** A live model patch after PDB interaction verified by the
independent verifier.

**Acceptance criteria for COMPLETED:** A live-model case reaches
verifier-confirmed `RESOLVED` with F2P/P2P evidence after recorded PDB
interaction.

### Item 26 — PARTIAL — FRIDAY PRESENTATION

**Exact wording:** "Sonuçları başarı oranı, localization accuracy, test pass rate, maliyet ve çalışma süresi açısından değerlendir."

**Current state:** All five metric families are defined in the evaluation
contracts (localization outcome, patch correctness via verifier F2P/P2P,
provider-reported token/cost and timing, debugger-action counts), and live
attempts produced accounting evidence (e.g., v3 `fddf1e39…`: 12 logical
calls, 13 attempts, provider-reported cost 0.010565556, 33,685 public
evidence bytes; v4 `3b5d7488…`: case 1 $0.007378, case 2 $0.012323, zero
verifier runs). No completed external-dataset model evaluation exists to
report against these metrics; no cross-dataset model result.

**Evidence (Layer 1):**
- `docs/PROJECT_TRACKER.md` Phase 7.1 (7.1.1–7.1.5 checked)
- `agentic_debugger/evaluation/live.py`, `outcome_taxonomy.py`, `verifier.py`
- Live attempt accounting: `TODO.md` §2026-08-04 entries; v4 campaign record
  (ignored `_ai-review/` — context only)
- `docs/FINAL_TECHNICAL_REPORT_V1.md` (metrics and limitations)

**Missing work:** A completed accepted campaign with verifier-authoritative
results against all five metric families over an external dataset.

**Acceptance criteria for COMPLETED:** A results report covers success rate,
localization accuracy, test pass rate, cost, and runtime from a completed,
verifier-backed campaign.

### Item 27 — COMPLETED (scope boundary) — FRIDAY PRESENTATION

**Exact wording:** "Çalışan bir agentic debugging demosu ve teknik rapor hazırla."

**Current state:** A working infrastructure demo (Task 9 deterministic
end-to-end demonstration: 10 cases, verifier `COMPLETED/RESOLVED` 10/10, F2P
10/10, P2P 22/22, localization correct 10/10, workspaces cleaned) and a
technical report (`FINAL_TECHNICAL_REPORT_V1.md`) plus demo guide
(`DEMO_GUIDE_V1.md`) are complete and accepted (2026-07-31).
`TODO.md` marks this item `[x]`.

Scope boundary: this demo does NOT prove external-dataset repair success,
PDB effectiveness for the live model, base-versus-tuned superiority, or a
completed QuixBugs paired comparison.

**Evidence (Layer 1):**
- `docs/FINAL_TECHNICAL_REPORT_V1.md`, `docs/DEMO_GUIDE_V1.md`
- `agentic_debugger/demo/` (offline deterministic demo)
- `docs/PROJECT_TRACKER.md` Phase 7.2 and Task 9 records
- `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md`

**Missing work:** None for the instructor wording within its stated scope; a
Friday refresh of the report with live-model findings is presentation work,
not a completion requirement.

**Acceptance criteria for COMPLETED:** A working demo and technical report
exist and are accepted; satisfied with the explicit scope boundary above.

## 5. Known project boundaries

- **QLoRA experiment boundary.** The QLoRA implementation and freeze live on
  the unmerged branch `experiment/qlora-patch-pilot-v1` (commit
  `461e1ca5ec129db326f6e56169e15a9f16f6e58c`). The human manual audit is
  still in progress; final training is not complete; held-out
  base-versus-tuned evaluation is not complete; the branch is not merged
  into `main`. The real CUDA checkpoint currently supports only IN PROGRESS
  claims: real CommitPackFT minimum-tier corpus produced; automated 50+25
  audit completed but human audit not complete; real one-step CUDA QLoRA
  update succeeded; adapter save/reload succeeded; final training not run;
  held-out generation not run; base-versus-tuned comparison not run.
- **QuixBugs live-campaign boundary.** The v4 live campaign
  (attempt `3b5d7488…`, 2026-08-04) demonstrated real provider interaction
  and correct model-level diagnosis/patch intent on case 1
  (`find_in_sorted`), but produced zero verifier-confirmed repairs, zero PDB
  observations, and no valid paired policy comparison (campaign
  `ABORTED/BUDGET_EXCEEDED`; case 2 applied a correct patch but exhausted
  public evidence before a verifier run; cases 3–6 unstarted). Earlier
  attempts (`705aa047…` protocol-invalid, `81f2e5d8…` and `4c7fc444…`
  infrastructure-failed, `8890ed9…`/`320550…` non-pilot diagnostics,
  `fddf1e39…` v3 `BUDGET_EXCEEDED`) are not valid experiments. The
  historical OpenCode Zen matrix is descriptive-only (static 2/2, PDB 0/2)
  and is not current-route evidence.
- **BugsInPy boundary.** BugsInPy source acquisition and execution remain
  license-gated (`docs/BUGSINPY_LICENSE_GATE_V1.md`); metadata/preflight
  work may continue, execution may not.
- **RAG / SFT / DPO decisions.** RAG NO-GO-FOR-NOW, SFT DEFER, DPO
  NO-GO-FOR-NOW are recorded decisions, not completions (see item 8).
- **Evidence-layer rule.** Layer 2 (FirstMate-reviewed external evidence not
  yet merged or durably tracked on main) supports IN PROGRESS claims only.
  No COMPLETED claim in this map rests solely on ignored `_ai-review/` or
  `operator/` artifacts.

## 6. Friday / post-Friday / long-term split

### FRIDAY PRESENTATION (active work + honest limitations; completion not implied)

- Items 1–4: reviewed literature and honest gaps.
- Items 5–7: completed dataset/system research.
- Item 8: selections reported with the explicit RAG selection gap.
- Item 9: real corpus produced; human audit in progress.
- Item 10: frozen model selection (branch-bound).
- Item 11: transformation executed; acceptance pending audit.
- Item 12: real QLoRA training as the active Friday target.
- Item 13: frozen held-out base-versus-tuned comparison as the active
  Friday target.
- Items 16–17: working prototype and demonstrable architecture.
- Item 18: honest live findings (diagnosis/patch intent, zero
  verifier-confirmed repairs).
- Item 22 (PDB portion): PDB adapter as part of the prototype.
- Items 24–25: mechanism demonstrated; zero live PDB observations reported
  honestly.
- Item 26: metrics and limitations from live attempts.
- Item 27: demo and technical report refresh.

### POST-FRIDAY NEAR TERM

- Item 9: human audit completion and corpus acceptance.
- Item 11: audit-gated transformation acceptance.
- Item 12: final training completion, gate approval, branch merge.
- Item 13: comparison completion and analysis.
- Item 23: fine-tuned model debugger-command generation and interpretation.
- Item 26: completion via a full authorized six-case campaign with
  verifier-authoritative results.
- BugsInPy execution if the license gate is cleared.

### LONG TERM

- Item 3: full multi-agent debugging studies.
- Items 14–15: RAG system and fine-tuned-model-plus-RAG integration.
- Items 19–21: preference dataset, DPO/RLHF, four-way comparison.
- Item 22: GDB and LLDB adapters.
- Defects4J integration (outside the current Python/PDB track).

## 7. Evidence index

- **Trackers:** `TODO.md`, `docs/PROJECT_TRACKER.md`,
  `CURRENT_AGENT_ROSTER.md`, `README.md`, `diary/diary.md`
- **Literature:** `research/literature_notes_01.md`,
  `research/reports/raw/`, `research/reports/synthesis/`,
  `research/papers/tier1_must_read/`, `research/papers/tier2_core_sections/`,
  `research/notes/`, `research/synthesis/`
- **Datasets:** `docs/DATASET_EVALUATION_DECISION_V1.md`,
  `docs/BUGSINPY_LICENSE_GATE_V1.md`, `research/bugsinpy/`,
  `research/quixbugs/*_MANIFEST_V1.json`,
  `research/quixbugs/EIGHT_TASK_PILOT_MANIFEST_V1.json`
- **QLoRA (unmerged branch `experiment/qlora-patch-pilot-v1`, commit
  `461e1ca`):** `experiments/qlora_patch_pilot_v1/` (freeze_record,
  training/generation/prompt/transformation configs, SMOKE_EVIDENCE,
  notebook), `agentic_debugger/training/patch_pilot.py`,
  `scripts/qlora_patch_pilot.py`, `tests/unit/test_qlora_patch_pilot.py`
- **Prototype:** `agentic_debugger/agent/`, `agentic_debugger/runtime/`,
  `agentic_debugger/skills/`, `agentic_debugger/evaluation/`,
  `agentic_debugger/events/`, `agentic_debugger/demo/`,
  `agentic_debugger/datasets/curated/`
- **Live campaigns:** `research/quixbugs/PAIRED_PILOT_V1..V4.json`,
  `scripts/quixbugs_paired_pilot.py`, `scripts/quixbugs_live_runner_v2.py`,
  `scripts/quixbugs_opencode_go_adapter.py`,
  `scripts/opencode_protocol_transport.py`, `docs/QUIXBUGS_*.md`
- **Reports:** `docs/FINAL_TECHNICAL_REPORT_V1.md`,
  `docs/DEMO_GUIDE_V1.md`, `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md`
- **Key commits:** Task 4A–4D `c8539a4`/`84fe9e2`/`9a921bd`/`24ecc7a`/
  `17a7ebb`; Task 5 `43d00c8`; Task 6 `eedcccb`; Task 7 `1b0af78`;
  Task 8 `ab9b8b7`; Task 9 `e7031fa`; Task 10A `14a0287`; 10B-R1
  `2996f16`; 10B-R3 `1bb1d52`; 10B-R5 `63fa27c`; QuixBugs smoke `96526fc`;
  eight-task baseline `2236775`; paired-pilot v2 `28ec775`/`cda3d0a`;
  OpenCode Go adapter `618c33f`; v3 `603b391`; v4 `39abb2a`; QLoRA branch
  `461e1ca`.

## 8. Method and limitations note

- This map was produced by inspecting the live repository at snapshot base
  `4087aa0` (branch `docs/instructor-todo-status-map-v1`, clean working
  tree): source, tests, manifests, decision documents, tracker, diary, and
  the unmerged experiment branch content.
- All 27 item wordings are quoted from
  `docs/INSTRUCTOR_AGENTIC_DEBUGGING_TODO.md` in order; the original file is
  unchanged (verified by an empty diff against the snapshot base).
- Statuses apply the conservative rules in Section 2. Layer-2 evidence is
  used only for IN PROGRESS claims; no COMPLETED claim rests on ignored
  `_ai-review/` or `operator/` artifacts.
- This map is a point-in-time assessment; statuses will change as the audit,
  final training, held-out comparison, and live campaigns progress.
- No new web research, provider contact, training, or live campaign was
  performed to produce this map.
