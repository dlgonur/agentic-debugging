from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from agentic_debugger.runtime.exceptions import (
    SourceDecodeError,
    SourceInspectionError,
    SourceParseError,
)
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.skills.file_skills import (
    MAX_FILE_SIZE_BYTES,
    SourceLine,
    _read_file_lines,
    _normalize_source_path,
)

MAX_QUERY_LENGTH = 200
MAX_SEARCH_MATCHES = 100
DEFAULT_SEARCH_MAX_MATCHES = 100
MAX_SEARCH_LINE_CHARS = 500


@dataclass(frozen=True)
class SearchMatch:
    path: str
    line_number: int
    line_text: str
    line_truncated: bool = False

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "line_number": self.line_number,
            "line_text": self.line_text,
            "line_truncated": self.line_truncated,
        }


@dataclass(frozen=True)
class SymbolMatch:
    path: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    decorator_start_line: int

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "decorator_start_line": self.decorator_start_line,
        }


@dataclass(frozen=True)
class SymbolSource:
    path: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    decorator_start_line: Optional[int]
    source_lines: List[SourceLine]

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "decorator_start_line": self.decorator_start_line,
            "source_lines": [sl.to_mapping() for sl in self.source_lines],
        }


def _is_hidden_or_cache(name: str) -> bool:
    if name == "__pycache__":
        return True
    if name.endswith(".pyc"):
        return True
    return False


def _walk_python_files(
    workspace: TaskWorkspace,
    scope: Optional[str] = None,
) -> List[str]:
    root_norm = os.path.normpath(workspace.root)

    if scope is not None:
        resolved = workspace.resolve_path(scope)
        resolved_norm = os.path.normpath(resolved)

        if not resolved_norm.startswith(root_norm):
            raise SourceInspectionError(
                f"Scope path escapes workspace: {scope!r}"
            )

        if os.path.isfile(resolved_norm):
            if os.path.islink(resolved_norm):
                return []
            if not resolved_norm.endswith(".py"):
                return []
            rel = os.path.relpath(resolved_norm, root_norm)
            return [rel.replace("\\", "/")]

        if not os.path.isdir(resolved_norm):
            raise SourceInspectionError(
                f"Scope path does not exist: {scope!r}"
            )

        base_norm = resolved_norm
    else:
        base_norm = root_norm

    results: List[str] = []
    for dirpath, dirnames, filenames in os.walk(
        base_norm, followlinks=False, topdown=True
    ):
        dirnames[:] = [
            d for d in dirnames
            if not _is_hidden_or_cache(d) and not d.startswith(".")
        ]
        for fn in sorted(filenames):
            if _is_hidden_or_cache(fn) or fn.startswith("."):
                continue
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue
            if not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, root_norm)
            rel = rel.replace("\\", "/")
            results.append(rel)
    return sorted(results)


def search_code(
    workspace: TaskWorkspace,
    query: str,
    path: Optional[str] = None,
    max_matches: int = DEFAULT_SEARCH_MAX_MATCHES,
    case_sensitive: bool = True,
) -> Tuple[List[SearchMatch], bool]:
    if not query or not query.strip():
        raise SourceInspectionError("Query must be non-empty")
    if len(query) > MAX_QUERY_LENGTH:
        raise SourceInspectionError(
            f"Query exceeds maximum length of {MAX_QUERY_LENGTH}"
        )
    if max_matches < 1:
        max_matches = 1
    if max_matches > MAX_SEARCH_MATCHES:
        max_matches = MAX_SEARCH_MATCHES

    python_files = _walk_python_files(workspace, scope=path)

    matches: List[SearchMatch] = []
    truncated = False
    target_count = max_matches + 1

    for rel_path in python_files:
        if len(matches) >= target_count:
            truncated = True
            break
        try:
            lines = _read_file_lines(workspace, rel_path)
        except (SourceDecodeError, SourceInspectionError):
            continue
        for i, line_bytes in enumerate(lines):
            if len(matches) >= target_count:
                truncated = True
                break
            haystack = line_bytes if case_sensitive else line_bytes.lower()
            needle = query if case_sensitive else query.lower()
            if needle in haystack:
                full_text = line_bytes.rstrip("\n\r")
                line_truncated = len(full_text) > MAX_SEARCH_LINE_CHARS
                if line_truncated:
                    half = MAX_SEARCH_LINE_CHARS // 2
                    display = full_text[:half] + "..." + full_text[-half:]
                else:
                    display = full_text
                matches.append(
                    SearchMatch(
                        path=rel_path,
                        line_number=i + 1,
                        line_text=display,
                        line_truncated=line_truncated,
                    )
                )

    if len(matches) > max_matches:
        matches = matches[:max_matches]
        truncated = True

    return matches, truncated


