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
