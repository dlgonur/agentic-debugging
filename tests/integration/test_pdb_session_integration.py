import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agentic_debugger.runtime.pdb_session import (
    PdbSession,
    PdbSessionState,
)
from agentic_debugger.runtime.pdb_protocol import (
    PROTOCOL_VERSION,
    MAX_LINE_LENGTH,
    PdbRequest,
    PdbResponse,
    PdbWorkerInfo,
    serialize_request,
    deserialize_request,
    deserialize_response,
    serialize_response,
)
from agentic_debugger.runtime.exceptions import (
    PdbProtocolError,
    PdbSessionError,
    PdbSessionStateError,
    PdbSessionTimeoutError,
    PdbWorkerExitedError,
)
from agentic_debugger.runtime.workspace import TaskWorkspace


_BREAKPOINT_SCRIPT = (
    "x = 1\n"
    "y = 2\n"
    "z = 3\n"
    "result = x + y + z\n"
)

_EXIT_EARLY_SCRIPT = (
    "x = 1\n"
    "import sys\n"
    "sys.stdout.write('done')\n"
    "# end\n"
)

_SYSEXIT_SCRIPT = (
    "import sys\n"
    "sys.exit(42)\n"
    "# end\n"
)

_SYSEXIT_NONE_SCRIPT = (
    "import sys\n"
    "sys.exit(None)\n"
    "# end\n"
)

_SYSEXIT_STRING_SCRIPT = (
    "import sys\n"
    "sys.exit('bye')\n"
    "# end\n"
)

_TARGET_EXCEPTION_SCRIPT = (
    "def main():\n"
    "    raise ValueError('example')\n"
    "main()\n"
    "# end\n"
)

_SYNTAX_ERROR_SCRIPT = (
    "def missing_colon()\n"
    "    pass\n"
)

_ARGV_SCRIPT = (
    "import sys\n"
    "x = sys.argv\n"
)

_IMPORT_SCRIPT = (
    "import sys\n"
    "sys.path.insert(0, '.')\n"
    "from subdir import util\n"
    "x = util.util()\n"
)

_CWD_SCRIPT = (
    "import os\n"
    "cwd = os.getcwd()\n"
)

_INFINITE_LOOP_SCRIPT = (
    "while True:\n"
    "    pass\n"
    "# never reached\n"
)

_PRINT_SCRIPT = (
    "print('hello from target')\n"
    "x = 42\n"
)

_STDERR_SCRIPT = (
    "import sys\n"
    "sys.stderr.write('stderr from target\\n')\n"
    "x = 42\n"
)

_INPUT_SCRIPT = (
    "try:\n"
    "    data = input()\n"
    "except EOFError:\n"
    "    x = 42\n"
    "else:\n"
    "    x = 0\n"
)

_FUNCTION_BP_SCRIPT = (
    "def main():\n"
    "    x = 1\n"
    "    y = 2\n"
    "    z = 3\n"
    "\n"
    "def other():\n"
    "    pass\n"
    "\n"
    "main()\n"
)

_MULTI_BP_SCRIPT = (
    "x = 1\n"
    "y = 2\n"
    "z = 3\n"
    "w = 4\n"
    "v = 5\n"
)


@pytest.fixture
def workspace():
    src = Path(tempfile.mkdtemp())
    try:
        (src / "test.py").write_text("x = 1\n")
        (src / "subdir").mkdir()
        (src / "subdir" / "util.py").write_text("def util(): return 42\n")
        with TaskWorkspace(str(src)) as ws:
            yield ws
    finally:
        shutil.rmtree(str(src), ignore_errors=True)


class TestPdbSessionIntegration:
    def test_start_and_valid_handshake(self, workspace):
        session = PdbSession(workspace)
        session.start()
        try:
            assert session.state == PdbSessionState.READY
            assert session.is_alive is True
        finally:
            session.stop()


# =====================================================================
# Task 4D - Safe evaluation and PDB integration hardening v1
# =====================================================================


@pytest.fixture
def safe_eval_workspace():
    root = Path(tempfile.mkdtemp())
    marker = root / "hostile-marker.txt"
    lines = [
        "import pathlib",
        "import math",
        "GLOBAL_ONLY = 9001",
        "IMPORTED_GLOBAL = math",
        "MARKER = pathlib.Path(__import__('sys').argv[1])",
        "def hit(name):",
        "    MARKER.write_text(name, encoding='utf-8')",
        "    raise AssertionError(name)",
        "class HostileMeta(type):",
        "    def __getattribute__(cls, name): hit('meta')",
        "class Descriptor:",
        "    def __get__(self, instance, owner): hit('descriptor')",
        "class Hostile(metaclass=HostileMeta):",
        "    descriptor = Descriptor()",
        "    @property",
        "    def attr(self): hit('property')",
        "    def __getattribute__(self, name): hit('getattribute')",
        "    def __repr__(self): hit('repr')",
        "    def __str__(self): hit('str')",
        "    def __format__(self, spec): hit('format')",
        "    def __bool__(self): hit('bool')",
        "    def __len__(self): hit('len')",
        "    def __iter__(self): hit('iter')",
        "    def __next__(self): hit('next')",
        "    def __getitem__(self, key): hit('getitem')",
        "    def __contains__(self, value): hit('contains')",
        "    def __eq__(self, other): hit('eq')",
        "    def __lt__(self, other): hit('lt')",
        "    def __le__(self, other): hit('le')",
        "    def __gt__(self, other): hit('gt')",
        "    def __ge__(self, other): hit('ge')",
        "    def __add__(self, other): hit('add')",
        "    def __sub__(self, other): hit('sub')",
        "    def __mul__(self, other): hit('mul')",
        "    def __truediv__(self, other): hit('truediv')",
        "    def __floordiv__(self, other): hit('floordiv')",
        "    def __mod__(self, other): hit('mod')",
        "    def __index__(self): hit('index')",
        "    def __hash__(self): hit('hash')",
        "class HostileKey:",
        "    def __hash__(self):",
        "        MARKER.write_text('key-hash', encoding='utf-8')",
        "        return 17",
        "    def __eq__(self, other): hit('key-eq')",
        "def outer():",
        "    caller_value = 77",
        "    def inner():",
        "        items = [42, 43]",
        "        text = 'hello'",
        "        exact_dict = {'safe': 88, 3: 99}",
        "        obj = Hostile()",
        "        hostile_key = HostileKey()",
        "        hostile_dict = {hostile_key: 'secret', 'safe': 123}",
        "        huge = 1 << 4095",
        "        bounded_int_key = 1 << 4095",
        "        oversized_int_key = 1 << 4096",
        "        bounded_text_key = '\u00e9' * 2048",
        "        oversized_text_key = '\u00e9' * 2049",
        "        bounded_bytes_key = b'x' * 4096",
        "        oversized_bytes_key = b'x' * 4097",
        "        bounded_key_dict = {bounded_int_key: 11, "
        "bounded_text_key: 12, bounded_bytes_key: 13}",
        "        oversized_stored_dict = {oversized_int_key: 1, "
        "oversized_text_key: 2, oversized_bytes_key: 3, 'target': 14}",
        "        large_float = 1e308",
        "        tiny_float = 1e-308",
        "        budget_value = ['x' * 2048 for _ in range(16)]",
        "        len = 'shadowed-local'",
        "        if MARKER.exists(): MARKER.unlink()",
        "        pause_one = items[0]",
        "        between = items[1]",
        "        pause_two = between",
        "        return pause_one + pause_two",
        "    return inner()",
        "outer()",
    ]
    first_line = lines.index("        pause_one = items[0]") + 1
    second_line = lines.index("        pause_two = between") + 1
    (root / "safe_eval_target.py").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    try:
        with TaskWorkspace(str(root)) as workspace:
            yield workspace, marker, first_line, second_line
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


@pytest.fixture
def frame_local_collision_workspace():
    root = Path(tempfile.mkdtemp())
    marker = root / "frame-local-collision.txt"
    lines = [
        "import pathlib",
        "import sys",
        "MARKER = pathlib.Path(sys.argv[1])",
        "class HostileKey:",
        "    active = False",
        "    def __init__(self, collision_hash):",
        "        self.collision_hash = collision_hash",
        "    def __hash__(self):",
        "        return self.collision_hash",
        "    def __eq__(self, other):",
        "        if self.active:",
        "            MARKER.write_text('hostile equality', encoding='utf-8')",
        "            raise RuntimeError('hostile equality')",
        "        return False",
        "def target_function():",
        "    target = 42",
        "    frame = sys._getframe()",
        "    local_mapping = frame.f_locals",
        "    unknown_key = HostileKey(hash('unknown_collision'))",
        "    extra_key = HostileKey(hash('extra_local'))",
        "    local_mapping[unknown_key] = 'ignored unknown collision'",
        "    local_mapping[extra_key] = 'ignored exact collision'",
        "    local_mapping['extra_local'] = 73",
        "    HostileKey.active = True",
        "    pause = target",
        "    return pause",
        "target_function()",
    ]
    pause_line = lines.index("    pause = target") + 1
    (root / "frame_local_collision.py").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    try:
        with TaskWorkspace(str(root)) as workspace:
            yield workspace, marker, pause_line
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


class TestFrameLocalCollisionIntegration:
    def test_eval_and_locals_never_reenter_mapping_by_key(
        self, frame_local_collision_workspace
    ):
        workspace, marker, pause_line = frame_local_collision_workspace
        session = PdbSession(workspace)
        session.start()
        try:
            session.start_paused_target(
                "frame_local_collision.py", [pause_line], [str(marker)]
            )
            generation = session.get_stack_summary()["pause_generation"]
            assert session.safe_eval_expression(
                0, generation, "target"
            )["value"]["value"] == 42
            with pytest.raises(PdbSessionError, match="Unknown local name"):
                session.safe_eval_expression(
                    0, generation, "unknown_collision"
                )
            assert not marker.exists()

            frame = session.get_frame(0, generation)["frame"]
            assert "extra_local" in frame["local_names"]
            locals_result = session.get_frame_locals(0, generation)
            values = {
                entry["name"]: entry["value"]
                for entry in locals_result["locals"]
            }
            assert values["target"]["value"] == 42
            assert values["extra_local"]["value"] == 73
            assert not marker.exists()
            assert session.get_target_status()["state"] == "paused"
            assert session.ping().success is True
            assert session.terminate_paused_target()["state"] == "terminated"
        finally:
            session.stop()


@pytest.fixture
def module_scope_eval_workspace():
    root = Path(tempfile.mkdtemp())
    lines = [
        "import math",
        "module_value = 42",
        "pause = module_value",
        "after = pause + 1",
    ]
    pause_line = lines.index("pause = module_value") + 1
    (root / "module_scope.py").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    try:
        with TaskWorkspace(str(root)) as workspace:
            yield workspace, pause_line
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


class TestModuleScopeSafeEvaluationIsolation:
    def test_current_module_frame_rejects_all_value_bearing_operations(
        self, module_scope_eval_workspace
    ):
        workspace, pause_line = module_scope_eval_workspace
        session = PdbSession(workspace)
        session.start()
        try:
            session.start_paused_target("module_scope.py", [pause_line])
            stack = session.get_stack_summary()
            generation = stack["pause_generation"]
            assert stack["frames"][0]["function"] == "<module>"
            assert session.get_target_status()["state"] == "paused"
            with pytest.raises(PdbSessionError, match="Module-scope"):
                session.get_frame(0, generation)
            with pytest.raises(PdbSessionError, match="Module-scope"):
                session.get_frame_locals(0, generation)
            for expression in [
                "module_value", "math", "open", "len", "1 + 2", "len('abc')",
            ]:
                with pytest.raises(PdbSessionError, match="Module-scope"):
                    session.safe_eval_expression(0, generation, expression)
            status = session.get_target_status()
            assert status["state"] == "paused"
            assert session.get_stack_summary()["pause_generation"] == generation
            assert session.ping().success is True
            assert session.continue_paused_target()["state"] == "exited"
        finally:
            session.stop()

    def test_current_module_frame_failure_preserves_termination(
        self, module_scope_eval_workspace
    ):
        workspace, pause_line = module_scope_eval_workspace
        session = PdbSession(workspace)
        session.start()
        try:
            session.start_paused_target("module_scope.py", [pause_line])
            generation = session.get_stack_summary()["pause_generation"]
            with pytest.raises(PdbSessionError, match="Module-scope"):
                session.safe_eval_expression(0, generation, "1 + 2")
            assert session.terminate_paused_target()["state"] == "terminated"
        finally:
            session.stop()


class TestSafeEvaluationIntegration:
    @staticmethod
    def _start(data, breakpoints=None):
        workspace, marker, first_line, second_line = data
        session = PdbSession(workspace)
        session.start()
        selected = [first_line, second_line] if breakpoints is None else breakpoints
        started = session.start_paused_target(
            "safe_eval_target.py", selected, [str(marker)]
        )
        assert started["state"] == "paused"
        return session

    def test_locals_only_intrinsic_len_and_success_schema(
        self, safe_eval_workspace
    ):
        session = self._start(safe_eval_workspace)
        try:
            stack = session.get_stack_summary()
            generation = stack["pause_generation"]
            result = session.safe_eval_expression(0, generation, "items[0]")
            assert set(result) == {
                "state", "pause_generation", "frame", "expression", "value",
            }
            assert result["state"] == "paused"
            assert result["pause_generation"] == generation
            assert result["frame"] == stack["frames"][0]
            assert result["expression"] == "items[0]"
            assert result["value"] == {
                "kind": "int", "type": "builtins.int", "value": 42,
                "special": None, "size": 6, "items": [], "entries": [],
                "truncated": False,
            }
            assert session.safe_eval_expression(
                1, generation, "caller_value"
            )["value"]["value"] == 77
            assert session.safe_eval_expression(
                0, generation, "len"
            )["value"]["value"] == "shadowed-local"
            assert session.safe_eval_expression(
                0, generation, "len(items)"
            )["value"]["value"] == 2
            assert session.safe_eval_expression(
                0, generation, "exact_dict['safe']"
            )["value"]["value"] == 88
            assert session.get_target_status()["line"] == result["frame"]["line"]
            assert session.get_stack_summary()["pause_generation"] == generation
            assert session.get_frame(0, generation)["frame"]["frame_id"] == 0
            assert session.get_frame_locals(0, generation)["frame_id"] == 0
            module_frames = [
                frame for frame in stack["frames"]
                if frame["function"] == "<module>"
            ]
            assert len(module_frames) == 1
            module_frame_id = module_frames[0]["frame_id"]
            assert module_frame_id == 2
            assert session.get_frame(0, generation)["frame"]["function"] == "inner"
            assert session.get_frame(1, generation)["frame"]["function"] == "outer"
            assert session.safe_eval_expression(
                0, generation, "items[0]"
            )["value"]["value"] == 42
            assert session.safe_eval_expression(
                1, generation, "caller_value"
            )["value"]["value"] == 77
            with pytest.raises(PdbSessionError, match="Module-scope"):
                session.get_frame(module_frame_id, generation)
            with pytest.raises(PdbSessionError, match="Module-scope"):
                session.get_frame_locals(module_frame_id, generation)
            for expression in [
                "GLOBAL_ONLY", "IMPORTED_GLOBAL", "__builtins__",
                "1 + 2", "len('abc')",
            ]:
                with pytest.raises(PdbSessionError, match="Module-scope"):
                    session.safe_eval_expression(
                        module_frame_id, generation, expression
                    )
            assert session.get_frame_locals(
                0, generation
            )["frame_id"] == 0
            assert session.get_stack_summary()["pause_generation"] == generation
            assert session.ping().success is True
            continued = session.continue_paused_target()
            assert continued["state"] == "paused"
            assert (
                session.get_stack_summary()["pause_generation"]
                == generation + 1
            )
            assert session.terminate_paused_target()["state"] == "terminated"
        finally:
            session.stop()

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("bounded_key_dict[bounded_int_key]", 11),
            ("bounded_key_dict[bounded_text_key]", 12),
            ("bounded_key_dict[bounded_bytes_key]", 13),
            ("oversized_stored_dict['target']", 14),
        ],
    )
    def test_bounded_and_skipped_stored_dict_keys(
        self, safe_eval_workspace, expression, expected
    ):
        session = self._start(safe_eval_workspace)
        try:
            generation = session.get_stack_summary()["pause_generation"]
            result = session.safe_eval_expression(0, generation, expression)
            assert result["value"]["value"] == expected
            assert session.get_stack_summary()["pause_generation"] == generation
            assert session.ping().success is True
            assert session.terminate_paused_target()["state"] == "terminated"
        finally:
            session.stop()

    @pytest.mark.parametrize(
        "expression",
        [
            "bounded_key_dict[oversized_int_key]",
            "bounded_key_dict[oversized_text_key]",
            "bounded_key_dict[oversized_bytes_key]",
        ],
    )
    def test_oversized_requested_dict_key_failure_is_recoverable(
        self, safe_eval_workspace, expression
    ):
        session = self._start(safe_eval_workspace)
        try:
            generation = session.get_stack_summary()["pause_generation"]
            with pytest.raises(PdbSessionError, match="safe bounds"):
                session.safe_eval_expression(0, generation, expression)
            assert session.get_stack_summary()["pause_generation"] == generation
            assert session.ping().success is True
            assert session.safe_eval_expression(
                0, generation, "exact_dict['safe']"
            )["value"]["value"] == 88
            continued = session.continue_paused_target()
            assert continued["state"] == "paused"
            assert (
                session.get_stack_summary()["pause_generation"]
                == generation + 1
            )
            assert session.terminate_paused_target()["state"] == "terminated"
        finally:
            session.stop()

    @pytest.mark.parametrize(
        "expression",
        [
            "large_float + large_float",
            "large_float * large_float",
            "large_float / tiny_float",
            "large_float // tiny_float",
        ],
    )
    def test_finite_float_overflow_is_ordinary_and_recoverable(
        self, safe_eval_workspace, expression
    ):
        session = self._start(safe_eval_workspace)
        try:
            generation = session.get_stack_summary()["pause_generation"]
            with pytest.raises(PdbSessionError, match="Finite arithmetic"):
                session.safe_eval_expression(0, generation, expression)
            assert session.get_target_status()["state"] == "paused"
            assert session.safe_eval_expression(
                0, generation, "large_float / 10.0"
            )["value"]["value"] == 1e307
            assert session.get_stack_summary()["pause_generation"] == generation
            assert session.ping().success is True
            assert session.terminate_paused_target()["state"] == "terminated"
        finally:
            session.stop()

    @pytest.mark.parametrize(
        "expression",
        [
            "GLOBAL_ONLY", "IMPORTED_GLOBAL", "open", "eval", "exec",
            "__import__('os')", "open('x')", "eval('1')", "exec('x')",
            "IMPORTED_GLOBAL.pi", "obj.attr", "lambda: 1", "[x for x in items]",
            "items[0:1]", "items + items", "obj == None", "obj + 1",
            "obj[0]", "len(obj)", "missing", "1 / 0", "huge * huge",
            " + ".join(["1"] * 30), "+" * 13 + "1",
        ],
    )
    def test_ordinary_failures_preserve_pause_and_worker(
        self, safe_eval_workspace, expression
    ):
        session = self._start(safe_eval_workspace)
        try:
            generation = session.get_stack_summary()["pause_generation"]
            with pytest.raises(PdbSessionError):
                session.safe_eval_expression(0, generation, expression)
            assert session.state == PdbSessionState.READY
            assert session._target_lifecycle_state == "paused"
            assert session.get_target_status()["state"] == "paused"
            assert session.get_stack_summary()["pause_generation"] == generation
            assert session.ping().success is True
            assert session.safe_eval_expression(
                0, generation, "items[-1]"
            )["value"]["value"] == 43
        finally:
            session.stop()

    def test_hostile_object_and_dict_key_have_no_side_effects(
        self, safe_eval_workspace
    ):
        session = self._start(safe_eval_workspace)
        marker = safe_eval_workspace[1]
        try:
            generation = session.get_stack_summary()["pause_generation"]
            bare = session.safe_eval_expression(0, generation, "obj")
            assert bare["value"]["kind"] == "object"
            assert session.safe_eval_expression(
                0, generation, "obj is None"
            )["value"]["value"] is False
            assert session.safe_eval_expression(
                0, generation, "hostile_dict['safe']"
            )["value"]["value"] == 123
            for expression in [
                "obj == None", "obj + 1", "obj[0]", "len(obj)",
                "obj.attr", "hostile_dict['missing']",
            ]:
                with pytest.raises(PdbSessionError):
                    session.safe_eval_expression(0, generation, expression)
            assert not marker.exists()
            assert session.ping().success is True
        finally:
            session.stop()

    def test_generation_staleness_then_new_generation_and_termination(
        self, safe_eval_workspace
    ):
        session = self._start(safe_eval_workspace)
        try:
            generation_one = session.get_stack_summary()["pause_generation"]
            assert session.safe_eval_expression(
                0, generation_one, "items[0]"
            )["value"]["value"] == 42
            continued = session.continue_paused_target()
            assert continued["state"] == "paused"
            generation_two = session.get_stack_summary()["pause_generation"]
            assert generation_two > generation_one
            with pytest.raises(PdbSessionError, match="generation"):
                session.safe_eval_expression(0, generation_one, "items[0]")
            with pytest.raises(PdbSessionError, match="frame_id"):
                session.safe_eval_expression(99, generation_two, "items[0]")
            assert session.safe_eval_expression(
                0, generation_two, "items[1]"
            )["value"]["value"] == 43
            assert session.terminate_paused_target()["state"] == "terminated"
            sent = []
            session._send_and_receive = lambda request, timeout: sent.append(request)
            with pytest.raises(PdbSessionStateError):
                session.safe_eval_expression(0, generation_two, "items[0]")
            assert sent == []
        finally:
            session.stop()

    def test_evaluation_does_not_mutate_and_continue_exits(
        self, safe_eval_workspace
    ):
        first_line = safe_eval_workspace[2]
        session = self._start(safe_eval_workspace, [first_line])
        try:
            generation = session.get_stack_summary()["pause_generation"]
            before = session.safe_eval_expression(0, generation, "items")["value"]
            assert session.safe_eval_expression(
                0, generation, "items[0] + items[1]"
            )["value"]["value"] == 85
            after = session.safe_eval_expression(0, generation, "items")["value"]
            assert after == before
            assert session.get_stack_summary()["pause_generation"] == generation
            assert session.continue_paused_target() == {
                "state": "exited", "script": "safe_eval_target.py",
                "exit_code": 0,
            }
            with pytest.raises(PdbSessionStateError):
                session.safe_eval_expression(0, generation, "items[0]")
        finally:
            session.stop()

    def test_result_budget_failure_is_ordinary(self, safe_eval_workspace):
        session = self._start(safe_eval_workspace)
        try:
            generation = session.get_stack_summary()["pause_generation"]
            with pytest.raises(PdbSessionError, match="32768-byte"):
                session.safe_eval_expression(0, generation, "budget_value")
            assert session.get_target_status()["state"] == "paused"
            assert session.safe_eval_expression(
                0, generation, "items[0]"
            )["value"]["value"] == 42
            assert session.terminate_paused_target()["state"] == "terminated"
        finally:
            session.stop()

    def test_stop_and_context_manager_cleanup_after_evaluation(
        self, safe_eval_workspace
    ):
        workspace, marker, first_line, _ = safe_eval_workspace
        with PdbSession(workspace) as session:
            session.start_paused_target(
                "safe_eval_target.py", [first_line], [str(marker)]
            )
            generation = session.get_stack_summary()["pause_generation"]
            assert session.safe_eval_expression(
                0, generation, "items[0]"
            )["value"]["value"] == 42
        assert session.state == PdbSessionState.STOPPED
        assert session._proc is None


