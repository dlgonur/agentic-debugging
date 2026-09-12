"""Structural ownership tests for the controller post-patch decomposition.

Enforces module boundaries introduced to bring ``controller.py`` below
1,000 physical lines without creating a second controller/state machine
or duplicating budget, dispatch, or observation authority.
"""

from __future__ import annotations

import ast
import pathlib


AGENT = pathlib.Path(__file__).resolve().parents[2] / "agentic_debugger" / "agent"

CONTROLLER = AGENT / "controller.py"
HELPER = AGENT / "controller_post_patch.py"
CONTRACTS = AGENT / "controller_contracts.py"

# Runtime step helpers with a single definition in controller_contracts,
# shared by the model loop (controller.py) and the deterministic
# post-patch helper (controller_post_patch.py).
SHARED_HELPERS = [
    "_remaining",
    "_consume_direct",
    "_failure_state",
    "_canonical_directive",
    "_assert_dispatch_identity",
    "_canonical_observation",
]

# Subset the post-patch helper reuses directly (directive
# canonicalization stays on the model-owned request path, so the helper
# never imports it — but still must not redefine it).
HELPER_SHARED_HELPERS = [
    "_remaining",
    "_consume_direct",
    "_failure_state",
    "_assert_dispatch_identity",
    "_canonical_observation",
]

SHARED_CONSTANTS = [
    "_BUDGET_FIELDS",
    "_NO_HANDLER_REASONS",
]


def _physical_lines(path: pathlib.Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _module_defs(path: pathlib.Path) -> tuple[set[str], set[str]]:
    """Return (function names, assigned top-level names) for a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assigned: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned.add(node.target.id)
    return functions, assigned


def test_controller_and_helper_below_1000_physical_lines():
    assert _physical_lines(CONTROLLER) < 1000, (
        f"controller.py has {_physical_lines(CONTROLLER)} lines, must be <1000"
    )
    assert HELPER.exists(), "missing decomposition module controller_post_patch.py"
    assert _physical_lines(HELPER) < 1000, (
        f"controller_post_patch.py has {_physical_lines(HELPER)} lines, must be <1000"
    )
    assert _physical_lines(CONTRACTS) < 1000, (
        f"controller_contracts.py has {_physical_lines(CONTRACTS)} lines, must be <1000"
    )


def test_no_second_controller_or_state_machine_in_helper():
    helper_src = HELPER.read_text(encoding="utf-8")
    tree = ast.parse(helper_src)
    classes = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }
    # Only the explicit runtime/context/callback/outcome holders.
    assert classes == {
        "PostPatchRuntime",
        "PostPatchContext",
        "PostPatchCallbacks",
        "PostPatchOutcome",
    }, f"helper must not define controller/state-machine classes: {sorted(classes)}"
    controller_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "agentic_debugger.agent.controller"
    ]
    assert controller_imports == [], (
        "post-patch helper must not import controller.py"
    )
    assert "ControllerRunResult(" not in helper_src
    assert "def run(" not in helper_src


def test_no_duplicated_step_helpers():
    import agentic_debugger.agent.controller_contracts as contracts
    import agentic_debugger.agent.controller_post_patch as helper
    import agentic_debugger.agent.controller as controller

    for name in SHARED_HELPERS:
        assert callable(getattr(contracts, name)), (
            f"controller_contracts must define shared helper {name}"
        )
        # The facade reuses the single definition (identical objects).
        assert getattr(controller, name) is getattr(contracts, name), (
            f"controller.{name} must be the controller_contracts single definition"
        )
    for name in HELPER_SHARED_HELPERS:
        assert getattr(helper, name) is getattr(contracts, name), (
            f"controller_post_patch.{name} must be the controller_contracts single definition"
        )
    controller_defs, controller_assigned = _module_defs(CONTROLLER)
    helper_defs, helper_assigned = _module_defs(HELPER)
    for name in SHARED_HELPERS:
        assert name not in controller_defs, (
            f"{name} must live in controller_contracts, not as a duplicate def in controller.py"
        )
        assert name not in helper_defs, (
            f"{name} must live in controller_contracts, not as a duplicate def in controller_post_patch.py"
        )
    for name in SHARED_CONSTANTS:
        assert name not in controller_assigned
        assert name not in helper_assigned


def test_no_eager_controller_cycle_in_helper():
    lines = HELPER.read_text(encoding="utf-8").splitlines()
    cleaned: list[str] = []
    in_tc = False
    tc_indent = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("if TYPE_CHECKING"):
            in_tc = True
            tc_indent = len(line) - len(line.lstrip())
            cleaned.append("")
            continue
        if in_tc:
            if stripped == "":
                cleaned.append("")
                continue
            indent = len(line) - len(line.lstrip())
            if indent > tc_indent:
                cleaned.append("")
                continue
            in_tc = False
        cleaned.append(line)
    for i, line in enumerate(cleaned, 1):
        if "from agentic_debugger.agent.controller import" in line:
            raise AssertionError(
                f"controller_post_patch.py:{i} must not eagerly import the controller facade "
                "(would create a cycle; use contracts/policy/results context instead)"
            )


def test_single_controller_authority_markers():
    controller_src = CONTROLLER.read_text(encoding="utf-8")
    helper_src = HELPER.read_text(encoding="utf-8")
    # The controller retains the run/transition implementation and is the
    # only producer of terminal run results.
    assert "class DeterministicController" in controller_src
    assert "def run(" in controller_src
    assert "ControllerRunResult(" in controller_src
    # The helper returns an outcome for the controller to translate; it
    # never constructs a run result and owns no model loop ordinals.
    assert "PostPatchOutcome(" in helper_src
    assert "def run_post_patch_validation(" in helper_src
