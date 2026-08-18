"""Frozen SWE-rebench execution seams.

The module contains only deterministic orchestration and verifier-boundary
adapters.  It does not call a model provider.  The model-facing controller
uses the same Local Application pipeline as curated tasks, while public pytest
commands are routed through a verified Docker dependency environment and the
candidate is judged by the official SWE-rebench evaluator.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome
from agentic_debugger.runtime.command_runner import CommandResult
from agentic_debugger.runtime.exceptions import CommandExecutionError
from agentic_debugger.runtime.execution import (
    ContainmentGuarantee,
    DependencyPreparation,
    PreparedEnvironment,
    VerifiedExecutionContext,
)
from agentic_debugger.swerebench.official_eval import (
    _run_official_eval,
    _report_item,
    _summarize_item,
    _write_isolated_spec,
)
from agentic_debugger.swerebench.records import (
    OfficialInstanceBundle,
    PublicInstanceRecord,
    VerifierPrivateRecord,
)


class SWERebenchExecutionError(RuntimeError):
    """External execution cannot be started honestly."""


_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def inspect_external_root_target(
    path: Path,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Describe whether an external execution root is safe to create.

    Authorization is deliberately non-mutating.  A nonexistent target is the
    only authorized target state; the executor then creates it exactly once.
    Existing empty and non-empty targets both fail closed to prevent stale
    campaign state from being reused.
    """

    project = (project_root or Path(__file__).resolve().parents[2]).resolve()
    root = Path(path).expanduser().resolve(strict=False)
    result: dict[str, Any] = {
        "requested": str(path),
        "resolved": str(root),
        "state": "nonexistent_target" if not root.exists() else "existing",
        "authorized": False,
        "parent_exists": root.parent.exists(),
        "parent_writable": False,
        "reason": None,
    }
    try:
        root.relative_to(project)
    except ValueError:
        pass
    else:
        result["reason"] = "external root is inside the project repository"
        return result
    if root == Path(root.anchor):
        result["reason"] = "external root must not be a filesystem root"
        return result
    if root.name.upper().rstrip(" .") in _WINDOWS_RESERVED_NAMES:
        result["reason"] = "external root uses a reserved special path name"
        return result
    if root.exists():
        if root.is_dir() and not root.is_symlink():
            result["state"] = "existing_empty" if not any(root.iterdir()) else "existing_nonempty"
        else:
            result["state"] = "existing_non_directory"
        result["reason"] = "external root already exists; a fresh target is required"
        return result

    probe = root.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir():
        result["reason"] = "external root parent cannot be resolved to a directory"
        return result
    result["parent_writable"] = os.access(probe, os.W_OK)
    if not result["parent_writable"]:
        result["reason"] = "external root parent is not writable"
        return result
    result["parent_exists"] = root.parent.exists()
    result["authorized"] = True
    return result


def create_external_execution_root(
    path: Path,
    *,
    project_root: Path | None = None,
) -> Path:
    """Create the authorized fresh execution root exactly once."""

    inspection = inspect_external_root_target(path, project_root=project_root)
    if not inspection["authorized"]:
        raise SWERebenchExecutionError(str(inspection["reason"] or "unsafe external root"))
    root = Path(inspection["resolved"])
    try:
        root.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise SWERebenchExecutionError(
            f"external execution root could not be created: {type(exc).__name__}"
        ) from exc
    return root


def load_private_bundle(path: Path) -> OfficialInstanceBundle:
    """Load a verifier-only bundle from an operator-owned external file."""

    data = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "public",
        "private",
        "gold_patch",
        "test_patch",
        "fail_to_pass",
        "pass_to_pass",
        "test_cmd",
        "install_config",
        "image_name",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise SWERebenchExecutionError("private bundle has an invalid exact schema")
    public_data = data["public"]
    private_data = data["private"]
    public = PublicInstanceRecord(**public_data)
    private = VerifierPrivateRecord(
        instance_id=private_data["instance_id"],
        fail_to_pass=tuple(private_data["fail_to_pass"]),
        pass_to_pass=tuple(private_data["pass_to_pass"]),
        test_cmd=private_data.get("test_cmd"),
        image_name=private_data.get("image_name"),
        python_version=private_data.get("python_version"),
        has_gold_patch=bool(private_data["has_gold_patch"]),
        has_test_patch=bool(private_data["has_test_patch"]),
        gold_patch_sha256=private_data.get("gold_patch_sha256"),
        test_patch_sha256=private_data.get("test_patch_sha256"),
    )
    if private.instance_id != public.instance_id:
        raise SWERebenchExecutionError("private bundle identity does not match public record")
    return OfficialInstanceBundle(
        public=public,
        private=private,
        _gold_patch=data["gold_patch"],
        _test_patch=data["test_patch"],
        _fail_to_pass=tuple(data["fail_to_pass"]),
        _pass_to_pass=tuple(data["pass_to_pass"]),
        _test_cmd=data["test_cmd"],
        _install_config=dict(data["install_config"]),
        _image_name=data["image_name"],
    )


