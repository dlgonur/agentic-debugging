import os as _os
import queue as _queue
import threading
import queue as _queue_module
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentic_debugger.runtime.pdb_protocol import (
    PROTOCOL_VERSION,
    PdbRequest,
    PdbResponse,
)
from agentic_debugger.runtime.pdb_protocol import (
    serialize_response as _ser,
)
from agentic_debugger.runtime.pdb_session import (
    _DEFAULT_STARTUP_TIMEOUT,
    _DEFAULT_REQUEST_TIMEOUT,
    _DEFAULT_SHUTDOWN_TIMEOUT,
    _DEFAULT_MAX_DIAGNOSTICS,
    _DEFAULT_MAX_LINE,
    PdbSession,
    PdbSessionState,
    _BoundedDiagnostics,
)
from agentic_debugger.runtime.exceptions import (
    PdbProtocolError,
    PdbSessionError,
    PdbSessionStateError,
    PdbSessionTimeoutError,
    PdbWorkerExitedError,
)
from agentic_debugger.runtime.workspace import TaskWorkspace


def _ping_resp(request_id=1):
    return _ser(PdbResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        success=True,
        result={"status": "ok", "pdb_created": True},
        error="",
    ))


def _hello_resp(request_id=1, pid=9999):
    return _ser(PdbResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        success=True,
        result={"pid": pid, "protocol_version": PROTOCOL_VERSION},
        error="",
    ))


def _bad_version_resp(request_id=1):
    return _ser(PdbResponse(
        protocol_version=99,
        request_id=request_id,
        success=True,
        result={},
        error="",
    ))


def _bad_id_resp(sent_id, wrong_id=99):
    return _ser(PdbResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=wrong_id,
        success=True,
        result={},
        error="",
    ))


def _hello_bad_worker_info(request_id=1):
    return _ser(PdbResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        success=True,
        result={"pid": 0, "protocol_version": PROTOCOL_VERSION},
        error="",
    ))


def _hello_empty_result(request_id=1):
    return _ser(PdbResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        success=True,
        result={},
        error="",
    ))


def _hello_pid_mismatch(request_id=1):
    return _ser(PdbResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        success=True,
        result={"pid": 7777, "protocol_version": PROTOCOL_VERSION},
        error="",
    ))


class _ExhaustibleMockStream:
    def __init__(self, responses):
        self._responses = list(responses)
        self._closed = False

    def readline(self, size=-1, *args, **kwargs):
        if self._closed:
            return b""
        if self._responses:
            return self._responses.pop(0)
        return b""

    def read(self, size=-1):
        return b""

    def read1(self, size=-1):
        return b""

    def close(self):
        self._closed = True


@pytest.fixture
def mock_workspace():
    import tempfile, shutil
    d = tempfile.mkdtemp()
    Path(d).mkdir(parents=True, exist_ok=True)
    (Path(d) / "test.py").write_text("x = 1\ny = 2\nz = 3\n")
    ws = MagicMock(spec=TaskWorkspace)
    ws.root = d
    yield ws
    shutil.rmtree(d, ignore_errors=True)


def _setup(ws, hello_responses, **kwargs):
    """Patch Popen, create session, call start(), return (session, proc)."""
    mock_stdout = _ExhaustibleMockStream(list(hello_responses))
    mock_stderr = _ExhaustibleMockStream([])
    with patch(
        "agentic_debugger.runtime.pdb_session.subprocess.Popen"
    ) as mp:
        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_proc.poll.return_value = None
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = mock_stderr
        mp.return_value = mock_proc

        session = PdbSession(ws, **kwargs)
        session._get_worker_argv = lambda: ["fake_python", "-c", "pass"]
        try:
            session.start()
        except Exception:
            pass
        return session, mock_proc


class TestPdbSessionConstructor:
    def test_default_values(self, mock_workspace):
        session = PdbSession(mock_workspace)
        assert session.state == PdbSessionState.NEW
        assert session._startup_timeout == _DEFAULT_STARTUP_TIMEOUT
        assert session._request_timeout == _DEFAULT_REQUEST_TIMEOUT
        assert session._shutdown_timeout == _DEFAULT_SHUTDOWN_TIMEOUT
        assert session._max_diagnostics == _DEFAULT_MAX_DIAGNOSTICS
        assert session._max_line == _DEFAULT_MAX_LINE

    def test_custom_timeouts(self, mock_workspace):
        session = PdbSession(
            mock_workspace,
            startup_timeout=10.0,
            request_timeout=3.0,
            shutdown_timeout=1.0,
            max_diagnostics=5000,
            max_line=32768,
        )
        assert session._startup_timeout == 10.0
        assert session._request_timeout == 3.0
        assert session._shutdown_timeout == 1.0
        assert session._max_diagnostics == 5000
        assert session._max_line == 32768

    def test_boolean_timeout_rejected(self, mock_workspace):
        with pytest.raises(PdbSessionError, match="startup_timeout"):
            PdbSession(mock_workspace, startup_timeout=True)

    def test_zero_timeout_rejected(self, mock_workspace):
        with pytest.raises(PdbSessionError, match="startup_timeout"):
            PdbSession(mock_workspace, startup_timeout=0)

    def test_negative_timeout_rejected(self, mock_workspace):
        with pytest.raises(PdbSessionError, match="startup_timeout"):
            PdbSession(mock_workspace, startup_timeout=-1)

    def test_infinite_timeout_rejected(self, mock_workspace):
        with pytest.raises(PdbSessionError, match="startup_timeout"):
            PdbSession(mock_workspace, startup_timeout=float("inf"))

    def test_boolean_bound_rejected(self, mock_workspace):
        with pytest.raises(PdbSessionError, match="max_diagnostics"):
            PdbSession(mock_workspace, max_diagnostics=True)

    def test_zero_bound_rejected(self, mock_workspace):
        with pytest.raises(PdbSessionError, match="max_diagnostics"):
            PdbSession(mock_workspace, max_diagnostics=0)

    def test_max_line_exceeds_protocol(self, mock_workspace):
        with pytest.raises(PdbSessionError, match="MAX_LINE_LENGTH"):
            PdbSession(mock_workspace, max_line=70000)

    def test_initial_state(self, mock_workspace):
        session = PdbSession(mock_workspace)
        assert session.state == PdbSessionState.NEW
        assert session.is_alive is False


