# BugsInPy Metadata-Only Preflight v1

## Purpose

The preflight is the fail-closed authorization boundary for BugsInPy. It reads
only the tracked pilot manifest, canonical licensing gate, and tracked validator.
It does not acquire a repository, resolve an upstream path, inspect a patch,
create a workspace or environment, form a dependency/containment command, run
tests, or contact a model/provider.

The earliest shared integration point is `NoModelSmokeRunner.run`: the
authority-backed decision is made before `ExternalWorkspace.create`, either
`GitSourceAcquirer.acquire` call, official patch reading, verifier creation, or
execution. A blocked decision returns `REAL_SMOKE_BLOCKED` and no collaborator
is called.

Direct `GitSourceAcquirer.acquire` and `read_gold_patch` calls are separately
protected. They require an immutable `BugsInPyOperationPermit` issued by an
ALLOWing preflight. The permit binds task ID, operation, the resolved manifest,
gate, and canonical-validator snapshot, exact source URL/revision pairs, exact
patch metadata paths, and deterministic decision run ID. The scope is derived
from the selected manifest task; it is not a project allowlist. A successful
acquisition returns an issuer-bound `AcquiredSourceReceipt`; patch reading
accepts that receipt, not an arbitrary directory. The receipt binds the task,
URL, revision, resolved root, authority snapshot, acquisition run ID, and
acquisition permit identity. Only the official BugsInPy framework receipt at
the pinned authority revision can authorize patch reading. The acquirer
re-reads both authorities, hashes all three authority files, runs the tracked
validator, and compares the complete snapshot before destination creation, Git
calls, path resolution, or file opening. A verdict-only change revokes a permit
even when the revision is unchanged. Its constructors require private issuer
sentinels; caller-constructed decision fields do not create capabilities.
`ExternalWorkspace` remains generic because licensed QuixBugs paths reuse it.

## Authorities

- `research/bugsinpy/PILOT_ELIGIBILITY_MANIFEST_V1.json`
- `research/bugsinpy/BUGSINPY_LICENSE_GATE_V1.json`
- `scripts/validate_bugsinpy_license_gate.py`

The validator is loaded and called in-process against the two bounded JSON
objects. Production always loads this exact tracked path; neither the CLI nor
the public preflight constructor accepts an arbitrary validator path or
callable. Missing, changed, or unloadable validator bytes, missing files,
malformed JSON, validator failure, duplicate task IDs, and mismatched authority
revisions fail closed.

## Operation vocabulary

The supported operations are `inspect_metadata`, `acquire_source`,
`checkout_revision`, `prepare_dependencies`, `start_containment`,
`reproduce_bug`, `run_debug_policy`, `verify_patch`, `package_evidence`, and
the narrowly scoped `package_metadata_evidence`.

`inspect_metadata` may be allowed after both authorities validate.
`package_metadata_evidence` is allowed only when `evidence_handling` is exactly
`sanitized_metadata_only`; source-bearing, raw-upstream, unspecified, and
unknown values are blocked. Every other operation requires exactly
`unspecified`; explicit sanitized, source-bearing, raw-upstream, or unknown
values are blocked. Every source, dependency, containment, execution,
debugging, verification, or source-bearing evidence operation is currently
blocked. `package_evidence` is blocked for every handling value and is never
the metadata-only operation.

## Decision schema

`MetadataPreflightDecision.to_mapping()` returns schema `1.0` with task and
operation identity, `ALLOW`/`BLOCK`, a stable `reason_code`, bounded reason
text, manifest/dataset/project/task verdicts, formal license status,
redistribution and private-use verdicts, operational gate, operator state,
containment/dependency readiness, authority revisions and paths, validation
status, a deterministic `run_id`, the immutable authority snapshot, and the
derived authorization scope. A permit is intentionally not serialized.

Downstream code must branch on `decision` and `reason_code`; it must not parse
the reason text.

## Stable reason codes

`ALLOWED_METADATA_INSPECTION`, `ALLOWED_SANITIZED_METADATA_EVIDENCE`,
`OPERATION_ALLOWED`,
`TASK_ID_REQUIRED`, `UNKNOWN_TASK`, `UNKNOWN_OPERATION`, `AUTHORITY_MISSING`,
`AUTHORITY_JSON_INVALID`, `MANIFEST_INVALID`, `LICENSE_VALIDATOR_FAILED`,
`AUTHORITY_REVISION_MISMATCH`, `TASK_VERDICT_EXCEEDS_AUTHORITY`,
`DATASET_VERDICT_BLOCKED`, `TASK_VERDICT_BLOCKED`,
`OPERATIONAL_GATE_BLOCKED`, `AFFIRMATIVE_VERDICT_REQUIRED`,
`OPERATOR_AUTHORIZATION_REQUIRED`, `CONTAINMENT_NOT_READY`,
`DEPENDENCY_NOT_READY`, `SOURCE_BEARING_EVIDENCE_PROHIBITED`,
`EVIDENCE_HANDLING_REQUIRED`, `RESOURCE_SCOPE_INVALID`,
`AUTHORITY_SNAPSHOT_MISMATCH`, `NONCANONICAL_AUTHORITY`, and
`PROJECT_PERMISSION_REQUIRED`, `DATASET_PERMISSION_REQUIRED`,
`TASK_PERMISSION_REQUIRED`, `OPERATIONAL_PERMISSION_REQUIRED`,
`PRIVATE_USE_PERMISSION_REQUIRED`, `FORMAL_LICENSE_PERMISSION_REQUIRED`, and
`UNSUPPORTED_OVERRIDE` are stable vocabulary.
Unknown operations, unsupported
override/bypass flags, and absent task IDs are rejected before any operation.

Operator approval is an input for a future affirmative execution decision; it
is not an override. It cannot clear a `BLOCKED` dataset, task, redistribution,
or operational verdict.

## Current result and future gates

The current authority remains: BugsInPy formal license `UNKNOWN`, redistribution
`BLOCKED`, private local research use `UNKNOWN`, operational execution gate
`BLOCKED`, all eight selected task verdicts `BLOCKED`, and overall pilot verdict
`BLOCKED`. Therefore all eight tasks allow metadata inspection and block every
source-bearing or execution operation.

Execution may be reconsidered only after the canonical gate is revised by its
authority, the tracked validator passes the revised manifest/gate pair, the
dataset/project/task/operational verdicts are affirmative, formal license and
private local research-use permission are affirmative (there is no separate
local-use permission basis in the current schema), explicit operator approval
is recorded, containment readiness is affirmatively demonstrated, dependency
readiness is affirmatively demonstrated, and evidence handling is limited to
the operation's permitted vocabulary. Redistribution clearance is not a
substitute for private-use permission and is not required solely for private
local execution; `package_evidence` remains independently prohibited. None of
those future gates is approved by this task, and no containment implementation
or execution is authorized here.

## Evidence restrictions

Tracked files and `_ai-review/` may contain only sanitized task IDs, URLs,
exact revisions, paths, hashes, verdicts, validation outcomes, and aggregate
results. Do not include upstream source, patches, tests, environments, raw
logs, candidate diffs, credentials, caches, or raw third-party license text.

Run the local command from the repository root, for example:

```text
python -m agentic_debugger.bugsinpy.preflight_cli --task bugsinpy-tqdm-003 --operation inspect_metadata
```

The command prints exactly one JSON decision. It exits 0 only for `ALLOW` and
nonzero for `BLOCK`, invalid input, or authority-validation failure. Semantic
unknown operations, absent/unknown tasks, unsupported authorization, and
invalid evidence handling are sent through the preflight rather than being
intercepted by argparse.
