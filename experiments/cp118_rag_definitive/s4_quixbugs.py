"""S4 — QuixBugs frozen-revision acquisition and scoped corpus materialization.

The S4 treatment measures *repository RAG*: the retrieval corpus is the
actual QuixBugs repository at the frozen revision ``4257f44b`` (the same
revision pinned by the frozen quix40 protocol and manifest), indexed with
the frozen ``agentic_debugger/rag`` repo-mode rules.

Anti-oracle scoping
-------------------

The frozen QuixBugs revision ships answer-bearing content alongside the
buggy programs:

* ``correct_python_programs/`` — the gold/fixed implementations (used by the
  frozen protocol's ``pytest --correct`` oracle sanity);
* ``correct_java_programs/``, ``JavaDeserialization.*``, ``tester.py``,
  ``conftest.py``, ``json_testcases/``, docs, gradle files, PDFs.

The S4 retrieval corpus is therefore a **scoped view** of the frozen
revision containing exactly two repository directories:

* ``python_programs/`` — the buggy repository source (50 programs);
* ``python_testcases/`` — the test suite (42 test files).

Everything else (including all gold/fixed code) is structurally absent.
The scoped view is byte-identical copies of the frozen-revision files
(per-file SHA-256 verified), so chunk paths remain the actual repository
relative paths.  The ``tests/``-prefix classification of the frozen
``build_corpus(mode="repo")`` is used unchanged: QuixBugs tests live under
``python_testcases/``, which does not start with ``tests/``, so those files
are recorded with kind ``source`` under the frozen literal rule — this is
honest provenance, documented here and in the contract, and retrieval does
not filter by kind.

The evaluation-time worktree (full checkout, used only for ``git apply``
and the ``pytest --correct`` oracle sanity of the frozen evaluator) is a
separate artifact and never enters the retrieval corpus.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

QUIXBUGS_URL = "https://github.com/jkoppel/QuixBugs.git"
QUIXBUGS_REVISION = "4257f44b0ff1181dedaedee6a447e133219fcebf"

#: The only repository directories that may enter the S4 retrieval corpus.
SCOPED_CORPUS_DIRS = ("python_programs", "python_testcases")

#: Known answer-bearing / non-source top-level entries at the frozen
#: revision that must never appear inside the scoped corpus root.
FORBIDDEN_SCOPED_ENTRIES = (
    "correct_python_programs",
    "correct_java_programs",
    "java_programs",
    "java_testcases",
    "json_testcases",
    "conftest.py",
    "tester.py",
    "quixbugs.pdf",
    "JavaDeserialization.java",
    "JavaDeserialization.class",
    "build.gradle",
    "README.md",
    "LICENSE",
    ".git",
)


class QuixBugsError(RuntimeError):
    """Raised when the frozen QuixBugs source cannot be obtained/verified."""


def run_cmd(cmd: List[str], *, timeout: int = 1200, check: bool = False,
            cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise QuixBugsError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
        )
    return proc


def ensure_quixbugs_repo(root: Path, *, logger=None) -> Path:
    """Clone (or reuse) the QuixBugs repository at the frozen revision.

    Mirrors the frozen protocol's acquisition path
    (``git clone https://github.com/jkoppel/QuixBugs.git`` then
    ``git checkout --detach <rev>``).  Fails closed unless HEAD equals the
    frozen revision.  ``core.autocrlf`` is forced to ``false`` and the
    working tree is reset to the frozen revision so the on-disk files are
    byte-identical to the canonical (LF) repository content — the form the
    frozen protocol consumed on Colab.
    """

    root = Path(root)
    repo = root / "QuixBugs"
    if repo.is_dir():
        proc = run_cmd(["git", "rev-parse", "HEAD"], cwd=str(repo))
        if proc.returncode != 0 or proc.stdout.strip() != QUIXBUGS_REVISION:
            raise QuixBugsError(
                f"existing QuixBugs checkout at {repo} is not at the frozen "
                f"revision {QUIXBUGS_REVISION}; refusing to reuse it"
            )
        _force_canonical_working_tree(repo)
        if logger:
            logger.log(f"REUSE QuixBugs checkout at frozen revision {QUIXBUGS_REVISION}")
        return repo
    root.mkdir(parents=True, exist_ok=True)
    if logger:
        logger.log(f"CLONE QuixBugs at frozen revision {QUIXBUGS_REVISION}")
    run_cmd(["git", "clone", "-q", QUIXBUGS_URL, str(repo)], timeout=1200, check=True)
    run_cmd(["git", "checkout", "--detach", "-q", QUIXBUGS_REVISION], cwd=str(repo),
            check=True)
    _force_canonical_working_tree(repo)
    head = run_cmd(["git", "rev-parse", "HEAD"], cwd=str(repo), check=True)
    if head.stdout.strip() != QUIXBUGS_REVISION:
        raise QuixBugsError(
            f"QuixBugs checkout HEAD {head.stdout.strip()!r} != frozen revision "
            f"{QUIXBUGS_REVISION}"
        )
    return repo


def _force_canonical_working_tree(repo: Path) -> None:
    """Force LF line endings and a pristine frozen-revision working tree."""

    run_cmd(["git", "config", "core.autocrlf", "false"], cwd=str(repo), check=True)
    run_cmd(["git", "reset", "--hard", QUIXBUGS_REVISION], cwd=str(repo), check=True)
    run_cmd(["git", "clean", "-fdx"], cwd=str(repo), check=True)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def compute_tree_identity(root: Path) -> Dict[str, Any]:
    """Deterministic tree identity of a directory tree.

    Same per-file convention as the accepted adapter identity but sorted by
    POSIX relative path string (explicit, cross-platform deterministic);
    returns the tree identity plus per-file records.
    """

    files: List[Dict[str, Any]] = []
    for full in sorted(root.rglob("*")):
        if not full.is_file():
            continue
        rel = full.relative_to(root).as_posix()
        digest = _sha256_file(full)
        files.append({"path": rel, "sha256": digest, "size_bytes": full.stat().st_size})
    combined = hashlib.sha256()
    for rec in sorted(files, key=lambda r: r["path"]):
        combined.update(rec["path"].encode("utf-8"))
        combined.update(b"\0")
        combined.update(rec["sha256"].encode("ascii"))
        combined.update(b"\0")
    return {"tree_identity_sha256": combined.hexdigest(), "files": files}


def materialize_scoped_corpus(repo: Path, scoped_root: Path, *, logger=None) -> Dict[str, Any]:
    """Build the anti-oracle-scoped corpus view from the verified checkout.

    Copies ``python_programs/`` and ``python_testcases/`` byte-identically
    into ``scoped_root`` (which is created fresh; any existing content is
    removed first so the view is exactly reproducible), verifies every
    copied file against the source SHA-256, asserts that no forbidden
    answer-bearing entry exists inside the scoped root, and returns the
    scoped tree identity.
    """

    scoped_root = Path(scoped_root)
    if scoped_root.exists():
        shutil.rmtree(scoped_root)
    scoped_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in SCOPED_CORPUS_DIRS:
        src = repo / name
        if not src.is_dir():
            raise QuixBugsError(f"frozen checkout lacks {name}/: {src}")
        dst = scoped_root / name
        shutil.copytree(src, dst)
        # Byte-identity verification of every copied file.
        for full in sorted(dst.rglob("*")):
            if not full.is_file():
                continue
            rel = full.relative_to(dst).as_posix()
            src_file = src / rel
            if _sha256_file(full) != _sha256_file(src_file):
                raise QuixBugsError(
                    f"scoped copy drift: {name}/{rel} differs from the frozen checkout"
                )
            copied += 1
    # Anti-oracle assertion: nothing but the two scope dirs may exist.
    for entry in sorted(os.listdir(scoped_root)):
        if entry not in SCOPED_CORPUS_DIRS:
            raise QuixBugsError(
                f"scoped corpus root contains unexpected entry {entry!r}"
            )
    identity = compute_tree_identity(scoped_root)
    if logger:
        logger.log(
            f"SCOPED corpus: {copied} files, tree_identity_sha256 "
            f"{identity['tree_identity_sha256']}"
        )
    return {"copied_files": copied, **identity}