def write_private_bundle(path: Path, bundle: OfficialInstanceBundle) -> None:
    """Write the verifier-only bundle outside the model workspace."""

    private = bundle.private
    payload = {
        "public": bundle.public.to_mapping(),
        "private": {
            "instance_id": private.instance_id,
            "fail_to_pass": list(private.fail_to_pass),
            "pass_to_pass": list(private.pass_to_pass),
            "test_cmd": private.test_cmd,
            "image_name": private.image_name,
            "python_version": private.python_version,
            "has_gold_patch": private.has_gold_patch,
            "has_test_patch": private.has_test_patch,
            "gold_patch_sha256": private.gold_patch_sha256,
            "test_patch_sha256": private.test_patch_sha256,
        },
        "gold_patch": bundle.gold_patch(),
        "test_patch": bundle.test_patch(),
        "fail_to_pass": list(bundle.hidden_tests()[0]),
        "pass_to_pass": list(bundle.hidden_tests()[1]),
        "test_cmd": bundle.test_cmd(),
        "install_config": bundle.install_config(),
        "image_name": bundle.image_name(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True),
        encoding="utf-8",
    )


class DockerPublicPytestRunner:
    """Run approved pytest argv in the pinned SWE-rebench image.

    The runner is intentionally narrow: ``VerifiedExecutionContext`` already
    rejects non-pytest commands.  The host workspace is mounted read/write as
    the model candidate workspace, network is denied, and the result is
    reduced to the common bounded ``CommandResult`` contract.
    """

    def __init__(
        self,
        image: str,
        *,
        root: Path,
        python_executable: str = "/usr/local/bin/python",
        runner_id: str = "swe-rebench-docker-network-none-v1",
    ) -> None:
        if not image or not root.is_absolute():
            raise SWERebenchExecutionError("Docker runner needs an image and absolute root")
        self.image = image
        self.root = root.resolve()
        self.runner_id = runner_id
        self.python_executable = python_executable
        self.boundary_guarantee = ContainmentGuarantee(
            str(self.root),
            runner_id,
            resource_limits={
                "timeout": "parent-and-docker",
                "memory": "official-image-default",
                "pids": "docker-default",
            },
        ).to_mapping()

    def run(
        self,
        argv: list[str],
        cwd: str,
        timeout_seconds: float,
        env: Mapping[str, str],
    ) -> CommandResult:
        mount_root = Path(cwd).resolve()
        try:
            mount_root.relative_to(self.root)
        except ValueError as exc:
            raise SWERebenchExecutionError(
                "public pytest cwd escaped the declared external root"
            ) from exc
        container_env = self._container_environment(env, mount_root)
        container_argv = [self.python_executable, *argv[1:]]
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--init",
            "--volume",
            f"{mount_root}:/workspace:rw",
            "--workdir",
            "/workspace",
        ]
        for name, value in sorted(container_env.items()):
            command.extend(["--env", f"{name}={value}"])
        command.extend([
            self.image,
            *container_argv,
        ])
        start = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(mount_root),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            if completed.returncode in {125, 126, 127}:
                raise CommandExecutionError(
                    "Docker engine/container launch failed "
                    f"(exit {completed.returncode})"
                )
            return CommandResult(
                argv=list(argv),
                cwd=cwd,
                exit_code=completed.returncode,
                timed_out=False,
                duration_ms=max(0, int((time.monotonic() - start) * 1000)),
                stdout=completed.stdout[-20000:],
                stderr=completed.stderr[-20000:],
                stdout_truncated=len(completed.stdout) > 20000,
                stderr_truncated=len(completed.stderr) > 20000,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                argv=list(argv),
                cwd=cwd,
                exit_code=None,
                timed_out=True,
                duration_ms=max(0, int((time.monotonic() - start) * 1000)),
                stdout=(exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "")[-20000:] if isinstance(exc.stderr, str) else "",
                stdout_truncated=False,
                stderr_truncated=False,
            )
        except OSError as exc:
            raise CommandExecutionError(
                f"Docker public pytest launch failed: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _container_environment(
        env: Mapping[str, str], mount_root: Path
    ) -> dict[str, str]:
        """Translate the verified host environment at the Docker boundary.

        ``VerifiedExecutionContext`` quite correctly expresses ``PYTHONPATH``
        using host paths.  Passing that mapping to the Docker CLI does not
        translate it into the container, so only container-side values are
        emitted here.  ``src`` is deliberately first, followed by the project
        root, which also makes the patched disposable tree win over any copy
        preinstalled in the official image.
        """
        values: dict[str, str] = {}
        for name, value in env.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise SWERebenchExecutionError("Docker environment must contain string pairs")
            if name == "PYTHONPATH" or name == "PATH":
                continue
            if "\x00" in name or "\x00" in value:
                raise SWERebenchExecutionError("Docker environment contains a NUL byte")
            values[name] = value
        pythonpath = ["/workspace/src"] if (mount_root / "src").is_dir() else []
        pythonpath.append("/workspace")
        values["PYTHONPATH"] = ":".join(pythonpath)
        values["PYTHONIOENCODING"] = "utf-8"
        return values


