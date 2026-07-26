# Task 9 — First End-to-End Demonstration

Status: implemented on `feature/mvp-end-to-end-demo-v1`.

This document describes the repeatable local demonstration of the agentic
debugging MVP, how to run it, what it measures, and — importantly — what its
results do **not** establish.

---

## 1. What the demonstration is

The demonstration runs the complete curated benchmark set through the real,
already-accepted MVP components and produces evidence-backed results for two
policy variants.

It is orchestration and measurement only. It adds no debugging capability, and
it contains no fixture-specific shortcut around the controller, tool policy,
runtime, patch lifecycle, debugger or verifier.

| Concern | Component actually executed |
|---|---|
| Task loading and validation | `evaluation.task_schema` via `evaluation.runner.load_task` |
| Controller loop, budgets, allowlists | `agent.controller.DeterministicController` |
| Action/state policy | `agent.controller_policy` |
| Debugger access decision | `agent.controller_policy.decide_pdb_access` |
| Tool dispatch boundary | `agent.tool_registry.ToolRegistry` |
| Disposable workspaces | `runtime.workspace.TaskWorkspace` |
| Test/reproduction execution | `runtime.test_runner.TestRunner` → `runtime.command_runner` |
| Source retrieval | `skills.search_skills`, `skills.file_skills` |
| Patch apply / syntax check | `runtime.patcher.PatchManager` |
| Bounded runtime evidence | `runtime.pdb_session.PdbSession` |
| Outcome taxonomy | `evaluation.outcome_taxonomy.classify_outcome` |
| Event trajectory | `agent.trajectory.project_controller_run` → `events.logger` |
| Trajectory validation | `events.replay.replay_events` / `semantic_projection` |
| Authoritative verification | `evaluation.verifier.EvaluationVerifier` |
| Diagnostic path redaction | `evaluation.runner.normalize_output` / `bounded_error` |

New code for Task 9 lives entirely in `agentic_debugger/demo/`, plus one shared
projection helper (`agentic_debugger/agent/trajectory.py`) that the Task 8
golden harness and Task 9 now both use so their trajectories cannot drift. The
curated reference repairs also moved out of the Task 8 test helper into
`agentic_debugger/demo/catalog.py` for the same reason; the Task 8 golden
artifacts pin the resulting patch hashes, so a catalog edit fails loudly.

---

## 2. Entry point and reproduction

```powershell
python -m pip install -e .[test]
python -m agentic_debugger.demo --output-dir demo-out
```

Useful options:

```powershell
python -m agentic_debugger.demo --list-tasks
python -m agentic_debugger.demo --output-dir demo-out --task-id curated-off-by-one-002
python -m agentic_debugger.demo --output-dir demo-out --policy static-baseline
python -m agentic_debugger.demo --output-dir demo-out --strict
```

Requirements: Python >= 3.11, `pytest` importable by the same interpreter, and
`python` resolvable on `PATH` (curated manifests invoke `python -m pytest`).
No credentials, no paid service and no network access are required or used.

Exit codes: `0` on success; `1` if any case hit a harness error, or — with
`--strict` — if any case did not complete, any verifier result is not
`COMPLETED`, or any verifier outcome is not `RESOLVED`; `2` for invalid input,
including a malformed curated manifest.

### Produced artifacts

| Path | Contents |
|---|---|
| `results.json` | Machine-readable results for every case |
| `technical-evaluation-summary.md` | Human summary generated *from* `results.json` |
| `REPRODUCE.md` | Clean-checkout reproduction instructions for that exact run |
| `trajectories/<case>.events.jsonl` | The raw controller trajectory |
| `trajectories/<case>.semantic.json` | The stable semantic projection |

The Markdown summary is rendered from the same mapping that is serialized to
`results.json`, so the two documents cannot disagree.

---

## 3. What one case does

A *case* is one curated task under one policy. Each case:

1. installs the in-process offline guard (section 7) and digests the canonical
   fixture;
2. copies the canonical fixture into a disposable workspace (the canonical
   fixture is never written to);
3. renders the reference repair as a unified diff **from the workspace bytes**,
   so a drifted fixture is a hard error rather than a stale patch;
4. for a PDB-enabled policy only, prepares a second disposable copy with one
   appended module-level driver, and resolves the breakpoint from the fixture
   AST;
5. runs the controller: reproduce → understand → (gate) → optional runtime
   evidence → patch → validate;
6. re-digests the canonical fixture, so the demonstration reports fixture
   immutability across the controller phase on its own authority;
