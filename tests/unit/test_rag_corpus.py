"""Corpus ingestion tests: sources, exclusions, oracle protection, fail-closed."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.rag.corpus import (
    CorpusError,
    build_corpus,
    is_excluded_rel,
    project_failure_output,
    task_issue_projection,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "agentic_debugger" / "datasets" / "curated"
TASK_ID = "curated-off-by-one-002"


def _make_tree(root: Path, entries) -> None:
    for rel, kind, content in entries:
        path = root / rel
        if kind == "dir":
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            if content is None or isinstance(content, bytes):
                path.write_bytes(content if content is not None else b"\x00\x01binary")
            else:
                path.write_text(content, encoding="utf-8")


def test_fixture_mode_discovers_all_source_categories():
    corpus = build_corpus(str(FIXTURES / TASK_ID), mode="fixture", task_id=TASK_ID)
    kinds = {(doc.kind, doc.path) for doc in corpus.documents}
    assert ("source", "recent_window.py") in kinds
    assert ("test", "tests/test_recent_window.py") in kinds
    assert ("issue", "task.json") in kinds
    assert corpus.task_id == TASK_ID
    assert corpus.digest


def test_fixture_mode_with_failure_document():
    failure = (
        "tests/test_recent_window.py F\n"
        "FAILED tests/test_recent_window.py::test_recent_window_returns_all_values_when_size_equals_length\n"
        "E   assert [10, 20, 30] == [10, 20, 30, 40]\n"
        "1 failed in 0.42s\n"
    )
    corpus = build_corpus(
        str(FIXTURES / TASK_ID), mode="fixture", task_id=TASK_ID, failure_text=failure
    )
    failure_docs = [d for d in corpus.documents if d.kind == "failure"]
    assert len(failure_docs) == 1
    text = failure_docs[0].text
    assert "assert [10, 20, 30]" in text
    assert "0.42s" not in text  # duration lines are dropped deterministically


def test_task_id_mismatch_is_rejected():
    with pytest.raises(CorpusError):
        build_corpus(str(FIXTURES / TASK_ID), mode="fixture", task_id="curated-none-handling-001")


def test_missing_task_json_is_rejected(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(CorpusError):
        build_corpus(str(tmp_path), mode="fixture")


def test_oracle_fields_cannot_enter_the_index():
    """The task/issue projection must never carry evaluator-only fields."""
    task = DebugTask.from_file(str(FIXTURES / TASK_ID / "task.json"))
    projection = task_issue_projection(task)
    for field in (
        "root_cause_summary",
        "target_files",
        "target_symbols",
        "runtime_evidence_hint",
        "fixed_revision",
        "reproduction",
        "fail_to_pass",
    ):
        assert field not in projection, f"oracle/agent-hidden field leaked: {field}"
    # The oracle values themselves must not appear verbatim.
    assert task.oracle.root_cause_summary not in projection
    for symbol in task.oracle.target_symbols:
        assert symbol not in projection

    corpus = build_corpus(str(FIXTURES / TASK_ID), mode="fixture", task_id=TASK_ID)
    corpus_text = "\n".join(doc.text for doc in corpus.documents)
    assert task.oracle.root_cause_summary not in corpus_text
    assert task.oracle.runtime_evidence_hint not in corpus_text


def test_exclusion_rules_cover_all_declared_categories(tmp_path: Path):
    entries = [
        ("src/a.py", "file", "def a():\n    return 1\n"),
        (".git/config", "file", "[core]\n"),
        (".opencode/settings.json", "file", "{}"),
        (".venv/lib/x.py", "file", "x = 1\n"),
        ("venv/y.py", "file", "y = 1\n"),
        ("__pycache__/a.cpython-311.pyc", "file", b"pyc"),
        (".pytest_cache/v/cache/lastfailed", "file", "{}"),
        (".mypy_cache/entry.py", "file", "{}"),
        ("models/model.safetensors", "file", b"weights"),
        ("checkpoints/c.pt", "file", b"ckpt"),
        ("_ai-review/task/agent-report.md", "file", "report"),
        ("operator/auth.json", "file", "{}"),
        ("runs/run1/results.json", "file", "{}"),
        ("outputs/out.json", "file", "{}"),
        ("artifacts/x.zip", "file", b"zip"),
        (".hidden/h.py", "file", "h = 1\n"),
        (".hidden.py", "file", "h = 1\n"),
        ("data/weights.bin", "file", b"bin"),
        ("docs/notes.pdf", "file", b"%PDF"),
        ("tmp/scratch.py", "file", "t = 1\n"),
        ("src/ok.py", "file", "def ok():\n    return 2\n"),
    ]
    _make_tree(tmp_path, entries)
    corpus = build_corpus(str(tmp_path), mode="repo")
    paths = [doc.path for doc in corpus.documents]
    assert "src/ok.py" in paths
    for excluded in (
        ".git/config",
        ".opencode/settings.json",
        ".venv/lib/x.py",
        "venv/y.py",
        "__pycache__/a.cpython-311.pyc",
        ".pytest_cache/v/cache/lastfailed",
        ".mypy_cache/entry.py",
        "models/model.safetensors",
        "checkpoints/c.pt",
        "_ai-review/task/agent-report.md",
        "operator/auth.json",
        "runs/run1/results.json",
        "outputs/out.json",
        "artifacts/x.zip",
        ".hidden/h.py",
        ".hidden.py",
        "data/weights.bin",
        "docs/notes.pdf",
        "tmp/scratch.py",
    ):
        assert excluded not in paths, f"excluded path was ingested: {excluded}"
    assert corpus.stats["excluded_files"] >= 3  # .hidden.py, weights.bin, notes.pdf


def test_excluded_dir_names_are_documented_and_consistent():
    for name in (".git", ".opencode", ".venv", "__pycache__", ".pytest_cache",
                 "_ai-review", "operator", "runs", "outputs", "artifacts",
                 "checkpoints", "models", "node_modules"):
        excluded, reason = is_excluded_rel(name, is_dir=True)
        assert excluded, f"{name} must be excluded"
        assert reason


def test_symlink_is_rejected(tmp_path: Path):
    (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")
    target = tmp_path / "real.py"
    link = tmp_path / "link.py"
    try:
        link.symlink_to(target.name)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    with pytest.raises(CorpusError):
        build_corpus(str(tmp_path), mode="repo")


def test_binary_selected_file_is_fail_closed(tmp_path: Path):
    (tmp_path / "evil.py").write_bytes(b"\x00\x01\x02not really python")
    with pytest.raises(CorpusError):
        build_corpus(str(tmp_path), mode="repo")


def test_undecodable_selected_file_is_fail_closed(tmp_path: Path):
    (tmp_path / "bad.py").write_bytes(b"def f():\n    return '\xff\xfe'\n")
    with pytest.raises(CorpusError):
        build_corpus(str(tmp_path), mode="repo")


def test_oversized_selected_file_is_fail_closed(tmp_path: Path):
    from agentic_debugger.rag.schema import MAX_FILE_BYTES

    (tmp_path / "big.py").write_text("x = 1\n" * (MAX_FILE_BYTES // 6), encoding="utf-8")
    with pytest.raises(CorpusError):
        build_corpus(str(tmp_path), mode="repo")


def test_repo_mode_include_docs_and_not_selected_counts(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("plain", encoding="utf-8")
    corpus = build_corpus(str(tmp_path), mode="repo", include_docs=True)
    kinds = {(doc.kind, doc.path) for doc in corpus.documents}
    assert ("source", "src/a.py") in kinds
    assert ("test", "tests/test_a.py") in kinds
    assert ("doc", "README.md") in kinds
    assert corpus.stats["not_selected_files"] == 1  # notes.txt


def test_declared_corpus_root_repo_mode_works(tmp_path: Path):
    (tmp_path / "module.py").write_text("VALUE = 42\n", encoding="utf-8")
    corpus = build_corpus(str(tmp_path), mode="repo")
    assert [d.path for d in corpus.documents] == ["module.py"]
    with pytest.raises(CorpusError):
        build_corpus(str(tmp_path), mode="repo", task_id="curated-off-by-one-002")


def test_failure_projection_is_deterministic_and_bounded():
    raw = (
        "============================= test session starts =============================\n"
        "F\n"
        "FAILED tests/test_recent_window.py::test_recent_window_returns_all_values_when_size_equals_length\n"
        "E   assert [10, 20, 30] == [10, 20, 30, 40]\n"
        "1 failed in 0.52s\n"
        "\n"
    )
    first = project_failure_output(raw)
    second = project_failure_output(raw)
    assert first == second
    assert "0.52s" not in first
    assert "assert [10, 20, 30]" in first

    tiny = project_failure_output(raw, cap_bytes=64)
    assert tiny.endswith("[failure-output-truncated]\n")
    assert len(tiny.encode("utf-8")) <= 64 + len("[failure-output-truncated]\n")


def test_corpus_digest_changes_with_document_content():
    a = build_corpus(str(FIXTURES / TASK_ID), mode="fixture", task_id=TASK_ID)
    b = build_corpus(str(FIXTURES / TASK_ID), mode="fixture", task_id=TASK_ID)
    assert a.digest == b.digest
    c = build_corpus(
        str(FIXTURES / TASK_ID),
        mode="fixture",
        task_id=TASK_ID,
        failure_text="E   assert 1 == 2\n",
    )
    assert c.digest != a.digest
