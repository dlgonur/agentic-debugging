"""Fixed offline model outputs for the Task 9 curated demonstration.

The MVP has no approved model provider, so the demonstration substitutes a
deterministic offline stand-in for the repair model.  This module holds
everything that stand-in would otherwise have to *produce*:

* the localization claim it asserts;
* the root-cause statement it asserts;
* the reference repair it proposes;
* the runtime probe it uses when the PDB gate allows debugger access.

This module deliberately contains **no** outcome claims.  Whether a repair
localizes correctly, applies, compiles, passes or regresses is always measured
by executing the real controller, runtime and verifier paths.

Reference repairs are stored as exact ``old``/``new`` source snippets rather
than pre-rendered diffs so that the unified diff is always regenerated from the
bytes actually present in the disposable workspace.  A snippet that no longer
matches the fixture is a hard error rather than a silently stale patch.
"""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


class DemoCatalogError(ValueError):
    """Raised when a demonstration scenario is missing or inconsistent."""


@dataclass(frozen=True)
class ReferenceRepair:
    """An exact, minimal source replacement proposed by the offline model."""

    target_path: str
    old_snippet: str
    new_snippet: str

    def __post_init__(self) -> None:
        if not self.target_path or self.target_path != self.target_path.strip():
            raise DemoCatalogError("target_path must be a non-empty trimmed path")
        if "\\" in self.target_path or self.target_path.startswith("/"):
            raise DemoCatalogError("target_path must be a relative POSIX path")
        if not self.old_snippet or not self.new_snippet:
            raise DemoCatalogError("reference repair snippets must be non-empty")
        if self.old_snippet == self.new_snippet:
            raise DemoCatalogError("reference repair must change the source")


@dataclass(frozen=True)
class LocalizationClaim:
    """The file and symbol the offline model claims to be defective."""

    file_path: str
    symbol: str

    def __post_init__(self) -> None:
        if not self.file_path or not self.symbol:
            raise DemoCatalogError("localization claim must be complete")

    def to_mapping(self) -> dict[str, str]:
        return {"file_path": self.file_path, "symbol": self.symbol}


@dataclass(frozen=True)
class RuntimeProbe:
    """A bounded, deterministic way to pause the defect under the debugger.

    The probe never edits the canonical fixture.  A disposable copy of the
    fixture receives one appended module-level driver so the target function
    actually executes, and the breakpoint is resolved from ``anchor`` inside
    the declared function via the standard-library AST rather than from a
    hard-coded line number.
    """

    module_path: str
    focus_function: str
    call_source: str
    anchor: str
    inspect_expressions: tuple[str, ...]
    exact_public_reproduction: bool = False
    breakpoint_line: int = 0

    def __post_init__(self) -> None:
        if not self.module_path.endswith(".py"):
            raise DemoCatalogError("runtime probe module must be a Python file")
        if not self.focus_function or not self.call_source or not self.anchor:
            raise DemoCatalogError("runtime probe fields must be non-empty")
        if type(self.inspect_expressions) is not tuple:
            raise DemoCatalogError("inspect_expressions must be a tuple")
        if not self.inspect_expressions:
            raise DemoCatalogError("runtime probe must declare at least one expression")
        if len(self.inspect_expressions) > MAX_PROBE_EXPRESSIONS:
            raise DemoCatalogError(
                f"runtime probe declares more than {MAX_PROBE_EXPRESSIONS} expressions"
            )
        if len(set(self.inspect_expressions)) != len(self.inspect_expressions):
            raise DemoCatalogError("inspect_expressions must be unique")
        if type(self.exact_public_reproduction) is not bool:
            raise DemoCatalogError("exact_public_reproduction must be boolean")
        if type(self.breakpoint_line) is not int or self.breakpoint_line < 0:
            raise DemoCatalogError("breakpoint_line must be a non-negative integer")
        if self.exact_public_reproduction and self.breakpoint_line <= 0:
            raise DemoCatalogError("exact probes must declare a breakpoint line")

    def to_mapping(self) -> dict[str, object]:
        return {
            "module_path": self.module_path,
            "focus_function": self.focus_function,
            "call_source": self.call_source,
            "anchor": self.anchor,
            "inspect_expressions": list(self.inspect_expressions),
            "exact_public_reproduction": self.exact_public_reproduction,
            "breakpoint_line": self.breakpoint_line,
        }