7. projects the run into events, writes JSONL, and validates it through replay;
8. hands the same candidate diff to the Task 7 verifier, which re-evaluates it
   from an **independent clean baseline** and produces the authoritative
   outcome;
9. releases every workspace, subprocess and debugger session, on success and on
   failure alike.

Note that step 4 is skipped for the static baseline, so that policy has no
debugger target available even before the gate is consulted. The gate is still
consulted and still denies, but probe availability duplicates the policy
decision — a deliberate belt-and-braces arrangement, not an independent one.

### The two policy variants

| Demonstration policy | `PdbPolicy` | Expected gate behaviour |
|---|---|---|
| `static-baseline` | `DISABLED` | gate denies with `policy_disabled` |
| `pdb-on-uncertainty` | `ON_UNCERTAINTY` | gate allows a low-confidence hypothesis that declares it needs runtime state |

The gate decision is taken by the real `decide_pdb_access` function over the
live snapshot (controller state, reproduction status, remaining PDB budget,
patch attempts, the active hypothesis) and every decision is recorded in
`results.json`, including the hypothesis confidence and runtime-evidence flag
that fed it.

One caveat worth stating plainly: the offline model always declares a
low-confidence hypothesis that requires runtime state, so under
`ON_UNCERTAINTY` the gate can only ever answer `allowed` (or deny on an
exhausted budget). The demonstration therefore observes the gate *allowing*
under uncertainty and *denying* under `DISABLED`, but never observes
`uncertainty_not_established`.

`ALWAYS_ON` and `AFTER_FAILED_PATCH` are implemented in the policy module but
are deliberately **not** demonstrated: with a single fixed candidate repair
there is no failed first attempt to trigger `AFTER_FAILED_PATCH`, and
`ALWAYS_ON` would only duplicate the `ON_UNCERTAINTY` trajectory. They belong
with real-model or multi-attempt work.

---

## 4. The offline model stand-in

There is **no model in the loop**. Real-model execution is separately approved
work and is not performed here.

`agentic_debugger.demo.model.DemoPolicyModel` replaces the model. Its fixed
outputs come from `agentic_debugger.demo.catalog`:

* the localization claim (file + symbol);
* the root-cause statement;
* the reference repair, stored as exact `old`/`new` source snippets;
* the runtime probe (focus function, call, breakpoint anchor, safe-eval
  expressions).

Two things the adapter decides at run time from live state, not from a script:

* whether the failure reproduced, read from the reproduction observation;
* whether the debugger may be used, delegated to `decide_pdb_access`.

It also reacts honestly to tool failures: a rejected patch, a failed syntax
check, an unavailable debugger session or an unexpected validation outcome each
drive the controller down a different, recorded path. Every debugger step is
issued one at a time and its observation inspected before the next directive,
so an abandoned debugger session is recorded as abandoned rather than silently
counted as evidence.

The adapter deliberately makes no claim it cannot support. When runtime
evidence has been collected it revises its hypothesis to record only that fact:
the statement is unchanged, the confidence is **not** raised, and the cited
evidence references are exactly the observations that actually succeeded.

**Consequence to keep in view:** because both policies receive the *same*
candidate repair for a task, identical repair outcomes are expected by
construction. That is a property of the harness, not a finding.

---

## 5. What is measured

Per case, `results.json` records:

* **Outcome** — verifier status, semantic outcome, F2P/P2P totals, full-suite
  status, plus the controller's own independent validation classification.
* **Localization** — the declared claim, the oracle targets, the files the patch
  actually changed, and the scored category.
* **Patch** — attempted, applied, changed files, SHA-256, syntax result.
* **Tests** — controller-side test runs and verifier command/test counts.
* **Runtime** — wall-clock milliseconds, in the separate `timing` section.
* **Model calls** — calls into the offline adapter (not provider requests).
* **Debugger use** — gate decisions (with the hypothesis inputs that fed them),
  session start, PDB action counts, inspection calls *attempted* versus
  inspection calls that actually *succeeded*, and which observations were
  collected.
* **Honesty fields** — tool errors, harness diagnostics, replay validity,
  observation status counts, offline-guard counters, and the canonical fixture
  digest before and after the controller phase.

Two counts that look similar are deliberately kept apart:
`controller.action_count` is every action the controller issued, including any
the registry rejected before a handler ran, while `controller.tool_call_count`
is only those that reached a demonstration handler. The same distinction
applies to `pdb_observation_attempts` versus `pdb_observations_succeeded`.

