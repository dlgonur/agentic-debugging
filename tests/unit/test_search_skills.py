from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from agentic_debugger.runtime.exceptions import (
    SourceInspectionError,
    SourceParseError,
)
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.skills.search_skills import (
    SymbolMatch,
    SymbolSource,
    find_class,
    find_function,
    get_class_source,
    get_function_source,
    search_code,
)


@pytest.fixture
def sample_workspace():
    tmp = Path(tempfile.mkdtemp())
    try:
        src = tmp / "source"
        src.mkdir()

        (src / "math_utils.py").write_text(
            "def add(a, b):\n"
            "    return a + b\n"
            "\n"
            "def subtract(a, b):\n"
            "    return a - b\n"
        )

        (src / "greeter.py").write_text(
            "def greet(name):\n"
            '    return f"Hello, {name}!"\n'
            "\n"
            "def greet_upper(name):\n"
            '    return f"HELLO, {name}!"\n'
        )

        (src / "async_demo.py").write_text(
            "async def fetch_data(url):\n"
            "    return url\n"
        )

        (src / "shop").mkdir()
        (src / "shop" / "models.py").write_text(
            "class Product:\n"
            "    def __init__(self, name, price):\n"
            "        self.name = name\n"
            "        self.price = price\n"
            "\n"
            "    def get_discounted(self, percent):\n"
            "        return self.price * (1 - percent / 100)\n"
            "\n"
            "class Inventory:\n"
            "    def __init__(self):\n"
            "        self.items = []\n"
        )

        (src / "nested.py").write_text(
            "def outer():\n"
            "    def inner():\n"
            "        return 42\n"
            "    return inner()\n"
            "\n"
            "class Outer:\n"
            "    class Inner:\n"
            "        pass\n"
        )

        (src / "decorated.py").write_text(
            "@staticmethod\n"
            "def static_example():\n"
            "    return 1\n"
            "\n"
            "@classmethod\n"
            "@property\n"
            "def multi_decorated(cls):\n"
            "    return 2\n"
        )

        (src / "syntax_error.py").write_text(
            "def broken(\n"
            "    pass\n"
        )

        (src / "subdir").mkdir()
        (src / "subdir" / "ignored.pyc").write_text("fake bytecode")
        (src / "subdir" / "__pycache__").mkdir()
        (src / "subdir" / "__pycache__" / "cached.pyc").write_text("cache")
        (src / "subdir" / "actual.py").write_text(
            "def hi():\n    return 'hello'\n"
        )

        yield TaskWorkspace(str(src))
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


