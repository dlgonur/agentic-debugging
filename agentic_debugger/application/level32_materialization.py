"""Level-32 Cookiecutter #967 workspace materialization and scaffolding.

Provides truthful, reusable preparation of the pinned Level-32 execution
environment: exports the pinned base production source from Docker (with local
caching), loads the authoritative SWE-rebench parquet row, writes the public
test scaffold and task manifest, and builds the canonical exact-PDB probe scenario.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from agentic_debugger.demo.catalog import (
    DemoScenario,
    LocalizationClaim,
    ReferenceRepair,
    RuntimeProbe,
)

LEVEL32_INSTANCE_ID = "audreyr__cookiecutter-967"
LEVEL32_TASK_ID = "swr-audreyr-cookiecutter-967-pdb"
LEVEL32_BASE_COMMIT = "ba5ba8c78e97f5dc7fb4e16c588d7be037e6e5e7"
LEVEL32_IMAGE = "docker.io/swerebenchv2/audreyr-cookiecutter:967-ba5ba8c"
LEVEL32_IMAGE_ID = "sha256:0bad37ac1e0a6d692a9ef417c05753b5ad45dfa8c32fd52b0f3ecabf722af8eb"
LEVEL32_SOURCE_SHA256 = "71de7ea915fee31e4e9104b89259deaa1c83ae0c8d3cbe249c878f5adbd5f6ee"
LEVEL32_DATASET_REVISION = "475dd5e8703bb5fb22dd3c60b5d038b019eba1e0"
LEVEL32_PARQUET_SHA256 = "0e0bf9355f892ad74ae98d4e1c404f39fd6654a8e351ee3e6ab162e4a64cd3ad"
LEVEL32_EVALUATOR_COMMIT = "c71902a8cf8d2b725f63d51f199f4d3e56f68d2d"
LEVEL32_F2P_COUNT = 5
LEVEL32_P2P_COUNT = 9
LEVEL32_PUBLIC_F2P = "tests/test_pdb_public_config_merge.py::test_builtin_abbreviations_survive_custom_config"
LEVEL32_PUBLIC_P2P = "tests/test_pdb_public_config_merge.py::test_scalar_override_preserves_other_defaults"


class Level32MaterializationError(RuntimeError):
    """Raised when Level-32 workspace materialization fails."""


ProofError = Level32MaterializationError


def default_cache_dir() -> Path:
    app_data = os.environ.get("LOCALAPPDATA")
    if app_data:
        return Path(app_data) / "agentic-debugging"
    return Path.home() / ".cache" / "agentic-debugging"


def default_parquet_path() -> Path:
    return (
        default_cache_dir()
        / "swe_rebench_v2_census_cache"
        / "datasets--nebius--SWE-rebench-V2/snapshots"
        / LEVEL32_DATASET_REVISION
        / "data/train-00000-of-00001.parquet"
    )


def default_base_source_cache_dir() -> Path:
    return default_cache_dir() / "cookiecutter-967-base-source"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_readonly_tree(path: Path) -> None:
    """Remove a directory whose files/directories may be read-only."""

    def make_writable(function: Any, target: str, _error: Any) -> None:
        try:
            os.chmod(target, stat.S_IWRITE)
        except OSError:
            pass
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:
        shutil.rmtree(path, onerror=make_writable)


def _run_cmd(
    argv: list[str], *, cwd: Path | None = None, timeout: float = 60.0
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Level32MaterializationError(
            f"command failed before completion: {argv[0]} ({type(exc).__name__})"
        ) from exc


def load_official_row(*, parquet_path: Path | None = None) -> dict[str, Any]:
    path = parquet_path or default_parquet_path()
    if not path.is_file() or sha256_file(path) != LEVEL32_PARQUET_SHA256:
        raise Level32MaterializationError(
            "pinned SWE-rebench parquet is missing or has the wrong SHA-256"
        )
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise Level32MaterializationError(
            "pyarrow is required for the pinned task row"
        ) from exc
    table = pq.read_table(path, filters=[("instance_id", "=", LEVEL32_INSTANCE_ID)])
    rows = table.to_pylist()
    if len(rows) != 1:
        raise Level32MaterializationError("pinned SWE-rebench task row is not unique")
    row = dict(rows[0])
    checks = {
        "base_commit": LEVEL32_BASE_COMMIT,
        "image_name": LEVEL32_IMAGE,
        "instance_id": LEVEL32_INSTANCE_ID,
        "repo": "audreyr/cookiecutter",
    }
    if any(row.get(key) != value for key, value in checks.items()):
        raise Level32MaterializationError(
            "pinned SWE-rebench task identity does not match the contract"
        )
    if (
        len(row.get("FAIL_TO_PASS") or ()) != LEVEL32_F2P_COUNT
        or len(row.get("PASS_TO_PASS") or ()) != LEVEL32_P2P_COUNT
    ):
        raise Level32MaterializationError(
            "official hidden-test counts do not match the frozen contract"
        )
    if not row.get("patch") or not row.get("test_patch"):
        raise Level32MaterializationError("official verifier row is incomplete")
    return row


def copy_image_source(
    fixture: Path,
    *,
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> None:
    fixture = Path(fixture).resolve()
    fixture.parent.mkdir(parents=True, exist_ok=True)
    target_cache = (
        cache_dir if cache_dir is not None else default_base_source_cache_dir()
    )

    if use_cache and target_cache is not None:
        cached_config = target_cache / "cookiecutter/config.py"
        if (
            cached_config.is_file()
            and sha256_file(cached_config) == LEVEL32_SOURCE_SHA256
        ):
            if fixture.exists():
                remove_readonly_tree(fixture)
            shutil.copytree(target_cache, fixture, dirs_exist_ok=True)
            source = fixture / "cookiecutter/config.py"
            if source.is_file() and sha256_file(source) == LEVEL32_SOURCE_SHA256:
                return

    created = _run_cmd(
        [
            "docker",
            "create",
            "--network",
            "none",
            "--entrypoint",
            "/bin/true",
            LEVEL32_IMAGE,
        ],
        timeout=30,
    )
    if created.returncode != 0 or not created.stdout.strip():
        raise Level32MaterializationError(
            "could not create the source-export container"
        )
    container_id = created.stdout.strip()
    try:
        copied = _run_cmd(
            ["docker", "cp", f"{container_id}:/cookiecutter/.", str(fixture)],
            timeout=120,
        )
        if copied.returncode != 0:
            raise Level32MaterializationError(
                "could not export the pinned source from Docker"
            )
    finally:
        _run_cmd(["docker", "rm", "-f", container_id], timeout=30)
    git_dir = fixture / ".git"
    if git_dir.exists():
        remove_readonly_tree(git_dir)
    source = fixture / "cookiecutter/config.py"
    if not source.is_file() or sha256_file(source) != LEVEL32_SOURCE_SHA256:
        raise Level32MaterializationError(
            "exported production source does not match the pinned base blob"
        )

    if use_cache and target_cache is not None:
        try:
            target_cache.parent.mkdir(parents=True, exist_ok=True)
            if target_cache.exists():
                remove_readonly_tree(target_cache)
            shutil.copytree(fixture, target_cache, dirs_exist_ok=True)
        except Exception:
            pass


def write_public_scaffold(fixture: Path, problem_statement: str) -> None:
    fixture = Path(fixture).resolve()
    fixture.mkdir(parents=True, exist_ok=True)
    (fixture / "tests").mkdir(parents=True, exist_ok=True)
    (fixture / "poyo.py").write_text(
        "import yaml\n\n"
        "class PoyoException(Exception):\n    pass\n\n"
        "class exceptions:\n    PoyoException = PoyoException\n\n"
        "def parse_string(text):\n"
        "    try:\n        return yaml.safe_load(text) or {}\n"
        "    except yaml.YAMLError as exc:\n        raise PoyoException(str(exc))\n",
        encoding="utf-8",
        newline="\n",
    )
    test_path = fixture / "tests/test_pdb_public_config_merge.py"
    test_path.write_text(
        "from cookiecutter import config\n\n\n"
        "def test_builtin_abbreviations_survive_custom_config(tmp_path):\n"
        "    path = tmp_path / 'cookiecutter.yaml'\n"
        "    path.write_text(\"abbreviations:\\n  local: https://example.invalid/{0}.git\\n\", encoding='utf-8')\n"
        "    loaded = config.get_config(str(path))\n"
        "    assert loaded['abbreviations']['local'] == 'https://example.invalid/{0}.git'\n"
        "    assert loaded['abbreviations']['gh'] == 'https://github.com/{0}.git'\n"
        "    defaults = {\n"
        "        'abbreviations': {'gh': 'https://github.com/{0}.git'},\n"
        "        'default_context': {'project': 'cookiecutter'},\n"
        "    }\n"
        "    overrides = {\n"
        "        'abbreviations': {'local': 'https://example.invalid/{0}.git'},\n"
        "        'default_context': {'owner': 'onur'},\n"
        "    }\n"
        "    merged = config.merge_configs(defaults, overrides)\n"
        "    assert merged['abbreviations']['gh'] == 'https://github.com/{0}.git'\n"
        "    assert merged['abbreviations']['local'] == 'https://example.invalid/{0}.git'\n"
        "    assert merged['default_context']['project'] == 'cookiecutter'\n"
        "    assert merged['default_context']['owner'] == 'onur'\n\n\n"
        "def test_scalar_override_preserves_other_defaults(tmp_path):\n"
        "    path = tmp_path / 'cookiecutter.yaml'\n"
        "    path.write_text(\"replay_dir: ./replays\\n\", encoding='utf-8')\n"
        "    loaded = config.get_config(str(path))\n"
        "    assert loaded['replay_dir'].endswith('replays')\n"
        "    assert loaded['cookiecutters_dir']\n",
        encoding="utf-8",
        newline="\n",
    )
    task = {
        "schema_version": "1.0",
        "task_id": LEVEL32_TASK_ID,
        "title": "Recursively preserve nested Cookiecutter configuration",
        "description": problem_statement
        + "\n\nPublic contract: configuration overlays must recursively preserve unrelated keys in nested mappings while allowing user values to override defaults. The reusable merge behavior is part of the public module contract.",
        "language": "python",
        "fixture_path": f"agentic_debugger/datasets/curated/{LEVEL32_TASK_ID}",
        "reproduction": {
            "argv": [
                "python",
                "-m",
                "pytest",
                LEVEL32_PUBLIC_F2P,
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
            ],
            "cwd": ".",
            "timeout_seconds": 20,
            "expected_exit_code": 1,
        },
        "tests": {
            "fail_to_pass": [LEVEL32_PUBLIC_F2P],
            "pass_to_pass": [LEVEL32_PUBLIC_P2P],
            "full_suite_argv": [
                "python",
                "-m",
                "pytest",
                "tests/test_pdb_public_config_merge.py",
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
            ],
            "timeout_seconds": 30,
        },
        "constraints": {
            "allowed_write_paths": ["cookiecutter/config.py"],
            "denied_write_paths": ["tests", "task.json"],
            "network_allowed": False,
            "external_services_allowed": False,
            "max_patch_attempts": 2,
            "max_test_runs": 5,
            "max_pdb_observations": 6,
        },
        "oracle": {
            "bug_category": "nested configuration merge",
            "target_files": ["cookiecutter/config.py"],
            "target_symbols": ["get_config", "merge_configs"],
            "root_cause_summary": "Nested user configuration requires recursive merge behavior rather than a shallow update.",
            "runtime_evidence_hint": "The get_config frame exposes the parsed override and the resulting nested mapping.",
        },
        "tags": [
            "swe-rebench-v2",
            "pdb-required",
            "capability-ladder-32",
            "oracle-localized",
        ],
    }
    (fixture / "task.json").write_text(
        json.dumps(task, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def build_level32_scenario(
    task_id: str = LEVEL32_TASK_ID,
) -> DemoScenario:
    return DemoScenario(
        task_id=task_id,
        hypothesis_id="cookiecutter-967-runtime-hypothesis",
        root_cause_statement="The reproduced nested configuration behavior requires runtime inspection.",
        localization=LocalizationClaim("cookiecutter/config.py", "get_config"),
        reference_repair=ReferenceRepair(
            "cookiecutter/config.py",
            "config_dict = copy.copy(DEFAULT_CONFIG)",
            "config_dict = copy.deepcopy(DEFAULT_CONFIG)",
        ),
        runtime_probe=RuntimeProbe(
            module_path="cookiecutter/config.py",
            focus_function="get_config",
            call_source="get_config('unused-public-driver-path')",
            anchor="config_dict.update(yaml_dict)",
            inspect_expressions=("yaml_dict", "config_dict"),
            exact_public_reproduction=True,
            breakpoint_line=54,
        ),
    )


def materialize_level32_task(staging_root: Path) -> Path:
    """Materialize the complete public Level-32 workspace under staging_root.

    Returns the fixture directory path containing the prepared production source,
    the public poyo compatibility shim, public test suite, and task.json.
    """
    staging_root = Path(staging_root).resolve()
    staging_root.mkdir(parents=True, exist_ok=True)
    fixture = (
        staging_root / "agentic_debugger" / "datasets" / "curated" / LEVEL32_TASK_ID
    )
    copy_image_source(fixture)
    row = load_official_row()
    write_public_scaffold(fixture, str(row["problem_statement"]))
    return fixture


__all__ = [
    "LEVEL32_BASE_COMMIT",
    "LEVEL32_DATASET_REVISION",
    "LEVEL32_EVALUATOR_COMMIT",
    "LEVEL32_F2P_COUNT",
    "LEVEL32_IMAGE",
    "LEVEL32_IMAGE_ID",
    "LEVEL32_INSTANCE_ID",
    "LEVEL32_P2P_COUNT",
    "LEVEL32_PARQUET_SHA256",
    "LEVEL32_PUBLIC_F2P",
    "LEVEL32_PUBLIC_P2P",
    "LEVEL32_SOURCE_SHA256",
    "LEVEL32_TASK_ID",
    "Level32MaterializationError",
    "ProofError",
    "build_level32_scenario",
    "copy_image_source",
    "default_base_source_cache_dir",
    "default_cache_dir",
    "default_parquet_path",
    "load_official_row",
    "materialize_level32_task",
    "remove_readonly_tree",
    "sha256_file",
    "write_public_scaffold",
]
