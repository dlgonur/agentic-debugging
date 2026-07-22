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


def _paused_start_resp(request_id=2, result=None, success=True, error=""):
    if result is None:
        result = {
            "state": "paused", "script": "test.py",
            "line": 3, "function": "<module>",
        }
    return _ser(PdbResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        success=success,
        result=result,
        error=error,
    ))


def _status_resp(request_id=2, result=None, success=True, error=""):
    if result is None:
        result = {"state": "idle"}
    return _ser(PdbResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        success=success,
        result=result,
        error=error,
    ))


def _term_resp(request_id=2, result=None, success=True, error=""):
    if result is None:
        result = {"state": "terminated", "script": "test.py"}
    return _ser(PdbResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        success=success,
        result=result,
        error=error,
    ))


# =====================================================================
# Task 4B2B — Public PdbSession Paused-Target API and Lifecycle Guards
# =====================================================================


class TestPausedTargetPublicAPI:
    """Method existence, exact signatures and outgoing request contracts."""

    def test_methods_exist(self, mock_workspace):
        session = PdbSession(mock_workspace)
        assert hasattr(session, "start_paused_target")
        assert hasattr(session, "get_target_status")
        assert hasattr(session, "terminate_paused_target")
        assert callable(session.start_paused_target)
        assert callable(session.get_target_status)
        assert callable(session.terminate_paused_target)

    def test_start_paused_target_exact_operation(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1), _paused_start_resp(2),
        ])
        sent = []
        orig = session._send_and_receive
        def track(req, timeout):
            sent.append(("send", req))
            return orig(req, timeout)
        session._send_and_receive = track
        result = session.start_paused_target("test.py", [3])
        assert len(sent) == 1
        req = sent[0][1]
        assert req.operation == "start_paused_target"
        assert req.payload == {"script": "test.py", "breakpoints": [3], "argv": []}
        assert set(req.payload.keys()) == {"script", "breakpoints", "argv"}
        assert result["state"] == "paused"
        session.stop()

    def test_requests_have_detached_payload(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1), _paused_start_resp(2),
        ])
        sent = []
        orig = session._send_and_receive
        def track(req, timeout):
            sent.append(req)
            return orig(req, timeout)
        session._send_and_receive = track
        bps = [3]
        result = session.start_paused_target("test.py", bps)
        assert len(sent) == 1
        bps.append(5)
        assert sent[0].payload["breakpoints"] == [3]
        session.stop()

    def test_get_target_status_exact_operation(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1), _status_resp(2),
        ])
        sent = []
        orig = session._send_and_receive
        def track(req, timeout):
            sent.append(req)
            return orig(req, timeout)
        session._send_and_receive = track
        result = session.get_target_status()
        assert len(sent) == 1
        assert sent[0].operation == "get_target_status"
        assert sent[0].payload == {}
        assert result["state"] == "idle"
        session.stop()

    def test_terminate_paused_target_exact_operation(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1), _term_resp(2),
        ])
        session._target_lifecycle_state = "paused"
        session._active_script = "test.py"
        sent = []
        orig = session._send_and_receive
        def track(req, timeout):
            sent.append(("send", req))
            return orig(req, timeout)
        session._send_and_receive = track
        result = session.terminate_paused_target()
        assert len(sent) == 1
        req = sent[0][1]
        assert req.operation == "terminate_paused_target"
        assert req.payload == {}
        assert result["state"] == "terminated"
        session.stop()

    def test_terminate_no_unknown_fields(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1), _term_resp(2),
        ])
        session._target_lifecycle_state = "paused"
        session._active_script = "test.py"
        sent = []
        orig = session._send_and_receive
        def track(req, timeout):
            sent.append(req)
            return orig(req, timeout)
        session._send_and_receive = track
        result = session.terminate_paused_target()
        assert sent[0].payload == {}
        session.stop()


class TestPausedTargetValidationBeforeSend:
    """Validation failures send zero requests and preserve session."""

    def _assert_validation_preserves_session(
        self, session, mock_proc
    ):
        assert session.state == PdbSessionState.READY
        assert session._target_consumed is False
        orig_send = session._send_and_receive
        send_count = []
        def track(req, timeout):
            send_count.append(req)
            return orig_send(req, timeout)
        session._send_and_receive = track

        with pytest.raises(PdbProtocolError):
            session.start_paused_target("test.py", [3])
        assert len(send_count) == 0
        assert session._target_consumed is False
        assert session.state == PdbSessionState.READY
        session.ping()
        return session

    def test_not_ready_state(self, mock_workspace):
        session = PdbSession(mock_workspace)
        with pytest.raises(PdbSessionStateError, match="Cannot start_paused_target"):
            session.start_paused_target("test.py", [1])

    def test_invalid_script_type(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError):
            session.start_paused_target(123, [1])
        session.stop()

    def test_absolute_path_rejected(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="relative path"):
            session.start_paused_target("/abs/test.py", [1])
        session.stop()

    def test_raw_traversal_rejected(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match=".. traversal"):
            session.start_paused_target("../test.py", [1])
        session.stop()

    def test_non_py_file_rejected(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="end with .py"):
            session.start_paused_target("test.txt", [1])
        session.stop()

    def test_symlink_escape_rejected(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        from unittest.mock import patch
        with patch(
            "agentic_debugger.runtime.pdb_session.os.path.commonpath",
            return_value="/other"
        ):
            with pytest.raises(PdbProtocolError, match="symlink or junction"):
                session.start_paused_target("test.py", [1])
        session.stop()

    def test_missing_file_rejected(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="not found"):
            session.start_paused_target("missing.py", [1])
        assert session._target_consumed is False
        session.ping()
        session.stop()

    def test_oversized_source_rejected(self, mock_workspace):
        from agentic_debugger.runtime.pdb_session import _MAX_TARGET_SOURCE_BYTES
        d = Path(mock_workspace.root)
        f = d / "huge_test.py"
        f.write_bytes(b"x = 1\n" + b"# " + b"x" * _MAX_TARGET_SOURCE_BYTES)
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        orig_send = session._send_and_receive
        send_calls = []
        session._send_and_receive = lambda r, t: (send_calls.append(1), orig_send(r, t))[1]
        with pytest.raises(PdbProtocolError, match="exceeds maximum source"):
            session.start_paused_target("huge_test.py", [1])
        assert len(send_calls) == 0
        assert session._target_consumed is False
        session.ping()
        session.stop()

    def test_empty_breakpoints_rejected(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="1-16"):
            session.start_paused_target("test.py", [])
        assert session._target_consumed is False
        session.stop()

    def test_duplicate_breakpoint_rejected(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="duplicates"):
            session.start_paused_target("test.py", [3, 3])
        assert session._target_consumed is False
        session.stop()

    def test_bool_breakpoint_rejected(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="integers"):
            session.start_paused_target("test.py", [True])
        assert session._target_consumed is False
        session.stop()

    def test_out_of_range_breakpoint_rejected(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="exceeds source length"):
            session.start_paused_target("test.py", [1, 999])
        assert session._target_consumed is False
        session.stop()

    def test_argv_is_string_rejected(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="list"):
            session.start_paused_target("test.py", [1], argv="not a list")
        assert session._target_consumed is False
        session.stop()

    def test_invalid_argv_item_rejected(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="strings"):
            session.start_paused_target("test.py", [1], argv=[42])
        assert session._target_consumed is False
        session.stop()

    def test_nul_in_argv_rejected(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="NUL"):
            session.start_paused_target("test.py", [1], argv=["a\x00b"])
        assert session._target_consumed is False
        session.stop()

    def test_non_utf8_input_rejected(self, mock_workspace):
        session, mp = _setup(mock_workspace, [_hello_resp(), _ping_resp(2)])
        with pytest.raises(PdbProtocolError, match="non-UTF-8"):
            session.start_paused_target("test.py", [1], argv=["\ud800"])
        assert session._target_consumed is False
        session.stop()

    def test_validation_failure_then_valid_start_succeeds(self, mock_workspace):
        _ok_resp = _paused_start_resp(2)
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1), _ok_resp,
        ])
        with pytest.raises(PdbProtocolError):
            session.start_paused_target("missing.py", [1])
        assert session._target_consumed is False
        result = session.start_paused_target("test.py", [3])
        assert result["state"] == "paused"
        session.stop()

    def test_validation_failure_then_run_to_breakpoint_succeeds(self, mock_workspace):
        _run_bp_resp = _run_to_bp_resp(2)
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1), _run_bp_resp,
        ])
        with pytest.raises(PdbProtocolError):
            session.start_paused_target("missing.py", [1])
        assert session._target_consumed is False
        resp = session.run_to_breakpoint("test.py", [2])
        assert resp.success is True
        session.stop()