class TestSearchCode:
    def test_literal_match(self, sample_workspace):
        matches, truncated = search_code(sample_workspace, "return a + b")
        assert len(matches) >= 1
        assert any("return a + b" in m.line_text for m in matches)

    def test_case_sensitive_default(self, sample_workspace):
        matches, _ = search_code(sample_workspace, "def add")
        assert len(matches) >= 1
        matches_upper, _ = search_code(sample_workspace, "DEF ADD")
        lower = sum(1 for m in matches_upper if "DEF ADD" in m.line_text)
        assert lower == 0

    def test_case_insensitive(self, sample_workspace):
        matches, _ = search_code(
            sample_workspace, "def add", case_sensitive=False
        )
        assert len(matches) >= 1

    def test_path_scoped(self, sample_workspace):
        matches, _ = search_code(
            sample_workspace, "return", path="greeter.py"
        )
        assert len(matches) >= 1
        assert all(m.path == "greeter.py" for m in matches)

    def test_directory_scoped(self, sample_workspace):
        matches, _ = search_code(
            sample_workspace, "def", path="shop"
        )
        assert len(matches) >= 2
        assert all(m.path.startswith("shop/") for m in matches)

    def test_python_only_default(self, sample_workspace):
        (Path(sample_workspace.root) / "readme.txt").write_text(
            "def hello\n"
        )
        matches, _ = search_code(sample_workspace, "def")
        assert not any(m.path == "readme.txt" for m in matches)

    def test_deterministic_order(self, sample_workspace):
        matches1, _ = search_code(sample_workspace, "def")
        matches2, _ = search_code(sample_workspace, "def")
        paths1 = [(m.path, m.line_number) for m in matches1]
        paths2 = [(m.path, m.line_number) for m in matches2]
        assert paths1 == paths2

    def test_match_limit_truncation_exact(self, sample_workspace):
        (Path(sample_workspace.root) / "many.py").write_text(
            "".join(f"x = {i}\n" for i in range(200))
        )
        matches, truncated = search_code(
            sample_workspace, "x = ", max_matches=50
        )
        assert len(matches) == 50
        assert truncated is True

    def test_match_limit_not_truncated(self, sample_workspace):
        (Path(sample_workspace.root) / "few.py").write_text(
            "a = 1\nb = 2\n"
        )
        matches, truncated = search_code(
            sample_workspace, "b = 2", max_matches=10
        )
        assert len(matches) == 1
        assert truncated is False

    def test_zero_matches_no_truncation(self, sample_workspace):
        matches, truncated = search_code(
            sample_workspace, "zzz_nonexistent_zzz"
        )
        assert len(matches) == 0
        assert truncated is False

    def test_match_limit_reached_exactly(self, sample_workspace):
        (Path(sample_workspace.root) / "exact.py").write_text(
            "".join(f"val = {i}\n" for i in range(5))
        )
        matches, truncated = search_code(
            sample_workspace, "val", max_matches=5
        )
        assert len(matches) == 5
        assert truncated is False

    def test_empty_query_rejected(self, sample_workspace):
        with pytest.raises(SourceInspectionError, match="non-empty"):
            search_code(sample_workspace, "")

    def test_whitespace_query_rejected(self, sample_workspace):
        with pytest.raises(SourceInspectionError, match="non-empty"):
            search_code(sample_workspace, "   ")

    def test_long_query_capped(self, sample_workspace):
        with pytest.raises(SourceInspectionError, match="maximum length"):
            search_code(sample_workspace, "x" * 300)

    def test_invalid_scope_path(self, sample_workspace):
        with pytest.raises(SourceInspectionError, match="does not exist"):
            search_code(sample_workspace, "def", path="nonexistent")

    def test_ignores_pyc_and_cache(self, sample_workspace):
        matches, _ = search_code(sample_workspace, "bytecode")
        assert not any("bytecode" in m.line_text for m in matches)

    def test_binary_file_skip(self, sample_workspace):
        bin_path = os.path.join(sample_workspace.root, "data.bin")
        with open(bin_path, "wb") as f:
            f.write(b"hello\x00world\ndef\n")
        matches, _ = search_code(sample_workspace, "def")
        assert all(m.path != "data.bin" for m in matches)

    def test_short_line_unchanged(self, sample_workspace):
        (Path(sample_workspace.root) / "short_line.py").write_text(
            "x = 1\n"
        )
        matches, _ = search_code(sample_workspace, "x = 1")
        assert len(matches) == 1
        assert matches[0].line_text == "x = 1"
        assert matches[0].line_truncated is False

    def test_long_line_bounded(self, sample_workspace):
        long_line = "x = " + "a" * 1000
        (Path(sample_workspace.root) / "long_line.py").write_text(
            long_line + "\n"
        )
        matches, _ = search_code(sample_workspace, "a" * 100)
        assert len(matches) >= 1
        assert len(matches[0].line_text) <= 510
        assert matches[0].line_truncated is True

    def test_truncated_mapping_contains_flag(self, sample_workspace):
        long_line = "x = " + "b" * 1000
        (Path(sample_workspace.root) / "trunc_map.py").write_text(
            long_line + "\n"
        )
        matches, _ = search_code(sample_workspace, "b" * 100)
        m = matches[0].to_mapping()
        assert "line_truncated" in m
        assert m["line_truncated"] is True

    def test_match_beyond_retained_start_discovered(self, sample_workspace):
        content = "prefix " + "x" * 600
        (Path(sample_workspace.root) / "beyond_retain.py").write_text(
            content + "\n"
        )
        matches, _ = search_code(sample_workspace, "x" * 50)
        assert len(matches) == 1

    def test_no_code_execution(self, sample_workspace):
        dangerous = os.path.join(sample_workspace.root, "evil.py")
        with open(dangerous, "w") as f:
            f.write('import os\nos.system("malicious")\n')
        matches, _ = search_code(sample_workspace, "malicious")
        assert len(matches) >= 1


