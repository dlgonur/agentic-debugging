"""Post-mortem PDB entry tests (TODO 6.1.3).

These tests exercise the offline-capable post-mortem PDB path: a real
``PdbSession`` over a real worker subprocess runs a Python script and
captures structured traceback evidence when the script terminates with an
unhandled exception.  No provider or network access occurs.  The tests cover
the acceptance contract: eligible failing Python enters post-mortem; a
successful exit does not; SystemExit(0) and SystemExit(nonzero) report exited
without post-mortem evidence; a non-Python/unsupported command fails closed;
tracebackless failures fail closed without fabricated PDB evidence; the
evidence is bound to the script and session identity; bounding limits are
enforced deterministically with marker-inclusive UTF-8 byte bounds; bounded
traceback traversal and bounded local inspection are proven by
instrumentation; exception and value summarization never invoke target code;
cleanup occurs; and no provider/network attempt is made.

Every test function has a unique name; there are no shadowed definitions.

The response is deterministic, bounded, JSON-serializable protocol evidence
suitable for later event persistence.  It is not currently integrated into the
accepted event/replay trajectory path; the determinism tests verify that two
independent runs produce identical evidence, not that a replay path exists.
"""
import io
import json
import linecache
import shutil
import socket as _socket
import subprocess as _subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agentic_debugger.runtime import pdb_worker as worker_module
from agentic_debugger.runtime.exceptions import PdbSessionStateError
from agentic_debugger.runtime.pdb_protocol import (
    MAX_LINE_LENGTH,
    PROTOCOL_VERSION,
    PdbResponse,
    deserialize_response,
    serialize_response,
)
from agentic_debugger.runtime.pdb_session import PdbSession, PdbSessionState
from agentic_debugger.runtime.pdb_worker import (
    PdbWorker,
    _MAX_BYTES_PREVIEW,
    _POST_MORTEM_EXC_ARGS_MAX_SCAN,
    _POST_MORTEM_LOCALS_SCAN_CEILING,
    _POST_MORTEM_MAX_EXC_MESSAGE_UTF8,
    _POST_MORTEM_MAX_FRAMES,
    _POST_MORTEM_MAX_LOCALS,
    _POST_MORTEM_MAX_TEXT_UTF8,
    _POST_MORTEM_TRUNCATION_MARKER,
    _bounded_traceback_frames,
    _capture_post_mortem_evidence_pure,
    _collect_bounded_locals,
    _has_traceback,
    _post_mortem_bounded_text,
    _post_mortem_missing_traceback_response,
    _safe_exception_error_message,
    _safe_exception_message,
    _safe_exception_type_name,
    _safe_local_summary,
)
from agentic_debugger.runtime.workspace import TaskWorkspace


def _make_workspace(script_name: str, script_text: str):
    root = Path(tempfile.mkdtemp())
    (root / script_name).write_text(script_text, encoding="utf-8")
    return root


@pytest.fixture
def failing_script_workspace():
    root = _make_workspace(
        "failing_target.py",
        "def broken(n):\n"
        "    total = n * 2\n"
        "    raise ValueError('off-by-one trigger')\n"
        "broken(21)\n",
    )
    try:
        with TaskWorkspace(str(root)) as ws:
            yield ws
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


@pytest.fixture
def success_script_workspace():
    root = _make_workspace(
        "success_target.py",
        "def good(n):\n"
        "    return n + 1\n"
        "result = good(41)\n",
    )
    try:
        with TaskWorkspace(str(root)) as ws:
            yield ws
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


def _run_script(workspace, script_name):
    with PdbSession(workspace) as session:
        return session.run_post_mortem(script_name)


# ---- injected traceback/frame fixtures ------------------------------------


class _FakeCode:
    def __init__(self, filename: str, name: str):
        self.co_filename = filename
        self.co_name = name


class _FakeFrame:
    def __init__(self, filename: str, name: str, lineno: int,
                 locals_: dict | None = None):
        self.f_code = _FakeCode(filename, name)
        self.f_lineno = lineno
        self.f_locals = locals_


class _FakeTb:
    def __init__(self, frame, next_node=None):
        self.tb_frame = frame
        self.tb_next = next_node


class _BrokenTb:
    """A traceback node whose expected fields are inaccessible."""

    @property
    def tb_frame(self):
        raise AttributeError("injected: no tb_frame")

    @property
    def tb_next(self):
        raise AttributeError("injected: no tb_next")


class _RaisingNextTb:
    def __init__(self, frame):
        self.tb_frame = frame

    @property
    def tb_next(self):
        raise AttributeError("injected: tb_next raises")


class _EmptyFrame:
    """A frame object without the expected code metadata."""


def _make_chain(length: int, file_prefix: str = "file",
                name_prefix: str = "fn"):
    """Build a deterministic fake traceback chain of ``length`` nodes;
    node 0 is the outermost node, node length-1 the innermost."""
    node = None
    for index in range(length - 1, -1, -1):
        node = _FakeTb(
            _FakeFrame(
                f"{file_prefix}_{index}.py", f"{name_prefix}_{index}",
                index + 1, locals_={},
            ),
            node,
        )
    return node


def _make_deep_traceback(depth: int):
    def recurse(remaining: int):
        if remaining <= 0:
            raise ValueError("deep failure")
        recurse(remaining - 1)

    try:
        recurse(depth)
    except ValueError:
        exc_type, exc_value, tb = sys.exc_info()
    return exc_type, exc_value, tb


# ---- session behavior ------------------------------------------------------


def test_session_eligible_failing_python_enters_post_mortem(failing_script_workspace):
    response = _run_script(failing_script_workspace, "failing_target.py")
    assert response.success is True
    result = response.result
    assert result["status"] == "post_mortem"
    assert result["post_mortem"] is True
    assert result["script"] == "failing_target.py"
    exc = result["exception"]
    assert exc["type"] == "ValueError"
    assert "off-by-one trigger" in exc["message"]
    assert exc["repr"] == "ValueError: Target raised ValueError: off-by-one trigger"
    frames = result["traceback_frames"]
    assert isinstance(frames, list) and len(frames) >= 1
    assert result["frames_truncated"] is False
    inner = result["innermost_frame"]
    assert inner["function"] == "broken"
    assert inner["line"] >= 1
    local_names = inner["local_names"]
    assert "n" in local_names
    assert "total" in local_names
    local_values = {item["name"]: item for item in inner["local_values"]}
    assert local_values["n"]["summary"]["kind"] == "int"
    assert local_values["n"]["summary"]["value"] == 21
    assert local_values["total"]["summary"]["kind"] == "int"
    assert local_values["total"]["summary"]["value"] == 42