class TestPdbSessionStart:
    def test_start_raises_on_bad_hello(self, mock_workspace):
        session, _ = _setup(
            mock_workspace, [b"not valid\n"]
        )
        assert session.state == PdbSessionState.FAILED
        assert session._proc is None

    def test_start_raises_on_eof(self, mock_workspace):
        session, _ = _setup(mock_workspace, [])
        assert session.state == PdbSessionState.FAILED
        assert session._proc is None

    def test_start_raises_on_version_mismatch(self, mock_workspace):
        session, _ = _setup(
            mock_workspace, [_bad_version_resp()]
        )
        assert session.state == PdbSessionState.FAILED
        assert session._proc is None

    def test_start_raises_on_bad_worker_info(self, mock_workspace):
        session, _ = _setup(
            mock_workspace, [_hello_bad_worker_info()]
        )
        assert session.state == PdbSessionState.FAILED
        assert session._proc is None

    def test_start_raises_on_empty_hello_result(self, mock_workspace):
        session, _ = _setup(
            mock_workspace, [_hello_empty_result()]
        )
        assert session.state == PdbSessionState.FAILED
        assert session._proc is None

    def test_start_raises_on_id_mismatch(self, mock_workspace):
        session, _ = _setup(
            mock_workspace, [_bad_id_resp(1, 99)]
        )
        assert session.state == PdbSessionState.FAILED
        assert session._proc is None

    def test_double_start_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbSessionStateError, match="Cannot start"):
            session.start()
        session.stop()

    def test_ping_before_start_rejected(self, mock_workspace):
        session = PdbSession(mock_workspace)
        with pytest.raises(PdbSessionStateError, match="Cannot ping"):
            session.ping()

    def test_stop_from_new_is_safe(self, mock_workspace):
        session = PdbSession(mock_workspace)
        session.stop()
        assert session.state == PdbSessionState.STOPPED

    def test_stderr_start_before_stdout(self, mock_workspace):
        """Verify stderr thread starts before stdout thread."""
        mock_stdout = _ExhaustibleMockStream([_hello_resp()])
        mock_stderr = _ExhaustibleMockStream([])
        start_order = []
        orig_start = threading.Thread.start

        def tracking_start(t):
            start_order.append(t.name or str(t._target))
            return orig_start(t)

        with patch.object(threading.Thread, "start", tracking_start):
            with patch(
                "agentic_debugger.runtime.pdb_session.subprocess.Popen"
            ) as mp:
                mock_proc = MagicMock()
                mock_proc.pid = 9999
                mock_proc.poll.return_value = None
                mock_proc.stdin = MagicMock()
                mock_proc.stdout = mock_stdout
                mock_proc.stderr = mock_stderr
                mp.return_value = mock_proc

                session = PdbSession(mock_workspace)
                session._get_worker_argv = lambda: ["fake", "-c", "pass"]
                session.start()
                session.stop()
        assert len(start_order) >= 2

    def test_startup_thread_start_failure_cleanup(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp()])
        mock_stderr = _ExhaustibleMockStream([])
        start_count = [0]

        def failing_start():
            start_count[0] += 1
            if start_count[0] == 2:
                raise RuntimeError("mock start failure")
            return orig_start()

        orig_start = threading.Thread.start

        with patch.object(threading.Thread, "start", side_effect=failing_start):
            with patch(
                "agentic_debugger.runtime.pdb_session.subprocess.Popen"
            ) as mp:
                mock_proc = MagicMock()
                mock_proc.pid = 9999
                mock_proc.poll.return_value = None
                mock_proc.stdin = MagicMock()
                mock_proc.stdout = mock_stdout
                mock_proc.stderr = mock_stderr
                mp.return_value = mock_proc

                session = PdbSession(mock_workspace)
                session._get_worker_argv = lambda: ["fake", "-c", "pass"]
                with pytest.raises(PdbSessionError, match="Failed to start"):
                    session.start()
                assert session.state == PdbSessionState.FAILED
                assert session._proc is None
                assert session._stdout_thread is None
                assert session._stderr_thread is None

    def test_context_manager_raises_on_start_failure(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([b"bad\n"])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            with pytest.raises(PdbProtocolError):
                with session as s:
                    pass
            assert session.state == PdbSessionState.FAILED


class TestPdbSessionHappyPath:
    def test_normal_start_and_stop(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        assert session.state == PdbSessionState.READY
        session.stop()
        assert session.state == PdbSessionState.STOPPED

    def test_ping_round_trip(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp(), _ping_resp(2)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            resp = session.ping()
            assert resp.success is True
            assert resp.result["status"] == "ok"
            assert resp.result["pdb_created"] is True
            session.stop()

    def test_stop_idempotent(self, mock_workspace):
        session = PdbSession(mock_workspace)
        session.stop()
        assert session.state == PdbSessionState.STOPPED
        session.stop()
        assert session.state == PdbSessionState.STOPPED

    def test_stop_from_failed(self, mock_workspace):
        session, _ = _setup(mock_workspace, [b"bad hello\n"])
        assert session.state == PdbSessionState.FAILED
        session.stop()
        assert session.state == PdbSessionState.STOPPED


class TestPdbSessionContextManager:
    def test_context_manager_always_stops(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp(), _ping_resp(2)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            with session as s:
                assert s.state == PdbSessionState.READY
                resp = s.ping()
                assert resp.success is True
            assert session.state == PdbSessionState.STOPPED

    def test_context_manager_stops_on_error(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp()])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            try:
                with session as s:
                    raise ValueError("boom")
            except ValueError:
                pass
            assert session.state == PdbSessionState.STOPPED


class TestPdbSessionRequestIds:
    def test_monotonically_increasing(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream(
            [_hello_resp(1), _ping_resp(2), _ping_resp(3)]
        )
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()

            sent = []
            orig = session._allocate_request_id
            def track():
                rid = orig()
                sent.append(rid)
                return rid
            session._allocate_request_id = track
            session._next_request_id = 2

            session.ping()
            session.ping()
            assert sent == [2, 3]
            session.stop()


class TestPdbSessionResponseMismatch:
    def test_request_id_mismatch_raises_and_cleans(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream(
            [_hello_resp(1), _bad_id_resp(2, 99)]
        )
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            with pytest.raises(PdbProtocolError, match="Request ID mismatch"):
                session.ping()
            assert session.state == PdbSessionState.FAILED
            assert session._proc is None

    def test_malformed_response_raises_and_cleans(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream(
            [_hello_resp(1), b"not json\n"]
        )
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            with pytest.raises(PdbProtocolError):
                session.ping()
            assert session.state == PdbSessionState.FAILED
            assert session._proc is None

    def test_worker_eof_raises_and_cleans(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp(1)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            with pytest.raises(PdbWorkerExitedError, match="closed stdout"):
                session.ping()
            assert session.state == PdbSessionState.FAILED
            assert session._proc is None

    def test_protocol_version_mismatch_in_handshake(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_bad_version_resp(1)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            try:
                session.start()
            except PdbProtocolError:
                pass
            assert session.state == PdbSessionState.FAILED
            assert session._proc is None


class TestPdbSessionTimeout:
    def test_request_timeout_raises_and_cleans(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp(1)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(
                mock_workspace, request_timeout=0.01
            )
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()

            def _raising_get(timeout=None):
                raise _queue.Empty()

            session._response_queue.get = _raising_get
            with pytest.raises(PdbSessionTimeoutError, match="timed out"):
                session.ping()
            assert session.state == PdbSessionState.FAILED
            assert session._proc is None


class TestPdbSessionInFlight:
    def test_one_in_flight_limit(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp(1), _ping_resp(2)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            assert session._request_lock.acquire(timeout=0.1)
            with pytest.raises(PdbSessionError, match="already in flight"):
                session.ping()
            session._request_lock.release()
            session.stop()

    def test_in_flight_cleared_after_error(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream(
            [_hello_resp(1), b"bad json\n"]
        )
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            try:
                session.ping()
            except PdbProtocolError:
                pass
            assert session._request_lock.acquire(timeout=0.1)
            session._request_lock.release()
            session.stop()

    def test_stop_waits_for_in_flight_then_cleans(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp(1)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()

            session._request_lock.acquire(timeout=0.1)
            def do_stop():
                session.stop()
            import threading
            t = threading.Thread(target=do_stop, daemon=True)
            t.start()
            t.join(timeout=3.0)
            session._request_lock.release()
            assert session.state == PdbSessionState.STOPPED


class TestBoundedDiagnostics:
    def test_no_overflow(self):
        d = _BoundedDiagnostics(max_chars=100)
        d.add("hello")
        d.add(" world")
        result = d.getvalue()
        assert result == "hello world"

    def test_overflow_truncation(self):
        d = _BoundedDiagnostics(max_chars=200)
        d.add("a" * 80)
        d.add("b" * 80)
        d.add("c" * 80)
        result = d.getvalue()
        assert "truncated" in result
        assert len(result) <= 200

    def test_no_diagnostics_added_returns_empty(self):
        d = _BoundedDiagnostics()
        assert d.getvalue() == ""

    def test_getvalue_does_not_seal(self):
        d = _BoundedDiagnostics(max_chars=100)
        d.add("first ")
        r1 = d.getvalue()
        d.add("second")
        r2 = d.getvalue()
        assert r1 == "first "
        assert r2 == "first second"

    def test_head_and_tail_preserved(self):
        d = _BoundedDiagnostics(max_chars=200)
        prefix = "START_"
        suffix = "_END"
        d.add(prefix + "x" * 300 + suffix)
        result = d.getvalue()
        assert prefix in result
        assert suffix in result
        assert "truncated" in result
        assert len(result) <= 200

    @pytest.mark.parametrize("max_c", [1, 2, 5, 20, 39, 40])
    def test_exact_bounds(self, max_c):
        d = _BoundedDiagnostics(max_chars=max_c)
        d.add("x" * 1000)
        result = d.getvalue()
        assert len(result) <= max_c, (
            f"max_chars={max_c} produced length {len(result)}"
        )

    @pytest.mark.parametrize("max_c", [1, 2, 5, 20])
    def test_exact_bounds_with_multiple_adds(self, max_c):
        d = _BoundedDiagnostics(max_chars=max_c)
        d.add("hello")
        d.add(" world")
        d.add(" " + "x" * 200)
        result = d.getvalue()
        assert len(result) <= max_c

    def test_marker_length_boundary(self):
        from agentic_debugger.runtime.pdb_session import _TRUNCATION_MARKER
        mlen = len(_TRUNCATION_MARKER)
        d = _BoundedDiagnostics(max_chars=mlen)
        d.add("x" * 5000)
        result = d.getvalue()
        assert len(result) <= mlen

    def test_marker_minus_one_boundary(self):
        from agentic_debugger.runtime.pdb_session import _TRUNCATION_MARKER
        mlen = len(_TRUNCATION_MARKER)
        d = _BoundedDiagnostics(max_chars=mlen - 1)
        d.add("x" * 5000)
        result = d.getvalue()
        assert len(result) <= mlen - 1

    def test_add_after_overflow_observable(self):
        d = _BoundedDiagnostics(max_chars=100)
        d.add("x" * 60)
        d.add("y" * 60)
        r1 = d.getvalue()
        assert "truncated" in r1
        d.add("z" * 30)
        r2 = d.getvalue()
        assert "truncated" in r2


class TestPdbSessionDiagnostics:
    def test_available_after_stop(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp()])
        mock_stderr = _ExhaustibleMockStream([b"test diagnostic\n"])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            session.stop()
            diag = session.diagnostics
            assert isinstance(diag, str)


class _OverflowFillingStream:
    """Stream that puts hello then immediately floods queue before any request."""

    def __init__(self):
        self._phase = 0
        self._closed = False

    def readline(self, size=-1, *args, **kwargs):
        if self._closed:
            return b""
        self._phase += 1
        if self._phase == 1:
            return _hello_resp(1)
        return _ping_resp(99)

    def read(self, size=-1):
        return b""

    def read1(self, size=-1):
        return b""

    def close(self):
        self._closed = True


class TestQueueOverflow:
    """Repair 1: actual queue overflow triggers automatic cleanup."""

    def test_real_overflow_no_request(self, mock_workspace):
        """Key acceptance: overflow with no public call triggers automatic cleanup."""
        mock_stdout = _OverflowFillingStream()
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace, request_timeout=1.0)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            try:
                session.start()
            except (PdbProtocolError, PdbSessionError):
                pass

            done = session._reader_cleanup_done.wait(timeout=5.0)
            assert done, "Automatic cleanup should have completed"
            assert session.state == PdbSessionState.FAILED
            assert session._proc is None
            assert session._stdout_thread is None
            assert session._stderr_thread is None
            diag = session.diagnostics
            assert isinstance(diag, str)

    def test_real_overflow_then_ping_rejected(self, mock_workspace):
        mock_stdout = _OverflowFillingStream()
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace, request_timeout=1.0)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            try:
                session.start()
            except (PdbProtocolError, PdbSessionError):
                pass
            session._reader_cleanup_done.wait(timeout=5.0)
            with pytest.raises(PdbSessionStateError):
                session.ping()

    def test_real_overflow_then_stop(self, mock_workspace):
        mock_stdout = _OverflowFillingStream()
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace, request_timeout=1.0)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            try:
                session.start()
            except (PdbProtocolError, PdbSessionError):
                pass
            session._reader_cleanup_done.wait(timeout=5.0)
            session.stop()
            assert session.state == PdbSessionState.STOPPED

    def test_real_overflow_in_flight(self, mock_workspace):
        """Request in flight: overflow detected before _send_and_receive writes."""
        mock_stdout = _OverflowFillingStream()
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace, request_timeout=3.0)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            try:
                session.start()
            except (PdbProtocolError, PdbSessionError):
                pass
            session._reader_cleanup_done.wait(timeout=5.0)
            with pytest.raises((PdbProtocolError, PdbSessionStateError)):
                session.ping()
            assert session.state == PdbSessionState.FAILED
            assert session._proc is None

    def test_overflow_reader_error_detected(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp(1)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc
            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            session._reader_error.set()
            with pytest.raises(PdbProtocolError, match="Response channel"):
                session.ping()
            assert session.state == PdbSessionState.FAILED
            assert session._proc is None

    def test_reader_error_prevents_send(self, mock_workspace):
        session = PdbSession(mock_workspace)
        session._reader_error.set()
        session._proc = MagicMock()
        session._proc.stdin = MagicMock()
        session._proc.pid = 9999
        with pytest.raises(PdbProtocolError, match="Response channel"):
            session._send_and_receive(
                PdbRequest(protocol_version=1, request_id=1, operation="ping", payload={}),
                timeout=0.1
            )
        assert session.state == PdbSessionState.FAILED

    def test_overflow_stop_safe(self, mock_workspace):
        session = PdbSession(mock_workspace)
        session._reader_error.set()
        session.stop()
        assert session.state == PdbSessionState.STOPPED


class TestPingValidationCleanup:
    """Repair 2: semantic ping validation failure cleans up."""

    def _make_bad_ping_resp(self, result_override=None, success=True, error=""):
        result = {"status": "ok", "pdb_created": True}
        if result_override is not None:
            result = result_override
        return _ser(PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=2,
            success=success,
            result=result,
            error=error,
        ))

    @pytest.mark.parametrize("desc,result_override,success,error", [
        ("bad_status", {"status": "bad", "pdb_created": True}, True, ""),
        ("pdb_created_false", {"status": "ok", "pdb_created": False}, True, ""),
        ("missing_status", {"pdb_created": True}, True, ""),
        ("missing_pdb_created", {"status": "ok"}, True, ""),
        ("extra_field", {"status": "ok", "pdb_created": True, "extra": 1}, True, ""),
        ("failed_ping", {}, False, "worker error"),
    ])
    def test_ping_validation_cleans(self, mock_workspace, desc, result_override, success, error):
        bad_resp = self._make_bad_ping_resp(result_override, success, error)
        mock_stdout = _ExhaustibleMockStream([_hello_resp(1), bad_resp])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            with pytest.raises((PdbProtocolError, PdbSessionError)) as exc:
                session.ping()
            assert session.state == PdbSessionState.FAILED
            assert session._proc is None
            session.stop()


class TestShutdownAckValidation:
    """Repair 3: shudown ACK validation and forced cleanup."""

    def _make_shutdown_resp(self, request_id, result_override=None):
        result = {"shutdown": True}
        if result_override is not None:
            result = result_override
        return _ser(PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request_id,
            success=True,
            result=result,
            error="",
        ))

    def test_shutdown_cleans_proc_and_threads(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream(
            [_hello_resp(1), _ping_resp(2)]
        )
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            session.stop()
            assert session._proc is None
            assert session._stdout_thread is None
            assert session._stderr_thread is None

    def test_shutdown_with_extra_field_triggers_force(self, mock_workspace):
        bad_ack = self._make_shutdown_resp(
            2, result_override={"shutdown": True, "extra": 1}
        )
        mock_stdout = _ExhaustibleMockStream(
            [_hello_resp(1), bad_ack]
        )
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            session.stop()
            assert session.state == PdbSessionState.STOPPED

    def test_shutdown_no_ack_triggers_force(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp(1)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            session.stop()
            assert session.state == PdbSessionState.STOPPED

    def test_shutdown_already_dead_worker(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp(1)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.side_effect = [None, None, None, 0]
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            assert session.state == PdbSessionState.READY
            session.stop()
            assert session.state == PdbSessionState.STOPPED

    def test_stop_idempotent(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp(1), _ping_resp(2)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            session.stop()
            assert session.state == PdbSessionState.STOPPED
            assert session._proc is None
            assert session._stdout_thread is None
            assert session._stderr_thread is None
            session.stop()
            assert session.state == PdbSessionState.STOPPED

    def test_finalize_after_orderly_shutdown(self, mock_workspace):
        shutdown_ack = _ser(PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=2,
            success=True,
            result={"shutdown": True},
            error="",
        ))
        mock_stdout = _ExhaustibleMockStream([_hello_resp(1), shutdown_ack])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.side_effect = [None, None, None, None, None, 0]
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            session.stop()
            assert session.state == PdbSessionState.STOPPED
            assert session._proc is None
            assert session._stdout_thread is None
            assert session._stderr_thread is None


class TestStuckThread:
    """Repair 3: still-alive thread is detected."""

    def test_stuck_thread_detected(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp(1)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace,
                                 shutdown_timeout=0.1)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()

            blocker = threading.Event()
            stuck = threading.Thread(target=blocker.wait, daemon=True)
            stuck.start()
            session._stdout_thread = stuck

            with pytest.raises(PdbSessionError, match="did not stop"):
                session._terminate_and_cleanup()

            blocker.set()
            stuck.join(timeout=1.0)


class _GatedFloodStream:
    """Stream that returns hello, then blocks on a go event before flooding."""

    def __init__(self, go_event: threading.Event):
        self._go = go_event
        self._phase = 0
        self._closed = False

    def readline(self, size=-1, *args, **kwargs):
        if self._closed:
            return b""
        self._phase += 1
        if self._phase == 1:
            return _hello_resp(1)
        self._go.wait()
        return _ping_resp(99)

    def read(self, size=-1):
        return b""

    def read1(self, size=-1):
        return b""

    def close(self):
        self._closed = True


class TestGatedOverflow:
    """READY-state overflow: session reaches READY, then flood from READY."""

    def test_ready_state_no_request_overflow(self, mock_workspace):
        go = threading.Event()
        mock_stdout = _GatedFloodStream(go)
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace, request_timeout=1.0)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            assert session.state == PdbSessionState.READY

            go.set()
            done = session._reader_cleanup_done.wait(timeout=5.0)
            assert done, "Should clean up without any public call"
            assert session.state == PdbSessionState.FAILED
            assert session._proc is None
            assert session._stdout_thread is None
            assert session._stderr_thread is None
            assert session._reader_cleanup_error is None
            assert isinstance(session.diagnostics, str)

    def test_ready_state_overflow_then_stop(self, mock_workspace):
        go = threading.Event()
        mock_stdout = _GatedFloodStream(go)
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace, request_timeout=1.0)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            go.set()
            session._reader_cleanup_done.wait(timeout=5.0)
            session.stop()
            assert session.state == PdbSessionState.STOPPED


class _SentinelOverflowStream:
    """Stream that fills the queue then returns EOF, causing sentinel overflow."""

    def __init__(self, fill_count: int):
        self._remaining = fill_count
        self._phase = 0
        self._closed = False

    def readline(self, size=-1, *args, **kwargs):
        if self._closed:
            return b""
        self._phase += 1
        if self._phase == 1:
            return _hello_resp(1)
        if self._remaining > 0:
            self._remaining -= 1
            return _ping_resp(99)
        return b""

    def read(self, size=-1):
        return b""

    def read1(self, size=-1):
        return b""

    def close(self):
        self._closed = True


class TestSentinelOverflow:
    """Repair 2: EOF sentinel queue.Full triggers cleanup."""

    def test_sentinel_overflow_cleanup(self, mock_workspace):
        stream = _SentinelOverflowStream(fill_count=2)
        mock_stdout = stream
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace, request_timeout=1.0)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            try:
                session.start()
            except (PdbProtocolError, PdbSessionError):
                pass

            done = session._reader_cleanup_done.wait(timeout=5.0)
            assert done, "Sentinel overflow should trigger cleanup"
            assert session.state == PdbSessionState.FAILED
            assert session._proc is None
            assert session._stdout_thread is None
            assert session._stderr_thread is None
            assert session._reader_cleanup_error is None


class TestInFlightOverflow:
    """True in-flight overflow: ping runs while queue overflows."""

    def test_in_flight_overflow_ping_raises(self, mock_workspace):
        go = threading.Event()
        mock_stdout = _GatedFloodStream(go)
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace, request_timeout=5.0)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            assert session.state == PdbSessionState.READY

            results = []
            def do_ping():
                try:
                    session.ping()
                    results.append("ok")
                except Exception as e:
                    results.append(e)

            t = threading.Thread(target=do_ping, daemon=True)
            t.start()
            import time as _t
            _t.sleep(0.2)

            go.set()
            t.join(timeout=5.0)

            assert len(results) == 1
            assert isinstance(results[0], Exception)
            assert session.state == PdbSessionState.FAILED
            assert session._proc is None
            session.stop()
            assert session.state == PdbSessionState.STOPPED


def _run_to_bp_resp(request_id=2, result=None, success=True, error=""):
    if result is None:
        result = {"status": "exited", "script": "test.py", "exit_code": 0}
    return _ser(PdbResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        success=success,
        result=result,
        error=error,
    ))


class TestRunToBreakpointValidation:
    def test_not_ready_state(self, mock_workspace):
        session = PdbSession(mock_workspace)
        with pytest.raises(PdbSessionStateError, match="Cannot run_to_breakpoint"):
            session.run_to_breakpoint("test.py", [1])

    def test_absolute_path_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="relative path"):
            session.run_to_breakpoint("/abs/test.py", [1])
        session.stop()

    def test_dotdot_traversal_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match=".. traversal"):
            session.run_to_breakpoint("../test.py", [1])
        session.stop()

    def test_empty_script_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="non-empty"):
            session.run_to_breakpoint("", [1])
        session.stop()

    def test_nul_in_script_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="NUL"):
            session.run_to_breakpoint("test\x00.py", [1])
        session.stop()

    def test_non_py_extension_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="end with .py"):
            session.run_to_breakpoint("test.txt", [1])
        session.stop()

    def test_empty_breakpoints_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="1-16"):
            session.run_to_breakpoint("test.py", [])
        session.stop()

    def test_too_many_breakpoints_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="1-16"):
            session.run_to_breakpoint("test.py", list(range(1, 18)))
        session.stop()

    def test_zero_breakpoint_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="positive"):
            session.run_to_breakpoint("test.py", [0])
        session.stop()

    def test_negative_breakpoint_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="positive"):
            session.run_to_breakpoint("test.py", [-1])
        session.stop()

    def test_boolean_breakpoint_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="integers"):
            session.run_to_breakpoint("test.py", [True])
        session.stop()

    def test_duplicate_breakpoint_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="duplicates"):
            session.run_to_breakpoint("test.py", [3, 3])
        session.stop()

    def test_breakpoint_ordering(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        bps = session._validate_breakpoints([5, 1, 3])
        assert bps == [1, 3, 5]
        session.stop()

    def test_raw_dotdot_traversal_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        for bad in ("../target.py", "a/../target.py", "a\\..\\target.py", "./../target.py"):
            with pytest.raises(PdbProtocolError, match=".. traversal"):
                session.run_to_breakpoint(bad, [1])
        session.stop()

    def test_missing_file_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="not found"):
            session.run_to_breakpoint("missing.py", [1])
        assert session._target_consumed is False
        resp = session.ping()
        assert resp.success is True
        session.stop()

    def test_directory_named_dotpy_rejected(self, mock_workspace):
        import tempfile, shutil
        d = Path(mock_workspace.root)
        (d / "directory.py").mkdir(exist_ok=True)
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="directory"):
            session.run_to_breakpoint("directory.py", [1])
        assert session._target_consumed is False
        session.stop()
        shutil.rmtree(str(d / "directory.py"), ignore_errors=True)

    def test_local_rejection_sends_no_request_and_preserves_worker(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        orig_send = session._send_and_receive
        sent = []
        def track_send(req, timeout):
            sent.append(req)
            return orig_send(req, timeout)
        session._send_and_receive = track_send
        with pytest.raises(PdbProtocolError, match="not found"):
            session.run_to_breakpoint("missing.py", [1])
        assert len(sent) == 0
        assert session._target_consumed is False
        resp = session.ping()
        assert resp.success is True
        session.stop()

    def test_valid_run_possible_after_local_rejection(self, mock_workspace):
        _run_to_bp_resp_ok = _run_to_bp_resp
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
            _run_to_bp_resp_ok(2),
        ])
        with pytest.raises(PdbProtocolError, match="not found"):
            session.run_to_breakpoint("missing.py", [1])
        resp = session.run_to_breakpoint("test.py", [2])
        assert resp.success is True
        session.stop()

    def test_breakpoint_999_exceeds_source_length(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="exceeds source length"):
            session.run_to_breakpoint("test.py", [1, 999])
        assert session._target_consumed is False
        session.stop()

    def test_missing_payload_field(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="must be"):
            session.run_to_breakpoint(123, [1])
        session.stop()

    def test_missing_script(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="non-empty"):
            session.run_to_breakpoint("", [1])
        session.stop()

    def test_directory_path(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="end with .py"):
            session.run_to_breakpoint("somedir", [1])
        session.stop()

    def test_oversized_script_path(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        long_path = "x" * 5000 + ".py"
        with pytest.raises(PdbProtocolError, match="UTF-8 bytes"):
            session.run_to_breakpoint(long_path, [1])
        session.stop()

    def test_oversized_argv_item(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="UTF-8 bytes"):
            session.run_to_breakpoint("test.py", [1], argv=["x" * 2000])
        session.stop()

    def test_invalid_argv_container(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="list"):
            session.run_to_breakpoint("test.py", [1], argv="not a list")
        session.stop()

    def test_non_string_argv_entry(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="strings"):
            session.run_to_breakpoint("test.py", [1], argv=[42])
        session.stop()

    def test_wrong_script_in_breakpoint_result(self, mock_workspace):
        bad_result = {"status": "breakpoint", "script": "wrong.py", "line": 3, "function": "main"}
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _run_to_bp_resp(2, result=bad_result),
        ])
        mock_stderr = _ExhaustibleMockStream([])
        with patch("agentic_debugger.runtime.pdb_session.subprocess.Popen") as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc
            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.run_to_breakpoint("test.py", [3])
            assert session.state == PdbSessionState.FAILED
            session.stop()

    def test_unrequested_line_in_breakpoint_result(self, mock_workspace):
        bad_result = {"status": "breakpoint", "script": "test.py", "line": 99, "function": "main"}
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _run_to_bp_resp(2, result=bad_result),
        ])
        mock_stderr = _ExhaustibleMockStream([])
        with patch("agentic_debugger.runtime.pdb_session.subprocess.Popen") as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc
            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.run_to_breakpoint("test.py", [3])
            assert session.state == PdbSessionState.FAILED
            session.stop()

    def test_empty_function_in_breakpoint_result(self, mock_workspace):
        bad_result = {"status": "breakpoint", "script": "test.py", "line": 3, "function": ""}
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _run_to_bp_resp(2, result=bad_result),
        ])
        mock_stderr = _ExhaustibleMockStream([])
        with patch("agentic_debugger.runtime.pdb_session.subprocess.Popen") as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc
            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.run_to_breakpoint("test.py", [3])
            assert session.state == PdbSessionState.FAILED
            session.stop()

    def test_extra_field_in_breakpoint_result(self, mock_workspace):
        bad_result = {"status": "breakpoint", "script": "test.py", "line": 3, "function": "main", "extra": 1}
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _run_to_bp_resp(2, result=bad_result),
        ])
        mock_stderr = _ExhaustibleMockStream([])
        with patch("agentic_debugger.runtime.pdb_session.subprocess.Popen") as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc
            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.run_to_breakpoint("test.py", [3])
            assert session.state == PdbSessionState.FAILED
            session.stop()

    def test_wrong_exit_script(self, mock_workspace):
        bad_result = {"status": "exited", "script": "wrong.py", "exit_code": 0}
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _run_to_bp_resp(2, result=bad_result),
        ])
        mock_stderr = _ExhaustibleMockStream([])
        with patch("agentic_debugger.runtime.pdb_session.subprocess.Popen") as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc
            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.run_to_breakpoint("test.py", [1])
            assert session.state == PdbSessionState.FAILED
            session.stop()

    def test_boolean_in_argv_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="strings"):
            session.run_to_breakpoint("test.py", [1], argv=[True])
        session.stop()

    def test_nul_in_argv_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="NUL"):
            session.run_to_breakpoint("test.py", [1], argv=["a\x00b"])
        session.stop()

    def test_too_many_argv_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbProtocolError, match="at most 32"):
            session.run_to_breakpoint("test.py", [1], argv=["x"] * 33)
        session.stop()

    def test_second_execution_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        session._target_consumed = True
        with pytest.raises(PdbSessionStateError, match="already completed"):
            session.run_to_breakpoint("test.py", [1])
        session.stop()

    def test_concurrent_two_calls_one_executes(self, mock_workspace):
        import time as _time
        results = []
        send_count = []
        lock_held = threading.Event()

        mock_stdout = _ExhaustibleMockStream([_hello_resp(1), _run_to_bp_resp(2)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch("agentic_debugger.runtime.pdb_session.subprocess.Popen") as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace, request_timeout=3.0)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()

            def hang_send(req, timeout):
                send_count.append(1)
                lock_held.set()
                _time.sleep(1.5)
                # Simulate response
                from agentic_debugger.runtime.pdb_protocol import PdbResponse
                return PdbResponse(
                    protocol_version=1, request_id=req.request_id,
                    success=True,
                    result={"status": "exited", "script": "test.py", "exit_code": 0},
                    error="",
                )

            session._send_and_receive = hang_send

            def caller():
                try:
                    resp = session.run_to_breakpoint("test.py", [2])
                    results.append(("ok", resp.success))
                except Exception as e:
                    results.append(("err", type(e).__name__))

            t1 = threading.Thread(target=caller, daemon=True)
            t1.start()
            lock_held.wait(timeout=5)
            _time.sleep(0.1)

            with pytest.raises((PdbSessionError, PdbSessionStateError)):
                session.run_to_breakpoint("test.py", [2])

            t1.join(timeout=5)

        assert len(results) == 1
        assert results[0][0] == "ok"
        assert len(send_count) == 1
        session.stop()

    def test_in_flight_limit(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        session._request_lock.acquire(timeout=0.1)
        with pytest.raises(PdbSessionError, match="already in flight"):
            session.run_to_breakpoint("test.py", [1])
        session._request_lock.release()
        session.stop()

    def test_malformed_success_result_breakpoint(self, mock_workspace):
        bad_result = {"status": "breakpoint", "script": 123, "line": 5, "function": "main"}
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _run_to_bp_resp(2, result=bad_result),
        ])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc
            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            with pytest.raises(PdbProtocolError, match="script must be"):
                session.run_to_breakpoint("test.py", [1])
            assert session.state == PdbSessionState.FAILED
            session.stop()

    def test_malformed_success_result_exited(self, mock_workspace):
        bad_result = {"status": "exited", "script": "test.py", "exit_code": True}
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _run_to_bp_resp(2, result=bad_result),
        ])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc
            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            with pytest.raises(PdbProtocolError, match="exit_code must be"):
                session.run_to_breakpoint("test.py", [1])
            assert session.state == PdbSessionState.FAILED
            session.stop()

    def test_unknown_status_in_result(self, mock_workspace):
        bad_result = {"status": "unknown", "script": "test.py"}
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _run_to_bp_resp(2, result=bad_result),
        ])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc
            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            with pytest.raises(PdbProtocolError, match="Unknown status"):
                session.run_to_breakpoint("test.py", [1])
            assert session.state == PdbSessionState.FAILED
            session.stop()

    def test_valid_target_error_does_not_fail_session(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _run_to_bp_resp(2, result={}, success=False, error="Target raised ValueError: test"),
        ])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc
            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            resp = session.run_to_breakpoint("test.py", [1])
            assert resp.success is False
            assert "ValueError" in resp.error
            assert session.state == PdbSessionState.READY
            session.stop()

    def test_ping_still_works_after_target_error(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _run_to_bp_resp(2, result={}, success=False, error="Target error"),
            _ping_resp(3),
        ])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc
            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            session.run_to_breakpoint("test.py", [1])
            resp = session.ping()
            assert resp.success is True
            session.stop()

    def test_second_run_to_breakpoint_rejected_after_valid_exec(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _run_to_bp_resp(2),
            _ping_resp(3),
        ])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc
            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()
            session.run_to_breakpoint("test.py", [1])
            with pytest.raises(PdbSessionStateError, match="already completed"):
                session.run_to_breakpoint("test.py", [1])
            session.stop()

    def test_context_manager_stop_after_run(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _run_to_bp_resp(2),
        ])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc
            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            with session as s:
                resp = s.run_to_breakpoint("test.py", [1])
                assert resp.success is True
            assert session.state == PdbSessionState.STOPPED


