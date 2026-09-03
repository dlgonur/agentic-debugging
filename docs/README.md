# docs — Navigation

This directory holds the repository's tracked documentation, organized by
purpose. Names follow short lowercase kebab-case. Content was reorganized
without rewriting scientific records; historical artifacts keep their
original dates, evidence, and internal old-path references where those are
historical statements.

## Final / current

| File | Purpose |
|---|---|
| `project-closeout.md` | **Current status authority** through the 2026-08-28 cycle closure |
| `release-closeout-2026-08-28.md` | Concise release, validation, and negative-boundary record |
| `pre-release-hardening-2026-08-27.md` | Durable PRE-RELEASE-HARDENING-01 closeout record (feature-freeze ready at `8fbea88`; PRH-D01..D09 dispositions) |
| `results-index.md` | Concise map from accepted conclusions to surviving evidence |
| `final-report.md` | Current technical report through 2026-08-13 (R1-R6 phase; S8/S9 snapshot archived) |
| `agentic-debugging-technical-project-report-2026-08-28.docx` | Concise Turkish project, architecture, evidence, validation, and closure report |
| `project-tracker.md` | Current execution tracker |

Repository-level landing: root `README.md`; roadmap `TODO.md`. The historical
2026-08-11 S9 closeout is archived at
`../outdated/docs-archive/status/project-closeout-2026-08-11.md`.
The 2026-08-10 master execution plan is a historical evidence carrier (not
current execution authority) at
`../outdated/docs-archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md`.

## architecture/

Accepted architecture and infrastructure designs. The current active application
architecture is `architecture/local-application-v1.md`; existing records include
the MVP implementation plan, PDB trajectory post-mortem integration, verifier
forwarder/cache repair, root-cause explanation metric, preference exporter, and
repository RAG. `architecture/agentic-debugger-v2-plan.md` is the accepted V2
target/migration **plan** (no V2 implementation has occurred).

## adr/

Architecture decision records (`NNNN-*.md`). `adr/0001-control-execution-plane-separation.md`
records the accepted V2 control/execution plane decision (logical seams, no
process split; verifier physical isolation deferred). ADRs with status
`Proposed` are decisions under review, not accepted architecture; `Accepted`
records an agreed target/migration direction, not completed implementation.

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

## ../outdated/

Superseded project documents are separated from current documentation and
kept intact under `outdated/` at the repository root. See
`../outdated/README.md` for the boundary and inventory. The former
`docs/archive/` content is now under `../outdated/docs-archive/`.

- `../outdated/docs-archive/reports/final-report-v1.md` — first technical report (2026-07-31).
- `../outdated/docs-archive/reports/final-report-2026-08-11.md` — exact 2026-08-11 S8/S9 report
  snapshot (byte-identical to the pre-R1-R6 `docs/final-report.md` blob).
- `../outdated/docs-archive/status/project-closeout-2026-08-11.md` — the 2026-08-11 S9 project
  closeout (moved from the repository root; content unchanged).
- `../outdated/docs-archive/status/instructor-status-map.md` — per-item status map snapshot.
- `../outdated/docs-archive/status/repo-reconciliation-2026-08-07.md` — repository status reconciliation.
- `../outdated/docs-archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md` — 2026-08-10
  master plan (still a frozen-in-repo carrier for some S5/RAW/cp118/DPO
  aggregates; its header still says ACTIVE at S1 and is not current status).
- `../outdated/docs-archive/status/README-historical-status-log-through-2026-08-07.md` — chronological
  status log formerly appended to the repository-root `README.md`. Archived
  unchanged; **not** current status authority.

## Frozen evaluation traces

Review-safe debugger JSON traces, schema, and manifests. **Path
sensitive: do not rename or move this directory.** Regeneration contracts and
integrity tests depend on `docs/professor_traces/` exactly.

## Historical delivery material (frozen)

The `../outdated/friday-delivery/FRIDAY_*` files are the frozen historical
delivery package.
They are path/provenance sensitive: their paths and pinned SHA-256 rows are
recorded in `../outdated/friday-delivery/FRIDAY_DELIVERY_MANIFEST_V1.md` and verified by
`scripts/verify_delivery_manifest_hashes.py`. Do not rename, move, or edit
them; `git mv` relocations of the pinned files are resolved by the verify
script's `LEGACY_PATH_MAP`, which maps delivery-time paths to current
locations while the manifest itself stays unchanged.
