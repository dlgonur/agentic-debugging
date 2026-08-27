# Pre-Release Hardening Closeout — PRE-RELEASE-HARDENING-01 (2026-08-27)

**Status:** ACCEPTED / FEATURE-FREEZE READY
**Baseline:** `main` @ `5cbe856` ("feat: add local project debugging")
**Accepted hardening commit:** `8fbea88` ("Harden pre-release runtime and
Local Project")
**Review:** independently reviewed by FirstMate; repaired in two FirstMate
rounds; ACCEPTED; committed as `8fbea88`, fast-forward merged to `main`, and
pushed by the owner.
**Evidence carrier:** the final hardening evidence lives in the untracked
local review package `_ai-review/PRE-RELEASE-HARDENING-01/` (not part of the
repository checkout); this document is the durable tracked record.

This document records only durable release-relevant facts. It supersedes the
2026-08-24 "next active direction = Local Application / UI and UX refinement"
current-direction language.

## Scope of the forensic audit

A final pre-release forensic audit of the accepted product at `main` @
`5cbe856`, across: architecture, static sweep, Local Project deep audit,
worker/IPC lifecycle, journal/reducer/replay, adversarial patching, PDB,
Level-32 invariants, Ollama transport, security, UI/UX, keyboard, error-UX,
performance, platform, test quality, and dead code.

## Findings and repairs

- **RED (9 total, none remaining):** PRH-001..004 from the original audit,
  all FIXED; PRH-025..028 found by FirstMate in the sealed candidate (repair
  round 1) and PRH-029 (repair round 2), all REPAIRED with regression
  coverage.
- **ORANGE (21):** 17 FIXED, 2 verified FALSE_POSITIVE, 2 bounded deferred
  (PRH-D04, PRH-D08).
- **Deferred/accepted debt (9):** PRH-D01..D09, documented below; none is a
  release blocker.

Major repaired trust/correctness areas:

- Local Project command execution delegates to the accepted runtime
  `CommandRunner` (bounded kill ladder, correct argv quoting, bounded UTF-8)
  instead of a weaker parallel implementation (PRH-001); isolated worktrees
  are cleaned post-mortem, including stale `git worktree` registrations in
  the owner repository (PRH-002).
- Apply To Project follows the accepted SESSION-LEDGER candidate semantics,
  requires a terminal session and the session-final view, runs gates/apply
  off the UI thread, reports the outcome, and preserves the canonical
  `LocalProjectTaskSpec` provenance across completed sessions (PRH-003,
  PRH-025, PRH-029).
- Durable-truth repairs: baseline reproduction is proven from the real exit
  code rather than the user's bug report (PRH-027); professor-trace
  `reproduced` derives from observation proof, not output text (PRH-017/
  PRH-026); a MODEL_CONFIGURED journal failure fails closed before any model
  request (PRH-028); artifact-write failures are surfaced durably (PRH-009).
- Frozen R6 evidence-capsule integrity is restored on Windows `autocrlf`
  checkouts via a `-text` `.gitattributes` rule (committed blob bytes
  untouched) (PRH-004).
- Unified-diff parse/apply splits on LF only, and revert rollback restores
  the true pre-revert bytes (PRH-011, PRH-012); production failure-injection
  sentinels were removed and tests inject real filesystem failures (PRH-005).

## FirstMate repair rounds

1. **Round 1 (PRH-025..028):** Apply To Project threading and outcome
   notification; professor-trace proof versus output text; Local Project
   baseline reproduction truth; MODEL_CONFIGURED journal fail-closed.
2. **Round 2 (PRH-029):** completed Local Project sessions overwrote the
   canonical task spec with an incompatible mapping, breaking Apply
   provenance after any ordinary completed session; fixed with strict
   round-trip preservation plus a real completed-session regression.

## Deferred debt and accepted risks (PRH-D01..D09)

Dispositions below are the durable record; full detail is in the review
package's `deferred-debt.md`.

**ACCEPTED_RISK — documented design trade-offs accepted with containment
arguments:**

- **PRH-D01** — live transport watchdog is idle-based per request; the
  canonical Ollama adapter enforces its own outer request deadline, and the
  Local Project product path is separately bounded by session cancellation
  and escalation.
- **PRH-D02** — `PdbSession.stop()` can wedge in STOPPING only if an orphan
  holds the protocol pipe after the kill ladder; job-object containment
  bounds orphans and the wedge self-heals at escalation.
- **PRH-D03** — environment inheritance for user commands and debug targets
  is the accepted trusted-user-configuration product requirement; processes
  are contained and journals are redacted at every event boundary.
- **PRH-D05** — the Ollama version gate is preflight-only; per-request
  identity assertions cover alias/upstream/remote-host/tags; documented
  adapter design.
- **PRH-D06** — the Level-32 supervisor reads operator-owned capture files
  unbounded before an 8192-byte projection bound; trusted local operator
  boundary.
- **PRH-D07** — three conservative credential-shape regex vocabularies with
  slightly differing coverage; every persisted surface is redacted by at
  least one layer; unification pre-freeze would touch scientific transport
  fingerprints.

**DEFERRED — bounded pre-existing flaws or harness debt, deliberately not
repaired pre-freeze:**

- **PRH-D04** — `test_cancel_interrupts_configured_request` fails
  pre-existing at base (races a fast scripted model against a 30 s window);
  the cooperative-cancellation contract is covered green elsewhere; a proper
  repair needs a slower dummy-model fixture.
- **PRH-D09** — `ModelRequestBudgetExceeded` is never raised in-repo and
  derives from the wrong base; the special terminalization is unreachable
  with the in-repo transport; re-parenting is a harness refactor with
  golden-trajectory risk.

**OWNER DECISION — optional product scope, not required for the freeze:**

- **PRH-D08** — configured-command and deterministic-offline UI modes are
  unreachable through the ladder-only Start picker (the worker sources remain
  fully covered by tests). Re-adding curated tasks to the picker or removing
  the dead mode UI is an owner product decision.

## Validation boundary

Validation was deterministic only (Windows host, uncommitted candidate
branches during the work):

- Final round: prerelease-hardening regression file 21/21; local project
  suite 50/50; Local Project UI and session-ledger-copy suites 51/52 (the 1
  failure is the documented pre-existing README-length assertion);
  `compileall` OK; `git diff --check` clean.
- Original round: 14 new regression tests; affected suites green (patcher
  107/107, worker/UI integration 110, professor traces 34/34 after the
  capsule fix, Ollama qualification 25/25, configured UI 14/15 with the
  documented PRH-D04 failure); offline demo OK; 12 SVG visual captures
  reviewed with 2 follow-up repairs.
- Full-suite base-versus-candidate sweeps on the same machine: remaining
  failures are identical to, or a subset of, the clean base failures
  (pre-existing environmental/missing-local-artifact cases), with the 5
  capsule-integrity base failures fixed.

External validations were **not** rerun, and their accepted evidence stands:
real Ollama provider inference, Docker/Level-32 official execution, WSL/
Bubblewrap QuixBugs campaigns, and BugsInPy (license-gated). Their contracts
remain covered deterministically by the qualification, operator, Level-32,
and QuixBugs suites.

## Release boundary

- No known RED release blocker remains.
- Hardening acceptance is feature-freeze ready; it does **not** itself mean
  a release tag was created. Tag/release Git operations remain owner
  decisions.
- Next phase: documentation/release/tag/closure under owner decision. No
  active required engineering campaign remains.
