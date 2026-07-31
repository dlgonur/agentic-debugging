"""Operator entry point: protocol-1.3 live-model reachability case for the
pinned QuixBugs ``gcd`` task, policy ``pdb-on-uncertainty``, 1 repetition.

This script is not part of the automated test suite. It is the smallest
coherent path that carries the already-accepted QuixBugs ``gcd`` task
through the existing verified WSL/Bubblewrap execution boundary, the
existing protocol-1.3 ``LiveModelAdapter``/``DeterministicController``, and
the existing ``EvaluationVerifier`` -- see
``agentic_debugger.evaluation.live_quixbugs`` for the actual integration.

Unlike ``scripts/quixbugs_eight_task_baseline.py`` and
``scripts/quixbugs_live_smoke.py``, this script never installs a Python
dependency and never clones or re-clones the pinned QuixBugs source: both
the pinned venv and the pinned checkout must already exist from prior
accepted setup, or the script fails closed with a blocked report before any
WSL, provider, or filesystem-mutating action.

Both explicit authorization switches are mandatory:
``--i-understand-this-contacts-a-real-model`` and
``--i-authorize-live-opencode-call``. Without both, the live model
configuration is never read and no provider is contacted.
"""
from __future__ import annotations

import argparse
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
from agentic_debugger.evaluation.live import (  # noqa: E402
    LiveConfigurationError,
    LiveExecutionAuthorization,
    LiveModelConfig,
    LiveOptInError,
    LiveRunLimits,
    render_live_report,
    validate_live_report,
)
from agentic_debugger.demo.policies import DemoPolicy  # noqa: E402
from agentic_debugger.evaluation.live_quixbugs import (  # noqa: E402
    QuixBugsLiveConfigurationError,
    run_live_quixbugs_evaluation,
)
from agentic_debugger.quixbugs.adapter import QuixBugsAdapter, QuixBugsPreflightFacts  # noqa: E402
from agentic_debugger.runtime.execution import DependencyPreparation  # noqa: E402
from scripts.opencode_protocol_transport import verify_opencode_launcher  # noqa: E402

DISTRO = "Ubuntu-22.04"
EXTERNAL_ROOT_POSIX = "/home/benya/.local/share/agentic-debugging-internship/quixbugs-smoke-v1"
PYTEST_VERSION = "7.4.4"
PYTHON_VERSION = "3.10.12"
DEFAULT_MANIFEST = REPO_ROOT / "research" / "quixbugs" / "GCD_SMOKE_MANIFEST_V1.json"
DEFAULT_MODEL_NAME = "opencode/deepseek-v4-flash-free"


class ReadinessError(RuntimeError):
    """A prerequisite (venv, pinned source) is missing.

    Raised rather than silently installing/cloning it: this script is not
    authorized to install dependencies or acquire sources.
    """


def _phase(name: str) -> None:
    print(f"\n=== {name} ===", file=sys.stderr, flush=True)


def _verify_environment_ready() -> tuple[WslProcess, WslBubblewrapRunner, str, str, str]:
    """Read-only readiness check: fails closed if setup would otherwise be
    required. Never runs ``pip install``, ``python -m venv``, or ``git clone``."""

    process = WslProcess(DISTRO)
    venv_posix = f"{EXTERNAL_ROOT_POSIX}/python-env/py310"

    _phase("readiness: pinned source already acquired (read-only)")
    source_check = process.run(
        ["bash", "-c", f"test -d {EXTERNAL_ROOT_POSIX}/sources/quixbugs && echo present || echo absent"],
        timeout_seconds=30,
    )
    if "present" not in source_check.stdout:
        raise ReadinessError(
            "pinned QuixBugs source is not already acquired; this script is not authorized to clone it"
        )

    _phase("readiness: pinned venv already built (read-only)")
    check = process.run(["bash", "-c", f"{venv_posix}/bin/python -m pytest --version 2>&1 || true"], timeout_seconds=30)
    if f"pytest {PYTEST_VERSION}" not in check.stdout:
        raise ReadinessError(
            "pinned venv/pytest is not already installed at the expected version; "
            "this script is not authorized to install dependencies"
        )
    freeze = process.run(["bash", "-c", f"{venv_posix}/bin/python -m pip freeze"], timeout_seconds=30)
    package_list = [line.strip() for line in freeze.stdout.splitlines() if "==" in line]
    env_fingerprint = fingerprint_environment({"python_version": PYTHON_VERSION, "packages": ",".join(sorted(package_list))})

    _phase("build WslBubblewrapRunner (no install, no clone)")
    root_host = wsl_unc_path(EXTERNAL_ROOT_POSIX, DISTRO)
    runner = WslBubblewrapRunner(
        root_host=root_host, python_root_posix=venv_posix, python_executable_posix=f"{venv_posix}/bin/python", distro=DISTRO,
    )

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

    return process, runner, root_host, venv_posix, env_fingerprint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument(
        "--model-command-json", required=True,
        help="JSON array: argv of the local protocol-1.3 OpenCode Zen transport wrapper "
        "(a JSON string, not nargs, so the wrapper's own flags such as --connection-file "
        "are never mistaken for this script's own arguments)",
    )
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--request-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--tool-version", default="quixbugs-gcd-live-case-v1")
    parser.add_argument("--max-model-requests", type=int, default=24)
    parser.add_argument("--max-controller-steps", type=int, default=32)
    parser.add_argument("--max-model-phase-seconds", type=int, default=600)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--max-response-bytes", type=int, default=1048576)
    parser.add_argument("--output", default="quixbugs-gcd-live-results.json")
    parser.add_argument("--human-output")
    parser.add_argument("--run-label", default="quixbugs-gcd-protocol13-static-live-v1")
    parser.add_argument("--i-understand-this-contacts-a-real-model", action="store_true", dest="understands")
    parser.add_argument("--i-authorize-live-opencode-call", action="store_true", dest="authorizes")
    return parser