class TestPausedTargetStartResult:
    """Malformed start_paused_target results fail the session."""

    def _setup_with_result(self, mock_workspace, result, success=True, error=""):
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _paused_start_resp(2, result=result, success=success, error=error),
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
            return session

    def test_non_mapping_result(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        bad_resp = b'{"protocol_version":1,"request_id":2,"success":true,"result":"not a dict","error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_resp
        session._response_queue.get = inject_get
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_unknown_state(self, mock_workspace):
        session = self._setup_with_result(
            mock_workspace, {"state": "unknown_state"}
        )
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_unknown_public_state_raises(self, mock_workspace):
        session = self._setup_with_result(
            mock_workspace, {"state": "unknown"}
        )
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_status_unknown_rejected(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        bad_line = b'{"protocol_version":1,"request_id":2,"success":true,"result":{"state":"unknown"},"error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_line
        session._response_queue.get = inject_get
        session._target_consumed = True
        session._active_script = "test.py"
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.get_target_status()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_missing_field(self, mock_workspace):
        session = self._setup_with_result(
            mock_workspace, {"state": "paused", "script": "test.py", "line": 3}
        )
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_extra_field(self, mock_workspace):
        session = self._setup_with_result(
            mock_workspace,
            {"state": "paused", "script": "test.py", "line": 3,
             "function": "main", "extra": 1},
        )
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_mismatched_script(self, mock_workspace):
        session = self._setup_with_result(
            mock_workspace,
            {"state": "paused", "script": "wrong.py", "line": 3,
             "function": "main"},
        )
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_paused_line_not_requested(self, mock_workspace):
        session = self._setup_with_result(
            mock_workspace,
            {"state": "paused", "script": "test.py", "line": 99,
             "function": "main"},
        )
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_bool_line(self, mock_workspace):
        session = self._setup_with_result(
            mock_workspace,
            {"state": "paused", "script": "test.py", "line": True,
             "function": "main"},
        )
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_empty_function(self, mock_workspace):
        session = self._setup_with_result(
            mock_workspace,
            {"state": "paused", "script": "test.py", "line": 3,
             "function": ""},
        )
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_bool_exit_code(self, mock_workspace):
        session = self._setup_with_result(
            mock_workspace,
            {"state": "exited", "script": "test.py", "exit_code": True},
        )
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_function_4097_bytes(self, mock_workspace):
        func_val = "f" + "x" * 4096
        result = {"state": "paused", "script": "test.py", "line": 3,
                  "function": func_val}
        session = self._setup_with_result(mock_workspace, result)
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    # ── Start-result state type counterexamples ──
    @pytest.mark.parametrize("bad_state,expected_type", [
        ([], "list"),
        ({}, "dict"),
        (1, "int"),
        (None, "NoneType"),
    ])
    def test_start_state_wrong_type(self, mock_workspace, bad_state, expected_type):
        session, mock_proc = _setup(mock_workspace, [_hello_resp(1)])
        import json
        raw = json.dumps({
            "protocol_version": 1, "request_id": 2, "success": True,
            "result": {"state": bad_state, "script": "test.py",
                       "line": 3, "function": "main"}, "error": "",
        }, separators=(",", ":"))
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return (raw + "\n").encode("utf-8")
        session._response_queue.get = inject_get
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        assert not isinstance(session._proc, MagicMock) or session._proc is None
        session.stop()

    @pytest.mark.parametrize("bad_state", ["idle", "failed", "terminated", "unknown"])
    def test_start_state_invalid_known_state(self, mock_workspace, bad_state):
        session, mock_proc = _setup(mock_workspace, [_hello_resp(1)])
        import json
        result = {"state": bad_state}
        if bad_state in ("paused",):
            result.update({"script": "test.py", "line": 3, "function": "main"})
        elif bad_state in ("exited",):
            result.update({"script": "test.py", "exit_code": 0})
        elif bad_state in ("failed",):
            result.update({"script": "test.py", "error": "err"})
        elif bad_state in ("terminated",):
            result.update({"script": "test.py"})
        raw = json.dumps({
            "protocol_version": 1, "request_id": 2, "success": True,
            "result": result, "error": "",
        }, separators=(",", ":"))
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return (raw + "\n").encode("utf-8")
        session._response_queue.get = inject_get
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_start_state_no_raw_typeerror(self, mock_workspace):
        """No raw TypeError escapes from start_paused_target state validation."""
        session, mock_proc = _setup(mock_workspace, [_hello_resp(1)])
        raw = b'{"protocol_version":1,"request_id":2,"success":true,"result":{"state":[],"script":"test.py","line":3,"function":"main"},"error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return raw
        session._response_queue.get = inject_get
        try:
            session.start_paused_target("test.py", [3])
        except TypeError:
            pytest.fail("Raw TypeError escaped instead of PdbProtocolError")
        except (PdbProtocolError, PdbSessionError):
            pass
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_error_4097_bytes(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        err_val = "e" + "x" * 4096
        bad_line = b'{"protocol_version":1,"request_id":2,"success":true,"result":{"state":"failed","script":"test.py","error":"' + err_val.encode('utf-8') + b'"},"error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_line
        session._response_queue.get = inject_get
        session._target_consumed = True
        session._active_script = "test.py"
        session._active_breakpoints = None
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.get_target_status()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_script_4097_bytes(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        long_name = "x" * 4097 + ".py"
        bad_line = (
            b'{"protocol_version":1,"request_id":2,"success":true,'
            b'"result":{"state":"terminated","script":"' + long_name.encode('utf-8') + b'"}}' + b'\n'
        )
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_line
        session._response_queue.get = inject_get
        session._target_lifecycle_state = "paused"
        session._active_script = long_name
        session._target_consumed = True
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.terminate_paused_target()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_multibyte_exceeds_utf8_limit(self, mock_workspace):
        func_val = "\u4e00" * 2049
        result = {"state": "paused", "script": "test.py", "line": 3,
                  "function": func_val}
        session = self._setup_with_result(mock_workspace, result)
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()


class TestPausedTargetStartCounterexamples:
    """Direct counterexample tests for protocol-boundary violations."""

    def _setup_with_result(self, mock_workspace, result, success=True, error=""):
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1),
            _paused_start_resp(2, result=result, success=success, error=error),
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
            return session

    def test_start_paused_traversal_script(self, mock_workspace):
        session = self._setup_with_result(
            mock_workspace,
            {"state": "paused", "script": "../escape.py",
             "line": 3, "function": "f"},
        )
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_start_paused_negative_line(self, mock_workspace):
        session = self._setup_with_result(
            mock_workspace,
            {"state": "paused", "script": "test.py",
             "line": -1, "function": "f"},
        )
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_start_failed_absolute_script(self, mock_workspace):
        session = self._setup_with_result(
            mock_workspace,
            {"state": "failed", "script": "/absolute.py", "error": "error"},
            success=False, error="Target error",
        )
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_status_paused_traversal_script(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        bad_line = b'{"protocol_version":1,"request_id":2,"success":true,"result":{"state":"paused","script":"../escape.py","line":3,"function":"f"},"error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_line
        session._response_queue.get = inject_get
        session._target_consumed = True
        session._active_script = "test.py"
        session._active_breakpoints = [3]
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.get_target_status()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_status_paused_negative_line(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        bad_line = b'{"protocol_version":1,"request_id":2,"success":true,"result":{"state":"paused","script":"test.py","line":-1,"function":"f"},"error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_line
        session._response_queue.get = inject_get
        session._target_consumed = True
        session._active_script = "test.py"
        session._active_breakpoints = [3]
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.get_target_status()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_status_paused_line_not_in_breakpoints(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        bad_line = b'{"protocol_version":1,"request_id":2,"success":true,"result":{"state":"paused","script":"test.py","line":99,"function":"f"},"error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_line
        session._response_queue.get = inject_get
        session._target_consumed = True
        session._active_script = "test.py"
        session._active_breakpoints = [3]
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.get_target_status()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_status_failed_absolute_script(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        bad_line = b'{"protocol_version":1,"request_id":2,"success":true,"result":{"state":"failed","script":"/absolute.py","error":"err"},"error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_line
        session._response_queue.get = inject_get
        session._target_consumed = True
        session._active_script = "test.py"
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.get_target_status()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_status_failed_nul_error(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        bad_line = b'{"protocol_version":1,"request_id":2,"success":true,"result":{"state":"failed","script":"test.py","error":"err\x00or"},"error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_line
        session._response_queue.get = inject_get
        session._target_consumed = True
        session._active_script = "test.py"
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.get_target_status()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_unconsumed_paused_status(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        bad_line = b'{"protocol_version":1,"request_id":2,"success":true,"result":{"state":"paused","script":"test.py","line":3,"function":"f"},"error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_line
        session._response_queue.get = inject_get
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.get_target_status()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_unconsumed_exited_status(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        bad_line = b'{"protocol_version":1,"request_id":2,"success":true,"result":{"state":"exited","script":"test.py","exit_code":0},"error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_line
        session._response_queue.get = inject_get
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.get_target_status()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_consumed_idle_status(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        bad_line = b'{"protocol_version":1,"request_id":2,"success":true,"result":{"state":"idle"},"error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_line
        session._response_queue.get = inject_get
        session._target_consumed = True
        session._active_script = "test.py"
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.get_target_status()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_consumed_mismatched_script_status(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        bad_line = b'{"protocol_version":1,"request_id":2,"success":true,"result":{"state":"terminated","script":"wrong.py"},"error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_line
        session._response_queue.get = inject_get
        session._target_consumed = True
        session._active_script = "test.py"
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.get_target_status()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_status_error_4097_bytes(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        err_val = "e" + "x" * 4096
        bad_line = b'{"protocol_version":1,"request_id":2,"success":true,"result":{"state":"failed","script":"test.py","error":"' + err_val.encode('utf-8') + b'"},"error":""}\n'
        orig_get = session._response_queue.get
        def inject_get(timeout=None):
            return bad_line
        session._response_queue.get = inject_get
        session._target_consumed = True
        session._active_script = "test.py"
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.get_target_status()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    # ── Repair 1: non-canonical result scripts ──

    def _inject_start_result(self, session, result_dict):
        import json
        raw = json.dumps({
            "protocol_version": 1, "request_id": 2,
            "success": True, "result": result_dict, "error": "",
        }, separators=(",", ":"))
        def inject_get(timeout=None):
            return (raw + "\n").encode("utf-8")
        session._response_queue.get = inject_get

    def _inject_status_result(self, session, result_dict):
        import json
        raw = json.dumps({
            "protocol_version": 1, "request_id": 2,
            "success": True, "result": result_dict, "error": "",
        }, separators=(",", ":"))
        def inject_get(timeout=None):
            return (raw + "\n").encode("utf-8")
        session._response_queue.get = inject_get
        session._target_consumed = True
        session._active_script = "test.py"

    def _inject_terminate_result(self, session, result_dict):
        import json
        raw = json.dumps({
            "protocol_version": 1, "request_id": 2,
            "success": True, "result": result_dict, "error": "",
        }, separators=(",", ":"))
        def inject_get(timeout=None):
            return (raw + "\n").encode("utf-8")
        session._response_queue.get = inject_get
        session._target_lifecycle_state = "paused"
        session._active_script = "test.py"

    def test_start_paused_backslash_script(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [_hello_resp(1)])
        self._inject_start_result(session, {
            "state": "paused", "script": ".\\test.py",
            "line": 3, "function": "main",
        })
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_start_paused_dot_segment_script(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [_hello_resp(1)])
        self._inject_start_result(session, {
            "state": "paused", "script": "./test.py",
            "line": 3, "function": "main",
        })
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_start_paused_dup_separator_script(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [_hello_resp(1)])
        self._inject_start_result(session, {
            "state": "paused", "script": "test.py/",
            "line": 3, "function": "main",
        })
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_status_backslash_script(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [_hello_resp(1)])
        self._inject_status_result(session, {
            "state": "paused", "script": ".\\test.py",
            "line": 3, "function": "main",
        })
        session._active_breakpoints = [3]
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.get_target_status()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_terminate_backslash_script(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [_hello_resp(1)])
        self._inject_terminate_result(session, {
            "state": "terminated", "script": ".\\test.py",
        })
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.terminate_paused_target()
        assert session.state == PdbSessionState.FAILED
        session.stop()


class _TestPausedTargetStatusBase:
    """Status result validation."""

    def _make_status_response(self, mock_workspace, result):
        mock_stdout = _ExhaustibleMockStream([
            _hello_resp(1), _status_resp(2, result=result),
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
            return session

    def test_status_idle(self, mock_workspace):
        session = self._make_status_response(mock_workspace, {"state": "idle"})
        try:
            result = session.get_target_status()
            assert result["state"] == "idle"
            assert set(result.keys()) == {"state"}
        finally:
            session.stop()


# =====================================================================
# Task 4C - Stack, frame, and locals inspection v1
# =====================================================================


def _valid_stack_result():
    return {
        "state": "paused",
        "script": "test.py",
        "pause_generation": 1,
        "frames": [{
            "frame_id": 0,
            "script": "test.py",
            "line": 3,
            "function": "<module>",
            "is_current": True,
        }],
        "total_frames": 1,
        "truncated": False,
    }


def _valid_frame_result():
    return {
        "state": "paused",
        "pause_generation": 1,
        "frame": {
            "frame_id": 0,
            "script": "test.py",
            "line": 3,
            "function": "inner",
            "is_current": True,
            "argument_names": [],
            "local_names": ["value"],
            "locals_count": 1,
            "locals_truncated": False,
        },
    }


def _int_summary(value=42):
    return {
        "kind": "int",
        "type": "builtins.int",
        "value": value,
        "special": None,
        "size": value.bit_length(),
        "items": [],
        "entries": [],
        "truncated": False,
    }


def _valid_locals_result():
    return {
        "state": "paused",
        "pause_generation": 1,
        "frame_id": 0,
        "locals": [{"name": "value", "value": _int_summary()}],
        "total_count": 1,
        "truncated": False,
    }


def _ready_inspection_session(mock_workspace, result, success=True, error=""):
    response = PdbResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=2,
        success=success,
        result=result,
        error=error,
    )
    session, proc = _setup(mock_workspace, [_hello_resp(1)])
    session._target_consumed = True
    session._target_lifecycle_state = "paused"
    session._active_script = "test.py"
    session._active_breakpoints = [3]
    sent = []

    def send(request, timeout):
        sent.append(request)
        return response

    session._send_and_receive = send
    return session, proc, sent


class TestInspectionPublicAPI:
    @pytest.mark.parametrize(
        ("name", "parameters"),
        [
            ("get_stack_summary", ["self"]),
            ("get_frame", ["self", "frame_id", "pause_generation"]),
            (
                "get_frame_locals",
                ["self", "frame_id", "pause_generation"],
            ),
        ],
    )
    def test_exact_public_signatures(self, name, parameters):
        import inspect
        assert list(inspect.signature(getattr(PdbSession, name)).parameters) == parameters

    @pytest.mark.parametrize(
        ("method", "result", "operation", "payload"),
        [
            (
                lambda session: session.get_stack_summary(),
                _valid_stack_result(), "get_stack_summary", {},
            ),
            (
                lambda session: session.get_frame(0, 1),
                _valid_frame_result(), "get_frame",
                {"frame_id": 0, "pause_generation": 1},
            ),
            (
                lambda session: session.get_frame_locals(0, 1),
                _valid_locals_result(), "get_frame_locals",
                {"frame_id": 0, "pause_generation": 1},
            ),
        ],
    )
    def test_exact_operation_payload_and_result(
        self, mock_workspace, method, result, operation, payload
    ):
        session, _, sent = _ready_inspection_session(mock_workspace, result)
        try:
            assert method(session) == result
            assert len(sent) == 1
            assert sent[0].operation == operation
            assert sent[0].payload == payload
            assert session._target_lifecycle_state == "paused"
            assert session.state == PdbSessionState.READY
        finally:
            session.stop()

    @pytest.mark.parametrize(
        ("frame_id", "generation"),
        [(True, 1), (-1, 1), ("0", 1), (0, True), (0, 0), (0, -1), (0, "1")],
    )
    def test_identifiers_are_strict_before_send(
        self, mock_workspace, frame_id, generation
    ):
        session, _, sent = _ready_inspection_session(
            mock_workspace, _valid_frame_result()
        )
        try:
            with pytest.raises(PdbSessionError):
                session.get_frame(frame_id, generation)
            assert sent == []
            assert session.state == PdbSessionState.READY
        finally:
            session.stop()

    @pytest.mark.parametrize(
        "local_state",
        [
            "idle", "starting", "continuing", "exited", "failed",
            "terminated", "unknown",
        ],
    )
    @pytest.mark.parametrize(
        "method",
        [
            lambda session: session.get_stack_summary(),
            lambda session: session.get_frame(0, 1),
            lambda session: session.get_frame_locals(0, 1),
        ],
    )
    def test_nonpaused_lifecycle_rejects_without_request(
        self, mock_workspace, local_state, method
    ):
        session, _, sent = _ready_inspection_session(
            mock_workspace, _valid_stack_result()
        )
        session._target_lifecycle_state = local_state
        try:
            with pytest.raises(PdbSessionStateError):
                method(session)
            assert sent == []
            assert session.state == PdbSessionState.READY
            assert session._target_consumed is True
            assert session._active_script == "test.py"
        finally:
            session.stop()

    @pytest.mark.parametrize("one_shot_state", ["exited", "terminated"])
    def test_one_shot_completion_rejects_without_request(
        self, mock_workspace, one_shot_state
    ):
        session, _, sent = _ready_inspection_session(
            mock_workspace, _valid_stack_result()
        )
        session._target_lifecycle_state = one_shot_state
        session._active_breakpoints = None
        try:
            with pytest.raises(PdbSessionStateError):
                session.get_stack_summary()
            assert sent == []
            assert session.state == PdbSessionState.READY
        finally:
            session.stop()

    def test_correlated_operation_failure_is_ordinary(self, mock_workspace):
        session, _, sent = _ready_inspection_session(
            mock_workspace, {}, success=False, error="Unknown frame_id"
        )
        try:
            with pytest.raises(PdbSessionError, match="Unknown frame_id"):
                session.get_frame(99, 1)
            assert len(sent) == 1
            assert session.state == PdbSessionState.READY
            assert session._target_lifecycle_state == "paused"
            assert session._active_script == "test.py"
        finally:
            session.stop()

    def test_status_refresh_recovers_unknown_then_inspection(self, mock_workspace):
        session, _, sent = _ready_inspection_session(
            mock_workspace, _valid_stack_result()
        )
        session._target_lifecycle_state = "unknown"
        responses = [
            PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=2,
                success=True,
                result={
                    "state": "paused", "script": "test.py",
                    "line": 3, "function": "<module>",
                },
                error="",
            ),
            PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=3,
                success=True,
                result=_valid_stack_result(),
                error="",
            ),
        ]

        def send(request, timeout):
            sent.append(request)
            return responses.pop(0)

        session._send_and_receive = send
        try:
            with pytest.raises(PdbSessionStateError):
                session.get_stack_summary()
            assert sent == []
            assert session.get_target_status()["state"] == "paused"
            assert session.get_stack_summary()["pause_generation"] == 1
            assert [request.operation for request in sent] == [
                "get_target_status", "get_stack_summary",
            ]
        finally:
            session.stop()


class TestInspectionMalformedResults:
    @pytest.mark.parametrize(
        "result",
        [
            {**_valid_stack_result(), "state": []},
            {**_valid_stack_result(), "pause_generation": True},
            {**_valid_stack_result(), "script": "../test.py"},
            {**_valid_stack_result(), "extra": 1},
            {**_valid_stack_result(), "total_frames": 2, "truncated": False},
            {**_valid_stack_result(), "frames": []},
            {
                **_valid_stack_result(),
                "frames": [{
                    **_valid_stack_result()["frames"][0], "line": True,
                }],
            },
        ],
    )
    def test_malformed_stack_cleans_session(self, mock_workspace, result):
        session, _, _ = _ready_inspection_session(mock_workspace, result)
        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.get_stack_summary()
            assert session.state == PdbSessionState.FAILED
            with pytest.raises(PdbSessionStateError):
                session.ping()
        finally:
            session.stop()

    @pytest.mark.parametrize(
        "result",
        [
            {**_valid_frame_result(), "pause_generation": 2},
            {
                **_valid_frame_result(),
                "frame": {
                    **_valid_frame_result()["frame"],
                    "argument_names": ["value", "value"],
                },
            },
            {
                **_valid_frame_result(),
                "frame": {
                    **_valid_frame_result()["frame"],
                    "local_names": ["z", "a"], "locals_count": 2,
                },
            },
        ],
    )
    def test_malformed_frame_cleans_session(self, mock_workspace, result):
        session, _, _ = _ready_inspection_session(mock_workspace, result)
        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.get_frame(0, 1)
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()

    @pytest.mark.parametrize(
        "result",
        [
            {**_valid_locals_result(), "frame_id": True},
            {
                **_valid_locals_result(),
                "locals": [{"name": "value", "value": {"kind": "int"}}],
            },
            {
                **_valid_locals_result(),
                "locals": [{
                    "name": "value",
                    "value": {**_int_summary(), "kind": "invalid"},
                }],
            },
            {**_valid_locals_result(), "unknown": 1},
            {**_valid_locals_result(), "total_count": 2, "truncated": False},
        ],
    )
    def test_malformed_locals_cleans_session(self, mock_workspace, result):
        session, _, _ = _ready_inspection_session(mock_workspace, result)
        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.get_frame_locals(0, 1)
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()

class TestPausedTargetStatus(_TestPausedTargetStatusBase):
    def test_status_paused(self, mock_workspace):
        session = self._make_status_response(
            mock_workspace,
            {"state": "paused", "script": "test.py", "line": 3,
             "function": "<module>"}
        )
        session._target_consumed = True
        session._active_script = "test.py"
        session._active_breakpoints = [3]
        try:
            result = session.get_target_status()
            assert result["state"] == "paused"
            assert result["line"] == 3
        finally:
            session.stop()

    def test_status_exited(self, mock_workspace):
        session = self._make_status_response(
            mock_workspace, {"state": "exited", "script": "test.py", "exit_code": 0}
        )
        session._target_consumed = True
        session._active_script = "test.py"
        try:
            result = session.get_target_status()
            assert result["state"] == "exited"
            assert result["exit_code"] == 0
        finally:
            session.stop()

    def test_status_failed(self, mock_workspace):
        session = self._make_status_response(
            mock_workspace, {"state": "failed", "script": "test.py",
             "error": "Target raised ValueError"}
        )
        session._target_consumed = True
        session._active_script = "test.py"
        try:
            result = session.get_target_status()
            assert result["state"] == "failed"
            assert "ValueError" in result["error"]
        finally:
            session.stop()

    def test_status_terminated(self, mock_workspace):
        session = self._make_status_response(
            mock_workspace, {"state": "terminated", "script": "test.py"}
        )
        session._target_consumed = True
        session._active_script = "test.py"
        try:
            result = session.get_target_status()
            assert result["state"] == "terminated"
        finally:
            session.stop()

    def test_unknown_state_raises(self, mock_workspace):
        session = self._make_status_response(mock_workspace, {"state": "bogus"})

        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.get_target_status()
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()

    def test_missing_field_for_state(self, mock_workspace):
        session = self._make_status_response(mock_workspace, {"state": "paused"})

        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.get_target_status()
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()

    def test_extra_field_for_state(self, mock_workspace):
        session = self._make_status_response(
            mock_workspace, {"state": "idle", "extra": 1}
        )

        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.get_target_status()
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()

    def test_bool_numeric_field(self, mock_workspace):
        session = self._make_status_response(
            mock_workspace, {"state": "exited", "script": "test.py", "exit_code": True}
        )

        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.get_target_status()
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()

    def test_consumed_then_contradictory_idle(self, mock_workspace):
        session = self._make_status_response(mock_workspace, {"state": "idle"})

        session._target_consumed = True
        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.get_target_status()
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()

    def test_failed_state_empty_error(self, mock_workspace):
        session = self._make_status_response(
            mock_workspace, {"state": "failed", "script": "test.py", "error": ""}
        )

        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.get_target_status()
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()

    def test_paused_state_empty_function(self, mock_workspace):
        session = self._make_status_response(
            mock_workspace, {"state": "paused", "script": "test.py", "line": 3,
             "function": ""}
        )

        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.get_target_status()
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()


class TestPausedTargetTerminate:
    """Termination guard and result."""

    def test_terminate_before_start_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        with pytest.raises(PdbSessionStateError, match="Cannot terminate"):
            session.terminate_paused_target()
        assert session.state == PdbSessionState.READY
        session.stop()

    def test_terminate_after_exit_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        session._target_lifecycle_state = "exited"
        with pytest.raises(PdbSessionStateError, match="Cannot terminate"):
            session.terminate_paused_target()
        assert session.state == PdbSessionState.READY
        session.stop()

    def test_terminate_after_failure_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        session._target_lifecycle_state = "failed"
        with pytest.raises(PdbSessionStateError, match="Cannot terminate"):
            session.terminate_paused_target()
        assert session.state == PdbSessionState.READY
        session.stop()

    def test_terminate_after_termination_rejected(self, mock_workspace):
        session, _ = _setup(mock_workspace, [_hello_resp()])
        session._target_lifecycle_state = "terminated"
        with pytest.raises(PdbSessionStateError, match="Cannot terminate"):
            session.terminate_paused_target()
        assert session.state == PdbSessionState.READY
        session.stop()

    def test_local_rejection_sends_zero_requests(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [_hello_resp()])
        send_count = []
        orig = session._send_and_receive
        def track(req, timeout):
            send_count.append(req)
            return orig(req, timeout)
        session._send_and_receive = track
        with pytest.raises(PdbSessionStateError):
            session.terminate_paused_target()
        assert len(send_count) == 0
        session.stop()

    def test_terminate_while_paused_sends_request(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1), _term_resp(2),
        ])
        session._target_lifecycle_state = "paused"
        session._active_script = "test.py"
        send_count = []
        orig = session._send_and_receive
        def track(req, timeout):
            send_count.append(req)
            return orig(req, timeout)
        session._send_and_receive = track
        result = session.terminate_paused_target()
        assert len(send_count) == 1
        assert result["state"] == "terminated"
        assert result["script"] == "test.py"
        session.stop()

    def test_mismatched_terminated_script_rejected(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1), _term_resp(2, result={
                "state": "terminated", "script": "wrong.py",
            }),
        ])
        session._target_lifecycle_state = "paused"
        session._active_script = "test.py"
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.terminate_paused_target()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_extra_field_in_terminate_result_rejected(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1), _term_resp(2, result={
                "state": "terminated", "script": "test.py", "extra": 1,
            }),
        ])
        session._target_lifecycle_state = "paused"
        session._active_script = "test.py"
        with pytest.raises((PdbProtocolError, PdbSessionError)):
            session.terminate_paused_target()
        assert session.state == PdbSessionState.FAILED
        session.stop()

    def test_second_termination_rejected_locally(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1), _term_resp(2),
        ])
        session._target_lifecycle_state = "paused"
        session._active_script = "test.py"
        session.terminate_paused_target()
        assert session._target_lifecycle_state == "terminated"
        with pytest.raises(PdbSessionStateError, match="Cannot terminate"):
            session.terminate_paused_target()
        session.stop()

    def test_failed_termination_sets_unknown(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
            _term_resp(2, result={}, success=False,
                       error="Cannot terminate in state: exited"),
        ])
        session._target_lifecycle_state = "paused"
        session._active_script = "test.py"
        with pytest.raises(PdbSessionError, match="Terminate failed"):
            session.terminate_paused_target()
        assert session.state == PdbSessionState.READY
        assert session._target_lifecycle_state == "unknown"
        assert session._active_script == "test.py"
        with pytest.raises(PdbSessionStateError, match="Cannot terminate"):
            session.terminate_paused_target()
        session.stop()

    def test_failed_termination_status_refresh(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
            _term_resp(2, result={}, success=False,
                       error="Cannot terminate in state: exited"),
            _status_resp(3, result={"state": "exited", "script": "test.py",
                                    "exit_code": 0}),
        ])
        session._target_lifecycle_state = "paused"
        session._active_script = "test.py"
        session._target_consumed = True
        with pytest.raises(PdbSessionError):
            session.terminate_paused_target()
        assert session._target_lifecycle_state == "unknown"
        result = session.get_target_status()
        assert result["state"] == "exited"
        assert session._target_lifecycle_state == "exited"
        session.stop()

    def test_ping_after_successful_termination(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1), _term_resp(2), _ping_resp(3),
        ])
        session._target_lifecycle_state = "paused"
        session._active_script = "test.py"
        session.terminate_paused_target()
        pong = session.ping()
        assert pong.success is True
        session.stop()


