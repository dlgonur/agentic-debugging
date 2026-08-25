"""Task 8 unit gates: the cancellable configured command transport.

Exercises the real subprocess/protocol path of the accepted JSON-lines
command transport (protocol reuse) with the bounded additions Task 8
requires: cooperative cancellation, request timeout, tree-wide termination,
bounded stdout/stderr, and the unchanged failure vocabulary (malformed
JSON, wrong response shape, empty output, extra noise, non-zero exit,
oversized output).  Malformed output is never reinterpreted as a valid
directive.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_debugger.application.command_transport import (
    CancellableJsonlCommandTransport,
)
from agentic_debugger.cancellation import CancellationError, CancellationToken
from agentic_debugger.evaluation.live import (
    LiveModelConfig,
    LiveTransportError,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "command_models" / "dummy_command_model.py"


def make_config(command) -> LiveModelConfig:
    return LiveModelConfig("dummy-command", tuple(command), request_timeout_seconds=30.0)


def transport_for(command, **kwargs) -> CancellableJsonlCommandTransport:
    return CancellableJsonlCommandTransport(make_config(command), **kwargs)


def py(code: str):
    return [sys.executable, "-c", code]


VALID_RESPONSE = "import sys,json; sys.stdout.write(json.dumps({'kind':'action','name':'run_reproduction','arguments':{'phase':'baseline'}})+chr(10))"


def wait_for_file(path: Path, timeout_seconds: float = 15.0) -> bool:
    """Bounded readiness wait: poll until ``path`` exists or the deadline.

    Process tests must synchronize on an explicit ready/PID marker written by
    the fixture, never on a fixed arbitrary sleep before cancellation.  The
    deadline is generous (loaded CI) but bounded, so a fixture that never
    becomes ready fails the test instead of hanging it.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def wait_until_dead(pid: int, timeout_seconds: float = 10.0) -> bool:
    """Bounded wait until ``pid`` is no longer alive (termination evidence)."""
    from agentic_debugger.application.process_tree import pid_is_alive

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not pid_is_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_is_alive(pid)


class TestRequestSuccess:
    def test_valid_response_round_trip(self):
        transport = transport_for(py(VALID_RESPONSE))
        response = transport.request({"controller": {"state": "Reproduce"}}, 30.0)
        assert response == {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}

    def test_stderr_does_not_break_a_valid_response(self):
        code = "import sys; sys.stderr.write('diagnostic line\\n'); " + VALID_RESPONSE
        transport = transport_for(py(code))
        response = transport.request({}, 30.0)
        assert response["kind"] == "action"

    def test_stderr_activity_refreshes_idle_watchdog(self):
        code = (
            "import sys,time; "
            "[(sys.stderr.write('\\n'),sys.stderr.flush(),time.sleep(.12)) for _ in range(6)]; "
            + VALID_RESPONSE
        )
        transport = transport_for(py(code))
        started = time.monotonic()
        response = transport.request({}, 0.25)
        assert response["kind"] == "action"
        assert time.monotonic() - started > 0.6

    def test_environment_overrides_are_applied(self):
        code = (
            "import os,sys,json; "
            "sys.stdout.write(json.dumps({'kind':'action','name':os.environ.get('DUMMY_MODEL_VAR','none'),'arguments':{}})+chr(10))"
        )
        transport = transport_for(py(code), environment={"DUMMY_MODEL_VAR": "from-override"})
        response = transport.request({}, 30.0)
        assert response["name"] == "from-override"

    def test_cwd_is_applied(self, tmp_path):
        code = (
            "import os,sys,json; "
            "sys.stdout.write(json.dumps({'kind':'action','name':os.getcwd(),'arguments':{}})+chr(10))"
        )
        transport = transport_for(py(code), cwd=str(tmp_path))
        response = transport.request({}, 30.0)
        assert Path(response["name"]).resolve() == tmp_path.resolve()