def test_session_successful_command_does_not_enter_post_mortem(success_script_workspace):
    response = _run_script(success_script_workspace, "success_target.py")
    assert response.success is True
    result = response.result
    assert result["status"] == "exited"
    assert result["post_mortem"] is False
    assert result["exit_code"] == 0
    assert "exception" not in result
    assert "traceback_frames" not in result
    assert "innermost_frame" not in result


def test_session_non_python_script_rejected(failing_script_workspace):
    with PdbSession(failing_script_workspace) as session:
        with pytest.raises(Exception):
            session.run_post_mortem("not_a_script.txt")
    assert session.state in (PdbSessionState.READY, PdbSessionState.STOPPED)


def test_session_one_execution_per_session_invariant(failing_script_workspace):
    with PdbSession(failing_script_workspace) as session:
        first = session.run_post_mortem("failing_target.py")
        assert first.success is True
        with pytest.raises(PdbSessionStateError):
            session.run_post_mortem("failing_target.py")


def test_session_with_traceback_none_raise_site_still_has_real_frames():
    # ``raise exc.with_traceback(None)`` still attaches a traceback at the
    # raise site, so this is a valid post-mortem with real frame evidence;
    # the genuinely tracebackless path is covered by the worker fail-closed
    # tests below.
    root = _make_workspace(
        "malformed_target.py",
        "import sys\n"
        "exc = ValueError('no traceback')\n"
        "raise exc.with_traceback(None)\n",
    )
    try:
        with TaskWorkspace(str(root)) as ws:
            with PdbSession(ws) as session:
                response = session.run_post_mortem("malformed_target.py")
        assert response.success is True
        assert response.result["status"] == "post_mortem"
        frames = response.result["traceback_frames"]
        assert len(frames) >= 1
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


# ---- SystemExit behavior ---------------------------------------------------


def test_session_system_exit_zero_is_exited_no_post_mortem():
    root = _make_workspace("sysexit_zero.py", "raise SystemExit(0)\n")
    try:
        with TaskWorkspace(str(root)) as ws:
            with PdbSession(ws) as session:
                response = session.run_post_mortem("sysexit_zero.py")
        assert response.success is True
        assert response.result["status"] == "exited"
        assert response.result["post_mortem"] is False
        assert response.result["exit_code"] == 0
        assert "exception" not in response.result
        assert "traceback_frames" not in response.result
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


def test_session_system_exit_nonzero_is_exited_with_exit_code_no_post_mortem():
    root = _make_workspace("sysexit_nonzero.py", "raise SystemExit(7)\n")
    try:
        with TaskWorkspace(str(root)) as ws:
            with PdbSession(ws) as session:
                response = session.run_post_mortem("sysexit_nonzero.py")
        assert response.success is True
        assert response.result["status"] == "exited"
        assert response.result["post_mortem"] is False
        assert response.result["exit_code"] == 7
        assert "exception" not in response.result
        assert "traceback_frames" not in response.result
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


# ---- traceback bounds ------------------------------------------------------


def test_bounded_traceback_chain_shorter_than_limit_reports_all_frames():
    chain = _make_chain(5)
    frames, innermost, truncated, terminated, error = _bounded_traceback_frames(chain)
    assert [f["function"] for f in frames] == ["fn_0", "fn_1", "fn_2", "fn_3", "fn_4"]
    assert len(frames) == 5
    assert truncated is False
    assert terminated is False
    assert error is None
    assert innermost is not None


def test_bounded_traceback_chain_exactly_at_limit_untouched():
    chain = _make_chain(_POST_MORTEM_MAX_FRAMES)
    frames, innermost, truncated, terminated, error = _bounded_traceback_frames(chain)
    assert len(frames) == _POST_MORTEM_MAX_FRAMES
    assert truncated is False
    assert terminated is False
    assert error is None


def test_bounded_traceback_chain_over_limit_keeps_deterministic_innermost_tail():
    chain = _make_chain(30)
    frames, innermost, truncated, terminated, error = _bounded_traceback_frames(chain)
    assert len(frames) == _POST_MORTEM_MAX_FRAMES
    assert truncated is True
    assert terminated is False
    assert error is None
    # Deterministic tail: the innermost 16 nodes, outermost-to-innermost order.
    assert [f["function"] for f in frames] == [
        f"fn_{index}" for index in range(14, 30)
    ]
    assert frames[0]["file"] == "file_14.py"
    assert frames[-1]["file"] == "file_29.py"


def test_bounded_traceback_deep_real_chain_through_evidence():
    exc_type, exc_value, tb = _make_deep_traceback(_POST_MORTEM_MAX_FRAMES + 10)
    evidence = _capture_post_mortem_evidence_pure(
        "deep.py", exc_type, exc_value, tb, _safe_exception_error_message
    )
    assert len(evidence["traceback_frames"]) == _POST_MORTEM_MAX_FRAMES
    assert evidence["frames_truncated"] is True
    assert "traceback_error" not in evidence
    json.dumps(evidence, allow_nan=False)


def test_bounded_traceback_injected_self_cycle_terminates_fail_closed():
    node = _FakeTb(_FakeFrame("cycle.py", "cycle", 1, locals_={}))
    node.tb_next = node
    frames, innermost, truncated, terminated, error = _bounded_traceback_frames(node)
    assert len(frames) == _POST_MORTEM_MAX_FRAMES
    assert truncated is True
    assert terminated is True
    assert error is not None and "ceiling" in error


def test_bounded_traceback_malformed_node_fails_closed():
    chain = _FakeTb(_EmptyFrame())
    frames, innermost, truncated, terminated, error = _bounded_traceback_frames(chain)
    assert frames == []
    assert innermost is None
    assert truncated is True
    assert error is not None and "malformed" in error


