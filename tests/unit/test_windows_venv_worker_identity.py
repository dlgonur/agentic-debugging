"""Windows-venv PDB worker identity regressions (FirstMate blocker).

On Windows, a CPython virtual environment's ``Scripts\\python.exe`` is a
redirector: ``Popen`` on it returns the redirector PID while the actual
Python worker reports its own PID, so the strict
``worker PID == Popen(...).pid`` handshake failed for every PDB session
launched from a standard venv.  The repair (central authority
``agentic_debugger.runtime.python_launcher``, CPython bpo-35797 pattern)
launches the real base interpreter directly with the standard
``__PYVENV_LAUNCHER__`` venv identity, so the Popen PID is the worker PID
again while ``sys.prefix``/packages stay the venv's.

Coverage contract (each class maps to one acceptance clause):

* ``TestAuthorityDecisionTable`` -- deterministic venv recognition and
  launch computation (cross-platform, injected interpreter state).
* ``TestSessionWiring`` -- both worker-launch authorities route through
  the central module; the confused-deputy PID check stays strict.
* ``TestLiveHandshake`` -- a real PDB session starts and shakes hands
  (handshake success IS the PID-equality proof: a mismatch fails
  closed); an intentionally wrong PID still fails.
* ``TestStockVenvLauncher`` (Windows-only) -- the untouched stock
  ``Scripts\\python.exe`` drives a successful session; venv semantics
  (prefix, executable, venv packages, project bootstrap) are preserved
  with no system-site fallback.
* ``TestLocalProjectPdbPath`` -- the exact Local Project PDB
  construction reaches a paused probe (offline, curated fixture).
* ``TestLadderPath`` -- the worker -> nested-PDB topology (the
  previously failing ladder/PDB shape) completes with verified cleanup.
* ``TestCleanupAndNoWorkaround`` -- no descendant survives; no copied
  ``python-real.exe`` or renamed interpreter exists anywhere.

Offline only: no provider, no network, no billable route.  The
Windows-only tests run under the CI venv itself, which IS the genuine
stock-venv subject (no nested venv, no executable copying).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentic_debugger.runtime.pdb_protocol import (
    PROTOCOL_VERSION,
    PdbResponse,
)
from agentic_debugger.runtime.pdb_protocol import (
    serialize_response as _serialize_response,
)
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.python_launcher import (
    build_worker_env,
    is_windows_venv_redirector,
    resolve_worker_executable,
    resolve_worker_spawn,
)
from agentic_debugger.runtime.workspace import TaskWorkspace

REPO_ROOT = Path(__file__).resolve().parents[2]
CURATED_TASK_ID = "curated-off-by-one-002"
CURATED_MODULE = "recent_window.py"
CURATED_FOCUS = "recent_window"

VENV_EXE = r"C:\venv\Scripts\python.exe"
BASE_EXE = r"C:\Python312\python.exe"

WIN32_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the stock Windows venv redirector only exists on win32",
)

# The stock-launcher tests are meaningful only when the test runner itself
# is a stock venv redirector launch (the Windows CI smoke job installs
# .[app,test] into a normally created venv and invokes its untouched
# Scripts\python.exe).  Anywhere else they skip: a system interpreter
# cannot prove stock-venv behavior, and failing there would be an
# environmental false negative.  The CI job asserts venv-ness up front
# (sys.executable != sys._base_executable) so a misconfigured lane that
# silently skips is impossible there.
STOCK_VENV_ONLY = pytest.mark.skipif(
    sys.platform != "win32" or not is_windows_venv_redirector(),
    reason="requires the test runner to be a stock Windows venv launcher",
)


def _hello(pid: int, request_id: int = 1):
    return _serialize_response(
        PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request_id,
            success=True,
            result={"pid": pid, "protocol_version": PROTOCOL_VERSION},
            error="",
        )
    )


class _ExhaustibleStream:
    def __init__(self, responses):
        self._responses = list(responses)

    def readline(self, size=-1, *args, **kwargs):
        if self._responses:
            return self._responses.pop(0)
        return b""

    def read(self, size=-1):
        return b""

    def read1(self, size=-1):
        return b""

    def close(self):
        pass


def _mock_workspace(tmp_path: Path) -> MagicMock:
    (tmp_path / "probe.py").write_text("x = 1\n")
    ws = MagicMock(spec=TaskWorkspace)
    ws.root = str(tmp_path)
    return ws


def _start_with_mocked_transport(ws, hello_pid, proc_pid=9999):
    stdout = _ExhaustibleStream([_hello(hello_pid)])
    stderr = _ExhaustibleStream([])
    with patch(
        "agentic_debugger.runtime.pdb_session.subprocess.Popen"
    ) as popen:
        proc = MagicMock()
        proc.pid = proc_pid
        proc.poll.return_value = None
        proc.stdin = MagicMock()
        proc.stdout = stdout
        proc.stderr = stderr
        popen.return_value = proc
        session = PdbSession(ws)
        session._get_worker_argv = lambda: ["fake_python", "-c", "pass"]
        try:
            session.start()
        except Exception as exc:
            return session, proc, exc
        return session, proc, None


def _make_source(tmp_path: Path, name: str = "src") -> Path:
    src = tmp_path / name
    src.mkdir()
    (src / "placeholder.txt").write_text("x", encoding="utf-8")
    return src


# ---------------------------------------------------------------------------
# 1. Authority decision table (deterministic, cross-platform)
# ---------------------------------------------------------------------------


class TestAuthorityDecisionTable:
    def test_redirector_detected_on_win32(self):
        assert (
            is_windows_venv_redirector(
                executable=VENV_EXE,
                base_executable=BASE_EXE,
                platform="win32",
            )
            is True
        )

    def test_no_redirector_when_paths_equal(self):
        assert (
            is_windows_venv_redirector(
                executable=BASE_EXE,
                base_executable=BASE_EXE,
                platform="win32",
            )
            is False
        )

    def test_no_redirector_case_insensitive_equal(self):
        assert (
            is_windows_venv_redirector(
                executable=r"C:\PYTHON312\PYTHON.EXE",
                base_executable=r"c:\python312\python.exe",
                platform="win32",
            )
            is False
        )

    def test_no_redirector_on_posix(self):
        # POSIX venvs use symlinks/copies: the launched process IS the
        # direct child, so the identity invariant already holds and the
        # launcher bypass must not engage.
        assert (
            is_windows_venv_redirector(
                executable="/venv/bin/python",
                base_executable="/usr/bin/python3",
                platform="linux",
            )
            is False
        )

    def test_no_redirector_when_base_missing(self, monkeypatch):
        # Frozen/ancient interpreters lack sys._base_executable: without a
        # base to compare against, no redirector may be inferred.
        monkeypatch.delattr(sys, "_base_executable", raising=False)
        assert (
            is_windows_venv_redirector(
                executable=VENV_EXE,
                platform="win32",
            )
            is False
        )

    def test_resolve_returns_base_inside_venv(self):
        assert (
            resolve_worker_executable(
                executable=VENV_EXE,
                base_executable=BASE_EXE,
                platform="win32",
            )
            == BASE_EXE
        )

    def test_resolve_returns_exe_outside_venv(self):
        assert (
            resolve_worker_executable(
                executable=BASE_EXE,
                base_executable=BASE_EXE,
                platform="win32",
            )
            == BASE_EXE
        )
        assert (
            resolve_worker_executable(
                executable="/usr/bin/python3",
                base_executable="/usr/bin/python3",
                platform="linux",
            )
            == "/usr/bin/python3"
        )

    def test_env_sets_launcher_inside_venv(self):
        env = build_worker_env(
            None,
            executable=VENV_EXE,
            base_executable=BASE_EXE,
            platform="win32",
            environ={"PATH": "x"},
        )
        assert env is not None
        assert env["__PYVENV_LAUNCHER__"] == VENV_EXE
        assert env["PATH"] == "x"

    def test_env_overwrites_caller_forged_launcher(self):
        env = build_worker_env(
            {"__PYVENV_LAUNCHER__": r"C:\evil\python.exe"},
            executable=VENV_EXE,
            base_executable=BASE_EXE,
            platform="win32",
            environ={},
        )
        # The launcher identity is parent-derived, never caller-supplied.
        assert env["__PYVENV_LAUNCHER__"] == VENV_EXE

    def test_env_input_mapping_not_mutated(self):
        base = {"A": "1"}
        build_worker_env(
            base,
            executable=VENV_EXE,
            base_executable=BASE_EXE,
            platform="win32",
            environ={},
        )
        assert base == {"A": "1"}

    def test_env_none_outside_venv_without_stale(self):
        assert (
            build_worker_env(
                None,
                executable=BASE_EXE,
                base_executable=BASE_EXE,
                platform="win32",
                environ={"PATH": "x"},
            )
            is None
        )

    def test_env_scrubs_stale_launcher_when_inheriting(self):
        env = build_worker_env(
            None,
            executable=BASE_EXE,
            base_executable=BASE_EXE,
            platform="win32",
            environ={"PATH": "x", "__PYVENV_LAUNCHER__": VENV_EXE},
        )
        assert env is not None
        assert "__PYVENV_LAUNCHER__" not in env
        assert env["PATH"] == "x"

    def test_env_mapping_outside_venv_scrubs_forged_launcher(self):
        env = build_worker_env(
            {"A": "1", "__PYVENV_LAUNCHER__": VENV_EXE},
            executable=BASE_EXE,
            base_executable=BASE_EXE,
            platform="win32",
            environ={},
        )
        assert "__PYVENV_LAUNCHER__" not in env
        assert env["A"] == "1"

    def test_resolve_worker_spawn_pair(self):
        exe, env = resolve_worker_spawn(
            None,
            executable=VENV_EXE,
            base_executable=BASE_EXE,
            platform="win32",
            environ={},
        )
        assert exe == BASE_EXE
        assert env["__PYVENV_LAUNCHER__"] == VENV_EXE

    def test_invalid_inputs_raise(self):
        with pytest.raises(ValueError):
            resolve_worker_executable(executable="", platform="win32")
        with pytest.raises(ValueError):
            build_worker_env(None, executable="", platform="win32")
        with pytest.raises(ValueError):
            build_worker_env(
                "not-a-mapping",
                executable=VENV_EXE,
                base_executable=BASE_EXE,
                platform="win32",
                environ={},
            )


# ---------------------------------------------------------------------------
# 2. Launch-authority wiring + strict PID identity
# ---------------------------------------------------------------------------


class TestSessionWiring:
    def test_pdb_argv_uses_resolved_executable(self, tmp_path):
        ws = _mock_workspace(tmp_path)
        session = PdbSession(ws)
        assert session._get_worker_argv()[0] == resolve_worker_executable()
        assert session._get_worker_argv()[1:4] == ["-I", "-u", "-c"]

    def test_pdb_worker_env_matches_authority(self, tmp_path):
        ws = _mock_workspace(tmp_path)
        session = PdbSession(ws)
        assert session._worker_env() == build_worker_env(None)

    def test_contained_session_worker_env_is_none(self):
        from agentic_debugger.quixbugs.contained_pdb import ContainedPdbSession

        session = ContainedPdbSession.__new__(ContainedPdbSession)
        assert session._worker_env() is None

    def test_worker_process_argv_uses_resolved_executable(self, tmp_path):
        from agentic_debugger.application.events import SourceKind
        from agentic_debugger.application.session import (
            SessionBudgets,
            SessionSpec,
        )
        from agentic_debugger.application.sources import ExecutionSourceSpec
        from agentic_debugger.application.worker_process import (
            SessionWorkerProcess,
        )

        spec = SessionSpec(
            task_id="probe-task",
            source=ExecutionSourceSpec(
                kind=SourceKind.OFFLINE_DEMO,
                task_id="probe-task",
                policy="static-baseline",
            ),
            budgets=SessionBudgets(),
        )
        worker = SessionWorkerProcess(
            session_dir=tmp_path / "sess",
            session_id="probe-session",
            spec=spec,
            run_id="run-probe",
            scenario="synthetic_work",
            scenario_params={"steps": 1, "step_interval_seconds": 0.01},
        )
        try:
            assert worker._worker_argv()[0] == resolve_worker_executable()
            assert worker._worker_argv()[1:4] == ["-I", "-u", "-c"]
        finally:
            worker.close()

    def test_matching_pid_handshake_succeeds(self, tmp_path):
        from agentic_debugger.runtime.pdb_session import PdbSessionState

        ws = _mock_workspace(tmp_path)
        session, _proc, error = _start_with_mocked_transport(
            ws, hello_pid=9999, proc_pid=9999
        )
        assert error is None
        assert session.state is PdbSessionState.READY
        session.stop()

    def test_wrong_pid_handshake_still_fails(self, tmp_path):
        from agentic_debugger.runtime.exceptions import PdbSessionError

        ws = _mock_workspace(tmp_path)
        session, _proc, error = _start_with_mocked_transport(
            ws, hello_pid=7777, proc_pid=9999
        )
        assert isinstance(error, PdbSessionError)
        assert "PID mismatch" in str(error)


# ---------------------------------------------------------------------------
# 3. Live handshake: real worker, strict PID identity (all platforms)
# ---------------------------------------------------------------------------


class TestLiveHandshake:
    def test_real_session_handshake_and_stop(self, tmp_path):
        src = _make_source(tmp_path)
        ws = TaskWorkspace(str(src), parent_dir=str(tmp_path))
        session = PdbSession(ws)
        try:
            session.start()
            # Handshake success is itself the PID-equality proof: any
            # redirector/grandchild mismatch fails closed in _handshake.
            assert session.is_alive
            assert session._proc is not None
            assert session._proc.poll() is None
        finally:
            session.stop()
        assert not session.is_alive


# ---------------------------------------------------------------------------
# 4. Stock Windows venv launcher (Windows-only, untouched Scripts\python.exe)
# ---------------------------------------------------------------------------


class TestStockVenvLauncher:
    @STOCK_VENV_ONLY
    def test_running_inside_real_venv_redirector(self):
        # Reached only when the runner is a stock venv redirector launch
        # (Windows CI smoke job).  The skip marker above -- plus the CI
        # job's up-front venv-ness assertion -- keeps a misconfigured
        # lane honest without red-on-system-python false negatives.
        launcher = Path(sys.executable)
        assert launcher.name.lower() == "python.exe"
        assert "python-real" not in launcher.name.lower()
        assert launcher.is_file()

    @STOCK_VENV_ONLY
    def test_stock_launcher_session_succeeds(self, tmp_path):
        src = _make_source(tmp_path)
        ws = TaskWorkspace(str(src), parent_dir=str(tmp_path))
        session = PdbSession(ws)
        try:
            # The repaired path launches the base interpreter directly:
            # no redirector hop, so the Popen PID is the worker PID.
            assert session._get_worker_argv()[0] == sys._base_executable
            session.start()
            assert session.is_alive
        finally:
            session.stop()
        assert not session.is_alive

    @STOCK_VENV_ONLY
    def test_venv_semantics_preserved_in_worker(self, tmp_path):
        probe = (
            "import sys\n"
            "open('venv_probe.txt', 'w').write("
            "sys.prefix + chr(10) + sys.executable + chr(10) "
            "+ sys.base_prefix)\n"
            "try:\n"
            "    import agentic_debugger\n"
            "    open('venv_import.txt', 'w').write("
            "agentic_debugger.__file__)\n"
            "except Exception as e:\n"
            "    open('venv_import.txt', 'w').write('IMPORT_FAIL:' + str(e))\n"
            "try:\n"
            "    import pytest\n"
            "    open('venv_pytest.txt', 'w').write(pytest.__version__)\n"
            "except Exception as e:\n"
            "    open('venv_pytest.txt', 'w').write('IMPORT_FAIL:' + str(e))\n"
        )
        src = _make_source(tmp_path)
        (src / "probe_venv.py").write_text(probe, encoding="utf-8")
        ws = TaskWorkspace(str(src), parent_dir=str(tmp_path))
        session = PdbSession(ws, startup_timeout=15.0, request_timeout=30.0)
        try:
            session.start()
            response = session.run_post_mortem("probe_venv.py")
            assert response.result.get("status") == "exited"
        finally:
            session.stop()
        root = Path(ws.root)
        prefix, executable, base_prefix = (
            (root / "venv_probe.txt").read_text(encoding="utf-8").split("\n")
        )
        # The worker stays in the intended venv: same prefix and launcher
        # identity as the parent, base interpreter underneath -- never a
        # global/system-site fallback.
        assert prefix == sys.prefix
        assert executable == sys.executable
        assert base_prefix == sys.base_prefix
        assert prefix != base_prefix
        assert "IMPORT_FAIL" not in (
            root / "venv_import.txt"
        ).read_text(encoding="utf-8")
        assert "IMPORT_FAIL" not in (root / "venv_pytest.txt").read_text(
            encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# 5. Local Project PDB path (offline, curated fixture)
# ---------------------------------------------------------------------------


class TestLocalProjectPdbPath:
    def test_local_project_pdb_construction_reaches_breakpoint(
        self, tmp_path
    ):
        # Same construction as Local Project's non-proof PDB factory
        # (startup 15 s / request 60 s): an offline probe session must
        # reach the declared breakpoint and return bounded evidence.
        fixture = (
            REPO_ROOT
            / "agentic_debugger"
            / "datasets"
            / "curated"
            / CURATED_TASK_ID
        )
        assert fixture.is_dir()
        ws = TaskWorkspace(str(fixture), parent_dir=str(tmp_path))
        module_path = Path(ws.root) / CURATED_MODULE
        assert module_path.is_file()
        original = module_path.read_text(encoding="utf-8")
        call_text = f"import {CURATED_MODULE[:-3]}\n{CURATED_MODULE[:-3]}.{CURATED_FOCUS}(list(range(10)), 3)\n"
        module_path.write_text(original + "\n" + call_text, encoding="utf-8")
        breakpoint_line = (original + "\n" + call_text).splitlines().index(
            call_text.splitlines()[-1]
        ) + 1
        session = PdbSession(
            ws, startup_timeout=15.0, request_timeout=60.0
        )
        try:
            session.start()
            started = session.start_paused_target(
                CURATED_MODULE, [breakpoint_line]
            )
            assert started.get("state") == "paused"
            assert started.get("script") == CURATED_MODULE
            stack = session.get_stack_summary()
            assert isinstance(stack.get("frames"), list)
            assert len(stack["frames"]) >= 1
        finally:
            session.stop()
        assert not session.is_alive


# ---------------------------------------------------------------------------
# 6. Ladder path: worker -> nested PDB topology with verified cleanup
# ---------------------------------------------------------------------------


class TestLadderPath:
    def test_worker_nested_pdb_completes_with_cleanup(self, tmp_path):
        from agentic_debugger.application.events import SourceKind
        from agentic_debugger.application.process_tree import pid_is_alive
        from agentic_debugger.application.session import (
            SessionBudgets,
            SessionSpec,
            SessionStatus,
        )
        from agentic_debugger.application.sources import ExecutionSourceSpec
        from agentic_debugger.application.worker_process import (
            SessionWorkerProcess,
        )

        diag = tmp_path / "ladder-pdb.json"
        spec = SessionSpec(
            task_id=CURATED_TASK_ID,
            source=ExecutionSourceSpec(
                kind=SourceKind.OFFLINE_DEMO,
                task_id=CURATED_TASK_ID,
                policy="static-baseline",
            ),
            budgets=SessionBudgets(),
        )
        worker = SessionWorkerProcess(
            session_dir=tmp_path / "ladder-pdb",
            session_id="ladder-pdb",
            spec=spec,
            run_id="run-ladder-pdb",
            scenario="pdb_session",
            scenario_params={
                "task_id": CURATED_TASK_ID,
                "module": CURATED_MODULE,
                "focus": CURATED_FOCUS,
                "diag_path": str(diag),
            },
            cooperative_grace_seconds=5.0,
            ready_timeout_seconds=60.0,
        )
        try:
            assert worker.start() is None
            result = worker.wait()
            assert result.status is SessionStatus.SUCCEEDED
            payload = json.loads(diag.read_text(encoding="utf-8"))
            assert payload["pdb_worker_pid"] is not None
            assert payload["pdb_worker_gone_after_stop"] is True
            assert pid_is_alive(payload["pdb_worker_pid"]) is False
        finally:
            worker.close()


# ---------------------------------------------------------------------------
# 7. Cleanup + no-workaround-artifacts
# ---------------------------------------------------------------------------


class TestCleanupAndNoWorkaround:
    def test_stopped_session_leaves_no_live_worker(self, tmp_path):
        from agentic_debugger.application.process_tree import pid_is_alive

        src = _make_source(tmp_path)
        ws = TaskWorkspace(str(src), parent_dir=str(tmp_path))
        session = PdbSession(ws)
        try:
            session.start()
            pid = session._proc.pid
            assert pid_is_alive(pid) is True
        finally:
            session.stop()
        assert not session.is_alive
        assert pid_is_alive(pid) is False

    def test_no_copied_or_renamed_interpreter_workaround(self):
        # The python-real.exe workaround (or any equivalent) must not
        # exist: the stock launcher is the only supported entry point.
        # Bounded deterministic scan over the repository root plus the
        # active interpreter's prefix/base_prefix and their Scripts/bin
        # interpreter directories -- exactly where the historical
        # <venv>\Scripts\python-real.exe lived.  Non-recursive per
        # directory so a huge base installation is never crawled.
        # Only the explicitly known workaround basenames (exact match,
        # case-insensitive) and the narrow *python-real*/*python_real*
        # patterns are flagged, so legitimate python.exe/pythonw.exe
        # are never false positives.
        scan_dirs: list[Path] = [REPO_ROOT]
        for _attr in ("prefix", "base_prefix"):
            _val = getattr(sys, _attr, None)
            if type(_val) is str and _val:
                _base = Path(_val)
                scan_dirs.extend(
                    [_base, _base / "Scripts", _base / "bin"]
                )
        _known = frozenset(
            {
                "python-real.exe",
                "python_real.exe",
                "python-real",
                "python_real",
            }
        )
        _patterns = ("*python-real*", "*python_real*")
        hits: list[str] = []
        seen_dirs: set[str] = set()
        seen_hits: set[str] = set()
        for root in scan_dirs:
            try:
                key = str(root.resolve())
            except OSError:
                continue
            if key in seen_dirs or not root.is_dir():
                continue
            seen_dirs.add(key)
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    name_lower = entry.name.lower()
                except OSError:
                    continue
                if name_lower not in _known:
                    continue
                try:
                    is_file = entry.is_file() or entry.is_symlink()
                except OSError:
                    continue
                if not is_file:
                    continue
                hit = str(entry)
                if hit not in seen_hits:
                    seen_hits.add(hit)
                    hits.append(hit)
            for pattern in _patterns:
                try:
                    candidates = list(root.glob(pattern))
                except OSError:
                    continue
                for p in candidates:
                    try:
                        if not (p.is_file() or p.is_symlink()):
                            continue
                    except OSError:
                        continue
                    hit = str(p)
                    if hit not in seen_hits:
                        seen_hits.add(hit)
                        hits.append(hit)
        hits.sort()
        assert hits == [], f"interpreter workaround artifacts: {hits}"

    def test_launch_authority_has_no_workaround_reference(self):
        text = (
            REPO_ROOT
            / "agentic_debugger"
            / "runtime"
            / "python_launcher.py"
        ).read_text(encoding="utf-8")
        assert "python-real" not in text.lower()
        assert "python_real" not in text.lower()

    def test_argv_uses_real_interpreter_name(self, tmp_path):
        ws = _mock_workspace(tmp_path)
        argv0 = Path(PdbSession(ws)._get_worker_argv()[0]).name.lower()
        assert argv0 in ("python.exe", "python", "python3", "python3.exe")
        assert "real" not in argv0


def _wait_for_json(path: Path, timeout_seconds: float = 60.0):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.1)
    return None