class TestFindFunction:
    def test_module_function(self, sample_workspace):
        sym = find_function(sample_workspace, "add")
        assert sym is not None
        assert sym.qualified_name == "add"
        assert sym.kind == "function"
        assert sym.start_line == 1
        assert sym.end_line == 2

    def test_async_function(self, sample_workspace):
        sym = find_function(sample_workspace, "fetch_data")
        assert sym is not None
        assert sym.kind == "async_function"

    def test_method(self, sample_workspace):
        sym = find_function(sample_workspace, "get_discounted", path="shop/models.py")
        assert sym is not None
        assert sym.qualified_name == "Product.get_discounted"
        assert sym.kind == "method"

    def test_nested_function(self, sample_workspace):
        sym = find_function(sample_workspace, "inner", path="nested.py")
        assert sym is not None
        assert sym.qualified_name == "outer.inner"
        assert sym.start_line == 2

    def test_decorated_start_line(self, sample_workspace):
        sym = find_function(
            sample_workspace, "multi_decorated", path="decorated.py"
        )
        assert sym is not None
        assert sym.decorator_start_line == 5

    def test_not_found(self, sample_workspace):
        sym = find_function(sample_workspace, "nonexistent_func")
        assert sym is None

    def test_syntax_error_file(self, sample_workspace):
        with pytest.raises((SourceParseError, SourceInspectionError)):
            find_function(sample_workspace, "broken", path="syntax_error.py")

    def test_symbol_not_found_in_scope(self, sample_workspace):
        sym = find_function(sample_workspace, "add", path="greeter.py")
        assert sym is None

    def test_ambiguous_name(self, sample_workspace):
        (Path(sample_workspace.root) / "dup.py").write_text(
            "def foo():\n    pass\n"
        )
        (Path(sample_workspace.root) / "dup2.py").write_text(
            "def foo():\n    pass\n"
        )
        with pytest.raises(SourceInspectionError, match="Ambiguous"):
            find_function(sample_workspace, "foo")


class TestFindClass:
    def test_class(self, sample_workspace):
        sym = find_class(sample_workspace, "Product")
        assert sym is not None
        assert sym.qualified_name == "Product"
        assert sym.kind == "class"

    def test_nested_class(self, sample_workspace):
        sym = find_class(sample_workspace, "Inner", path="nested.py")
        assert sym is not None
        assert sym.qualified_name == "Outer.Inner"

    def test_not_found(self, sample_workspace):
        sym = find_class(sample_workspace, "NonexistentClass")
        assert sym is None


