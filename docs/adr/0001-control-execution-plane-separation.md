# ADR 0001 — Control/Execution Plane Separation as Logical Seams, Not Process Split

**Status:** Accepted (target/migration decision — no V2 implementation has occurred)
**Date:** 2026-09-03 (rev. 02 — FirstMate architecture-review repair applied)
**Baseline:** `4606933`; plan candidate lineage `3481b58` → repair commit 02
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
Owner/FirstMate review of the resulting plan (revision 01) accepted the core
direction and ordered a repair that tightened the execution and trust
boundaries; this revision records the accepted decision.

## Decision

Adopt **explicit logical planes and first-class authority contracts in the
current principal process topology** (V2 Alternative B), with **no new
long-lived processes and no committed process moves**:

1. First-class contracts: `Session`, `AgentDefinition` (product/runtime
   provenance only), `ExecutionEnvironment` (one declarative authority with
   role-scoped least-authority child derivations), `ModelGateway`,
   `CredentialVault` (binding separated from secret materialization), an
   `Executor` **interface**, and a `VerifierService` **logical boundary**.
2. `ExecutionEnvironment` is the keystone and the first high-value seam: one
   typed, versioned policy declaration (interpreter identity, workspace
   policy, role-scoped environment rules, per-capability network/trust policy,
   limits) consumed by every child-process construction site — model adapter,
   project repro/test commands, PDB worker, verifier commands, legacy CLI —
   instead of each site re-deriving them.
3. Credentials resolve through one authority: a non-secret session-stable
   `CredentialBinding` (provider identity, endpoint/profile binding, source
   backend identity — journaled safely as provenance) separated from the
   secret-bearing material/lease injected only into model-adapter request
   children. The worker may retain the authorized secret in bounded private
   session state (the transport spawns a fresh adapter child per request);
   this retention is stated honestly rather than described as single-child
   secret lifetime.
4. The independent verifier remains the sole correctness authority and keeps
   its existing substantive independence (source-commit binding, clean-source
   export, separate disposable workspaces, independent patch evaluation and
   outcome taxonomy). `VerifierService` becomes a first-class **logical**
   boundary. **Physical verifier subprocess isolation is DEFERRED** behind
   explicit triggers (verifier crash/hang materially threatening the worker
   lifecycle; in-process environment isolation proving insufficient; a
   concrete untrusted-code security boundary; measured operational evidence
   that isolation pays for its Windows cost). The logical seam must keep
   later subprocess extraction cheap if a trigger fires.
5. Compatibility for the execution-environment change is bounded non-secret
   extension only — explicit extra variable names, role-policy adjustments,
   fail-closed rejection of credential-shaped variables, name-only
   diagnostics. **No full-environment mode may exist**, because it would
   reintroduce the credential exposure the first stage exists to eliminate.

A general control-plane/execution-plane process split (Alternative C) is
**rejected**: no observed failure class requires long-lived executor
processes, and the added IPC, Windows lifecycle surface, and
workspace-ownership-across-restart complexity are not earned. The adopted
seams keep a later selective move toward C designable if the checkpoint/resume
trigger ever fires.

## Consequences

- Migration starts with a real invariant change, not typing: **V2-01
  ExecutionEnvironment authority + credential isolation** closes the active
  execution-boundary exposure found during the investigation —
  `runtime/command_runner.py::_build_env` copies the full worker environment
  (including the forwarded private provider credential variable) into user
  reproduction/verification and verifier command children. V2-01 makes
  provider credentials structurally unavailable to project execution children
  and proves it with secret-exclusion regression tests.
- Stage order: V2-01 (environment + secret isolation) → V2-02
  (Session/AgentDefinition/Executor/VerifierService logical seams) → V2-03
  (ModelGateway + truthful provider status vocabulary + advanced
  provider-type/endpoint-contract UI disclosure) → V2-04 (CredentialVault
  binding/backend seam) → V2-05 (OPTIONAL verifier process-isolation
  evaluation; resolves to NOT JUSTIFIED / DEFERRED unless a trigger is
  evidenced).
- The deterministic controller, PDB subsystem, journal/evidence chain, patch
  policy, disposable-workspace semantics, verifier logic and outcome taxonomy
  remain structurally untouched.
- Scientific treatment qualification remains independent from interactive
  provider availability; `AgentDefinition` never becomes the qualification
  authority for existing frozen scientific treatment identity (treatment
  roster, prompt-profile identity, qualification rules, frozen provenance
  stay authoritative; any future migration of scientific identity into
  `AgentDefinition` is a separate explicit compatibility decision outside this
  plan).
- Provider UX states become precise separate facts (`Configured`,
  `Credential ready`, `Model runnable`, `Catalog refreshed at T`,
  `Live verified at T`, `Runtime succeeded at T`); credential presence alone
  is never labeled `Connected`. This is validated by a real post-plan
  incident: the UI displayed `Connected · saved` for CommandCode GOAT while
  its configured endpoint (`http://127.0.0.1:57788`) had no listener and the
  real model request failed.
- Checkpoint/resume is DEFERRED (owner decision): current retry-chain +
  durable journal + replay remain the product behavior, and replay is never
  called resume.

## Triggers to revisit

- **Checkpoint/resume / Alternative C:** actual evidence that loss of
  long-running controller progress is a material operator problem, or another
  concrete requirement needing resumable sessions.
- **Verifier physical isolation (V2-05):** the four explicit triggers in
  Decision item 4.
- **Fallback toward Alternative A (contract hardening only):** inability to
  land V2-01's role-scoped environments without destabilizing the worker
  boundary tests, or real user repro scripts that cannot be satisfied safely
  through bounded non-secret opt-ins — with the credential exposure then
  documented as residual risk.
