# BugsInPy Pilot Readiness v1

## Verdict

**READY WITH EXPLICIT GATES**

The research specification is ready for a bounded adapter implementation. The external pilot is not ready for task execution. The manifest defines exactly eight proposed tasks from four BugsInPy projects and seven patch-derived bug families, but no task has execution-level eligibility yet.

## Task verification status

All eight tasks are sufficiently verified at the metadata level: the pinned official BugsInPy snapshot contains each `project.info`, `bug.info`, `run_test.sh`, requirements recipe, and isolated source patch. The official metadata supplies the project, bug ID, buggy revision, fixed revision, Python version, failing test file, and reproduction entry point.

None of the eight is sufficiently verified for execution. Baseline failure, P2P preservation, exact pytest collection, full-suite behavior, target symbols, breakpoint reachability, cleanup, containment, dependency provenance, and licensing remain unverified. The pass-list entries in the manifest are candidate P2P nodes, not execution results.

The proposed tasks are:

| Project | Task IDs | Family coverage | Current status |
| --- | --- | --- | --- |
| FastAPI | `bugsinpy-fastapi-001`, `bugsinpy-fastapi-009` | API/data-shaping contract | metadata verified; execution gated |
| HTTPie | `bugsinpy-httpie-001`, `bugsinpy-httpie-002` | filesystem/name-length boundary; request-policy/control-flow propagation | metadata verified; local-service/network risk gated |
| tqdm | `bugsinpy-tqdm-002`, `bugsinpy-tqdm-003` | terminal/display representation; object protocol/truthiness contract | metadata verified; old Python/toolchain gated |
| thefuck | `bugsinpy-thefuck-001`, `bugsinpy-thefuck-002` | quoted-token parser boundary; platform path-separator compatibility | metadata verified; process/environment determinism gated |

## Remaining assumptions

- The official metadata revision `11c5f1eea954a42132cfd06bf257766a7963e0fd` remains the pinned source of task definitions.
- A Linux reference environment will be used first. Windows compatibility is not assumed.
- The official `run_test.sh` command identifies the F2P node, but the current repository’s verifier needs an adapter-normalized argv and exact pytest collection.
- The official project pass inventories identify candidate P2P nodes; they do not establish that those nodes pass on each selected buggy revision.
- The current `DebugTask`/verifier path is curated-only and trusted-local. An external source-root and containment boundary must be implemented before execution.
- Target symbols, breakpoint lines, full-project suite commands, resource limits, dependency hashes, and environment fingerprints must be reviewed per task.

## Licensing conclusion

Licensing does not currently clear the intended workflow. No root `LICENSE` file was present in the inspected official BugsInPy snapshot, and the underlying project licenses/notices were not independently verified. This does not establish that local research use is prohibited, but it means the repository must not redistribute BugsInPy metadata, project sources, environments, or generated artifacts until BugsInPy terms and every selected project’s terms/notices are reviewed and recorded. Local acquisition/execution is therefore a pre-execution gate, not an unconditional permission.

## Checks required before any task execution

1. Keep the official BugsInPy source and any project checkout outside this repository; pin and hash the metadata, project revision, dependency recipe, and environment.
2. Obtain a documented OS/process/filesystem/network/resource containment boundary. Docker is only an implementation option; its image digest, user, mounts, capabilities, network, limits, and teardown checks must be recorded.
3. Pass the existing five curated fixtures as the architecture smoke gate, including verifier, PDB lifecycle, replay, cleanup, and fixture immutability checks.
4. Run a metadata-only adapter preflight for all eight entries and fail closed on unknown license, source, platform, setup, or command fields.
5. For each candidate, establish genuine buggy F2P failure, at least two genuine buggy P2P passes, exact pytest collection, fixed-revision regression pass, bounded selected-suite behavior, and cleanup.
6. Review changed source files, target symbols, breakpoint lines, pytest-aware PDB driver behavior, and bounded runtime evidence for each task.
7. Prove no external network/service/database/GUI/native-build/long-runtime/nondeterminism requirement remains in the selected execution path. Replace and record any task that fails this check.

## Exact minimum implementation task that follows

Implement **“BugsInPy adapter preflight v1 — manifest parser, pinned external source/workspace boundary, pytest F2P/P2P normalization, and deterministic evidence”**. It must remain Linux-reference, offline during task execution, no-model, one-task-at-a-time, and fail closed. It should add no BugsInPy benchmark execution to this documentation change and should not begin the 32-case model matrix. The first execution authorization should occur only after that preflight and the containment gate are accepted.
