# BugsInPy Adapter Usage v1

The adapter consumes `research/bugsinpy/PILOT_ELIGIBILITY_MANIFEST_V1.json`,
selects an entry by stable `pilot_task_id`, normalizes its pytest commands, and
maps it into the existing `DebugTask` contract. It does not create a parallel
evaluation model. The normalized `selected_suite_argv` is used for the current
schema's bounded full-suite field when the manifest has no verified broader
suite; this is reported as selected-suite-only behavior.

## Preflight

`BugsInPyAdapter.preflight()` returns deterministic gate evidence. Authorization
requires every gate to be explicitly cleared:

- manifest validity;
- Linux reference platform;
- pinned BugsInPy/project revisions and source identity;
- BugsInPy/project license and notice review;
- exact task Python runtime availability;
- task-local dependency-install boundary;
- normalized pytest command availability;
- OS/process/filesystem/network containment;
- owned external workspace cleanup; and
- reviewed target symbols/breakpoint plan.
- pytest-aware PDB launch plan compatible with the existing pause-target contract.

Missing or unknown facts block execution. Authorization also requires a concrete
`VerifiedExecutionContext`: a prepared task-local dependency result, exact
Python executable/version, reviewed relative cwd/PYTHONPATH/environment, and a
containment runner with denied network/credentials and declared resource limits.
Boolean facts alone cannot authorize benchmark execution. Unit tests use local
mappings and fakes; they never acquire source, install dependencies, or execute
benchmark code.

## External workspace and smoke

`ExternalWorkspace` creates a marker-owned directory under an operator-selected
external parent. It rejects a parent inside the tracked repository, requires
acquisition destinations below its owner marker, and removes only its own
marker-owned root. A selected project remains under `sources/<project>` and is
represented by an external `TaskSource` carrying manifest, BugsInPy revision,
project, bug, buggy-revision, and fixed-revision provenance; it is never labeled
as a curated fixture. `GitSourceAcquirer` is
only called after all preflight gates pass and checks out full pinned revisions.

The operator entry point is metadata-preflight-only:

```text
python -m agentic_debugger.bugsinpy.smoke --manifest research/bugsinpy/PILOT_ELIGIBILITY_MANIFEST_V1.json --task bugsinpy-tqdm-003 --external-parent C:\\tmp\\bugsinpy-smoke-v1
```

Without an explicit facts file this command performs metadata preflight only
This CLI intentionally does not accept the facts-json option for authorization:
ordinary JSON cannot reconstruct a concrete prepared environment or executable
containment runner. It returns `REAL_SMOKE_BLOCKED` instead of deserializing
arbitrary execution objects. The library smoke path requires a task-bound
VerifiedExecutionContext and runner supplied by the operator before it may
obtain the pinned sources, read the evaluator-only official patch, or invoke
the existing verifier, patch, and test-runner lifecycle. It never calls a model,
provider, OpenCode, paid API, or coding agent.

The current manifest does not clear licensing, Linux/runtime, dependency,
containment, target-annotation, execution-context, or PDB-planning gates.
Therefore no external source or dependency was acquired for this v1
implementation.

## Supported and deferred

Supported: strict eight-entry manifest validation, stable task selection,
pytest argv normalization, one F2P plus reviewed P2P candidates, bounded
selected-suite mapping, explicit preflight evidence, task-bound dependency and
runtime identity, external ownership and provenance, verified-context command
binding, approved public HTTPS pinned acquisition, and no-model official-patch
verifier integration.

Deferred: unittest/tox translation, verified broader project suites, PDB launch
or reachability execution, environment creation, OS-level containment
implementation, license clearance, and the eight-task model campaign.
