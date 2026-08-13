# BugsInPy Adapter Design v1

Status: design only. This document does not implement an adapter, acquire a project checkout, install dependencies, or execute a benchmark task.

The adapter is a narrow external-dataset source and manifest bridge around the existing controller, runtime, PDB session, patch manager, verifier, event, and live-report contracts. It is not a second evaluation framework. The pilot input is [`research/bugsinpy/PILOT_ELIGIBILITY_MANIFEST_V1.json`](../research/bugsinpy/PILOT_ELIGIBILITY_MANIFEST_V1.json), pinned to the official BugsInPy metadata snapshot recorded there.

## 1. Current contracts and the adapter boundary

The live source establishes these constraints:

| Existing contract | Consequence for BugsInPy |
| --- | --- |
| `DebugTask` schema `1.0` accepts Python only, relative paths, a curated fixture prefix, one F2P node, at least two P2P nodes, explicit reproduction and suite argv, denied test/task writes, and an evaluator-only oracle. | An external task cannot be loaded by copying a `task.json` into the repository or by weakening path validation. The adapter needs a versioned external-source resolution boundary. |
| `evaluation.runner.load_task` is the authoritative task loader. | External manifests must be parsed and validated once, then converted into the same logical task object used by the controller. |
| `TaskWorkspace` copies a source directory into a disposable workspace, rejects symlinks and traversal, and cleans it. | The acquired BugsInPy checkout is an immutable source/cache outside the repository; each case receives a fresh materialization. |
| `CommandRunner` uses argv, `shell=False`, bounded output, timeouts, and process cleanup. | Test and setup commands must be argv-normalized and run through this boundary. Shell scripts are recipes to audit/translate, not arbitrary agent actions. |
| `PdbSession.start_paused_target(script, breakpoints, argv)` launches one bounded target and pauses at declared source lines. | PDB entry must be an adapter-prepared pytest driver or a direct Python target that deterministically invokes the selected failing test. It is not enough to pass a project test command to a generic debugger. |
| `EvaluationVerifier` independently checks baseline, reproduction, selected nodes, patch/syntax, post-patch checks, full-suite consistency, cleanup, and canonical fixture integrity. | The adapter supplies source roots and normalized test plans; correctness remains verifier-owned. |
| `live.py` records controller state, PDB observations, cleanup, bounded requests, and report status; protocol `1.3` is authoritative. | Dataset provenance, environment fingerprint, task source identity, and execution gates belong in deterministic case metadata, not in provider text or hidden model hints. |

The smallest compatible shape is therefore:

1. Parse the external manifest into an immutable `BugsInPyTaskSpec`.
2. Resolve and verify a pinned project source outside the repository.
3. Prepare an isolated environment from a reviewed, hashed recipe.
4. Materialize a disposable task workspace and a normalized `DebugTask` view.
5. Pass that view and the workspace source to the existing controller/tool registry and verifier.
6. Destroy the case environment and record the evidence regardless of outcome.

The first implementation should make the source-root dependency explicit in the evaluator rather than manufacturing a fake curated path. A versioned `DebugTask` external-source field or a typed `TaskSource` passed alongside `DebugTask` is required; the choice must preserve strict relative paths and fail closed when a source is not a verified materialization. Do not silently reinterpret `fixture_path` or place benchmark code under `agentic_debugger/datasets/curated/`.

## 2. Manifest fields required by the adapter

The v1 manifest already records the research-facing fields. The adapter needs the following machine-validated subset, with unknown values rejected before execution:

- stable `pilot_task_id`, dataset project, integer bug ID, official project URL, metadata snapshot revision, buggy revision, fixed revision, and source metadata paths;
- exact Python version, project-relative `pythonpath`, dependency recipe path and hash, setup recipe identity and hash, platform target, and environment fingerprint inputs;
- reproduction argv/cwd/timeout, one exact F2P node, at least two candidate P2P nodes, an explicit bounded selected-suite argv, and an independently recorded full-project command or an explicit `full_suite_status: unavailable`;
- allowed source files derived from the isolated patch, denied test/metadata/build paths, network and external-service policy, per-command and per-case resource limits;
- candidate target files, reviewed target symbols, breakpoint lines, PDB script/argv, and `pdb_reachability_status`;
- bug-family annotation with evidence basis and confidence, plus a separate root-cause oracle that is never included in `DebugTask.agent_visible_mapping()`;
- license/notice provenance, redistribution status, acquisition status, and unresolved verification items;
- source references, content hashes, and a deterministic manifest-entry fingerprint.

