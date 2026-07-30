"""Verified execution-context contracts for external benchmark commands."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


class ExecutionContextError(ValueError):
    """A verified execution context is incomplete or unsafe."""


class ContainmentRunner(Protocol):
    """Runner supplied by an external containment boundary."""

    runner_id: str
    boundary_guarantee: Mapping[str, Any]

    def run(
        self,
        argv: list[str],
        cwd: str,
        timeout_seconds: float,
        env: Mapping[str, str],
    ) -> Any:
        """Run one command under the declared boundary."""


@dataclass(frozen=True)
class PdbLaunchPlan:
    """Reviewed pytest-aware launch plan; this contract does not launch PDB."""

    python_executable: str
    driver: str
    target: str
    breakpoints: tuple[int, ...]
    cwd: str
    argv: tuple[str, ...]
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        if not Path(self.python_executable).is_absolute():
            raise ExecutionContextError("PDB launch plan needs an absolute Python executable")
        _validate_relative(self.driver, "PDB driver")
        _validate_relative(self.target, "PDB target")
        _validate_relative(self.cwd, "PDB cwd")
        if not self.breakpoints or not all(isinstance(item, int) and item > 0 for item in self.breakpoints):
            raise ExecutionContextError("PDB launch plan needs positive breakpoints")
        if not self.argv or not all(isinstance(item, str) and item for item in self.argv):
            raise ExecutionContextError("PDB launch plan argv is required")
        if not isinstance(self.environment, Mapping):
            raise ExecutionContextError("PDB launch plan environment is required")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "python_executable": self.python_executable,
            "driver": self.driver,
            "target": self.target,
            "breakpoints": list(self.breakpoints),
            "cwd": self.cwd,
            "argv": list(self.argv),
            "environment": dict(sorted(self.environment.items())),
        }


@dataclass(frozen=True)
class DependencyPreparation:
    pilot_task_id: str
    manifest_fingerprint: str
    authority_revision: str
    project: str
    bug_id: str
    buggy_revision: str
    recipe_path: str
    recipe_sha256: str
    installed_fingerprint: str
    status: str = "prepared"
    network_disabled: bool = True

    def __post_init__(self) -> None:
        for name in ("pilot_task_id", "manifest_fingerprint", "authority_revision", "project", "bug_id", "buggy_revision", "recipe_path", "recipe_sha256", "installed_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ExecutionContextError(f"{name} must be a non-empty string")
        if self.status != "prepared":
            raise ExecutionContextError("dependency preparation must be prepared")
        if type(self.network_disabled) is not bool or not self.network_disabled:
            raise ExecutionContextError("execution dependencies must be prepared before network denial")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "pilot_task_id": self.pilot_task_id,
            "manifest_fingerprint": self.manifest_fingerprint,
            "authority_revision": self.authority_revision,
            "project": self.project,
            "bug_id": self.bug_id,
            "buggy_revision": self.buggy_revision,
            "recipe_path": self.recipe_path,
            "recipe_sha256": self.recipe_sha256,
            "installed_fingerprint": self.installed_fingerprint,
            "status": self.status,
            "network_disabled": self.network_disabled,
        }


@dataclass(frozen=True)
class PreparedEnvironment:
    python_executable: str
    python_version: str
    project_cwd: str
    pythonpath: tuple[str, ...]
    environment: Mapping[str, str]
    dependencies: DependencyPreparation

    def __post_init__(self) -> None:
        executable = Path(self.python_executable)
        if not executable.is_absolute() or not self.python_executable:
            raise ExecutionContextError("verified python_executable must be absolute")
        if not isinstance(self.python_version, str) or not self.python_version:
            raise ExecutionContextError("verified python_version must be non-empty")
        _validate_relative(self.project_cwd, "project_cwd")
        if not isinstance(self.pythonpath, tuple) or not all(isinstance(item, str) and item for item in self.pythonpath):
            raise ExecutionContextError("pythonpath must be a tuple of relative entries")
        for item in self.pythonpath:
            _validate_relative(item, "pythonpath entry")
        if not isinstance(self.environment, Mapping):
            raise ExecutionContextError("environment must be a mapping")
        for key, value in self.environment.items():
            if not isinstance(key, str) or not key or not isinstance(value, str):
                raise ExecutionContextError("environment keys and values must be strings")
            if any(secret in key.upper() for secret in ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "CREDENTIAL")):
                raise ExecutionContextError("credential-like environment variables are prohibited")
        if not isinstance(self.dependencies, DependencyPreparation):
            raise ExecutionContextError("dependencies must be a prepared result")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "python_executable": self.python_executable,
            "python_version": self.python_version,
            "project_cwd": self.project_cwd,
            "pythonpath": list(self.pythonpath),
            "environment": dict(sorted(self.environment.items())),
            "dependencies": self.dependencies.to_mapping(),
        }


@dataclass(frozen=True)
class ContainmentGuarantee:
    root: str
    runner_id: str
    network_allowed: bool = False
    credentials_visible: bool = False
    process_tree_isolated: bool = True
    resource_limits: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not Path(self.root).is_absolute() or not self.runner_id:
            raise ExecutionContextError("containment root and runner_id are required")
        if self.network_allowed or self.credentials_visible or not self.process_tree_isolated:
            raise ExecutionContextError("external execution requires denied network/credentials and process isolation")
        if not isinstance(self.resource_limits, Mapping) or not self.resource_limits:
            raise ExecutionContextError("resource limits must be explicitly declared")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "runner_id": self.runner_id,
            "network_allowed": self.network_allowed,
            "credentials_visible": self.credentials_visible,
            "process_tree_isolated": self.process_tree_isolated,
            "resource_limits": dict(sorted(self.resource_limits.items())),
        }


@dataclass(frozen=True)
class VerifiedExecutionContext:
    environment: PreparedEnvironment
    containment: ContainmentGuarantee
    runner: ContainmentRunner

    def __post_init__(self) -> None:
        if not isinstance(self.environment, PreparedEnvironment):
            raise ExecutionContextError("execution context needs a PreparedEnvironment")
        if not isinstance(self.containment, ContainmentGuarantee):
            raise ExecutionContextError("execution context needs containment guarantees")
        if not hasattr(self.runner, "run"):
            raise ExecutionContextError("execution context needs a containment runner")
        runner_id = getattr(self.runner, "runner_id", None)
        if runner_id != self.containment.runner_id:
            raise ExecutionContextError("runner identity does not match containment guarantee")
        boundary = getattr(self.runner, "boundary_guarantee", None)
        expected = self.containment.to_mapping()
        if not isinstance(boundary, Mapping) or dict(boundary) != expected:
            raise ExecutionContextError("runner boundary guarantee does not match containment declaration")

    def bind_argv(self, argv: Sequence[str]) -> list[str]:
        values = list(argv)
        if not values or not all(isinstance(item, str) and item for item in values):
            raise ExecutionContextError("command argv must contain non-empty strings")
        basename = Path(values[0]).name.lower()
        if basename in {"pytest", "pytest.exe", "python", "python3", "python.exe", "python3.exe"}:
            if basename.startswith("python"):
                if len(values) < 3 or values[1] != "-m" or values[2] != "pytest":
                    raise ExecutionContextError("verified external commands must invoke pytest as a Python module")
                values = values[3:]
            else:
                values = values[1:]
            return [self.environment.python_executable, "-m", "pytest", *values]
        raise ExecutionContextError(f"external command is not an approved pytest command: {values[0]!r}")

    def bind_cwd(self, cwd: str) -> str:
        _validate_relative(cwd, "command cwd")
        if cwd.replace("\\", "/") != self.environment.project_cwd.replace("\\", "/"):
            raise ExecutionContextError(
                f"command cwd {cwd!r} does not match verified project cwd {self.environment.project_cwd!r}"
            )
        return cwd

    def build_environment(self, workspace_root: str) -> dict[str, str]:
        root = Path(workspace_root).resolve()
        values = dict(self.environment.environment)
        values["PYTHONIOENCODING"] = "utf-8"
        paths = [str(root / item) for item in self.environment.pythonpath]
        if paths:
            values["PYTHONPATH"] = os.pathsep.join(paths)
        return values

    def to_mapping(self) -> dict[str, Any]:
        return {
            "environment": self.environment.to_mapping(),
            "containment": self.containment.to_mapping(),
            "runner_id": self.containment.runner_id,
        }


def _validate_relative(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")):
        raise ExecutionContextError(f"{label} must be a non-empty relative path")
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        raise ExecutionContextError(f"{label} must be relative")
    if ".." in value.replace("\\", "/").split("/"):
        raise ExecutionContextError(f"{label} must not contain traversal")
