# Release closeout — 2026-08-28

## Decision

The mandatory project cycle is closed. No required engineering campaign
remains open.

- Release tag: `v0.1.0` at `d01f7a5`.
- Post-tag application repair: `8b2479b`.
- Merge: not performed.
- Push: not performed.
- External provider, Docker, WSL, paid route, and BugsInPy execution: not
  performed during closure.

## Closed work

- Restored all accepted curated sources to the Start picker while preserving
  the frozen capability-ladder order.
- Removed the transient UI cancellation-state race.
- Reconciled stale request-budget and campaign-timestamp debt with source and
  regression evidence.
- Replaced the chronological roadmap with a concise closed TODO and tracker.
- Moved superseded material under `outdated/` while preserving historical delivery
  hash verification.

## Negative boundaries

- BugsInPy remains license-gated and was not executed.
- The optional OpenCode Go six-case campaign was not executed.
- The stronger R6 holdout remains `INCOMPLETE_HARDWARE_STOP`.
- Fine-tuned + RAG remains partial and `NOT_EVALUATED` for correctness.
- DPO remains closed as not justified.
- Capability escalation remains paused at the accepted Level-32 boundary.

## Authority

- Current status: `docs/project-closeout.md`.
- Accepted evidence: `docs/results-index.md`.
- Closed roadmap: `TODO.md`.
- Execution record: `docs/project-tracker.md`.
- Technical project report:
  `docs/agentic-debugging-technical-project-report-2026-08-28.docx`.
- Historical material: `outdated/README.md`.

## Deterministic validation

- Test collection: 6002 tests.
- Changed application/UI surface: 746 passed after five stale expectations
  were reconciled; repaired nodes 8 passed.
- Core release package: 1145 passed in 718.17 seconds. It covered controller,
  state and budgets, patch/workspace, verifier, events/journal, application
  contracts, configured and deterministic sources, and golden trajectories.
- Full-suite attempt: 76 passed before a controlled interrupt at 1071.50
  seconds and about 1% progress. Projected duration was disproportionate; no
  failure had appeared. This is not represented as a completed full-suite run.
- Public documentation navigation: 10 passed.
- Frozen delivery manifest: 13/13 SHA-256 rows matched.
- `python -m compileall -q agentic_debugger scripts`: passed.
- Offline demo on `curated-off-by-one-002`: both static and PDB-on-uncertainty
  cases independently verifier-resolved (F2P 1/1, P2P 2/2); PDB observations
  5/5 in the PDB arm; provider/network attempts 0.

The task-owned `demo-out-closure-20260828/` output was moved out of the
repository root into the ignored review package after evidence
capture. No tracked source or canonical fixture was changed.

## Technical report artifact

- Output: `docs/agentic-debugging-technical-project-report-2026-08-28.docx`.
- Format: eight-page Word document; 49,140 bytes.
- SHA-256:
  `5762A2D7DFD4E32CA18ADA25DBBDC6590B46374B8682A83AC451A8C12E368D76`.
- Visual QA: all eight pages inspected from the sealed Word-to-PDF render;
  no clipping, overflow, orphaned table header, or broken page end found.
- Accessibility audit: high 0, medium 0, low 0.
- Style lint: passed under UTF-8; reported direct formatting is intentional
  masthead, metadata, table, and code treatment.
- Table geometry: all nine tables have matching width, indent, grid, and cell
  widths.
- Package integrity and placeholder scan: passed.