The adapter must distinguish `unknown`, `unverified`, `possible`, `verified`, and `rejected`. A missing value must not be coerced into a safe default. In particular, unknown network, service, database, native-library, GUI, runtime, or nondeterminism requirements are execution gates, not false values.

## 3. Acquisition and checkout boundary

Acquisition is operator-side and occurs outside the repository. The first slice should support a pinned Git source only:

- Resolve the project URL and exact buggy revision from the manifest; do not follow the moving default branch.
- Use a pre-approved acquisition mechanism to fetch source into an external cache. Do not run project code during acquisition.
- Verify the revision, repository identity, and recorded source hash before making a case workspace.
- Keep the fixed revision and isolated gold patch in evaluator-only storage. The model receives neither the fixed revision nor the patch.
- Reject submodules, symlinks, generated vendored code, or checkout paths that escape the external cache policy until explicitly handled.
- Copy or materialize into a per-case workspace only after acquisition verification. The canonical cache is read-only; the case workspace is disposable.

The existing `TaskWorkspace` is a useful lifecycle primitive but its source path and repository-root assumptions are trusted-local. The adapter must add an external source resolver and a containment-aware workspace factory rather than bypassing `TaskWorkspace` or passing an arbitrary absolute path through a manifest field.

## 4. Environment preparation boundary

Environment preparation is separate from task execution and from model control. A reviewed preparation step may create an isolated interpreter/environment outside the repository, install only the pinned dependency set, and record:

- OS image or host platform, Python executable identity, `sys.version`, package lock/requirements hash, installer version, and environment fingerprint;
- whether a dependency came from a wheel, source build, VCS URL, or local path;
- setup commands translated from `setup.sh`/`tox` into a bounded, non-interactive recipe;
- network access used during preparation and the exact transition to network-denied execution.

The first slice targets a Linux reference environment because the official recipes include POSIX shell, `python3`, `tox`, and VCS-style dependencies. Windows support is a separate gate, not an implied property of the current Windows development checkout. A task whose official setup cannot be translated without shell, network, native build, or interactive behavior is rejected for the pilot.

## 5. Test discovery and F2P/P2P/full-suite mapping

The official `run_test.sh` command is the source of the F2P identity. For the selected slice, all eight commands are pytest-compatible, but the command strings use `pytest` or `python3 -m pytest` and sometimes a project `pythonpath`. The adapter must normalize these into argv without shell expansion.

Mapping rules:

1. **F2P:** use exactly one official failing node. The baseline must fail on the buggy revision with a genuine pytest `FAILED` result and no collection error, skip, xfail, or external-service error.
2. **P2P:** choose at least two nodes from the official project pass inventory, then verify on the buggy revision that they are genuine passes and do not overlap F2P. A pass-list entry is a candidate oracle, not proof that it passes in every other bug revision.
3. **Collection:** run the normalized selected suite with `--collect-only`; require every declared node exactly once. This is necessary because the current verifier uses exact node identity.
4. **Bounded regression suite:** map F2P plus reviewed P2P nodes to the current `Tests.fail_to_pass`, `Tests.pass_to_pass`, and `Tests.full_suite_argv` fields. Until the verifier’s current exact-count semantics are extended, this field is a declared selected regression suite, not an assertion that the whole project suite ran.
5. **Official full suite:** preserve the BugsInPy/framework full-project command as a separate provenance field. Run it only after selected-node verification and only when its duration, skips, errors, network behavior, and output parser are bounded. Do not label the bounded selected suite “full project” merely to satisfy the current schema.
6. **Framework variants:** the first slice is pytest-only. Unittest and tox entries require a later adapter translator and independent output/collection contracts; they are not silently passed through pytest parsing.

