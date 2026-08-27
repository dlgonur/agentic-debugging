# docs — Navigation

This directory holds the repository's tracked documentation, organized by
purpose. Names follow short lowercase kebab-case. Content was reorganized
without rewriting scientific records; historical artifacts keep their
original dates, evidence, and internal old-path references where those are
historical statements.

## Final / current

| File | Purpose |
|---|---|
| `project-closeout.md` | **Current status authority** (2026-08-13 R1–R6 state, plus 2026-08-16/17 product closeout, 2026-08-18 Nemotron capability-probe closeout, and the 2026-08-27 pre-release hardening update) |
| `pre-release-hardening-2026-08-27.md` | Durable PRE-RELEASE-HARDENING-01 closeout record (feature-freeze ready at `8fbea88`; PRH-D01..D09 dispositions) |
| `results-index.md` | Concise map from accepted conclusions to surviving evidence |
| `final-report.md` | Current technical report through 2026-08-13 (R1-R6 phase; S8/S9 snapshot archived) |
| `project-tracker.md` | Current execution tracker |
| `instructor-todo.md` | Instructor's original 27-item task list (byte-identical) |

Repository-level landing: root `README.md`; roadmap `TODO.md`. The historical
2026-08-11 S9 closeout is archived at `archive/status/project-closeout-2026-08-11.md`.
The 2026-08-10 master execution plan is a historical evidence carrier (not
current execution authority) at
`archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md`.

## architecture/

Accepted architecture and infrastructure designs. The current active application
architecture is `architecture/local-application-v1.md`; existing records include
the MVP implementation plan, PDB trajectory post-mortem integration, verifier
forwarder/cache repair, root-cause explanation metric, preference exporter, and
repository RAG.

## evaluation/

Evaluation and model-decision records: comparison harness, real-model
evaluation protocol, model/RAG/SFT/DPO decision gate, RAG comparison decision.

## datasets/

Dataset selection and evaluation decision. Subdirectories:

- `bugsinpy/` — adapter design/usage, license gate, metadata preflight, pilot readiness.
- `quixbugs/` — smoke guide, eight-task baseline, paired pilots (v1/v2), v2 authorization, v2 live runner, OpenCode Go execution adapter.

## research/

Literature surveys and comparisons: automated debugging survey, LLM-based
debugging review, debugging approach comparison.

## demo/

- `guide.md` — deterministic offline demo guide.
- `task-9.md` — Task 9 demonstration contract and results.

## archive/

Historical snapshots, kept intact:

- `reports/final-report-v1.md` — first technical report (2026-07-31).
- `reports/final-report-2026-08-11.md` — exact 2026-08-11 S8/S9 report
  snapshot (byte-identical to the pre-R1-R6 `docs/final-report.md` blob).
- `status/project-closeout-2026-08-11.md` — the 2026-08-11 S9 project
  closeout (moved from the repository root; content unchanged).
- `status/instructor-status-map.md` — per-item status map snapshot.
- `status/repo-reconciliation-2026-08-07.md` — repository status reconciliation.
- `status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` — 2026-08-10
  master plan (still a frozen-in-repo carrier for some S5/RAW/cp118/DPO
  aggregates; its header still says ACTIVE at S1 and is not current status).
- `status/README-historical-status-log-through-2026-08-07.md` — chronological
  status log formerly appended to the repository-root `README.md`. Archived
  unchanged; **not** current status authority.

## professor_traces/

Professor-facing debugger JSON traces, schema, and manifests. **Path
sensitive: do not rename or move this directory.** Regeneration contracts and
integrity tests depend on `docs/professor_traces/` exactly.

## Historical delivery material (frozen)

The `docs/FRIDAY_*` files are the frozen Friday professor-delivery package.
They are path/provenance sensitive: their paths and pinned SHA-256 rows are
recorded in `docs/FRIDAY_DELIVERY_MANIFEST_V1.md` and verified by
`scripts/verify_delivery_manifest_hashes.py`. Do not rename, move, or edit
them; `git mv` relocations of the pinned files are resolved by the verify
script's `LEGACY_PATH_MAP`, which maps delivery-time paths to current
locations while the manifest itself stays unchanged.