def _rejected(reason: str, output: Path, human_output: Path) -> int:
    from agentic_debugger.evaluation.live import rejected_live_report

    report = rejected_live_report(reason)
    payload = report.to_mapping()
    validate_live_report(payload)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    human_output.write_text(render_live_report(report), encoding="utf-8")
    print(f"live execution rejected: {reason}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output)
    human_output = Path(args.human_output) if args.human_output else output.with_suffix(".txt")

    if not args.understands or not args.authorizes:
        return _rejected(
            "live execution rejected: both --i-understand-this-contacts-a-real-model and "
            "--i-authorize-live-opencode-call are required",
            output, human_output,
        )

    try:
        model_command = json.loads(args.model_command_json)
    except json.JSONDecodeError as exc:
        return _rejected(f"--model-command-json is not valid JSON: {exc}", output, human_output)
    if not isinstance(model_command, list) or not all(isinstance(item, str) for item in model_command):
        return _rejected("--model-command-json must be a JSON array of strings", output, human_output)

    try:
        config = LiveModelConfig(
            model_name=args.model_name, command=tuple(model_command),
            request_timeout_seconds=args.request_timeout_seconds, tool_version=args.tool_version,
        )
        limits = LiveRunLimits(
            max_model_requests=args.max_model_requests, max_controller_steps=args.max_controller_steps,
            max_model_phase_seconds=args.max_model_phase_seconds, max_retries=args.max_retries,
            max_response_bytes=args.max_response_bytes,
        )
    except LiveConfigurationError as exc:
        return _rejected(str(exc), output, human_output)

    try:
        launcher_preflight = verify_opencode_launcher()
        preflight_path = output.with_name("opencode-launcher-preflight.json")
        preflight_path.parent.mkdir(parents=True, exist_ok=True)
        preflight_path.write_text(json.dumps(launcher_preflight, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, RuntimeError) as exc:
        return _rejected(f"blocked before any provider contact: {exc}", output, human_output)

    try:
        process, runner, root_host, venv_posix, env_fingerprint = _verify_environment_ready()
    except ReadinessError as exc:
        return _rejected(f"blocked before any provider contact: {exc}", output, human_output)

    adapter = QuixBugsAdapter.from_manifest(args.manifest)
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

    try:
        report = run_live_quixbugs_evaluation(
            repository_root=str(REPO_ROOT), authorization=LiveExecutionAuthorization.authorize(True, True),
            manifest_path=args.manifest, sources_parent=sources_host, facts=facts, config=config, limits=limits,
            repetitions=1, evaluation_id=args.run_label,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        )
    except (LiveConfigurationError, LiveOptInError, QuixBugsLiveConfigurationError) as exc:
        return _rejected(str(exc), output, human_output)

    validate_live_report(report)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    human_output.write_text(render_live_report(report), encoding="utf-8")

    case = report["cases"][0]
    print(json.dumps({"task_id": case["task_id"], "status": case["status"], "completion": report["completion"]}, indent=2))

    if report.get("evaluation_cleanup") == "failed" or case.get("status") == "CLEANUP_FAILED":
        return 1
    if report.get("completion") != "complete":
        return 3
    if case.get("status") != "RESOLVED":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
