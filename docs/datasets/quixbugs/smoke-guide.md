# QuixBugs Resource-Limited Smoke Usage v1

BugsInPy execution remains license-gated (see `docs/datasets/bugsinpy/adapter-usage.md`
and `docs/datasets/bugsinpy/pilot-readiness.md`). This is a narrow fallback: it
implements the smallest QuixBugs-specific layer needed to run one genuine,
real, no-model smoke — Python `gcd` only — through the existing runtime,
evaluation, and workspace contracts, extended with a live-self-tested
resource-control profile so the accepted WSL2/Bubblewrap sandbox is no longer
fail-closed on CPU/memory/process-count enforcement.

## Why QuixBugs, why now

`agentic_debugger/bugsinpy/wsl.py` already blocked benchmark execution with
`ResourceIsolationUnavailable` because CPU, memory, and process-count limits
were not enforced (only a wall-clock timeout was). That module is extended
narrowly — `ResourceLimits`, `build_prlimit_argv`,
`WslBubblewrapRunner.self_test_resource_limits`, and
`WslBubblewrapRunner.prepare_resource_isolation` — to wrap the existing
Bubblewrap sandbox with `prlimit --cpu=... --as=... --nproc=...`. The gate
stays fail-closed: `prepare_resource_isolation` only flips
`resource_isolation_ready` to `True` after live self-test evidence proves all
three limits are enforced inside the sandbox; otherwise it raises
`ResourceIsolationUnavailable`. `create_verified_context` gained an optional
`runner=` parameter so a pre-prepared runner can be reused; the default
(no-`runner`) path is unchanged, so every prior BugsInPy test kept passing
unmodified.

## Repository pin and license

- Repository: `https://github.com/jkoppel/QuixBugs` (default branch `master`)
- Pinned revision: `4257f44b0ff1181dedaedee6a447e133219fcebf`
- License: MIT (Copyright 2017-2019 James Koppel)
- `legal_notes.txt` documents the Quixey-bankruptcy IP situation and creator
  Liron Shapira's explicit written consent ("I'm personally happy to support
  your use of this data"). Both files were hashed from the pinned checkout
  (see the review package). Conclusion: supports local, non-redistributed
  research execution.

## The gcd bug (real, not synthesized)

`python_programs/gcd.py` computes `gcd(a % b, b)`. Because `b` never changes
across the recursive calls, every case except the trivial `b == 0` one
recurses forever and raises `RecursionError`. Of the six official
parametrized cases in `json_testcases/gcd.json`, exactly **one**
(`[17, 0] -> 17`) passes on the buggy baseline; the other five fail. This is
real upstream data, confirmed by a live pytest run, not curated.

Because only one node passes on the baseline, `DebugTask.tests.pass_to_pass`'s
minimum was lowered from 2 to 1 entries in
`agentic_debugger/evaluation/task_schema.py` — a deliberate, narrow,
backward-compatible relaxation (every existing curated/BugsInPy task already
supplies ≥2, so nothing that previously validated now fails) made specifically
so this real external-dataset shape does not require fabricating a second
passing node.

## Storage layout (WSL-owned, outside `/mnt/c`)

```
~/.local/share/agentic-debugging-internship/quixbugs-smoke-v1/
├── sources/quixbugs/      # immutable pinned clone -- never auto-deleted
├── python-env/py310/      # task-local venv -- never auto-deleted
├── runs/<uuid>/           # disposable per-run workspace -- auto-deleted after each run
├── cache/                 # pip cache -- never auto-deleted
└── runtime/empty/         # Bubblewrap hidden-mount source
```

`QuixBugsSmokeRunner.ensure_source()` acquires the pinned repository once; a
second call against an already-populated directory only re-verifies the pin
(SHA-1 + detached HEAD) and never re-clones or mutates existing bytes.
`ExternalWorkspace` (reused unmodified from `agentic_debugger.bugsinpy.adapter`)
owns only the disposable `runs/<uuid>/` root; its `cleanup()` never touches
`sources/`, `python-env/`, or `cache/`.

## Python environment

