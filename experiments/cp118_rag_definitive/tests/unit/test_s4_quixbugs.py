"""S4 — QuixBugs frozen-revision scoping and anti-oracle tests.

Prove, WITHOUT network access or model load:

1. the scoped corpus materialization copies only
   ``python_programs/`` + ``python_testcases/`` byte-identically and
   rejects any other top-level entry (gold code, harness, docs);
2. the tree identity is deterministic;
3. the frozen-revision checkout (when present under ``tmp/s4/QuixBugs``)
   contains the answer-bearing directories that the scoping must exclude,
   and the scoped view of the real checkout contains none of them.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from experiments.cp118_rag_definitive.s4_quixbugs import (
    FORBIDDEN_SCOPED_ENTRIES,
    SCOPED_CORPUS_DIRS,
    QuixBugsError,
    compute_tree_identity,
    materialize_scoped_corpus,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
REAL_CHECKOUT = REPO_ROOT / "tmp" / "s4" / "QuixBugs"
REAL_SCOPED = REPO_ROOT / "tmp" / "s4" / "scoped-corpus-v1"


def _fake_repo(tmp_path: Path) -> Path:
    """A fake QuixBugs-shaped checkout containing gold code and harness
    files that the scoping must exclude."""

    repo = tmp_path / "QuixBugs"
    (repo / "python_programs").mkdir(parents=True)
    (repo / "python_testcases").mkdir(parents=True)
    (repo / "correct_python_programs").mkdir()
    (repo / "json_testcases").mkdir()
    (repo / "python_programs" / "gcd.py").write_text("def gcd(a, b):\n    pass\n",
                                                     encoding="utf-8")
    (repo / "python_testcases" / "test_gcd.py").write_text(
        "def test_gcd():\n    pass\n", encoding="utf-8")
    (repo / "correct_python_programs" / "gcd.py").write_text(
        "def gcd(a, b):\n    return a\n", encoding="utf-8")
    (repo / "conftest.py").write_text("# harness\n", encoding="utf-8")
    (repo / "README.md").write_text("# readme\n", encoding="utf-8")
    return repo


def test_scoped_materialization_excludes_gold_and_harness(tmp_path):
    repo = _fake_repo(tmp_path)
    scoped = tmp_path / "scoped"
    result = materialize_scoped_corpus(repo, scoped)
    assert result["copied_files"] == 2
    entries = sorted(p.name for p in scoped.iterdir())
    assert entries == sorted(SCOPED_CORPUS_DIRS)
    for forbidden in FORBIDDEN_SCOPED_ENTRIES:
        assert not (scoped / forbidden).exists()
    # Byte identity preserved.
    assert (scoped / "python_programs" / "gcd.py").read_bytes() == (
        repo / "python_programs" / "gcd.py").read_bytes()


def test_scoped_tree_identity_deterministic(tmp_path):
    repo = _fake_repo(tmp_path)
    a = materialize_scoped_corpus(repo, tmp_path / "s1")
    b = materialize_scoped_corpus(repo, tmp_path / "s2")
    assert a["tree_identity_sha256"] == b["tree_identity_sha256"]
    assert compute_tree_identity(tmp_path / "s1") == compute_tree_identity(
        tmp_path / "s2")


def test_scoped_rejects_unknown_entry(tmp_path, monkeypatch):
    """The final anti-oracle guard fires if a copy step ever planted an
    entry outside the two scope dirs (simulated copy bug)."""

    import shutil

    import experiments.cp118_rag_definitive.s4_quixbugs as qx

    repo = _fake_repo(tmp_path)
    scoped = tmp_path / "scoped"
    real_copytree = shutil.copytree

    def sneaky(src, dst, *args, **kwargs):
        real_copytree(src, dst, *args, **kwargs)
        if Path(src).name == "python_programs":
            (Path(dst).parent / "extra.txt").write_text("x", encoding="utf-8")

    monkeypatch.setattr(qx.shutil, "copytree", sneaky)
    with pytest.raises(QuixBugsError, match="unexpected entry"):
        materialize_scoped_corpus(repo, scoped)


@pytest.mark.skipif(
    not REAL_CHECKOUT.is_dir() or not (REAL_CHECKOUT / ".git").is_dir(),
    reason="frozen QuixBugs checkout not present under tmp/s4",
)
def test_real_checkout_contains_gold_dirs_that_scoping_excludes():
    """The frozen revision genuinely ships answer-bearing content — the
    scoping is not hypothetical."""

    assert (REAL_CHECKOUT / "correct_python_programs").is_dir()
    assert (REAL_CHECKOUT / "correct_java_programs").is_dir()
    assert (REAL_CHECKOUT / "conftest.py").is_file()


@pytest.mark.skipif(
    not REAL_SCOPED.is_dir(),
    reason="scoped corpus not yet materialized under tmp/s4",
)
def test_real_scoped_corpus_is_anti_oracle_clean():
    for forbidden in FORBIDDEN_SCOPED_ENTRIES:
        assert not (REAL_SCOPED / forbidden).exists()
    entries = sorted(p.name for p in REAL_SCOPED.iterdir())
    assert entries == sorted(SCOPED_CORPUS_DIRS)