class TestSafeEvaluationRawProtocol:
    def test_payload_validation_aliases_and_utf8_boundary(
        self, safe_eval_workspace
    ):
        session = TestSafeEvaluationIntegration._start(safe_eval_workspace)
        try:
            invalid_payloads = [
                {"pause_generation": 1, "expression": "items[0]"},
                {"frame_id": 0, "expression": "items[0]"},
                {"frame_id": 0, "pause_generation": 1},
                {"frame_id": 0, "pause_generation": 1,
                 "expression": "items[0]", "extra": 1},
                {"frame_id": True, "pause_generation": 1,
                 "expression": "items[0]"},
                {"frame_id": -1, "pause_generation": 1,
                 "expression": "items[0]"},
                {"frame_id": 0, "pause_generation": True,
                 "expression": "items[0]"},
                {"frame_id": 0, "pause_generation": 0,
                 "expression": "items[0]"},
                {"frame_id": 0, "pause_generation": 1, "expression": ""},
                {"frame_id": 0, "pause_generation": 1, "expression": " x"},
                {"frame_id": 0, "pause_generation": 1, "expression": "x\n"},
                {"frame_id": 0, "pause_generation": 1,
                 "expression": "x" * 1025},
            ]
            for offset, payload in enumerate(invalid_payloads):
                response = _raw_op(
                    session, 7000 + offset, "safe_eval_expression", payload
                )
                assert response.success is False
                assert response.result == {}
                assert response.error
            boundary = _raw_op(session, 7100, "safe_eval_expression", {
                "frame_id": 0, "pause_generation": 1,
                "expression": "x" * 1024,
            })
            assert boundary.success is False
            assert "Identifier" in boundary.error
            for offset, alias in enumerate(
                    ["eval", "evaluate", "safe_eval", "pdb_eval"]):
                response = _raw_op(session, 7200 + offset, alias, {})
                assert response.success is False
                assert response.result == {}
                assert "Unsupported operation" in response.error
            assert session.get_target_status()["state"] == "paused"
            assert session.ping().success is True
        finally:
            session.stop()

    def test_worker_is_alive_after_start(self, workspace):
        session = PdbSession(workspace)
        session.start()
        try:
            proc = session._proc
            assert proc is not None
            assert proc.poll() is None
        finally:
            session.stop()

    def test_ping_round_trip(self, workspace):
        session = PdbSession(workspace)
        session.start()
        try:
            response = session.ping()
            assert response.success is True
            assert response.result["status"] == "ok"
            assert response.result["pdb_created"] is True
            assert response.protocol_version == PROTOCOL_VERSION
            assert response.error == ""
        finally:
            session.stop()

    def test_multiple_sequential_pings(self, workspace):
        session = PdbSession(workspace)
        session.start()
        try:
            for i in range(5):
                response = session.ping()
                assert response.success is True
        finally:
            session.stop()

    def test_orderly_shutdown(self, workspace):
        session = PdbSession(workspace)
        session.start()
        assert session.is_alive is True
        session.stop()
        assert session.state == PdbSessionState.STOPPED
        assert session.is_alive is False

    def test_context_manager_shutdown(self, workspace):
        with PdbSession(workspace) as session:
            assert session.state == PdbSessionState.READY
            response = session.ping()
            assert response.success is True
        assert session.state == PdbSessionState.STOPPED

    def test_worker_terminated_then_session_cleanup(self, workspace):
        session = PdbSession(workspace)
        session.start()
        assert session.is_alive is True

        proc = session._proc
        assert proc is not None
        proc.kill()
        proc.wait(timeout=3.0)
        assert proc.poll() is not None
        time.sleep(0.2)

        session.stop()
        assert session.state == PdbSessionState.STOPPED

    def test_unknown_operation_via_wire(self, workspace):
        session = PdbSession(workspace)
        session.start()
        try:
            proc = session._proc
            assert proc is not None
            import json as _json
            raw = _json.dumps({
                "protocol_version": PROTOCOL_VERSION,
                "request_id": 42,
                "operation": "nonexistent",
                "payload": {},
            }, separators=(",", ":"))
            proc.stdin.write((raw + "\n").encode("utf-8"))
            proc.stdin.flush()
            line = session._response_queue.get(timeout=3.0)
            assert line is not None
            resp = deserialize_response(line)
            assert resp.success is False
            assert resp.request_id == 42
            assert "Unsupported operation" in resp.error
        finally:
            session.stop()

    def test_unknown_operation_unicode_via_wire(self, workspace):
        session = PdbSession(workspace)
        session.start()
        try:
            proc = session._proc
            assert proc is not None
            import json as _json
            raw = _json.dumps({
                "protocol_version": PROTOCOL_VERSION,
                "request_id": 77,
                "operation": "\u00e9trang\u00e9_ops",
                "payload": {},
            }, separators=(",", ":"))
            proc.stdin.write((raw + "\n").encode("utf-8"))
            proc.stdin.flush()
            line = session._response_queue.get(timeout=3.0)
            assert line is not None
            resp = deserialize_response(line)
            assert resp.success is False
            assert resp.request_id == 77
        finally:
            session.stop()

    def test_protocol_version_mismatch_via_wire(self, workspace):
        session = PdbSession(workspace)
        session.start()
        try:
            proc = session._proc
            assert proc is not None
            bad_req = PdbRequest(
                protocol_version=99,
                request_id=7,
                operation="ping",
                payload={},
            )
            data = serialize_request(bad_req)
            proc.stdin.write(data)
            proc.stdin.flush()
            line = session._response_queue.get(timeout=3.0)
            assert line is not None
            resp = deserialize_response(line)
            assert resp.success is False
            assert resp.request_id == 7
            assert "Unsupported protocol version" in resp.error
        finally:
            session.stop()

    def test_malformed_json_via_wire(self, workspace):
        session = PdbSession(workspace)
        session.start()
        try:
            proc = session._proc
            assert proc is not None
            proc.stdin.write(b"not json at all\n")
            proc.stdin.flush()
            line = session._response_queue.get(timeout=3.0)
            assert line is not None
            resp = deserialize_response(line)
            assert resp.success is False
            assert resp.request_id == 0
        finally:
            session.stop()

    def test_worker_exits_on_stdin_eof(self, workspace):
        from agentic_debugger.runtime.pdb_session import PdbSession as _Ps
        root = Path(_Ps._compute_project_root()).as_posix()
        bootstrap = (
            "import sys; import runpy; "
            "sys.path.insert(0, " + repr(root) + "); "
            "runpy.run_module('agentic_debugger.runtime.pdb_worker', run_name='__main__')"
        )
        proc = subprocess.Popen(
            [sys.executable, "-I", "-u", "-c", bootstrap],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        hello_data = serialize_request(
            PdbRequest(
                protocol_version=PROTOCOL_VERSION,
                request_id=1,
                operation="hello",
                payload={},
            )
        )
        proc.stdin.write(hello_data)
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line, "Expected hello response"
        proc.stdin.close()
        proc.wait(timeout=3.0)
        assert proc.returncode == 0

    def test_state_transitions(self, workspace):
        session = PdbSession(workspace)
        assert session.state == PdbSessionState.NEW
        session.start()
        assert session.state == PdbSessionState.READY
        session.stop()
        assert session.state == PdbSessionState.STOPPED

    def test_stop_is_idempotent(self, workspace):
        session = PdbSession(workspace)
        session.start()
        session.stop()
        assert session.state == PdbSessionState.STOPPED
        session.stop()
        assert session.state == PdbSessionState.STOPPED

    def test_ping_after_stop_raises_error(self, workspace):
        session = PdbSession(workspace)
        session.start()
        session.stop()
        with pytest.raises(PdbSessionStateError):
            session.ping()

    def test_diagnostics_available(self, workspace):
        session = PdbSession(workspace)
        session.start()
        try:
            diag = session.diagnostics
            assert isinstance(diag, str)
        finally:
            session.stop()

    def test_oversized_input_causes_worker_exit(self, workspace):
        session = PdbSession(workspace)
        session.start()
        try:
            proc = session._proc
            assert proc is not None
            big_line = b"x" * (MAX_LINE_LENGTH + 1) + b"\n"
            proc.stdin.write(big_line)
            proc.stdin.flush()
            line = session._response_queue.get(timeout=3.0)
            assert line is not None
            resp = deserialize_response(line)
            assert resp.success is False
            assert "exceeds maximum length" in resp.error
            proc.wait(timeout=3.0)
            assert proc.poll() is not None, "Worker must exit after oversized input"
        finally:
            session.stop()

    def test_multiple_oversized_no_recovery(self, workspace):
        session = PdbSession(workspace)
        session.start()
        try:
            proc = session._proc
            assert proc is not None
            big_line = b"x" * (MAX_LINE_LENGTH + 1) + b"\n"
            proc.stdin.write(big_line)
            proc.stdin.flush()
            line = session._response_queue.get(timeout=3.0)
            assert line is not None
            proc.wait(timeout=3.0)
            assert proc.poll() is not None
            with pytest.raises(PdbWorkerExitedError):
                session.ping()
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()

    def test_bootstrap_with_apostrophe_path(self, workspace):
        from agentic_debugger.runtime.pdb_session import PdbSession as _Ps
        root = "/Users/test_o'brien/project"
        bootstrap = (
            "import sys; import runpy; "
            "sys.path.insert(0, " + repr(root) + "); "
            "runpy.run_module('agentic_debugger.runtime.pdb_worker', run_name='__main__')"
        )
        assert "o'brien" in bootstrap
        assert repr(root) in bootstrap
        import ast
        ast.parse(bootstrap)

    def test_hello_handshake_validates_worker_info(self, workspace):
        session = PdbSession(workspace)
        session.start()
        try:
            proc = session._proc
            assert proc is not None
            assert proc.pid > 0
        finally:
            session.stop()

    def test_ping_response_contract(self, workspace):
        session = PdbSession(workspace)
        session.start()
        try:
            resp = session.ping()
            assert resp.success is True
            assert resp.error == ""
            assert resp.result["status"] == "ok"
            assert resp.result["pdb_created"] is True
        finally:
            session.stop()


class TestWorkerIsolationIntegration:
    @pytest.fixture
    def workspace_with_shadow(self):
        """Create workspace with files that would shadow the trusted worker."""
        src = Path(tempfile.mkdtemp())
        try:
            pkg_dir = src / "agentic_debugger" / "runtime"
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / "__init__.py").write_text("")
            shadow_marker = src / "SHADOW_EXECUTED.txt"

            shadow_worker = (
                "import sys; "
                f"open({str(shadow_marker)!r}, 'w').write('shadowed'); "
                "print('{\"protocol_version\":1,\"request_id\":1,\"success\":true,\"result\":{\"pid\":0},\"error\":\"\"}'); "
                "sys.stdout.flush(); "
                "sys.exit(0)"
            )
            (pkg_dir / "pdb_worker.py").write_text(shadow_worker)

            with TaskWorkspace(str(src)) as ws:
                yield ws, shadow_marker
        finally:
            shutil.rmtree(str(src), ignore_errors=True)

    def test_workspace_shadow_package_not_imported(self, workspace_with_shadow):
        ws, marker = workspace_with_shadow
        session = PdbSession(ws)
        session.start()
        try:
            resp = session.ping()
            assert resp.success is True
            assert not marker.exists(), (
                "Workspace-shadowing package was imported"
            )
        finally:
            session.stop()

    @pytest.fixture
    def workspace_with_sitecustomize(self):
        """Create workspace with a sitecustomize.py that would create a marker."""
        src = Path(tempfile.mkdtemp())
        try:
            marker = src / "SITECUSTOMIZE_EXECUTED.txt"
            (src / "sitecustomize.py").write_text(
                f"open({str(marker)!r}, 'w').write('executed')\n"
            )
            with TaskWorkspace(str(src)) as ws:
                yield ws, marker
        finally:
            shutil.rmtree(str(src), ignore_errors=True)

    def test_workspace_sitecustomize_not_executed(self, workspace_with_sitecustomize):
        ws, marker = workspace_with_sitecustomize
        session = PdbSession(ws)
        session.start()
        try:
            resp = session.ping()
            assert resp.success is True
            assert not marker.exists(), (
                "Workspace sitecustomize.py was executed"
            )
        finally:
            session.stop()


