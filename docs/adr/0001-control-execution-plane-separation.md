# ADR 0001 — Control/Execution Plane Separation as Logical Seams, Not Process Split

**Status:** Accepted (target/migration decision — V2-01 execution-environment authority + control/provider secret isolation implemented; V2-02 and later stages not implemented)
**Date:** 2026-09-03 (rev. 04 — implementation-readiness reconciliation before V2-01)
**Baseline:** `4606933`; plan candidate lineage `3481b58` → `3d414c6` → `ff81f44` → repair commit 04
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
Owner/FirstMate reviews of the resulting plan (revisions 02–04) accepted the
core direction and ordered repairs that tightened the execution and trust
boundaries, finalized the authority rules, and reconciled two repository facts
the earlier revisions missed; this revision records the accepted decision.

## Decision

Adopt **explicit logical planes and first-class authority contracts in the
current principal process topology** (V2 Alternative B), with **no new
long-lived processes and no committed process moves**:

1. First-class contracts: `Session`; `AgentDefinition` (what the agent is
   ALLOWED/REQUESTS: controller/prompt policy, requested model identity,
   allowed tool capabilities, budget defaults); `ExecutionEnvironment` (what
   the machine/session CAN provide — the **product/local-session** execution
   policy authority: interpreter/runtime, workspace/process policy, available
   execution capabilities including PDB availability, role-scoped
   child-environment rules, per-capability network/trust policy);
   `EffectiveSessionCapabilities` (computed once from
   `AgentDefinition.allowed_capabilities ∩ ExecutionEnvironment.available_
   capabilities ∩ task/product policy`, recorded as session provenance — no
   consumer recomputes the intersection); `ModelGateway` and `ModelBinding`
   (runtime transport provenance); `CredentialVault` (binding separated from
   secret materialization); an `Executor` **interface**; a `VerifierService`
   **logical boundary**; and a `ProjectRuntimeEnvironmentSpec` ingress
   (V2-02).
2. **The existing `VerifiedExecutionContext` authority is preserved, not
   replaced.** `runtime/execution.py` already provides the specialized
   reviewed scientific/contained execution contracts (`PreparedEnvironment`
   with credential-like variable prohibition, `ContainmentGuarantee`
   fail-closed containment, `VerifiedExecutionContext` approved-command
   binding, `PdbLaunchPlan`). These remain the authoritative specialized
   contract for BugsInPy, QuixBugs, contained PDB, and external evaluation —
   not migrated, weakened, broadened, or reinterpreted by any V2 stage. The
   product `ExecutionEnvironment` governs only the paths that currently fall
   through to implicit parent-environment inheritance. Where both authorities
   could reach one call site, precedence is explicit and fail closed:
   conflicting product and verified-scientific environment authorities are
   rejected, never merged. The two concepts are never casually called "the
   execution context" interchangeably; any future unification is a separate
   explicit compatibility decision (shared lower-level primitive only when
   semantics are proven identical and frozen/scientific behavior unchanged),
   never an implicit V2-01 consequence.
3. `ExecutionEnvironment` is the keystone and the first high-value seam: one
   typed, versioned policy declaration consumed by every product
   child-process construction site — model adapter, project repro/test
   commands, PDB worker, verifier commands, legacy CLI — instead of each site
   re-deriving them. Its target contract is **positive/declarative**:
   `ProjectRuntimeEnvironment = platform/runtime essentials + explicitly
   authorized project variables + explicitly authorized project-secret
   bindings`; no arbitrary ambient inheritance. Because **no product ingress
   for that contract exists today** (verified: `SessionSpec`,
   `ExecutionSourceSpec`, and the Local Project start UI carry no
   project-environment surface), **V2-01 uses an explicit transitional
   `LEGACY PROJECT AMBIENT` compatibility bridge**: project-role-only;
   created and classified by the session's V2 environment authority and
   passed explicitly to runners (the runner itself stops reading
   `os.environ` — authority moves out of `_build_env()`, not merely a
   denylist inside it); structurally excludes all Agentic Debugger
   control/model/provider channels **by classified provenance/known authority
   identity** (name heuristics are a secondary fail-safe only); never feeds
   the model adapter or prompts; never journals or fingerprints environment
   values; preserves project/PDB/verifier parity; carries a named
   compatibility identity/version and explicit removal criteria. Its
   **documented residual risk**: unclassified operator environment variables
   may still reach project code during the bridge — exactly as today; the
   bridge is a bounded migration state, not the target least-authority model,
   and never a user-facing "full environment" switch.
4. **Secret trust classes are distinct authorities.** Control/model secrets
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
5. **Credential sequencing is honest.** V2-01–V2-03 preserve the existing
   provider credential authority and private model-channel forwarding
   (`provider_connections.py:2417,2458`); the `CredentialVault`
   interface, `CredentialBinding` (non-secret, session-stable) versus secret
   material/lease (per-request injection into the exact model-adapter child;
   the worker may retain the authorized secret in bounded private session
   state), and removal of the adapter's ambient re-resolution arrive in
   **V2-04**. Target diagrams may show the final Vault; migration sections
   state which authority exists at each stage.
6. The independent verifier remains the sole correctness authority and keeps
   its existing substantive independence (source-commit binding, clean-source
   export, separate disposable workspaces, independent patch evaluation and
   outcome taxonomy). `VerifierService` becomes a first-class **logical**
   boundary whose role environment is **control/provider-credential-free**
   while receiving the declared project-runtime inputs the project needs for
   reproducibility (bridge snapshot in V2-01, `ProjectRuntimeEnvironmentSpec`
   from V2-02), injected through the verifier's own runner factory so the
   controller/model path cannot mutate it after verification begins.
   **Physical verifier subprocess isolation is DEFERRED** behind explicit
   triggers (verifier crash/hang materially threatening the worker lifecycle;
   in-process environment isolation proving insufficient; a concrete
   untrusted-code security boundary; measured operational evidence that
   isolation pays for its Windows cost). **Same-session verifier
   re-verification is likewise DEFERRED**: a verifier failure remains an
   honest terminal outcome under the existing taxonomy; a future re-verify is
   a separate product feature behind the VerifierService seam with explicit
   evidence-lineage semantics. The logical seam must keep later extraction
   cheap if a trigger fires.