class TestScopeClassification:
    def test_lowercase_class_method(self, sample_workspace):
        (Path(sample_workspace.root) / "lowercase_cls.py").write_text(
            "class lowercase:\n"
            "    def method(self):\n"
            "        pass\n"
        )
        sym = find_function(
            sample_workspace, "lowercase.method", path="lowercase_cls.py"
        )
        assert sym is not None
        assert sym.kind == "method"

    def test_uppercase_function_nested(self, sample_workspace):
        (Path(sample_workspace.root) / "upper_func.py").write_text(
            "def Upper():\n"
            "    def inner():\n"
            "        pass\n"
        )
        sym = find_function(
            sample_workspace, "Upper.inner", path="upper_func.py"
        )
        assert sym is not None
        assert sym.kind == "function"

    def test_lowercase_class_find_class(self, sample_workspace):
        (Path(sample_workspace.root) / "foo_cls.py").write_text(
            "class foo:\n"
            "    pass\n"
        )
        sym = find_class(sample_workspace, "foo")
        assert sym is not None
        assert sym.qualified_name == "foo"

    def test_function_not_masked_by_class(self, sample_workspace):
        (Path(sample_workspace.root) / "shadow.py").write_text(
            "class foo:\n"
            "    def foo(self):\n"
            "        pass\n"
            "\n"
            "def foo():\n"
            "    pass\n"
        )
        sym = find_function(sample_workspace, "foo", path="shadow.py")
        assert sym is not None

    def test_class_method_collision(self, sample_workspace):
        (Path(sample_workspace.root) / "collision.py").write_text(
            "class foo:\n"
            "    pass\n"
            "\n"
            "class C:\n"
            "    def foo(self):\n"
            "        pass\n"
        )
        cls_sym = find_class(sample_workspace, "foo", path="collision.py")
        assert cls_sym is not None
        assert cls_sym.qualified_name == "foo"
        assert cls_sym.kind == "class"
        func_sym = find_function(sample_workspace, "foo", path="collision.py")
        assert func_sym is not None
        assert func_sym.qualified_name == "C.foo"
        assert func_sym.kind == "method"

    def test_kind_filter_before_exact_match(self, sample_workspace):
        (Path(sample_workspace.root) / "kind_order.py").write_text(
            "class foo:\n"
            "    pass\n"
            "\n"
            "def foo():\n"
            "    pass\n"
        )
        func_sym = find_function(sample_workspace, "foo", path="kind_order.py")
        assert func_sym is not None
        assert func_sym.kind == "function"

    def test_multiple_methods_same_basename_ambiguous(self, sample_workspace):
        (Path(sample_workspace.root) / "multi_method.py").write_text(
            "class A:\n"
            "    def foo(self):\n"
            "        pass\n"
            "\n"
            "class B:\n"
            "    def foo(self):\n"
            "        pass\n"
        )
        with pytest.raises(SourceInspectionError, match="Ambiguous"):
            find_function(sample_workspace, "foo", path="multi_method.py")

    def test_qualified_lookup_resolves(self, sample_workspace):
        (Path(sample_workspace.root) / "qual_lookup.py").write_text(
            "class A:\n"
            "    def foo(self):\n"
            "        pass\n"
            "\n"
            "class B:\n"
            "    def foo(self):\n"
            "        pass\n"
        )
        sym = find_function(
            sample_workspace, "A.foo", path="qual_lookup.py"
        )
        assert sym is not None
        assert sym.qualified_name == "A.foo"


class TestGetFunctionSource:
    def test_source_range_exact(self, sample_workspace):
        src = get_function_source(sample_workspace, "add")
        assert src is not None
        assert src.start_line == 1
        assert src.end_line == 2
        assert len(src.source_lines) == 2
        assert src.source_lines[0].text == "def add(a, b):"
        assert src.source_lines[1].text == "    return a + b"

    def test_method_source(self, sample_workspace):
        src = get_function_source(
            sample_workspace, "get_discounted", path="shop/models.py"
        )
        assert src is not None
        assert src.qualified_name == "Product.get_discounted"

    def test_not_found(self, sample_workspace):
        src = get_function_source(sample_workspace, "nonexistent")
        assert src is None

    def test_decorated_function_source_includes_decorators(self, sample_workspace):
        src = get_function_source(
            sample_workspace, "multi_decorated", path="decorated.py"
        )
        assert src is not None
        assert src.decorator_start_line == 5
        assert src.start_line == 7
        first_line = src.source_lines[0]
        assert first_line.line_number == 5
        assert first_line.text == "@classmethod"

    def test_static_method_source_includes_decorator(self, sample_workspace):
        src = get_function_source(
            sample_workspace, "static_example", path="decorated.py"
        )
        assert src is not None
        assert src.decorator_start_line == 1
        assert src.start_line == 2
        assert src.source_lines[0].text == "@staticmethod"

    def test_async_function_source(self, sample_workspace):
        src = get_function_source(sample_workspace, "fetch_data")
        assert src is not None
        assert src.kind == "async_function"


class TestGetClassSource:
    def test_class_source_range(self, sample_workspace):
        src = get_class_source(sample_workspace, "Product")
        assert src is not None
        assert src.kind == "class"
        assert src.start_line >= 1

    def test_not_found(self, sample_workspace):
        src = get_class_source(sample_workspace, "NonexistentClass")
        assert src is None
