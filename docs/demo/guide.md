# Demo Guide v1

This guide uses only existing, already-accepted entry points. It adds no new
demo framework and changes no runtime code. **It validates evaluation
infrastructure — task loading, sandboxing, patch application, and
verification — not model debugging performance.** No demo command in this
guide runs a model, PDB, RAG, training, or a paid API.

If you only run one thing, run Section 2 (offline, no WSL, no network,
~seconds). Sections 3–4 describe the QuixBugs WSL entry points as
already-accepted reproduction commands; **do not re-run them casually** —
they are real, resource-limited executions against a live WSL sandbox and
are treated as expensive accepted campaigns, not push-button smoke tests.

## 1. Prerequisites

| For | Requirement |
|---|---|
| Section 2 (curated demo) | Python ≥ 3.11; `pip install -e .[test]`; `pytest` and `python` on `PATH`. No network, no WSL, no credentials. |
| Sections 3–4 (QuixBugs) | Windows host with WSL2 and the `Ubuntu-22.04` distro installed and already provisioned per `docs/datasets/quixbugs/smoke-guide.md` (Bubblewrap available, pinned QuixBugs source and `python-env/py310` venv already acquired at `~/.local/share/agentic-debugging-internship/quixbugs-smoke-v1/` under WSL, outside `/mnt/c`). These scripts are operator scripts (`scripts/quixbugs_live_smoke.py`, `scripts/quixbugs_eight_task_baseline.py`), not part of the automated test suite, and are hardcoded to a specific WSL distro name and external root path. |

## 2. In-repo deterministic smoke: the Task 9 demo (recommended first step)

No WSL, no network, no model — the fastest way to confirm the controller,
tool registry, workspace, patch, PDB, and verifier stack imports and runs
cleanly on your checkout.

```powershell
python -m pip install -e .[test]
python -m agentic_debugger.demo --output-dir demo-out
```

Useful variants (`--output-dir` is required by every invocation, including
`--list-tasks`):

```powershell
python -m agentic_debugger.demo --output-dir demo-out --list-tasks
python -m agentic_debugger.demo --output-dir demo-out --task-id curated-off-by-one-002
python -m agentic_debugger.demo --output-dir demo-out --strict
```

Verified live on this checkout: `--output-dir demo-out --list-tasks` prints
the 5 curated task IDs; `--output-dir demo-out --strict` exits `0` with 10
cases recorded.