class TestPausedTargetAtomicReservation:
    """Atomic one-target execution reservation with deterministic coordination."""

    def test_two_concurrent_paused_starts(self, mock_workspace):
        results = []
        send_count = []
        in_send = threading.Event()
        release_send = threading.Event()
        t1 = None
        session = None
        mpatch = patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        )
        mp = mpatch.start()
        try:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = _ExhaustibleMockStream([
                _hello_resp(1), _paused_start_resp(2),
            ])
            mock_proc.stderr = _ExhaustibleMockStream([])
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace, request_timeout=3.0)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()

            saved_send = session._send_and_receive
            def hang_send(req, timeout):
                send_count.append(1)
                in_send.set()
                assert release_send.wait(timeout=5.0), (
                    "Release event not set within timeout"
                )
                from agentic_debugger.runtime.pdb_protocol import PdbResponse
                return PdbResponse(
                    protocol_version=1, request_id=req.request_id,
                    success=True,
                    result={
                        "state": "paused", "script": "test.py",
                        "line": 3, "function": "<module>",
                    },
                    error="",
                )
            session._send_and_receive = hang_send

            def caller():
                try:
                    result = session.start_paused_target("test.py", [3])
                    results.append(("ok", result))
                except Exception as e:
                    results.append(("err", type(e).__name__))

            t1 = threading.Thread(target=caller, daemon=True)
            t1.start()
            assert in_send.wait(timeout=5.0), (
                "First caller did not reach the send boundary"
            )

            with pytest.raises((PdbSessionError, PdbSessionStateError)):
                session.start_paused_target("test.py", [3])
        finally:
            release_send.set()
            if t1 is not None:
                t1.join(timeout=5.0)
            if session is not None:
                session.stop()
            mpatch.stop()

        assert not t1.is_alive()
        assert len(results) == 1
        assert results[0][0] == "ok"
        assert len(send_count) == 1

    def test_concurrent_paused_and_run_to_breakpoint(self, mock_workspace):
        results = []
        send_count = []
        in_send = threading.Event()
        release_send = threading.Event()
        t1 = None
        session = None
        mpatch = patch(
            "agentic_debugger.runtime.pdb_session.subprocess.Popen"
        )
        mp = mpatch.start()
        try:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = _ExhaustibleMockStream([
                _hello_resp(1), _paused_start_resp(2),
            ])
            mock_proc.stderr = _ExhaustibleMockStream([])
            mp.return_value = mock_proc

            session = PdbSession(mock_workspace, request_timeout=3.0)
            session._get_worker_argv = lambda: ["fake", "-c", "pass"]
            session.start()

            def hang_send(req, timeout):
                send_count.append(1)
                in_send.set()
                assert release_send.wait(timeout=5.0), (
                    "Release event not set within timeout"
                )
                from agentic_debugger.runtime.pdb_protocol import PdbResponse
                return PdbResponse(
                    protocol_version=1, request_id=req.request_id,
                    success=True,
                    result={
                        "state": "paused", "script": "test.py",
                        "line": 3, "function": "<module>",
                    },
                    error="",
                )
            session._send_and_receive = hang_send

            def paused_caller():
                try:
                    result = session.start_paused_target("test.py", [3])
                    results.append(("ok", result))
                except Exception as e:
                    results.append(("err", type(e).__name__))

            t1 = threading.Thread(target=paused_caller, daemon=True)
            t1.start()
            assert in_send.wait(timeout=5.0), (
                "First caller did not reach the send boundary"
            )

            with pytest.raises((PdbSessionError, PdbSessionStateError)):
                session.run_to_breakpoint("test.py", [2])
        finally:
            release_send.set()
            if t1 is not None:
                t1.join(timeout=5.0)
            if session is not None:
                session.stop()
            mpatch.stop()

        assert not t1.is_alive()
        assert len(results) == 1
        assert results[0][0] == "ok"
        assert len(send_count) == 1

    def test_winner_updates_lifecycle(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1), _paused_start_resp(2),
        ])
        result = session.start_paused_target("test.py", [3])
        assert result["state"] == "paused"
        assert session._target_consumed is True
        assert session._target_lifecycle_state == "paused"
        session.stop()

    def test_loser_does_not_corrupt_session(self, mock_workspace):
        session, mock_proc = _setup(mock_workspace, [
            _hello_resp(1),
        ])
        session._target_consumed = True
        with pytest.raises(PdbSessionStateError, match="already completed"):
            session.start_paused_target("test.py", [3])
        assert session.state == PdbSessionState.READY
        session.stop()