def _collect_symbols(tree: ast.AST, source_path: str) -> List[SymbolMatch]:
    symbols: List[SymbolMatch] = []

    class Collector(ast.NodeVisitor):
        def __init__(self) -> None:
            self._namespace_stack: List[Tuple[str, str]] = []

        def _qualified_name(self, name: str) -> str:
            if self._namespace_stack:
                parts = [ns[0] for ns in self._namespace_stack]
                return ".".join(parts) + "." + name
            return name

        def _enclosing_scope_kind(self) -> Optional[str]:
            if self._namespace_stack:
                return self._namespace_stack[-1][1]
            return None

        def _kind(self, node: ast.AST) -> str:
            if isinstance(node, ast.ClassDef):
                return "class"
            encl = self._enclosing_scope_kind()
            if isinstance(node, ast.AsyncFunctionDef):
                return "async_method" if encl == "class" else "async_function"
            if isinstance(node, ast.FunctionDef):
                return "method" if encl == "class" else "function"
            return "unknown"

        def _decorator_start(self, node: ast.AST) -> int:
            decs = getattr(node, "decorator_list", [])
            if decs:
                return min(d.lineno for d in decs)
            return node.lineno

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            qname = self._qualified_name(node.name)
            symbols.append(
                SymbolMatch(
                    path=source_path,
                    qualified_name=qname,
                    kind=self._kind(node),
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    decorator_start_line=self._decorator_start(node),
                )
            )
            self._namespace_stack.append((node.name, "function"))
            self.generic_visit(node)
            self._namespace_stack.pop()

        def visit_AsyncFunctionDef(
            self, node: ast.AsyncFunctionDef
        ) -> None:
            qname = self._qualified_name(node.name)
            symbols.append(
                SymbolMatch(
                    path=source_path,
                    qualified_name=qname,
                    kind=self._kind(node),
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    decorator_start_line=self._decorator_start(node),
                )
            )
            self._namespace_stack.append((node.name, "function"))
            self.generic_visit(node)
            self._namespace_stack.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            qname = self._qualified_name(node.name)
            symbols.append(
                SymbolMatch(
                    path=source_path,
                    qualified_name=qname,
                    kind="class",
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    decorator_start_line=self._decorator_start(node),
                )
            )
            self._namespace_stack.append((node.name, "class"))
            self.generic_visit(node)
            self._namespace_stack.pop()

    Collector().visit(tree)
    symbols.sort(key=lambda s: (s.path, s.start_line, s.qualified_name))
    return symbols


def _parse_python_file(
    workspace: TaskWorkspace, rel_path: str
) -> ast.AST:
    resolved = workspace.resolve_path(rel_path, must_exist=True)
    if not os.path.isfile(resolved):
        raise SourceInspectionError(
            f"Path is not a regular file: {rel_path!r}"
        )
    if os.path.islink(resolved):
        raise SourceInspectionError(
            f"Path is a symlink: {rel_path!r}"
        )
    try:
        lines = _read_file_lines(workspace, rel_path)
    except SourceDecodeError as e:
        raise SourceParseError(
            f"Cannot decode file: {rel_path!r}: {e}"
        ) from e
    source = "".join(lines)
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError as e:
        raise SourceParseError(
            f"Syntax error in {rel_path!r}: {e}"
        ) from e
    return tree