def test_bounded_traceback_broken_node_fails_closed():
    chain = _BrokenTb()
    frames, innermost, truncated, terminated, error = _bounded_traceback_frames(chain)
    assert frames == []
    assert innermost is None
    assert error is not None


def test_bounded_traceback_raising_next_fails_closed():
    chain = _RaisingNextTb(_FakeFrame("a.py", "a", 1, locals_={}))
    frames, innermost, truncated, terminated, error = _bounded_traceback_frames(chain)
    assert error is not None and "chain is malformed" in error
    assert len(frames) == 1
    assert truncated is True


def test_bounded_traceback_never_loads_source_lines(monkeypatch):
    def _fail_getline(*args, **kwargs):
        raise AssertionError(
            "linecache.getline must not be called during evidence capture"
        )

    monkeypatch.setattr(linecache, "getline", _fail_getline)
    exc_type, exc_value, tb = _make_deep_traceback(30)
    evidence = _capture_post_mortem_evidence_pure(
        "deep.py", exc_type, exc_value, tb, _safe_exception_error_message
    )
    assert len(evidence["traceback_frames"]) == _POST_MORTEM_MAX_FRAMES


def test_bounded_traceback_long_filename_and_function_metadata():
    long_file = "x" * 3000 + ".py"
    long_func = "f" + "y" * 2500
    chain = _make_chain(2, file_prefix="f", name_prefix="n")
    chain.tb_frame = _FakeFrame(long_file, long_func, 1, locals_={})
    frames, innermost, truncated, terminated, error = _bounded_traceback_frames(chain)
    for frame in frames:
        assert len(frame["file"].encode("utf-8")) <= 512
        assert len(frame["function"].encode("utf-8")) <= 512


def test_bounded_traceback_malformed_evidence_reports_bounded_error():
    chain = _FakeTb(_EmptyFrame())
    evidence = _capture_post_mortem_evidence_pure(
        "bad.py", ValueError, ValueError("x"), chain, _safe_exception_error_message
    )
    assert evidence["traceback_frames"] == []
    assert evidence["innermost_frame"] == {}
    assert evidence["frames_truncated"] is True
    assert "traceback_error" in evidence
    assert len(evidence["traceback_error"].encode("utf-8")) <= _POST_MORTEM_MAX_TEXT_UTF8
    json.dumps(evidence, allow_nan=False)


# ---- local-variable bounds -------------------------------------------------


def test_bounded_locals_large_mapping_is_not_fully_enumerated():
    def _items(mapping):
        for index in range(1000):
            if index > 40:
                raise AssertionError("mapping was fully enumerated")
            yield f"v{index}", index

    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: 1000,
        items_op=_items,
    )
    assert error is None
    assert len(entries) == _POST_MORTEM_MAX_LOCALS
    assert inspected <= _POST_MORTEM_LOCALS_SCAN_CEILING
    assert inspected < 40  # the scan stopped at the acceptance bound
    assert truncated is True


def test_bounded_locals_inspected_never_exceeds_ceiling():
    def _items(mapping):
        for index in range(5000):
            yield f"__dunder_{index}", index

    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: 5000,
        items_op=_items,
    )
    assert error is None
    assert inspected <= _POST_MORTEM_LOCALS_SCAN_CEILING
    assert truncated is True
    assert entries == []


def test_bounded_locals_exactly_accepts_maximum_entries():
    def _items(mapping):
        for index in range(100):
            yield f"v{index}", index

    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: 100,
        items_op=_items,
    )
    assert error is None
    assert len(entries) == _POST_MORTEM_MAX_LOCALS
    # Entries are emitted in deterministic sorted name order.
    assert [name for name, _ in entries] == sorted(
        f"v{index}" for index in range(_POST_MORTEM_MAX_LOCALS)
    )
    assert truncated is True


def test_bounded_locals_deterministic_ordering_across_calls():
    def _items(mapping):
        for index in range(40):
            yield f"v{index}", index

    first, _, _, first_error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: 40,
        items_op=_items,
    )
    second, _, _, second_error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: 40,
        items_op=_items,
    )
    assert first_error is None and second_error is None
    assert first == second
    names = [name for name, _ in first]
    assert names == sorted(names)


def test_bounded_locals_mutation_during_scan_fails_closed():
    state = {"length_calls": 0}

    def _length(mapping):
        state["length_calls"] += 1
        return 2 if state["length_calls"] == 1 else 3

    def _items(mapping):
        yield "a", 1

    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS, length_op=_length, items_op=_items
    )
    assert entries is None
    assert error is not None and "mutated" in error


def test_bounded_locals_iteration_failure_fails_closed():
    def _items(mapping):
        raise RuntimeError("dictionary changed size during iteration")

    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: 0,
        items_op=_items,
    )
    assert entries is None
    assert error is not None


def test_bounded_locals_skips_dunder_and_non_string_keys():
    def _items(mapping):
        yield "__hidden", 1
        yield "b", 2
        yield 42, 3
        yield "a", 4

    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: 4,
        items_op=_items,
    )
    assert error is None
    assert [name for name, _ in entries] == ["a", "b"]
    assert inspected == 4