7. **Runtime transport route is session provenance, not agent identity.**
   `AgentDefinition` declares requested model identity/configuration
   (provider logical identity, model identity, prompt policy); the concrete
   resolved route (direct vs legacy, effective protocol, endpoint
   contract/transport profile, resolved endpoint, credential binding
   reference, adapter provenance) belongs to the session's `ModelBinding`,
   produced by `ModelGateway` — so an agent definition cannot become stale
   merely because provider runtime configuration changes.
8. **The bridge exits through V2-02, not by undefined fiat.** V2-02
   introduces the explicit `ProjectRuntimeEnvironmentSpec` product/session
   ingress (project variable names / explicit non-secret values, project
   network/trust requirements, project-secret binding *references*, per-
   declaration provenance — never embedding secret values in durable
   evidence; minimal contract, no UI redesign, no cross-machine secret
   synchronization). The bridge's removal criterion: ordinary Local Project
   execution no longer requires arbitrary legacy ambient inheritance.
9. **Scientific/contained execution contracts are fenced from V2-01 product
   work.** V2-01 must not alter `PreparedEnvironment` behavior/serialization,
   `VerifiedExecutionContext` binding, `ContainmentGuarantee`, WSL/bubblewrap
   execution, QuixBugs/BugsInPy execution, contained-PDB launch-plan
   environment semantics, or frozen scientific environment/evidence identity;
   the directly relevant test suites are NON-REGRESSION gates whenever shared
   runtime modules are touched. The execution-contract fingerprint is
   PRODUCT/local-session metadata initially, fenced out of frozen scientific
   event serialization, treatment identity, hash inputs, evidence manifests,
   and qualification; adding it to frozen scientific evidence requires a
   separate explicit compatibility decision. Compatibility for the
   execution-environment change beyond the bridge is bounded classified
   extension only; no full-environment mode may exist.

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
  children, unchanged model-adapter and Windows interpreter behavior, the
  transitional bridge for compatibility, `VerifiedExecutionContext` untouched,
  and an eleven-item acceptance test set including authority-conflict and
  scientific NON-REGRESSION gates) to close the active execution-boundary
  exposure — `runtime/command_runner.py::_build_env` copies the full worker
  environment (including the forwarded private provider credential variable)
  into user reproduction/verification and verifier command children.
- Stage order: V2-01 (environment + secret isolation, existing credential
  forwarding preserved, LEGACY PROJECT AMBIENT bridge explicit and
  temporary) → V2-02 (Session/AgentDefinition/Executor/VerifierService
  logical seams + capability intersection + `ProjectRuntimeEnvironmentSpec`
  ingress whose adoption retires the bridge) → V2-03 (ModelGateway +
  ModelBinding + truthful, history-derived provider status vocabulary +
  advanced provider-type/endpoint-contract UI disclosure) → V2-04
  (CredentialVault binding/backend seam) → V2-05 (OPTIONAL verifier
  process-isolation evaluation; resolves to NOT JUSTIFIED / DEFERRED unless a
  trigger is evidenced).
- The deterministic controller, PDB subsystem, journal/evidence chain, patch
  policy, disposable-workspace semantics, verifier logic and outcome taxonomy,
  and the verified scientific/contained execution contracts remain
  structurally untouched.
- Scientific treatment qualification remains independent from interactive
  provider availability; `AgentDefinition` never becomes the qualification
  authority for existing frozen scientific treatment identity (treatment
  roster, prompt-profile identity, qualification rules, frozen provenance
  stay authoritative).
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
  Decision item 6.
- **Same-session verifier re-verification:** operator demand showing real
  value in re-running verification over a retained candidate (separate
  product feature behind the VerifierService seam, with evidence-lineage
  semantics).
- **Execution-authority unification:** field evidence that the product
  `ExecutionEnvironment` and the verified execution context diverge in ways
  causing real defects — a shared lower-level primitive may then be extracted
  only under the §6.2a conditions of the full analysis (proven-identical
  semantics, unchanged frozen/scientific behavior, contained-execution tests
  still authoritative).
- **Fallback toward Alternative A (contract hardening only):** inability to
  land V2-01's role-scoped environments without destabilizing the worker
  boundary tests, or real user repro scripts that cannot be satisfied safely
  through the bridge plus explicit declaration — with the credential exposure
  then documented as residual risk.

## V2-01 implementation note (status only — decision unchanged)

V2-01 implements the Decision-item-1 first slice only: the product
`ExecutionEnvironment` authority with `PROJECT_COMMAND` / `PRODUCT_PDB` /
`VERIFIER` role derivations, LEGACY PROJECT AMBIENT bridge identity
`legacy-project-ambient/v1`, explicit threading to Local Project commands,
product PDB (via `build_worker_env`), and verifier commands (via
`command_runner_factory`); conflicting verified/product authorities fail
closed; `runtime/execution.py` and the model-adapter transport are unchanged.
V2-02+ is not implemented. Proxy/TLS provenance: ordinary ambient
`HTTPS_PROXY`/`NO_PROXY`/CA values pass through the V2-01 bridge unchanged
(residual compatibility for V2-02); provider-derived transport overrides are
never merged into project roles.