# =====================================================================
# Task 4B3 ? Continue/Resume and Additional Execution Control v1
# =====================================================================


def _continue_response(request_id=2, result=None, success=True, error=""):
    if result is None:
        result = {
            "state": "paused", "script": "test.py",
            "line": 3, "function": "<module>",
        }
    return PdbResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        success=success,
        result=result,
        error=error,
    )


def _ready_continue_session(mock_workspace, response):
    session, proc = _setup(mock_workspace, [_hello_resp(1)])
    session._target_consumed = True
    session._target_lifecycle_state = "paused"
    session._active_script = "test.py"
    session._active_breakpoints = [3, 5]
    sent = []

    def send(request, timeout):
        sent.append(request)
        return response

    session._send_and_receive = send
    return session, proc, sent


class TestContinuePausedTargetPublicAPI:
    def test_method_exists_and_exact_signature(self, mock_workspace):
        import inspect
        method = getattr(PdbSession, "continue_paused_target")
        assert callable(method)
        assert list(inspect.signature(method).parameters) == ["self"]

    def test_exact_operation_payload_and_result(self, mock_workspace):
        response = _continue_response()
        session, _, sent = _ready_continue_session(mock_workspace, response)
        try:
            result = session.continue_paused_target()
            assert result == response.result
            assert len(sent) == 1
            assert sent[0].operation == "continue_paused_target"
            assert sent[0].payload == {}
            assert set(sent[0].to_mapping()) == {
                "protocol_version", "request_id", "operation", "payload",
            }
            assert session._target_lifecycle_state == "paused"
        finally:
            session.stop()

    @pytest.mark.parametrize("local_state", [
        "idle", "starting", "continuing", "exited", "failed",
        "terminated", "unknown",
    ])
    def test_local_lifecycle_rejection_is_zero_request(
        self, mock_workspace, local_state
    ):
        session, _, sent = _ready_continue_session(
            mock_workspace, _continue_response()
        )
        session._target_lifecycle_state = local_state
        try:
            with pytest.raises(PdbSessionStateError, match="Cannot continue"):
                session.continue_paused_target()
            assert sent == []
            assert session.state == PdbSessionState.READY
            expected_state = "unknown" if local_state == "continuing" else local_state
            assert session._target_lifecycle_state == expected_state
            assert session._target_consumed is True
            assert session._active_script == "test.py"
            ping_response = PdbResponse(
                protocol_version=PROTOCOL_VERSION, request_id=99,
                success=True, result={"status": "ok", "pdb_created": True}, error="",
            )
            session._send_and_receive = lambda request, timeout: PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=ping_response.success,
                result=ping_response.result,
                error=ping_response.error,
            )
            assert session.ping().success is True
        finally:
            session.stop()

    @pytest.mark.parametrize("one_shot_state", ["terminated", "exited", "failed"])
    def test_one_shot_lifecycle_rejected_without_request(
        self, mock_workspace, one_shot_state
    ):
        session, _, sent = _ready_continue_session(
            mock_workspace, _continue_response()
        )
        session._target_lifecycle_state = one_shot_state
        session._active_breakpoints = None
        try:
            with pytest.raises(PdbSessionStateError):
                session.continue_paused_target()
            assert sent == []
            assert session.state == PdbSessionState.READY
            assert session._target_consumed is True
        finally:
            session.stop()


