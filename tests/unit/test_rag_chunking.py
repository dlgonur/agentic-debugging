"""Deterministic chunking tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_debugger.rag.chunking import ChunkingError, chunk_document
from agentic_debugger.rag.corpus import CorpusError, build_corpus
from agentic_debugger.rag.schema import (
    MAX_CHUNK_BYTES,
    MAX_CHUNK_LINES,
    Chunk,
    CorpusDocument,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "agentic_debugger" / "datasets" / "curated"
TASK_ID = "curated-off-by-one-002"


def _doc(text: str, kind: str = "source", path: str = "a.py") -> CorpusDocument:
    return CorpusDocument(
        document_id=f"{kind}:{path}", kind=kind, path=path, text=text
    )


def test_symbol_chunks_are_deterministic():
    corpus = build_corpus(str(FIXTURES / TASK_ID), mode="fixture", task_id=TASK_ID)
    source = [d for d in corpus.documents if d.path == "recent_window.py"][0]
    chunks = chunk_document(source)
    again = chunk_document(source)
    assert [c.chunk_id for c in chunks] == [c.chunk_id for c in again]
    assert chunks[0].start_line == 1
    assert chunks[0].kind == "source"
    assert chunks[0].document_id == "source:recent_window.py"
    assert all(c.path == "recent_window.py" for c in chunks)


def test_fallback_line_windows_for_unparseable_text():
    text = "\n".join(f"line {i} not python" for i in range(1, 100)) + "\n"
    doc = _doc(text)
    chunks = chunk_document(doc)
    assert len(chunks) >= 2  # 99 lines split into windows of 40
    assert all(c.end_line >= c.start_line for c in chunks)
    # Windows are contiguous and cover the document.
    first, last = chunks[0], chunks[-1]
    assert first.start_line == 1
    assert last.end_line == 99


def test_symbol_wider_than_max_is_subdivided():
    body = "\n".join(f"    pass  # {i}" for i in range(1, MAX_CHUNK_LINES + 50))
    text = f"def huge():\n{body}\n"
    chunks = chunk_document(_doc(text))
    assert len(chunks) > 1
    assert all(c.end_line - c.start_line + 1 <= MAX_CHUNK_LINES for c in chunks)
    # Coverage is complete and non-overlapping.
    covered = sorted((c.start_line, c.end_line) for c in chunks)
    assert covered[0][0] == 1
    assert covered[-1][1] == MAX_CHUNK_LINES + 50
    for (_, prev_end), (next_start, _) in zip(covered, covered[1:]):
        assert next_start == prev_end + 1


def test_oversized_line_is_fail_closed():
    from agentic_debugger.rag.schema import MAX_LINE_BYTES

    text = "x" * (MAX_LINE_BYTES + 1) + "\n"
    with pytest.raises(ChunkingError):
        chunk_document(_doc(text))


def test_chunk_bytes_are_bounded():
    lines = ["y" * 4000 + "\n", "z" * 4000 + "\n"] * 5
    doc = _doc("".join(lines))
    chunks = chunk_document(doc)
    assert all(c.text_bytes <= MAX_CHUNK_BYTES for c in chunks)


def test_empty_document_has_no_chunks():
    assert chunk_document(_doc("")) == []


def test_chunking_error_is_a_corpus_error():
    assert issubclass(ChunkingError, CorpusError)


def test_chunk_identity_requires_provenance_and_text():
    text = "def f():\n    return 1\n"
    a = Chunk.identity("source:a.py", "a.py", 1, 2, text)
    b = Chunk.identity("source:a.py", "a.py", 1, 2, text)
    c = Chunk.identity("source:a.py", "a.py", 2, 2, text)
    assert a == b
    assert a != c


def test_complete_module_line_coverage_with_gap_chunks():
    """Every non-empty line must be represented by at least one chunk."""
    module = (
        '"""Module docstring."""\n'
        "import os\n"
        "from typing import Optional\n"
        "\n"
        "CONSTANT = 42\n"
        "\n"
        "\n"
        "@decorator\n"
        "def decorated(value):\n"
        "    return value\n"
        "\n"
        "\n"
        "class Widget:\n"
        "    def method(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        'between = "code"\n'
        "\n"
        "\n"
        "def last():\n"
        "    return CONSTANT\n"
        "\n"
        "\n"
        "TRAILING = True\n"
    )
    doc = _doc(module, path="module.py")
    chunks = chunk_document(doc)
    again = chunk_document(doc)
    assert [c.chunk_id for c in chunks] == [c.chunk_id for c in again]
    assert len(chunks) >= 5  # docstring/imports + constant + function + class + trailing

    total_lines = len(module.splitlines())
    covered = set()
    for chunk in chunks:
        covered.update(range(chunk.start_line, chunk.end_line + 1))
    assert covered == set(range(1, total_lines + 1)), "line coverage is incomplete"

    # No silent loss and deterministic non-overlapping symbol/gap layout:
    # every line appears in at least one chunk and chunk boundaries are
    # deterministic (asserted via identical ids above).
    texts = [c.text for c in chunks]
    assert any("Module docstring" in t for t in texts)      # docstring retrievable
    assert any("import os" in t for t in texts)              # imports retrievable
    assert any("CONSTANT = 42" in t for t in texts)          # constant retrievable
    assert any("TRAILING = True" in t for t in texts)        # trailing assignment
    assert any("@decorator" in t for t in texts)             # decorated symbol
    assert any("class Widget" in t for t in texts)           # class symbol

    # Symbol chunks remain useful: the decorated function starts at its
    # decorator line.
    decorated = [c for c in chunks if "@decorator" in c.text]
    assert decorated
    assert decorated[0].start_line == 8