Token and cost fields are absent because no metered service is used. "Cost"
here means call and command counts; no per-action latency is measured.

Localization is reported both as a single flat category and as the individual
facts behind it (`claim_file_matches_oracle`, `claim_symbol_matches_oracle`,
`patch_within_oracle_files`, `patch_applied`), because one flat category cannot
express both where the claim pointed and whether a patch was produced.

### Runtime-evidence categories

The accepted evaluation design lists categories such as *PDB useful / changed
diagnosis* and *PDB confirmed existing diagnosis*. This demonstration cannot
honestly emit either, because the diagnosis is fixed before the run and nothing
compares the collected values against it. It emits only:

`PDB_NOT_REACHED`, `PDB_NOT_USED`, `PDB_TOOL_FAILURE`,
`PDB_EVIDENCE_COLLECTED`

and always sets
`diagnosis_change_attributable_to_runtime_evidence: false` with an explicit
attribution note. An automated test asserts that no category name in this
vocabulary contains "useful", "confirm", "refut", "changed" or "misread".

---

## 6. Determinism

`results.json` is byte-stable across repeated runs of the same working tree
apart from two explicitly declared top-level keys:

* `environment` — Git HEAD/branch/dirty flag, working-tree status digest,
  `agentic_debugger/` source digest, interpreter, platform, generation time.
* `timing` — wall-clock milliseconds per case.

`deterministic_view()` strips exactly those keys; the automated test
`TestDeterminism` and the reproduction instructions both use it.

Raw event JSONL carries real UTC timestamps and is therefore *not* byte-stable.
The semantic projections are stable: replay strips timestamps and duration
metadata and aliases run-scoped identifiers.

The `environment.source_tree_sha256` digest pins the exact
`agentic_debugger/` bytes that were executed, including uncommitted changes, so
a result document always identifies the tree it came from.

Two caveats on the stability claim:

* Diagnostics and tool errors are inside the deterministic section, so every
  one of them is passed through the accepted `normalize_output` redaction
  before it is stored. A raw `PermissionError` naming a `mkdtemp` directory
  would otherwise make that section unstable; an automated test injects exactly
  that failure and asserts no disposable path survives.
* Several deterministic fields are outcomes of real pytest subprocesses
  (`verifier.f2p_passed`, `full_suite_status`, `timeout`). On a machine loaded
  enough to hit a task timeout, those values change. Byte stability is a
  healthy-machine property, not a guarantee.

---

## 7. Safety and isolation

### Offline enforcement (measured, not promised)

While each case runs, an in-process guard intercepts, **counts and refuses**:

* outbound socket use (`socket.socket.connect`, `connect_ex`,
  `socket.create_connection`, `socket.getaddrinfo`);
* imports of a fixed list of remote model-provider SDK roots (`anthropic`,
  `openai`, `cohere`, `mistralai`, `litellm`, `ollama`, `replicate`,
  `together`, `vertexai`, `google.generativeai`).

The resulting counters appear per case under `offline` and are aggregated in
`aggregates.offline`. They are measurements, not assertions.

The guard's honest scope is reported alongside the counts and repeated here:
it is **in-process only**. Child processes — the pytest subprocesses and the
PDB worker — are separate interpreters and are not covered by it; they run only
curated fixture suites and the bundled worker. The provider guard recognises a
fixed list of SDK roots and cannot prove that no provider was reached by some
other means.

`environment.model_provider_policy` and `environment.network_access_policy` are
stated *policy*, clearly separate from the measured counters.

### Other guarantees

* Canonical curated fixtures are never modified. Each case digests the fixture
  before and after the controller phase and reports
  `canonical_fixture_unchanged_by_controller` on its own authority; the
  verifier independently reports `canonical_fixture_unchanged` for its own
  phase. Both are needed: the verifier's baseline hash is taken after the
  controller has already run.
* All execution happens in disposable workspaces inside one per-case directory
  under a caller-supplied parent. The runner deletes that per-case directory on
  success and on failure, and leaves the caller's parent otherwise untouched.
* Patch paths are enforced by `PatchManager` against the manifest allow/deny
  lists; `tests/` and `task.json` are denied by default.
* Debugger access returns typed bounded observations only; the model never sees
  a raw PDB prompt, and expression evaluation goes through the accepted AST
  allowlist.
* The execution boundary is `TRUSTED_LOCAL_WORKSPACE`. It is **not** an
  OS-level hostile-code sandbox.