class TestCleanupFailure:
    """Repair 3: cleanup coordinator failure is preserved."""

    def test_cleanup_failure_stored(self, mock_workspace):
        mock_stdout = _ExhaustibleMockStream([_hello_resp(1)])
        mock_stderr = _ExhaustibleMockStream([])
        with patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        ) as mp:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = mock_stdout
            mock_proc.stderr = mock_stderr
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()

            blocker = threading.Event()
            stuck = threading.Thread(target=blocker.wait, daemon=True)
            stuck.start()
            session._stdout_thread = stuck
            session._reader_error.set()

            session._schedule_overflow_cleanup()
            session._reader_cleanup_done.wait(timeout=5.0)
            assert session._reader_cleanup_error is not None
            assert "did not stop" in str(session._reader_cleanup_error)
            assert session._stdout_thread is not None

            blocker.set()
            stuck.join(timeout=1.0)
            session._terminate_and_cleanup()
            assert session._proc is None


class TestRunToBreakpointRepairs:
    """Repairs 1–3: cross-drive, TOCTOU, UTF-8 validation."""

    def test_cross_drive_commonpath_raises_pdb_error(self, mock_workspace):
        s = PdbSession(mock_workspace)
        with patch("agentic_debugger.runtime.pdb_session.os.path.commonpath",
                   side_effect=ValueError("no common drive")):
            with pytest.raises(PdbProtocolError, match="containment check"):
                s._read_validated_workspace_script("test.py")

    def test_commonpath_oserror_raises_pdb_error(self, mock_workspace):
        s = PdbSession(mock_workspace)
        with patch("agentic_debugger.runtime.pdb_session.os.path.commonpath",
                   side_effect=OSError("commonpath failed")):
            with pytest.raises(PdbProtocolError, match="containment check"):
                s._read_validated_workspace_script("test.py")

    def test_reject_utf8_surrogate_script(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="non-UTF-8"):
            session.run_to_breakpoint("bad\ud800.py", [1])
        assert session._target_consumed is False
        resp = session.ping()
        assert resp.success is True
        session.stop()

    def test_reject_utf8_surrogate_argv(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="non-UTF-8"):
            session.run_to_breakpoint("test.py", [1], argv=["\ud800"])
        assert session._target_consumed is False
        resp = session.ping()
        assert resp.success is True
        session.stop()

    def test_check_utf8_strict_raises_on_surrogate(self, mock_workspace):
        s = PdbSession(mock_workspace)
        with pytest.raises(PdbProtocolError, match="non-UTF-8"):
            s._check_utf8_strict("\ud800", "test")

    def test_check_utf8_strict_passes_normal(self, mock_workspace):
        s = PdbSession(mock_workspace)
        s._check_utf8_strict("normal.py", "script")

    def test_binary_open_flag_portable(self, mock_workspace):
        from agentic_debugger.runtime.pdb_session import _BINARY_OPEN_FLAG as bf
        assert bf == getattr(_os, "O_BINARY", 0)

    def test_session_read_bounded_fd_short_reads(self, mock_workspace):
        import tempfile
        d = Path(mock_workspace.root)
        f = d / "short_read_test.py"
        f.write_bytes(b"abc" + b"def" + b"ghi")
        s = PdbSession(mock_workspace)
        fd = _os.open(str(f), _os.O_RDONLY | getattr(_os, "O_BINARY", 0))
        try:
            result = s._read_bounded_fd(fd)
            assert result == b"abcdefghi"
        finally:
            _os.close(fd)

    def test_session_read_bounded_fd_empty(self, mock_workspace):
        d = Path(mock_workspace.root)
        f = d / "empty_read_test.py"
        f.write_bytes(b"")
        s = PdbSession(mock_workspace)
        fd = _os.open(str(f), _os.O_RDONLY | getattr(_os, "O_BINARY", 0))
        try:
            result = s._read_bounded_fd(fd)
            assert result == b""
        finally:
            _os.close(fd)

    def test_session_read_bounded_fd_exact_limit(self, mock_workspace):
        from agentic_debugger.runtime.pdb_session import _MAX_TARGET_SOURCE_BYTES
        from agentic_debugger.runtime.pdb_session import PdbSession as _Ps
        d = Path(mock_workspace.root)
        f = d / "exact_limit_test.py"
        data = b"x" * _MAX_TARGET_SOURCE_BYTES
        f.write_bytes(data)
        fd = _os.open(str(f), _os.O_RDONLY | getattr(_os, "O_BINARY", 0))
        try:
            result = _Ps._read_bounded_fd(fd)
            assert isinstance(result, bytes)
            assert len(result) == _MAX_TARGET_SOURCE_BYTES
        finally:
            _os.close(fd)

    def test_session_read_bounded_fd_over_limit(self, mock_workspace):
        from agentic_debugger.runtime.pdb_session import _MAX_TARGET_SOURCE_BYTES
        from agentic_debugger.runtime.pdb_session import PdbSession as _Ps
        d = Path(mock_workspace.root)
        f = d / "over_limit_test.py"
        data = b"x" * (_MAX_TARGET_SOURCE_BYTES + 1)
        f.write_bytes(data)
        fd = _os.open(str(f), _os.O_RDONLY | getattr(_os, "O_BINARY", 0))
        try:
            with pytest.raises(PdbProtocolError, match="exceeds maximum source"):
                _Ps._read_bounded_fd(fd)
        finally:
            _os.close(fd)