class _CountingIterator:
    """Instrumented iterator wrapper: counts every ``next()`` invocation
    (including a final StopIteration probe), every successful advance, and
    records the retrieved values so tests can prove the scan never retrieves
    an entry beyond the declared ceiling."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0
        self.successful = 0
        self.retrieved = []

    def __iter__(self):
        return self

    def __next__(self):
        self.calls += 1
        value = next(self._inner)
        self.successful += 1
        self.retrieved.append(value)
        return value


def _counting_items(count: int, dunder: bool = False):
    def _items(mapping):
        for index in range(count):
            yield (f"__dunder_{index}" if dunder else f"v{index}"), index

    return _items


def test_bounded_locals_large_dunder_mapping_advances_exactly_ceiling():
    counting = _CountingIterator(
        _counting_items(5000, dunder=True)(object())
    )
    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: 5000,
        items_op=lambda mapping: counting,
    )
    assert error is None
    # The scan budget is checked before every advance: exactly ceiling
    # successful advances, no ceiling+1 retrieval, no StopIteration probe.
    assert counting.calls == counting.successful == _POST_MORTEM_LOCALS_SCAN_CEILING
    assert inspected == counting.successful
    assert counting.retrieved[-1][0] == f"__dunder_{_POST_MORTEM_LOCALS_SCAN_CEILING - 1}"
    assert truncated is True
    assert entries == []


def test_bounded_locals_mapping_exactly_at_ceiling_has_no_probe():
    counting = _CountingIterator(
        _counting_items(_POST_MORTEM_LOCALS_SCAN_CEILING, dunder=True)(object())
    )
    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: _POST_MORTEM_LOCALS_SCAN_CEILING,
        items_op=lambda mapping: counting,
    )
    assert error is None
    # The exact length proves the scan is complete: no extra advance and no
    # StopIteration probe occur, so calls == successful == inspected.
    assert counting.calls == counting.successful == _POST_MORTEM_LOCALS_SCAN_CEILING
    assert inspected == counting.successful
    assert truncated is False
    assert entries == []


def test_bounded_locals_mapping_one_before_ceiling_matches_exactly():
    count = _POST_MORTEM_LOCALS_SCAN_CEILING - 1
    counting = _CountingIterator(
        _counting_items(count, dunder=True)(object())
    )
    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: count,
        items_op=lambda mapping: counting,
    )
    assert error is None
    assert counting.calls == counting.successful == count
    assert inspected == counting.successful
    assert truncated is False
    assert entries == []


def test_bounded_locals_32_accepted_no_remainder_advances_exactly_32():
    count = _POST_MORTEM_MAX_LOCALS
    counting = _CountingIterator(_counting_items(count)(object()))
    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: count,
        items_op=lambda mapping: counting,
    )
    assert error is None
    assert counting.calls == counting.successful == count
    assert inspected == counting.successful
    assert truncated is False
    assert len(entries) == count
    assert [name for name, _ in entries] == sorted(
        f"v{index}" for index in range(count)
    )


def test_bounded_locals_32_accepted_with_remainder_no_extra_advance():
    counting = _CountingIterator(_counting_items(100)(object()))
    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: 100,
        items_op=lambda mapping: counting,
    )
    assert error is None
    # With the exact mapping length available, the acceptance-bound decision
    # needs no extra advance: inspected == collected == 32 and truncation is
    # reported from the known unseen remainder.
    assert counting.calls == counting.successful == _POST_MORTEM_MAX_LOCALS
    assert inspected == counting.successful
    assert truncated is True
    assert len(entries) == _POST_MORTEM_MAX_LOCALS


def test_bounded_locals_unavailable_length_ceiling_reports_truncated_without_advance():
    counting = _CountingIterator(
        _counting_items(5000, dunder=True)(object())
    )
    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: None,
        items_op=lambda mapping: counting,
    )
    assert error is None
    assert counting.calls == counting.successful == _POST_MORTEM_LOCALS_SCAN_CEILING
    assert inspected == counting.successful
    assert truncated is True
    assert entries == []


def test_bounded_locals_unavailable_length_one_advance_finds_remainder():
    counting = _CountingIterator(_counting_items(33)(object()))
    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: None,
        items_op=lambda mapping: counting,
    )
    assert error is None
    # Without a usable length, one additional advance is required to discover
    # that a 33rd entry exists; it succeeds, so all 33 calls advanced.
    assert counting.calls == counting.successful == 33
    assert inspected == counting.successful
    assert truncated is True
    assert len(entries) == _POST_MORTEM_MAX_LOCALS


def test_bounded_locals_unavailable_length_no_remainder_probes_stop():
    count = _POST_MORTEM_MAX_LOCALS
    counting = _CountingIterator(_counting_items(count)(object()))
    entries, inspected, truncated, error = _collect_bounded_locals(
        object(), _POST_MORTEM_MAX_LOCALS,
        length_op=lambda mapping: None,
        items_op=lambda mapping: counting,
    )
    assert error is None
    # Without a usable length, the single StopIteration probe is the only way
    # to learn that no remainder exists; the probe does not retrieve an entry.
    assert counting.calls == count + 1
    assert counting.successful == count
    assert inspected == counting.successful
    assert truncated is False
    assert len(entries) == count


def test_local_limit_enforced_on_real_frame():
    many_locals_src_lines = ["def f():"]
    for index in range(_POST_MORTEM_MAX_LOCALS + 20):
        many_locals_src_lines.append(f"    v_{index} = {index}")
    many_locals_src_lines.append("    raise ValueError('many locals')")
    many_locals_src = "\n".join(many_locals_src_lines)
    namespace: dict = {}
    exec(compile(many_locals_src, "<many-locals>", "exec"), namespace)
    try:
        namespace["f"]()
    except ValueError:
        exc_type, exc_value, tb = sys.exc_info()
    evidence = _capture_post_mortem_evidence_pure(
        "many.py", exc_type, exc_value, tb, _safe_exception_error_message
    )
    inner = evidence["innermost_frame"]
    assert len(inner["local_values"]) <= _POST_MORTEM_MAX_LOCALS
    assert inner["locals_truncated"] is True
    for item in inner["local_values"]:
        assert "summary" in item
        assert "repr" not in item


def test_real_frame_locals_are_sorted_and_deterministic(failing_script_workspace):
    response = _run_script(failing_script_workspace, "failing_target.py")
    inner = response.result["innermost_frame"]
    names = [item["name"] for item in inner["local_values"]]
    assert names == sorted(names)
    assert "n" in names and "total" in names


# ---- safe value summarization ----------------------------------------------


def test_safe_local_summary_never_invokes_user_repr():
    class Adversarial:
        repr_called = False
        str_called = False

        def __repr__(self):
            Adversarial.repr_called = True
            raise RuntimeError("repr must not be called")

        def __str__(self):
            Adversarial.str_called = True
            raise RuntimeError("str must not be called")

    summary = _safe_local_summary(Adversarial())
    assert summary["kind"] == "object"
    assert summary["value"] is None
    assert Adversarial.repr_called is False
    assert Adversarial.str_called is False


def test_safe_local_summary_adversarial_socket_and_mutation():
    sentinel = {"mutated": False}

    class Adversarial:
        def __repr__(self):
            sentinel["mutated"] = True
            _socket.socket()
            return "should never reach"

        def __str__(self):
            sentinel["mutated"] = True
            return "should never reach"

    summary = _safe_local_summary(Adversarial())
    assert summary["kind"] == "object"
    assert sentinel["mutated"] is False


def test_safe_local_summary_builtins_are_summarized_without_repr():
    assert _safe_local_summary(None)["kind"] == "none"
    assert _safe_local_summary(True)["kind"] == "bool"
    assert _safe_local_summary(42)["kind"] == "int"
    assert _safe_local_summary("hello")["kind"] == "str"
    assert _safe_local_summary([1, 2])["kind"] == "list"
    assert _safe_local_summary({"a": 1})["kind"] == "dict"


def test_safe_local_summary_hostile_value_in_evidence_frame():
    calls = {"repr": 0, "str": 0}

    class Adversarial:
        def __repr__(self):
            calls["repr"] += 1
            raise RuntimeError("repr must not be called")

        def __str__(self):
            calls["str"] += 1
            raise RuntimeError("str must not be called")

    chain = _FakeTb(
        _FakeFrame("hostile.py", "f", 1, locals_={"evil": Adversarial()})
    )
    evidence = _capture_post_mortem_evidence_pure(
        "hostile.py", ValueError, ValueError("x"), chain,
        _safe_exception_error_message,
    )
    inner = evidence["innermost_frame"]
    assert inner["local_values"][0]["summary"]["kind"] == "object"
    assert calls == {"repr": 0, "str": 0}
    json.dumps(evidence, allow_nan=False)


# ---- exception summarization -----------------------------------------------


def test_safe_exception_message_str_raises_is_not_invoked():
    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError("__str__ must not be invoked")

    exc = Hostile("payload")
    message = _safe_exception_message(exc)
    assert "payload" in message


def test_safe_exception_message_str_mutating_sentinel_is_not_invoked():
    sentinel = {"mutated": False}

    class Hostile(Exception):
        def __str__(self):
            sentinel["mutated"] = True
            return "spoofed"

    exc = Hostile("payload")
    message = _safe_exception_message(exc)
    assert sentinel["mutated"] is False
    assert "payload" in message
    assert "spoofed" not in message


def test_safe_exception_message_never_runs_socket_process_file_activity(monkeypatch):
    attempts = {"socket": 0, "popen": 0, "open": 0}

    def _no_socket(*args, **kwargs):
        attempts["socket"] += 1
        raise AssertionError("socket must not be opened during exception summarization")

    def _no_popen(*args, **kwargs):
        attempts["popen"] += 1
        raise AssertionError("subprocess must not be launched during exception summarization")

    def _no_open(*args, **kwargs):
        attempts["open"] += 1
        raise AssertionError("files must not be opened during exception summarization")

    monkeypatch.setattr(_socket, "socket", _no_socket)
    monkeypatch.setattr(_subprocess, "Popen", _no_popen)
    monkeypatch.setattr("builtins.open", _no_open)

    class Hostile(Exception):
        def __str__(self):
            _socket.socket()
            _subprocess.Popen(["cmd"])
            open("evil.txt", "w")
            return "spoofed"

    exc = Hostile("payload")
    message = _safe_exception_message(exc)
    assert attempts == {"socket": 0, "popen": 0, "open": 0}
    assert "payload" in message


def test_safe_exception_message_hostile_arguments_become_opaque_metadata():
    calls = {"repr": 0, "str": 0}

    class HostileArg:
        def __repr__(self):
            calls["repr"] += 1
            raise RuntimeError("repr must not be called")

        def __str__(self):
            calls["str"] += 1
            raise RuntimeError("str must not be called")

    exc = ValueError("scalar", HostileArg(), b"raw", 42, None, True)
    message = _safe_exception_message(exc)
    assert calls == {"repr": 0, "str": 0}
    assert "scalar" in message
    assert "raw" in message
    assert "42" in message
    assert "None" in message
    # The hostile object is summarized as opaque type metadata only.
    assert "HostileArg" in message
    json.dumps(message)


def test_safe_exception_type_ignores_hostile_metaclass_hooks():
    sentinel = {"hook": 0}

    class _HostileMeta(type):
        @property
        def __name__(cls):
            sentinel["hook"] += 1
            return "SpoofedName"

        def __getattribute__(cls, name):
            sentinel["hook"] += 1
            return type.__getattribute__(cls, name)

    class Hostile(Exception, metaclass=_HostileMeta):
        pass

    exc = Hostile("x")
    type_name = _safe_exception_type_name(type(exc))
    assert type_name == "Hostile"
    assert sentinel["hook"] == 0
    message = _safe_exception_message(exc)
    assert "x" in message
    assert sentinel["hook"] == 0


def test_safe_exception_error_message_shape_and_bounds():
    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError("must not be invoked")

    exc = Hostile("boom")
    message = _safe_exception_error_message(exc)
    assert message == "Target raised Hostile: boom"
    assert len(message.encode("utf-8")) <= _POST_MORTEM_MAX_EXC_MESSAGE_UTF8


def test_safe_exception_message_long_input_is_byte_bounded():
    exc = ValueError("a" * 5000)
    message = _safe_exception_message(exc)
    assert len(message.encode("utf-8")) <= _POST_MORTEM_MAX_EXC_MESSAGE_UTF8


def test_evidence_exception_fields_never_invoke_target_code_and_stay_bounded():
    calls = {"str": 0}

    class Hostile(Exception):
        def __str__(self):
            calls["str"] += 1
            raise RuntimeError("must not be invoked")

    exc = Hostile("x" * 3000)
    chain = _FakeTb(_FakeFrame("a.py", "f", 1, locals_={}))
    evidence = _capture_post_mortem_evidence_pure(
        "a.py", type(exc), exc, chain, _safe_exception_error_message
    )
    assert calls == {"str": 0}
    for field in ("type", "message", "repr"):
        value = evidence["exception"][field]
        assert len(value.encode("utf-8")) <= _POST_MORTEM_MAX_EXC_MESSAGE_UTF8
    json.dumps(evidence, allow_nan=False)


# ---- exception-argument work bounds ----------------------------------------


def test_safe_exception_message_never_inspects_beyond_args_ceiling():
    # 10,000 arguments: the message contains the first ceiling arguments and
    # the omission marker, and never reaches argument index `ceiling`.
    exc = ValueError(*range(10000))
    message = _safe_exception_message(exc)
    assert _POST_MORTEM_EXC_ARGS_MAX_SCAN > 0
    assert f"{_POST_MORTEM_EXC_ARGS_MAX_SCAN - 1}" in message
    assert f"; {_POST_MORTEM_EXC_ARGS_MAX_SCAN}" not in message
    assert _POST_MORTEM_TRUNCATION_MARKER in message
    assert len(message.encode("utf-8")) <= _POST_MORTEM_MAX_EXC_MESSAGE_UTF8
    json.dumps(message)


def test_safe_exception_message_omitted_arg_beyond_ceiling_is_marker_only():
    # The argument at index `ceiling` is a huge exact int whose metadata would
    # be visible if it were inspected; it must not appear, proving the scan
    # stopped at the documented ceiling.
    exc = ValueError(*range(_POST_MORTEM_EXC_ARGS_MAX_SCAN), 10**5000)
    message = _safe_exception_message(exc)
    assert "<int bits=16610>" not in message
    assert _POST_MORTEM_TRUNCATION_MARKER in message
    assert len(message.encode("utf-8")) <= _POST_MORTEM_MAX_EXC_MESSAGE_UTF8


def test_safe_exception_message_very_large_exact_string_is_previewed():
    exc = ValueError("a" * (10**6))
    message = _safe_exception_message(exc)
    assert len(message.encode("utf-8")) <= _POST_MORTEM_MAX_EXC_MESSAGE_UTF8
    assert _POST_MORTEM_TRUNCATION_MARKER in message
    assert "a" in message
    json.dumps(message)


def test_safe_exception_message_large_exact_bytes_never_fully_decoded(monkeypatch):
    # ``bytes.decode`` is an immutable built-in method and cannot be patched;
    # instrument the module's ``_utf8_preview`` seam instead: every decoded
    # text must pass through it, so the largest string it ever sees proves
    # the decode input was a bounded prefix, never the complete 2 MB object.
    seen = []
    real_preview = worker_module._utf8_preview

    def spying_preview(value, maximum):
        seen.append(str.__len__(value))
        return real_preview(value, maximum)

    monkeypatch.setattr(worker_module, "_utf8_preview", spying_preview)
    payload = b"x" * (2 * 10**6)
    message = _safe_exception_message(ValueError(payload))
    assert seen and max(seen) <= _MAX_BYTES_PREVIEW
    assert len(message.encode("utf-8")) <= _POST_MORTEM_MAX_EXC_MESSAGE_UTF8
    assert _POST_MORTEM_TRUNCATION_MARKER in message
    json.dumps(message)


def test_safe_exception_message_huge_positive_integer_is_bounded_metadata():
    message = _safe_exception_message(ValueError(10**5000))
    assert message == "<int bits=16610>"
    assert len(message.encode("utf-8")) <= _POST_MORTEM_MAX_EXC_MESSAGE_UTF8
    json.dumps(message)


def test_safe_exception_message_huge_negative_integer_is_bounded_metadata():
    message = _safe_exception_message(ValueError(-(10**5000)))
    assert message == "<int bits=16610>"
    assert len(message.encode("utf-8")) <= _POST_MORTEM_MAX_EXC_MESSAGE_UTF8
    json.dumps(message)


def test_safe_exception_message_mixed_args_exhausting_byte_budget():
    exc = ValueError("a" * 900, "b" * 900, "c" * 900)
    first = _safe_exception_message(exc)
    second = _safe_exception_message(exc)
    assert first == second
    assert len(first.encode("utf-8")) <= _POST_MORTEM_MAX_EXC_MESSAGE_UTF8
    assert first.startswith("a")
    json.dumps(first)


def test_safe_exception_message_small_exception_output_unchanged():
    assert _safe_exception_message(ValueError("boom")) == "boom"
    assert _safe_exception_message(ValueError("a", "b")) == "a; b"
    assert (
        _safe_exception_message(ValueError(1, "two", 3.5, True, None, b"raw"))
        == "1; two; 3.5; True; None; raw"
    )


def test_safe_exception_message_hostile_str_never_invoked_with_huge_int():
    sentinel = {"str": 0}

    class Hostile(Exception):
        def __str__(self):
            sentinel["str"] += 1
            raise RuntimeError("must not be invoked")

    exc = Hostile(10**5000, "payload")
    message = _safe_exception_message(exc)
    assert sentinel["str"] == 0
    assert "payload" in message
    assert "<int bits=16610>" in message
    json.dumps(message)


def test_safe_exception_message_huge_integer_evidence_level_does_not_raise():
    chain = _FakeTb(_FakeFrame("huge.py", "f", 1, locals_={}))
    evidence = _capture_post_mortem_evidence_pure(
        "huge.py", ValueError, ValueError(10**5000), chain,
        _safe_exception_error_message,
    )
    message = evidence["exception"]["message"]
    assert "<int bits=16610>" in message
    for field in ("type", "message", "repr"):
        assert len(evidence["exception"][field].encode("utf-8")) <= (
            _POST_MORTEM_MAX_EXC_MESSAGE_UTF8
        )
    json.dumps(evidence, allow_nan=False)


def test_session_huge_integer_argument_yields_bounded_post_mortem():
    root = _make_workspace(
        "huge_int_target.py",
        "def boom():\n"
        "    raise ValueError(10 ** 5000)\n"
        "boom()\n",
    )
    try:
        with TaskWorkspace(str(root)) as ws:
            with PdbSession(ws) as session:
                response = session.run_post_mortem("huge_int_target.py")
    finally:
        shutil.rmtree(str(root), ignore_errors=True)
    assert response.success is True
    result = response.result
    assert result["status"] == "post_mortem"
    message = result["exception"]["message"]
    assert "<int bits=16610>" in message
    assert len(message.encode("utf-8")) <= _POST_MORTEM_MAX_EXC_MESSAGE_UTF8
    serialized = serialize_response(response)
    assert len(serialized) <= MAX_LINE_LENGTH
    assert json.loads(serialized)["result"]["exception"]["message"] == message


# ---- missing-traceback failure ---------------------------------------------


def test_missing_traceback_authoritative_response_constructor():
    response = _post_mortem_missing_traceback_response(request_id=9)
    assert isinstance(response, PdbResponse)
    assert response.request_id == 9
    assert response.protocol_version == PROTOCOL_VERSION
    assert response.success is False
    assert response.result == {}
    assert response.error
    assert len(response.error.encode("utf-8")) <= MAX_LINE_LENGTH
    assert "no traceback" in response.error


def test_missing_traceback_response_is_strict_finite_json():
    response = _post_mortem_missing_traceback_response(request_id=3)
    serialized = serialize_response(response)
    assert len(serialized) <= MAX_LINE_LENGTH
    assert json.loads(serialized)["success"] is False
    assert json.loads(serialized)["result"] == {}


def test_worker_missing_traceback_real_branch_emits_authoritative_response(monkeypatch):
    root = _make_workspace("raising_target.py", 'raise ValueError("boom")\n')
    out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="\n")
    try:
        worker = PdbWorker()
        worker._protocol_stdout = out
        monkeypatch.setattr(worker_module, "_has_traceback", lambda captured: False)
        worker._execute_post_mortem_target(
            "raising_target.py",
            str((root / "raising_target.py").resolve()),
            [],
            b'raise ValueError("boom")\n',
            7,
        )
        raw = out.buffer.getvalue()
        response = deserialize_response(raw)
        assert response.request_id == 7
        assert response.protocol_version == PROTOCOL_VERSION
        assert response.success is False
        assert response.result == {}
        assert response.error
        assert len(response.error.encode("utf-8")) <= MAX_LINE_LENGTH
        assert "no traceback" in response.error
        # Worker lifecycle after the fail-closed branch.
        assert worker._lifecycle["state"] == "failed"
        assert worker._lifecycle["script"] == "raising_target.py"
        assert worker._lifecycle["error"] == response.error
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


def test_session_fail_closed_response_lifecycle_and_cleanup(
    failing_script_workspace, monkeypatch
):
    fail_response = _post_mortem_missing_traceback_response(request_id=1)
    with PdbSession(failing_script_workspace) as session:
        monkeypatch.setattr(
            session, "_send_and_receive",
            lambda request, timeout: fail_response,
        )
        response = session.run_post_mortem("failing_target.py")
        assert response is fail_response
        assert response.success is False
        assert response.result == {}
        assert session._target_lifecycle_state == "failed"
        assert session._target_consumed is True
    assert session.state == PdbSessionState.STOPPED
    assert session._proc is None


def test_none_traceback_pure_helper_returns_empty_evidence():
    exc = ValueError("no traceback")
    evidence = _capture_post_mortem_evidence_pure(
        "none.py", type(exc), exc, None, _safe_exception_error_message
    )
    assert evidence["traceback_frames"] == []
    assert evidence["innermost_frame"] == {}
    assert evidence["frames_truncated"] is False
    assert evidence["exception"]["type"] == "ValueError"
    assert evidence["exception"]["message"] == (
        "Target raised ValueError: no traceback"
    )


def test_has_traceback_factored_helper_decides_correctly():
    assert _has_traceback(None) is False
    exc = ValueError("no tb")
    assert _has_traceback((type(exc), exc, None)) is False
    try:
        raise ValueError("real tb")
    except ValueError:
        exc_type, exc_value, tb = sys.exc_info()
    assert _has_traceback((exc_type, exc_value, tb)) is True


# ---- identity --------------------------------------------------------------


def test_post_mortem_evidence_is_bound_to_script_and_session_identity(failing_script_workspace):
    with PdbSession(failing_script_workspace) as session:
        response = session.run_post_mortem("failing_target.py")
    assert response.success is True
    assert response.result["script"] == "failing_target.py"
    assert response.request_id >= 1


# ---- cleanup ---------------------------------------------------------------


def test_workspace_cleanup_occurs_after_post_mortem(failing_script_workspace):
    with PdbSession(failing_script_workspace) as session:
        session.run_post_mortem("failing_target.py")
    assert session.state == PdbSessionState.STOPPED
    assert session._proc is None


# ---- determinism -----------------------------------------------------------


def test_post_mortem_evidence_is_deterministic_across_runs(failing_script_workspace):
    def capture():
        with PdbSession(failing_script_workspace) as session:
            return session.run_post_mortem("failing_target.py").result

    r1 = capture()
    r2 = capture()
    assert r1["exception"] == r2["exception"]
    assert r1["traceback_frames"] == r2["traceback_frames"]
    assert r1["frames_truncated"] == r2["frames_truncated"]
    assert r1["innermost_frame"]["function"] == r2["innermost_frame"]["function"]
    assert r1["innermost_frame"]["line"] == r2["innermost_frame"]["line"]
    assert r1["innermost_frame"]["local_values"] == r2["innermost_frame"]["local_values"]


# ---- no-provider / no-network ----------------------------------------------


def test_no_provider_or_network_attempt(failing_script_workspace, monkeypatch):
    def _no_socket(*args, **kwargs):
        raise AssertionError("post-mortem path must not open a network socket")

    monkeypatch.setattr(_socket, "socket", _no_socket)
    response = _run_script(failing_script_workspace, "failing_target.py")
    assert response.success is True
    assert response.result["status"] == "post_mortem"


# ---- complete protocol byte bound ------------------------------------------


def test_post_mortem_operation_in_supported_operations():
    from agentic_debugger.runtime.pdb_protocol import SUPPORTED_OPERATIONS

    assert "run_post_mortem" in SUPPORTED_OPERATIONS


def test_post_mortem_protocol_version_bound(failing_script_workspace):
    response = _run_script(failing_script_workspace, "failing_target.py")
    assert response.protocol_version == PROTOCOL_VERSION


def test_post_mortem_evidence_is_strict_finite_json(failing_script_workspace):
    response = _run_script(failing_script_workspace, "failing_target.py")
    assert response.success is True
    serialized = json.dumps(response.result, allow_nan=False, sort_keys=True)
    assert json.loads(serialized) == response.result


def test_total_post_mortem_response_within_max_line_length(failing_script_workspace):
    response = _run_script(failing_script_workspace, "failing_target.py")
    assert response.success is True
    serialized = serialize_response(response)
    assert len(serialized) <= MAX_LINE_LENGTH, (
        f"post-mortem response exceeds MAX_LINE_LENGTH ({len(serialized)} > {MAX_LINE_LENGTH})"
    )


# ---- UTF-8 byte bounds -----------------------------------------------------


def _encoded_length(value: str) -> int:
    return len(value.encode("utf-8"))


def test_bounded_text_limit_zero_returns_empty():
    assert _post_mortem_bounded_text("abc", 0) == ""
    assert _encoded_length(_post_mortem_bounded_text("abc", 0)) == 0


def test_bounded_text_limits_one_and_two_without_room_for_marker():
    # The 3-byte marker cannot fit into limits 1 and 2; the preview is
    # truncated to the budget without a marker and never exceeds it.
    out1 = _post_mortem_bounded_text("abcdef", 1)
    assert _encoded_length(out1) <= 1
    out2 = _post_mortem_bounded_text("abcdef", 2)
    assert _encoded_length(out2) <= 2


def test_bounded_text_limit_three_exactly_fits_marker():
    # Limit 3 is exactly the marker byte size: an over-limit value becomes
    # the bare marker, 3 bytes, never more.
    out = _post_mortem_bounded_text("abcdef", 3)
    assert _encoded_length(out) == 3
    assert out == "\u2026"


def test_bounded_text_ascii_boundary_minus_one():
    out = _post_mortem_bounded_text("a" * 300, 255)
    assert _encoded_length(out) <= 255


def test_bounded_text_ascii_exact_boundary_unchanged():
    assert _post_mortem_bounded_text("a" * 256, 256) == "a" * 256


def test_bounded_text_ascii_boundary_plus_one_marker_included():
    out = _post_mortem_bounded_text("a" * 257, 256)
    assert _encoded_length(out) == 256
    assert out.endswith("\u2026")
    assert out.count("a") == 253  # 253 content bytes + 3-byte marker


def test_bounded_text_long_ascii_never_exceeds_limit():
    for limit in (64, 128, 256, 1024):
        out = _post_mortem_bounded_text("x" * 5000, limit)
        assert _encoded_length(out) <= limit
        json.dumps(out)


def test_bounded_text_long_two_byte_characters():
    # Greek alpha is two UTF-8 bytes; 300 characters are 600 bytes.
    multi_byte = "\u03b1" * 300
    out = _post_mortem_bounded_text(multi_byte, 256)
    assert _encoded_length(out) <= 256
    assert out.endswith("\u2026")


def test_bounded_text_two_byte_chars_that_fit_stay_unchanged():
    # 100 alphas = 200 bytes, below the 256 limit: unchanged, no marker.
    multi_byte = "\u03b1" * 100
    assert _post_mortem_bounded_text(multi_byte, 256) == multi_byte


def test_bounded_text_three_and_four_byte_code_points():
    euro = "\u20ac" * 200  # 3-byte code points
    emoji = "\U0001F4A5" * 200  # 4-byte code points
    for text in (euro, emoji):
        out = _post_mortem_bounded_text(text, 256)
        assert _encoded_length(out) <= 256
        json.dumps(out)


def test_bounded_text_straddling_multibyte_character_boundary():
    # 253 ASCII bytes fill the content budget; the next 2-byte alpha does
    # not fit in the remaining byte, so the preview stops at the clean
    # boundary and the marker keeps the total inside the limit.
    text = "a" * 253 + "\u03b1" + "b" * 5
    out = _post_mortem_bounded_text(text, 256)
    assert _encoded_length(out) == 256
    assert out.endswith("\u2026")
    assert out.count("a") == 253


def test_bounded_text_lone_surrogate_is_replaced_and_serializable():
    text = "\ud800" + "x" * 300
    out = _post_mortem_bounded_text(text, 256)
    assert _encoded_length(out) <= 256
    json.dumps(out)  # must not raise on the lone surrogate
    assert "\ud800" not in out


def test_bounded_text_empty_input_stays_empty():
    assert _post_mortem_bounded_text("", 256) == ""
    assert _post_mortem_bounded_text("", 0) == ""


def test_bounded_text_short_input_stays_unchanged():
    assert _post_mortem_bounded_text("hello", 256) == "hello"


def test_bounded_text_exact_builtin_scalars_use_builtin_str():
    assert _post_mortem_bounded_text(42, 256) == "42"
    assert _post_mortem_bounded_text(3.5, 256) == "3.5"
    assert _post_mortem_bounded_text(True, 256) == "True"


def test_bounded_text_huge_exact_int_uses_metadata_never_decimalizes():
    assert _post_mortem_bounded_text(10**5000, 256) == "<int bits=16610>"
    assert _post_mortem_bounded_text(-(10**5000), 256) == "<int bits=16610>"
    assert _post_mortem_bounded_text(0, 256) == "0"
    assert _post_mortem_bounded_text(42, 256) == "42"
    out = _post_mortem_bounded_text(10**5000, 8)
    assert len(out.encode("utf-8")) <= 8
    json.dumps(out)


def test_bounded_text_unknown_value_is_opaque_without_str():
    class Hostile:
        def __str__(self):
            raise RuntimeError("str must not be invoked")

        def __repr__(self):
            raise RuntimeError("repr must not be invoked")

    assert _post_mortem_bounded_text(Hostile(), 256) == ""


def test_bounded_text_all_limits_never_exceed(monkeypatch):
    # Exhaustive small-limit sweep over ASCII, 2-byte, 3-byte, and 4-byte
    # content plus a surrogate; every result stays within the limit.
    samples = [
        "a" * 50,
        "\u03b1" * 50,
        "\u20ac" * 50,
        "\U0001F4A5" * 50,
        "\ud800" + "a" * 50,
    ]
    for limit in range(0, 12):
        for text in samples:
            out = _post_mortem_bounded_text(text, limit)
            assert _encoded_length(out) <= limit
            json.dumps(out)