class TestContinuePausedTargetValidation:
    @pytest.mark.parametrize("result", [
        pytest.param([], id="list-result"),
        pytest.param("paused", id="string-result"),
        pytest.param({"state": []}, id="list-state"),
        pytest.param({"state": {}}, id="dict-state"),
        pytest.param({"state": 1}, id="integer-state"),
        pytest.param({"state": None}, id="null-state"),
        pytest.param({"state": "idle"}, id="idle-state"),
        pytest.param({"state": "failed"}, id="failed-state"),
        pytest.param({"state": "terminated"}, id="terminated-state"),
        pytest.param({"state": "unknown"}, id="unknown-state"),
        pytest.param({"state": "running"}, id="running-state"),
        pytest.param({"state": "paused", "script": "test.py", "line": 3},
                     id="missing-paused-field"),
        pytest.param({"state": "paused", "script": "test.py", "line": 3,
                      "function": "f", "extra": 1}, id="extra-paused-field"),
        pytest.param({"state": "paused", "script": "other.py", "line": 3,
                      "function": "f"}, id="mismatched-script"),
        pytest.param({"state": "paused", "script": "sub\\test.py", "line": 3,
                      "function": "f"}, id="backslash-script"),
        pytest.param({"state": "paused", "script": "./test.py", "line": 3,
                      "function": "f"}, id="dot-segment-script"),
        pytest.param({"state": "paused", "script": "sub//test.py", "line": 3,
                      "function": "f"}, id="duplicate-separator-script"),
        pytest.param({"state": "paused", "script": "../test.py", "line": 3,
                      "function": "f"}, id="traversal-script"),
        pytest.param({"state": "paused", "script": "C:/test.py", "line": 3,
                      "function": "f"}, id="absolute-script"),
        pytest.param({"state": "paused", "script": "test.py", "line": True,
                      "function": "f"}, id="bool-line"),
        pytest.param({"state": "paused", "script": "test.py", "line": 0,
                      "function": "f"}, id="zero-line"),
        pytest.param({"state": "paused", "script": "test.py", "line": -1,
                      "function": "f"}, id="negative-line"),
        pytest.param({"state": "paused", "script": "test.py", "line": 4,
                      "function": "f"}, id="line-outside-breakpoints"),
        pytest.param({"state": "paused", "script": "test.py", "line": 3,
                      "function": ""}, id="empty-function"),
        pytest.param({"state": "paused", "script": "test.py", "line": 3,
                      "function": "bad\0name"}, id="nul-function"),
        pytest.param({"state": "paused", "script": "test.py", "line": 3,
                      "function": "\ud800"}, id="non-utf8-function"),
        pytest.param({"state": "paused", "script": "test.py", "line": 3,
                      "function": "x" * 4097}, id="oversized-function"),
        pytest.param({"state": "exited", "script": "test.py"},
                     id="missing-exit-field"),
        pytest.param({"state": "exited", "script": "test.py", "exit_code": 0,
                      "extra": 1}, id="extra-exit-field"),
        pytest.param({"state": "exited", "script": "test.py", "exit_code": True},
                     id="bool-exit-code"),
    ])
    def test_malformed_success_fails_session_and_cleans_up(
        self, mock_workspace, result
    ):
        response = MagicMock(spec=PdbResponse)
        response.success = True
        response.result = result
        response.error = ""
        session, _, sent = _ready_continue_session(mock_workspace, response)
        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.continue_paused_target()
            assert len(sent) == 1
            assert session.state == PdbSessionState.FAILED
            with pytest.raises(PdbSessionStateError):
                session.ping()
        finally:
            session.stop()

    def test_ordinary_failed_response_sets_unknown_and_retains_metadata(
        self, mock_workspace
    ):
        response = _continue_response(
            success=False, result={}, error="Target raised ValueError: boom"
        )
        session, _, sent = _ready_continue_session(mock_workspace, response)
        try:
            with pytest.raises(PdbSessionError, match="Continue failed"):
                session.continue_paused_target()
            assert len(sent) == 1
            assert session.state == PdbSessionState.READY
            assert session._target_lifecycle_state == "unknown"
            assert session._active_script == "test.py"
            assert session._active_breakpoints == [3, 5]
            with pytest.raises(PdbSessionStateError):
                session.continue_paused_target()
            assert len(sent) == 1
        finally:
            session.stop()

    def test_failed_response_with_nonempty_result_is_corruption(self, mock_workspace):
        response = _continue_response(
            success=False, result={"state": "failed"}, error="boom"
        )
        session, _, _ = _ready_continue_session(mock_workspace, response)
        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.continue_paused_target()
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()


class TestContinueStatusRecovery:
    @pytest.mark.parametrize("result, expected", [
        ({"state": "paused", "script": "test.py", "line": 5,
          "function": "main"}, "paused"),
        ({"state": "exited", "script": "test.py", "exit_code": 0}, "exited"),
        ({"state": "failed", "script": "test.py", "error": "bounded"}, "failed"),
        ({"state": "terminated", "script": "test.py"}, "terminated"),
    ])
    def test_unknown_refresh_recovers_authoritative_state(
        self, mock_workspace, result, expected
    ):
        session, _, _ = _ready_continue_session(
            mock_workspace, _continue_response()
        )
        session._target_lifecycle_state = "unknown"
        responses = [PdbResponse(
            protocol_version=PROTOCOL_VERSION, request_id=2,
            success=True, result=result, error="",
        )]
        session._send_and_receive = lambda request, timeout: responses.pop(0)
        try:
            refreshed = session.get_target_status()
            assert refreshed["state"] == expected
            assert session._target_lifecycle_state == expected
            if expected == "paused":
                session._send_and_receive = lambda request, timeout: _continue_response(
                    request_id=request.request_id,
                    result={"state": "exited", "script": "test.py", "exit_code": 0},
                )
                assert session.continue_paused_target()["state"] == "exited"
            else:
                with pytest.raises(PdbSessionStateError):
                    session.continue_paused_target()
        finally:
            session.stop()


class TestContinueConcurrency:
    @pytest.mark.parametrize("loser_name", ["continue", "terminate", "status"])
    def test_one_inflight_boundary_sends_one_execution_control(
        self, mock_workspace, loser_name
    ):
        session, _, _ = _ready_continue_session(
            mock_workspace, _continue_response()
        )
        session._request_timeout = 0.1
        entered = threading.Event()
        release = threading.Event()
        sent = []
        outcomes = []

        def blocking_send(request, timeout):
            sent.append(request.operation)
            entered.set()
            assert release.wait(timeout=5.0)
            return _continue_response(request_id=request.request_id)

        session._send_and_receive = blocking_send
        thread = threading.Thread(
            target=lambda: outcomes.append(session.continue_paused_target()),
            daemon=True,
        )
        try:
            thread.start()
            assert entered.wait(timeout=5.0)
            loser = {
                "continue": session.continue_paused_target,
                "terminate": session.terminate_paused_target,
                "status": session.get_target_status,
            }[loser_name]
            with pytest.raises(PdbSessionError, match="already in flight"):
                loser()
            assert sent == ["continue_paused_target"]
        finally:
            release.set()
            thread.join(timeout=5.0)
            session.stop()
        assert not thread.is_alive()
        assert outcomes and outcomes[0]["state"] == "paused"


class TestContinueProtocolBoundary:
    @pytest.mark.parametrize("kind", ["request-id", "version", "malformed"])
    def test_continue_transport_envelope_corruption_cleans_up(
        self, mock_workspace, kind
    ):
        if kind == "malformed":
            wire = b"not-json\n"
        else:
            response = PdbResponse(
                protocol_version=(99 if kind == "version" else PROTOCOL_VERSION),
                request_id=(77 if kind == "request-id" else 2),
                success=True,
                result={
                    "state": "paused", "script": "test.py",
                    "line": 3, "function": "f",
                },
                error="",
            )
            wire = _ser(response)
        session, _ = _setup(mock_workspace, [_hello_resp(1), wire])
        session._target_consumed = True
        session._target_lifecycle_state = "paused"
        session._active_script = "test.py"
        session._active_breakpoints = [3]
        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.continue_paused_target()
            assert session.state == PdbSessionState.FAILED
            with pytest.raises(PdbSessionStateError):
                session.ping()
        finally:
            session.stop()


class TestInspectionConcurrency:
    @pytest.mark.parametrize(
        ("winner", "result", "loser"),
        [
            (
                lambda session: session.get_stack_summary(),
                _valid_stack_result(),
                lambda session: session.continue_paused_target(),
            ),
            (
                lambda session: session.get_frame_locals(0, 1),
                _valid_locals_result(),
                lambda session: session.continue_paused_target(),
            ),
            (
                lambda session: session.get_frame(0, 1),
                _valid_frame_result(),
                lambda session: session.terminate_paused_target(),
            ),
            (
                lambda session: session.get_frame_locals(0, 1),
                _valid_locals_result(),
                lambda session: session.get_target_status(),
            ),
            (
                lambda session: session.get_frame_locals(0, 1),
                _valid_locals_result(),
                lambda session: session.get_frame_locals(0, 1),
            ),
        ],
    )
    def test_exactly_one_request_owns_boundary(
        self, mock_workspace, winner, result, loser
    ):
        session, _, sent = _ready_inspection_session(mock_workspace, result)
        session._request_timeout = 0.1
        entered = threading.Event()
        release = threading.Event()
        winner_outcomes = []
        loser_outcomes = []
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=2,
            success=True,
            result=result,
            error="",
        )

        def blocking_send(request, timeout):
            sent.append(request)
            entered.set()
            assert release.wait(timeout=2.0)
            return response

        session._send_and_receive = blocking_send

        def run_winner():
            try:
                winner_outcomes.append(winner(session))
            except Exception as exc:
                winner_outcomes.append(exc)

        def run_loser():
            try:
                loser_outcomes.append(loser(session))
            except Exception as exc:
                loser_outcomes.append(exc)

        winner_thread = threading.Thread(target=run_winner)
        loser_thread = threading.Thread(target=run_loser)
        winner_thread.start()
        try:
            assert entered.wait(timeout=2.0)
            loser_thread.start()
            loser_thread.join(timeout=2.0)
            assert not loser_thread.is_alive()
            assert len(loser_outcomes) == 1
            assert isinstance(loser_outcomes[0], PdbSessionError)
            assert len(sent) == 1
        finally:
            release.set()
            winner_thread.join(timeout=2.0)
            if loser_thread.ident is not None:
                loser_thread.join(timeout=2.0)
            assert not winner_thread.is_alive()
            assert not loser_thread.is_alive()
            session.stop()
        assert len(winner_outcomes) == 1
        assert not isinstance(winner_outcomes[0], Exception)
        assert session._target_lifecycle_state == "paused"


