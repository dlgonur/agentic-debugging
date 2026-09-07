# Security policy

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue. Contact the
repository owner privately through the security contact shown on the GitHub
profile that owns this repository. Include affected versions, reproduction
steps, impact, and any suggested mitigation.

Do not include live credentials, private datasets, or sensitive machine output
in the report. Redact tokens and personal paths.

## Supported version

Security fixes target the current default branch. Historical research
artifacts and frozen evidence are retained for reproducibility and are not
maintained as deployable services.

## Scope

The project runs model-provided commands only through explicit operator
configuration and documented containment boundaries. Reports about path
escape, command isolation, credential exposure, unsafe patch application,
verifier bypass, or cleanup failure are in scope.

## Architecture and security boundaries

Agentic Debugger enforces strict security and trust boundaries across its
control plane and execution plane:

### 1. ExecutionEnvironment and role-scoped least authority

Child process creation is governed by `ExecutionEnvironment` policy authority,
which derives role-scoped environments with least authority:
- **`PROJECT_COMMAND` & `PRODUCT_PDB`:** Project reproduction, test, and PDB
  subprocesses receive runtime essentials and explicitly authorized project
  variables only. Agentic Debugger control, model, and provider credentials
  are structurally excluded by provenance classification.
- **`VERIFIER`:** Independent verifier child commands execute in clean,
  disposable workspaces with dedicated child processes via `CommandRunner`,
  structurally free of control and provider secrets.
- **`CLEANUP`:** Post-run worktree pruning and cleanup execute under minimal
  Git utility authority.
- **`MODEL_ADAPTER`:** Model adapter subprocesses receive only the exact
  issued credential channel authorized for that request.

On the normal product path, ambient host environment variables are not
inherited into project or verifier roles; projects declare required variables
and secrets declaratively via `ProjectRuntimeEnvironmentSpec` (`ProjEnv`).

### 2. CredentialVault and credential authority

Provider credentials are managed through `CredentialVault`:
- **Binding vs. materialization:** Safe, non-secret `CredentialBinding` records
  (session-stable, safe to serialize in session provenance and events) are
  separated from ephemeral, non-serializable `CredentialLease` handles.
- **Credential non-serialization:** Raw API keys and secret tokens never enter
  tracked files, configuration stores (`config/command-models.json`,
  `provider-catalog-cache.json`), command-line arguments, session manifests,
  durable journals, event payloads, or exported evidence.
- **Storage:** Reusable credentials persist exclusively in the OS secure
  store (Windows Credential Manager) or session memory; changing a provider's
  endpoint while a credential is saved requires explicit key re-entry to
  prevent silent credential rebinding.
- **Opaque in-process tickets:** Sessions pin authorized credentials at launch
  using single-snapshot executable and credential authority.

### 3. Project-secret redaction

When a project defines secrets in its `ProjectRuntimeEnvironmentSpec`, the
runtime enforces bounded redaction markers over terminal streams, diagnostics,
and session journals to prevent project secrets from escaping into model
prompts, transcripts, or review packages.

### 4. Verifier and correctness authority

The independent verifier is the sole correctness authority:
- Verifications execute in fresh, disposable workspaces generated from clean
  source archives, strictly isolated from the controller's working tree.
- Verifier commands run in subprocesses without control-plane or provider
  credentials.
- Physical verifier process isolation remains evaluated and trigger-gated
  behind the four Section 5.6 triggers (lifecycle, environment isolation,
  security boundary, operational cost).

### 5. Configured provider execution

Live model execution is explicit and operator-authorized:
- The provider registry is user-owned; clean installations configure zero
  providers.
- Availability probes (`--doctor`) are offline and presence-only, never
  reading secret bytes or initiating network connections.
- Provider adapters enforce strict agreement between declared authentication
  mode (`bearer`, `anthropic`, `none`), protocol, and endpoint contract before
  dispatching requests.
- No-auth execution (`auth_mode: none`) is restricted to loopback and
  self-hosted endpoints and transmits no credential.
