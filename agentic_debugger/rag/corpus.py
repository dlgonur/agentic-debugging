"""Corpus ingestion for the repository-native RAG subsystem.

Two modes are supported:

* ``fixture`` (default) — the corpus root is a curated task fixture
  directory; source files, test files, the safe task/issue projection and an
  optional captured failure excerpt are ingested; the corpus is bound to the
  task identity;
* ``repo`` — a declared corpus root (e.g. the whole repository) is walked
  with the same caps and exclusion rules; ``*.py`` files are ingested as
  source (tests under a ``tests`` directory as test documents) and,
  when requested, ``*.md`` files as documentation documents.

Selection policy is explicit, never silent:

* paths matched by the declared exclusion rules are skipped and counted;
* paths that are not selected by the mode's selection policy are counted as
  not selected;
* a *selected* file that is oversized, binary, undecodable, or a symlink is
  a typed error (fail-closed) — it is never silently ingested or skipped.

The task/issue projection is built from an explicit whitelist of agent-visible
fields only.  Oracle fields (ground-truth localization, expected patch, root
cause summary, fixed revision) can never enter the index; this is unit-tested.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.rag.schema import (
    MAX_FAILURE_DOC_BYTES,
    MAX_FILE_BYTES,
    CorpusDocument,
    CorpusSourceKind,
    RagInputError,
    RagValidationError,
    canonical_json,
    corpus_digest_of,
    sha256_text,
)

#: Directories excluded by declared rule (in addition to hidden dirs).
EXCLUDED_DIR_NAMES: Tuple[str, ...] = (
    ".git",
    ".opencode",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
    "_ai-review",
    "operator",
    "runs",
    "outputs",
    "artifacts",
    "checkpoints",
    "models",
    "tmp",
    "temp",
    "demo-out",
)

#: File suffixes excluded by declared rule.
EXCLUDED_FILE_SUFFIXES: Tuple[str, ...] = (
    ".pyc",
    ".pyo",
    ".pt",
    ".pth",
    ".safetensors",
    ".bin",
    ".ckpt",
    ".onnx",
    ".log",
    ".zip",
    ".gz",
    ".tar",
    ".tgz",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".whl",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".lock",
    ".pkl",
)

#: Hard cap on a derived failure-output document (bytes).
FAILURE_EXCERPT_CAP_BYTES = MAX_FAILURE_DOC_BYTES

#: Lines dropped from derived failure output (pytest durations and the like).
_FAILURE_DROP_LINE_RE = re.compile(r"^(\s*\d+ failed|1 passed|.*in \d+\.\d+[a-z]+\.?\s*$)")
_FAILURE_KEEP_LINE_RE = re.compile(
    r"^(E |FAILED|tests?[/\\]|.*(?:Error|assert|raise|Traceback|File \")|"
    r"\s*>\s+|.*Exception|\[failure-output-truncated\])"
)


class CorpusError(RagInputError):
    """Raised when a corpus cannot be built from the declared root."""


def is_excluded_rel(rel_path: str, is_dir: bool) -> Tuple[bool, str]:
    """Declared exclusion decision for a repository-relative path.

    Returns ``(excluded, reason)``.  Hidden entries, virtual environments,
    caches, model weights, review packages, generated output directories and
    known binary/archive suffixes are excluded by rule.
    """

    normalized = rel_path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    if is_dir:
        if name in EXCLUDED_DIR_NAMES:
            return True, f"excluded directory: {name}"
        if name.startswith("."):
            return True, "hidden directory"
        return False, ""
    if name.startswith("."):
        return True, "hidden file"
    if name.endswith(".pyc") or name.endswith(".pyo"):
        return True, "bytecode"
    if any(name.endswith(suffix) for suffix in EXCLUDED_FILE_SUFFIXES):
        return True, f"excluded suffix: {name.rsplit('.', 1)[-1]}"
    return False, ""


def project_failure_output(text: str, *, cap_bytes: int = FAILURE_EXCERPT_CAP_BYTES,
                           workspace_root: Optional[str] = None) -> str:
    """Derive a deterministic, bounded failure/traceback document.

    Only stable diagnostic lines are kept (assertions, exceptions, FAILED
    markers, traceback frames); pytest duration lines are dropped; disposable
    workspace paths are normalized away.  The result is capped at
    ``cap_bytes`` with an explicit truncation marker when the cap is reached.
    """

    if type(text) is not str:
        raise CorpusError("failure output must be a string")
    from agentic_debugger.evaluation.runner import normalize_output

    normalized = normalize_output(text, workspace_root)
    kept: List[str] = []
    for raw_line in normalized.splitlines():
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        if _FAILURE_DROP_LINE_RE.match(line):
            continue
        if _FAILURE_KEEP_LINE_RE.match(line):
            kept.append(line)
    if not kept:
        return ""
    joined = "\n".join(kept) + "\n"
    encoded = joined.encode("utf-8")
    if len(encoded) <= cap_bytes:
        return joined
    cut = encoded[:cap_bytes]
    # Cut at the last newline inside the cap for a deterministic boundary.
    boundary = cut.rfind(b"\n")
    if boundary > 0:
        cut = cut[:boundary]
    marker = b"\n[failure-output-truncated]\n"
    if len(cut) + len(marker) > cap_bytes:
        cut = cut[: cap_bytes - len(marker)]
    return cut.decode("utf-8", errors="replace") + marker.decode("utf-8")


def task_issue_projection(task: DebugTask) -> str:
    """The safe task/issue projection for indexing.

    Built from an explicit whitelist of agent-visible fields only:
    ``task_id``, ``title``, ``description`` and ``tags``.  Oracle fields
    (``oracle.root_cause_summary``, ``oracle.target_files``,
    ``oracle.target_symbols``, ``oracle.runtime_evidence_hint``),
    ``source.provenance.fixed_revision`` and every other evaluator-only field
    are structurally absent.
    """

    lines = [
        f"task_id: {task.task_id}",
        f"title: {task.title}",
        f"description: {task.description}",
        f"tags: {','.join(task.tags)}",
    ]
    return "\n".join(lines) + "\n"


def _is_binary(blob: bytes) -> bool:
    return b"\x00" in blob[:8192]


def _read_selected_file(full_path: str, rel_path: str) -> str:
    if os.path.islink(full_path):
        raise CorpusError(f"selected file is a symlink: {rel_path!r}")
    try:
        size = os.path.getsize(full_path)
    except OSError as exc:
        raise CorpusError(f"cannot stat selected file {rel_path!r}: {exc}") from None
    if size > MAX_FILE_BYTES:
        raise CorpusError(
            f"selected file {rel_path!r} exceeds the {MAX_FILE_BYTES}-byte cap"
        )
    try:
        with open(full_path, "rb") as handle:
            blob = handle.read()
    except OSError as exc:
        raise CorpusError(f"cannot read selected file {rel_path!r}: {exc}") from None
    if _is_binary(blob):
        raise CorpusError(f"selected file {rel_path!r} is binary")
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorpusError(f"selected file {rel_path!r} is not UTF-8 decodable") from exc


@dataclass(frozen=True)
class Corpus:
    """A built corpus: documents, digest, identity and build statistics."""

    mode: str
    root: str
    task_id: Optional[str]
    documents: Tuple[CorpusDocument, ...]
    stats: Dict[str, Any]

    @property
    def digest(self) -> str:
        return corpus_digest_of(self.documents)

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "root": self.root,
            "task_id": self.task_id,
            "digest": self.digest,
            "document_count": len(self.documents),
            "documents": [
                {"document_id": doc.document_id, "kind": doc.kind, "path": doc.path}
                for doc in self.documents
            ],
            "stats": dict(self.stats),
        }


def build_corpus(
    corpus_root: str,
    *,
    mode: str = "fixture",
    task_id: Optional[str] = None,
    failure_text: Optional[str] = None,
    failure_workspace_root: Optional[str] = None,
    include_docs: bool = False,
) -> Corpus:
    """Build a deterministic corpus from a declared root.

    ``fixture`` mode (default): the root must contain ``task.json``; the
    optional ``task_id`` must match it.  Source ``*.py`` files outside a
    ``tests`` directory become ``source`` documents, files under ``tests``
    become ``test`` documents, the task/issue projection becomes one
    ``issue`` document, and ``failure_text`` (already projected via
    :func:`project_failure_output`) becomes one ``failure`` document.

    ``repo`` mode: any declared root; ``*.py`` files become ``source`` or
    ``test`` documents, ``*.md`` files become ``doc`` documents when
    ``include_docs`` is set.  No task binding is applied.
    """

    if type(corpus_root) is not str or not corpus_root:
        raise CorpusError("corpus_root must be a non-empty string")
    if mode not in {"fixture", "repo"}:
        raise CorpusError(f"unknown corpus mode: {mode!r}")
    root = os.path.realpath(corpus_root)
    if not os.path.isdir(root):
        raise CorpusError(f"corpus_root is not a directory: {corpus_root!r}")
    _reject_symlink_tree(root)

    documents: List[CorpusDocument] = []
    stats: Dict[str, Any] = {
        "mode": mode,
        "selected_files": 0,
        "excluded_dirs": 0,
        "excluded_files": 0,
        "not_selected_files": 0,
        "document_count": 0,
    }
    resolved_task_id: Optional[str] = None

    if mode == "fixture":
        task_json = os.path.join(root, "task.json")
        if not os.path.isfile(task_json):
            raise CorpusError(f"fixture corpus root lacks task.json: {corpus_root!r}")
        try:
            task = DebugTask.from_file(task_json)
        except Exception as exc:  # noqa: BLE001 - typed CorpusError boundary
            raise CorpusError(f"fixture task.json is invalid: {exc}") from exc
        if task_id is not None and task_id != task.task_id:
            raise CorpusError(
                f"task_id {task_id!r} does not match fixture task {task.task_id!r}"
            )
        resolved_task_id = task.task_id
        documents.append(
            CorpusDocument(
                document_id=f"issue:{task.task_id}",
                kind=CorpusSourceKind.ISSUE,
                path="task.json",
                text=task_issue_projection(task),
            )
        )
        if failure_text is not None:
            projected = project_failure_output(
                failure_text, workspace_root=failure_workspace_root
            )
            if projected:
                documents.append(
                    CorpusDocument(
                        document_id=f"failure:{task.task_id}:baseline",
                        kind=CorpusSourceKind.FAILURE,
                        path="failure/baseline.txt",
                        text=projected,
                    )
                )
    else:
        if task_id is not None:
            raise CorpusError("repo mode does not accept a task_id binding")

    for rel_path, is_dir, full_path in _walk_sorted(root, mode=mode,
                                                    include_docs=include_docs):
        if is_dir:
            excluded, _reason = is_excluded_rel(rel_path, True)
            if excluded:
                stats["excluded_dirs"] += 1
            continue
        excluded, _reason = is_excluded_rel(rel_path, False)
        if excluded:
            stats["excluded_files"] += 1
            continue
        if not _selects_file(rel_path, mode=mode, include_docs=include_docs):
            stats["not_selected_files"] += 1
            continue
        text = _read_selected_file(full_path, rel_path)
        if rel_path.endswith(".py"):
            is_test = rel_path.replace("\\", "/").startswith("tests/")
            kind = CorpusSourceKind.TEST if is_test else CorpusSourceKind.SOURCE
        else:
            kind = CorpusSourceKind.DOC
        documents.append(
            CorpusDocument(
                document_id=f"{kind}:{rel_path}",
                kind=kind,
                path=rel_path,
                text=text,
            )
        )
        stats["selected_files"] += 1

    documents.sort(key=lambda doc: (doc.kind, doc.path, doc.document_id))
    stats["document_count"] = len(documents)
    return Corpus(
        mode=mode,
        root=root,
        task_id=resolved_task_id,
        documents=tuple(documents),
        stats=stats,
    )


def _selects_file(rel_path: str, *, mode: str, include_docs: bool) -> bool:
    if rel_path.endswith(".py"):
        return True
    if mode == "fixture":
        return False
    return include_docs and rel_path.endswith(".md")


def _reject_symlink_tree(root: str) -> None:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, topdown=True):
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                raise CorpusError(f"corpus contains a symlink directory: {full!r}")
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                raise CorpusError(f"corpus contains a symlink file: {full!r}")


def _walk_sorted(root: str, *, mode: str, include_docs: bool):
    """Deterministic sorted walk that yields ``(rel_path, is_dir, full_path)``."""

    entries: List[Tuple[str, bool, str]] = []

    def visit(dirpath: str, rel: str) -> None:
        try:
            names = sorted(os.listdir(dirpath))
        except OSError as exc:
            raise CorpusError(f"cannot list corpus directory {dirpath!r}: {exc}") from None
        for name in names:
            full = os.path.join(dirpath, name)
            rel_path = f"{rel}/{name}" if rel else name
            if os.path.isdir(full):
                if os.path.islink(full):
                    raise CorpusError(f"corpus contains a symlink directory: {full!r}")
                excluded, _ = is_excluded_rel(rel_path, True)
                if not excluded:
                    entries.append((rel_path, True, full))
                    visit(full, rel_path)
            elif os.path.isfile(full):
                if os.path.islink(full):
                    raise CorpusError(f"corpus contains a symlink file: {full!r}")
                entries.append((rel_path, False, full))

    visit(root, "")
    entries.sort(key=lambda item: (item[0], item[1]))
    return entries


__all__ = [
    "EXCLUDED_DIR_NAMES",
    "EXCLUDED_FILE_SUFFIXES",
    "FAILURE_EXCERPT_CAP_BYTES",
    "CorpusError",
    "Corpus",
    "build_corpus",
    "is_excluded_rel",
    "project_failure_output",
    "task_issue_projection",
]
