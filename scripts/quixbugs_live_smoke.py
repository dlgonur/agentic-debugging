"""Uncommitted operator orchestration script for the QuixBugs gcd real smoke.

This script is not part of the automated test suite. It builds the concrete
WSL/Bubblewrap execution context (network, venv, resource limits) that
cannot be reconstructed from JSON, then drives the existing
``QuixBugsSmokeRunner`` through the real WSL distro. It prints a JSON
evidence bundle to stdout; the operator archives that output into the
review package.

Reuses the existing pinned source and Python environment when they already
match the pinned revision / pinned pytest version -- it does not reinstall
or re-clone unnecessarily.
"""

from __future__ import annotations

import hashlib
import json
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
from agentic_debugger.quixbugs.adapter import (  # noqa: E402
    QuixBugsAdapter,
    QuixBugsPreflightFacts,
    QuixBugsSmokeRunner,
    QuixBugsSourceAcquirer,
)
from agentic_debugger.runtime.execution import DependencyPreparation  # noqa: E402

DISTRO = "Ubuntu-22.04"
EXTERNAL_ROOT_POSIX = "/home/benya/.local/share/agentic-debugging-internship/quixbugs-smoke-v1"
PYTEST_VERSION = "7.4.4"
PYTHON_VERSION = "3.10.12"
MANIFEST = REPO_ROOT / "research" / "quixbugs" / "GCD_SMOKE_MANIFEST_V1.json"

report: dict = {}


def _phase(name: str) -> None:
    print(f"\n=== {name} ===", file=sys.stderr, flush=True)