**Expected output / success criteria:** exit code `0`; `demo-out/results.json`
and `demo-out/technical-evaluation-summary.md` report 5 curated tasks × 2
policies = 10 cases, all controller `Done`, all verifier
`COMPLETED`/`RESOLVED`, fail-to-pass 10/10, pass-to-pass 22/22, localization
`CORRECT_TARGET_SYMBOL` in all 10 cases, canonical fixtures unchanged, every
workspace cleaned (`docs/demo/task-9.md`). This demo uses a scripted, offline
model stand-in — it is not a model debugging performance result (see that
document's Section 4).

**Evidence location:** the `--output-dir` you pass (e.g. `demo-out/`), plus
`trajectories/<case>.events.jsonl` and `.semantic.json` per case.

## 3. One-task QuixBugs `gcd` real smoke (already-accepted entry point)

```powershell
python scripts/quixbugs_live_smoke.py
```

This is the accepted single-task infrastructure smoke described in
`docs/datasets/quixbugs/smoke-guide.md`. It builds/reuses the WSL venv, runs the
Bubblewrap and `prlimit` resource self-tests, then runs the real `gcd`
QuixBugs task (pinned revision `4257f44b0ff1181dedaedee6a447e133219fcebf`)
through the adapter, patch lifecycle, and verifier — with the literal
upstream fix as the candidate patch, not a generated one.

**Expected output / success criteria:** JSON evidence bundle on stdout ending
in `"verdict": "ACCEPT_CANDIDATE_REAL_SMOKE_PASSED"`, exit code `0`.
Discovery collects 6 nodes (5 F2P, 1 P2P); post-patch F2P 1/1, P2P 1/1, full
suite 2/2; canonical fixture hash unchanged; workspace lifecycle `CLEANED`.

**Evidence location:** stdout JSON (archive it yourself — the script does
not write a file); cross-check against `docs/datasets/quixbugs/smoke-guide.md`.

## 4. Eight-task QuixBugs gold baseline (already-accepted entry point)

```powershell
python scripts/quixbugs_eight_task_baseline.py --skip-excluded
```

`--skip-excluded` runs exactly the 8 selected, tracked manifests under
`research/quixbugs/` (`gcd`, `bucketsort`, `find_in_sorted`, `flatten`,
`kth`, `hanoi`, `is_valid_parenthesization`, `kheapsort`) without requiring
the excluded-candidate screening manifests (`bitcount`,
`find_first_in_sorted`, `get_factors`), which are local working evidence
under `_ai-review/quixbugs-eight-task-baseline-v1/exclusion-evidence/` and
are not tracked in Git (`docs/datasets/quixbugs/baseline-8-task.md`). To
restrict to a single algorithm instead, use `--only <algorithm>` (exact
name, e.g. `--only kth`).

**Expected output / success criteria:** JSON report on stdout ending in
`"verdict": "ACCEPT_CANDIDATE_EIGHT_TASK_BASELINE_COMPLETE"` and exit code
`0` (with `--skip-excluded`, the completeness check only compares the
selected list, so this remains achievable without the excluded manifests).
`report["aggregate"]["solved_rate"]` should read `1.0` with
`all_passed: true`.

**Evidence location:** stdout JSON; cross-check totals against
`docs/datasets/quixbugs/baseline-8-task.md` (49/49 nodes passed, 8/8 tasks
solved) and `_ai-review/quixbugs-eight-task-baseline-v1/` for the original
accepted run's full archived evidence.

## 5. What none of this demonstrates

- **No model was run.** Sections 3–4 apply the literal upstream fix, not a
  generated patch. Section 2 uses a scripted stand-in, not a model.
- **No PDB session was opened** in Sections 3–4. Section 2's PDB-enabled
  policy attaches to a driver script with a pre-known breakpoint, not the
  failing test itself (`docs/demo/task-9.md` Section 8, item 8).
- **No repository-scale or BugsInPy result.** BugsInPy execution remains
  license-blocked (`docs/datasets/bugsinpy/pilot-readiness.md`).
- **No RAG, fine-tuning, or preference-optimization behavior.** None of
  those workstreams have any runtime code to demo yet
  (`docs/evaluation/model-rag-sft-dpo.md`).

## 6. Blockers and safe recovery

| Symptom | Likely cause | Safe recovery |
|---|---|---|
| Section 2 fails to import `agentic_debugger` | Package not installed in the active interpreter | `python -m pip install -e .[test]`, then retry. Do not modify runtime source to work around an environment issue. |
| Section 2 exits `1` under `--strict` | A case did not reach `RESOLVED`/`COMPLETED` | Compare `results.json` against the documented 10/10 baseline in `docs/demo/task-9.md`; this is a regression signal, not expected variance — stop and investigate before assuming a demo-guide error. |
| Sections 3–4 raise `ResourceIsolationUnavailable` | The `prlimit`/Bubblewrap self-tests failed live in your WSL environment | Do not weaken or bypass the gate. Fix the WSL/Bubblewrap/`prlimit` environment per `docs/datasets/quixbugs/smoke-guide.md`; the gate is fail-closed by design. |
| Sections 3–4 raise `SetupError` or `OrchestrationError` before any task runs | Local validation failure (manifest drift, `--only` typo, cap exceeded, WSL layout/venv bootstrap failure) | These are raised *before* any WSL/task side effect (`validate_campaign` in `scripts/quixbugs_eight_task_baseline.py`); re-read the error message, fix the invocation, and retry. No cleanup is needed because nothing ran yet. |
| Sections 3–4 return `IMPLEMENTED_REAL_SMOKE_BLOCKED` / `IMPLEMENTED_BASELINE_BLOCKED` | Preflight gate did not authorize (license, platform, dependency, or containment fact unresolved) | Expected fail-closed behavior for an unmet gate — not a crash. Do not force authorization; review which preflight fact is unresolved. |
| Any command appears to hang | A buggy baseline that does not terminate (as with the historically-excluded `bitcount`/`find_first_in_sorted` candidates) | The `prlimit` CPU-time cap kills such a case automatically (exit 137) within the configured profile (`cpu_seconds=5`, plus a 30s wall-clock timeout); wait for that bound rather than manually killing the WSL process. |
| You are unsure whether to re-run Sections 3–4 | These are accepted, evidence-recorded campaigns | Prefer reading `docs/datasets/quixbugs/smoke-guide.md` / `docs/datasets/quixbugs/baseline-8-task.md` and the archived `_ai-review/` evidence over re-running; only re-run with a clear reason (e.g., verifying a real inconsistency), consistent with this campaign's own instruction not to re-run accepted benchmarks casually. |

## 7. One-line summary to keep in view

Every command in this guide proves the platform can load a task, sandbox
its execution, apply a patch, and verify the result — it never proves that
an LLM, with or without a debugger, can find or fix the bug itself.
