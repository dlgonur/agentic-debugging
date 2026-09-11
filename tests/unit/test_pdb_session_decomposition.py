"""Structural ownership tests for the PDB session decomposition.

Enforces module boundaries introduced to bring ``pdb_session.py`` below
1,000 physical lines without duplicating state, validation or transport
authority.
"""

from __future__ import annotations

import ast
import pathlib


RUNTIME = pathlib.Path(__file__).resolve().parents[2] / "agentic_debugger" / "runtime"

SESSION = RUNTIME / "pdb_session.py"
NEW_MODULES = [
    RUNTIME / "pdb_session_limits.py",
    RUNTIME / "pdb_session_diagnostics.py",
    RUNTIME / "pdb_session_validation.py",
    RUNTIME / "pdb_session_inspection.py",
    RUNTIME / "pdb_session_outcomes.py",
    RUNTIME / "pdb_session_transport.py",
    RUNTIME / "pdb_session_control.py",
]


def _physical_lines(path: pathlib.Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_session_and_new_modules_below_1000_physical_lines():
    assert _physical_lines(SESSION) < 1000, (
        f"pdb_session.py has {_physical_lines(SESSION)} lines, must be <1000"
    )
    for mod in NEW_MODULES:
        assert mod.exists(), f"missing decomposition module {mod.name}"
        assert _physical_lines(mod) < 1000, (
            f"{mod.name} has {_physical_lines(mod)} lines, must be <1000"
        )


def test_no_mixin_bases_no_method_shadowing():
    from agentic_debugger.runtime.pdb_session import PdbSession

    # Composition-only design: no mixins, so no accidental shadowing
    # (candidate-60 defect class).
    assert PdbSession.__bases__ == (object,)
    methods = [
        name for name, value in PdbSession.__dict__.items()
        if callable(value) and not name.startswith("__")
    ]
    assert len(methods) == len(set(methods))
    # Moved authorities must not linger as duplicate implementations.
    for stale in (
        "_validate_breakpoints",
        "_validate_argv",
        "_validate_script_and_read",
        "_read_validated_workspace_script",
        "_read_bounded_fd",
        "_check_utf8_strict",
        "_validate_run_result",
        "_validate_stack_summary_result",
        "_validate_frame_result",
        "_validate_locals_result",
        "_validate_safe_eval_result",
        "_validate_value_summary",
        "_validate_persistent_outcome_result",
        "_validate_status_result",
        "_validate_terminate_result",
        "_start_reader_threads",
        "_schedule_overflow_cleanup",
        "_automatic_overflow_cleanup",
        "_wait_for_reader_cleanup",
        "_shutdown_worker_if_ready",
        "_terminate_and_cleanup",
        "_finalize_after_stop",
        "_close_proc_pipes",
        "_perform_inspection",
        "_resume_paused_target",
    ):
        assert stale not in PdbSession.__dict__, (
            f"{stale} must live in its authoritative helper module, "
            "not as a duplicate on PdbSession"
        )


def test_single_mutable_authority_markers():
    session_src = SESSION.read_text(encoding="utf-8")
    # Session owns the mutable state fields exactly once.
    for marker in (
        "self._proc",
        "self._next_request_id",
        "self._request_lock",
        "self._target_lifecycle_state",
        "self._diag_accum",
        "self._response_queue",
    ):
        assert marker in session_src
    # Helpers must not re-own subprocess/request state.
    for mod in NEW_MODULES:
        if mod.name in (
            "pdb_session_limits.py",
            "pdb_session_diagnostics.py",
            "pdb_session_validation.py",
            "pdb_session_inspection.py",
            "pdb_session_outcomes.py",
        ):
            src = mod.read_text(encoding="utf-8")
            assert "self._proc" not in src, f"{mod.name} must not own _proc"
            assert "self._next_request_id" not in src, (
                f"{mod.name} must not duplicate request-ID authority"
            )


def test_contained_hooks_called_through_self():
    session_src = SESSION.read_text(encoding="utf-8")
    for hook in (
        "self._get_worker_argv()",
        "self._worker_cwd()",
        "self._worker_env()",
        "self._expected_worker_pid()",
        "self._allocate_request_id()",
        "self._send_and_receive(",
    ):
        assert hook in session_src, (
            f"PdbSession must call {hook} through self so "
            "ContainedPdbSession overrides stay effective"
        )
    for mod in (
        RUNTIME / "pdb_session_transport.py",
        RUNTIME / "pdb_session_control.py",
    ):
        tree = ast.parse(mod.read_text(encoding="utf-8"))
        runtime_imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                runtime_imports.append(node.module)
        # TYPE_CHECKING-only imports are allowed; runtime bindings are not.
        # Detect a runtime (non-TYPE_CHECKING) import of pdb_session.
        lines = mod.read_text(encoding="utf-8").splitlines()
        in_type_checking = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("if TYPE_CHECKING"):
                in_type_checking = True
                continue
            if in_type_checking:
                if line and not line[0].isspace() and stripped:
                    in_type_checking = False
                else:
                    continue
            if "from agentic_debugger.runtime.pdb_session import" in line:
                raise AssertionError(
                    f"{mod.name}:{i+1} must not bind base-class helpers "
                    "directly at runtime (would bypass ContainedPdbSession "
                    "overrides / create a cycle)"
                )


def test_dependency_graph_is_acyclic():
    internal = {p.stem for p in [SESSION, *NEW_MODULES]}
    edges: dict[str, set[str]] = {name: set() for name in internal}

    def _module_name(path: pathlib.Path) -> str:
        return path.stem

    def _runtime_deps(path: pathlib.Path) -> set[str]:
        """Runtime imports only (TYPE_CHECKING blocks excluded)."""
        lines = path.read_text(encoding="utf-8").splitlines()
        # blank out TYPE_CHECKING blocks so they don't count as runtime deps
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
        tree = ast.parse("\n".join(cleaned))
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
                if mod.startswith("agentic_debugger.runtime."):
                    leaf = mod.rsplit(".", 1)[-1]
                    if leaf in internal and leaf != _module_name(path):
                        deps.add(leaf)
        return deps

    for path in [SESSION, *NEW_MODULES]:
        edges[_module_name(path)] = _runtime_deps(path)

    # No validation/result duplication: inspection/outcomes depend on
    # validation primitives; validation depends on neither.
    assert edges["pdb_session_validation"].isdisjoint(
        {"pdb_session_inspection", "pdb_session_outcomes",
         "pdb_session_control", "pdb_session_transport", "pdb_session"}
    )
    assert edges["pdb_session_inspection"].isdisjoint(
        {"pdb_session_outcomes", "pdb_session_control",
         "pdb_session_transport", "pdb_session"}
    )
    assert edges["pdb_session_transport"].isdisjoint(
        {"pdb_session_validation", "pdb_session_inspection",
         "pdb_session_outcomes", "pdb_session_control", "pdb_session"}
    )

    # Generic cycle check over the internal graph.
    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(node: str, stack: list[str]) -> None:
        if node in visited:
            return
        assert node not in visiting, (
            f"circular pdb_session dependency: {' -> '.join([*stack, node])}"
        )
        visiting.add(node)
        for dep in sorted(edges[node]):
            _visit(dep, [*stack, node])
        visiting.remove(node)
        visited.add(node)

    for name in sorted(edges):
        _visit(name, [])


def test_public_contract_preserved():
    from agentic_debugger.runtime.pdb_session import PdbSession, PdbSessionState
    from agentic_debugger.runtime import PdbSession as R1, PdbSessionState as R2

    assert PdbSession is R1
    assert PdbSessionState is R2
    for op in (
        "start", "ping", "run_to_breakpoint", "run_post_mortem",
        "start_paused_target", "get_target_status", "get_stack_summary",
        "get_frame", "get_frame_locals", "safe_eval_expression",
        "continue_paused_target", "step_paused_target",
        "next_paused_target", "terminate_paused_target", "stop",
        "diagnostics",
    ):
        assert hasattr(PdbSession, op), f"missing public op {op}"
    # Single-source compatibility re-exports (no duplicate definitions).
    import agentic_debugger.runtime.pdb_session as sess
    import agentic_debugger.runtime.pdb_session_limits as lim
    import agentic_debugger.runtime.pdb_session_diagnostics as diag

    assert sess._DEFAULT_STARTUP_TIMEOUT == lim._DEFAULT_STARTUP_TIMEOUT
    assert sess._MAX_TARGET_SOURCE_BYTES == lim._MAX_TARGET_SOURCE_BYTES
    assert sess._TRUNCATION_MARKER == lim._TRUNCATION_MARKER
    assert sess._BoundedDiagnostics is diag._BoundedDiagnostics