@dataclass(frozen=True)
class DemoScenario:
    """The complete fixed offline model output for one curated task."""

    task_id: str
    hypothesis_id: str
    root_cause_statement: str
    localization: LocalizationClaim
    reference_repair: ReferenceRepair
    runtime_probe: RuntimeProbe

    def __post_init__(self) -> None:
        if not self.task_id or not self.hypothesis_id:
            raise DemoCatalogError("scenario identifiers must be non-empty")
        statement = self.root_cause_statement
        if not statement or statement != statement.strip():
            raise DemoCatalogError("root_cause_statement must be trimmed and non-empty")
        if len(statement.encode("utf-8")) > 4096:
            raise DemoCatalogError("root_cause_statement exceeds the hypothesis bound")

    def to_mapping(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "hypothesis_id": self.hypothesis_id,
            "root_cause_statement": self.root_cause_statement,
            "localization": self.localization.to_mapping(),
            "reference_repair": {
                "target_path": self.reference_repair.target_path,
            },
            "runtime_probe": self.runtime_probe.to_mapping(),
        }


#: The offline model may evaluate at most this many probe expressions so that
#: the demonstration stays inside the curated ``max_pdb_observations`` budget.
MAX_PROBE_EXPRESSIONS = 3

#: Name of the module-level driver appended to the disposable probe copy.
PROBE_DRIVER_FUNCTION = "_demo_runtime_probe"