def _find_symbol(
    workspace: TaskWorkspace,
    name: str,
    path: Optional[str] = None,
    symbol_kind: Optional[str | Tuple[str, ...]] = None,
) -> List[SymbolMatch]:
    if not name:
        raise SourceInspectionError("Symbol name must be non-empty")

    python_files: List[str]
    is_explicit_file = False
    if path is not None:
        resolved = workspace.resolve_path(path, must_exist=True)
        if os.path.isfile(resolved):
            npath = _normalize_source_path(workspace, path)
            python_files = [npath]
            is_explicit_file = True
        elif os.path.isdir(resolved):
            python_files = _walk_python_files(workspace, scope=path)
        else:
            raise SourceInspectionError(
                f"Path does not exist: {path!r}"
            )
    else:
        python_files = _walk_python_files(workspace)

    results: List[SymbolMatch] = []
    for rel_path in python_files:
        try:
            tree = _parse_python_file(workspace, rel_path)
        except (SourceParseError, SourceDecodeError, SourceInspectionError):
            if is_explicit_file:
                raise
            continue
        symbols = _collect_symbols(tree, rel_path)
        for sym in symbols:
            if symbol_kind is not None:
                if isinstance(symbol_kind, str):
                    if sym.kind != symbol_kind:
                        continue
                elif sym.kind not in symbol_kind:
                    continue
            if sym.qualified_name == name:
                results.append(sym)
            elif sym.qualified_name.endswith("." + name):
                results.append(sym)

    if not results:
        return []

    if len(results) == 1:
        return results

    exact = [s for s in results if s.qualified_name == name]
    if len(exact) == 1:
        return exact

    return results


def find_function(
    workspace: TaskWorkspace,
    name: str,
    path: Optional[str] = None,
) -> Optional[SymbolMatch]:
    results = _find_symbol(
        workspace, name, path=path,
        symbol_kind=("function", "method", "async_function", "async_method"),
    )

    if not results:
        return None

    if len(results) == 1:
        return results[0]

    exact = [r for r in results if r.qualified_name == name]
    if len(exact) == 1:
        return exact[0]

    raise SourceInspectionError(
        f"Ambiguous function name {name!r}: "
        f"{[s.qualified_name for s in results]}"
    )


def find_class(
    workspace: TaskWorkspace,
    name: str,
    path: Optional[str] = None,
) -> Optional[SymbolMatch]:
    results = _find_symbol(workspace, name, path=path, symbol_kind="class")

    if not results:
        return None

    if len(results) == 1:
        return results[0]

    exact = [r for r in results if r.qualified_name == name]
    if len(exact) == 1:
        return exact[0]

    raise SourceInspectionError(
        f"Ambiguous class name {name!r}: "
        f"{[s.qualified_name for s in results]}"
    )


def _get_source_for_symbol(
    workspace: TaskWorkspace,
    symbol: SymbolMatch,
) -> SymbolSource:
    lines = _read_file_lines(workspace, symbol.path)
    source_start = symbol.decorator_start_line
    selected = lines[source_start - 1 : symbol.end_line]
    source_lines = [
        SourceLine(
            path=symbol.path,
            line_number=source_start + i,
            text=l.rstrip("\n\r"),
            is_focal=False,
        )
        for i, l in enumerate(selected)
    ]
    return SymbolSource(
        path=symbol.path,
        qualified_name=symbol.qualified_name,
        kind=symbol.kind,
        start_line=symbol.start_line,
        end_line=symbol.end_line,
        decorator_start_line=symbol.decorator_start_line,
        source_lines=source_lines,
    )


def get_function_source(
    workspace: TaskWorkspace,
    name: str,
    path: Optional[str] = None,
) -> Optional[SymbolSource]:
    sym = find_function(workspace, name, path=path)
    if sym is None:
        return None
    return _get_source_for_symbol(workspace, sym)


def get_class_source(
    workspace: TaskWorkspace,
    name: str,
    path: Optional[str] = None,
) -> Optional[SymbolSource]:
    sym = find_class(workspace, name, path=path)
    if sym is None:
        return None
    return _get_source_for_symbol(workspace, sym)
