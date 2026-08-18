"""Materialize SWE-rebench sources at the frozen base commit outside the repo."""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
from pathlib import Path

from agentic_debugger.swerebench.authority import (
    B14_REPO_MATERIALIZATION_NAME,
    DEFAULT_B14_REPO_CACHE,
    DEFAULT_EXTERNAL_ROOT,
    b14_v3_dir,
)


class MaterializationError(RuntimeError):
    """Source checkout could not be created honestly."""


def load_repo_cache_index(path: Path | None = None) -> dict[str, Path]:
    csv_path = path or (b14_v3_dir() / B14_REPO_MATERIALIZATION_NAME)
    index: dict[str, Path] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            canonical = row["repo_canonical"].strip().lower()
            cache = Path(row["cache_path"])
            if row.get("clone_or_cache_success", "").lower() == "true":
                index[canonical] = cache
    return index


def _run_git(args: list[str], cwd: Path) -> str:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    completed = subprocess.run(
        ["git", "-c", "credential.helper=", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise MaterializationError(f"git {' '.join(args)} failed: {detail[:400]}")
    return completed.stdout.strip()


def _verify_clean_commit(destination: Path, instance_id: str, base_commit: str) -> None:
    head = _run_git(["rev-parse", "HEAD"], destination)
    if head != base_commit:
        raise MaterializationError(
            f"{instance_id} HEAD {head} != required base_commit {base_commit}"
        )
    dirty = _run_git(["status", "--porcelain"], destination)
    if dirty:
        raise MaterializationError(f"{instance_id} checkout is dirty")


def materialize_base_commit(
    *,
    instance_id: str,
    repo: str,
    repo_canonical: str,
    base_commit: str,
    dest_parent: Path,
    cache_index: dict[str, Path] | None = None,
    repo_cache_root: Path = DEFAULT_B14_REPO_CACHE,
) -> Path:
    """Create a disposable checkout at exactly ``base_commit``.

    The B14 object cache is not a reliable full worktree source. Full trees
    are cloned from the public GitHub repository, then detached at the
    frozen base commit. Gold patches and test patches are not applied.
    """

    dest_parent = dest_parent.resolve()
    if not dest_parent.is_dir():
        raise MaterializationError(f"destination parent does not exist: {dest_parent}")
    destination = dest_parent / instance_id
    if destination.exists():
        raise MaterializationError(f"materialization destination already exists: {destination}")

    url = f"https://github.com/{repo}.git"
    errors: list[str] = []
    try:
        destination.mkdir(parents=False, exist_ok=False)
        _run_git(["init", "--quiet"], destination)
        _run_git(["config", "core.longpaths", "true"], destination)
        _run_git(["remote", "add", "origin", url], destination)
        _run_git(
            ["fetch", "--depth", "1", "--no-tags", "origin", base_commit],
            destination,
        )
        _run_git(["checkout", "--detach", "FETCH_HEAD"], destination)
        _verify_clean_commit(destination, instance_id, base_commit)
        return destination
    except Exception as exc:
        errors.append(str(exc))
        shutil.rmtree(destination, ignore_errors=True)

    index = cache_index if cache_index is not None else load_repo_cache_index()
    cache = index.get(repo_canonical.lower())
    if cache is None or not cache.exists():
        raise MaterializationError(
            f"{instance_id} GitHub clone failed and no B14 cache exists: "
            + "; ".join(errors)
        )
    try:
        _run_git(
            [
                "clone",
                "--no-tags",
                "--no-recurse-submodules",
                str(cache),
                str(destination),
            ],
            dest_parent,
        )
        _run_git(["-c", "advice.detachedHead=false", "checkout", "--force", "--detach", base_commit], destination)
        _run_git(["clean", "-fdx"], destination)
        _verify_clean_commit(destination, instance_id, base_commit)
        return destination
    except Exception as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise MaterializationError(
            f"{instance_id} materialization failed via GitHub and B14 cache: "
            f"{errors[0] if errors else ''} / {exc}"
        ) from exc


def default_external_root() -> Path:
    """Return the configured target; the executor owns its creation."""

    return DEFAULT_EXTERNAL_ROOT