The manifest’s `full_project_suite` values are currently unknown. That is an explicit readiness gate, not an omission to fill with `pytest` by assumption.

## 6. Debugger and pytest-aware launch

PDB is a separate execution path from ordinary reproduction. The adapter should prepare a small driver inside the disposable workspace that:

- invokes the exact selected failing node through the pinned interpreter and pytest configuration;
- sets a breakpoint only in an adapter-reviewed changed source function/line;
- preserves the project root, `pythonpath`, pytest import mode, environment variables, and test fixture setup needed to reach that line;
- suppresses or bounds pytest/plugin output and does not expose hidden oracle data;
- starts through `PdbSession.start_paused_target` with a relative script and argv, then records pause state, script, line, function, stack, and bounded locals;
- stops the session and removes the driver before case cleanup.

Do not use pytest’s interactive `--pdb` as a substitute for the existing persistent PDB protocol. `--pdb` may be useful as an offline reachability diagnostic, but the controller path needs a deterministic pause and cleanup contract. A task with no stable changed line reached by the selected test is not PDB-eligible even if its test passes after a patch.

## 7. Patch and verifier integration

The agent-visible task contains the title/description, reproduction plan, allowed source paths, and test contract but not the fixed revision, `bug_patch.txt`, root-cause oracle, target symbol oracle, or expected patch. The adapter derives `constraints.allowed_write_paths` from reviewed source-only patch paths and always denies tests, task metadata, dependency manifests, setup scripts, cache directories, and generated artifacts.

Candidate patches flow through the existing `PatchManager`. The independent verifier then:

- creates a fresh buggy workspace;
- checks the declared node collection and genuine baseline failure;
- applies only the candidate patch to authorized source paths;
- runs syntax checking, post-patch reproduction, F2P, P2P, and bounded selected-suite checks;
- optionally runs the separately recorded full project suite under its own gate;
- classifies `RESOLVED`, `BREAKING_RESOLVED`, `PARTIALLY_RESOLVED`, `WORK_IN_PROGRESS`, `NO_OP`, or `REGRESSION` through the existing taxonomy;
- records patch digest, command argv/cwd, exit code, bounded output, timing, timeout, cleanup, and canonical-source immutability.

The gold patch is used to validate the adapter’s source-path and oracle derivation before a campaign and for post-hoc diagnosis only. It is never supplied to the controller or model.

## 8. Immutability, cleanup, timeout, and resources

Each case receives unique evaluation, case, run, trajectory, and request identities through the existing live harness. The external source cache is immutable; the workspace, environment overlay, PDB driver, logs, and test caches are disposable.

Required controls are:

- wall-clock deadline for setup, each command, PDB startup/request/shutdown, controller phase, and whole case;
- process-tree termination on timeout on both platforms, with a post-termination process check;
- bounded stdout/stderr and diagnostic payloads, inherited from `CommandRunner`/PDB plus an adapter-level case cap;
- CPU, memory, disk, process-count, open-file, and workspace-size limits supplied by the containment layer;
- network denied during task execution, with explicit local-loopback policy only if a task’s verified fixture requires it;
- cleanup in `finally` for PDB sessions, subprocesses, workspace, environment overlay, temporary drivers, sockets, and logs;
- a cleanup failure that leaves unknown state is a case failure and blocks further external execution until investigated.

The current command and workspace classes provide timeouts, output bounds, argv execution, symlink rejection, and best-effort cleanup. They do not provide OS-level memory/CPU/network/filesystem isolation; that missing capability is a pre-execution gate.

## 9. Windows versus Linux

The official snapshot contains old Python versions, POSIX shell recipes, `python3`, `tox`, VCS dependencies, and project-specific environment assumptions. The first adapter implementation should therefore be Linux-reference only and record a hard platform gate. Windows may be supported later only when:

- every setup/test command is translated to argv without shell-specific semantics;
- path separators and `os.pathsep` behavior are explicit;
- the old interpreter and dependency wheels are available without unreviewed native builds;
- pytest collection/output and PDB breakpoint paths are stable;
- process-tree, network, resource, and cleanup controls are equivalent.

The existence of a Windows-compatible current `CommandRunner` or `PdbSession` does not establish benchmark compatibility.