class TestFailureVocabulary:
    def test_malformed_json(self):
        transport = transport_for(py("print('{not-json}')"))
        with pytest.raises(LiveTransportError) as exc:
            transport.request({}, 30.0)
        assert exc.value.kind == "invalid_response"

    def test_empty_output(self):
        transport = transport_for(py("pass"))
        with pytest.raises(LiveTransportError) as exc:
            transport.request({}, 30.0)
        assert exc.value.kind == "invalid_response"

    def test_extra_output_noise(self):
        transport = transport_for(py("import sys; print('noise line'); " + VALID_RESPONSE))
        with pytest.raises(LiveTransportError) as exc:
            transport.request({}, 30.0)
        assert exc.value.kind == "invalid_response"

    def test_wrong_response_shape(self):
        transport = transport_for(py("print('[1,2,3]')"))
        with pytest.raises(LiveTransportError) as exc:
            transport.request({}, 30.0)
        assert exc.value.kind == "invalid_response"

    def test_non_zero_exit(self):
        transport = transport_for(py("import sys; sys.exit(3)"))
        with pytest.raises(LiveTransportError) as exc:
            transport.request({}, 30.0)
        assert exc.value.kind == "process_error"

    def test_typed_adapter_error_preserves_safe_kind_and_message(self):
        envelope = json.dumps(
            {
                "schema_version": "command-error-v1",
                "kind": "http_error",
                "message": "Ollama HTTP request returned status 401",
            },
            separators=(",", ":"),
        )
        command = py(
            f"import sys; sys.stdin.read(); sys.stderr.write({envelope!r}); "
            "raise SystemExit(1)"
        )
        with pytest.raises(LiveTransportError) as exc:
            transport_for(command).request({}, 30.0)
        assert exc.value.kind == "http_error"
        assert exc.value.safe_message == "Ollama HTTP request returned status 401"

    def test_credential_shaped_typed_message_falls_back_to_process_error(self):
        command = py(
            "import sys,json; sys.stdin.read(); message='to'+'ken'+'='+'must-not-survive'; "
            "sys.stderr.write(json.dumps({'schema_version':'command-error-v1',"
            "'kind':'http_error','message':message},separators=(',',':'))); "
            "raise SystemExit(1)"
        )
        with pytest.raises(LiveTransportError) as exc:
            transport_for(command).request({}, 30.0)
        assert exc.value.kind == "process_error"
        assert exc.value.safe_message is None

    def test_launch_error_for_missing_executable(self):
        transport = transport_for(["definitely-not-a-real-executable-xyz"])
        with pytest.raises(LiveTransportError) as exc:
            transport.request({}, 30.0)
        assert exc.value.kind == "launch_error"

    def test_oversized_stdout_fails_bounded(self):
        transport = transport_for(
            py("import sys; sys.stdout.write('x' * 2000000)"),
            max_output_bytes=1024,
        )
        with pytest.raises(LiveTransportError) as exc:
            transport.request({}, 30.0)
        assert exc.value.kind == "response_too_large"

    def test_oversized_stderr_does_not_break_response(self):
        code = "import sys; sys.stderr.write('y' * 2000000); " + VALID_RESPONSE
        transport = transport_for(py(code), max_output_bytes=1024)
        response = transport.request({}, 30.0)
        assert response["kind"] == "action"