def build_docker_execution_context(
    *,
    bundle: OfficialInstanceBundle,
    external_root: Path,
    instance_id: str,
    manifest_fingerprint: str,
    authority_revision: str,
    project: str,
    bug_id: str,
    buggy_revision: str,
    image: str | None = None,
) -> VerifiedExecutionContext:
    """Create the verified model-side runtime descriptor for one task."""

    image_name = image or bundle.image_name()
    if not image_name:
        raise SWERebenchExecutionError("official SWE-rebench image is missing")
    root = external_root.resolve()
    if not root.is_dir():
        raise SWERebenchExecutionError(
            "verified Docker runtime requires an executor-created directory root"
        )
    recipe = f"docker:{image_name}:python-pytest"
    recipe_sha = hashlib.sha256(recipe.encode("utf-8")).hexdigest()
    dependencies = DependencyPreparation(
        pilot_task_id=instance_id,
        manifest_fingerprint=manifest_fingerprint,
        authority_revision=authority_revision,
        project=project,
        bug_id=bug_id,
        buggy_revision=buggy_revision,
        recipe_path=recipe,
        recipe_sha256=recipe_sha,
        installed_fingerprint=hashlib.sha256(image_name.encode("utf-8")).hexdigest(),
    )
    prepared = PreparedEnvironment(
        # PreparedEnvironment validates a host-platform absolute path.  The
        # Docker runner translates argv[0] to the container-side ``python``
        # command at the execution boundary above.
        python_executable=(
            "C:/swe-rebench/python.exe" if os.name == "nt" else "/opt/swe-rebench/python"
        ),
        python_version=bundle.private.python_version or "official-image",
        project_cwd=".",
        pythonpath=(".",),
        environment={"PYTHONUNBUFFERED": "1"},
        dependencies=dependencies,
    )
    runner = DockerPublicPytestRunner(image_name, root=root)
    containment = ContainmentGuarantee(
        str(root), runner.runner_id, resource_limits=dict(runner.boundary_guarantee["resource_limits"])
    )
    return VerifiedExecutionContext(prepared, containment, runner)