## 10. Containment and untrusted code

BugsInPy source, tests, setup scripts, and dependencies are third-party executable code. The current runtime is documented as a trusted-local evaluator and is not an OS-level hostile-code sandbox. Before any benchmark task executes, require a separate containment layer with:

- an unprivileged account/container or equivalent process boundary;
- read-only source/cache mounts and a writable per-case workspace;
- no host credentials, agent configuration, repository `.git`, or broad filesystem visibility;
- default-deny network and controlled loopback, DNS, subprocess, file, device, and IPC policy;
- CPU, memory, disk, process, file descriptor, and wall-clock limits;
- immutable audit logs outside the benchmark workspace;
- forced teardown and verification that no descendant process or socket remains.

Docker is an official BugsInPy setup recommendation, but using Docker alone is not an acceptance argument. The implementation must document the image digest, user, mounts, capabilities, network mode, resource limits, and teardown checks. No container image is created by this task.

## 11. Deterministic evidence and failure taxonomy

Every case must record a canonical JSON evidence record containing manifest fingerprint, official metadata revision, project/bug/revision, environment fingerprint, platform, policy, repetition, command argv/cwd, selected node identities, baseline/post results, patch digest, PDB lifecycle and observation count, controller/live status, verifier outcome, resource/timing summaries, cleanup state, and source immutability result. Normalize temporary paths, timestamps only in timing fields, and bounded command output; never record secrets or raw provider credentials.

At minimum, report these failure classes separately:

- metadata/schema invalid;
- source acquisition or revision mismatch;
- license/redistribution gate;
- dependency/environment preparation failure;
- platform/setup translation failure;
- baseline collection failure or non-genuine F2P/P2P result;
- network, external-service, database, GUI, native-library, or nondeterminism violation;
- pytest node discovery/output parse failure;
- PDB unreachable, breakpoint mismatch, session/protocol failure, or cleanup failure;
- controller rejection, invalid directive, model/provider transport error, timeout, or incomplete report;
- patch authorization/application/syntax failure;
- post-patch test timeout/error, F2P miss, P2P regression, full-suite contradiction, or verifier failure;
- workspace/process/resource containment failure.

These are not all “model failures.” The existing `LiveCaseStatus`, verifier status, outcome taxonomy, event schema, and cleanup fields should remain the authoritative layers.

## 12. First implementation slice

The smallest useful implementation is one offline, no-model adapter preflight for the eight manifest entries:

1. strict manifest parser and fingerprint;
2. pinned metadata/provenance validator;
3. external source acquisition interface with a fake/local metadata-only implementation for tests, but no benchmark execution in this design task;
4. Linux-only environment recipe and containment preflight that fails closed;
5. pytest command/node normalizer for the exact F2P and P2P contract;
6. external workspace factory integrated with the existing `TaskWorkspace`/`TestRunner`/`PatchManager`/`EvaluationVerifier` lifecycle;
7. PDB driver builder and reachability preflight using the existing `PdbSession` contract;
8. deterministic evidence and failure-taxonomy record integrated with existing live/evaluation report schemas;
9. curated five-fixture smoke gate before any external task is admitted.

The first real execution, when separately authorized, should be one task, one policy, one repetition, with no live model: baseline, PDB reachability, gold-patch oracle check, verifier, cleanup, and evidence. Expand to the eight-task paired pilot only after every task passes the same gate.

## 13. Explicitly deferred

- implementing the adapter or modifying `agentic_debugger/`;
- executing BugsInPy tasks, installing dependencies, building environments, or creating Docker images in this task;
- Windows parity and unittest/tox adapter support;
- full-project suite execution when its command/output/timeout contract is unknown;
- arbitrary shell recipe execution, networked tests, external services, databases, GUI/native-library tasks, and long-running or nondeterministic tasks;
- broad BugsInPy sampling, train/test splits, fine-tuning, RAG, DPO/RLHF, or general dataset research;
- adaptive PDB gating, new controller policy work, and any revisit of accepted Task 10B-R5;
- causal static-versus-PDB claims before a real PDB-enabled path is proven and the paired pilot is separately authorized.