class TestTimeoutAndCancellation:
    def test_request_timeout(self):
        transport = transport_for(py("import time; time.sleep(60)"), cancel_check=None)
        started = time.monotonic()
        with pytest.raises(LiveTransportError) as exc:
            transport.request({}, 1.0)
        assert exc.value.kind == "request_timeout"
        assert exc.value.timed_out is True
        assert time.monotonic() - started < 30.0

    def test_cancellation_raises_cancellation_error(self, tmp_path):
        # Deterministic: cancellation is gated on the fixture's explicit
        # readiness marker (its own pid file), never on a fixed sleep.
        pid_file = tmp_path / "simple.pid"
        code = (
            "import os,sys,time; "
            f"open({str(pid_file)!r},'w').write(str(os.getpid())); "
            "time.sleep(60)"
        )
        token = CancellationToken()
        transport = transport_for(py(code), cancel_check=token.check)

        def _cancel() -> None:
            assert wait_for_file(pid_file), "command never became ready"
            token.request()

        threading.Thread(target=_cancel, daemon=True).start()
        with pytest.raises(CancellationError):
            transport.request({}, 60.0)

    def test_cancellation_is_never_a_transport_error(self):
        # The accepted contract: the worker classifies cancellation as
        # CANCELLED, never as a model/transport failure.
        token = CancellationToken()
        transport = transport_for(
            py("import time; time.sleep(60)"),
            cancel_check=token.check,
        )
        token.request()
        try:
            transport.request({}, 60.0)
        except CancellationError:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            pytest.fail(f"cancellation surfaced as {type(exc).__name__}: {exc}")
        else:  # pragma: no cover - defensive
            pytest.fail("cancellation did not raise")

    def test_timeout_terminates_descendant_tree(self, tmp_path):
        # Deterministic: the fixture spawns child -> grandchild and records
        # both pids before it ever reads the request.  The request timeout is
        # the trigger; it is set comfortably longer than the bounded readiness
        # wait so the tree is confirmed spawned before it fires.  No fixed
        # pre-sleep.
        child_pid_file = tmp_path / "child.pid"
        grandchild_pid_file = tmp_path / "grandchild.pid"
        command = [
            sys.executable,
            str(FIXTURE),
            "spawn_child",
            "--state-dir",
            str(tmp_path / "state"),
            "--child-pid-file",
            str(child_pid_file),
            "--grandchild-pid-file",
            str(grandchild_pid_file),
            "--delay",
            "60",
        ]
        transport = transport_for(command)
        with pytest.raises(LiveTransportError) as exc:
            transport.request({}, 6.0)
        assert exc.value.kind == "request_timeout"
        # Readiness evidence: the tree was spawned before the timeout fired.
        assert wait_for_file(child_pid_file), "child was never spawned"
        assert wait_for_file(grandchild_pid_file), "grandchild was never spawned"
        child_pid = int(child_pid_file.read_text())
        grandchild_pid = int(grandchild_pid_file.read_text())
        assert wait_until_dead(child_pid), "descendant survived the request timeout"
        assert wait_until_dead(grandchild_pid), "grandchild survived the request timeout"

    def test_cancellation_terminates_descendant_tree(self, tmp_path):
        # Deterministic: cancellation is gated on the fixture's explicit
        # readiness markers (child + grandchild pid files), never on a fixed
        # sleep, so the tree is confirmed spawned before cancellation fires.
        child_pid_file = tmp_path / "child.pid"
        grandchild_pid_file = tmp_path / "grandchild.pid"
        command = [
            sys.executable,
            str(FIXTURE),
            "spawn_child",
            "--state-dir",
            str(tmp_path / "state"),
            "--child-pid-file",
            str(child_pid_file),
            "--grandchild-pid-file",
            str(grandchild_pid_file),
            "--delay",
            "60",
        ]
        token = CancellationToken()
        transport = transport_for(command, cancel_check=token.check)

        def _cancel() -> None:
            assert wait_for_file(child_pid_file), "child was never spawned"
            assert wait_for_file(grandchild_pid_file), "grandchild was never spawned"
            token.request()

        threading.Thread(target=_cancel, daemon=True).start()
        with pytest.raises(CancellationError):
            transport.request({}, 60.0)
        child_pid = int(child_pid_file.read_text())
        grandchild_pid = int(grandchild_pid_file.read_text())
        assert wait_until_dead(child_pid), "descendant survived cancellation"
        assert wait_until_dead(grandchild_pid), "grandchild survived cancellation"

    def test_stdin_write_timeout(self):
        # A command that never reads stdin with a request larger than the
        # pipe buffer must fail with the stdin-write timeout, not hang.
        transport = transport_for(py("import time; time.sleep(60)"))
        big_payload = {"blob": "x" * 200000}
        with pytest.raises(LiveTransportError) as exc:
            transport.request(big_payload, 1.0)
        assert exc.value.kind == "request_timeout"