class OfficialSWERebenchVerifier:
    """Candidate verifier backed by the official evaluator, not host pytest."""

    def __init__(
        self,
        bundle: OfficialInstanceBundle,
        *,
        work_root: Path,
        baseline_valid: bool,
        evaluate_fn: Callable[[Path, Path, Path], dict[str, Any]] = _run_official_eval,
    ) -> None:
        if not baseline_valid:
            raise SWERebenchExecutionError("official baseline is not valid")
        self.bundle = bundle
        self.work_root = work_root.resolve()
        self.baseline_valid = baseline_valid
        self.evaluate_fn = evaluate_fn

    def evaluate(self, candidate_patch: str) -> dict[str, Any]:
        if not isinstance(candidate_patch, str):
            raise SWERebenchExecutionError("candidate patch must be text")
        private = self.work_root / f"candidate-verification-private-{uuid.uuid4().hex}"
        private.mkdir(parents=True, exist_ok=True)
        spec_path = private / "candidate.json"
        report_path = private / "candidate_report.json"
        # The private bundle is consulted only here, after the model has
        # produced a candidate.  The candidate spec never reaches the model
        # request/task mapping.
        result: dict[str, Any]
        try:
            try:
                spec = _write_isolated_spec(
                    spec_path,
                    self.bundle,
                    use_gold=False,
                    candidate_patch=candidate_patch,
                )
                raw = self.evaluate_fn(spec, report_path, private)
            except Exception as exc:
                result = {
                    "authority": "official-swerebench-docker-evaluator",
                    "verifier_ran": True,
                    "verifier_infrastructure_valid": False,
                    "baseline_valid": self.baseline_valid,
                    "fail_to_pass": {"passed": 0, "total": len(self.bundle.hidden_tests()[0])},
                    "pass_to_pass": {
                        "failed": 0,
                        "total": len(self.bundle.hidden_tests()[1]),
                        "empty": not bool(self.bundle.hidden_tests()[1]),
                    },
                    "full_suite": None,
                    "verifier_outcome": "UNRESOLVED",
                    "resolved": False,
                    "candidate_patch_sha256": hashlib.sha256(candidate_patch.encode("utf-8")).hexdigest(),
                    "cleanup": False,
                    "official_error": f"{type(exc).__name__}: {str(exc)[:400]}",
                    "official_process_exit_code": None,
                    "official_passed_match": False,
                }
            else:
                raw_mapping = raw if isinstance(raw, dict) else {}
                report = raw_mapping.get("report")
                item = _report_item(report, self.bundle.public.instance_id)
                summary = _summarize_item(
                    item,
                    empty_p2p=not bool(self.bundle.hidden_tests()[1]),
                    requested_instance_id=self.bundle.public.instance_id,
                    expected_f2p_count=len(self.bundle.hidden_tests()[0]),
                    expected_p2p_count=len(self.bundle.hidden_tests()[1]),
                )
                f2p_count = int(summary.get("f2p_now_passing_count") or 0)
                p2p_failed = int(summary.get("p2p_failed_count") or 0)
                f2p_total = len(self.bundle.hidden_tests()[0])
                p2p_total = len(self.bundle.hidden_tests()[1])
                resolved = bool(summary.get("passed_match"))
                report_valid = bool(summary.get("valid_result"))
                result = {
                "authority": "official-swerebench-docker-evaluator",
                "verifier_ran": True,
                # The official evaluator returns exit 1 for a scientifically
                # valid ordinary mismatch.  Report structure/identity, not
                # the process status alone, decides infrastructure validity.
                "verifier_infrastructure_valid": report_valid,
                "baseline_valid": self.baseline_valid,
                "fail_to_pass": {
                    "passed": f2p_count,
                    "total": f2p_total,
                },
                "pass_to_pass": {
                    "failed": p2p_failed,
                    "total": p2p_total,
                    "empty": p2p_total == 0,
                },
                "full_suite": None,
                "verifier_outcome": SemanticOutcome.RESOLVED.value if resolved else "UNRESOLVED",
                "resolved": resolved,
                "candidate_patch_sha256": hashlib.sha256(candidate_patch.encode("utf-8")).hexdigest(),
                "cleanup": True,
                "official_error": summary.get("error") or raw_mapping.get("report_error"),
                "official_process_exit_code": raw_mapping.get("exit_code"),
                    "official_passed_match": summary.get("official_passed_match"),
                }
        finally:
            # The evaluator may create logs and other private output besides
            # the two known files.  Remove and verify the entire uniquely
            # owned workspace, then make cleanup failure infrastructure-invalid.
            cleanup_error: str | None = None
            try:
                if private.exists():
                    shutil.rmtree(private)
                if private.exists():
                    cleanup_error = "verifier-private workspace remains"
            except OSError as exc:
                cleanup_error = f"verifier-private cleanup failed: {type(exc).__name__}"
            result["cleanup"] = cleanup_error is None and not private.exists()
            if cleanup_error is not None:
                result["verifier_infrastructure_valid"] = False
                result["resolved"] = False
                result["verifier_outcome"] = "UNRESOLVED"
                prior = result.get("official_error")
                result["official_error"] = (
                    f"{prior}; {cleanup_error}" if prior else cleanup_error
                )
        return result


__all__ = [
    "create_external_execution_root",
    "DockerPublicPytestRunner",
    "inspect_external_root_target",
    "OfficialSWERebenchVerifier",
    "SWERebenchExecutionError",
    "build_docker_execution_context",
    "load_private_bundle",
    "write_private_bundle",
]
