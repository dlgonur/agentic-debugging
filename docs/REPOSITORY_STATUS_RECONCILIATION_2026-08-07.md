# Main-Repository Status Reconciliation — 2026-08-07

## Scope and authority

This reconciliation compares the instructor checklist, `TODO.md`,
`README.md`, and `docs/PROJECT_TRACKER.md` with the tracked history reachable
from `1e680b13eb25f19bd14fdeb6004e85bd9f8adb4d` on
`goal/friday-final-completion-codex-v1`. Source, tests, tracked artifacts, and
reachable commits are the evidence authority. Historical entries remain
historical; this document records the current interpretation.

The separate QLoRA repository/branch and external training artifacts are not
modified or reclassified here. Their recorded status remains external:
implementation accepted at `3f0d3e7`, later experimental evidence outside
this main-repository reconciliation, and held-out comparison not proven by
main-repository evidence.

No provider, live campaign, WSL benchmark, BugsInPy acquisition/execution,
training, or held-out generation was run for this reconciliation.

## History corrections

| Earlier tracker claim | Current evidence | Reconciled state |
|---|---|---|
| Post-mortem PDB was an unmerged candidate at 70 tests. | `f7ba129`, `62deca4`, `0b9c1a5`, and `e92634e` are all reachable from HEAD; the final tracked suite contains 107 unique focused tests. | Completed and integrated. |
| No repository RAG existed; RAG was only `NO-GO-FOR-NOW`. | `1e680b1` adds `agentic_debugger/rag/`, strict index/retrieval artifacts, bounded context injection, tests, and `docs/RAG_COMPARISON_DECISION_V2.md`. | RAG infrastructure item completed; model-performance claims remain open. |
| No preference dataset machinery or four-condition comparison existed. | `1e680b1` adds verifier-backed preference-pair export and a unified comparison harness, including deterministic demo-scale artifacts. | Both instructor items are partial: infrastructure exists, production/model evidence does not. |
| Literature items 1, 2, and 4 lacked consolidated deliverables. | `3c23b6e` adds the automated-debugging survey, LLM-debugging review, and approach comparison. The documents use reviewed tracked sources and exclude unresolved claims. | Items 1, 2, and 4 completed within the documented review scope. Frontier multi-agent breadth remains partial. |
| PDB-only meant the debugger-adapter item was partial. | The instructor wording uses “PDB, GDB veya LLDB” (or), and the accepted project is explicitly Python/PDB-first. The full PDB adapter and post-mortem entry are tracked and tested. | Item 22 completed for PDB; GDB/LLDB remain out of accepted scope, not missing completion work. |
| The Friday delivery bundle / V4 identity fix were pending candidates. | `ab464dd` and `fc7c85b` are reachable from HEAD. | Integrated historical milestones. |

## Reconciled instructor status

| Status | Items | Count |
|---|---|---:|
| COMPLETED | 1, 2, 4, 5, 6, 7, 8, 10, 14, 16, 17, 22, 27 | 13 |
| PARTIAL | 3, 15, 18, 19, 21, 24, 25, 26 | 8 |
| IN PROGRESS (external QLoRA evidence; unchanged) | 9, 11, 12 | 3 |
| NOT STARTED | 13, 20, 23 | 3 |

Completion means the literal item has a working, tracked deliverable; it does
not promote stronger scientific claims. In particular:

- item 14 is a completed RAG *system*, not proof that RAG improves repair;
- item 22 is a completed PDB adapter, not a GDB/LLDB implementation;
- items 24 and 25 remain partial because the mechanism is proven only with
  deterministic scripted models, not an accepted live-model PDB repair;
- items 19 and 21 remain partial because demo-scale/synthetic infrastructure
  is not a production preference corpus or a real four-model comparison.

## Genuinely open main-repository work

Completed after the reconciliation snapshot:

- tracker 7.1.2, strict root-cause explanation metric, is implemented and
  documented in `docs/ROOT_CAUSE_EXPLANATION_METRIC_V1.md`.
- bounded post-mortem PDB evidence is integrated through the existing
  controller ToolResult/Observation and canonical event/replay path; see
  `docs/POST_MORTEM_TRAJECTORY_INTEGRATION_V1.md`.
- the historical 32-node synthetic OpenCode wrapper full-suite family is
  repaired. The proven cause was a test-only forwarder cache collision across
  distinct target scripts; the post-fix suite completed with 3733 passed and
  3 skipped. See `docs/FULL_SUITE_FORWARDER_CACHE_REPAIR_V1.md`.

The remaining local maintenance item is to keep the daily diary and durable
status records current as milestones land.

The following work cannot be completed honestly from the main repository
alone:

- final QLoRA corpus/training/held-out decisions and tuned-model evidence;
- fine-tuned-plus-RAG execution and fine-tuned debugger-command evidence;
- DPO/RLHF, which requires an accepted SFT baseline and production preference
  corpus;
- a production preference corpus and real four-condition comparison, which
  require authorized real generations;
- a verifier-authoritative six-case live campaign and live PDB-effectiveness
  claim, which require fresh operator authorization and provider execution;
- BugsInPy source acquisition/execution, which remains license-gated.

These are blockers on particular outcomes, not blockers to continued bounded
main-repository engineering.