_SCENARIOS: tuple[DemoScenario, ...] = (
    DemoScenario(
        task_id="curated-none-handling-001",
        hypothesis_id="demo-none-handling-001",
        root_cause_statement=(
            "format_display_name calls a string method on its optional name "
            "argument before normalizing a missing value to the fallback."
        ),
        localization=LocalizationClaim("display_name.py", "format_display_name"),
        reference_repair=ReferenceRepair(
            "display_name.py",
            "normalized_name = name.strip()",
            "normalized_name = name.strip() if name is not None else \"\"",
        ),
        runtime_probe=RuntimeProbe(
            module_path="display_name.py",
            focus_function="format_display_name",
            call_source="format_display_name(None)",
            anchor="normalized_name = name.strip()",
            inspect_expressions=("name",),
        ),
    ),
    DemoScenario(
        task_id="curated-off-by-one-002",
        hypothesis_id="demo-off-by-one-002",
        root_cause_statement=(
            "recent_window subtracts one from the exclusive end index when the "
            "requested size equals the sequence length, dropping the last value."
        ),
        localization=LocalizationClaim("recent_window.py", "recent_window"),
        reference_repair=ReferenceRepair(
            "recent_window.py",
            "end_index - (1 if requested_size == sequence_length else 0)",
            "end_index",
        ),
        runtime_probe=RuntimeProbe(
            module_path="recent_window.py",
            focus_function="recent_window",
            call_source="recent_window([10, 20, 30, 40], 4)",
            anchor="calculated_indexes = list(",
            inspect_expressions=("sequence_length", "requested_size", "start_index"),
        ),
    ),
    DemoScenario(
        task_id="curated-wrong-branch-003",
        hypothesis_id="demo-wrong-branch-003",
        root_cause_statement=(
            "choose_access evaluates the broad employee condition before the "
            "more specific employee-and-pass case, so the combined case is "
            "never reached."
        ),
        localization=LocalizationClaim("access_branch.py", "choose_access"),
        reference_repair=ReferenceRepair(
            "access_branch.py",
            "    if employee_flag:\n        selected_branch = \"employee\"\n"
            "    elif employee_flag and pass_flag:\n        selected_branch = \"priority\"",
            "    if employee_flag and pass_flag:\n        selected_branch = \"priority\"\n"
            "    elif employee_flag:\n        selected_branch = \"employee\"",
        ),
        runtime_probe=RuntimeProbe(
            module_path="access_branch.py",
            focus_function="choose_access",
            call_source="choose_access(True, True)",
            anchor="if employee_flag:",
            inspect_expressions=("employee_flag", "pass_flag"),
        ),
    ),
    DemoScenario(
        task_id="curated-mutation-alias-004",
        hypothesis_id="demo-mutation-alias-004",
        root_cause_statement=(
            "add_label aliases the caller-owned list instead of copying it, so "
            "appending the new label mutates the caller's original collection."
        ),
        localization=LocalizationClaim("labels.py", "add_label"),
        reference_repair=ReferenceRepair(
            "labels.py",
            "    caller_labels = labels\n    working_labels = caller_labels\n"
            "    shared_identity = id(caller_labels) == id(working_labels)\n"
            "    if not shared_identity:\n        raise RuntimeError(\"unexpected collection identity\")",
            "    caller_labels = labels\n    working_labels = list(caller_labels)",
        ),
        runtime_probe=RuntimeProbe(
            module_path="labels.py",
            focus_function="add_label",
            call_source="add_label([\"base\"], \"new\")",
            anchor="working_labels.append(label)",
            inspect_expressions=("shared_identity", "caller_labels", "working_labels"),
        ),
    ),
    DemoScenario(
        task_id="curated-caller-callee-005",
        hypothesis_id="demo-caller-callee-005",
        root_cause_statement=(
            "format_price forwards a dollar amount to a callee that documents "
            "and requires cents, so the representation contract is broken at "
            "the caller/callee boundary."
        ),
        localization=LocalizationClaim("price.py", "format_price"),
        reference_repair=ReferenceRepair(
            "price.py",
            "    callee_input = caller_amount\n    return _format_price(callee_input)",
            "    callee_input = caller_amount * 100 if caller_representation == \"dollars\" else caller_amount\n"
            "    return _format_price(callee_input)",
        ),
        runtime_probe=RuntimeProbe(
            module_path="price.py",
            focus_function="format_price",
            call_source="format_price(12, \"dollars\")",
            anchor="callee_input = caller_amount",
            inspect_expressions=("caller_amount", "caller_representation"),
        ),
    ),
    DemoScenario(
        task_id="pdb-required-boundary-006",
        hypothesis_id="proof-boundary-006",
        root_cause_statement=(
            "tail_window applies an unnecessary boundary adjustment when the "
            "requested window covers the complete sequence."
        ),
        localization=LocalizationClaim("window_tail.py", "tail_window"),
        reference_repair=ReferenceRepair(
            "window_tail.py",
            "selected = values[start_index:end_index - (1 if requested_size == item_count else 0)]",
            "selected = values[start_index:end_index]",
        ),
        runtime_probe=RuntimeProbe(
            module_path="window_tail.py",
            focus_function="tail_window",
            call_source="tail_window([10, 20, 30, 40], 4)",
            anchor="selected = values[",
            inspect_expressions=("requested_size",),
            exact_public_reproduction=True,
            breakpoint_line=9,
        ),
    ),
)


_BY_ID: Mapping[str, DemoScenario] = MappingProxyType(
    {scenario.task_id: scenario for scenario in _SCENARIOS}
)


def scenario_ids() -> tuple[str, ...]:
    """Return every catalogued task id in stable, sorted order."""

    return tuple(sorted(_BY_ID))


def scenario_for(task_id: str) -> DemoScenario:
    """Return the fixed offline model output for one curated task."""

    if type(task_id) is not str:
        raise DemoCatalogError("task_id must be a string")
    try:
        return _BY_ID[task_id]
    except KeyError:
        raise DemoCatalogError(f"no demonstration scenario for task {task_id!r}") from None


