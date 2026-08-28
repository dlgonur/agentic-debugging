"""Operator entry point: one deterministic, no-model contained-PDB
reachability case for the pinned QuixBugs ``gcd`` task.

Mirrors the read-only readiness verification already accepted in
``scripts/quixbugs_live_smoke.py``: it never installs a dependency, never
clones or resets the pinned checkout, and never mutates the WSL/Bubblewrap
environment beyond an owned disposable case workspace under
``EXTERNAL_ROOT_POSIX/runs`` (removed before this script returns).

This script makes no model or provider call. It drives exactly one fixed,
scripted controller sequence (see
``agentic_debugger.quixbugs.contained_pdb.DeterministicPdbReachabilityDriver``)
through the real controller, the real ``decide_pdb_access`` gate, and the real
PDB protocol, with both the PDB worker and the debug target running inside
the verified WSL/Bubblewrap boundary.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.bugsinpy.wsl import (  # noqa: E402
    ResourceLimits,
    WslBubblewrapRunner,
    WslProcess,
    create_verified_context,
    fingerprint_environment,
    wsl_unc_path,
)
from agentic_debugger.quixbugs.adapter import QuixBugsPreflightFacts  # noqa: E402
from agentic_debugger.quixbugs.contained_pdb import run_quixbugs_gcd_pdb_reachability_case  # noqa: E402
from agentic_debugger.runtime.execution import DependencyPreparation  # noqa: E402

DISTRO = "Ubuntu-22.04"
EXTERNAL_ROOT_POSIX = os.environ.get("AGENTIC_DEBUGGER_QUIXBUGS_ROOT", "").rstrip("/")
PYTEST_VERSION = "7.4.4"
PYTHON_VERSION = "3.10.12"
DEFAULT_MANIFEST = REPO_ROOT / "research" / "quixbugs" / "GCD_SMOKE_MANIFEST_V1.json"


class ReadinessError(RuntimeError):
    """A prerequisite (venv, pinned source) is missing.

    Raised rather than silently installing/cloning it: this script is not
    authorized to install dependencies or acquire sources.
    """


def _phase(name: str) -> None:
    print(f"\n=== {name} ===", file=sys.stderr, flush=True)


def _verify_environment_ready() -> tuple[WslBubblewrapRunner, str, str, str]:
    if not EXTERNAL_ROOT_POSIX.startswith("/"):
        raise ReadinessError(
            "set AGENTIC_DEBUGGER_QUIXBUGS_ROOT to the absolute WSL path of "
            "the prepared QuixBugs environment"
        )
    process = WslProcess(DISTRO)
    venv_posix = f"{EXTERNAL_ROOT_POSIX}/python-env/py310"

    _phase("readiness: pinned source already acquired (read-only)")
    source_check = process.run(
        ["bash", "-c", f"test -d {EXTERNAL_ROOT_POSIX}/sources/quixbugs && echo present || echo absent"],
        timeout_seconds=30,
    )
    if "present" not in source_check.stdout:
        raise ReadinessError("pinned QuixBugs source is not already acquired; this script is not authorized to clone it")

    _phase("readiness: pinned venv already built (read-only)")
    check = process.run(["bash", "-c", f"{venv_posix}/bin/python -m pytest --version 2>&1 || true"], timeout_seconds=30)
    if f"pytest {PYTEST_VERSION}" not in check.stdout:
        raise ReadinessError("pinned venv/pytest is not already installed at the expected version")
    freeze = process.run(["bash", "-c", f"{venv_posix}/bin/python -m pip freeze"], timeout_seconds=30)
    package_list = [line.strip() for line in freeze.stdout.splitlines() if "==" in line]
    env_fingerprint = fingerprint_environment({"python_version": PYTHON_VERSION, "packages": ",".join(sorted(package_list))})

    _phase("build WslBubblewrapRunner (no install, no clone)")
    root_host = wsl_unc_path(EXTERNAL_ROOT_POSIX, DISTRO)
    runner = WslBubblewrapRunner(root_host=root_host, python_root_posix=venv_posix, python_executable_posix=f"{venv_posix}/bin/python", distro=DISTRO)

    _phase("Bubblewrap self-tests (network/mounts/home/write/readonly/child-isolation/python)")
    selftest_posix = f"{EXTERNAL_ROOT_POSIX}/runs/selftest"
    result = process.run(["bash", "-c", f"mkdir -p {selftest_posix}"], timeout_seconds=30)
    if result.exit_code != 0:
        raise ReadinessError("failed to create the selftest workspace directory")
    selftest_host = wsl_unc_path(selftest_posix, DISTRO)
    bwrap_results = runner.self_test(selftest_host, expected_python_version=PYTHON_VERSION)
    failed = [name for name, entry in bwrap_results.items() if not entry["passed"]]
    if failed:
        raise ReadinessError(f"Bubblewrap self-test failed: {failed}")

    _phase("resource readiness: internal live probes + non-forgeable gate open")
    profile = ResourceLimits(cpu_seconds=5, memory_bytes=268435456, max_processes=8)
    runner.verify_and_open_resource_isolation(selftest_host, profile)
    if runner.resource_isolation_ready is not True:
        raise ReadinessError("resource isolation gate did not open")

    return runner, root_host, venv_posix, env_fingerprint


def main() -> int:
    output = Path("quixbugs-gcd-pdb-reachability-results.json")

    try:
        runner, root_host, venv_posix, env_fingerprint = _verify_environment_ready()
    except ReadinessError as exc:
        payload = {"verdict": "BLOCKED_ENVIRONMENT_NOT_READY", "reason": str(exc)}
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2))
        return 2

    from agentic_debugger.quixbugs.adapter import QuixBugsAdapter

    adapter = QuixBugsAdapter.from_manifest(DEFAULT_MANIFEST)
    recipe_description = f"pytest=={PYTEST_VERSION}"
    import hashlib

    dependencies = DependencyPreparation(
        pilot_task_id=adapter.manifest.task_id, manifest_fingerprint=adapter.manifest.fingerprint,
        authority_revision=adapter.manifest.authority_revision, project="quixbugs", bug_id=adapter.manifest.algorithm,
        buggy_revision=adapter.manifest.authority_revision, recipe_path=recipe_description,
        recipe_sha256=hashlib.sha256(recipe_description.encode("utf-8")).hexdigest(), installed_fingerprint=env_fingerprint,
    )
    execution_context = create_verified_context(
        root_host=root_host, python_root_posix=venv_posix, python_executable_posix=f"{venv_posix}/bin/python",
        python_version=PYTHON_VERSION, project_cwd=".", pythonpath=(), reviewed_environment={}, dependencies=dependencies, runner=runner,
    )
    runs_host = wsl_unc_path(f"{EXTERNAL_ROOT_POSIX}/runs", DISTRO)
    sources_host = wsl_unc_path(f"{EXTERNAL_ROOT_POSIX}/sources", DISTRO)
    facts = QuixBugsPreflightFacts(
        platform="linux", pinned_source_verified=True, license_reviewed=True, test_command_available=True,
        workspace_cleanup_ready=True, target_annotation_reviewed=True, external_parent=runs_host, execution_context=execution_context,
    )
    resource_limits = ResourceLimits(cpu_seconds=5, memory_bytes=268435456, max_processes=8)

    _phase("deterministic no-model contained-PDB reachability case")
    result = run_quixbugs_gcd_pdb_reachability_case(
        repository_root=str(REPO_ROOT), manifest_path=str(DEFAULT_MANIFEST), sources_parent=sources_host,
        facts=facts, resource_limits=resource_limits,
    )
    payload = result.to_mapping()
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))

    return 0 if result.verdict == "REACHABILITY_CASE_PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
