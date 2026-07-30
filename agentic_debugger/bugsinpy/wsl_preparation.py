"""Offline-verifiable contracts for the WSL BugsInPy preparation gate."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping

from agentic_debugger.runtime.execution import DependencyPreparation

from .wsl import MINIFORGE_SHA256_URL, MINIFORGE_URL, PYTHON_VERSION, WslGateError


class ExactPythonVersionError(WslGateError):
    """The approved environment did not provide the exact task interpreter."""


def require_exact_python_version(observed: str, expected: str = PYTHON_VERSION) -> str:
    if observed != expected:
        raise ExactPythonVersionError(f"exact Python gate failed: expected {expected}, observed {observed}")
    return observed


def verify_published_sha256(actual_sha256: str, published_sha256: str) -> str:
    if not isinstance(actual_sha256, str) or actual_sha256.lower() != published_sha256.strip().split()[0].lower():
        raise WslGateError("published Miniforge SHA-256 does not match downloaded artifact")
    if len(actual_sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in actual_sha256):
        raise WslGateError("artifact SHA-256 is malformed")
    return actual_sha256.lower()


def recipe_sha256(recipe_path: str | Path) -> str:
    path = Path(recipe_path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_exact_environment_argv(conda_executable: str, prefix: str) -> list[str]:
    return [conda_executable, "create", "--yes", "--prefix", prefix, "python=3.6.9", "--override-channels", "--channel", "conda-forge"]


def build_official_recipe_argv(python_executable: str, recipe_path: str) -> list[str]:
    return [python_executable, "-m", "pip", "install", "--no-input", "--no-cache-dir", "--requirement", recipe_path]


def bind_dependency_preparation(*, task_id: str, manifest_fingerprint: str, framework_revision: str, project: str, bug_id: str, buggy_revision: str, recipe_path: str, recipe_sha256_value: str, installed_fingerprint: str) -> DependencyPreparation:
    return DependencyPreparation(task_id, manifest_fingerprint, framework_revision, project, bug_id, buggy_revision, recipe_path, recipe_sha256_value, installed_fingerprint)


def miniforge_provenance() -> Mapping[str, str]:
    return {"url": MINIFORGE_URL, "checksum_url": MINIFORGE_SHA256_URL, "python_version": PYTHON_VERSION}