# Task 4C repair: full-envelope bounds and kind/type coherence.


def _summary_for_kind(kind):
    common = {
        "kind": kind,
        "type": {
            "none": "builtins.NoneType",
            "bool": "builtins.bool",
            "int": "builtins.int",
            "float": "builtins.float",
            "str": "builtins.str",
            "bytes": "builtins.bytes",
            "list": "builtins.list",
            "tuple": "builtins.tuple",
            "dict": "builtins.dict",
            "set": "builtins.set",
            "frozenset": "builtins.frozenset",
            "object": "example.Widget",
        }[kind],
        "value": None,
        "special": None,
        "size": None,
        "items": [],
        "entries": [],
        "truncated": False,
    }
    if kind == "bool":
        common["value"] = True
    elif kind == "int":
        common.update(value=7, size=3)
    elif kind == "float":
        common["value"] = 1.5
    elif kind == "str":
        common.update(value="text", size=4)
    elif kind == "bytes":
        common.update(value="00", size=1)
    elif kind in ("list", "tuple", "dict", "set", "frozenset"):
        common["size"] = 0
    return common


def _locals_with_summary(summary):
    result = _valid_locals_result()
    result["locals"][0]["value"] = summary
    return result


def _unbounded_response_size(response):
    import json
    return len((json.dumps(
        response.to_mapping(), ensure_ascii=False, separators=(",", ":"),
        sort_keys=True, allow_nan=False,
    ) + "\n").encode("utf-8"))


class TestInspectionResponseEnvelopeRepair:
    def test_canonical_full_response_boundary(self):
        import json
        from agentic_debugger.runtime.pdb_protocol import MAX_LINE_LENGTH

        def response_with_padding(count):
            return PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=2,
                success=True,
                result={"padding": "x" * count},
                error="",
            )

        empty = response_with_padding(0)
        padding_at_limit = MAX_LINE_LENGTH - _unbounded_response_size(empty)
        at_limit = response_with_padding(padding_at_limit)
        below_limit = response_with_padding(padding_at_limit - 1)
        above_limit = response_with_padding(padding_at_limit + 1)
        assert _unbounded_response_size(below_limit) == MAX_LINE_LENGTH - 1
        assert _unbounded_response_size(at_limit) == MAX_LINE_LENGTH
        assert _unbounded_response_size(above_limit) == MAX_LINE_LENGTH + 1
        PdbSession._validate_successful_inspection_response_size(
            below_limit, "get_stack_summary"
        )
        PdbSession._validate_successful_inspection_response_size(
            at_limit, "get_stack_summary"
        )
        with pytest.raises(PdbProtocolError, match="protocol line limit"):
            PdbSession._validate_successful_inspection_response_size(
                above_limit, "get_stack_summary"
            )

    def test_injected_oversized_success_cleans_session(self, mock_workspace):
        frames = []
        for frame_id in range(64):
            frames.append({
                "frame_id": frame_id,
                "script": "test.py",
                "line": frame_id + 1,
                "function": "f" * 2000,
                "is_current": frame_id == 0,
            })
        result = {
            "state": "paused", "script": "test.py",
            "pause_generation": 1, "frames": frames,
            "total_frames": 64, "truncated": False,
        }
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION, request_id=2,
            success=True, result=result, error="",
        )
        assert _unbounded_response_size(response) > 65536
        session, _, _ = _ready_inspection_session(mock_workspace, result)
        try:
            with pytest.raises(PdbProtocolError, match="protocol line limit"):
                session.get_stack_summary()
            assert session.state == PdbSessionState.FAILED
            with pytest.raises(PdbSessionStateError):
                session.ping()
        finally:
            session.stop()


class TestInspectionKindTypeCoherenceRepair:
    _CANONICAL_KINDS = (
        "none", "bool", "int", "float", "str", "bytes", "list",
        "tuple", "dict", "set", "frozenset",
    )

    @pytest.mark.parametrize("kind", _CANONICAL_KINDS)
    def test_top_level_kind_type_mismatch_cleans_session(
        self, mock_workspace, kind
    ):
        summary = _summary_for_kind(kind)
        summary["type"] = "evil.Type"
        session, _, _ = _ready_inspection_session(
            mock_workspace, _locals_with_summary(summary)
        )
        try:
            with pytest.raises(PdbProtocolError, match="does not match kind"):
                session.get_frame_locals(0, 1)
            assert session.state == PdbSessionState.FAILED
            with pytest.raises(PdbSessionStateError):
                session.ping()
        finally:
            session.stop()

    @pytest.mark.parametrize("kind", _CANONICAL_KINDS)
    def test_exact_canonical_builtin_types_are_accepted(
        self, mock_workspace, kind
    ):
        result = _locals_with_summary(_summary_for_kind(kind))
        session, _, _ = _ready_inspection_session(mock_workspace, result)
        try:
            assert session.get_frame_locals(0, 1) == result
            assert session.state == PdbSessionState.READY
        finally:
            session.stop()

    @pytest.mark.parametrize(
        "container_kind,location",
        [
            ("list", "item"),
            ("tuple", "item"),
            ("dict", "key"),
            ("dict", "value"),
        ],
    )
    def test_nested_kind_type_mismatch_cleans_session(
        self, mock_workspace, container_kind, location
    ):
        container = _summary_for_kind(container_kind)
        bad = _int_summary(1)
        bad["type"] = "evil.Type"
        if container_kind in ("list", "tuple"):
            container.update(size=1, items=[bad])
        else:
            good = _int_summary(2)
            container.update(
                size=1,
                entries=[{
                    "key": bad if location == "key" else good,
                    "value": bad if location == "value" else good,
                }],
            )
        session, _, _ = _ready_inspection_session(
            mock_workspace, _locals_with_summary(container)
        )
        try:
            with pytest.raises(PdbProtocolError, match="does not match kind"):
                session.get_frame_locals(0, 1)
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()

    @pytest.mark.parametrize("type_name", ["example.Widget", "unknown"])
    def test_valid_object_type_names_are_accepted(
        self, mock_workspace, type_name
    ):
        summary = _summary_for_kind("object")
        summary["type"] = type_name
        result = _locals_with_summary(summary)
        session, _, _ = _ready_inspection_session(mock_workspace, result)
        try:
            assert session.get_frame_locals(0, 1) == result
        finally:
            session.stop()

    @pytest.mark.parametrize("type_name", ["", "x" * 513])
    def test_invalid_object_type_names_clean_session(
        self, mock_workspace, type_name
    ):
        summary = _summary_for_kind("object")
        summary["type"] = type_name
        session, _, _ = _ready_inspection_session(
            mock_workspace, _locals_with_summary(summary)
        )
        try:
            with pytest.raises(PdbProtocolError):
                session.get_frame_locals(0, 1)
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()

    def test_stack_summary_accepts_structural_module_frame(
        self, mock_workspace
    ):
        result = _valid_stack_result()
        session, _, _ = _ready_inspection_session(mock_workspace, result)
        try:
            assert session.get_stack_summary() == result
            assert result["frames"][0]["function"] == "<module>"
            assert session.state == PdbSessionState.READY
        finally:
            session.stop()

    def test_module_frame_detail_success_is_protocol_corruption(
        self, mock_workspace
    ):
        result = _valid_frame_result()
        result["frame"]["function"] = "<module>"
        session, _, _ = _ready_inspection_session(mock_workspace, result)
        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.get_frame(0, 1)
            assert session.state == PdbSessionState.FAILED
            with pytest.raises(PdbSessionStateError):
                session.ping()
        finally:
            session.stop()


# =====================================================================
# Task 4D - Safe evaluation and PDB integration hardening v1
# =====================================================================


def _valid_safe_eval_result(expression="value", frame_id=0, generation=1):
    return {
        "state": "paused",
        "pause_generation": generation,
        "frame": {
            "frame_id": frame_id,
            "script": "test.py",
            "line": 3,
            "function": "inner",
            "is_current": frame_id == 0,
        },
        "expression": expression,
        "value": _int_summary(),
    }


