"""Operator orchestration script for the QuixBugs eight-task gold baseline.

Generalizes ``scripts/quixbugs_live_smoke.py`` (kept unmodified, still valid
for the single accepted ``gcd`` smoke) to drive the same pinned WSL/Bubblewrap
resource-limited execution context across all eight selected QuixBugs Python
tasks, plus two screening candidates that were formally executed and excluded.
It builds the environment once (venv reuse, Bubblewrap self-test, resource
readiness), then runs the existing ``QuixBugsSmokeRunner`` once per task
through the shared runner, and prints one JSON evidence bundle to stdout.

Not part of the automated test suite -- an operator/CI script, same as
``quixbugs_live_smoke.py``. Reuses the existing pinned source and Python
environment when they already match; it does not reinstall or re-clone
unnecessarily. Excluded-candidate manifests live outside ``research/quixbugs``
(only the eight selected tasks' manifests are tracked there) and are passed
in via ``--excluded-manifest`` so this script never has to fabricate one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter as _Counter
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.bugsinpy.adapter import ExternalWorkspace  # noqa: E402
from agentic_debugger.bugsinpy.wsl import (  # noqa: E402
    ResourceLimits,
    WslBubblewrapRunner,
    WslProcess,
    create_verified_context,
    fingerprint_environment,
    wsl_unc_path,
)
from agentic_debugger.quixbugs.adapter import (  # noqa: E402
    DiscoveryError,
    QuixBugsAdapter,
    QuixBugsPreflightFacts,
    QuixBugsSmokeRunner,
    QuixBugsSourceAcquirer,
)
from agentic_debugger.runtime.execution import DependencyPreparation  # noqa: E402
from agentic_debugger.runtime.workspace import TaskWorkspace  # noqa: E402

DISTRO = "Ubuntu-22.04"
EXTERNAL_ROOT_POSIX = os.environ.get("AGENTIC_DEBUGGER_QUIXBUGS_ROOT", "").rstrip("/")
PYTEST_VERSION = "7.4.4"
PYTHON_VERSION = "3.10.12"
PILOT_MANIFEST = REPO_ROOT / "research" / "quixbugs" / "EIGHT_TASK_PILOT_MANIFEST_V1.json"

SELECTED_MANIFESTS = [
    "research/quixbugs/GCD_SMOKE_MANIFEST_V1.json",
    "research/quixbugs/BUCKETSORT_SMOKE_MANIFEST_V1.json",
    "research/quixbugs/FIND_IN_SORTED_SMOKE_MANIFEST_V1.json",
    "research/quixbugs/FLATTEN_SMOKE_MANIFEST_V1.json",
    "research/quixbugs/KTH_SMOKE_MANIFEST_V1.json",
    "research/quixbugs/HANOI_SMOKE_MANIFEST_V1.json",
    "research/quixbugs/IS_VALID_PARENTHESIZATION_SMOKE_MANIFEST_V1.json",
    "research/quixbugs/KHEAPSORT_SMOKE_MANIFEST_V1.json",
]

# The exact eight algorithm names declared by the pilot manifest. The complete
# verdict is only valid when the exact eight declared algorithms were each
# executed exactly once and all passed -- never for an empty run, a subset, a
# superset, a duplicate, or an unknown --only value. Counts alone are
# insufficient, and reducing to a set is insufficient (it collapses duplicates);
# the verdict validates ordered lists with exactly-once semantics.
EXPECTED_SELECTED_ALGORITHMS = (
    "gcd",
    "bucketsort",
    "find_in_sorted",
    "flatten",
    "kth",
    "hanoi",
    "is_valid_parenthesization",
    "kheapsort",
)
EXPECTED_SELECTED_COUNT = len(EXPECTED_SELECTED_ALGORITHMS)

# Hard cap on the total number of unique candidates whose tests may be executed
# through the resource-limited verifier during one campaign (selected + excluded).
# Matches the pilot manifest's ``candidate_cap``. Enforced in the orchestration
# path so a future run cannot silently exceed it.
CANDIDATE_CAP = 12


class SetupError(RuntimeError):
    """A safety-critical environment setup check failed.

    Raised (never asserted) so failure stops before task execution even under
    ``python -O`` (which strips bare ``assert`` statements).
    """


class OrchestrationError(RuntimeError):
    """The orchestration cannot proceed: bad --only selection, cap exceeded, etc."""


def _phase(name: str) -> None:
    print(f"\n=== {name} ===", file=sys.stderr, flush=True)


def _setup_environment() -> tuple[WslProcess, WslBubblewrapRunner, str, str, str, dict]:
    if not EXTERNAL_ROOT_POSIX.startswith("/"):
        raise SetupError(
            "set AGENTIC_DEBUGGER_QUIXBUGS_ROOT to the absolute WSL path of "
            "the prepared QuixBugs environment"
        )
    process = WslProcess(DISTRO)
    venv_posix = f"{EXTERNAL_ROOT_POSIX}/python-env/py310"
    report: dict = {}

    _phase("layout")
    layout_script = (
        f"set -e; "
        f"mkdir -p {EXTERNAL_ROOT_POSIX}/sources {EXTERNAL_ROOT_POSIX}/python-env "
        f"{EXTERNAL_ROOT_POSIX}/runs {EXTERNAL_ROOT_POSIX}/cache {EXTERNAL_ROOT_POSIX}/evidence "
        f"{EXTERNAL_ROOT_POSIX}/runtime/empty; "
        f"echo done"
    )
    result = process.run(["bash", "-c", layout_script], timeout_seconds=60)
    if result.exit_code != 0:
        raise SetupError(f"failed to create external root layout (exit_code={result.exit_code})")
    report["layout"] = {"exit_code": result.exit_code}

    _phase("venv reuse check (do not reinstall unless the pinned pytest version is missing)")
    check_script = f"{venv_posix}/bin/python -m pytest --version 2>&1 || true"
    check = process.run(["bash", "-c", check_script], timeout_seconds=30)
    venv_reusable = f"pytest {PYTEST_VERSION}" in check.stdout
    report["venv_reused"] = venv_reusable
    print(f"  reusable={venv_reusable}: {check.stdout.strip()}", file=sys.stderr)

    if not venv_reusable:
        _phase("venv + pip bootstrap (network, dependency-prep phase only)")
        setup_script = (
            f"set -e; "
            f"rm -rf {venv_posix}; "
            f"python3 -m venv --copies --without-pip {venv_posix}; "
            f"curl -sS https://bootstrap.pypa.io/get-pip.py -o {EXTERNAL_ROOT_POSIX}/cache/get-pip.py; "
            f"{venv_posix}/bin/python {EXTERNAL_ROOT_POSIX}/cache/get-pip.py --no-input --quiet; "
            f"{venv_posix}/bin/python -m pip install --no-input --quiet "
            f"--cache-dir {EXTERNAL_ROOT_POSIX}/cache/pip 'pytest=={PYTEST_VERSION}'; "
            f"{venv_posix}/bin/python --version; "
            f"{venv_posix}/bin/python -m pytest --version; "
            f"{venv_posix}/bin/python -m pip freeze"
        )
        result = process.run(["bash", "-c", setup_script], timeout_seconds=180)
        if result.exit_code != 0:
            raise SetupError(f"failed to build task-local venv (exit_code={result.exit_code})")
        freeze_stdout = result.stdout
    else:
        freeze_result = process.run(["bash", "-c", f"{venv_posix}/bin/python -m pip freeze"], timeout_seconds=30)
        freeze_stdout = freeze_result.stdout

    package_list = [line.strip() for line in freeze_stdout.splitlines() if "==" in line]
    env_fingerprint = fingerprint_environment({"python_version": PYTHON_VERSION, "packages": ",".join(sorted(package_list))})
    report["environment"] = {
        "python_executable_posix": f"{venv_posix}/bin/python",
        "python_version": PYTHON_VERSION,
        "packages": package_list,
        "fingerprint": env_fingerprint,
    }

    _phase("build WslBubblewrapRunner")
    root_host = wsl_unc_path(EXTERNAL_ROOT_POSIX, DISTRO)
    runner = WslBubblewrapRunner(
        root_host=root_host,
        python_root_posix=venv_posix,
        python_executable_posix=f"{venv_posix}/bin/python",
        distro=DISTRO,
    )

    selftest_posix = f"{EXTERNAL_ROOT_POSIX}/runs/selftest"
    result = process.run(["bash", "-c", f"mkdir -p {selftest_posix}"], timeout_seconds=30)
    if result.exit_code != 0:
        raise SetupError(f"failed to create selftest workspace (exit_code={result.exit_code})")

    _phase("Bubblewrap self-tests (network/mounts/home/write/readonly/child-isolation/python)")
    selftest_host = wsl_unc_path(selftest_posix, DISTRO)
    bwrap_results = runner.self_test(selftest_host, expected_python_version=PYTHON_VERSION)
    report["bubblewrap_self_test"] = bwrap_results
    failed_selftests = [name for name, entry in bwrap_results.items() if not entry["passed"]]
    if failed_selftests:
        raise SetupError(f"Bubblewrap self-test failed: {failed_selftests}")

    _phase("resource readiness: internal live probes + non-forgeable gate open")
    profile = ResourceLimits(cpu_seconds=5, memory_bytes=268435456, max_processes=8)
    readiness_results = runner.verify_and_open_resource_isolation(selftest_host, profile)
    report["resource_readiness"] = readiness_results
    if runner.resource_isolation_ready is not True:
        raise SetupError("resource isolation gate did not open (runner.resource_isolation_ready is not True)")
    report["resource_profile"] = profile.to_mapping()
    report["boundary_guarantee"] = runner.boundary_guarantee

    return process, runner, root_host, venv_posix, env_fingerprint, report


def _dependencies_for(adapter: QuixBugsAdapter, env_fingerprint: str) -> DependencyPreparation:
    recipe_description = f"pytest=={PYTEST_VERSION}"
    return DependencyPreparation(
        pilot_task_id=adapter.manifest.task_id,
        manifest_fingerprint=adapter.manifest.fingerprint,
        authority_revision=adapter.manifest.authority_revision,
        project="quixbugs",
        bug_id=adapter.manifest.algorithm,
        buggy_revision=adapter.manifest.authority_revision,
        recipe_path=recipe_description,
        recipe_sha256=hashlib.sha256(recipe_description.encode("utf-8")).hexdigest(),
        installed_fingerprint=env_fingerprint,
    )


def _run_one_task(
    *, manifest_path: Path, root_host: str, venv_posix: str, runner: WslBubblewrapRunner,
    env_fingerprint: str, sources_host: str, runs_host: str,
) -> dict:
    adapter = QuixBugsAdapter.from_manifest(manifest_path)
    context = create_verified_context(
        root_host=root_host,
        python_root_posix=venv_posix,
        python_executable_posix=f"{venv_posix}/bin/python",
        python_version=PYTHON_VERSION,
        project_cwd=".",
        pythonpath=(),
        reviewed_environment={},
        dependencies=_dependencies_for(adapter, env_fingerprint),
        runner=runner,
    )
    facts = QuixBugsPreflightFacts(
        platform="linux",
        pinned_source_verified=True,
        license_reviewed=True,
        test_command_available=True,
        workspace_cleanup_ready=True,
        target_annotation_reviewed=True,
        external_parent=runs_host,
        execution_context=context,
    )
    preflight = adapter.preflight(facts, repository_root=str(REPO_ROOT))
    if not preflight.authorized:
        return {
            "task_id": adapter.manifest.task_id,
            "algorithm": adapter.manifest.algorithm,
            "verdict": "IMPLEMENTED_BASELINE_BLOCKED",
            "preflight": preflight.to_mapping(),
            "duration_seconds": 0.0,
        }

    smoke = QuixBugsSmokeRunner(adapter, QuixBugsSourceAcquirer())
    start = time.monotonic()
    evidence = smoke.run(facts=facts, sources_parent=sources_host, external_parent=runs_host, repository_root=str(REPO_ROOT))
    duration = time.monotonic() - start

    evidence_map = evidence.to_mapping()
    semantic = evidence_map["evaluation"] or {}
    full_suite_counts = (semantic.get("full_suite") or {}).get("counts") or {}
    requirements_met = (
        evidence.discovery is not None
        and len(evidence.discovery.f2p_candidates) >= 1
        and len(evidence.discovery.p2p_candidates) >= 1
        and evidence.discovery.oracle_correct_exit_code == 0
        and semantic.get("status") == "COMPLETED"
        and semantic.get("f2p_total") == semantic.get("f2p_passed")
        and semantic.get("p2p_total") == semantic.get("p2p_passed")
        and full_suite_counts.get("failed") == 0
        and semantic.get("workspace", {}).get("lifecycle") == "CLEANED"
        and semantic.get("workspace", {}).get("canonical_fixture_unchanged") is True
    )
    final_pass = evidence.verdict == "REAL_SMOKE_PASSED" and requirements_met and evidence.cleanup_succeeded
    return {
        "task_id": adapter.manifest.task_id,
        "algorithm": adapter.manifest.algorithm,
        "verdict": "ACCEPT_CANDIDATE_REAL_SMOKE_PASSED" if final_pass else "IMPLEMENTED_REAL_SMOKE_BLOCKED",
        "duration_seconds": round(duration, 3),
        "evidence": evidence_map,
        "requirements_met": requirements_met,
    }


def _run_excluded_candidate(*, manifest_path: Path, root_host: str, venv_posix: str, runner: WslBubblewrapRunner, sources_host: str, runs_host: str) -> dict:
    """Formally execute discovery only (no gold patch/verifier) for a candidate
    expected to fail the 'reproducible failing node' eligibility rule, and
    record exactly why -- through the same resource-limited sandbox so any
    non-terminating buggy baseline is safely killed by the enforced CPU-time
    and wall-clock limits rather than actually hanging."""
    adapter = QuixBugsAdapter.from_manifest(manifest_path)
    env_fingerprint = "excluded-candidate-screening"  # not used for any authorized run
    context = create_verified_context(
        root_host=root_host,
        python_root_posix=venv_posix,
        python_executable_posix=f"{venv_posix}/bin/python",
        python_version=PYTHON_VERSION,
        project_cwd=".",
        pythonpath=(),
        reviewed_environment={},
        dependencies=_dependencies_for(adapter, env_fingerprint),
        runner=runner,
    )
    source_dir = Path(sources_host) / "quixbugs"
    external = ExternalWorkspace.create(runs_host, repository_root=str(REPO_ROOT), containment_root=context.containment.root)
    external.verifier_workspace_parent.mkdir(parents=True, exist_ok=True)
    external.assert_contained(external.verifier_workspace_parent)
    start = time.monotonic()
    workspace = TaskWorkspace(str(source_dir), parent_dir=str(external.verifier_workspace_parent))
    try:
        smoke = QuixBugsSmokeRunner(adapter, QuixBugsSourceAcquirer())
        try:
            record = smoke.discover(context, workspace)
            duration = time.monotonic() - start
            result = {
                "algorithm": adapter.manifest.algorithm,
                "status": "UNEXPECTED_ELIGIBLE",
                "duration_seconds": round(duration, 3),
                "discovery": record.to_mapping(),
            }
        except DiscoveryError as exc:
            duration = time.monotonic() - start
            result = {
                "algorithm": adapter.manifest.algorithm,
                "status": "EXCLUDED",
                "duration_seconds": round(duration, 3),
                "discovery_error": str(exc),
            }
    finally:
        workspace.cleanup()
        external.cleanup()
    return result


def _algorithm_from_manifest_path(relative: str) -> str:
    """Extract the canonical algorithm name from a selected manifest path.

    The pilot manifest declares each selected task's ``algorithm``; the
    selected manifest paths follow ``research/quixbugs/<ALGO>_SMOKE_MANIFEST_V1.json``,
    so the algorithm is the stem with the ``_SMOKE_MANIFEST_V1`` suffix
    stripped, lowercased. This is used only to resolve ``--only`` against
    SELECTED_MANIFESTS -- the manifest's own ``target.algorithm`` remains the
    authoritative identity used for execution.
    """
    stem = Path(relative).stem
    suffix = "_SMOKE_MANIFEST_V1"
    if not stem.endswith(suffix):
        raise OrchestrationError(f"unexpected manifest path (not a *_SMOKE_MANIFEST_V1.json): {relative}")
    return stem[: -len(suffix)].lower()


def load_declared_selected_algorithms(pilot_manifest_path: str | Path) -> tuple[str, ...]:
    """Load the exact ordered list of declared selected algorithm names from the pilot manifest.

    The pilot manifest's ``selected_tasks`` array is the authoritative
    declaration of which eight algorithms constitute the complete baseline.
    This reads it directly so the verdict comparison is against the manifest's
    own declaration, not a hardcoded constant that could drift from it.

    Rejects:
    - a missing or empty ``selected_tasks`` array,
    - a malformed entry (missing/non-string ``algorithm``),
    - a duplicate algorithm entry,
    - a count other than exactly eight unique selected tasks.
    """
    with open(pilot_manifest_path, encoding="utf-8") as fh:
        data = json.load(fh)
    tasks = data.get("selected_tasks")
    if not isinstance(tasks, list) or not tasks:
        raise OrchestrationError(f"pilot manifest {pilot_manifest_path} has no selected_tasks")
    algorithms: list[str] = []
    for entry in tasks:
        if not isinstance(entry, dict) or not isinstance(entry.get("algorithm"), str) or not entry["algorithm"]:
            raise OrchestrationError(f"pilot manifest {pilot_manifest_path} has a malformed selected_tasks entry: {entry!r}")
        algorithms.append(entry["algorithm"].lower())
    dup_counts = _Counter(algorithms)
    duplicates = sorted(name for name, count in dup_counts.items() if count > 1)
    if duplicates:
        raise OrchestrationError(f"pilot manifest {pilot_manifest_path} has duplicate selected_tasks entries: {duplicates}")
    if len(algorithms) != EXPECTED_SELECTED_COUNT:
        raise OrchestrationError(
            f"pilot manifest {pilot_manifest_path} declares {len(algorithms)} selected tasks, "
            f"expected exactly {EXPECTED_SELECTED_COUNT}"
        )
    return tuple(algorithms)


def select_manifests(only: Sequence[str]) -> list[str]:
    """Resolve ``--only`` against SELECTED_MANIFESTS using exact algorithm names.

    ``--only`` accepts exact algorithm names (e.g. ``gcd``, ``kth``,
    ``kheapsort``), never prefix matches: ``--only kth`` selects only ``kth``,
    not ``kheapsort``. An empty ``only`` returns every selected manifest.
    Unknown names raise ``OrchestrationError`` so a typo can never silently
    produce an empty or wrong subset run.
    """
    if not only:
        return list(SELECTED_MANIFESTS)
    lowered = [name.lower() for name in only]
    requested = set(lowered)
    available = {_algorithm_from_manifest_path(m): m for m in SELECTED_MANIFESTS}
    unknown = sorted(requested - available.keys())
    if unknown:
        raise OrchestrationError(
            f"unknown --only algorithm name(s): {unknown}; "
            f"available: {sorted(available)}"
        )
    duplicates = [name for name, count in _Counter(lowered).items() if count > 1]
    if duplicates:
        raise OrchestrationError(f"duplicate --only algorithm name(s): {duplicates}")
    return [available[name] for name in sorted(requested, key=lambda n: list(available.keys()).index(n))]


def complete_verdict(solved_algorithms: Sequence[str], executed_algorithms: Sequence[str], declared_algorithms: Sequence[str]) -> str:
    """Return the complete verdict only when the exact declared list was executed once and all passed.

    Compares ordered lists, **not** frozensets -- reducing to a set would
    collapse duplicates, so a run where ``gcd`` executed twice and ``kheapsort``
    never executed could still pass a set comparison if the right eight unique
    names are present. Exactly-once semantics require:
    - the executed list equals the declared list (same length, same elements,
      no duplicates, no missing, no extra),
    - the solved list equals the declared list (all passed, no unsolved).
    """
    executed_list = list(executed_algorithms)
    solved_list = list(solved_algorithms)
    declared_list = list(declared_algorithms)
    if executed_list == declared_list and solved_list == declared_list:
        return "ACCEPT_CANDIDATE_EIGHT_TASK_BASELINE_COMPLETE"
    return "IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED"


def exit_code_for(verdict: str, executed_algorithms: Sequence[str], declared_algorithms: Sequence[str]) -> int:
    """Exit 0 only for the complete eight-task verdict with the exact declared list executed once.

    A partially-blocked run (including an empty, subset, or duplicate run)
    must exit 1 so a shell/CI caller cannot mistake it for a complete campaign.
    """
    if verdict == "ACCEPT_CANDIDATE_EIGHT_TASK_BASELINE_COMPLETE" and list(executed_algorithms) == list(declared_algorithms):
        return 0
    return 1


def verdict_discrepancy(solved_algorithms: Sequence[str], executed_algorithms: Sequence[str], declared_algorithms: Sequence[str]) -> dict[str, list[str]]:
    """Describe exactly how an executed/solved list differs from the declared list.

    Used in the aggregate report so a partial run's evidence states which
    algorithms are missing, extra, duplicate, or unsolved rather than only a
    count. Uses set arithmetic for missing/extra (which algorithms never
    appeared or appeared that should not have) and separately reports duplicates
    and unsolved entries.
    """
    declared_set = set(declared_algorithms)
    executed_set = set(executed_algorithms)
    solved_set = set(solved_algorithms)
    missing = sorted(declared_set - executed_set)
    extra = sorted(executed_set - declared_set)
    executed_not_solved = sorted(executed_set - solved_set)
    dup_counts = _Counter(executed_algorithms)
    duplicates = sorted(name for name, count in dup_counts.items() if count > 1)
    return {
        "missing_from_declared": missing,
        "extra_beyond_declared": extra,
        "executed_not_solved": executed_not_solved,
        "duplicate_executions": duplicates,
    }


def enforce_candidate_cap(selected_count: int, excluded_count: int) -> None:
    """Enforce the 12-unique-candidate cap in the orchestration path.

    Every candidate whose tests are executed through the resource-limited
    verifier -- selected tasks plus excluded/screened candidates -- counts
    against the cap. Exceeding it raises ``OrchestrationError`` before any
    further execution.
    """
    total = selected_count + excluded_count
    if total > CANDIDATE_CAP:
        raise OrchestrationError(
            f"candidate cap exceeded: {selected_count} selected + {excluded_count} excluded = {total} > {CANDIDATE_CAP}"
        )


def _load_excluded_algorithm(manifest_path: str | Path) -> str:
    """Load the algorithm identity from an excluded-candidate manifest.

    Reads only the manifest's ``target.algorithm`` -- no network, no WSL, no
    source acquisition. Used to detect selected/excluded overlap and to count
    excluded candidates against the cap before any environment side effect.
    """
    with open(manifest_path, encoding="utf-8") as fh:
        data = json.load(fh)
    target = data.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("algorithm"), str) or not target["algorithm"]:
        raise OrchestrationError(f"excluded manifest {manifest_path} has no valid target.algorithm")
    return target["algorithm"].lower()


def validate_campaign(
    only: Sequence[str],
    excluded_manifest_paths: Sequence[str | Path],
    *,
    skip_excluded: bool = False,
    pilot_manifest_path: str | Path = PILOT_MANIFEST,
) -> tuple[list[str], tuple[str, ...], list[str]]:
    """Perform all local validation before any environment side effect.

    Invalid input, manifest drift, or cap violation must raise
    ``OrchestrationError`` here -- never after WSL, network, venv setup,
    Bubblewrap, or resource probes have been invoked. Returns the resolved
    selected manifest paths, the declared algorithm list, and the excluded
    algorithm list.

    Checks (in order):
    1. Load and validate the pilot manifest (exactly eight unique selected
       tasks, no duplicates, no malformed entries).
    2. Resolve and validate ``--only`` (exact names, no unknowns, no
       duplicates).
    3. Verify SELECTED_MANIFESTS matches the pilot manifest exactly (no
       drift, no duplicates in the constant).
    4. Load excluded-manifest identities (no network).
    5. Reject duplicate excluded algorithms.
    6. Reject selected/excluded-overlapping algorithms.
    7. Enforce the 12-unique-candidate cap.
    """
    # 1. Load and validate the pilot manifest.
    declared_algorithms = load_declared_selected_algorithms(pilot_manifest_path)

    # 2. Resolve and validate --only.
    selected = select_manifests(only)

    # 3. Verify SELECTED_MANIFESTS matches the pilot manifest exactly.
    selected_from_constant = [_algorithm_from_manifest_path(m) for m in SELECTED_MANIFESTS]
    if len(selected_from_constant) != len(set(selected_from_constant)):
        dupes = sorted(name for name, count in _Counter(selected_from_constant).items() if count > 1)
        raise OrchestrationError(f"SELECTED_MANIFESTS contains duplicate algorithm(s): {dupes}")
    if sorted(selected_from_constant) != sorted(declared_algorithms):
        raise OrchestrationError(
            f"manifest drift: SELECTED_MANIFESTS resolves to {sorted(selected_from_constant)} "
            f"but the pilot manifest declares {sorted(declared_algorithms)}"
        )

    # 4-6. Load excluded identities, reject duplicates and overlap.
    excluded_algorithms: list[str] = []
    if not skip_excluded:
        for path_str in excluded_manifest_paths:
            algo = _load_excluded_algorithm(path_str)
            excluded_algorithms.append(algo)
        excluded_dupes = sorted(name for name, count in _Counter(excluded_algorithms).items() if count > 1)
        if excluded_dupes:
            raise OrchestrationError(f"duplicate excluded-candidate algorithm(s): {excluded_dupes}")
        selected_set = set(declared_algorithms)
        overlap = sorted(set(excluded_algorithms) & selected_set)
        if overlap:
            raise OrchestrationError(
                f"selected/excluded overlap: algorithm(s) {overlap} appear in both the "
                f"pilot manifest's selected_tasks and an excluded-candidate manifest"
            )

    # 7. Enforce the 12-unique-candidate cap.
    enforce_candidate_cap(len(selected), 0 if skip_excluded else len(excluded_algorithms))

    return selected, declared_algorithms, excluded_algorithms


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--excluded-manifest", action="append", default=[], help="path to an excluded-candidate manifest (screened, not selected)")
    parser.add_argument("--skip-excluded", action="store_true", help="skip re-running the excluded-candidate screening evidence")
    parser.add_argument("--only", action="append", default=[], help="restrict the selected-task run to these exact algorithm names (repeatable); default is all SELECTED_MANIFESTS")
    args = parser.parse_args()

    # All local validation BEFORE any environment side effect: invalid input,
    # manifest drift, or cap violation must not invoke WSL, network, venv
    # setup, Bubblewrap, or resource probes.
    selected, declared_algorithms, excluded_algorithms = validate_campaign(
        args.only,
        args.excluded_manifest,
        skip_excluded=args.skip_excluded,
    )

    report: dict = {"pilot_manifest": str(PILOT_MANIFEST)}
    process, runner, root_host, venv_posix, env_fingerprint, setup_report = _setup_environment()
    report["setup"] = setup_report

    runs_host = wsl_unc_path(f"{EXTERNAL_ROOT_POSIX}/runs", DISTRO)
    sources_host = wsl_unc_path(f"{EXTERNAL_ROOT_POSIX}/sources", DISTRO)

    tasks: list[dict] = []
    total_start = time.monotonic()
    for relative in selected:
        manifest_path = REPO_ROOT / relative
        _phase(f"task: {manifest_path.name}")
        result = _run_one_task(
            manifest_path=manifest_path, root_host=root_host, venv_posix=venv_posix, runner=runner,
            env_fingerprint=env_fingerprint, sources_host=sources_host, runs_host=runs_host,
        )
        print(json.dumps({"algorithm": result["algorithm"], "verdict": result["verdict"], "duration_seconds": result["duration_seconds"]}), file=sys.stderr)
        tasks.append(result)
    report["tasks"] = tasks
    report["total_duration_seconds"] = round(time.monotonic() - total_start, 3)

    excluded: list[dict] = []
    if not args.skip_excluded:
        for path_str in args.excluded_manifest:
            manifest_path = Path(path_str)
            _phase(f"excluded candidate screening: {manifest_path.name}")
            result = _run_excluded_candidate(
                manifest_path=manifest_path, root_host=root_host, venv_posix=venv_posix, runner=runner, sources_host=sources_host, runs_host=runs_host,
            )
            print(json.dumps(result), file=sys.stderr)
            excluded.append(result)
    report["excluded_candidates"] = excluded

    solved_list = [t["algorithm"].lower() for t in tasks if t["verdict"] == "ACCEPT_CANDIDATE_REAL_SMOKE_PASSED"]
    executed_list = [t["algorithm"].lower() for t in tasks]
    executed = len(tasks)
    discrepancy = verdict_discrepancy(solved_list, executed_list, declared_algorithms)
    report["aggregate"] = {
        "selected_task_count": executed,
        "solved_count": len(solved_list),
        "solved_rate": round(len(solved_list) / executed, 4) if executed else 0.0,
        "all_passed": solved_list == executed_list,
        "expected_selected_count": EXPECTED_SELECTED_COUNT,
        "declared_algorithms": list(declared_algorithms),
        "executed_algorithms": executed_list,
        "solved_algorithms": solved_list,
        "is_complete_eight_task_baseline": executed_list == list(declared_algorithms) and solved_list == list(declared_algorithms),
        "discrepancy": discrepancy,
    }
    report["candidate_accounting"] = {
        "candidate_cap": CANDIDATE_CAP,
        "selected_count": executed,
        "excluded_count": len(excluded),
        "total_candidates_attempted": executed + len(excluded),
        "cap_compliance": "within-cap" if executed + len(excluded) <= CANDIDATE_CAP else "exceeded",
    }
    verdict = complete_verdict(solved_list, executed_list, declared_algorithms)
    report["verdict"] = verdict
    print(json.dumps(report, indent=2, default=str))
    return exit_code_for(verdict, executed_list, declared_algorithms)


if __name__ == "__main__":
    raise SystemExit(main())