def main() -> int:
    process = WslProcess(DISTRO)
    venv_posix = f"{EXTERNAL_ROOT_POSIX}/python-env/py310"

    _phase("layout")
    layout_script = (
        f"set -e; "
        f"mkdir -p {EXTERNAL_ROOT_POSIX}/sources {EXTERNAL_ROOT_POSIX}/python-env "
        f"{EXTERNAL_ROOT_POSIX}/runs {EXTERNAL_ROOT_POSIX}/cache {EXTERNAL_ROOT_POSIX}/evidence "
        f"{EXTERNAL_ROOT_POSIX}/runtime/empty; "
        f"echo done"
    )
    result = process.run(["bash", "-c", layout_script], timeout_seconds=60)
    print(result.stdout, result.stderr, file=sys.stderr)
    assert result.exit_code == 0, "failed to create external root layout"
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
            # --copies: the venv's bin/python must be a real file, not a symlink
            # chain ending in an absolute host path (e.g. /usr/bin/python3) --
            # the Windows \\\\wsl.localhost\\ bridge cannot resolve that hop, which
            # would make python_executable invisible to the Windows-side gate checks.
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
        print(result.stdout, result.stderr, file=sys.stderr)
        assert result.exit_code == 0, "failed to build task-local venv"
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

    _phase("layout: create selftest workspace dir")
    selftest_posix = f"{EXTERNAL_ROOT_POSIX}/runs/selftest"
    result = process.run(["bash", "-c", f"mkdir -p {selftest_posix}"], timeout_seconds=30)
    assert result.exit_code == 0

    _phase("Bubblewrap self-tests (network/mounts/home/write/readonly/child-isolation/python)")
    selftest_host = wsl_unc_path(selftest_posix, DISTRO)
    bwrap_results = runner.self_test(selftest_host, expected_python_version=PYTHON_VERSION)
    report["bubblewrap_self_test"] = bwrap_results
    for name, entry in bwrap_results.items():
        print(f"  {name}: passed={entry['passed']}", file=sys.stderr)
    assert all(entry["passed"] for entry in bwrap_results.values())

    _phase("resource readiness: internal live probes + non-forgeable gate open")
    profile = ResourceLimits(cpu_seconds=5, memory_bytes=268435456, max_processes=8)
    readiness_results = runner.verify_and_open_resource_isolation(selftest_host, profile)
    report["resource_readiness"] = readiness_results
    for name, entry in readiness_results.items():
        print(f"  {name}: passed={entry['passed']} exit_code={entry['exit_code']} detail={entry['detail']}", file=sys.stderr)
    assert runner.resource_isolation_ready is True
    report["resource_profile"] = profile.to_mapping()
    report["boundary_guarantee"] = runner.boundary_guarantee

    _phase("adapter + build VerifiedExecutionContext")
    adapter = QuixBugsAdapter.from_manifest(MANIFEST)
    recipe_description = f"pytest=={PYTEST_VERSION}"
    dependencies = DependencyPreparation(
        pilot_task_id=adapter.manifest.task_id,
        manifest_fingerprint=adapter.manifest.fingerprint,
        authority_revision=adapter.manifest.authority_revision,
        project="quixbugs",
        bug_id="gcd",
        buggy_revision=adapter.manifest.authority_revision,
        recipe_path=recipe_description,
        recipe_sha256=hashlib.sha256(recipe_description.encode("utf-8")).hexdigest(),
        installed_fingerprint=env_fingerprint,
    )
    context = create_verified_context(
        root_host=root_host,
        python_root_posix=venv_posix,
        python_executable_posix=f"{venv_posix}/bin/python",
        python_version=PYTHON_VERSION,
        project_cwd=".",
        pythonpath=(),
        reviewed_environment={},
        dependencies=dependencies,
        runner=runner,
    )
    runs_posix = f"{EXTERNAL_ROOT_POSIX}/runs"
    runs_host = wsl_unc_path(runs_posix, DISTRO)
    sources_host = wsl_unc_path(f"{EXTERNAL_ROOT_POSIX}/sources", DISTRO)
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
    report["preflight"] = preflight.to_mapping()
    print(json.dumps(preflight.to_mapping(), indent=2), file=sys.stderr)
    if not preflight.authorized:
        report["verdict"] = "IMPLEMENTED_REAL_SMOKE_BLOCKED"
        print(json.dumps(report, indent=2, default=str))
        return 2

    _phase("real no-model smoke (discovery + gold patch + verifier)")
    smoke = QuixBugsSmokeRunner(adapter, QuixBugsSourceAcquirer())
    evidence = smoke.run(facts=facts, sources_parent=sources_host, external_parent=runs_host, repository_root=str(REPO_ROOT))
    report["evidence"] = evidence.to_mapping()
    print(json.dumps(evidence.to_mapping(), indent=2, default=str), file=sys.stderr)

    discovery_ok = (
        evidence.discovery is not None
        and len(evidence.discovery.collected_nodes) == 6
        and len(evidence.discovery.f2p_candidates) == 5
        and len(evidence.discovery.p2p_candidates) == 1
        and evidence.discovery.oracle_correct_exit_code == 0
    )
    verifier_ok = False
    if evidence.evaluation is not None:
        semantic = evidence.evaluation.semantic_mapping() if hasattr(evidence.evaluation, "semantic_mapping") else {}
        full_suite_counts = (semantic.get("full_suite") or {}).get("counts") or {}
        verifier_ok = (
            semantic.get("status") == "COMPLETED"
            and semantic.get("f2p_total") == 5 and semantic.get("f2p_passed") == 5
            and semantic.get("p2p_total") == 1 and semantic.get("p2p_passed") == 1
            and full_suite_counts.get("passed") == 6 and full_suite_counts.get("failed") == 0
            and semantic.get("workspace", {}).get("lifecycle") == "CLEANED"
            and semantic.get("workspace", {}).get("canonical_fixture_unchanged") is True
        )
    report["discovery_requirements_met"] = discovery_ok
    report["verifier_requirements_met"] = verifier_ok

    final_pass = evidence.verdict == "REAL_SMOKE_PASSED" and discovery_ok and verifier_ok and evidence.cleanup_succeeded
    report["verdict"] = "ACCEPT_CANDIDATE_REAL_SMOKE_PASSED" if final_pass else "IMPLEMENTED_REAL_SMOKE_BLOCKED"
    print(json.dumps(report, indent=2, default=str))
    return 0 if final_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
