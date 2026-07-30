from __future__ import annotations

from pathlib import Path

import pytest

from agentic_debugger.bugsinpy.wsl import WslGateError
from agentic_debugger.bugsinpy.wsl_preparation import (
    ExactPythonVersionError, build_exact_environment_argv, build_official_recipe_argv,
    recipe_sha256, require_exact_python_version, verify_published_sha256,
)


def test_exact_python_version_rejects_substitution() -> None:
    assert require_exact_python_version("3.6.9") == "3.6.9"
    with pytest.raises(ExactPythonVersionError):
        require_exact_python_version("3.11.9")


def test_checksum_and_recipe_fingerprints_are_deterministic(tmp_path: Path) -> None:
    path = tmp_path / "recipe.txt"
    path.write_text("pytest==5.4.3\n", encoding="utf-8")
    assert len(recipe_sha256(path)) == 64
    assert verify_published_sha256("a" * 64, "A" * 64 + "  artifact") == "a" * 64
    with pytest.raises(WslGateError):
        verify_published_sha256("b" * 64, "a" * 64)


def test_preparation_commands_are_explicit_prefix_and_recipe_argv() -> None:
    assert build_exact_environment_argv("/opt/conda/bin/conda", "/owned/env")[5] == "python=3.6.9"
    assert build_official_recipe_argv("/owned/env/bin/python", "/owned/recipe.txt")[-2:] == ["--requirement", "/owned/recipe.txt"]