class TestRunToBreakpointIntegration:
    """Integration tests for run_to_breakpoint using real worker processes."""

    @pytest.fixture
    def ws_run(self):
        src = Path(tempfile.mkdtemp())
        try:
            (src / "target.py").write_text(_BREAKPOINT_SCRIPT)
            (src / "exit_early.py").write_text(_EXIT_EARLY_SCRIPT)
            (src / "sysexit.py").write_text(_SYSEXIT_SCRIPT)
            (src / "sysexit_none.py").write_text(_SYSEXIT_NONE_SCRIPT)
            (src / "sysexit_string.py").write_text(_SYSEXIT_STRING_SCRIPT)
            (src / "target_exc.py").write_text(_TARGET_EXCEPTION_SCRIPT)
            (src / "syntax_err.py").write_text(_SYNTAX_ERROR_SCRIPT)
            (src / "argv_test.py").write_text(_ARGV_SCRIPT)
            (src / "import_test.py").write_text(_IMPORT_SCRIPT)
            (src / "cwd_test.py").write_text(_CWD_SCRIPT)
            (src / "infinite.py").write_text(_INFINITE_LOOP_SCRIPT)
            (src / "print_test.py").write_text(_PRINT_SCRIPT)
            (src / "stderr_test.py").write_text(_STDERR_SCRIPT)
            (src / "input_test.py").write_text(_INPUT_SCRIPT)
            (src / "func_bp.py").write_text(_FUNCTION_BP_SCRIPT)
            (src / "multi_bp.py").write_text(_MULTI_BP_SCRIPT)
            (src / "argv_visible.py").write_text("import sys\nx = sys.argv\n")
            (src / "subdir").mkdir()
            (src / "subdir" / "__init__.py").write_text("")
            (src / "subdir" / "util.py").write_text("def util(): return 42\n")
            with TaskWorkspace(str(src)) as ws:
                yield ws
        finally:
            shutil.rmtree(str(src), ignore_errors=True)

    # 1. Breakpoint reached in a simple script
    def test_breakpoint_reached(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("target.py", [3])
            assert resp.success is True
            assert resp.result["status"] == "breakpoint"
            assert resp.result["line"] == 3
            assert resp.result["function"] == "<module>"
        finally:
            session.stop()

    # 2. Returned script, line and function are exact
    def test_breakpoint_exact_fields(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("target.py", [3])
            assert resp.result["script"] == "target.py"
            assert resp.result["line"] == 3
            assert resp.result["function"] == "<module>"
        finally:
            session.stop()

    # 3. Code after the breakpoint is not executed
    def test_code_after_breakpoint_not_executed(self, ws_run):
        script = "x = 1\ny = 2\nimport sys\nsys.exit(99)\nz = 3\n"
        (Path(ws_run.root) / "stop_bp.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("stop_bp.py", [3])
            assert resp.result["status"] == "breakpoint"
            assert resp.result["line"] == 3
        finally:
            session.stop()

    # 4. First of multiple configured breakpoints wins
    def test_first_breakpoint_wins(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("multi_bp.py", [2, 4])
            assert resp.result["status"] == "breakpoint"
            assert resp.result["line"] == 2
        finally:
            session.stop()

    def test_earlier_breakpoint_wins(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("multi_bp.py", [5, 2])
            assert resp.result["status"] == "breakpoint"
            assert resp.result["line"] == 2
        finally:
            session.stop()

    # 5. Program exits before any breakpoint
    def test_exit_before_breakpoint(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("exit_early.py", [4])
            assert resp.success is True
            assert resp.result["status"] == "exited"
            assert resp.result["exit_code"] == 0
        finally:
            session.stop()

    # 6. Normal exit code is 0
    def test_normal_exit_code(self, ws_run):
        script = "x = 1\ny = 2\nz = 3\n# end\n"
        (Path(ws_run.root) / "normal_exit.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("normal_exit.py", [4])
            assert resp.result["status"] == "exited"
            assert resp.result["exit_code"] == 0
        finally:
            session.stop()

    # 7. Integer SystemExit is preserved
    def test_sysexit_integer_preserved(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("sysexit.py", [3])
            assert resp.result["status"] == "exited"
            assert resp.result["exit_code"] == 42
        finally:
            session.stop()

    def test_sysexit_none_is_zero(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("sysexit_none.py", [3])
            assert resp.result["status"] == "exited"
            assert resp.result["exit_code"] == 0
        finally:
            session.stop()

    # 8. Non-integer SystemExit maps to 1
    def test_sysexit_string_maps_to_one(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("sysexit_string.py", [3])
            assert resp.result["status"] == "exited"
            assert resp.result["exit_code"] == 1
        finally:
            session.stop()

    # 9. Target syntax error produces a failed correlated response
    def test_syntax_error(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("syntax_err.py", [1])
            assert resp.success is False
            assert "SyntaxError" in resp.error or "syntax" in resp.error.lower()
        finally:
            session.stop()

    # 10. Target exception produces a failed correlated response
    def test_target_exception(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("target_exc.py", [4])
            assert resp.success is False
            assert "ValueError" in resp.error
        finally:
            session.stop()

    # 11. Worker remains pingable after breakpoint
    def test_ping_after_breakpoint(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            session.run_to_breakpoint("target.py", [3])
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 12. Worker remains pingable after normal exit
    def test_ping_after_exit(self, ws_run):
        script = "x = 1\ny = 2\nz = 3\n# end\n"
        (Path(ws_run.root) / "ping_exit.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            session.run_to_breakpoint("ping_exit.py", [4])
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 13. Worker remains pingable after target exception
    def test_ping_after_target_exception(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            session.run_to_breakpoint("target_exc.py", [1])
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 14. Second execution in the same session is rejected
    def test_second_execution_rejected(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            session.run_to_breakpoint("target.py", [3])
            with pytest.raises(PdbSessionStateError):
                session.run_to_breakpoint("target.py", [3])
        finally:
            session.stop()

    # 15. Target print() does not corrupt the protocol
    def test_target_print_does_not_corrupt(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("print_test.py", [2])
            assert resp.success is True
            assert resp.result["status"] == "breakpoint"
        finally:
            session.stop()

    # 16. Target stderr does not enter protocol diagnostics
    def test_target_stderr_isolated(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("stderr_test.py", [3])
            assert resp.success is True
        finally:
            session.stop()

    # 17. Target input() does not block indefinitely
    def test_target_input_does_not_block(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("input_test.py", [4])
            assert resp.success is True
            assert resp.result["status"] == "breakpoint"
        finally:
            session.stop()

    # 18. sys.argv is visible correctly
    def test_argv_correct(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("argv_visible.py", [1], argv=["a", "b"])
            assert resp.success is True
        finally:
            session.stop()

    # 19. Script-directory imports work
    def test_script_directory_imports(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("import_test.py", [4])
            assert resp.success is True
        finally:
            session.stop()

    # 20. Worker cwd and Python state are restored
    def test_worker_state_restored(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            before_cwd = os.getcwd()
            session.run_to_breakpoint("target.py", [3])
            after_cwd = os.getcwd()
            assert after_cwd == before_cwd
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    def test_worker_cwd_restored_after_exit(self, ws_run):
        script = "x = 1\ny = 2\nz = 3\n# end\n"
        (Path(ws_run.root) / "cwd_exit.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            before_cwd = os.getcwd()
            session.run_to_breakpoint("cwd_exit.py", [4])
            after_cwd = os.getcwd()
            assert after_cwd == before_cwd
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    def test_worker_cwd_restored_after_exception(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        try:
            before_cwd = os.getcwd()
            session.run_to_breakpoint("target_exc.py", [1])
            after_cwd = os.getcwd()
            assert after_cwd == before_cwd
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 21. Infinite-loop target triggers timeout and complete automatic cleanup
    def test_infinite_loop_timeout(self, ws_run):
        session = PdbSession(ws_run, request_timeout=1.0)
        session.start()
        try:
            proc = session._proc
            assert proc is not None
            with pytest.raises(PdbSessionTimeoutError):
                session.run_to_breakpoint("infinite.py", [3])
            assert session.state == PdbSessionState.FAILED
            assert session._proc is None
        finally:
            session.stop()

    # 22. Malformed result validation test (direct validation)
    def test_malformed_result_validation(self, ws_run):
        from agentic_debugger.runtime.pdb_session import PdbSession as _Ps
        bad_result = {"status": "breakpoint", "script": 123, "line": 5, "function": "main"}
        bad_resp = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=1,
            success=True,
            result=bad_result,
            error="",
        )
        with pytest.raises(PdbProtocolError, match="script must be"):
            _Ps._validate_run_result(bad_resp)

    # 23. Context-manager stop remains clean after a completed target run
    def test_context_manager_stop_after_run(self, ws_run):
        with PdbSession(ws_run) as session:
            resp = session.run_to_breakpoint("target.py", [3])
            assert resp.success is True
        assert session.state == PdbSessionState.STOPPED

    # 24. Repair 1: breakpoint sentinel inside ordinary except Exception
    def test_breakpoint_sentinel_not_swallowed_by_except_exception(self, ws_run):
        marker = Path(ws_run.root) / "sentinel_marker.txt"
        script = (
            "import sys\n"
            "try:\n"
            "    x = 1\n"
            "except Exception:\n"
            "    swallowed = True\n"
            "# after\n"
        )
        (Path(ws_run.root) / "sentinel_test.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("sentinel_test.py", [3])
            assert resp.success is True
            assert resp.result["status"] == "breakpoint"
            assert resp.result["line"] == 3
            assert marker.exists() is False
        finally:
            session.stop()

    # 25. Repair 3: [1, 999] on two-line file rejected
    def test_breakpoint_999_on_two_line_file(self, ws_run):
        script = "x = 1\n# end\n"
        (Path(ws_run.root) / "two_line.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            with pytest.raises(PdbProtocolError, match="exceeds source length"):
                session.run_to_breakpoint("two_line.py", [1, 999])
        finally:
            session.stop()

    # 26. Repair 4: .pdbrc not read during execution
    def test_no_pdbrc_read(self, ws_run):
        pdbrc_path = Path(ws_run.root) / ".pdbrc"
        pdbrc_path.write_text("raise RuntimeError('pdbrc was read')\n")
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("target.py", [3])
            assert resp.success is True
        finally:
            session.stop()

    # 27. Repair 5: saved trace restored
    def test_saved_trace_restored(self, ws_run):
        session = PdbSession(ws_run)
        session.start()
        proc = session._proc
        assert proc is not None
        import json as _json
        hello = _json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": 1,
            "operation": "hello",
            "payload": {},
        }, separators=(",", ":"))
        proc.stdin.write((hello + "\n").encode("utf-8"))
        proc.stdin.flush()
        line = session._response_queue.get(timeout=3.0)
        assert line is not None
        req = _json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": 2,
            "operation": "run_to_breakpoint",
            "payload": {"script": "target.py", "breakpoints": [3], "argv": []},
        }, separators=(",", ":"))
        proc.stdin.write((req + "\n").encode("utf-8"))
        proc.stdin.flush()
        line = session._response_queue.get(timeout=3.0)
        assert line is not None
        resp = deserialize_response(line)
        assert resp.success is True
        ping = session.ping()
        assert ping.success is True
        session.stop()

    # 28. Repair 6: KeyboardInterrupt as target failure
    def test_keyboard_interrupt_as_target_failure(self, ws_run):
        script = "x = 1\nraise KeyboardInterrupt()\n# end\n"
        (Path(ws_run.root) / "kbi_test.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("kbi_test.py", [3])
            assert resp.success is False
            assert "KeyboardInterrupt" in resp.error
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 29. Repair 6: custom BaseException as target failure
    def test_custom_baseexception_as_target_failure(self, ws_run):
        script = (
            "x = 1\n"
            "class CustomError(BaseException): pass\n"
            "raise CustomError('boom')\n"
            "# end\n"
        )
        (Path(ws_run.root) / "custom_be.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("custom_be.py", [4])
            assert resp.success is False
            assert "CustomError" in resp.error
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 30. Repair 7: UTF-8 BOM source
    def test_utf8_bom_source(self, ws_run):
        path = Path(ws_run.root) / "bom_test.py"
        path.write_bytes(b'\xef\xbb\xbfx = 1\n')
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("bom_test.py", [1])
            assert resp.success is True
            assert resp.result["status"] == "breakpoint"
        finally:
            session.stop()

    # 31. Repair 7: valid PEP 263 encoding cookie
    def test_encoding_cookie_source(self, ws_run):
        path = Path(ws_run.root) / "cookie_test.py"
        path.write_bytes('# -*- coding: latin-1 -*-\nx = "\xe9"\n'.encode('latin-1'))
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("cookie_test.py", [2])
            assert resp.success is True
            assert resp.result["status"] == "breakpoint"
        finally:
            session.stop()

    # 32. Repair 7: invalid source encoding fails gracefully
    def test_invalid_source_encoding(self, ws_run):
        path = Path(ws_run.root) / "bad_enc.py"
        path.write_bytes(b'# -*- coding: nonexistent -*-\nx = 1\n')
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("bad_enc.py", [1])
            assert resp.success is False
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 33. Repair 9: observational argv test with side-effect file
    def test_observational_argv(self, ws_run):
        marker = Path(ws_run.root) / "argv_marker.txt"
        script = (
            "import sys, json\n"
            f"with open({str(marker)!r}, 'w') as f:\n"
            "    json.dump(sys.argv, f)\n"
            "x = 1\n"
        )
        (Path(ws_run.root) / "obs_argv.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("obs_argv.py", [4], argv=["--flag", "value"])
            assert resp.success is True
            assert resp.result["status"] == "breakpoint"
            assert marker.exists()
            import json as _json
            argv = _json.loads(marker.read_text())
            assert len(argv) >= 1
            assert argv[0] == "obs_argv.py"
            assert argv[1:] == ["--flag", "value"]
        finally:
            session.stop()

    # 34. Repair 9: observational cwd restoration via direct side-effect
    def test_observational_cwd_restored(self, ws_run):
        marker = Path(ws_run.root) / "cwd_marker.txt"
        script = (
            "import os\n"
            f"with open({str(marker)!r}, 'w') as f:\n"
            "    f.write(os.getcwd())\n"
            "os.chdir('/' if os.name != 'nt' else '..')\n"
            "x = 1\n"
        )
        (Path(ws_run.root) / "obs_cwd.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("obs_cwd.py", [5])
            assert resp.success is True
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 35. Repair 9: stderr isolation with unique marker
    def test_observational_stderr_isolated(self, ws_run):
        import uuid
        marker = str(uuid.uuid4())
        script = (
            "import sys\n"
            f"sys.stderr.write({marker!r})\n"
            "x = 1\n"
        )
        (Path(ws_run.root) / "obs_stderr.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("obs_stderr.py", [3])
            assert resp.success is True
            assert marker not in session.diagnostics
        finally:
            session.stop()

    # 36. Repair 9: worker-side second execution rejection
    def test_worker_side_second_execution_rejected(self, ws_run):
        import json as _json
        session = PdbSession(ws_run)
        session.start()
        proc = session._proc
        assert proc is not None

        _req_id = [10]

        def send_raw(payload):
            _req_id[0] += 1
            rid = _req_id[0]
            req = _json.dumps({
                "protocol_version": PROTOCOL_VERSION,
                "request_id": rid,
                "operation": "run_to_breakpoint",
                "payload": payload,
            }, separators=(",", ":"))
            proc.stdin.write((req + "\n").encode("utf-8"))
            proc.stdin.flush()
            line = session._response_queue.get(timeout=3.0)
            return deserialize_response(line)

        resp1 = send_raw({"script": "target.py", "breakpoints": [3], "argv": []})
        assert resp1.success is True

        resp2 = send_raw({"script": "target.py", "breakpoints": [3], "argv": []})
        assert resp2.success is False
        assert "already completed" in resp2.error

        ping_req = _json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": _req_id[0] + 1,
            "operation": "ping",
            "payload": {},
        }, separators=(",", ":"))
        proc.stdin.write((ping_req + "\n").encode("utf-8"))
        proc.stdin.flush()
        line = session._response_queue.get(timeout=3.0)
        ping_resp = deserialize_response(line)
        assert ping_resp.success is True
        session.stop()

    # 37. Repair 9: side-effect file shows code after breakpoint not executed
    def test_code_after_breakpoint_side_effect(self, ws_run):
        marker = Path(ws_run.root) / "after_bp_marker.txt"
        script = (
            "x = 1\n"
            f"open({str(marker)!r}, 'w').write('should not exist')\n"
        )
        (Path(ws_run.root) / "after_bp.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("after_bp.py", [1])
            assert resp.success is True
            assert resp.result["status"] == "breakpoint"
            assert not marker.exists()
        finally:
            session.stop()

    # 38. Repair 1: breakpoint sentinel not caught by except Exception (bare except catches BaseException, but that's expected)
    def test_breakpoint_sentinel_not_caught_by_except_exception_bare(self, ws_run):
        marker = Path(ws_run.root) / "bare_except_marker.txt"
        script = (
            "try:\n"
            "    x = 1\n"
            "except Exception:\n"
            "    pass\n"
            f"open({str(marker)!r}, 'w').write('after')\n"
        )
        (Path(ws_run.root) / "bare_except_test.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("bare_except_test.py", [2])
            assert resp.success is True
            assert resp.result["status"] == "breakpoint"
            assert resp.result["line"] == 2
            assert not marker.exists()
        finally:
            session.stop()

    # 39. Repair 3: raw-worker protocol test for rooted/absolute path
    def test_worker_rejects_rooted_path(self, ws_run):
        import json as _json
        session = PdbSession(ws_run)
        session.start()
        proc = session._proc
        assert proc is not None
        for bad_path in ["/absolute/test.py", "\\rooted\\test.py", "C:/abs.py", "//unc/test.py"]:
            req = _json.dumps({
                "protocol_version": PROTOCOL_VERSION,
                "request_id": 99,
                "operation": "run_to_breakpoint",
                "payload": {"script": bad_path, "breakpoints": [1], "argv": []},
            }, separators=(",", ":"))
            proc.stdin.write((req + "\n").encode("utf-8"))
            proc.stdin.flush()
            line = session._response_queue.get(timeout=3.0)
            assert line is not None
            resp = deserialize_response(line)
            assert resp.success is False
            assert "relative path" in resp.error
        ping_req = _json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": 200,
            "operation": "ping",
            "payload": {},
        }, separators=(",", ":"))
        proc.stdin.write((ping_req + "\n").encode("utf-8"))
        proc.stdin.flush()
        line = session._response_queue.get(timeout=3.0)
        ping_resp = deserialize_response(line)
        assert ping_resp.success is True
        assert ping_resp.request_id == 200
        session.stop()

    # 40. Repair 4: invalid UTF-8 byte with coding cookie
    def test_invalid_utf8_byte_target(self, ws_run):
        path = Path(ws_run.root) / "bad_utf8.py"
        path.write_bytes(b"# coding: utf-8\nx = '\xff'\n")
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("bad_utf8.py", [2])
            assert resp.success is False
            assert "Internal worker error" not in resp.error
            assert "UnicodeDecodeError" in resp.error or "SyntaxError" in resp.error
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 41. Repair 5: unprintable BaseException.__str__()
    def test_unprintable_exception_str(self, ws_run):
        script = (
            "class Unprintable(BaseException):\n"
            "    def __str__(self):\n"
            "        raise KeyboardInterrupt()\n"
            "raise Unprintable()\n"
            "# end\n"
        )
        (Path(ws_run.root) / "unprintable.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("unprintable.py", [5])
            assert resp.success is False
            assert "<unprintable exception>" in resp.error
            assert "Unprintable" in resp.error
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 42. Repair 5: __str__ raising BaseException (KeyboardInterrupt)
    def test_str_raises_keyboard_interrupt(self, ws_run):
        script = (
            "class BadStr(BaseException):\n"
            "    def __str__(self):\n"
            "        raise KeyboardInterrupt()\n"
            "raise BadStr()\n"
            "# end\n"
        )
        (Path(ws_run.root) / "badstr_kbi.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("badstr_kbi.py", [5])
            assert resp.success is False
            assert "<unprintable exception>" in resp.error
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 43. Repair 5: ordinary RuntimeError still formats correctly
    def test_ordinary_runtime_error_still_formats(self, ws_run):
        script = "raise RuntimeError('test message')\n# end\n"
        (Path(ws_run.root) / "ordinary_err.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("ordinary_err.py", [2])
            assert resp.success is False
            assert "RuntimeError" in resp.error
            assert "test message" in resp.error
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 44. Repair 6: boolean SystemExit codes
    def test_sysexit_false_is_zero(self, ws_run):
        script = "import sys\nsys.exit(False)\n# end\n"
        (Path(ws_run.root) / "sysexit_false.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("sysexit_false.py", [3])
            assert resp.success is True
            assert resp.result["status"] == "exited"
            ec = resp.result["exit_code"]
            assert isinstance(ec, int)
            assert not isinstance(ec, bool)
            assert ec == 0
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    def test_sysexit_true_is_one(self, ws_run):
        script = "import sys\nsys.exit(True)\n# end\n"
        (Path(ws_run.root) / "sysexit_true.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("sysexit_true.py", [3])
            assert resp.success is True
            assert resp.result["status"] == "exited"
            ec = resp.result["exit_code"]
            assert isinstance(ec, int)
            assert not isinstance(ec, bool)
            assert ec == 1
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    def test_sysexit_negative_integer(self, ws_run):
        script = "import sys\nsys.exit(-3)\n# end\n"
        (Path(ws_run.root) / "sysexit_neg.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("sysexit_neg.py", [3])
            assert resp.success is True
            assert resp.result["status"] == "exited"
            ec = resp.result["exit_code"]
            assert isinstance(ec, int)
            assert not isinstance(ec, bool)
            assert ec == -3
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    def test_sysexit_object_maps_to_one(self, ws_run):
        script = "import sys\nsys.exit('bye')\n# end\n"
        (Path(ws_run.root) / "sysexit_obj.py").write_text(script)
        session = PdbSession(ws_run)
        session.start()
        try:
            resp = session.run_to_breakpoint("sysexit_obj.py", [3])
            assert resp.success is True
            assert resp.result["status"] == "exited"
            ec = resp.result["exit_code"]
            assert isinstance(ec, int)
            assert not isinstance(ec, bool)
            assert ec == 1
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 45. Failed-response structural validation
    def test_failed_response_with_nonempty_result_is_corruption(self, ws_run):
        import json as _json
        session = PdbSession(ws_run)
        session.start()
        proc = session._proc
        assert proc is not None
        req = _json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": 50,
            "operation": "run_to_breakpoint",
            "payload": {"script": "target.py", "breakpoints": [3], "argv": []},
        }, separators=(",", ":"))
        proc.stdin.write((req + "\n").encode("utf-8"))
        proc.stdin.flush()
        line = session._response_queue.get(timeout=3.0)
        worker_resp = deserialize_response(line)
        assert worker_resp.success is True
        bad_resp = _json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": 50,
            "success": False,
            "result": {"status": "exited", "script": "target.py", "exit_code": 0},
            "error": "some error",
        }, separators=(",", ":"))
        proc.stdin.write((bad_resp + "\n").encode("utf-8"))
        proc.stdin.flush()
        ready_resp = session._response_queue.get(timeout=3.0)
        assert ready_resp is not None
        # The first run_to_breakpoint already used the session, so second fails locally
        session.stop()

    # 46. Saved-trace identity test (direct worker level via raw protocol)
    def test_saved_trace_identity(self, ws_run):
        import json as _json
        session = PdbSession(ws_run)
        session.start()
        proc = session._proc
        assert proc is not None
        marker_py = Path(ws_run.root) / "trace_marker.py"
        marker_py.write_text(
            "import sys\n"
            "import json\n"
            "with open(sys.argv[1], 'w') as f:\n"
            "    json.dump({'trace': str(sys.gettrace())}, f)\n"
            "x = 1\n"
        )
        trace_marker = Path(ws_run.root) / "trace_out.json"
        req = _json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": 60,
            "operation": "run_to_breakpoint",
            "payload": {
                "script": "trace_marker.py",
                "breakpoints": [5],
                "argv": [str(trace_marker)],
            },
        }, separators=(",", ":"))
        proc.stdin.write((req + "\n").encode("utf-8"))
        proc.stdin.flush()
        line = session._response_queue.get(timeout=3.0)
        resp = deserialize_response(line)
        assert resp.success is True
        ping_req = _json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": 61,
            "operation": "ping",
            "payload": {},
        }, separators=(",", ":"))
        proc.stdin.write((ping_req + "\n").encode("utf-8"))
        proc.stdin.flush()
        line = session._response_queue.get(timeout=3.0)
        ping_resp = deserialize_response(line)
        assert ping_resp.success is True
        assert ping_resp.result["pdb_created"] is True
        session.stop()

    # 47. Worker-level second-run rejection with ping
    def test_worker_second_rejection_keeps_worker_alive(self, ws_run):
        import json as _json
        session = PdbSession(ws_run)
        session.start()
        proc = session._proc
        assert proc is not None

        def _raw_op(rid, op, payload):
            req = _json.dumps({
                "protocol_version": PROTOCOL_VERSION,
                "request_id": rid,
                "operation": op,
                "payload": payload,
            }, separators=(",", ":"))
            proc.stdin.write((req + "\n").encode("utf-8"))
            proc.stdin.flush()
            line = session._response_queue.get(timeout=3.0)
            return deserialize_response(line)

        r1 = _raw_op(70, "run_to_breakpoint",
                     {"script": "target.py", "breakpoints": [3], "argv": []})
        assert r1.success is True
        r2 = _raw_op(71, "run_to_breakpoint",
                     {"script": "target.py", "breakpoints": [3], "argv": []})
        assert r2.success is False
        assert "already completed" in r2.error
        r3 = _raw_op(72, "ping", {})
        assert r3.success is True
        session.stop()

    # 48a. Session TOCTOU: identity mismatch rejection
    def test_session_read_validated_rejects_identity_mismatch(self, ws_run):
        import os as _os
        session = PdbSession(ws_run)
        session.start()
        try:
            script = "x = 1\n"
            inside = Path(ws_run.root) / "identity_test.py"
            inside.write_text(script)
            normalized = "identity_test.py"

            from unittest.mock import patch as _patch
            with _patch("agentic_debugger.runtime.pdb_session.os.path.samestat",
                       return_value=False):
                with pytest.raises(PdbProtocolError, match="script file changed"):
                    session._read_validated_workspace_script(normalized)

            resp = session.ping()
            assert resp.success is True
        finally:
            session.stop()

    # 48b. Worker stable-source execution test
    def test_worker_stable_source_execution(self, ws_run):
        import os as _os
        from agentic_debugger.runtime.pdb_worker import PdbWorker

        marker = Path(ws_run.root) / "stable_source.py"
        original_content = "x = 1\n"
        marker.write_text(original_content)
        abs_path = _os.path.realpath(str(marker))
        source_bytes = marker.read_bytes()

        marker.write_text("x = 2\n")

        worker = PdbWorker()
        responses = []
        worker._send_response = lambda r: responses.append(r)

        worker._execute_target(
            script_normalized="stable_source.py",
            script_abs=abs_path,
            breakpoints=[1],
            argv=[],
            source_bytes=source_bytes,
            request_id=1,
        )
        assert len(responses) == 1
        assert responses[0].success is True

    # 48c. Session TOCTOU: samestat identity check
    def test_samestat_identity_check(self, ws_run):
        import os as _os
        a = Path(ws_run.root) / "file_a.py"
        a.write_text("x = 1\n")
        b = Path(ws_run.root) / "file_b.py"
        b.write_text("y = 2\n")

        stat_a = _os.stat(str(a))
        stat_b = _os.stat(str(b))
        assert not _os.path.samestat(stat_a, stat_b)

        binary_open_flag = getattr(_os, "O_BINARY", 0)
        fd = _os.open(str(a), _os.O_RDONLY | binary_open_flag)
        try:
            fstat_a = _os.fstat(fd)
            assert _os.path.samestat(stat_a, fstat_a)
        finally:
            _os.close(fd)

    # 48d. Worker rejects identity mismatch during read
    def test_worker_read_validated_rejects_identity_mismatch(self, ws_run):
        import os as _os
        from unittest.mock import patch as _patch
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        with _patch("agentic_debugger.runtime.pdb_worker.os.path.samestat",
                   return_value=False):
            worker = PdbWorker()
            result = worker._read_validated_workspace_script(
                "target.py", ws_run.root, 1
            )
            assert result is None

    # 48f. Worker read_bounded_fd short-read accumulation
    def test_worker_read_bounded_fd_short_reads(self, ws_run):
        import os as _os
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        from agentic_debugger.runtime.pdb_worker import _BINARY_OPEN_FLAG
        d = Path(ws_run.root)
        f = d / "worker_short_read.py"
        f.write_bytes(b"abc" + b"def" + b"ghi")
        fd = _os.open(str(f), _os.O_RDONLY | _BINARY_OPEN_FLAG)
        try:
            worker = PdbWorker()
            result = worker._read_bounded_fd(fd, 1)
            assert result == b"abcdefghi"
        finally:
            _os.close(fd)

    # 48g. Worker read_bounded_fd empty
    def test_worker_read_bounded_fd_empty(self, ws_run):
        import os as _os
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        from agentic_debugger.runtime.pdb_worker import _BINARY_OPEN_FLAG
        d = Path(ws_run.root)
        f = d / "worker_empty_read.py"
        f.write_bytes(b"")
        fd = _os.open(str(f), _os.O_RDONLY | _BINARY_OPEN_FLAG)
        try:
            worker = PdbWorker()
            result = worker._read_bounded_fd(fd, 1)
            assert result == b""
        finally:
            _os.close(fd)

    # 48h. Worker read_bounded_fd exact limit
    def test_worker_read_bounded_fd_exact_limit(self, ws_run):
        import os as _os
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        from agentic_debugger.runtime.pdb_worker import _BINARY_OPEN_FLAG
        from agentic_debugger.runtime.pdb_worker import _MAX_TARGET_SOURCE_BYTES
        d = Path(ws_run.root)
        f = d / "worker_exact_limit.py"
        f.write_bytes(b"x" * _MAX_TARGET_SOURCE_BYTES)
        fd = _os.open(str(f), _os.O_RDONLY | _BINARY_OPEN_FLAG)
        try:
            worker = PdbWorker()
            result = worker._read_bounded_fd(fd, 1)
            assert result is not None
            assert len(result) == _MAX_TARGET_SOURCE_BYTES
        finally:
            _os.close(fd)

    # 48i. Worker read_bounded_fd over limit
    def test_worker_read_bounded_fd_over_limit(self, ws_run):
        import os as _os
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        from agentic_debugger.runtime.pdb_worker import _BINARY_OPEN_FLAG
        from agentic_debugger.runtime.pdb_worker import _MAX_TARGET_SOURCE_BYTES
        d = Path(ws_run.root)
        f = d / "worker_over_limit.py"
        f.write_bytes(b"x" * (_MAX_TARGET_SOURCE_BYTES + 1))
        fd = _os.open(str(f), _os.O_RDONLY | _BINARY_OPEN_FLAG)
        try:
            worker = PdbWorker()
            result = worker._read_bounded_fd(fd, 1)
            assert result is None
        finally:
            _os.close(fd)

    # 48j. Session end-to-end over-limit rejection
    def test_session_over_limit_rejection(self, ws_run):
        import os as _os
        from agentic_debugger.runtime.pdb_session import _MAX_TARGET_SOURCE_BYTES
        d = Path(ws_run.root)
        f = d / "huge_test.py"
        f.write_bytes(b"x = 1\n" + b"# " + b"x" * (_MAX_TARGET_SOURCE_BYTES))
        session = PdbSession(ws_run)
        session.start()
        orig_send = session._send_and_receive
        send_calls = []
        session._send_and_receive = lambda r, t: (send_calls.append(1), orig_send(r, t))[1]
        try:
            with pytest.raises(PdbProtocolError, match="exceeds maximum source"):
                session.run_to_breakpoint("huge_test.py", [1])
            assert session._target_consumed is False
            assert len(send_calls) == 0
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 48k. Worker end-to-end over-limit rejection via raw protocol
    def test_worker_over_limit_rejection(self, ws_run):
        import json as _json
        from agentic_debugger.runtime.pdb_worker import _MAX_TARGET_SOURCE_BYTES
        d = Path(ws_run.root)
        f = d / "worker_huge.py"
        f.write_bytes(b"x = 1\n" + b"x" * (_MAX_TARGET_SOURCE_BYTES))
        session = PdbSession(ws_run)
        session.start()
        proc = session._proc
        assert proc is not None
        try:
            req = _json.dumps({
                "protocol_version": PROTOCOL_VERSION,
                "request_id": 90,
                "operation": "run_to_breakpoint",
                "payload": {"script": "worker_huge.py", "breakpoints": [1], "argv": []},
            }, separators=(",", ":"))
            proc.stdin.write((req + "\n").encode("utf-8"))
            proc.stdin.flush()
            line = session._response_queue.get(timeout=3.0)
            resp = deserialize_response(line)
            assert resp.success is False
            assert "exceeds maximum source" in resp.error
            ping_req = _json.dumps({
                "protocol_version": PROTOCOL_VERSION,
                "request_id": 91,
                "operation": "ping",
                "payload": {},
            }, separators=(",", ":"))
            proc.stdin.write((ping_req + "\n").encode("utf-8"))
            proc.stdin.flush()
            line = session._response_queue.get(timeout=3.0)
            ping_resp = deserialize_response(line)
            assert ping_resp.success is True
        finally:
            session.stop()

    # 48l. Confirm no direct os.O_BINARY in production or test Task 4B1 files
    def test_no_direct_o_binary_access_in_task4b_files(self, ws_run):
        import ast
        files = [
            Path(__file__).parent.parent.parent / "agentic_debugger" / "runtime" / "pdb_session.py",
            Path(__file__).parent.parent.parent / "agentic_debugger" / "runtime" / "pdb_worker.py",
            Path(__file__).parent.parent.parent / "tests" / "unit" / "test_pdb_session.py",
            Path(__file__).parent.parent.parent / "tests" / "integration" / "test_pdb_session_integration.py",
        ]
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "O_BINARY":
                    raise AssertionError(
                        f"Direct .O_BINARY access found in {path.name} at line {node.lineno}"
                    )

    # 48e. Worker stable-source: file changes after bytes captured
    def test_worker_stable_source_after_disk_change(self, ws_run):
        import os as _os
        from agentic_debugger.runtime.pdb_worker import PdbWorker

        marker = Path(ws_run.root) / "stable_source2.py"
        marker.write_text("x = 1\n")
        abs_path = _os.path.realpath(str(marker))
        source_bytes = marker.read_bytes()

        marker.write_text("raise ValueError('should not run')\n")

        worker = PdbWorker()
        responses = []
        worker._send_response = lambda r: responses.append(r)

        worker._execute_target(
            script_normalized="stable_source2.py",
            script_abs=abs_path,
            breakpoints=[99],
            argv=[],
            source_bytes=source_bytes,
            request_id=1,
        )
        assert len(responses) == 1
        assert responses[0].success is True
        assert responses[0].result["status"] == "exited"

    # 49. Worker accepts surrogate script path (raw JSON)
    def test_worker_rejects_surrogate_script(self, ws_run):
        import json as _json
        session = PdbSession(ws_run)
        session.start()
        proc = session._proc
        assert proc is not None
        req = _json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": 80,
            "operation": "run_to_breakpoint",
            "payload": {"script": "bad\ud800.py", "breakpoints": [1], "argv": []},
        }, separators=(",", ":"))
        proc.stdin.write((req + "\n").encode("utf-8"))
        proc.stdin.flush()
        line = session._response_queue.get(timeout=3.0)
        assert line is not None
        resp = deserialize_response(line)
        assert resp.success is False
        assert "non-UTF-8" in resp.error or "utf" in resp.error.lower()
        ping_req = _json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": 81,
            "operation": "ping",
            "payload": {},
        }, separators=(",", ":"))
        proc.stdin.write((ping_req + "\n").encode("utf-8"))
        proc.stdin.flush()
        line = session._response_queue.get(timeout=3.0)
        ping_resp = deserialize_response(line)
        assert ping_resp.success is True
        assert ping_resp.request_id == 81
        session.stop()

    # 49. Worker rejects surrogate argv entry (raw JSON)
    def test_worker_rejects_surrogate_argv(self, ws_run):
        import json as _json
        session = PdbSession(ws_run)
        session.start()
        proc = session._proc
        assert proc is not None
        req = _json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": 82,
            "operation": "run_to_breakpoint",
            "payload": {"script": "target.py", "breakpoints": [1], "argv": ["\ud800"]},
        }, separators=(",", ":"))
        proc.stdin.write((req + "\n").encode("utf-8"))
        proc.stdin.flush()
        line = session._response_queue.get(timeout=3.0)
        assert line is not None
        resp = deserialize_response(line)
        assert resp.success is False
        assert "non-UTF-8" in resp.error or "utf" in resp.error.lower()
        ping_req = _json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": 83,
            "operation": "ping",
            "payload": {},
        }, separators=(",", ":"))
        proc.stdin.write((ping_req + "\n").encode("utf-8"))
        proc.stdin.flush()
        line = session._response_queue.get(timeout=3.0)
        ping_resp = deserialize_response(line)
        assert ping_resp.success is True
        assert ping_resp.request_id == 83
        session.stop()

    # 50. Repairs 4+5: Saved-trace identity via direct _execute_target
    def test_saved_trace_identity_via_execute_target(self, ws_run):
        import os as _os, sys as _sys
        from agentic_debugger.runtime.pdb_worker import PdbWorker

        marker = Path(ws_run.root) / "trace_check.py"
        marker.write_text("x = 1\n")
        abs_path = _os.path.realpath(str(marker))

        tracer_log = []
        def sentinel_trace(frame, event, arg):
            tracer_log.append(event)
            return sentinel_trace

        saved_trace = _sys.gettrace()
        _sys.settrace(sentinel_trace)
        original_cwd = _os.getcwd()
        try:
            worker = PdbWorker()
            responses = []
            def collect_resp(resp):
                responses.append(resp)
            worker._send_response = collect_resp

            # Breakpoint outcome
            worker._execute_target(
                script_normalized="trace_check.py",
                script_abs=abs_path,
                breakpoints=[1],
                argv=[],
                source_bytes=marker.read_bytes(),
                request_id=1,
            )
            assert _sys.gettrace() is sentinel_trace, \
                f"trace after breakpoint: {_sys.gettrace()}"
            assert len(responses) == 1
            assert responses[0].success is True
            assert responses[0].result["status"] == "breakpoint"
            responses.clear()

            # Normal exit outcome
            worker._execute_target(
                script_normalized="trace_check.py",
                script_abs=abs_path,
                breakpoints=[99],
                argv=[],
                source_bytes=marker.read_bytes(),
                request_id=2,
            )
            assert _sys.gettrace() is sentinel_trace, \
                f"trace after exit: {_sys.gettrace()}"
            assert len(responses) == 1
            assert responses[0].success is True
            assert responses[0].result["status"] == "exited"
            responses.clear()

            # Target exception outcome
            exc_marker = Path(ws_run.root) / "exc_check.py"
            exc_marker.write_text("raise ValueError('test')\n")
            exc_abs = _os.path.realpath(str(exc_marker))

            worker._execute_target(
                script_normalized="exc_check.py",
                script_abs=exc_abs,
                breakpoints=[99],
                argv=[],
                source_bytes=exc_marker.read_bytes(),
                request_id=3,
            )
            assert _sys.gettrace() is sentinel_trace, \
                f"trace after exception: {_sys.gettrace()}"
            assert len(responses) == 1
            assert responses[0].success is False
        finally:
            _os.chdir(original_cwd)
            _sys.settrace(saved_trace)

    # 51. Worker-cwd restoration via direct _execute_target
    def test_worker_cwd_restoration_via_execute_target(self, ws_run):
        import os as _os
        from agentic_debugger.runtime.pdb_worker import PdbWorker

        original_cwd = _os.getcwd()

        def _run_cwd_test(dest_dir, bp_file, bp_line, expected_status):
            worker = PdbWorker()
            responses = []
            def collect_resp(resp):
                responses.append(resp)
            worker._send_response = collect_resp

            change_dir = str(dest_dir)
            script = (
                f"import os\n"
                f"os.chdir({change_dir!r})\n"
                f"x = 1\n"
            )
            marker = dest_dir / "cwd_test_worker.py"
            marker.write_text(script)
            abs_path = _os.path.realpath(str(marker))

            worker._execute_target(
                script_normalized=marker.name,
                script_abs=abs_path,
                breakpoints=bp_line,
                argv=[],
                source_bytes=marker.read_bytes(),
                request_id=1,
            )
            assert len(responses) == 1
            assert responses[0].success is True
            assert responses[0].result["status"] == expected_status

        other_dir = Path(ws_run.root) / "subdir_cwd"
        other_dir.mkdir(parents=True, exist_ok=True)

        saved = _os.getcwd()
        try:
            _run_cwd_test(other_dir, other_dir / "cwd_bp.py", [3], "breakpoint")
            assert _os.getcwd() == saved, "cwd not restored after breakpoint"

            _run_cwd_test(other_dir, other_dir / "cwd_exit.py", [99], "exited")
            assert _os.getcwd() == saved, "cwd not restored after exit"

            exc_dir = Path(ws_run.root) / "subdir_exc"
            exc_dir.mkdir(parents=True, exist_ok=True)
            exc_script = "import os\nos.chdir({!r})\nraise ValueError('test')\n".format(str(exc_dir))
            exc_file = exc_dir / "cwd_exc.py"
            exc_file.write_text(exc_script)
            exc_abs = _os.path.realpath(str(exc_file))

            worker = PdbWorker()
            exc_responses = []
            def collect_exc_resp(resp):
                exc_responses.append(resp)
            worker._send_response = collect_exc_resp

            worker._execute_target(
                script_normalized=exc_file.name,
                script_abs=exc_abs,
                breakpoints=[99],
                argv=[],
                source_bytes=exc_file.read_bytes(),
                request_id=1,
            )
            assert len(exc_responses) == 1
            assert exc_responses[0].success is False
            assert _os.getcwd() == saved, "cwd not restored after exception"
        finally:
            _os.chdir(saved)


# ──────────────────────────────────────────────────────────────────────
# Task 4B2A — Worker-Side Persistent Pause Lifecycle v1
# ──────────────────────────────────────────────────────────────────────


_SIMPLE_PAUSE_SCRIPT = (
    "x = 1\n"
    "y = 2\n"
    "z = 3\n"
    "# end\n"
)

_PAUSE_WITH_PRINT = (
    "print('hello from target')\n"
    "x = 42\n"
    "# end\n"
)

_PAUSE_WITH_STDERR = (
    "import sys\n"
    "sys.stderr.write('stderr from target\\n')\n"
    "x = 42\n"
    "# end\n"
)

_PAUSE_WITH_INPUT = (
    "try:\n"
    "    data = input()\n"
    "except EOFError:\n"
    "    x = 42\n"
    "else:\n"
    "    x = 0\n"
    "# end\n"
)

_PAUSE_FUNCTION_SCRIPT = (
    "def main():\n"
    "    x = 1\n"
    "    y = 2\n"
    "    z = 3\n"
    "\n"
    "def other():\n"
    "    pass\n"
    "\n"
    "main()\n"
)

_PAUSE_EXIT_EARLY = (
    "x = 1\n"
    "import sys\n"
    "sys.stdout.write('done')\n"
    "# end\n"
)

_PAUSE_SYSEXIT = (
    "import sys\n"
    "sys.exit(42)\n"
    "# end\n"
)

_PAUSE_TARGET_EXCEPTION = (
    "def main():\n"
    "    raise ValueError('example')\n"
    "main()\n"
    "# end\n"
)

_PAUSE_FINALLY_SCRIPT = (
    "import sys\n"
    "try:\n"
    "    x = 1\n"
    "finally:\n"
    "    with open(sys.argv[1], 'w') as f:\n"
    "        f.write('finally executed')\n"
    "# after\n"
)

_PAUSE_CODE_AFTER_SCRIPT = (
    "x = 1\n"
    "import sys\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    f.write('SHOULD_NOT_EXIST')\n"
    "# end\n"
)


def _raw_op(session, rid, op, payload):
    """Send a raw protocol operation to the worker and return the response."""
    import json as _json
    proc = session._proc
    assert proc is not None
    req = _json.dumps({
        "protocol_version": PROTOCOL_VERSION,
        "request_id": rid,
        "operation": op,
        "payload": payload,
    }, separators=(",", ":"))
    proc.stdin.write((req + "\n").encode("utf-8"))
    proc.stdin.flush()
    line = session._response_queue.get(timeout=5.0)
    return deserialize_response(line)


@pytest.fixture
def ws_pause():
    """Workspace fixture for persistent pause tests."""
    src = Path(tempfile.mkdtemp())
    try:
        (src / "pause_target.py").write_text(_SIMPLE_PAUSE_SCRIPT)
        (src / "pause_func.py").write_text(_PAUSE_FUNCTION_SCRIPT)
        (src / "pause_print.py").write_text(_PAUSE_WITH_PRINT)
        (src / "pause_stderr.py").write_text(_PAUSE_WITH_STDERR)
        (src / "pause_input.py").write_text(_PAUSE_WITH_INPUT)
        (src / "pause_exit_early.py").write_text(_PAUSE_EXIT_EARLY)
        (src / "pause_sysexit.py").write_text(_PAUSE_SYSEXIT)
        (src / "pause_exception.py").write_text(_PAUSE_TARGET_EXCEPTION)
        with TaskWorkspace(str(src)) as ws:
            yield ws
    finally:
        shutil.rmtree(str(src), ignore_errors=True)


class TestPersistentPauseRawProtocol:
    """Persistent pause lifecycle tests via raw protocol wires."""

    # ── 1/2/3/4. Start reaches first breakpoint, exact result, target pauses ──
    def test_start_paused_reaches_breakpoint(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 10, "start_paused_target", {
                "script": "pause_target.py", "breakpoints": [3], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "paused"
            assert resp.result["script"] == "pause_target.py"
            assert resp.result["line"] == 3
            assert resp.result["function"] == "<module>"
            assert set(resp.result.keys()) == {"state", "script", "line", "function"}
        finally:
            session.stop()

    # 5. Ping succeeds while paused
    def test_ping_while_paused(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 20, "start_paused_target", {
                "script": "pause_target.py", "breakpoints": [3], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "paused"
            ping = session.ping()
            assert ping.success is True
            assert ping.result["status"] == "ok"
        finally:
            session.stop()

    # 6. Status returns exact paused result
    def test_status_while_paused(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 30, "start_paused_target", {
                "script": "pause_target.py", "breakpoints": [3], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "paused"
            status = _raw_op(session, 31, "get_target_status", {})
            assert status.success is True
            assert status.result["state"] == "paused"
            assert status.result["script"] == "pause_target.py"
            assert status.result["line"] == 3
            assert status.result["function"] == "<module>"
        finally:
            session.stop()

    # 7. Target stdout before pause does not corrupt protocol
    def test_target_stdout_does_not_corrupt(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 40, "start_paused_target", {
                "script": "pause_print.py", "breakpoints": [2], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "paused"
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 8. Target stderr before pause does not enter diagnostics
    def test_target_stderr_isolated(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 50, "start_paused_target", {
                "script": "pause_stderr.py", "breakpoints": [3], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "paused"
            diag = session.diagnostics
            assert "stderr from target" not in diag
        finally:
            session.stop()

    # 9. Target input() cannot consume protocol requests
    def test_target_input_does_not_block_protocol(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 60, "start_paused_target", {
                "script": "pause_input.py", "breakpoints": [4], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "paused"
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 10. First of multiple configured breakpoints wins
    def test_first_breakpoint_wins(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 70, "start_paused_target", {
                "script": "pause_target.py", "breakpoints": [3, 4], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "paused"
            assert resp.result["line"] == 3
        finally:
            session.stop()

    # 11. No second breakpoint reached while paused (code after doesn't execute)
    def test_code_after_breakpoint_not_executed(self, ws_pause):
        marker = Path(ws_pause.root) / "pause_after_marker.txt"
        script = (
            "x = 1\n"
            f"open({str(marker)!r}, 'w').write('SHOULD_NOT_EXIST')\n"
            "# end\n"
        )
        (Path(ws_pause.root) / "pause_after.py").write_text(script)
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 80, "start_paused_target", {
                "script": "pause_after.py", "breakpoints": [1], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "paused"
            assert resp.result["line"] == 1
            assert not marker.exists()
        finally:
            session.stop()

    # 12/13/14. Terminate paused target successfully
    def test_terminate_paused_target(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 90, "start_paused_target", {
                "script": "pause_target.py", "breakpoints": [3], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "paused"
            term = _raw_op(session, 91, "terminate_paused_target", {})
            assert term.success is True
            assert term.result["state"] == "terminated"
            assert term.result["script"] == "pause_target.py"
        finally:
            session.stop()

    # 14. Status after termination
    def test_status_after_termination(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 100, "start_paused_target", {
                "script": "pause_target.py", "breakpoints": [3], "argv": [],
            })
            assert resp.success is True
            _raw_op(session, 101, "terminate_paused_target", {})
            status = _raw_op(session, 102, "get_target_status", {})
            assert status.success is True
            assert status.result["state"] == "terminated"
            assert status.result["script"] == "pause_target.py"
        finally:
            session.stop()

    # 15. Target finally executes during termination
    def test_target_finally_during_termination(self, ws_pause):
        marker = Path(ws_pause.root) / "pause_finally_marker.txt"
        marker_str = str(marker)
        (Path(ws_pause.root) / "pause_finally.py").write_text(
            "import sys\n"
            "try:\n"
            "    x = 1\n"
            "finally:\n"
            f"    with open({marker_str!r}, 'w') as f:\n"
            "        f.write('finally executed')\n"
            "# after\n"
        )
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 110, "start_paused_target", {
                "script": "pause_finally.py", "breakpoints": [3], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "paused"
            _raw_op(session, 111, "terminate_paused_target", {})
            assert marker.exists()
            assert marker.read_text() == "finally executed"
        finally:
            session.stop()

    # 16. Code after interrupted flow does not execute
    def test_code_after_interrupted_flow(self, ws_pause):
        marker = Path(ws_pause.root) / "pause_after_interrupt.txt"
        marker_str = str(marker)
        script = (
            "x = 1\n"
            f"with open({marker_str!r}, 'w') as f:\n"
            "    f.write('AFTER')\n"
            "# end\n"
        )
        (Path(ws_pause.root) / "pause_after_int.py").write_text(script)
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 120, "start_paused_target", {
                "script": "pause_after_int.py", "breakpoints": [1], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "paused"
            _raw_op(session, 121, "terminate_paused_target", {})
            assert not marker.exists()
        finally:
            session.stop()

    # 19. Ping succeeds after successful termination
    def test_ping_after_termination(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 130, "start_paused_target", {
                "script": "pause_target.py", "breakpoints": [3], "argv": [],
            })
            assert resp.success is True
            _raw_op(session, 131, "terminate_paused_target", {})
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 20. Second termination rejected while worker healthy
    def test_second_termination_rejected(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 140, "start_paused_target", {
                "script": "pause_target.py", "breakpoints": [3], "argv": [],
            })
            assert resp.success is True
            _raw_op(session, 141, "terminate_paused_target", {})
            term2 = _raw_op(session, 142, "terminate_paused_target", {})
            assert term2.success is False
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 21. Target exits before breakpoint
    def test_target_exits_before_breakpoint(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 150, "start_paused_target", {
                "script": "pause_exit_early.py", "breakpoints": [4], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "exited"
            assert resp.result["exit_code"] == 0
        finally:
            session.stop()

    # 22. Status after normal exit
    def test_status_after_normal_exit(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 160, "start_paused_target", {
                "script": "pause_exit_early.py", "breakpoints": [4], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "exited"
            status = _raw_op(session, 161, "get_target_status", {})
            assert status.success is True
            assert status.result["state"] == "exited"
            assert status.result["script"] == "pause_exit_early.py"
            assert status.result["exit_code"] == 0
        finally:
            session.stop()

    # 23. SystemExit normalization preserved
    def test_sysexit_integer_preserved(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 170, "start_paused_target", {
                "script": "pause_sysexit.py", "breakpoints": [3], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "exited"
            assert resp.result["exit_code"] == 42
        finally:
            session.stop()

    # 24. Target exception before breakpoint
    def test_target_exception_before_breakpoint(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 180, "start_paused_target", {
                "script": "pause_exception.py", "breakpoints": [4], "argv": [],
            })
            assert resp.success is False
            assert resp.result == {}
            assert "ValueError" in resp.error
        finally:
            session.stop()

    # 25. Status after target failure
    def test_status_after_failure(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 190, "start_paused_target", {
                "script": "pause_exception.py", "breakpoints": [4], "argv": [],
            })
            assert resp.success is False
            status = _raw_op(session, 191, "get_target_status", {})
            assert status.success is True
            assert status.result["state"] == "failed"
            assert status.result["script"] == "pause_exception.py"
            assert "ValueError" in status.result["error"]
        finally:
            session.stop()

    # 26. Worker pingable after exit
    def test_ping_after_exit(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 200, "start_paused_target", {
                "script": "pause_exit_early.py", "breakpoints": [4], "argv": [],
            })
            assert resp.success is True
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 27. Worker pingable after target failure
    def test_ping_after_failure(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp = _raw_op(session, 210, "start_paused_target", {
                "script": "pause_exception.py", "breakpoints": [4], "argv": [],
            })
            assert resp.success is False
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    # 28. Second persistent start rejected
    def test_second_persistent_start_rejected(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            resp1 = _raw_op(session, 220, "start_paused_target", {
                "script": "pause_target.py", "breakpoints": [3], "argv": [],
            })
            assert resp1.success is True
            assert resp1.result["state"] == "paused"
            resp2 = _raw_op(session, 221, "start_paused_target", {
                "script": "pause_target.py", "breakpoints": [3], "argv": [],
            })
            assert resp2.success is False
            assert "already completed" in resp2.error
            ping = session.ping()
            assert ping.success is True
            term = _raw_op(session, 222, "terminate_paused_target", {})
            assert term.success is True
        finally:
            session.stop()

    # 31. Concurrent duplicate raw start requests create exactly one target thread
    def test_concurrent_duplicate_start(self, ws_pause):
        """Send two start_paused_target rapidly; only one should start."""
        session = PdbSession(ws_pause)
        session.start()
        proc = session._proc
        assert proc is not None
        try:
            import json as _json
            req_body = _json.dumps({
                "protocol_version": PROTOCOL_VERSION,
                "request_id": 230,
                "operation": "start_paused_target",
                "payload": {"script": "pause_target.py", "breakpoints": [3], "argv": []},
            }, separators=(",", ":"))
            req_body2 = _json.dumps({
                "protocol_version": PROTOCOL_VERSION,
                "request_id": 231,
                "operation": "start_paused_target",
                "payload": {"script": "pause_target.py", "breakpoints": [3], "argv": []},
            }, separators=(",", ":"))
            proc.stdin.write((req_body + "\n").encode("utf-8"))
            proc.stdin.flush()
            proc.stdin.write((req_body2 + "\n").encode("utf-8"))
            proc.stdin.flush()
            line1 = session._response_queue.get(timeout=5.0)
            resp1 = deserialize_response(line1)
            line2 = session._response_queue.get(timeout=5.0)
            resp2 = deserialize_response(line2)
            successes = sum([resp1.success, resp2.success])
            assert successes == 1
            _raw_op(session, 232, "terminate_paused_target", {})
        finally:
            session.stop()

    # 35. Status before any target is idle
    def test_status_idle_before_target(self, ws_pause):
        session = PdbSession(ws_pause)
        session.start()
        try:
            status = _raw_op(session, 240, "get_target_status", {})
            assert status.success is True
            assert status.result["state"] == "idle"
        finally:
            session.stop()


class TestPersistentPauseOneTargetRule:
    """One-target rule tests: start_paused_target and run_to_breakpoint mutual exclusion."""

    @pytest.fixture
    def ws_onetarget(self):
        src = Path(tempfile.mkdtemp())
        try:
            (src / "target.py").write_text("x = 1\ny = 2\nz = 3\n")
            (src / "bp_target.py").write_text("x = 1\ny = 2\nz = 3\n")
            with TaskWorkspace(str(src)) as ws:
                yield ws
        finally:
            shutil.rmtree(str(src), ignore_errors=True)

    # 29. Persistent start after run_to_breakpoint rejected
    def test_persistent_start_after_run_to_breakpoint(self, ws_onetarget):
        session = PdbSession(ws_onetarget)
        session.start()
        try:
            resp = session.run_to_breakpoint("target.py", [3])
            assert resp.success is True
            resp2 = _raw_op(session, 310, "start_paused_target", {
                "script": "bp_target.py", "breakpoints": [3], "argv": [],
            })
            assert resp2.success is False
            assert "already completed" in resp2.error
        finally:
            session.stop()

    # 30. run_to_breakpoint after persistent start rejected
    def test_run_to_breakpoint_after_persistent_start(self, ws_onetarget):
        session = PdbSession(ws_onetarget)
        session.start()
        try:
            resp = _raw_op(session, 320, "start_paused_target", {
                "script": "bp_target.py", "breakpoints": [3], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "paused"
            resp2 = _raw_op(session, 321, "run_to_breakpoint", {
                "script": "target.py", "breakpoints": [3], "argv": [],
            })
            assert resp2.success is False
            assert "already completed" in resp2.error
            _raw_op(session, 322, "terminate_paused_target", {})
        finally:
            session.stop()


class TestPersistentPauseShutdownAndCleanup:
    """Shutdown and cleanup while target is paused."""

    @pytest.fixture
    def ws_cleanup(self):
        src = Path(tempfile.mkdtemp())
        try:
            (src / "pause_target.py").write_text(_SIMPLE_PAUSE_SCRIPT)
            with TaskWorkspace(str(src)) as ws:
                yield ws
        finally:
            shutil.rmtree(str(src), ignore_errors=True)

    # 32. PdbSession.stop() while target paused terminates/reaps worker
    def test_stop_while_paused(self, ws_cleanup):
        session = PdbSession(ws_cleanup)
        session.start()
        resp = _raw_op(session, 340, "start_paused_target", {
            "script": "pause_target.py", "breakpoints": [3], "argv": [],
        })
        assert resp.success is True
        assert resp.result["state"] == "paused"
        session.stop()
        assert session.state == PdbSessionState.STOPPED

    # 33. Context-manager exit while target paused cleans the worker
    def test_context_manager_while_paused(self, ws_cleanup):
        with PdbSession(ws_cleanup) as session:
            resp = _raw_op(session, 350, "start_paused_target", {
                "script": "pause_target.py", "breakpoints": [3], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "paused"
        assert session.state == PdbSessionState.STOPPED


class TestPersistentPauseStateRestoration:
    """Process-global state restoration after termination, exit, failure via direct worker tests."""

    def test_sys_argv_restored_after_termination(self):
        import os as _os, sys as _sys, threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        d = Path(tempfile.mkdtemp())
        try:
            (d / "state_test.py").write_text("x = 1\n")
            abs_path = _os.path.realpath(str(d / "state_test.py"))
            saved_argv = list(_sys.argv)
            saved_cwd = _os.getcwd()
            try:
                worker = PdbWorker()
                responses = []
                worker._send_response = lambda r: responses.append(r)
                worker._target_started = True
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'state_test.py'
                    worker._lifecycle['script'] = 'state_test.py'
                thread = _threading.Thread(
                    target=worker._execute_target_persistent,
                    args=('state_test.py', abs_path, [1], [], b"x = 1\n"),
                    daemon=True,
                )
                thread.start()
                with worker._condition:
                    while worker._lifecycle['state'] == 'starting':
                        worker._condition.wait()
                    assert worker._lifecycle['state'] == 'paused'
                with worker._condition:
                    worker._lifecycle['state'] = 'terminating'
                    worker._condition.notify_all()
                thread.join(timeout=3.0)
                assert list(_sys.argv) == saved_argv
            finally:
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)

    def test_sys_path_restored_after_exit(self):
        import os as _os, sys as _sys, threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        d = Path(tempfile.mkdtemp())
        try:
            (d / "path_test.py").write_text("x = 1\n")
            abs_path = _os.path.realpath(str(d / "path_test.py"))
            saved_path = list(_sys.path)
            saved_cwd = _os.getcwd()
            try:
                worker = PdbWorker()
                responses = []
                worker._send_response = lambda r: responses.append(r)
                worker._target_started = True
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'path_test.py'
                    worker._lifecycle['script'] = 'path_test.py'
                thread = _threading.Thread(
                    target=worker._execute_target_persistent,
                    args=('path_test.py', abs_path, [99], [], b"x = 1\n"),
                    daemon=True,
                )
                thread.start()
                thread.join(timeout=3.0)
                with worker._condition:
                    state = worker._lifecycle['state']
                assert state in ('exited',)
                assert list(_sys.path) == saved_path
            finally:
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)

    def test_std_streams_restored_after_failure(self):
        import os as _os, sys as _sys, threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        d = Path(tempfile.mkdtemp())
        try:
            (d / "fail_test.py").write_text("raise ValueError('test')\n")
            abs_path = _os.path.realpath(str(d / "fail_test.py"))
            saved_stdin = _sys.stdin
            saved_stdout = _sys.stdout
            saved_stderr = _sys.stderr
            saved_cwd = _os.getcwd()
            try:
                worker = PdbWorker()
                responses = []
                worker._send_response = lambda r: responses.append(r)
                worker._target_started = True
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'fail_test.py'
                    worker._lifecycle['script'] = 'fail_test.py'
                thread = _threading.Thread(
                    target=worker._execute_target_persistent,
                    args=('fail_test.py', abs_path, [99], [], b"raise ValueError('test')\n"),
                    daemon=True,
                )
                thread.start()
                thread.join(timeout=3.0)
                assert _sys.stdin is saved_stdin
                assert _sys.stdout is saved_stdout
                assert _sys.stderr is saved_stderr
            finally:
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)

    def test_cwd_restored_after_termination(self):
        import os as _os, sys as _sys, threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        d = Path(tempfile.mkdtemp())
        try:
            (d / "cwd_test.py").write_text("x = 1\n")
            abs_path = _os.path.realpath(str(d / "cwd_test.py"))
            saved_cwd = _os.getcwd()
            try:
                other_dir = d / "subdir"
                other_dir.mkdir(exist_ok=True)
                worker = PdbWorker()
                responses = []
                worker._send_response = lambda r: responses.append(r)
                worker._target_started = True
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'cwd_test.py'
                    worker._lifecycle['script'] = 'cwd_test.py'
                thread = _threading.Thread(
                    target=worker._execute_target_persistent,
                    args=('cwd_test.py', abs_path, [1], [], b"x = 1\n"),
                    daemon=True,
                )
                thread.start()
                with worker._condition:
                    while worker._lifecycle['state'] == 'starting':
                        worker._condition.wait()
                    assert worker._lifecycle['state'] == 'paused'
                _os.chdir(str(other_dir))
                with worker._condition:
                    worker._lifecycle['state'] = 'terminating'
                    worker._condition.notify_all()
                thread.join(timeout=3.0)
                assert _os.getcwd() == saved_cwd
            finally:
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)

    def test_trace_restored_after_exit(self):
        import os as _os, sys as _sys, threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        d = Path(tempfile.mkdtemp())
        try:
            (d / "trace_test.py").write_text("x = 1\n")
            abs_path = _os.path.realpath(str(d / "trace_test.py"))
            saved_trace = _sys.gettrace()
            saved_cwd = _os.getcwd()
            tracer_log = []
            def sentinel_trace(frame, event, arg):
                tracer_log.append(event)
                return sentinel_trace
            try:
                _sys.settrace(sentinel_trace)
                worker = PdbWorker()
                responses = []
                worker._send_response = lambda r: responses.append(r)
                worker._target_started = True
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'trace_test.py'
                    worker._lifecycle['script'] = 'trace_test.py'
                thread = _threading.Thread(
                    target=worker._execute_target_persistent,
                    args=('trace_test.py', abs_path, [99], [], b"x = 1\n"),
                    daemon=True,
                )
                thread.start()
                thread.join(timeout=3.0)
                assert _sys.gettrace() is sentinel_trace
            finally:
                _sys.settrace(saved_trace)
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)


class TestPersistentPauseMalformedRequests:
    """Malformed request validation for persistent lifecycle operations."""

    @pytest.fixture
    def ws_malformed(self):
        src = Path(tempfile.mkdtemp())
        try:
            (src / "target.py").write_text("x = 1\ny = 2\nz = 3\n# end\n")
            (src / "exit_early.py").write_text("x = 1\nimport sys\nsys.stdout.write('done')\n# end\n")
            with TaskWorkspace(str(src)) as ws:
                yield ws
        finally:
            shutil.rmtree(str(src), ignore_errors=True)

    def test_start_missing_script(self, ws_malformed):
        session = PdbSession(ws_malformed)
        session.start()
        try:
            resp = _raw_op(session, 400, "start_paused_target", {
                "breakpoints": [1], "argv": [],
            })
            assert resp.success is False
            assert "Missing" in resp.error
        finally:
            session.stop()

    def test_start_missing_breakpoints(self, ws_malformed):
        session = PdbSession(ws_malformed)
        session.start()
        try:
            resp = _raw_op(session, 410, "start_paused_target", {
                "script": "target.py", "argv": [],
            })
            assert resp.success is False
            assert "Missing" in resp.error
        finally:
            session.stop()

    def test_start_missing_argv(self, ws_malformed):
        session = PdbSession(ws_malformed)
        session.start()
        try:
            resp = _raw_op(session, 420, "start_paused_target", {
                "script": "target.py", "breakpoints": [1],
            })
            assert resp.success is False
            assert "Missing" in resp.error
        finally:
            session.stop()

    def test_start_unknown_field(self, ws_malformed):
        session = PdbSession(ws_malformed)
        session.start()
        try:
            resp = _raw_op(session, 430, "start_paused_target", {
                "script": "target.py", "breakpoints": [1], "argv": [], "extra": 42,
            })
            assert resp.success is False
            assert "Unknown" in resp.error
        finally:
            session.stop()

    def test_start_invalid_script_type(self, ws_malformed):
        session = PdbSession(ws_malformed)
        session.start()
        try:
            resp = _raw_op(session, 440, "start_paused_target", {
                "script": 123, "breakpoints": [1], "argv": [],
            })
            assert resp.success is False
        finally:
            session.stop()

    def test_status_with_unknown_field_rejected(self, ws_malformed):
        session = PdbSession(ws_malformed)
        session.start()
        try:
            resp = _raw_op(session, 450, "get_target_status", {"unknown": "value"})
            assert resp.success is False
            assert "Unknown" in resp.error
        finally:
            session.stop()

    def test_terminate_with_unknown_field_rejected(self, ws_malformed):
        session = PdbSession(ws_malformed)
        session.start()
        try:
            resp = _raw_op(session, 460, "terminate_paused_target", {"unknown": "value"})
            assert resp.success is False
            assert "Unknown" in resp.error
        finally:
            session.stop()

    def test_terminate_when_idle_rejected(self, ws_malformed):
        session = PdbSession(ws_malformed)
        session.start()
        try:
            resp = _raw_op(session, 470, "terminate_paused_target", {})
            assert resp.success is False
            assert "Cannot terminate" in resp.error
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()

    def test_terminate_after_exit_rejected(self, ws_malformed):
        session = PdbSession(ws_malformed)
        session.start()
        try:
            sysexit_py = (Path(ws_malformed.root) / "sysexit.py")
            sysexit_py.write_text("import sys\nsys.exit(0)\nx = 1\n# end\n")
            resp = _raw_op(session, 480, "start_paused_target", {
                "script": "sysexit.py", "breakpoints": [3], "argv": [],
            })
            assert resp.success is True
            assert resp.result["state"] == "exited"
            term = _raw_op(session, 481, "terminate_paused_target", {})
            assert term.success is False
            assert "Cannot terminate" in term.error
            ping = session.ping()
            assert ping.success is True
        finally:
            session.stop()


class TestPersistentPauseOneShotStatus:
    """One-shot run_to_breakpoint lifecycle status coherence (Repair 4)."""

    @pytest.fixture
    def ws_oneshot(self):
        src = Path(tempfile.mkdtemp())
        try:
            (src / "target.py").write_text("x = 1\ny = 2\nz = 3\n")
            (src / "sysexit.py").write_text("import sys\nsys.exit(42)\n# end\n")
            (src / "fail.py").write_text("raise ValueError('test')\n# end\n")
            with TaskWorkspace(str(src)) as ws:
                yield ws
        finally:
            shutil.rmtree(str(src), ignore_errors=True)

    def test_breakpoint_status_after_oneshot(self, ws_oneshot):
        session = PdbSession(ws_oneshot)
        session.start()
        try:
            resp = session.run_to_breakpoint("target.py", [3])
            assert resp.success is True
            assert resp.result["status"] == "breakpoint"
            status = _raw_op(session, 510, "get_target_status", {})
            assert status.success is True
            assert status.result["state"] == "terminated"
            assert status.result["script"] == "target.py"
        finally:
            session.stop()

    def test_exit_status_after_oneshot(self, ws_oneshot):
        session = PdbSession(ws_oneshot)
        session.start()
        try:
            resp = session.run_to_breakpoint("sysexit.py", [3])
            assert resp.success is True
            assert resp.result["status"] == "exited"
            status = _raw_op(session, 520, "get_target_status", {})
            assert status.success is True
            assert status.result["state"] == "exited"
            assert status.result["exit_code"] == 42
        finally:
            session.stop()

    def test_failure_status_after_oneshot(self, ws_oneshot):
        session = PdbSession(ws_oneshot)
        session.start()
        try:
            resp = session.run_to_breakpoint("fail.py", [2])
            assert resp.success is False
            assert "ValueError" in resp.error
            status = _raw_op(session, 530, "get_target_status", {})
            assert status.success is True
            assert status.result["state"] == "failed"
            assert "ValueError" in status.result["error"]
        finally:
            session.stop()


class TestPersistentPauseHangingFinally:
    """Deterministic hanging-finally timeout coverage.

    The target pauses inside a try block. Controlled termination raises the
    private termination sentinel, which unwinds the target stack and enters its
    finally block. The finally block waits on a test-controlled Event, allowing
    the test to deterministically hold the target thread past the bounded
    termination timeout.
    """

    def test_explicit_terminate_hanging_finally_one_response(self):
        import os as _os, sys as _sys, threading as _threading
        import builtins as _builtins
        from agentic_debugger.runtime import pdb_worker as _pw
        from agentic_debugger.runtime.pdb_protocol import PdbRequest, PROTOCOL_VERSION
        d = Path(tempfile.mkdtemp())
        saved_timeout = _pw._WORKER_TERMINATION_TIMEOUT
        saved_cwd = _os.getcwd()
        event = _threading.Event()
        thread = None
        worker = None
        responses = []
        try:
            (d / "hang_finally.py").write_text(
                "import builtins\ntry:\n    x = 1\nfinally:\n    builtins._task4b2a_release_event.wait()\n# end\n"
            )
            abs_path = _os.path.realpath(str(d / "hang_finally.py"))
            source = b"import builtins\ntry:\n    x = 1\nfinally:\n    builtins._task4b2a_release_event.wait()\n# end\n"
            _builtins._task4b2a_release_event = event
            worker = _pw.PdbWorker()
            worker._send_response = lambda r: responses.append(r)
            worker._target_started = True
            with worker._condition:
                worker._lifecycle['state'] = 'starting'
                worker._lifecycle['_start_script'] = 'hang_finally.py'
                worker._lifecycle['script'] = 'hang_finally.py'
            thread = _threading.Thread(
                target=worker._execute_target_persistent,
                args=('hang_finally.py', abs_path, [3], [], source),
                daemon=True,
            )
            worker._target_thread = thread
            thread.start()
            with worker._condition:
                while worker._lifecycle['state'] == 'starting':
                    worker._condition.wait()
            assert worker._lifecycle['state'] == 'paused'
            _pw._WORKER_TERMINATION_TIMEOUT = 0.05
            worker._handle_terminate_paused_target(PdbRequest(
                protocol_version=PROTOCOL_VERSION, request_id=42,
                operation='terminate_paused_target', payload={},
            ))
            assert len(responses) == 1
            resp = responses[0]
            assert resp.request_id == 42
            assert resp.success is False
            assert resp.result == {}
            assert "timeout" in resp.error or "termination" in resp.error
            assert worker._unsafe is True
            assert worker._running is False
            assert thread.is_alive()
            assert worker._lifecycle['state'] == 'terminating'
        finally:
            event.set()
            _pw._WORKER_TERMINATION_TIMEOUT = saved_timeout
            if thread is not None:
                thread.join(timeout=3.0)
            if hasattr(_builtins, '_task4b2a_release_event'):
                del _builtins._task4b2a_release_event
            if worker is not None:
                _os.chdir(saved_cwd)
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)
        assert not thread.is_alive()
        assert len(responses) == 1

    def test_shutdown_hanging_finally_one_response(self):
        import os as _os, sys as _sys, threading as _threading
        import builtins as _builtins
        from agentic_debugger.runtime import pdb_worker as _pw
        from agentic_debugger.runtime.pdb_protocol import PdbRequest, PROTOCOL_VERSION
        d = Path(tempfile.mkdtemp())
        saved_timeout = _pw._WORKER_TERMINATION_TIMEOUT
        saved_cwd = _os.getcwd()
        event = _threading.Event()
        thread = None
        worker = None
        responses = []
        try:
            (d / "shutdown_hang.py").write_text(
                "import builtins\ntry:\n    x = 1\nfinally:\n    builtins._task4b2a_release_event.wait()\n# end\n"
            )
            abs_path = _os.path.realpath(str(d / "shutdown_hang.py"))
            source = b"import builtins\ntry:\n    x = 1\nfinally:\n    builtins._task4b2a_release_event.wait()\n# end\n"
            _builtins._task4b2a_release_event = event
            worker = _pw.PdbWorker()
            worker._send_response = lambda r: responses.append(r)
            worker._target_started = True
            with worker._condition:
                worker._lifecycle['state'] = 'starting'
                worker._lifecycle['_start_script'] = 'shutdown_hang.py'
                worker._lifecycle['script'] = 'shutdown_hang.py'
            thread = _threading.Thread(
                target=worker._execute_target_persistent,
                args=('shutdown_hang.py', abs_path, [3], [], source),
                daemon=True,
            )
            worker._target_thread = thread
            thread.start()
            with worker._condition:
                while worker._lifecycle['state'] == 'starting':
                    worker._condition.wait()
            assert worker._lifecycle['state'] == 'paused'
            _pw._WORKER_TERMINATION_TIMEOUT = 0.05
            worker._handle_shutdown(PdbRequest(
                protocol_version=PROTOCOL_VERSION, request_id=99,
                operation='shutdown', payload={},
            ))
            assert len(responses) == 1
            resp = responses[0]
            assert resp.request_id == 99
            assert resp.success is False
            assert resp.result == {}
            assert "timeout" in resp.error or "termination" in resp.error
            assert worker._unsafe is True
            assert worker._running is False
            assert thread.is_alive()
            assert worker._lifecycle['state'] == 'terminating'
        finally:
            event.set()
            _pw._WORKER_TERMINATION_TIMEOUT = saved_timeout
            if thread is not None:
                thread.join(timeout=3.0)
            if hasattr(_builtins, '_task4b2a_release_event'):
                del _builtins._task4b2a_release_event
            if worker is not None:
                _os.chdir(saved_cwd)
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)
        assert not thread.is_alive()
        assert len(responses) == 1


class TestPersistentPauseStartHandlerThread:
    """Start-handler thread cleanup after exit and failure via _handle_start_paused_target (Repair 3)."""

    def test_start_handler_clears_thread_after_exit(self):
        import os as _os, sys as _sys, threading as _threading
        from agentic_debugger.runtime import pdb_worker as _pw
        from agentic_debugger.runtime.pdb_protocol import PdbRequest, PROTOCOL_VERSION
        d = Path(tempfile.mkdtemp())
        try:
            (d / "exit_target.py").write_text("x = 1\ny = 2\nz = 3\n# end\n" + "# padding\n" * 100)
            saved_cwd = _os.getcwd()
            _os.chdir(str(d))
            try:
                captured_threads = []
                saved_thread_factory = _pw.threading.Thread
                def recording_thread(*a, **kw):
                    t = saved_thread_factory(*a, **kw)
                    captured_threads.append(t)
                    return t
                _pw.threading.Thread = recording_thread
                worker = _pw.PdbWorker()
                responses = []
                worker._send_response = lambda r: responses.append(r)
                worker._handle_start_paused_target(PdbRequest(
                    protocol_version=PROTOCOL_VERSION, request_id=10,
                    operation='start_paused_target',
                    payload={"script": "exit_target.py", "breakpoints": [99], "argv": []},
                ))
                assert len(responses) == 1
                resp = responses[0]
                assert resp.success is True
                assert resp.result["state"] == "exited"
                assert worker._target_thread is None
                assert len(captured_threads) == 1
                assert not captured_threads[0].is_alive()
            finally:
                _pw.threading.Thread = saved_thread_factory
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)

    def test_start_handler_clears_thread_after_failure(self):
        import os as _os, sys as _sys, threading as _threading
        from agentic_debugger.runtime import pdb_worker as _pw
        from agentic_debugger.runtime.pdb_protocol import PdbRequest, PROTOCOL_VERSION
        d = Path(tempfile.mkdtemp())
        try:
            (d / "fail_target.py").write_text("raise ValueError('boom')\n# pad\n" * 50)
            saved_cwd = _os.getcwd()
            _os.chdir(str(d))
            try:
                captured_threads = []
                saved_thread_factory = _pw.threading.Thread
                def recording_thread(*a, **kw):
                    t = saved_thread_factory(*a, **kw)
                    captured_threads.append(t)
                    return t
                _pw.threading.Thread = recording_thread
                worker = _pw.PdbWorker()
                responses = []
                worker._send_response = lambda r: responses.append(r)
                worker._handle_start_paused_target(PdbRequest(
                    protocol_version=PROTOCOL_VERSION, request_id=11,
                    operation='start_paused_target',
                    payload={"script": "fail_target.py", "breakpoints": [99], "argv": []},
                ))
                assert len(responses) == 1
                resp = responses[0]
                assert resp.success is False
                assert "ValueError" in resp.error
                assert worker._target_thread is None
                assert len(captured_threads) == 1
                assert not captured_threads[0].is_alive()
            finally:
                _pw.threading.Thread = saved_thread_factory
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)


class TestPersistentPauseRunnerWeakRef:
    """Runner collectability via weak reference (Repair 4)."""

    def test_runner_retained_while_paused(self):
        import os as _os, threading as _threading, gc as _gc, weakref as _wr
        from agentic_debugger.runtime import pdb_worker as _pw
        d = Path(tempfile.mkdtemp())
        try:
            (d / "runner_test.py").write_text("x = 1\n")
            abs_path = _os.path.realpath(str(d / "runner_test.py"))
            saved_cwd = _os.getcwd()
            worker = _pw.PdbWorker()
            responses = []
            worker._send_response = lambda r: responses.append(r)
            worker._target_started = True
            runner_ref = [None]
            saved_runner_class = _pw._PdbPersistentRunner
            class CapturingRunner(saved_runner_class):
                def __new__(cls, *a, **kw):
                    inst = super().__new__(cls)
                    runner_ref[0] = _wr.ref(inst)
                    return inst
            _pw._PdbPersistentRunner = CapturingRunner
            try:
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'runner_test.py'
                    worker._lifecycle['script'] = 'runner_test.py'
                thread = _threading.Thread(
                    target=worker._execute_target_persistent,
                    args=('runner_test.py', abs_path, [1], [], b"x = 1\n"),
                    daemon=True,
                )
                worker._target_thread = thread
                thread.start()
                with worker._condition:
                    while worker._lifecycle['state'] == 'starting':
                        worker._condition.wait()
                    assert worker._lifecycle['state'] == 'paused'
                r = runner_ref[0]
                assert r is not None
                assert r() is not None, "Runner should be alive while paused"
                assert thread.is_alive()
                worker._request_target_termination()
                thread.join(timeout=3.0)
            finally:
                _pw._PdbPersistentRunner = saved_runner_class
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)

    def test_runner_collectable_after_exit(self):
        import os as _os, threading as _threading, gc as _gc, weakref as _wr
        from agentic_debugger.runtime import pdb_worker as _pw
        d = Path(tempfile.mkdtemp())
        try:
            (d / "runner_test.py").write_text("x = 1\n")
            abs_path = _os.path.realpath(str(d / "runner_test.py"))
            saved_cwd = _os.getcwd()
            worker = _pw.PdbWorker()
            responses = []
            worker._send_response = lambda r: responses.append(r)
            worker._target_started = True
            runner_ref = [None]
            saved_runner_class = _pw._PdbPersistentRunner
            class CapturingRunner(saved_runner_class):
                def __new__(cls, *a, **kw):
                    inst = super().__new__(cls)
                    runner_ref[0] = _wr.ref(inst)
                    return inst
            _pw._PdbPersistentRunner = CapturingRunner
            try:
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'runner_test.py'
                    worker._lifecycle['script'] = 'runner_test.py'
                thread = _threading.Thread(
                    target=worker._execute_target_persistent,
                    args=('runner_test.py', abs_path, [99], [], b"x = 1\n"),
                    daemon=True,
                )
                thread.start()
                thread.join(timeout=3.0)
                assert not thread.is_alive()
                runner_weakref = runner_ref[0]
                assert runner_weakref is not None, "Persistent runner was never created"
                _gc.collect()
                assert runner_weakref() is None, "Persistent runner remained strongly referenced"
            finally:
                _pw._PdbPersistentRunner = saved_runner_class
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)

    def test_runner_collectable_after_failure(self):
        import os as _os, threading as _threading, gc as _gc, weakref as _wr
        from agentic_debugger.runtime import pdb_worker as _pw
        d = Path(tempfile.mkdtemp())
        try:
            (d / "runner_test.py").write_text("raise ValueError('x')\n")
            abs_path = _os.path.realpath(str(d / "runner_test.py"))
            saved_cwd = _os.getcwd()
            worker = _pw.PdbWorker()
            responses = []
            worker._send_response = lambda r: responses.append(r)
            worker._target_started = True
            runner_ref = [None]
            saved_runner_class = _pw._PdbPersistentRunner
            class CapturingRunner(saved_runner_class):
                def __new__(cls, *a, **kw):
                    inst = super().__new__(cls)
                    runner_ref[0] = _wr.ref(inst)
                    return inst
            _pw._PdbPersistentRunner = CapturingRunner
            try:
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'runner_test.py'
                    worker._lifecycle['script'] = 'runner_test.py'
                thread = _threading.Thread(
                    target=worker._execute_target_persistent,
                    args=('runner_test.py', abs_path, [99], [], b"raise ValueError('x')\n"),
                    daemon=True,
                )
                thread.start()
                thread.join(timeout=3.0)
                assert not thread.is_alive()
                runner_weakref = runner_ref[0]
                assert runner_weakref is not None, "Persistent runner was never created"
                _gc.collect()
                assert runner_weakref() is None, "Persistent runner remained strongly referenced"
            finally:
                _pw._PdbPersistentRunner = saved_runner_class
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)

    def test_runner_collectable_after_termination(self):
        import os as _os, threading as _threading, gc as _gc, weakref as _wr
        from agentic_debugger.runtime import pdb_worker as _pw
        d = Path(tempfile.mkdtemp())
        try:
            (d / "runner_test.py").write_text("x = 1\n")
            abs_path = _os.path.realpath(str(d / "runner_test.py"))
            saved_cwd = _os.getcwd()
            worker = _pw.PdbWorker()
            responses = []
            worker._send_response = lambda r: responses.append(r)
            worker._target_started = True
            runner_ref = [None]
            saved_runner_class = _pw._PdbPersistentRunner
            class CapturingRunner(saved_runner_class):
                def __new__(cls, *a, **kw):
                    inst = super().__new__(cls)
                    runner_ref[0] = _wr.ref(inst)
                    return inst
            _pw._PdbPersistentRunner = CapturingRunner
            try:
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'runner_test.py'
                    worker._lifecycle['script'] = 'runner_test.py'
                thread = _threading.Thread(
                    target=worker._execute_target_persistent,
                    args=('runner_test.py', abs_path, [1], [], b"x = 1\n"),
                    daemon=True,
                )
                worker._target_thread = thread
                thread.start()
                with worker._condition:
                    while worker._lifecycle['state'] == 'starting':
                        worker._condition.wait()
                    assert worker._lifecycle['state'] == 'paused'
                term_result = worker._request_target_termination()
                assert term_result == {"state": "terminated"}
                assert worker._target_thread is None
                thread.join(timeout=3.0)
                assert not thread.is_alive()
                runner_weakref = runner_ref[0]
                assert runner_weakref is not None, "Persistent runner was never created"
                _gc.collect()
                assert runner_weakref() is None, "Persistent runner remained strongly referenced"
            finally:
                _pw._PdbPersistentRunner = saved_runner_class
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)


class TestPersistentPauseCwdTermination:
    """Target-driven cwd restoration after termination (Repair 5)."""

    def test_target_driven_cwd_restored_after_termination(self):
        import os as _os, sys as _sys, threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        d = Path(tempfile.mkdtemp())
        try:
            other = d / "other_term"
            other.mkdir(exist_ok=True)
            saved_cwd = _os.getcwd()
            script = (
                "import os\n"
                f"os.chdir({str(other)!r})\n"
                "x = 1\n"
            )
            (d / "cwd_term.py").write_text(script)
            abs_path = _os.path.realpath(str(d / "cwd_term.py"))
            try:
                worker = PdbWorker()
                responses = []
                worker._send_response = lambda r: responses.append(r)
                worker._target_started = True
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'cwd_term.py'
                    worker._lifecycle['script'] = 'cwd_term.py'
                thread = _threading.Thread(
                    target=worker._execute_target_persistent,
                    args=('cwd_term.py', abs_path, [3], [], script.encode()),
                    daemon=True,
                )
                worker._target_thread = thread
                thread.start()
                with worker._condition:
                    while worker._lifecycle['state'] == 'starting':
                        worker._condition.wait()
                    assert worker._lifecycle['state'] == 'paused'
                worker._request_target_termination()
                thread.join(timeout=3.0)
                assert not thread.is_alive()
                assert _os.getcwd() == saved_cwd
            finally:
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)


class TestPersistentPauseTraceReleaseAndCwd:
    """Trace identity, thread release, target-driven cwd for exit/failure (Repair 6)."""

    def test_target_thread_trace_identity(self):
        import os as _os, sys as _sys, threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        d = Path(tempfile.mkdtemp())
        try:
            (d / "trace_id.py").write_text("x = 1\n")
            abs_path = _os.path.realpath(str(d / "trace_id.py"))
            saved_cwd = _os.getcwd()
            saved_trace = _sys.gettrace()
            observed = []
            def sentinel_trace(frame, event, arg):
                return sentinel_trace
            _threading.settrace(sentinel_trace)
            try:
                worker = PdbWorker()
                responses = []
                worker._send_response = lambda r: responses.append(r)
                worker._target_started = True
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'trace_id.py'
                    worker._lifecycle['script'] = 'trace_id.py'
                def wrapper():
                    worker._execute_target_persistent(
                        'trace_id.py', abs_path, [99], [], b"x = 1\n"
                    )
                    observed.append(_sys.gettrace() is sentinel_trace)
                thread = _threading.Thread(target=wrapper, daemon=True)
                thread.start()
                thread.join(timeout=3.0)
                assert len(observed) == 1
                assert observed[0] is True
            finally:
                _threading.settrace(None)
                _sys.settrace(saved_trace)
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)

    def test_target_thread_trace_after_failure(self):
        import os as _os, sys as _sys, threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        d = Path(tempfile.mkdtemp())
        try:
            (d / "trace_fail.py").write_text("raise ValueError('x')\n")
            abs_path = _os.path.realpath(str(d / "trace_fail.py"))
            saved_cwd = _os.getcwd()
            saved_trace = _sys.gettrace()
            observed = []
            def sentinel_trace(frame, event, arg):
                return sentinel_trace
            _threading.settrace(sentinel_trace)
            try:
                worker = PdbWorker()
                responses = []
                worker._send_response = lambda r: responses.append(r)
                worker._target_started = True
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'trace_fail.py'
                    worker._lifecycle['script'] = 'trace_fail.py'
                def wrapper():
                    worker._execute_target_persistent(
                        'trace_fail.py', abs_path, [99], [], b"raise ValueError('x')\n"
                    )
                    observed.append(_sys.gettrace() is sentinel_trace)
                thread = _threading.Thread(target=wrapper, daemon=True)
                thread.start()
                thread.join(timeout=3.0)
                assert len(observed) == 1
                assert observed[0] is True
            finally:
                _threading.settrace(None)
                _sys.settrace(saved_trace)
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)

    def test_target_thread_trace_after_termination(self):
        import os as _os, sys as _sys, threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        d = Path(tempfile.mkdtemp())
        try:
            (d / "trace_term.py").write_text("x = 1\n")
            abs_path = _os.path.realpath(str(d / "trace_term.py"))
            saved_cwd = _os.getcwd()
            saved_trace = _sys.gettrace()
            observed = []
            def sentinel_trace(frame, event, arg):
                return sentinel_trace
            _threading.settrace(sentinel_trace)
            try:
                worker = PdbWorker()
                responses = []
                worker._send_response = lambda r: responses.append(r)
                worker._target_started = True
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'trace_term.py'
                    worker._lifecycle['script'] = 'trace_term.py'
                def wrapper():
                    worker._execute_target_persistent(
                        'trace_term.py', abs_path, [1], [], b"x = 1\n"
                    )
                    observed.append(_sys.gettrace() is sentinel_trace)
                thread = _threading.Thread(target=wrapper, daemon=True)
                thread.start()
                with worker._condition:
                    while worker._lifecycle['state'] == 'starting':
                        worker._condition.wait()
                    assert worker._lifecycle['state'] == 'paused'
                worker._request_target_termination()
                thread.join(timeout=3.0)
                assert len(observed) == 1
                assert observed[0] is True
            finally:
                _threading.settrace(None)
                _sys.settrace(saved_trace)
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)

    def test_target_driven_cwd_restored(self):
        import os as _os, sys as _sys, threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        d = Path(tempfile.mkdtemp())
        try:
            other = d / "other_dir"
            other.mkdir(exist_ok=True)
            saved_cwd = _os.getcwd()
            script = (
                "import os\n"
                f"os.chdir({str(other)!r})\n"
                "x = 1\n"
            )
            (d / "cwd_target.py").write_text(script)
            abs_path = _os.path.realpath(str(d / "cwd_target.py"))
            try:
                worker = PdbWorker()
                responses = []
                worker._send_response = lambda r: responses.append(r)
                worker._target_started = True
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'cwd_target.py'
                    worker._lifecycle['script'] = 'cwd_target.py'
                thread = _threading.Thread(
                    target=worker._execute_target_persistent,
                    args=('cwd_target.py', abs_path, [99], [], script.encode()),
                    daemon=True,
                )
                thread.start()
                thread.join(timeout=3.0)
                assert _os.getcwd() == saved_cwd
            finally:
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)

    def test_target_driven_cwd_restored_after_failure(self):
        import os as _os, sys as _sys, threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        d = Path(tempfile.mkdtemp())
        try:
            other = d / "other_fail"
            other.mkdir(exist_ok=True)
            saved_cwd = _os.getcwd()
            script = (
                "import os\n"
                f"os.chdir({str(other)!r})\n"
                "raise ValueError('boom')\n"
            )
            (d / "cwd_fail.py").write_text(script)
            abs_path = _os.path.realpath(str(d / "cwd_fail.py"))
            try:
                worker = PdbWorker()
                responses = []
                worker._send_response = lambda r: responses.append(r)
                worker._target_started = True
                with worker._condition:
                    worker._lifecycle['state'] = 'starting'
                    worker._lifecycle['_start_script'] = 'cwd_fail.py'
                    worker._lifecycle['script'] = 'cwd_fail.py'
                thread = _threading.Thread(
                    target=worker._execute_target_persistent,
                    args=('cwd_fail.py', abs_path, [99], [], script.encode()),
                    daemon=True,
                )
                thread.start()
                thread.join(timeout=3.0)
                assert _os.getcwd() == saved_cwd
            finally:
                _os.chdir(saved_cwd)
        finally:
            import shutil as _shutil
            _shutil.rmtree(str(d), ignore_errors=True)


class TestPersistentPauseShutdownUnpaused:
    """Normal (unpaused) shutdown produces exactly one response."""

    def test_shutdown_unpaused_worker_one_response(self):
        from agentic_debugger.runtime import pdb_worker as _pw
        from agentic_debugger.runtime.pdb_protocol import PdbRequest, PROTOCOL_VERSION
        worker = _pw.PdbWorker()
        responses = []
        worker._send_response = lambda r: responses.append(r)
        worker._handle_shutdown(PdbRequest(
            protocol_version=PROTOCOL_VERSION, request_id=1,
            operation='shutdown', payload={},
        ))
        assert len(responses) == 1, f"Got {len(responses)} responses"
        resp = responses[0]
        assert resp.success is True
        assert resp.result.get('shutdown') is True


class TestPersistentPauseSystemExit:
    """SystemExit normalization for persistent targets (Repair 6)."""

    @pytest.fixture
    def ws_sysexit(self):
        src = Path(tempfile.mkdtemp())
        try:
            (src / "target.py").write_text("x = 1\ny = 2\nz = 3\n")
            with TaskWorkspace(str(src)) as ws:
                yield ws
        finally:
            shutil.rmtree(str(src), ignore_errors=True)

    def _run_target_and_get_exit(self, ws, script_lines):
        full_lines = script_lines + ["# padding", "# end"]
        script_content = "\n".join(full_lines)
        script_path = Path(ws.root) / "tmp_exit.py"
        script_path.write_text(script_content)
        bp_line = len(full_lines)
        session = PdbSession(ws)
        session.start()
        try:
            resp = _raw_op(session, 600, "start_paused_target", {
                "script": "tmp_exit.py", "breakpoints": [bp_line], "argv": [],
            })
            assert resp.success is True, f"start failed: {resp.error}"
            assert resp.result["state"] == "exited"
            ec = resp.result["exit_code"]
            assert isinstance(ec, int) and not isinstance(ec, bool)
            return ec
        finally:
            session.stop()

    def test_sysexit_none(self, ws_sysexit):
        ec = self._run_target_and_get_exit(ws_sysexit, [
            "import sys", "sys.exit(None)", "# end",
        ])
        assert ec == 0

    def test_sysexit_false(self, ws_sysexit):
        ec = self._run_target_and_get_exit(ws_sysexit, [
            "import sys", "sys.exit(False)", "# end",
        ])
        assert ec == 0

    def test_sysexit_true(self, ws_sysexit):
        ec = self._run_target_and_get_exit(ws_sysexit, [
            "import sys", "sys.exit(True)", "# end",
        ])
        assert ec == 1

    def test_sysexit_42(self, ws_sysexit):
        ec = self._run_target_and_get_exit(ws_sysexit, [
            "import sys", "sys.exit(42)", "# end",
        ])
        assert ec == 42

    def test_sysexit_negative(self, ws_sysexit):
        ec = self._run_target_and_get_exit(ws_sysexit, [
            "import sys", "sys.exit(-3)", "# end",
        ])
        assert ec == -3

    def test_sysexit_string(self, ws_sysexit):
        ec = self._run_target_and_get_exit(ws_sysexit, [
            "import sys", "sys.exit('bye')", "# end",
        ])
        assert ec == 1


class TestPersistentPauseInternalState:
    """Internal-state invariant enforcement (Repair 7)."""

    def test_unexpected_state_returns_failed_response(self):
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        from agentic_debugger.runtime.pdb_protocol import PdbRequest, PROTOCOL_VERSION
        worker = PdbWorker()
        responses = []
        worker._send_response = lambda r: responses.append(r)
        with worker._condition:
            worker._lifecycle['state'] = 'starting'
        worker._handle_get_target_status(PdbRequest(
            protocol_version=PROTOCOL_VERSION, request_id=1,
            operation='get_target_status', payload={},
        ))
        assert len(responses) == 1
        resp = responses[0]
        assert resp.success is False
        assert "Unexpected" in resp.error

    def test_internal_terminating_returns_failed(self):
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        from agentic_debugger.runtime.pdb_protocol import PdbRequest, PROTOCOL_VERSION
        worker = PdbWorker()
        responses = []
        worker._send_response = lambda r: responses.append(r)
        with worker._condition:
            worker._lifecycle['state'] = 'terminating'
        worker._handle_get_target_status(PdbRequest(
            protocol_version=PROTOCOL_VERSION, request_id=1,
            operation='get_target_status', payload={},
        ))
        assert len(responses) == 1
        resp = responses[0]
        assert resp.success is False
        assert "Unexpected" in resp.error


# =====================================================================
# Task 4B2B — Public PdbSession Paused-Target API and Lifecycle Guards
# =====================================================================


class TestPublicPausedTargetIntegration:
    """Public PdbSession paused-target integration tests."""

    @pytest.fixture
    def ws_pub(self):
        src = Path(tempfile.mkdtemp())
        try:
            (src / "simple.py").write_text(
                "x = 1\n"
                "y = 2\n"
                "z = 3\n"
                "w = 4\n"
                "# end\n"
            )
            (src / "with_finally.py").write_text(
                "import sys\n"
                "try:\n"
                "    x = 1\n"
                "finally:\n"
                "    with open(sys.argv[1], 'w') as f:\n"
                "        f.write('finally executed')\n"
                "# after\n"
            )
            (src / "exit_early.py").write_text(
                "x = 1\n"
                "import sys\n"
                "sys.stdout.write('done')\n"
                "# end\n"
            )
            (src / "fail_target.py").write_text(
                "def main():\n"
                "    raise ValueError('example')\n"
                "main()\n"
                "# end\n"
            )
            (src / "break_before_exit.py").write_text(
                "x = 1\n"
                "y = 2\n"
                "# end\n"
            )
            (src / "code_after.py").write_text(
                "x = 1\n"
                "import sys\n"
                "with open(sys.argv[1], 'w') as f:\n"
                "    f.write('AFTER_SHOULD_NOT_EXIST')\n"
                "# end\n"
            )
            (src / "oneshot_target.py").write_text("x = 1\ny = 2\nz = 3\n")
            (src / "oneshot_exit.py").write_text(
                "import sys\nsys.exit(42)\n# end\n"
            )
            (src / "oneshot_fail.py").write_text(
                "raise ValueError('test')\n# end\n"
            )
            with TaskWorkspace(str(src)) as ws:
                yield ws
        finally:
            shutil.rmtree(str(src), ignore_errors=True)

    # 57. Public start reaches paused breakpoint
    def test_public_start_reaches_breakpoint(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            result = session.start_paused_target("simple.py", [3])
            assert result["state"] == "paused"
            assert result["script"] == "simple.py"
            assert result["line"] == 3
            assert result["function"] == "<module>"
        finally:
            session.stop()

    # 58. Exact paused result
    def test_exact_paused_result(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            result = session.start_paused_target("simple.py", [3])
            assert set(result.keys()) == {"state", "script", "line", "function"}
        finally:
            session.stop()

    # 59. Code after breakpoint not executed
    def test_code_after_breakpoint_not_executed(self, ws_pub):
        marker = Path(ws_pub.root) / "after_bp_pub.txt"
        (Path(ws_pub.root) / "after_bp_pub.py").write_text(
            "x = 1\n"
            f"open({str(marker)!r}, 'w').write('SHOULD_NOT_EXIST')\n"
            "# end\n"
        )
        session = PdbSession(ws_pub)
        session.start()
        try:
            result = session.start_paused_target("after_bp_pub.py", [1])
            assert result["state"] == "paused"
            assert result["line"] == 1
            assert not marker.exists()
        finally:
            session.stop()

    # 60. Ping while paused
    def test_ping_while_paused(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            session.start_paused_target("simple.py", [3])
            pong = session.ping()
            assert pong.success is True
        finally:
            session.stop()

    # 61. Public status while paused
    def test_status_while_paused(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            session.start_paused_target("simple.py", [3])
            status = session.get_target_status()
            assert status["state"] == "paused"
            assert status["script"] == "simple.py"
            assert status["line"] == 3
        finally:
            session.stop()

    # 62/63. Public terminate and exact result
    def test_public_terminate(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            session.start_paused_target("simple.py", [3])
            result = session.terminate_paused_target()
            assert result["state"] == "terminated"
            assert result["script"] == "simple.py"
            assert set(result.keys()) == {"state", "script"}
        finally:
            session.stop()

    # 64. Status after termination
    def test_status_after_termination(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            session.start_paused_target("simple.py", [3])
            session.terminate_paused_target()
            status = session.get_target_status()
            assert status["state"] == "terminated"
            assert status["script"] == "simple.py"
        finally:
            session.stop()

    # 65. Target finally executes
    def test_target_finally_executes(self, ws_pub):
        marker = Path(ws_pub.root) / "finally_pub.txt"
        marker_str = str(marker)
        (Path(ws_pub.root) / "finally_pub.py").write_text(
            "import sys\n"
            "try:\n"
            "    x = 1\n"
            "finally:\n"
            f"    with open({marker_str!r}, 'w') as f:\n"
            "        f.write('finally executed')\n"
            "# after\n"
        )
        session = PdbSession(ws_pub)
        session.start()
        try:
            session.start_paused_target("finally_pub.py", [3], argv=[marker_str])
            session.terminate_paused_target()
            assert marker.exists()
            assert marker.read_text() == "finally executed"
        finally:
            session.stop()

    # 66. Code after interrupted flow does not execute
    def test_code_after_interrupted_flow(self, ws_pub):
        marker = Path(ws_pub.root) / "after_int_pub.txt"
        marker_str = str(marker)
        (Path(ws_pub.root) / "after_int_pub.py").write_text(
            "x = 1\n"
            f"with open({marker_str!r}, 'w') as f:\n"
            "    f.write('AFTER')\n"
            "# end\n"
        )
        session = PdbSession(ws_pub)
        session.start()
        try:
            session.start_paused_target("after_int_pub.py", [1], argv=[marker_str])
            session.terminate_paused_target()
            assert not marker.exists()
        finally:
            session.stop()

    # 67. Ping after termination
    def test_ping_after_termination(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            session.start_paused_target("simple.py", [3])
            session.terminate_paused_target()
            pong = session.ping()
            assert pong.success is True
        finally:
            session.stop()

    # 68. Second terminate locally rejected
    def test_second_terminate_locally_rejected(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            session.start_paused_target("simple.py", [3])
            session.terminate_paused_target()
            with pytest.raises(PdbSessionStateError, match="Cannot terminate"):
                session.terminate_paused_target()
            pong = session.ping()
            assert pong.success is True
        finally:
            session.stop()

    # 69/70. Start target exits before breakpoint
    def test_start_target_exits_before_breakpoint(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            result = session.start_paused_target("exit_early.py", [4])
            assert result["state"] == "exited"
            assert result["exit_code"] == 0
        finally:
            session.stop()

    def test_status_after_exit(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            session.start_paused_target("exit_early.py", [4])
            status = session.get_target_status()
            assert status["state"] == "exited"
            assert status["exit_code"] == 0
        finally:
            session.stop()

    # 71/72. Start target fails before breakpoint
    def test_start_target_fails_before_breakpoint(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            with pytest.raises(PdbSessionError, match="ValueError"):
                session.start_paused_target("fail_target.py", [4])
            assert session.state == PdbSessionState.READY
        finally:
            session.stop()

    def test_status_after_failure(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            with pytest.raises(PdbSessionError):
                session.start_paused_target("fail_target.py", [4])
            status = session.get_target_status()
            assert status["state"] == "failed"
            assert "ValueError" in status["error"]
        finally:
            session.stop()

    # 73. Worker remains pingable after target failure
    def test_ping_after_target_failure(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            with pytest.raises(PdbSessionError):
                session.start_paused_target("fail_target.py", [4])
            pong = session.ping()
            assert pong.success is True
        finally:
            session.stop()

    # 74. Stop while paused
    def test_stop_while_paused(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        session.start_paused_target("simple.py", [3])
        session.stop()
        assert session.state == PdbSessionState.STOPPED

    # 75. Context-manager exit while paused
    def test_context_manager_while_paused(self, ws_pub):
        with PdbSession(ws_pub) as session:
            session.start_paused_target("simple.py", [3])
            assert session.state == PdbSessionState.READY
        assert session.state == PdbSessionState.STOPPED

    # 76. One-shot breakpoint followed by public status
    def test_oneshot_breakpoint_then_status_terminated(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            resp = session.run_to_breakpoint("oneshot_target.py", [3])
            assert resp.success is True
            status = session.get_target_status()
            assert status["state"] == "terminated"
        finally:
            session.stop()

    # 77. One-shot exit followed by public status
    def test_oneshot_exit_then_status_exited(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            resp = session.run_to_breakpoint("oneshot_exit.py", [3])
            assert resp.success is True
            status = session.get_target_status()
            assert status["state"] == "exited"
        finally:
            session.stop()

    # 78. One-shot failure followed by public status
    def test_oneshot_failure_then_status_failed(self, ws_pub):
        session = PdbSession(ws_pub)
        session.start()
        try:
            resp = session.run_to_breakpoint("oneshot_fail.py", [2])
            assert resp.success is False
            status = session.get_target_status()
            assert status["state"] == "failed"
            assert "ValueError" in status["error"]
        finally:
            session.stop()

    # 79/80. Timeout before first breakpoint triggers session cleanup
    def test_timeout_before_breakpoint(self, ws_pub):
        (Path(ws_pub.root) / "timeout_pub.py").write_text(
            "import time\ntime.sleep(300)\nx = 1\n"
        )
        session = PdbSession(ws_pub, request_timeout=1.0)
        session.start()
        try:
            with pytest.raises(PdbSessionTimeoutError):
                session.start_paused_target("timeout_pub.py", [3])
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()

    # 81. No orphan worker after timeout cleanup
    def test_no_orphan_worker_after_timeout(self, ws_pub):
        (Path(ws_pub.root) / "inf_pub.py").write_text(
            "import time\ntime.sleep(300)\nx = 1\n"
        )
        session = PdbSession(ws_pub, request_timeout=1.0)
        session.start()
        proc = session._proc
        assert proc is not None
        try:
            with pytest.raises(PdbSessionTimeoutError):
                session.start_paused_target("inf_pub.py", [3])
        finally:
            session.stop()
        proc.wait(timeout=3.0)
        assert proc.poll() is not None


# =====================================================================
# Task 4B3 ? Continue/Resume and Additional Execution Control v1
# =====================================================================


@pytest.fixture
def continue_workspace():
    root = Path(tempfile.mkdtemp())
    try:
        (root / "multi.py").write_text(
            "import sys\n"
            "open(sys.argv[1], 'a').write('A')\n"
            "first = 1\n"
            "open(sys.argv[1], 'a').write('B')\n"
            "second = 2\n"
            "open(sys.argv[1], 'a').write('C')\n"
            "# end\n"
        )
        (root / "loop.py").write_text(
            "import sys\n"
            "for i in range(2):\n"
            "    with open(sys.argv[1], 'a') as f:\n"
            "        f.write(str(i))\n"
            "# end\n"
        )
        (root / "fail_after.py").write_text(
            "x = 1\n"
            "raise ValueError('resume boom')\n"
            "# end\n"
        )
        (root / "terminate_later.py").write_text(
            "import sys\n"
            "try:\n"
            "    first = 1\n"
            "    middle = 2\n"
            "    second = 3\n"
            "    open(sys.argv[1], 'w').write('AFTER')\n"
            "finally:\n"
            "    open(sys.argv[2], 'w').write('FINAL')\n"
        )
        (root / "io_cycles.py").write_text(
            "import sys\n"
            "first = 1\n"
            "print('target stdout')\n"
            "sys.stderr.write('target stderr')\n"
            "data = sys.stdin.read()\n"
            "open(sys.argv[1], 'w').write(repr(data))\n"
            "second = 2\n"
            "open(sys.argv[1], 'a').write('|done')\n"
        )
        (root / "timeout_after.py").write_text(
            "first = 1\n"
            "while True:\n"
            "    pass\n"
        )
        with TaskWorkspace(str(root)) as workspace:
            yield workspace
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


class TestContinueRepeatedExecutionIntegration:
    def test_different_lines_pause_independently_then_exit(self, continue_workspace):
        marker = Path(continue_workspace.root) / "multi-marker.txt"
        session = PdbSession(continue_workspace)
        session.start()
        try:
            first = session.start_paused_target(
                "multi.py", [3, 5], argv=[str(marker)]
            )
            assert first == {
                "state": "paused", "script": "multi.py",
                "line": 3, "function": "<module>",
            }
            assert marker.read_text() == "A"
            assert session.get_target_status()["line"] == 3
            assert session.ping().success is True

            second = session.continue_paused_target()
            assert second == {
                "state": "paused", "script": "multi.py",
                "line": 5, "function": "<module>",
            }
            assert marker.read_text() == "AB"
            assert session.get_target_status()["line"] == 5
            assert session.ping().success is True

            exited = session.continue_paused_target()
            assert exited == {
                "state": "exited", "script": "multi.py", "exit_code": 0,
            }
            assert marker.read_text() == "ABC"
            assert session.get_target_status() == exited
            assert session.ping().success is True
            with pytest.raises(PdbSessionStateError):
                session.continue_paused_target()
        finally:
            session.stop()

    def test_same_line_loop_is_a_new_pause_and_never_auto_continues(
        self, continue_workspace
    ):
        marker = Path(continue_workspace.root) / "loop-marker.txt"
        session = PdbSession(continue_workspace)
        session.start()
        try:
            first = session.start_paused_target(
                "loop.py", [4], argv=[str(marker)]
            )
            assert first["line"] == 4
            assert marker.read_text() == ""

            second = session.continue_paused_target()
            assert second["state"] == "paused"
            assert second["line"] == first["line"] == 4
            assert marker.read_text() == "0"
            assert session.get_target_status() == second
            assert marker.read_text() == "0"

            exited = session.continue_paused_target()
            assert exited["state"] == "exited"
            assert marker.read_text() == "01"
        finally:
            session.stop()

    def test_multiple_breakpoint_order_skips_unreachable(self, continue_workspace):
        marker = Path(continue_workspace.root) / "ordering.txt"
        session = PdbSession(continue_workspace)
        session.start()
        try:
            assert session.start_paused_target(
                "multi.py", [3, 5, 7], argv=[str(marker)]
            )["line"] == 3
            assert session.continue_paused_target()["line"] == 5
            assert session.continue_paused_target()["state"] == "exited"
        finally:
            session.stop()


class TestContinueFailureAndRecoveryIntegration:
    def test_failure_after_resume_is_operational_and_recoverable(
        self, continue_workspace
    ):
        session = PdbSession(continue_workspace)
        session.start()
        try:
            assert session.start_paused_target("fail_after.py", [1])["line"] == 1
            with pytest.raises(PdbSessionError, match="ValueError") as exc_info:
                session.continue_paused_target()
            assert "Traceback" not in str(exc_info.value)
            assert "locals" not in str(exc_info.value).lower()
            assert session.state == PdbSessionState.READY
            assert session._target_lifecycle_state == "unknown"
            with pytest.raises(PdbSessionStateError):
                session.continue_paused_target()
            assert session.ping().success is True
            status = session.get_target_status()
            assert status["state"] == "failed"
            assert "ValueError" in status["error"]
            with pytest.raises(PdbSessionStateError):
                session.continue_paused_target()
        finally:
            session.stop()

    @pytest.mark.parametrize("value, expected", [
        ("None", 0), ("False", 0), ("True", 1),
        ("17", 17), ("-9", -9), ("'text'", 1),
    ])
    def test_system_exit_normalization_after_continue(
        self, continue_workspace, value, expected
    ):
        path = Path(continue_workspace.root) / "exit_after.py"
        path.write_text(
            "first = 1\n"
            "import sys\n"
            f"sys.exit({value})\n"
        )
        session = PdbSession(continue_workspace)
        session.start()
        try:
            assert session.start_paused_target("exit_after.py", [1])["line"] == 1
            result = session.continue_paused_target()
            assert result["state"] == "exited"
            assert result["exit_code"] == expected
            assert isinstance(result["exit_code"], int)
            assert not isinstance(result["exit_code"], bool)
        finally:
            session.stop()


class TestContinueTerminationAndCleanupIntegration:
    def test_termination_at_later_pause_runs_finally_and_releases_thread(
        self, continue_workspace
    ):
        after = Path(continue_workspace.root) / "after.txt"
        final = Path(continue_workspace.root) / "final.txt"
        session = PdbSession(continue_workspace)
        session.start()
        try:
            assert session.start_paused_target(
                "terminate_later.py", [3, 5], argv=[str(after), str(final)]
            )["line"] == 3
            assert session.continue_paused_target()["line"] == 5
            result = session.terminate_paused_target()
            assert result == {
                "state": "terminated", "script": "terminate_later.py",
            }
            assert final.read_text() == "FINAL"
            assert not after.exists()
            assert session.get_target_status() == result
            assert session.ping().success is True
            with pytest.raises(PdbSessionStateError):
                session.terminate_paused_target()
        finally:
            session.stop()

    def test_stop_after_continue_pause_is_bounded(self, continue_workspace):
        marker = Path(continue_workspace.root) / "stop.txt"
        session = PdbSession(continue_workspace)
        try:
            session.start()
            session.start_paused_target(
                "multi.py", [3, 5], argv=[str(marker)]
            )
            session.continue_paused_target()
            session.stop()
            assert session.state == PdbSessionState.STOPPED
            assert marker.read_text() == "AB"
        finally:
            if session.state != PdbSessionState.STOPPED:
                session.stop()

    def test_context_exit_after_multiple_pauses_cleans_worker(
        self, continue_workspace
    ):
        marker = Path(continue_workspace.root) / "context.txt"
        with PdbSession(continue_workspace) as session:
            proc = session._proc
            session.start_paused_target("loop.py", [4], argv=[str(marker)])
            session.continue_paused_target()
        assert session.state == PdbSessionState.STOPPED
        assert proc is not None
        proc.wait(timeout=3.0)
        assert proc.poll() is not None
        assert marker.read_text() == "0"

    def test_target_io_isolated_across_continue_cycles(self, continue_workspace):
        marker = Path(continue_workspace.root) / "io.txt"
        session = PdbSession(continue_workspace)
        session.start()
        try:
            assert session.start_paused_target(
                "io_cycles.py", [2, 7], argv=[str(marker)]
            )["line"] == 2
            assert session.continue_paused_target()["line"] == 7
            assert marker.read_text() == "''"
            assert session.ping().success is True
            assert session.continue_paused_target()["state"] == "exited"
            assert marker.read_text() == "''|done"
            assert session.ping().success is True
        finally:
            session.stop()

    def test_timeout_after_resume_kills_worker_and_rejects_late_ping(
        self, continue_workspace
    ):
        session = PdbSession(continue_workspace, request_timeout=0.5)
        session.start()
        proc = session._proc
        try:
            assert session.start_paused_target("timeout_after.py", [1])["line"] == 1
            with pytest.raises(PdbSessionTimeoutError):
                session.continue_paused_target()
            assert session.state == PdbSessionState.FAILED
            assert proc is not None
            proc.wait(timeout=3.0)
            assert proc.poll() is not None
            with pytest.raises(PdbSessionStateError):
                session.ping()
        finally:
            session.stop()


class _TestContinueWorkerCoordinationBase:
    @pytest.mark.parametrize("state", [
        "idle", "starting", "running", "resuming", "exited", "failed",
        "terminated", "terminating",
    ])
    def test_raw_nonpaused_state_fails_once_without_resume(self, state):
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        worker = PdbWorker()
        responses = []
        worker._send_response = lambda response: responses.append(response)
        with worker._condition:
            worker._lifecycle["state"] = state
            generation = worker._lifecycle["pause_generation"]
        worker._handle(PdbRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=41,
            operation="continue_paused_target",
            payload={},
        ))
        assert len(responses) == 1
        assert responses[0].request_id == 41
        assert responses[0].success is False
        assert responses[0].result == {}
        assert worker._lifecycle["state"] == state
        assert worker._lifecycle["pause_generation"] == generation
        assert worker._target_thread is None

    def test_unknown_continue_payload_field_fails_once(self):
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        worker = PdbWorker()
        responses = []
        worker._send_response = lambda response: responses.append(response)
        worker._handle(PdbRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=42,
            operation="continue_paused_target",
            payload={"resume": True},
        ))
        assert len(responses) == 1
        assert responses[0].success is False
        assert responses[0].result == {}

    def test_real_same_line_advances_private_generation(self):
        import threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        root = Path(tempfile.mkdtemp())
        worker = PdbWorker()
        thread = None
        try:
            script = "for i in range(2):\n    value = i\n# end\n"
            path = root / "generation.py"
            path.write_text(script)
            worker._target_started = True
            with worker._condition:
                worker._lifecycle["state"] = "starting"
                worker._lifecycle["script"] = "generation.py"
                worker._lifecycle["_start_script"] = "generation.py"
            thread = _threading.Thread(
                target=worker._execute_target_persistent,
                args=("generation.py", os.path.realpath(str(path)), [2], [],
                      script.encode("utf-8")),
                daemon=True,
            )
            worker._target_thread = thread
            thread.start()
            deadline = time.monotonic() + 3.0
            with worker._condition:
                while worker._lifecycle["state"] == "starting":
                    remaining = deadline - time.monotonic()
                    assert remaining > 0, (
                        "Target did not leave starting state"
                    )
                    worker._condition.wait(timeout=remaining)
                first_generation = worker._lifecycle["pause_generation"]
                first_line = worker._lifecycle["line"]
            assert first_generation == 1
            responses = []
            worker._send_response = lambda response: responses.append(response)
            worker._handle_continue_paused_target(PdbRequest(
                protocol_version=PROTOCOL_VERSION, request_id=1,
                operation="continue_paused_target", payload={},
            ))
            with worker._condition:
                second_generation = worker._lifecycle["pause_generation"]
                second_line = worker._lifecycle["line"]
            assert second_generation > first_generation
            assert second_line == first_line == 2
            assert len(responses) == 1
            assert responses[0].success is True
            assert responses[0].result["state"] == "paused"
            worker._handle_continue_paused_target(PdbRequest(
                protocol_version=PROTOCOL_VERSION, request_id=2,
                operation="continue_paused_target", payload={},
            ))
            assert len(responses) == 2
            assert responses[1].result["state"] == "exited"
            assert worker._target_thread is None
            thread.join(timeout=3.0)
            assert not thread.is_alive()
        finally:
            if thread is not None and thread.is_alive():
                worker._request_target_termination()
                thread.join(timeout=3.0)
            shutil.rmtree(str(root), ignore_errors=True)


# =====================================================================
# Task 4C - Stack, frame, and locals inspection v1
# =====================================================================


@pytest.fixture
def inspection_workspace():
    root = Path(tempfile.mkdtemp())
    try:
        lines = [
            "import math",
            "import sys",
            "def trip():",
            "    with open(sys.argv[1], 'a') as marker:",
            "        marker.write('called')",
            "    raise RuntimeError('hostile method invoked')",
            "class HostileDescriptor:",
            "    def __get__(self, instance, owner):",
            "        return trip()",
            "class Hostile:",
            "    descriptor = HostileDescriptor()",
            "    @property",
            "    def property_value(self):",
            "        return trip()",
            "    def __repr__(self): return trip()",
            "    def __str__(self): return trip()",
            "    def __format__(self, spec): return trip()",
            "    def __len__(self): return trip()",
            "    def __iter__(self): return trip()",
            "    def __getitem__(self, key): return trip()",
            "    def __getattribute__(self, name): return trip()",
            "class HostileList(list):",
            "    def __iter__(self): return trip()",
            "    def __len__(self): return trip()",
            "    def __getitem__(self, key): return trip()",
            "class HostileDict(dict):",
            "    def __iter__(self): return trip()",
            "    def __len__(self): return trip()",
            "    def __getitem__(self, key): return trip()",
            "def outer(value):",
            "    caller_local = 'caller'",
            "    return inner(value, 7, 8, keyword=9, extra=10)",
            "def inner(value, /, positional, *args, keyword, **kwargs):",
            "    a_none = None",
            "    a_false = False",
            "    a_true = True",
            "    a_positive = 42",
            "    a_negative = -17",
            "    a_large = 1 << 1000",
            "    a_huge = 1 << 100000",
            "    a_finite = 1.25",
            "    a_nan = float('nan')",
            "    a_inf = float('inf')",
            "    a_ninf = float('-inf')",
            "    a_ascii = 'hello'",
            "    a_multibyte = 'é' * 2000",
            "    a_nul = 'left\\x00right'",
            "    a_bytes = b'abc'",
            "    a_big_bytes = b'x' * 2048",
            "    a_list = [1, 2, 3]",
            "    a_tuple = ('a', 'b')",
            "    a_dict = {'first': 1, 'second': 2}",
            "    a_set = {1, 2}",
            "    a_frozenset = frozenset({1, 2})",
            "    a_nested = [[['deep']]]",
            "    a_long_list = list(range(20))",
            "    a_long_dict = {str(i): i for i in range(20)}",
            "    a_cycle_list = []",
            "    a_cycle_list.append(a_cycle_list)",
            "    a_cycle_dict = {}",
            "    a_cycle_dict['self'] = a_cycle_dict",
            "    a_hostile = Hostile()",
            "    a_list_subclass = HostileList([1, 2])",
            "    a_dict_subclass = HostileDict({'x': 1})",
        ]
        lines.extend(f"    z_filler_{i:03d} = {i}" for i in range(140))
        lines.extend([
            "    pause_one = 1",
            "    pause_two = 2",
            "    return pause_two",
            "outer(42)",
        ])
        inspection_text = "\n".join(lines) + "\n"
        first_line = lines.index("    pause_one = 1") + 1
        second_line = lines.index("    pause_two = 2") + 1
        (root / "inspection.py").write_text(inspection_text, encoding="utf-8")

        budget_lines = ["def budget():"]
        budget_lines.extend(
            f"    value_{i:03d} = 'é' * 1024" for i in range(40)
        )
        budget_lines.extend(["    pause = 1", "    return pause", "budget()"])
        budget_line = budget_lines.index("    pause = 1") + 1
        (root / "budget.py").write_text(
            "\n".join(budget_lines) + "\n", encoding="utf-8"
        )

        deep_lines = []
        for index in range(70):
            deep_lines.append(f"def frame_{index}():")
            if index == 69:
                deep_lines.append("    pause = 1")
                deep_line = len(deep_lines)
                deep_lines.append("    return pause")
            else:
                deep_lines.append(f"    return frame_{index + 1}()")
        deep_lines.append("frame_0()")
        (root / "deep.py").write_text(
            "\n".join(deep_lines) + "\n", encoding="utf-8"
        )

        with TaskWorkspace(str(root)) as workspace:
            yield workspace, first_line, second_line, budget_line, deep_line
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


def _locals_by_name(result):
    return {entry["name"]: entry["value"] for entry in result["locals"]}


class TestInspectionPublicIntegration:
    def test_nested_stack_frame_locals_and_generation(
        self, inspection_workspace
    ):
        import json
        workspace, first_line, second_line, _, _ = inspection_workspace
        marker = Path(workspace.root) / "hostile-marker.txt"
        session = PdbSession(workspace)
        session.start()
        try:
            started = session.start_paused_target(
                "inspection.py", [first_line, second_line], [str(marker)]
            )
            assert started["line"] == first_line

            stack = session.get_stack_summary()
            assert set(stack) == {
                "state", "script", "pause_generation", "frames",
                "total_frames", "truncated",
            }
            assert stack["state"] == "paused"
            assert stack["script"] == "inspection.py"
            assert stack["pause_generation"] == 1
            assert [frame["frame_id"] for frame in stack["frames"]] == [0, 1, 2]
            assert [frame["function"] for frame in stack["frames"]] == [
                "inner", "outer", "<module>",
            ]
            assert [frame["is_current"] for frame in stack["frames"]] == [
                True, False, False,
            ]
            assert all(frame["script"] == "inspection.py" for frame in stack["frames"])
            assert all(not os.path.isabs(frame["script"]) for frame in stack["frames"])
            assert stack["total_frames"] == 3
            assert stack["truncated"] is False
            assert session.get_stack_summary() == stack
            assert session.get_target_status()["line"] == first_line
            assert session.ping().success is True

            current = session.get_frame(0, 1)
            caller = session.get_frame(1, 1)
            assert current["frame"]["function"] == "inner"
            assert caller["frame"]["function"] == "outer"
            assert current["frame"]["argument_names"] == [
                "value", "positional", "keyword", "args", "kwargs",
            ]
            assert current["frame"]["local_names"] == sorted(
                current["frame"]["local_names"]
            )
            assert len(current["frame"]["local_names"]) == 128
            assert current["frame"]["locals_count"] > 128
            assert current["frame"]["locals_truncated"] is True
            assert "caller_local" in caller["frame"]["local_names"]

            locals_result = session.get_frame_locals(0, 1)
            encoded = json.dumps(
                locals_result, ensure_ascii=False, separators=(",", ":"),
                sort_keys=True, allow_nan=False,
            ).encode("utf-8")
            assert len(encoded) <= 32768
            assert len(locals_result["locals"]) <= 128
            assert locals_result["total_count"] > 128
            assert locals_result["truncated"] is True
            names = [entry["name"] for entry in locals_result["locals"]]
            assert names == sorted(names)
            assert session.get_frame_locals(0, 1) == locals_result
            assert b"NaN" not in encoded and b"Infinity" not in encoded

            values = _locals_by_name(locals_result)
            assert values["a_none"]["kind"] == "none"
            assert values["a_false"]["value"] is False
            assert values["a_true"]["value"] is True
            assert values["a_positive"]["value"] == 42
            assert values["a_negative"]["value"] == -17
            assert values["a_large"]["value"] == 1 << 1000
            assert values["a_huge"]["value"] is None
            assert values["a_huge"]["size"] == 100001
            assert values["a_huge"]["truncated"] is True
            assert values["a_finite"]["value"] == 1.25
            assert values["a_nan"]["special"] == "nan"
            assert values["a_inf"]["special"] == "inf"
            assert values["a_ninf"]["special"] == "-inf"
            assert values["a_ascii"]["value"] == "hello"
            assert len(values["a_multibyte"]["value"].encode("utf-8")) == 2048
            assert values["a_multibyte"]["truncated"] is True
            assert values["a_nul"]["value"] == "left\x00right"
            assert values["a_bytes"]["value"] == "616263"
            assert len(values["a_big_bytes"]["value"]) == 2048
            assert values["a_big_bytes"]["truncated"] is True
            assert [item["value"] for item in values["a_list"]["items"]] == [1, 2, 3]
            assert [item["value"] for item in values["a_tuple"]["items"]] == ["a", "b"]
            assert [entry["key"]["value"] for entry in values["a_dict"]["entries"]] == [
                "first", "second",
            ]
            assert values["a_set"]["items"] == []
            assert values["a_set"]["size"] == 2
            assert values["a_set"]["truncated"] is True
            assert values["a_frozenset"]["items"] == []
            assert values["a_nested"]["items"][0]["items"][0]["truncated"] is True
            assert len(values["a_long_list"]["items"]) == 16
            assert values["a_long_list"]["truncated"] is True
            assert len(values["a_long_dict"]["entries"]) == 16
            assert values["a_cycle_list"]["items"][0]["truncated"] is True
            cycle_dict_value = values["a_cycle_dict"]["entries"][0]["value"]
            assert cycle_dict_value["kind"] == "dict"
            assert cycle_dict_value["truncated"] is True
            assert values["a_hostile"]["kind"] == "object"
            assert values["a_list_subclass"]["kind"] == "object"
            assert values["a_dict_subclass"]["kind"] == "object"
            assert not marker.exists()

            with pytest.raises(PdbSessionError):
                session.get_frame(99, 1)
            with pytest.raises(PdbSessionError):
                session.get_frame_locals(99, 1)
            assert session.state == PdbSessionState.READY
            assert session.get_target_status()["line"] == first_line

            resumed = session.continue_paused_target()
            assert resumed["line"] == second_line
            stack_two = session.get_stack_summary()
            assert stack_two["pause_generation"] == 2
            assert stack_two["frames"][0]["frame_id"] == 0
            with pytest.raises(PdbSessionError):
                session.get_frame(0, 1)
            with pytest.raises(PdbSessionError):
                session.get_frame_locals(0, 1)
            assert session.state == PdbSessionState.READY
            assert session.get_target_status()["line"] == second_line
            assert session.get_frame(0, 2)["frame"]["function"] == "inner"
            assert session.get_frame_locals(0, 2)["pause_generation"] == 2
            assert session.continue_paused_target()["state"] == "exited"
            assert session._target_lifecycle_state == "exited"
        finally:
            session.stop()

    def test_locals_budget_stops_before_overflow(self, inspection_workspace):
        import json
        workspace, _, _, budget_line, _ = inspection_workspace
        session = PdbSession(workspace)
        session.start()
        try:
            session.start_paused_target("budget.py", [budget_line])
            stack = session.get_stack_summary()
            captured = []
            original = session._send_and_receive

            def capture(request, timeout):
                response = original(request, timeout)
                captured.append((request, response))
                return response

            session._send_and_receive = capture
            result = session.get_frame_locals(0, stack["pause_generation"])
            encoded = json.dumps(
                result, ensure_ascii=False, separators=(",", ":"),
                sort_keys=True, allow_nan=False,
            ).encode("utf-8")
            assert len(encoded) <= 32768
            assert captured[-1][0].operation == "get_frame_locals"
            assert len(serialize_response(captured[-1][1])) < MAX_LINE_LENGTH
            assert result["total_count"] == 40
            assert len(result["locals"]) < 40
            assert result["truncated"] is True
            assert session.get_frame_locals(0, 1) == result
            assert session.ping().success is True
        finally:
            session.stop()

    def test_stack_truncation_is_bounded_and_stable(self, inspection_workspace):
        workspace, _, _, _, deep_line = inspection_workspace
        session = PdbSession(workspace)
        session.start()
        try:
            session.start_paused_target("deep.py", [deep_line])
            result = session.get_stack_summary()
            assert len(result["frames"]) == 64
            assert result["total_frames"] == 71
            assert result["truncated"] is True
            assert [frame["frame_id"] for frame in result["frames"]] == list(range(64))
            assert result["frames"][0]["function"] == "frame_69"
            assert result["frames"][1]["function"] == "frame_68"
            assert session.get_stack_summary() == result
            assert session.get_target_status()["state"] == "paused"
        finally:
            session.stop()

    def test_inspection_then_termination(self, inspection_workspace):
        workspace, first_line, second_line, _, _ = inspection_workspace
        marker = Path(workspace.root) / "terminate-hostile-marker.txt"
        with PdbSession(workspace) as session:
            session.start_paused_target(
                "inspection.py", [first_line, second_line], [str(marker)]
            )
            generation = session.get_stack_summary()["pause_generation"]
            session.get_frame(0, generation)
            session.get_frame_locals(0, generation)
            assert session.terminate_paused_target()["state"] == "terminated"
            with pytest.raises(PdbSessionStateError):
                session.get_stack_summary()
            assert not marker.exists()


class TestInspectionRawProtocol:
    @pytest.mark.parametrize(
        ("operation", "payload"),
        [
            ("get_stack_summary", {"extra": 1}),
            ("get_frame", {"frame_id": 0, "pause_generation": 1, "extra": 1}),
            ("get_frame", {"pause_generation": 1}),
            ("get_frame", {"frame_id": True, "pause_generation": 1}),
            ("get_frame", {"frame_id": -1, "pause_generation": 1}),
            ("get_frame", {"frame_id": 0, "pause_generation": True}),
            ("get_frame", {"frame_id": 0, "pause_generation": 0}),
            ("get_frame_locals", {"frame_id": "0", "pause_generation": 1}),
            ("get_frame_locals", {"frame_id": 0, "pause_generation": -1}),
        ],
    )
    def test_exact_payload_validation(self, inspection_workspace, operation, payload):
        workspace, _, _, _, _ = inspection_workspace
        session = PdbSession(workspace)
        session.start()
        try:
            response = _raw_op(session, 900, operation, payload)
            assert response.success is False
            assert response.result == {}
        finally:
            session.stop()

    @pytest.mark.parametrize(
        "alias", ["stack", "get_stack", "frame", "locals", "get_locals"]
    )
    def test_aliases_rejected(self, inspection_workspace, alias):
        workspace, _, _, _, _ = inspection_workspace
        session = PdbSession(workspace)
        session.start()
        try:
            response = _raw_op(session, 901, alias, {})
            assert response.success is False
            assert response.result == {}
            assert "Unsupported operation" in response.error
        finally:
            session.stop()


class TestInspectionWorkerInvariant:
    @pytest.mark.parametrize("condition", ["missing_thread", "dead_thread", "missing_frame", "outside_frame"])
    def test_false_paused_state_fails_once_and_cleans(self, condition):
        import sys
        import threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker

        worker = PdbWorker()
        responses = []
        worker._send_response = responses.append
        worker._workspace_root_real = os.path.realpath(tempfile.gettempdir())
        worker._lifecycle.update({
            "state": "paused", "script": "target.py",
            "pause_generation": 1, "_paused_frame": sys._getframe(),
        })
        release = threading.Event()
        thread = None
        if condition == "dead_thread":
            thread = threading.Thread(target=lambda: None)
            thread.start()
            thread.join(timeout=2.0)
            assert not thread.is_alive()
            worker._target_thread = thread
        elif condition in ("missing_frame", "outside_frame"):
            def paused_owner():
                with worker._condition:
                    while worker._lifecycle["state"] == "paused":
                        worker._condition.wait()
                release.set()

            thread = threading.Thread(target=paused_owner, daemon=True)
            worker._target_thread = thread
            thread.start()
            assert thread.is_alive()
            if condition == "missing_frame":
                worker._lifecycle["_paused_frame"] = None
        else:
            worker._target_thread = None

        try:
            worker._handle_get_stack_summary(PdbRequest(
                protocol_version=PROTOCOL_VERSION,
                request_id=902,
                operation="get_stack_summary",
                payload={},
            ))
            assert len(responses) == 1
            assert responses[0].success is False
            assert responses[0].result == {}
            assert worker._lifecycle["state"] == "failed"
            assert worker._lifecycle["_paused_frame"] is None
            assert worker._target_thread is None
        finally:
            with worker._condition:
                if worker._lifecycle["state"] == "paused":
                    worker._lifecycle["state"] = "terminating"
                    worker._condition.notify_all()
            release.set()
            if thread is not None:
                thread.join(timeout=3.0)
                assert not thread.is_alive()

class TestSafeEvaluationWorkerInvariant:
    @pytest.mark.parametrize(
        "condition",
        [
            "missing_thread", "dead_thread", "missing_frame",
            "invalid_generation", "outside_workspace", "cannot_canonicalize",
        ],
    )
    def test_false_paused_state_fails_once_and_cleans(self, condition):
        import threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker

        worker = PdbWorker()
        responses = []
        worker._send_response = responses.append
        worker._workspace_root_real = os.path.realpath(os.getcwd())
        worker._lifecycle.update({
            "state": "paused",
            "script": "tests/integration/test_pdb_session_integration.py",
            "pause_generation": 1,
            "_paused_frame": sys._getframe(),
        })
        release = threading.Event()
        target_thread = None
        if condition == "dead_thread":
            target_thread = threading.Thread(target=lambda: None)
            target_thread.start()
            target_thread.join(timeout=2.0)
            assert not target_thread.is_alive()
        elif condition != "missing_thread":
            def paused_owner():
                try:
                    with worker._condition:
                        while worker._lifecycle["state"] == "paused":
                            worker._condition.wait()
                finally:
                    release.set()

            target_thread = threading.Thread(target=paused_owner, daemon=True)
            target_thread.start()
            assert target_thread.is_alive()
        worker._target_thread = target_thread
        if condition == "missing_frame":
            worker._lifecycle["_paused_frame"] = None
        elif condition == "invalid_generation":
            worker._lifecycle["pause_generation"] = 0
        elif condition == "outside_workspace":
            worker._workspace_root_real = os.path.realpath(tempfile.gettempdir())
        elif condition == "cannot_canonicalize":
            worker._canonical_workspace_frame_script = lambda frame: None

        try:
            worker._handle_safe_eval_expression(PdbRequest(
                protocol_version=PROTOCOL_VERSION,
                request_id=7300,
                operation="safe_eval_expression",
                payload={
                    "frame_id": 0,
                    "pause_generation": 1,
                    "expression": "value",
                },
            ))
            assert len(responses) == 1
            response = responses[0]
            assert response.request_id == 7300
            assert response.success is False
            assert response.result == {}
            assert response.error
            assert worker._lifecycle["state"] == "failed"
            assert worker._lifecycle["_paused_frame"] is None
            assert worker._target_thread is None
            assert worker._running is True
            assert worker._unsafe is False
        finally:
            with worker._condition:
                if worker._lifecycle["state"] == "paused":
                    worker._lifecycle["state"] = "terminating"
                    worker._condition.notify_all()
            release.set()
            if target_thread is not None:
                target_thread.join(timeout=3.0)
                assert not target_thread.is_alive()


class TestContinueWorkerCoordination(_TestContinueWorkerCoordinationBase):
    def test_stale_pause_generation_fails_closed(self):
        import threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        worker = PdbWorker()
        responses = []
        worker._send_response = lambda response: responses.append(response)
        ready = _threading.Event()
        publisher_errors = []

        def stale_publisher():
            deadline = time.monotonic() + 3.0
            try:
                with worker._condition:
                    ready.set()
                    while worker._lifecycle["state"] == "paused":
                        remaining = deadline - time.monotonic()
                        assert remaining > 0, (
                            "Continue did not release stale publisher"
                        )
                        worker._condition.wait(timeout=remaining)
                    worker._lifecycle["state"] = "paused"
                    worker._condition.notify_all()
            except BaseException as exc:
                publisher_errors.append(exc)

        with worker._condition:
            worker._lifecycle["state"] = "paused"
            worker._lifecycle["script"] = "test.py"
            worker._lifecycle["line"] = 3
            worker._lifecycle["function"] = "f"
            worker._lifecycle["pause_generation"] = 7
        thread = _threading.Thread(target=stale_publisher, daemon=True)
        worker._target_thread = thread
        try:
            thread.start()
            assert ready.wait(timeout=3.0)
            worker._handle_continue_paused_target(PdbRequest(
                protocol_version=PROTOCOL_VERSION, request_id=9,
                operation="continue_paused_target", payload={},
            ))
            assert len(responses) == 1
            response = responses[0]
            assert response.request_id == 9
            assert response.success is False
            assert response.result == {}
            assert "stale pause generation" in response.error
            thread.join(timeout=3.0)
            assert not thread.is_alive()
            assert publisher_errors == []
            assert worker._target_thread is None
            assert worker._lifecycle["pause_generation"] == 7
            assert worker._lifecycle["state"] == "failed"
            assert worker._lifecycle["error"] == response.error
            assert worker._running is True
            assert worker._unsafe is False

            worker._handle_get_target_status(PdbRequest(
                protocol_version=PROTOCOL_VERSION, request_id=10,
                operation="get_target_status", payload={},
            ))
            assert len(responses) == 2
            status = responses[1]
            assert status.request_id == 10
            assert status.success is True
            assert status.result["state"] == "failed"
            assert status.result["error"]
            assert len(status.result["error"].encode("utf-8")) <= 4096
        finally:
            if thread.is_alive():
                with worker._condition:
                    worker._lifecycle["state"] = "terminating"
                    worker._condition.notify_all()
                thread.join(timeout=3.0)
            assert not thread.is_alive()

    @pytest.mark.parametrize("thread_kind", ["missing", "dead"])
    def test_continue_repairs_paused_without_live_thread(self, thread_kind):
        import threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        worker = PdbWorker()
        responses = []
        worker._send_response = lambda response: responses.append(response)
        stale_thread = None
        if thread_kind == "dead":
            stale_thread = _threading.Thread(target=lambda: None)
            stale_thread.start()
            stale_thread.join(timeout=3.0)
            assert not stale_thread.is_alive()
        with worker._condition:
            worker._lifecycle["state"] = "paused"
            worker._lifecycle["script"] = "test.py"
            worker._lifecycle["line"] = 3
            worker._lifecycle["function"] = "f"
            worker._lifecycle["pause_generation"] = 4
            worker._target_thread = stale_thread

        worker._handle_continue_paused_target(PdbRequest(
            protocol_version=PROTOCOL_VERSION, request_id=20,
            operation="continue_paused_target", payload={},
        ))
        assert len(responses) == 1
        response = responses[0]
        assert response.request_id == 20
        assert response.success is False
        assert response.result == {}
        assert "target thread" in response.error
        assert worker._lifecycle["pause_generation"] == 4
        assert worker._lifecycle["state"] == "failed"
        assert worker._target_thread is None
        assert worker._running is True
        assert worker._unsafe is False

        worker._handle_get_target_status(PdbRequest(
            protocol_version=PROTOCOL_VERSION, request_id=21,
            operation="get_target_status", payload={},
        ))
        assert len(responses) == 2
        assert responses[1].success is True
        assert responses[1].result["state"] == "failed"
        assert responses[1].result["error"]

    @pytest.mark.parametrize("thread_kind", ["missing", "dead"])
    def test_status_repairs_paused_without_live_thread(self, thread_kind):
        import threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        worker = PdbWorker()
        responses = []
        worker._send_response = lambda response: responses.append(response)
        stale_thread = None
        if thread_kind == "dead":
            stale_thread = _threading.Thread(target=lambda: None)
            stale_thread.start()
            stale_thread.join(timeout=3.0)
            assert not stale_thread.is_alive()
        with worker._condition:
            worker._lifecycle["state"] = "paused"
            worker._lifecycle["script"] = "test.py"
            worker._lifecycle["line"] = 3
            worker._lifecycle["function"] = "f"
            worker._target_thread = stale_thread

        worker._handle_get_target_status(PdbRequest(
            protocol_version=PROTOCOL_VERSION, request_id=30,
            operation="get_target_status", payload={},
        ))
        assert len(responses) == 1
        assert responses[0].request_id == 30
        assert responses[0].success is True
        assert responses[0].result["state"] == "failed"
        assert responses[0].result["error"]
        assert worker._lifecycle["state"] == "failed"
        assert worker._target_thread is None


class TestContinueProcessGlobalRestoration:
    def test_globals_restored_after_continue_to_exit(self):
        import threading as _threading
        from agentic_debugger.runtime.pdb_worker import PdbWorker
        root = Path(tempfile.mkdtemp())
        other = root / "other"
        other.mkdir()
        worker = PdbWorker()
        responses = []
        thread = None
        saved_argv = list(sys.argv)
        saved_path = list(sys.path)
        saved_stdin = sys.stdin
        saved_stdout = sys.stdout
        saved_stderr = sys.stderr
        saved_cwd = os.getcwd()
        try:
            script = (
                "import os\n"
                f"os.chdir({str(other)!r})\n"
                "paused = 1\n"
                "finished = 2\n"
            )
            path = root / "restore_continue.py"
            path.write_text(script)
            worker._target_started = True
            worker._send_response = lambda response: responses.append(response)
            with worker._condition:
                worker._lifecycle["state"] = "starting"
                worker._lifecycle["script"] = "restore_continue.py"
                worker._lifecycle["_start_script"] = "restore_continue.py"
            thread = _threading.Thread(
                target=worker._execute_target_persistent,
                args=("restore_continue.py", os.path.realpath(str(path)), [3],
                      ["arg"], script.encode("utf-8")),
                daemon=True,
            )
            worker._target_thread = thread
            thread.start()
            deadline = time.monotonic() + 3.0
            with worker._condition:
                while worker._lifecycle["state"] == "starting":
                    remaining = deadline - time.monotonic()
                    assert remaining > 0, (
                        "Target did not leave starting state"
                    )
                    worker._condition.wait(timeout=remaining)
                assert worker._lifecycle["state"] == "paused"
            worker._handle_continue_paused_target(PdbRequest(
                protocol_version=PROTOCOL_VERSION, request_id=55,
                operation="continue_paused_target", payload={},
            ))
            thread.join(timeout=3.0)
            assert not thread.is_alive()
            assert worker._target_thread is None
            assert responses[-1].result["state"] == "exited"
            assert sys.argv == saved_argv
            assert sys.path == saved_path
            assert sys.stdin is saved_stdin
            assert sys.stdout is saved_stdout
            assert sys.stderr is saved_stderr
            assert os.getcwd() == saved_cwd
        finally:
            os.chdir(saved_cwd)
            sys.argv = saved_argv
            sys.path = saved_path
            sys.stdin = saved_stdin
            sys.stdout = saved_stdout
            sys.stderr = saved_stderr
            if thread is not None and thread.is_alive():
                worker._request_target_termination()
                thread.join(timeout=3.0)
            shutil.rmtree(str(root), ignore_errors=True)


# Task 4C repair: complete response-envelope fitting.


def _unbounded_wire_size(response):
    import json
    return len((json.dumps(
        response.to_mapping(), ensure_ascii=False, separators=(",", ":"),
        sort_keys=True, allow_nan=False,
    ) + "\n").encode("utf-8"))


@pytest.fixture
def inspection_response_budget_workspace():
    root = Path(tempfile.mkdtemp())
    try:
        stack_names = []
        stack_lines = []
        for index in range(64):
            prefix = f"stack_{index:03d}_"
            stack_names.append(prefix + "x" * (1500 - len(prefix)))
        for index, name in enumerate(stack_names):
            stack_lines.append(f"def {name}():")
            if index == 63:
                stack_lines.append("    pause = 1")
                stack_pause_line = len(stack_lines)
                stack_lines.append("    return pause")
            else:
                stack_lines.append(f"    return {stack_names[index + 1]}()")
        stack_lines.append(f"{stack_names[0]}()")
        (root / "long_stack.py").write_text(
            "\n".join(stack_lines) + "\n", encoding="utf-8"
        )

        local_names = []
        local_lines = ["def local_target():"]
        for index in range(128):
            prefix = f"local_{index:03d}_"
            name = prefix + "x" * (512 - len(prefix))
            local_names.append(name)
            local_lines.append(f"    {name} = {index}")
        local_lines.append("    pause = 1")
        local_pause_line = len(local_lines)
        local_lines.extend(["    return pause", "local_target()"])
        (root / "long_locals.py").write_text(
            "\n".join(local_lines) + "\n", encoding="utf-8"
        )

        argument_names = []
        argument_lines = ["def argument_target("]
        for index in range(128):
            prefix = f"argument_{index:03d}_"
            name = prefix + "x" * (512 - len(prefix))
            argument_names.append(name)
            argument_lines.append(f"    {name}=0,")
        argument_lines.extend([
            "):",
            "    pause = 1",
            "    return pause",
            "argument_target()",
        ])
        argument_pause_line = argument_lines.index("    pause = 1") + 1
        (root / "long_arguments.py").write_text(
            "\n".join(argument_lines) + "\n", encoding="utf-8"
        )

        with TaskWorkspace(str(root)) as workspace:
            yield {
                "workspace": workspace,
                "stack_names": stack_names,
                "stack_pause_line": stack_pause_line,
                "local_names": sorted(local_names),
                "local_pause_line": local_pause_line,
                "argument_names": argument_names,
                "argument_pause_line": argument_pause_line,
            }
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


class TestInspectionCompleteResponseBudgetRepair:
    @staticmethod
    def _capture_operations(session):
        captured = []
        original = session._send_and_receive

        def capture(request, timeout):
            response = original(request, timeout)
            captured.append((request, response))
            return response

        session._send_and_receive = capture
        return captured

    def test_long_stack_is_trimmed_to_complete_response_budget(
        self, inspection_response_budget_workspace
    ):
        data = inspection_response_budget_workspace
        session = PdbSession(data["workspace"])
        session.start()
        try:
            session.start_paused_target(
                "long_stack.py", [data["stack_pause_line"]]
            )
            captured = self._capture_operations(session)
            next_request_id = session._next_request_id
            pre_frames = []
            for frame_id, index in enumerate(range(63, -1, -1)):
                pre_frames.append({
                    "frame_id": frame_id,
                    "script": "long_stack.py",
                    "line": (
                        data["stack_pause_line"] if index == 63
                        else index * 2 + 2
                    ),
                    "function": data["stack_names"][index],
                    "is_current": frame_id == 0,
                })
            pre_response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=next_request_id,
                success=True,
                result={
                    "state": "paused", "script": "long_stack.py",
                    "pause_generation": 1, "frames": pre_frames,
                    "total_frames": 65, "truncated": True,
                },
                error="",
            )
            assert _unbounded_wire_size(pre_response) > MAX_LINE_LENGTH

            result = session.get_stack_summary()
            request, response = captured[-1]
            assert request.operation == "get_stack_summary"
            repaired_wire = serialize_response(response)
            assert len(repaired_wire) < MAX_LINE_LENGTH
            assert result["frames"][0]["frame_id"] == 0
            assert result["frames"][0]["is_current"] is True
            assert [frame["frame_id"] for frame in result["frames"]] == list(
                range(len(result["frames"]))
            )
            assert len(result["frames"]) < 64
            assert result["total_frames"] == 65
            assert result["truncated"] is True
            assert session.get_target_status()["state"] == "paused"
            assert session.ping().success is True
            assert session.terminate_paused_target()["state"] == "terminated"
        finally:
            session.stop()

    def test_long_local_names_are_trimmed_to_complete_response_budget(
        self, inspection_response_budget_workspace
    ):
        data = inspection_response_budget_workspace
        session = PdbSession(data["workspace"])
        session.start()
        try:
            session.start_paused_target(
                "long_locals.py", [data["local_pause_line"]]
            )
            stack = session.get_stack_summary()
            generation = stack["pause_generation"]
            current = stack["frames"][0]
            captured = self._capture_operations(session)
            next_request_id = session._next_request_id
            pre_response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=next_request_id,
                success=True,
                result={
                    "state": "paused", "pause_generation": generation,
                    "frame": {
                        **current,
                        "argument_names": [],
                        "local_names": data["local_names"],
                        "locals_count": 128,
                        "locals_truncated": False,
                    },
                },
                error="",
            )
            assert _unbounded_wire_size(pre_response) > MAX_LINE_LENGTH

            result = session.get_frame(0, generation)
            request, response = captured[-1]
            assert request.operation == "get_frame"
            repaired_wire = serialize_response(response)
            returned_names = result["frame"]["local_names"]
            assert len(repaired_wire) < MAX_LINE_LENGTH
            assert returned_names == sorted(returned_names)
            assert len(returned_names) < 128
            assert result["frame"]["locals_count"] == 128
            assert result["frame"]["locals_truncated"] is True
            assert session.get_stack_summary()["pause_generation"] == generation
            assert session.get_target_status()["state"] == "paused"
            assert session.terminate_paused_target()["state"] == "terminated"
        finally:
            session.stop()

    def test_argument_only_overflow_is_ordinary_correlated_failure(
        self, inspection_response_budget_workspace
    ):
        data = inspection_response_budget_workspace
        session = PdbSession(data["workspace"])
        session.start()
        try:
            started = session.start_paused_target(
                "long_arguments.py", [data["argument_pause_line"]]
            )
            stack = session.get_stack_summary()
            generation = stack["pause_generation"]
            current = stack["frames"][0]
            pre_response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=session._next_request_id,
                success=True,
                result={
                    "state": "paused", "pause_generation": generation,
                    "frame": {
                        **current,
                        "argument_names": data["argument_names"],
                        "local_names": [],
                        "locals_count": 128,
                        "locals_truncated": True,
                    },
                },
                error="",
            )
            assert _unbounded_wire_size(pre_response) > MAX_LINE_LENGTH
            captured = self._capture_operations(session)

            with pytest.raises(PdbSessionError, match="argument metadata"):
                session.get_frame(0, generation)
            request, response = captured[-1]
            assert request.operation == "get_frame"
            assert response.request_id == request.request_id
            assert response.success is False
            assert response.result == {}
            assert session.state == PdbSessionState.READY
            assert session._target_lifecycle_state == "paused"
            assert session.get_target_status()["line"] == started["line"]
            assert session.get_stack_summary()["pause_generation"] == generation
            assert session.ping().success is True
            assert session.terminate_paused_target()["state"] == "terminated"
        finally:
            session.stop()
