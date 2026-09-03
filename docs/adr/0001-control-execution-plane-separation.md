# ADR 0001 — Control/Execution Plane Separation as Logical Seams, Not Process Split

**Status:** Accepted (target/migration decision — no V2 implementation has occurred)
**Date:** 2026-09-03 (rev. 03 — final convergence repair before V2-01 implementation)
**Baseline:** `4606933`; plan candidate lineage `3481b58` → `3d414c6` → repair commit 03
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
Owner/FirstMate reviews of the resulting plan (revisions 02 and 03) accepted
the core direction and ordered repairs that tightened the execution and trust
boundaries and finalized the authority rules; this revision records the
accepted decision.

## Decision

Adopt **explicit logical planes and first-class authority contracts in the
current principal process topology** (V2 Alternative B), with **no new
long-lived processes and no committed process moves**:

1. First-class contracts: `Session`; `AgentDefinition` (what the agent is
   ALLOWED/REQUESTS: controller/prompt policy, requested model identity,
   allowed tool capabilities, budget defaults); `ExecutionEnvironment` (what
   the machine/session CAN provide: interpreter/runtime, workspace/process
   policy, available execution capabilities including PDB availability,
   role-scoped child-environment rules, per-capability network/trust policy);
   `EffectiveSessionCapabilities` (computed once from
   `AgentDefinition.allowed_capabilities ∩ ExecutionEnvironment.available_
   capabilities ∩ task/product policy`, recorded as session provenance — no
   consumer recomputes the intersection); `ModelGateway` and `ModelBinding`
   (runtime transport provenance); `CredentialVault` (binding separated from
   secret materialization); an `Executor` **interface**; and a
   `VerifierService` **logical boundary**.
2. `ExecutionEnvironment` is the keystone and the first high-value seam: one
   typed, versioned policy declaration consumed by every child-process
   construction site — model adapter, project repro/test commands, PDB
   worker, verifier commands, legacy CLI — instead of each site re-deriving
   them. Its target contract is **positive/declarative**:
   `ProjectRuntimeEnvironment = platform/runtime essentials + explicitly
   authorized project variables + explicitly authorized project-secret
   bindings`; no arbitrary ambient inheritance. A temporary bounded
   compatibility bridge is permitted in V2-01 only if investigation proves
   the positive contract cannot land safely in one slice; the bridge excludes
   Agentic Debugger provider/model credential channels **by classified
   identity** (provenance, with name/value detection as fail-safe only),
   never automatically inherits arbitrary parent variables, and carries
   explicit exit criteria. No full-environment mode may exist.
3. **Secret trust classes are distinct authorities.** Control/model secrets
   (provider API credentials, model-channel variables, provider CLI-auth and
   credential-store material) must never reach project repro/test commands,
   PDB/project code, or verifier commands — the active V2-01 invariant.
   Project runtime secrets (test database credentials, local service tokens,
   application keys, fixture credentials) are explicitly authorized
   ProjectRuntimeEnvironment: they may be consumed by project execution
   roles, but never flow into the model adapter or prompts, journals,
   fingerprints, or diagnostics, never become provider credentials, and are
   never implicitly copied from the parent process. Environment variables are
   classified by provenance/capability (platform/runtime; Agentic Debugger
   internal control; model/provider transport; project runtime; project
   runtime secret; diagnostic-only); known-name/value detection is a
   secondary fail-safe, never the architectural authority.
4. **Credential sequencing is honest.** V2-01–V2-03 preserve the existing
   provider credential authority and private model-channel forwarding
   mechanism (`provider_connections.py:2417,2458`); the `CredentialVault`
   interface, `CredentialBinding` (non-secret, session-stable) versus secret
   material/lease (per-request injection into the exact model-adapter child;
   the worker may retain the authorized secret in bounded private session
   state), and removal of the adapter's ambient re-resolution arrive in
   **V2-04**. Target diagrams may show the final Vault; migration sections
   state which authority exists at each stage.
5. The independent verifier remains the sole correctness authority and keeps
   its existing substantive independence (source-commit binding, clean-source
   export, separate disposable workspaces, independent patch evaluation and
   outcome taxonomy). `VerifierService` becomes a first-class **logical**
   boundary whose role environment is **control/provider-credential-free**
   while receiving the explicitly authorized ProjectRuntimeEnvironment the
   project needs for reproducibility. **Physical verifier subprocess
   isolation is DEFERRED** behind explicit triggers (verifier crash/hang
   materially threatening the worker lifecycle; in-process environment
   isolation proving insufficient; a concrete untrusted-code security
   boundary; measured operational evidence that isolation pays for its
   Windows cost). **Same-session verifier re-verification is likewise
   DEFERRED**: a verifier failure remains an honest terminal outcome under
   the existing taxonomy; a future re-verify is a separate product feature
   behind the VerifierService seam with explicit evidence-lineage semantics.
   The logical seam must keep later extraction cheap if a trigger fires.
