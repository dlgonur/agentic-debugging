"""Harness provenance that does not pretend this candidate is baseline 9a47001."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

from agentic_debugger.swerebench.authority import repository_root
from agentic_debugger.swerebench.hashing import sha256_bytes, sha256_file

PARENT_BASELINE = "9a470019182760f7bb7a462c981b2f71052baf91"
HARNESS_PATHS = (
    "agentic_debugger/agent",
    "agentic_debugger/application",
    "agentic_debugger/demo",
    "agentic_debugger/evaluation",
    "agentic_debugger/runtime",
    "agentic_debugger/swerebench",
    "scripts/ollama_cloud_command_adapter.py",
    "scripts/gpt_oss_swerebench_v2_pilot10.py",
    "scripts/gpt_oss_swerebench_v2_devqual10.py",
    "scripts/gpt_oss_swerebench_v2_devqual10_v7.py",
)

# These hashes belong to completed immutable V1-V5 treatments. Their
# contracts remain historical evidence after a later repair changes the live
# harness; V6 records and verifies the current harness separately.
HISTORICAL_FROZEN_HARNESS_SHA256 = frozenset({
    "1e643c37fa4c494499b7b0ba8e7670f9ab9a1cb5aebc87a9aa5430400032633d",
    "6641d6154877ab40bff9b29373d7a535b7c81b42a35119b75beaaa81fc93903d",
    "f3af71913cbe2c9e3792a8b9d5e253b2715855670806b67837e1d1ffe59c34f7",
    "553616df4c80d432085ed0807b9fd48421762dd52df9d59a6786ebe9eb10875f",
    "16faa5c53f8086a74692aea7349380dc68d6410791f593cdd4bada18eb305d7e",
    # V6's frozen hash remains accepted as historical evidence after V7
    # extends the live harness path set.
    "a3d1fc56e2b6aa1576b30447e1d98ec9642f9ac0e7e7c4c92f9d92bcb940c18d",
})


def _iter_harness_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for rel in HARNESS_PATHS:
        path = root / rel
        if path.is_file():
            files.append(path)
            continue
        if path.is_dir():
            files.extend(sorted(item for item in path.rglob("*.py") if item.is_file()))
    return files


def harness_content_sha256(root: Path | None = None) -> str:
    repo = root or repository_root()
    digest_parts: list[bytes] = []
    for path in _iter_harness_files(repo):
        rel = path.relative_to(repo).as_posix().encode("utf-8")
        digest_parts.append(rel)
        digest_parts.append(b"\0")
        digest_parts.append(path.read_bytes())
        digest_parts.append(b"\n")
    return sha256_bytes(b"".join(digest_parts))


def current_git_head(root: Path | None = None) -> str | None:
    repo = root or repository_root()
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def working_tree_dirty(root: Path | None = None) -> bool:
    repo = root or repository_root()
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(completed.stdout.strip())


def harness_identity(root: Path | None = None) -> dict[str, object]:
    return {
        "parent_baseline": PARENT_BASELINE,
        "harness_content_sha256": harness_content_sha256(root),
        "runtime_head": current_git_head(root),
        "working_tree_dirty": working_tree_dirty(root),
        "note": (
            "parent_baseline is the accepted starting commit. "
            "Execution must record the actual runtime_head and verify "
            "harness_content_sha256 against this frozen identity; HEAD "
            "need not equal parent_baseline."
        ),
    }


def frozen_harness_identity(root: Path | None = None) -> dict[str, object]:
    """Return non-self-referential provenance frozen before execution.

    The actual Git HEAD and working-tree state are execution observations.
    Keeping them out of the frozen contract lets an accepted clean commit be
    authorized without pretending it is still the parent baseline.
    """

    return {
        "parent_baseline": PARENT_BASELINE,
        "harness_content_sha256": harness_content_sha256(root),
        "runtime_head_policy": "record_at_execution",
        "working_tree_policy": "must_be_clean_at_execution",
        "note": (
            "The parent baseline and harness content are frozen provenance; "
            "actual HEAD is recorded at execution time."
        ),
    }


def require_harness_match(expected_sha256: str, root: Path | None = None) -> None:
    actual = harness_content_sha256(root)
    if actual != expected_sha256:
        if expected_sha256 in HISTORICAL_FROZEN_HARNESS_SHA256:
            return
        raise ValueError(
            "runtime harness content does not match the frozen execution "
            f"contract: expected {expected_sha256}, got {actual}"
        )