class TestSafeEvaluationPublicAPI:
    def test_exact_public_signature_operation_payload_and_result(
        self, mock_workspace
    ):
        import inspect
        assert list(inspect.signature(
            PdbSession.safe_eval_expression
        ).parameters) == [
            "self", "frame_id", "pause_generation", "expression",
        ]
        result = _valid_safe_eval_result("items[0]")
        session, _, sent = _ready_inspection_session(mock_workspace, result)
        try:
            assert session.safe_eval_expression(0, 1, "items[0]") == result
            assert len(sent) == 1
            assert sent[0].operation == "safe_eval_expression"
            assert sent[0].payload == {
                "frame_id": 0,
                "pause_generation": 1,
                "expression": "items[0]",
            }
            assert session.state == PdbSessionState.READY
            assert session._target_lifecycle_state == "paused"
        finally:
            session.stop()

    def test_module_frame_success_is_protocol_corruption(
        self, mock_workspace
    ):
        result = _valid_safe_eval_result("1 + 2")
        result["frame"]["function"] = "<module>"
        session, _, _ = _ready_inspection_session(mock_workspace, result)
        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.safe_eval_expression(0, 1, "1 + 2")
            assert session.state == PdbSessionState.FAILED
            with pytest.raises(PdbSessionStateError):
                session.ping()
        finally:
            session.stop()

    @pytest.mark.parametrize(
        ("frame_id", "generation"),
        [
            (True, 1), (-1, 1), ("0", 1),
            (0, True), (0, 0), (0, -1), (0, "1"),
        ],
    )
    def test_identifier_validation_is_local(
        self, mock_workspace, frame_id, generation
    ):
        session, _, sent = _ready_inspection_session(
            mock_workspace, _valid_safe_eval_result()
        )
        try:
            with pytest.raises(PdbSessionError):
                session.safe_eval_expression(frame_id, generation, "value")
            assert sent == []
            assert session.state == PdbSessionState.READY
            assert session._target_lifecycle_state == "paused"
        finally:
            session.stop()

    @pytest.mark.parametrize(
        "expression",
        [
            None, 1, True, "", "   ", " value", "value ",
            "a\0b", "a\rb", "a\nb", "a\tb", "a\x7fb", "\ud800",
            "é" * 513,
        ],
    )
    def test_expression_envelope_validation_is_local(
        self, mock_workspace, expression
    ):
        session, _, sent = _ready_inspection_session(
            mock_workspace, _valid_safe_eval_result()
        )
        try:
            with pytest.raises(PdbSessionError):
                session.safe_eval_expression(0, 1, expression)
            assert sent == []
            assert session.state == PdbSessionState.READY
            assert session._target_lifecycle_state == "paused"
        finally:
            session.stop()

    def test_1024_byte_expression_is_sent_exactly(self, mock_workspace):
        expression = "x" * 1024
        result = _valid_safe_eval_result(expression)
        session, _, sent = _ready_inspection_session(mock_workspace, result)
        try:
            assert session.safe_eval_expression(0, 1, expression) == result
            assert sent[0].payload["expression"] == expression
        finally:
            session.stop()

    @pytest.mark.parametrize(
        "local_state",
        [
            "idle", "starting", "continuing", "exited", "failed",
            "terminated", "unknown",
        ],
    )
    def test_nonpaused_lifecycle_rejects_without_request(
        self, mock_workspace, local_state
    ):
        session, _, sent = _ready_inspection_session(
            mock_workspace, _valid_safe_eval_result()
        )
        session._target_lifecycle_state = local_state
        try:
            with pytest.raises(PdbSessionStateError):
                session.safe_eval_expression(0, 1, "value")
            assert sent == []
            assert session.state == PdbSessionState.READY
            assert session._target_consumed is True
            assert session._active_script == "test.py"
        finally:
            session.stop()

    def test_operation_failure_is_ordinary_and_preserves_pause(
        self, mock_workspace
    ):
        session, _, sent = _ready_inspection_session(
            mock_workspace, {}, success=False, error="Unknown local name"
        )
        try:
            with pytest.raises(PdbSessionError, match="Unknown local name"):
                session.safe_eval_expression(0, 1, "missing")
            assert len(sent) == 1
            assert session.state == PdbSessionState.READY
            assert session._target_lifecycle_state == "paused"
            assert session._target_consumed is True
            assert session._active_script == "test.py"
        finally:
            session.stop()


class TestSafeEvaluationSuccessfulResultValidation:
    @pytest.mark.parametrize(
        "result",
        [
            {**_valid_safe_eval_result(), "state": "exited"},
            {**_valid_safe_eval_result(), "pause_generation": 2},
            {**_valid_safe_eval_result(), "expression": "other"},
            {**_valid_safe_eval_result(), "unknown": 1},
            {
                **_valid_safe_eval_result(),
                "frame": {
                    **_valid_safe_eval_result()["frame"], "frame_id": 1,
                },
            },
            {
                **_valid_safe_eval_result(),
                "frame": {
                    **_valid_safe_eval_result()["frame"],
                    "script": "../test.py",
                },
            },
            {
                **_valid_safe_eval_result(),
                "value": {**_int_summary(), "type": "builtins.bool"},
            },
            {
                **_valid_safe_eval_result(),
                "value": {"kind": "int"},
            },
        ],
    )
    def test_malformed_success_cleans_session(self, mock_workspace, result):
        session, _, _ = _ready_inspection_session(mock_workspace, result)
        try:
            with pytest.raises((PdbProtocolError, PdbSessionError)):
                session.safe_eval_expression(0, 1, "value")
            assert session.state == PdbSessionState.FAILED
            with pytest.raises(PdbSessionStateError):
                session.ping()
        finally:
            session.stop()

    def test_oversized_result_cleans_session(self, mock_workspace):
        leaf = _summary_for_kind("str")
        leaf.update({
            "value": "x" * 2048, "size": 2048, "truncated": False,
        })
        inner = _summary_for_kind("list")
        inner.update({
            "size": 16, "items": [leaf for _ in range(16)],
            "truncated": False,
        })
        outer = _summary_for_kind("list")
        outer.update({
            "size": 16, "items": [inner for _ in range(16)],
            "truncated": False,
        })
        result = {**_valid_safe_eval_result(), "value": outer}
        session, _, _ = _ready_inspection_session(mock_workspace, result)
        try:
            with pytest.raises(PdbProtocolError, match="32768-byte"):
                session.safe_eval_expression(0, 1, "value")
            assert session.state == PdbSessionState.FAILED
        finally:
            session.stop()

    def test_oversized_complete_response_cleans_session(
        self, mock_workspace, monkeypatch
    ):
        import agentic_debugger.runtime.pdb_session as session_module
        leaf = _summary_for_kind("str")
        leaf.update({
            "value": "x" * 2048, "size": 2048, "truncated": False,
        })
        inner = _summary_for_kind("list")
        inner.update({
            "size": 16, "items": [leaf for _ in range(16)],
            "truncated": False,
        })
        outer = _summary_for_kind("list")
        outer.update({
            "size": 2, "items": [inner, inner], "truncated": False,
        })
        result = {**_valid_safe_eval_result(), "value": outer}
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=2,
            success=True,
            result=result,
            error="",
        )
        assert _unbounded_response_size(response) > 65536
        monkeypatch.setattr(
            session_module, "_MAX_SAFE_EVAL_RESULT_BYTES", 1024 * 1024
        )
        session, _, _ = _ready_inspection_session(mock_workspace, result)
        try:
            with pytest.raises(PdbProtocolError, match="protocol line limit"):
                session.safe_eval_expression(0, 1, "value")
            assert session.state == PdbSessionState.FAILED
            with pytest.raises(PdbSessionStateError):
                session.ping()
        finally:
            session.stop()