6. **Runtime transport route is session provenance, not agent identity.**
   `AgentDefinition` declares requested model identity/configuration
   (provider logical identity, model identity, prompt policy); the concrete
   resolved route (direct vs legacy, effective protocol, endpoint
   contract/transport profile, resolved endpoint, credential binding
   reference, adapter provenance) belongs to the session's `ModelBinding`,
   produced by `ModelGateway` — so an agent definition cannot become stale
   merely because provider runtime configuration changes.
7. Compatibility for the execution-environment change is bounded classified
   extension only — explicit extra variable names, role-policy adjustments,
   fail-closed rejection of known control-secret variables, name-only
   diagnostics. No full-environment mode may exist, because it would
   reintroduce the credential exposure the first stage exists to eliminate.

A general control-plane/execution-plane process split (Alternative C) is
**rejected**: no observed failure class requires long-lived executor
processes, and the added IPC, Windows lifecycle surface, and
workspace-ownership-across-restart complexity are not earned. The adopted
seams keep a later selective move toward C designable if the checkpoint/resume
trigger ever fires.

## Consequences

- Migration starts with a real invariant change, not typing: **V2-01
  ExecutionEnvironment authority + credential isolation** uses a minimal
  coherent surface (typed policy authority, explicit child roles, structural
  exclusion of control/model secret channels from project/PDB/verifier
  children, unchanged model-adapter and Windows interpreter behavior, proven
  project runtime compatibility, secret-exclusion regression tests) to close
  the active execution-boundary exposure —
  `runtime/command_runner.py::_build_env` copies the full worker environment
  (including the forwarded private provider credential variable) into user
  reproduction/verification and verifier command children.
- Stage order: V2-01 (environment + secret isolation, existing credential
  forwarding preserved) → V2-02 (Session/AgentDefinition/Executor/
  VerifierService logical seams + capability intersection) → V2-03
  (ModelGateway + ModelBinding + truthful, history-derived provider status
  vocabulary + advanced provider-type/endpoint-contract UI disclosure) →
  V2-04 (CredentialVault binding/backend seam) → V2-05 (OPTIONAL verifier
  process-isolation evaluation; resolves to NOT JUSTIFIED / DEFERRED unless a
  trigger is evidenced).
- The deterministic controller, PDB subsystem, journal/evidence chain, patch
  policy, disposable-workspace semantics, verifier logic and outcome taxonomy
  remain structurally untouched.
- Scientific treatment qualification remains independent from interactive
  provider availability; `AgentDefinition` never becomes the qualification
  authority for existing frozen scientific treatment identity (treatment
  roster, prompt-profile identity, qualification rules, frozen provenance
  stay authoritative). The execution-contract fingerprint is
  PRODUCT/local-session metadata initially and is fenced out of frozen
  scientific event serialization, treatment identity, hash inputs, evidence
  manifests, and qualification; adding it to frozen scientific evidence
  requires a separate explicit compatibility decision.
- Provider UX states become precise separate facts (`Configured`,
  `Credential ready`, `Model runnable`, `Catalog refreshed at T`,
  `Live verified at T`, `Runtime succeeded at T`); credential presence alone
  is never labeled `Connected`. `Runtime succeeded at T` is observational
  history derived from the durable session/event history — never provider
  config truth, route-selection input, or qualification input. This is
  validated by a real post-plan incident: the UI displayed `Connected ·
  saved` for CommandCode GOAT while its configured endpoint
  (`http://127.0.0.1:57788`) had no listener and the real model request
  failed.
- Checkpoint/resume and same-session verifier re-verification are both
  DEFERRED (owner decisions): retry-chain + durable journal + replay remain
  the product behavior, and replay is never called resume.

## Triggers to revisit

- **Checkpoint/resume / Alternative C:** actual evidence that loss of
  long-running controller progress is a material operator problem, or another
  concrete requirement needing resumable sessions.
- **Verifier physical isolation (V2-05):** the four explicit triggers in
  Decision item 5.
- **Same-session verifier re-verification:** operator demand showing real
  value in re-running verification over a retained candidate (separate
  product feature behind the VerifierService seam, with evidence-lineage
  semantics).
- **Fallback toward Alternative A (contract hardening only):** inability to
  land V2-01's role-scoped environments without destabilizing the worker
  boundary tests, or real user repro scripts that cannot be satisfied safely
  through explicit declaration plus the bounded bridge — with the credential
  exposure then documented as residual risk.