- System `/usr/bin/python3` in the approved Ubuntu-22.04 WSL2 distro: `3.10.12`
- `python3 -m venv --copies --without-pip <venv>` — **`--copies` is required**:
  the default symlink mode produces `bin/python -> python3 -> /usr/bin/python3`,
  and the final absolute-path hop is invisible through the
  `\\wsl.localhost\Ubuntu-22.04\` Windows bridge, which made
  `Path(python_executable).is_file()` return `False` even though the
  interpreter worked perfectly from inside WSL. `--copies` makes `bin/python`
  a real ELF file, fixing Windows-side visibility.
- pip bootstrapped via `https://bootstrap.pypa.io/get-pip.py` into the venv
  (network only during this dependency-prep step)
- Pinned package: `pytest==7.4.4`
- Environment fingerprint: canonical SHA-256 of `{python_version, sorted
  pip-freeze package list}` via the existing `fingerprint_environment()`
  helper, recorded in `DependencyPreparation.installed_fingerprint`

## Resource-control mechanism: `prlimit`

Two mechanisms were live-tested (cgroup v2/`systemd-run --user --scope`, which
works passwordless but is a rate/quota model without a direct CPU-seconds
total; and `prlimit`, which composes cleanly inside the existing
`bwrap --unshare-all` sandbox and directly expresses CPU-time,
address-space, and process-count caps). `prlimit` was selected because it is
literally what the task's mechanism B calls for and its enforcement was
directly proven inside the Bubblewrap execution path.

Profile used: `cpu_seconds=5`, `memory_bytes=268435456` (256 MiB),
`max_processes=8`. These were sized empirically against the real gcd/pytest
workload (peak RSS ≈27 MB, wall time <0.1 s, comfortably fits even at a 64 MB
address-space ceiling in isolation testing) before being fixed and then
live self-tested for enforcement immediately before the real smoke ran.

Live self-test results (recorded in the review package):

| Check | Result |
|---|---|
| CPU-time cap (`--cpu=5`, spin loop) | killed, exit 137 (SIGKILL) |
| Address-space cap (`--as=256MiB`, oversized allocation) | clean `MemoryError`, exit 1 |
| Process-count cap (`--nproc=8`, 64-fork attempt) | blocked, exit 3 |
| Network denial | `OSError: Network is unreachable` |
| Windows mounts hidden | `/mnt/c` absent |
| Unrelated WSL home hidden | absent |
| Owned workspace write | succeeds |
| Runtime mounts read-only | `/usr/bin` write fails |
| Child-process isolation | `getppid() == 1` inside the sandbox |
| Exact interpreter | `3.10.12` |

`WslBubblewrapRunner.prepare_resource_isolation()` only opens the gate after
all three resource checks report `passed: true`; it raises
`ResourceIsolationUnavailable` otherwise. Retained-output bounding (20,000
chars) and single-owned-bind-mount writes were already enforced by the
existing `CommandRunner`/`build_bwrap_command` contracts and are unchanged.

## Real no-model smoke result

Verdict: **`ACCEPT CANDIDATE — REAL SMOKE PASSED`**

- Collected 6 nodes for `python_testcases/test_gcd.py`; baseline classified 5
  F2P candidates and 1 P2P candidate (matches the analysis above exactly)
- Independent `--correct` oracle run: 6/6 passed
- Gold patch generated via `difflib.unified_diff` between the pinned buggy
  and corrected `gcd.py`, hashed, and confirmed to touch only
  `python_programs/gcd.py`
- `EvaluationVerifier.evaluate()` through the prepared `VerifiedExecutionContext`:
  status `COMPLETED`, outcome `RESOLVED`, F2P 5/5 passed post-patch, P2P 1/1 passed, full suite (all 6 collected nodes) 6/6 passed, canonical fixture
  hash unchanged before/after, workspace lifecycle `CLEANED`
- Disposable run workspace removed; pinned source, venv, and cache persisted

## Supported and deferred

Supported: single-task manifest validation, live pytest collection/
classification, gold-patch generation and scope restriction, prlimit resource
profile with live self-test evidence, full existing verifier/patch/workspace
lifecycle reuse, persistent-vs-disposable WSL storage separation.

Deferred: BugsInPy execution (still license-gated, not rejected), PDB
execution or planning (out of scope for this smoke), any QuixBugs task other
than `gcd`, the broader benchmark campaign, model/provider execution.