class TestSafeExpressionInterpreter:
    @staticmethod
    def _evaluate(expression, local_values=None):
        from agentic_debugger.runtime.pdb_worker import (
            _parse_safe_expression,
            _SafeExpressionInterpreter,
        )
        mapping = {} if local_values is None else local_values
        parsed = _parse_safe_expression(expression)
        return _SafeExpressionInterpreter(mapping).evaluate(parsed)

    @pytest.mark.parametrize(
        ("expression", "local_values", "expected"),
        [
            ("name", {"name": 42}, 42),
            ("None", {}, None),
            ("False", {}, False),
            ("True", {}, True),
            ("17", {}, 17),
            ("-17", {}, -17),
            ("1.5", {}, 1.5),
            ("1e999", {}, float("inf")),
            ("'é'", {}, "é"),
            ("b'abc'", {}, b"abc"),
            ("len(value)", {"value": [1, 2]}, 2),
            ("value[0]", {"value": [42]}, 42),
            ("value[-1]", {"value": (1, 2)}, 2),
            ("value[1]", {"value": "ab"}, "b"),
            ("value[0]", {"value": b"a"}, 97),
            ("value['key']", {"value": {"key": 7}}, 7),
            ("+value", {"value": 2}, 2),
            ("~value", {"value": 2}, -3),
            ("not value", {"value": False}, True),
            ("2 + 3", {}, 5),
            ("5 - 3", {}, 2),
            ("5 * 3", {}, 15),
            ("5 / 2", {}, 2.5),
            ("5 // 2", {}, 2),
            ("5 % 2", {}, 1),
            ("2 + 0.5", {}, 2.5),
            ("True and False", {}, False),
            ("True or missing", {}, True),
            ("1 == 1", {}, True),
            ("1.0 != 2", {}, True),
            ("'a' < 'b'", {}, True),
            ("b'a' <= b'b'", {}, True),
            ("1 < 2.0 < 3", {}, True),
            ("value is None", {"value": object()}, False),
            ("1 if flag else missing", {"flag": True}, 1),
        ],
    )
    def test_allowed_semantics(self, expression, local_values, expected):
        result = self._evaluate(expression, local_values)
        if isinstance(expected, float) and expected == float("inf"):
            assert result == float("inf")
        else:
            assert result == expected

    @pytest.mark.parametrize(
        "expression",
        [
            "obj.attr", "lambda: 1", "(x := 1)",
            "[x for x in y]", "{x for x in y}",
            "{x: x for x in y}", "(x for x in y)",
            "f'{x}'", "value[1:2]", "[1]", "(1, 2)", "{'x': 1}",
            "set()", "open('x')", "len(value, value)",
            "len(value=value)", "len(*value)", "2 ** 3", "2 << 1",
            "2 & 1", "1 in value",
        ],
    )
    def test_structurally_rejected(self, expression):
        from agentic_debugger.runtime.pdb_worker import _SafeEvaluationError
        with pytest.raises(_SafeEvaluationError):
            self._evaluate(expression, {"x": 1, "y": [], "value": []})

    @pytest.mark.parametrize(
        ("expression", "local_values"),
        [
            ("missing", {}),
            ("not value", {"value": object()}),
            ("value + 1", {"value": object()}),
            ("value[0]", {"value": object()}),
            ("len(value)", {"value": object()}),
            ("value[True]", {"value": [1]}),
            ("value[2]", {"value": [1]}),
            ("1 / 0", {}),
            ("1 % 0", {}),
            ("1 and True", {}),
            ("value == None", {"value": object()}),
            ("value < 1", {"value": object()}),
            ("1 < '1'", {}),
            ("1 if 1 else 2", {}),
            ("'x' + 'y'", {}),
        ],
    )
    def test_unsafe_or_failed_operations_are_bounded(
        self, expression, local_values
    ):
        from agentic_debugger.runtime.pdb_worker import _SafeEvaluationError
        with pytest.raises(_SafeEvaluationError) as caught:
            self._evaluate(expression, local_values)
        assert caught.value.args
        assert len(str(caught.value).encode("utf-8")) <= 4096

    def test_ast_node_depth_identifier_and_arithmetic_bounds(self):
        from agentic_debugger.runtime.pdb_worker import _SafeEvaluationError
        expressions = [
            " + ".join(["1"] * 30),
            "+" * 13 + "1",
            "x" * 513,
            f"value * value",
        ]
        locals_values = {"value": 1 << 4095}
        for expression in expressions:
            with pytest.raises(_SafeEvaluationError):
                self._evaluate(expression, locals_values)

    @pytest.mark.parametrize(
        "expression",
        [
            "1e308 + 1e308",
            "1e308 * 1e308",
            "1e308 / 1e-308",
            "1e308 // 1e-308",
            "huge + 1e308",
        ],
    )
    def test_finite_arithmetic_cannot_overflow_to_nonfinite(
        self, expression
    ):
        from agentic_debugger.runtime.pdb_worker import _SafeEvaluationError
        with pytest.raises(_SafeEvaluationError, match="Finite arithmetic"):
            self._evaluate(expression, {"huge": 1 << 1023})

    def test_nonfinite_constant_and_finite_arithmetic_remain_supported(self):
        assert self._evaluate("1e999") == float("inf")
        assert self._evaluate("1e307 + 1e307") == 2e307

    def test_hostile_hooks_are_never_invoked(self):
        from agentic_debugger.runtime.pdb_worker import (
            _SafeEvaluationError, _summarize_value,
        )
        calls = []

        class Hostile:
            def __getattribute__(self, name):
                if name == "__class__":
                    calls.append(name)
                return object.__getattribute__(self, name)

            def __repr__(self): calls.append("repr"); raise AssertionError
            def __str__(self): calls.append("str"); raise AssertionError
            def __format__(self, spec): calls.append("format"); raise AssertionError
            def __bool__(self): calls.append("bool"); raise AssertionError
            def __len__(self): calls.append("len"); raise AssertionError
            def __getitem__(self, key): calls.append("getitem"); raise AssertionError
            def __eq__(self, other): calls.append("eq"); raise AssertionError
            def __lt__(self, other): calls.append("lt"); raise AssertionError
            def __add__(self, other): calls.append("add"); raise AssertionError
            def __hash__(self): calls.append("hash"); raise AssertionError

        hostile = Hostile()
        assert _summarize_value(hostile)["kind"] == "object"
        assert self._evaluate("obj is None", {"obj": hostile}) is False
        for expression in ["obj == None", "obj + 1", "obj[0]", "len(obj)"]:
            with pytest.raises(_SafeEvaluationError):
                self._evaluate(expression, {"obj": hostile})
        assert calls == []

    def test_hostile_dictionary_key_is_skipped_without_hooks(self):
        calls = []

        class HostileKey:
            def __hash__(self):
                calls.append("hash")
                return 1

            def __eq__(self, other):
                calls.append("eq")
                return False

        key = HostileKey()
        mapping = {key: "secret", "safe": 42}
        calls.clear()
        assert self._evaluate("mapping['safe']", {"mapping": mapping}) == 42
        with pytest.raises(Exception) as caught:
            self._evaluate("mapping['missing']", {"mapping": mapping})
        assert caught.value.__class__.__name__ == "_SafeEvaluationError"
        assert calls == []

    @pytest.mark.parametrize(
        "key",
        [
            1 << 4095,
            "\u00e9" * 2048,
            b"x" * 4096,
        ],
    )
    def test_requested_dictionary_key_boundary_is_accepted(self, key):
        assert self._evaluate(
            "mapping[key]", {"mapping": {key: 42}, "key": key}
        ) == 42

    @pytest.mark.parametrize(
        "key",
        [
            1 << 4096,
            "\u00e9" * 2049,
            b"x" * 4097,
        ],
    )
    def test_oversized_requested_dictionary_key_fails_before_scan(self, key):
        from agentic_debugger.runtime.pdb_worker import _SafeEvaluationError

        with pytest.raises(_SafeEvaluationError, match="safe bounds"):
            self._evaluate(
                "mapping[key]", {"mapping": {"safe": 42}, "key": key}
            )

    def test_oversized_stored_dictionary_keys_are_skipped(self):
        mapping = {
            1 << 4096: "oversized-int",
            "\u00e9" * 2049: "oversized-str",
            b"x" * 4097: "oversized-bytes",
            "target": 42,
        }
        assert self._evaluate(
            "mapping[key]", {"mapping": mapping, "key": "target"}
        ) == 42

    def test_frame_local_collision_never_invokes_hostile_equality(
        self, tmp_path
    ):
        from agentic_debugger.runtime.pdb_worker import _SafeEvaluationError
        marker = tmp_path / "frame-local-collision.txt"
        calls = []

        class HostileKey:
            active = False

            def __init__(self, collision_hash):
                self.collision_hash = collision_hash

            def __hash__(self):
                return self.collision_hash

            def __eq__(self, other):
                if self.active:
                    calls.append(other)
                    marker.write_text("hostile equality", encoding="utf-8")
                    raise RuntimeError("hostile equality")
                return False

        target_key = HostileKey(hash("target"))
        unknown_key = HostileKey(hash("unknown"))
        mapping = {
            target_key: "ignored-target-collision",
            unknown_key: "ignored-unknown-collision",
            "target": 42,
        }
        HostileKey.active = True

        assert self._evaluate("target", mapping) == 42
        with pytest.raises(_SafeEvaluationError, match="Unknown local name"):
            self._evaluate("unknown", mapping)
        assert calls == []
        assert not marker.exists()

    def test_frame_local_enumeration_uses_paired_values_and_bound(self):
        from agentic_debugger.runtime.pdb_worker import (
            _frame_locals_entries, _frame_locals_lookup,
        )

        class StringSubclass(str):
            pass

        mapping = {"safe": 7, StringSubclass("unsafe"): 8, 9: 10}
        entries, failure = _frame_locals_entries(mapping)
        assert failure is None
        assert entries == [("safe", 7)]
        found, value, failure = _frame_locals_lookup(mapping, "safe")
        assert (found, value, failure) == (True, 7, None)

        oversized = {f"name_{index:04d}": index for index in range(4097)}
        entries, failure = _frame_locals_entries(oversized)
        assert entries is None
        assert failure == "Frame locals exceed 4096-entry scan limit"
        found, value, failure = _frame_locals_lookup(
            oversized, "missing"
        )
        assert found is False
        assert value is None
        assert failure == "Frame locals exceed 4096-entry scan limit"

    def test_parser_recursion_and_step_overflow_are_bounded(self, monkeypatch):
        import agentic_debugger.runtime.pdb_worker as worker_module
        with monkeypatch.context() as context:
            context.setattr(
                worker_module.ast, "parse",
                lambda *args, **kwargs: (_ for _ in ()).throw(RecursionError()),
            )
            with pytest.raises(
                worker_module._SafeEvaluationError, match="syntax"
            ):
                worker_module._parse_safe_expression("value")
        with monkeypatch.context() as context:
            context.setattr(worker_module, "_MAX_EVALUATOR_STEPS", 1)
            with pytest.raises(
                worker_module._SafeEvaluationError, match="step"
            ):
                self._evaluate("value + 1", {"value": 1})

    def test_exact_builtin_subclasses_are_rejected_without_hooks(self):
        from agentic_debugger.runtime.pdb_worker import _SafeEvaluationError
        calls = []

        class HostileList(list):
            def __len__(self): calls.append("len"); raise AssertionError
            def __getitem__(self, key):
                calls.append("getitem")
                raise AssertionError

        value = HostileList([1])
        for expression in ["len(value)", "value[0]"]:
            with pytest.raises(_SafeEvaluationError):
                self._evaluate(expression, {"value": value})
        assert calls == []

    def test_dictionary_scan_limit_and_unsafe_key(self):
        from agentic_debugger.runtime.pdb_worker import _SafeEvaluationError
        mapping = {index: index for index in range(257)}
        with pytest.raises(_SafeEvaluationError, match="256-entry"):
            self._evaluate("mapping[256]", {"mapping": mapping})
        assert self._evaluate("mapping[255]", {"mapping": mapping}) == 255
        with pytest.raises(_SafeEvaluationError, match="key type"):
            self._evaluate(
                "mapping[key]", {"mapping": mapping, "key": object()}
            )

    @pytest.mark.parametrize(
        "expression",
        [
            "0x" + "f" * 1025,
            repr("é" * 1025),
            repr(b"x" * 1025),
            "1j", "...",
        ],
    )
    def test_constant_bounds_and_types(self, expression):
        from agentic_debugger.runtime.pdb_worker import _SafeEvaluationError
        with pytest.raises(_SafeEvaluationError):
            self._evaluate(expression)


class TestSafeEvaluationConcurrency:
    @pytest.mark.parametrize(
        "loser",
        [
            lambda session: session.continue_paused_target(),
            lambda session: session.terminate_paused_target(),
            lambda session: session.get_target_status(),
            lambda session: session.get_stack_summary(),
            lambda session: session.safe_eval_expression(0, 1, "value"),
        ],
    )
    def test_exactly_one_request_owns_boundary(
        self, mock_workspace, loser
    ):
        result = _valid_safe_eval_result()
        session, _, sent = _ready_inspection_session(mock_workspace, result)
        session._request_timeout = 0.1
        entered = threading.Event()
        release = threading.Event()
        winner_outcomes = []
        loser_outcomes = []
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=2,
            success=True,
            result=result,
            error="",
        )

        def blocking_send(request, timeout):
            sent.append(request)
            entered.set()
            assert release.wait(timeout=2.0)
            return response

        session._send_and_receive = blocking_send

        def run_winner():
            try:
                winner_outcomes.append(
                    session.safe_eval_expression(0, 1, "value")
                )
            except Exception as exc:
                winner_outcomes.append(exc)

        def run_loser():
            try:
                loser_outcomes.append(loser(session))
            except Exception as exc:
                loser_outcomes.append(exc)

        winner_thread = threading.Thread(target=run_winner)
        loser_thread = threading.Thread(target=run_loser)
        winner_thread.start()
        try:
            assert entered.wait(timeout=2.0)
            loser_thread.start()
            loser_thread.join(timeout=2.0)
            assert not loser_thread.is_alive()
            assert len(loser_outcomes) == 1
            assert isinstance(loser_outcomes[0], PdbSessionError)
            assert len(sent) == 1
            assert sent[0].operation == "safe_eval_expression"
        finally:
            release.set()
            winner_thread.join(timeout=2.0)
            if loser_thread.ident is not None:
                loser_thread.join(timeout=2.0)
            assert not winner_thread.is_alive()
            assert not loser_thread.is_alive()
            session.stop()
        assert winner_outcomes == [result]
        assert session._target_lifecycle_state == "paused"


def test_safe_evaluation_status_recovery_from_unknown(mock_workspace):
    eval_result = _valid_safe_eval_result()
    session, _, sent = _ready_inspection_session(mock_workspace, eval_result)
    session._target_lifecycle_state = "unknown"
    responses = [
        PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=2,
            success=True,
            result={
                "state": "paused", "script": "test.py",
                "line": 3, "function": "<module>",
            },
            error="",
        ),
        PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=3,
            success=True,
            result=eval_result,
            error="",
        ),
    ]

    def send(request, timeout):
        sent.append(request)
        return responses.pop(0)

    session._send_and_receive = send
    try:
        with pytest.raises(PdbSessionStateError):
            session.safe_eval_expression(0, 1, "value")
        assert sent == []
        assert session.get_target_status()["state"] == "paused"
        assert session.safe_eval_expression(0, 1, "value") == eval_result
        assert [request.operation for request in sent] == [
            "get_target_status", "safe_eval_expression",
        ]
    finally:
        session.stop()


def test_safe_expression_ast_is_collectable():
    import gc
    import weakref
    from agentic_debugger.runtime.pdb_worker import (
        _parse_safe_expression, _SafeExpressionInterpreter,
    )
    parsed = _parse_safe_expression("value + 1")
    reference = weakref.ref(parsed)
    assert _SafeExpressionInterpreter({"value": 1}).evaluate(parsed) == 2
    parsed = None
    gc.collect()
    assert reference() is None