def reference_repair_snippets(task_id: str) -> tuple[str, str, str]:
    """Return ``(target_path, old_snippet, new_snippet)`` for a curated task."""

    repair = scenario_for(task_id).reference_repair
    return repair.target_path, repair.old_snippet, repair.new_snippet


def build_reference_patch(source_text: str, repair: ReferenceRepair) -> str:
    """Render the reference repair as a unified diff over ``source_text``.

    The diff is derived from the exact bytes handed in, so a drifted fixture
    produces a hard error instead of a patch that cannot apply.
    """

    if type(source_text) is not str:
        raise DemoCatalogError("source_text must be a string")
    if type(repair) is not ReferenceRepair:
        raise DemoCatalogError("repair must be a ReferenceRepair")
    occurrences = source_text.count(repair.old_snippet)
    if occurrences != 1:
        raise DemoCatalogError(
            f"reference repair anchor for {repair.target_path!r} matched "
            f"{occurrences} times; exactly one match is required"
        )
    patched = source_text.replace(repair.old_snippet, repair.new_snippet, 1)
    diff = "".join(
        difflib.unified_diff(
            source_text.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=f"a/{repair.target_path}",
            tofile=f"b/{repair.target_path}",
            lineterm="\n",
        )
    )
    if not diff:
        raise DemoCatalogError("reference repair produced an empty diff")
    return diff


