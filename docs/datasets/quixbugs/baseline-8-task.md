# QuixBugs Eight-Task Gold Baseline v1

Expands the accepted resource-limited `gcd` smoke (see
`docs/datasets/quixbugs/smoke-guide.md`) into a reproducible eight-task no-model
baseline on the same pinned QuixBugs revision, reusing the same adapter, WSL
runner, resource profile, environment, source checkout, patch lifecycle, and
`EvaluationVerifier` unmodified in behavior. **This campaign validates dataset
eligibility, gold patches, verifier behavior, runtime stability, and evidence
quality. It does not evaluate a model or PDB** — every gold patch is the
literal buggy→corrected diff from the pinned repository, applied and verified
with no model in the loop.

## What changed to generalize beyond `gcd`

`agentic_debugger/quixbugs/adapter.py` was narrowly generalized (no second
evaluation framework):

- `QuixBugsManifest` gained `algorithm`, `support_paths`, and `oracle`
  properties.
- `_validate_manifest` no longer pins `manifest_id`/`target.algorithm` to the
  literal `quixbugs-gcd-smoke-v1`/`"gcd"`. It now derives the required
  `quixbugs-<algorithm>-smoke-v1` identity from `target.algorithm` and
  requires `buggy_path`/`corrected_path`/`pytest_path` to match that
  algorithm's upstream naming convention exactly (`python_programs/<algo>.py`,
  `correct_python_programs/<algo>.py`, `python_testcases/test_<algo>.py`) —
  fail-closed on any mismatch, not weakened.
- A new required `oracle` manifest section (`bug_category`, `target_symbols`,
  `root_cause_summary`, `runtime_evidence_hint`) replaces the previously
  hardcoded gcd-only oracle text in `to_debug_task()`.
- `source_provenance()`, the dependency-gate's expected `bug_id`, and
  `to_debug_task()`'s title/description/tags/denied-write-paths now derive
  from `manifest.algorithm` instead of a literal `"gcd"` string.
- All 63 tests in `tests/unit/test_quixbugs_adapter.py` (50 original + 13 new
  generalization tests, including a second real manifest fixture) pass
  unchanged in intent; the final full unit suite passed with 1952 passed and
  2 skipped.

`scripts/quixbugs_eight_task_baseline.py` generalizes
`scripts/quixbugs_live_smoke.py` (kept unmodified) to build the WSL
environment once and drive the existing `QuixBugsSmokeRunner` across a list
of manifests, reusing the same self-tested `WslBubblewrapRunner` and resource
profile for every task.

## Task selection (live evidence, 12-candidate cap)

Selection was deterministic: alphabetical order over the pinned repository's
`json_testcases`-backed `python_testcases/test_*.py` files (excluding `gcd`,
already accepted). Triage that preceded formal execution used an
**unsandboxed `pytest` run under a shell `timeout`** to identify
structurally-skipped (`pytest.skip`) or zero-`pass_to_pass` candidates — this
was test execution outside the resource-limited runner, **not** read-only
metadata inspection. Its exact historical inventory cannot be reconstructed,
so historical compliance with the candidate cap is unproven. For future runs,
only static file/metadata inspection may occur outside the resource-limited
runner. Candidates were then **formally executed** one at a
time through the generalized adapter + resource-limited WSL verifier
pipeline (the pipeline described in `docs/datasets/quixbugs/smoke-guide.md`) until
7 additional eligible tasks joined the already-accepted `gcd`:

| Order | Algorithm | Formal result |
|---|---|---|
| 0 | `gcd` | eligible (previously accepted, re-run for consistent evidence) |
| 1 | `bitcount` | **excluded** — buggy `n ^= n - 1` never terminates (converges to `n == 1` and spins forever); no reproducible failing node. Live evidence: the per-node run returned `exit_code=137` (SIGKILL — the exact `prlimit` CPU-time-limit kill signature, also proven live in this run's own `resource_readiness.cpu_limit_enforced` self-test), not a clean 0/1 exit |
| 2 | `bucketsort` | eligible |
| 3 | `find_first_in_sorted` | **excluded** — buggy `while lo <= hi:` fails to converge for one official case. Live evidence: that node's run also returned `exit_code=137` (SIGKILL) |
| 4 | `find_in_sorted` | eligible |
| 5 | `flatten` | eligible |
| 6 | `get_factors` | **excluded** — 11 collected nodes (10 F2P + 1 P2P) push `verifier_command_count(10,1) = 26` past `task_schema.Constraints.max_test_runs`'s `[1, 20]` range; preflight passed, discovery/oracle/gold-patch all succeeded, but `DebugTask` construction itself raised `SchemaValidationError`. This is a schema-representability limit, not a resource, reproducibility, or scope failure, and the range was **not** widened to force it in (no authorization to relax that bound for this campaign) — the candidate was excluded and replaced |
| 7 | `hanoi` | eligible |
| 8 | `is_valid_parenthesization` | eligible |
| 9 | `kheapsort` | eligible |
| 10 | `kth` | eligible (formally executed as the replacement for the excluded `get_factors`) |

**11 unique candidates were executed through the resource-limited runner**:
8 selected (all eligible and solved, all reaching the `EvaluationVerifier`
with `COMPLETED`/`RESOLVED`) + 3 excluded. Of the 3 excluded, 2 were
excluded at the **discovery stage** (`bitcount`, `find_first_in_sorted` —
non-terminating buggy baselines, killed by the enforced CPU-time limit;
their tests ran through the resource-limited runner but did **not** reach
the `EvaluationVerifier`) and 1 at the **pre-verifier schema-construction
stage** (`get_factors` — `max_test_runs` range exceeded; preflight,
discovery, oracle, and gold-patch all succeeded through the resource-limited
runner, but `DebugTask` construction raised `SchemaValidationError` before
the `EvaluationVerifier` could run, so `get_factors` did **not** reach the
`EvaluationVerifier`).

**Historical cap compliance is unproven.** The exploratory triage that
preceded formal execution used unsandboxed `pytest` runs under a shell
`timeout` (test execution outside the resource-limited runner, **not**
read-only metadata inspection). The exact inventory of every algorithm
whose tests ran during that triage cannot be proven from the surviving
evidence — the triage was exploratory and not fully logged. Therefore the
12-unique-candidate cap's historical compliance cannot be asserted, and the
prior "11 of 12" claim has been removed. What can be proven: 11 unique
algorithms were executed through the resource-limited runner. For future
runs, only static file/metadata inspection (no test execution) may occur
outside the resource-limited runner, and the 12-unique-attempted-algorithm
cap is enforced in the orchestration path. Full exclusion evidence
(including `get_factors`'s complete preflight/discovery/gold-patch/error
trace) lives in `_ai-review/quixbugs-eight-task-baseline-v1/exclusion-evidence/`
(local working evidence, not committed — see Boundaries below) and in
`research/quixbugs/EIGHT_TASK_PILOT_MANIFEST_V1.json`.

## Selected eight tasks

Each task reuses the identical environment/resource profile as the accepted
`gcd` smoke: system Python `3.10.12` in the approved Ubuntu-22.04 WSL2 distro,
`pytest==7.4.4`, environment fingerprint
`4cee35b2f786fc4857e468abb87269cc932eefab10af6b163a162e2c50bd6d83`; `prlimit`
profile `cpu_seconds=5`, `memory_bytes=268435456`, `max_processes=8`,
`wall_clock_timeout_seconds=30`, `retained_output_chars=20000`,
`network_denied=true`.

| Algorithm | Manifest | Collected | F2P | P2P | Gold-patch hunks | Verifier outcome | Full suite | Fixture unchanged | Cleanup | Duration |
|---|---|---|---|---|---|---|---|---|---|---|
| `gcd` | `GCD_SMOKE_MANIFEST_V1.json` | 6 | 5/5 | 1/1 | 1 | COMPLETED / RESOLVED | 6/6 | yes | CLEANED | 62.3s |
| `bucketsort` | `BUCKETSORT_SMOKE_MANIFEST_V1.json` | 7 | 6/6 | 1/1 | 2 | COMPLETED / RESOLVED | 7/7 | yes | CLEANED | 58.4s |
| `find_in_sorted` | `FIND_IN_SORTED_SMOKE_MANIFEST_V1.json` | 7 | 2/2 | 5/5 | 2 | COMPLETED / RESOLVED | 7/7 | yes | CLEANED | 52.2s |
| `flatten` | `FLATTEN_SMOKE_MANIFEST_V1.json` | 7 | 6/6 | 1/1 | 1 | COMPLETED / RESOLVED | 7/7 | yes | CLEANED | 51.3s |
| `kth` | `KTH_SMOKE_MANIFEST_V1.json` | 7 | 4/4 | 3/3 | 2 | COMPLETED / RESOLVED | 7/7 | yes | CLEANED | 65.1s |
| `hanoi` | `HANOI_SMOKE_MANIFEST_V1.json` | 8 | 7/7 | 1/1 | 1 | COMPLETED / RESOLVED | 8/8 | yes | CLEANED | 55.4s |
| `is_valid_parenthesization` | `IS_VALID_PARENTHESIZATION_SMOKE_MANIFEST_V1.json` | 3 | 1/1 | 2/2 | 1 | COMPLETED / RESOLVED | 3/3 | yes | CLEANED | 43.7s |
| `kheapsort` | `KHEAPSORT_SMOKE_MANIFEST_V1.json` | 4 | 3/3 | 1/1 | 1 | COMPLETED / RESOLVED | 4/4 | yes | CLEANED | 48.1s |

Every gold patch touched exactly one file (the manifest's `buggy_path`) and
zero others, matching the `allowed_write_paths` constraint. Every task's
canonical fixture hash was identical before and after evaluation, and every
task's disposable run workspace was `CLEANED` (verified: the WSL `runs/`
directory contains only the persistent `selftest/` scaffold after the full
run, no leftover per-run UUID directories).

## Aggregate result

- **Selected tasks: 8/8 eligible and solved** (gold patch verified
  end-to-end): solved rate **100%**, full-suite pass rate **100%**
  (49/49 total collected nodes passed post-patch across all eight tasks,
  0 failures/errors/skips/xfails/xpasses in any full-suite run).
- Total measured task runtime: ≈436s across the 8 selected tasks (plus
  screening overhead for 2 excluded and 1 replacement candidate); one-time
  environment setup (venv reuse check, 7 Bubblewrap self-tests, 5 resource
  self-tests) adds a few seconds, not repeated per task since the same
  `WslBubblewrapRunner` instance is reused for the whole run.
- **Infrastructure vs. model quality**: this is a no-model, gold-patch
  baseline. A 100% solved rate here demonstrates the dataset, adapter,
  resource-limited sandbox, and verifier are functioning correctly on eight
  real QuixBugs tasks — it says nothing about how a model or PDB-driven agent
  would perform, since the "candidate patch" in every run is the literal
  upstream fix, not a generated one.

## Reproducing

```
python scripts/quixbugs_eight_task_baseline.py \
  --excluded-manifest <path-to-bitcount-screening-manifest> \
  --excluded-manifest <path-to-find_first_in_sorted-screening-manifest>
```

Reuses the existing pinned source and Python environment; does not reinstall
or re-clone unless live evidence proves the pin or the pinned pytest version
is invalid. Network stays disabled during all task execution (only the
one-time venv/pip bootstrap step, if needed, uses network). `--only <algo>`
restricts the selected-task loop to matching manifests (used here to add
`kth` after `get_factors` was excluded, without re-running the other seven).

## Boundaries

No models, providers, OpenCode, MCP, RAG, SFT, DPO, or PDB were run. No
system software was installed, no credentials were accessed, and no
Windows/WSL system configuration was modified (all environment reuse was
verified live, not assumed). Eleven unique candidates were executed through
the resource-limited runner (8 selected reaching the `EvaluationVerifier`
+ 3 excluded; `get_factors` did not reach the `EvaluationVerifier`).
Historical compliance with the 12-unique-candidate cap is **unproven**
(the exploratory unsandboxed triage inventory cannot be reconstructed from
surviving evidence); future runs are fail-closed at 12 unique attempted
algorithms, enforced in the orchestration path. BugsInPy execution remains
license-gated and out of scope for this campaign. See
`_ai-review/quixbugs-eight-task-baseline-v1/` (local working evidence,
excluded from commits via `.git/info/exclude`, consistent with the prior
QuixBugs smoke review package) for the full diff, manifests, exclusion
evidence, per-task results, test output, cleanup evidence, and exact git
status.
