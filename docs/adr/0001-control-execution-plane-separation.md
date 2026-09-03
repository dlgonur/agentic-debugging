# ADR 0001 — Control/Execution Plane Separation as Logical Seams, Not Process Split

**Status:** Proposed (owner decision pending on the five unresolved questions in the V2 plan)
**Date:** 2026-09-03
**Baseline:** `plan/architecture-v2-control-execution-separation` at `4606933`
**Full analysis:** `docs/architecture/agentic-debugger-v2-plan.md`

## Context

The 2026-08 provider/runtime hardening cycle closed six boundary failure classes
(credential authority divergence between parent and child adapter, TLS/proxy
environment loss across the worker/adapter boundary, OpenCode auth-store
visibility, Windows venv launcher PID indirection, transport/auth/subprocess
coupling in the provider core, and configuration presence being treated as
execution readiness). Every repair succeeded *inside* the current three-tier
process topology (UI → session worker → tool/adapter children) by adding an
explicit contract at each divergence site.

The architectural question was whether those incidents expose a deeper coupling
problem requiring a control-plane / execution-plane **process** separation.

## Decision

Adopt **explicit logical planes with the current process topology** (V2
Alternative B), with exactly one targeted process-boundary change:

1. First-class contracts: `Session`, `AgentDefinition`,
   `ExecutionEnvironment`, `ModelGateway`, `CredentialVault`, an
   `Executor` **interface** (no new process), and a `Verifier` **service**.
2. `ExecutionEnvironment` is the keystone: one typed, fingerprinted
   declaration of interpreter identity, workspace roots, environment
   allowlist rules, network/trust policy, and limits — consumed by every
   child-process construction site instead of each site re-deriving them.
3. The independent verifier stops executing inside the session worker
   process and runs as a short-lived child spawned by the session shell,
   with its own credential-free environment and workspace ownership.
4. Credentials resolve exactly once per session (vault-issued ephemeral
   binding); no execution-plane child ever receives a credential variable.

A general control-plane/execution-plane process split (Alternative C) is
**rejected**: no observed failure class requires long-lived executor
processes, and the added IPC, Windows lifecycle surface, and
workspace-ownership-across-restart complexity are not earned. The seams
adopted here keep a later selective move to C designable if evidence ever
demands it.

## Consequences

- The incident classes are closed at their generators (undeclared
  environment/credential/identity state re-derived at multiple sites),
  including an active leak found during this investigation: the forwarded
  provider credential variable is currently inherited by user
  reproduction/verification and verifier command children via
  `runtime/command_runner.py` full-environment copies.
- The deterministic controller, PDB subsystem, journal/evidence chain,
  patch policy, disposable-workspace semantics, and outcome taxonomy remain
  structurally untouched.
- Scientific treatment qualification remains independent from interactive
  provider availability; no configured provider becomes experiment-qualified
  by this architecture.
- Migration is five incremental stages (V2-01 … V2-05), each shippable and
  reversible; the first slice is pure typing (`SessionLaunch`,
  `AgentDefinition`, `Executor` interface), the second closes the
  credential-inheritance leak.
- Checkpoint/resume is deferred: current replay is audit/UI replay only, no
  failure class demands resume, and resume would require workspace-generation
  equivalence plus credential epochs.

## Triggers to revisit

- Recurring loss of expensive controller progress in long interactive
  sessions (would reopen checkpoint/resume and re-evaluate Alternative C).
- Inability to land V2-01/02 without destabilizing the worker boundary
  tests, or real user repro scripts breaking under the execution environment
  allowlist (would fall back toward Alternative A with documented residual
  risk).
- Verifier-out-of-process measurably complicating Windows cleanup or adding
  user-visible latency (would restore the in-process call under a stricter
  code-boundary rule).