def _own_statement_lines(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[int]:
    """Return the statement start lines that belong to this function's own scope.

    Nested functions, async functions and classes are deliberately not
    descended into: a breakpoint inside a nested scope would pause in a frame
    that is not the declared focus function.
    """

    lines: set[int] = set()

    def visit(statement: ast.stmt) -> None:
        lines.add(statement.lineno)
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        for child in ast.iter_child_nodes(statement):
            if isinstance(child, ast.stmt):
                visit(child)

    for statement in function.body:
        visit(statement)
    return lines


def resolve_probe_breakpoint(source_text: str, probe: RuntimeProbe) -> int:
    """Resolve the probe anchor to a one-based line inside the focus function.

    Resolution is deliberately strict, because a breakpoint that lands on the
    wrong line degrades silently into "no runtime evidence":

    * only statement start lines are considered, so a continuation line of a
      multi-line statement -- on which ``bdb`` would never break -- is rejected;
    * lines belonging to a nested function or class are excluded, so the pause
      always happens in the declared focus frame;
    * the anchor must be a prefix of the stripped line, so a substring that also
      occurs inside a longer expression cannot match;
    * exactly one match is required.
    """

    if type(source_text) is not str:
        raise DemoCatalogError("source_text must be a string")
    if type(probe) is not RuntimeProbe:
        raise DemoCatalogError("probe must be a RuntimeProbe")
    try:
        tree = ast.parse(source_text, filename=probe.module_path)
    except SyntaxError as exc:
        raise DemoCatalogError(
            f"probe module {probe.module_path!r} does not parse: {exc}"
        ) from exc
    target: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == probe.focus_function:
            if target is not None:
                raise DemoCatalogError(
                    f"probe function {probe.focus_function!r} is ambiguous in "
                    f"{probe.module_path!r}"
                )
            target = node
    if target is None:
        raise DemoCatalogError(
            f"probe function {probe.focus_function!r} not found in {probe.module_path!r}"
        )
    lines = source_text.splitlines()
    matches = [
        number
        for number in sorted(_own_statement_lines(target))
        if 1 <= number <= len(lines) and lines[number - 1].strip().startswith(probe.anchor)
    ]
    if len(matches) != 1:
        raise DemoCatalogError(
            f"probe anchor {probe.anchor!r} matched {len(matches)} own statement "
            f"lines inside {probe.focus_function!r}; exactly one match is required"
        )
    return matches[0]


def probe_driver_source(probe: RuntimeProbe) -> str:
    """Return the module-level driver appended to the disposable probe copy."""

    if type(probe) is not RuntimeProbe:
        raise DemoCatalogError("probe must be a RuntimeProbe")
    return (
        f"\n\ndef {PROBE_DRIVER_FUNCTION}():\n"
        f"    return {probe.call_source}\n\n\n"
        f"{PROBE_DRIVER_FUNCTION}()\n"
    )


def exact_pytest_driver_source(probe: RuntimeProbe, pytest_args: tuple[str, ...]) -> str:
    """Return a guarded driver that runs the declared pytest node in-place."""

    if type(probe) is not RuntimeProbe or not probe.exact_public_reproduction:
        raise DemoCatalogError("exact pytest driver requires an exact probe")
    if type(pytest_args) is not tuple or not pytest_args or any(
        type(item) is not str or not item for item in pytest_args
    ):
        raise DemoCatalogError("pytest_args must be a non-empty string tuple")
    return (
        f"\n\ndef {PROBE_DRIVER_FUNCTION}():\n"
        "    import os\n"
        "    import sys\n"
        "    import threading\n"
        "    sys.path.insert(0, os.path.dirname(__file__))\n"
        "    os.environ[\"PYTEST_DISABLE_PLUGIN_AUTOLOAD\"] = \"1\"\n"
        "    # Keep pytest's fd capture off so it cannot redirect the bounded PDB protocol.\n"
        "    os.environ[\"PYTEST_ADDOPTS\"] = \"-s\"\n"
        "    saved_trace = sys.gettrace()\n"
        "    def restore_pdb_trace():\n"
        "        if saved_trace is not None:\n"
        "            sys.settrace(saved_trace)\n"
        "            threading.settrace(saved_trace)\n"
        "    import pytest\n"
        "    import functools\n"
        "    import importlib\n"
        f"    target_module = importlib.import_module({repr(probe.module_path[:-3].replace('/', '.'))})\n"
        f"    original_function = getattr(target_module, {probe.focus_function!r})\n"
        "    @functools.wraps(original_function)\n"
        "    def traced_production_call(*args, **kwargs):\n"
        "        restore_pdb_trace()\n"
        "        return original_function(*args, **kwargs)\n"
        f"    setattr(target_module, {probe.focus_function!r}, traced_production_call)\n"
        "    class _RestorePdbTrace:\n"
        "        def pytest_runtest_call(self, item):\n"
        "            restore_pdb_trace()\n"
        "        def pytest_collection_modifyitems(self, session, config, items):\n"
        "            for item in items:\n"
        f"                module_function = getattr(item.module, {probe.focus_function!r}, None)\n"
        "                if module_function is not None:\n"
        "                    @functools.wraps(module_function)\n"
        "                    def traced_test_binding(*args, __original=module_function, **kwargs):\n"
        "                        restore_pdb_trace()\n"
        "                        return __original(*args, **kwargs)\n"
        f"                    setattr(item.module, {probe.focus_function!r}, traced_test_binding)\n"
        "                original = item.obj\n"
        "                @functools.wraps(original)\n"
        "                def wrapped(*args, __original=original, **kwargs):\n"
        "                    restore_pdb_trace()\n"
        "                    return __original(*args, **kwargs)\n"
        "                item.obj = wrapped\n"
        f"    return pytest.main({repr(list(pytest_args))}, plugins=[_RestorePdbTrace()])\n\n\n"
        f"if __name__ == \"__main__\":\n    {PROBE_DRIVER_FUNCTION}()\n"
    )


__all__ = [
    "MAX_PROBE_EXPRESSIONS",
    "PROBE_DRIVER_FUNCTION",
    "DemoCatalogError",
    "DemoScenario",
    "LocalizationClaim",
    "ReferenceRepair",
    "RuntimeProbe",
    "build_reference_patch",
    "exact_pytest_driver_source",
    "probe_driver_source",
    "reference_repair_snippets",
    "resolve_probe_breakpoint",
    "scenario_for",
    "scenario_ids",
]