---

## 8. Limitations, assumptions and deferred work

Limitations (all of these are also emitted into every generated summary):

1. No model in the loop; the demonstration measures the platform, not repair
   capability.
2. Both policies receive the identical candidate diff per task, so outcome
   parity between them is structural. Parity is now *derived* from the recorded
   patch digests rather than asserted, and reported per task.
3. Five small single-file Python defects; nothing generalizes to
   repository-scale work.
4. Trusted-local boundary, not a security sandbox.
5. Only two of the four planned policy variants are exercised.
6. Localization is scored at file+symbol granularity, not statement level.
7. No token or cost metrics; cost is call and command counts only.
8. **The debugger never pauses inside the failing test.** It attaches to a
   second disposable copy of the fixture carrying an appended driver that calls
   the focus function with catalog-supplied arguments. Runtime evidence
   therefore describes a curated stand-in reproduction, not the fail-to-pass
   run itself.
9. The breakpoint anchor and probe expressions were authored with knowledge of
   each defect, so the debugger is guaranteed to pause on the relevant line.
   That is scaffolding, not a capability result.
10. The localization claim is pinned to the task oracle by an automated test,
    so a clean run can only ever score `CORRECT_TARGET_SYMBOL`.
11. The curated fixtures are synthetic and partly instrumented for
    observability. Two reference repairs also remove planted assertion
    scaffolding, so they are not minimal real-world bug fixes.
12. Controller-side validation aggregates all pass-to-pass tests into one
    batched pytest exit code. Per-node evidence comes only from the verifier,
    which is therefore the authoritative source and is labelled as such.

Assumptions:

* `python -m pytest` behaves identically in the demonstration and in the
  verifier, because both use the manifest argv through the same command runner.
* One pause generation is enough runtime evidence for these defects; the probe
  pauses once inside the focus function.
* The curated manifests' budgets are the operative limits; the demonstration
  never raises them.
* A malformed canonical manifest is a repository-integrity failure, so it fails
  the whole run fast with exit code 2 rather than being recorded as one bad
  case.

Deliberately deferred: real-model execution, the remaining two PDB gate
variants, multi-attempt repair loops, BugsInPy/SWE-bench ingestion, hostile-code
containment, and all fine-tuning, RAG and preference-optimization work.

---

## 9. Automated coverage

| Test module | Scope |
|---|---|
| `tests/unit/test_demo_catalog.py` | Catalog covers the live curated set; claims match oracles; repairs render exactly one applicable diff; probes resolve to a statement start inside their own scope and fit the PDB budget; drift, ambiguity, nested-scope anchors and continuation-line anchors are hard errors. |
| `tests/unit/test_demo_model.py` | Every phase and abort branch of the offline adapter, driven by a miniature controller that enforces the real allowlist, transition graph and budgets: unreproduced failure, missing symbol, rejected patch, failed syntax check, failed post-patch reproduction, failed regression, non-`RESOLVED` outcome, each debugger step failing, gate denial on an exhausted budget, and the no-confidence-bump revision rule. |
| `tests/unit/test_demo_tools.py` | Tool boundary: argument validation, state allowlist, denied write paths, malformed diffs, missing symbols, debugger lifecycle guards, retry-safe session release, path-redacted diagnostics, probe preparation, argv construction. |
| `tests/unit/test_demo_isolation.py` | The offline guard refuses and counts every socket entry point and provider-SDK import, leaves unrelated imports alone, and restores global state on both the success and failure paths. |
| `tests/unit/test_demo_metrics.py` | Localization and runtime-evidence scoring matrices, the localization sub-fields, patch-parity derivation, offline aggregation, determinism view, environment and fixture digests, summary rendering including the failure section, CLI contracts. |
| `tests/integration/test_demo_end_to_end.py` | Real integrated runs for both policies, real bounded PDB evidence, byte-stable repeats on both the static and debugger paths, a failing case whose diagnostics leak no disposable path, honest recording of a non-reproducing baseline, a regressing repair caught independently by the verifier, a drifted reference repair, a broken probe, a malformed manifest, multi-case aggregation, workspace cleanup on every path, and CLI artifact agreement and exit codes. |

Targeted command:

```powershell
python -m pytest tests/unit/test_demo_catalog.py tests/unit/test_demo_model.py tests/unit/test_demo_tools.py tests/unit/test_demo_isolation.py tests/unit/test_demo_metrics.py tests/integration/test_demo_end_to_end.py -q
```