class TestBlockedStdinCancellation:
    """Blocker A: cancellation must win over a blocked request write.

    A configured command that never reads stdin fills the OS pipe with a
    large request, blocking the writer thread.  An explicit cancellation
    must interrupt that blocked write promptly and surface the neutral
    :class:`CancellationError` -- never ``request_timeout`` -- and must
    terminate the command process (and any descendant) without leaking it.
    """

    def test_cancel_wins_over_blocked_stdin_write(self, tmp_path):
        pid_file = tmp_path / "child.pid"
        command = [
            sys.executable,
            str(FIXTURE),
            "hang_on_stdin",
            "--pid-file",
            str(pid_file),
        ]
        token = CancellationToken()
        transport = transport_for(command, cancel_check=token.check)

        def _cancel() -> None:
            # Deterministic: gate cancellation on the fixture's explicit
            # readiness marker (its own pid file), never on a fixed sleep.
            assert wait_for_file(pid_file), "command never became ready"
            token.request()

        threading.Thread(target=_cancel, daemon=True).start()
        # A request large enough to fill the pipe (the child never reads
        # stdin) with a timeout long enough to prove cancellation, not the
        # deadline, is what ends the request.
        big_payload = {"blob": "x" * 500000}
        started = time.monotonic()
        with pytest.raises(CancellationError):
            transport.request(big_payload, 30.0)
        elapsed = time.monotonic() - started
        # Prompt: well under the 30 s request timeout.
        assert elapsed < 10.0, f"cancellation took {elapsed:.1f}s"

        # The command process is dead (no orphan from the blocked write).
        child_pid = int(pid_file.read_text())
        assert wait_until_dead(child_pid), "command survived blocked-write cancel"

    def test_cancel_before_spawn_raises_without_launch(self):
        # Cancellation requested before the request is issued must raise the
        # neutral signal and never spawn the command.
        token = CancellationToken()
        token.request()
        transport = transport_for(
            py("import time; time.sleep(60)"), cancel_check=token.check
        )
        with pytest.raises(CancellationError):
            transport.request({}, 30.0)

    def test_blocked_stdin_write_timeout_still_distinct(self, tmp_path):
        # With no cancellation, a blocked stdin write still honors the
        # request deadline as a genuine request_timeout (semantics preserved).
        command = [sys.executable, str(FIXTURE), "hang_on_stdin"]
        transport = transport_for(command, cancel_check=None)
        big_payload = {"blob": "x" * 500000}
        with pytest.raises(LiveTransportError) as exc:
            transport.request(big_payload, 1.0)
        assert exc.value.kind == "request_timeout"
        assert exc.value.timed_out is True


class TestProtocolParity:
    def test_uses_the_same_wire_format_as_the_scientific_transport(self):
        # The accepted scientific transport parses the same stdout bytes;
        # prove the cancellable variant emits a request the scientific
        # transport's command contract accepts (fixture "valid" full-run
        # coverage lives in the integration suite).
        from agentic_debugger.evaluation.live import JsonlCommandTransport

        code = (
            "import sys,json; line=sys.stdin.buffer.readline(); "
            "req=json.loads(line); "
            "sys.stdout.write(json.dumps({'kind':'transition','target_state':'Understand','reason':'ok'})+chr(10))"
        )
        config = LiveModelConfig("dummy", tuple(py(code)), request_timeout_seconds=30.0)
        scientific = JsonlCommandTransport(config, max_output_bytes=1024)
        cancellable = CancellableJsonlCommandTransport(config, max_output_bytes=1024)
        payload = {"controller": {"state": "Reproduce"}, "hello": "world"}
        assert scientific.request(payload, 30.0) == cancellable.request(payload, 30.0)
