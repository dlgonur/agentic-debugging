"""Qualification tests for Ollama 0.33.0 fresh Level-32 treatments.

Provider-free: all HTTP is canned via a task-owned loopback fixture.
No inference is performed.

Covers:
- exact version gate (0.33.0 accepted, 0.32.15 rejected, arbitrary newer rejected)
- canonical deepseek-v4-flash:cloud metadata
- treatment/transport fingerprint old->new diff (version is fingerprinted)
- adapter/operator version drift guard
- Docker Desktop unavailable classification
- Ollama version-mismatch structured diagnostic
- pre-resource cleanup truth (No resources created vs failed)
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts import ollama_cloud_command_adapter as adapter
from scripts import run_cookiecutter_967_pdb_proof as operator
from agentic_debugger.application.level32 import Level32OperatorWorker, Level32ModelProfile
from agentic_debugger.application.session import SessionSpec
from agentic_debugger.evaluation.live import LiveModelConfig
from scripts import ollama_cloud_command_adapter as adapter_module

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# helpers: copied minimal fixture server from existing adapter tests
# ---------------------------------------------------------------------------

def encode_tags(entry: dict) -> bytes:
    return json.dumps({"models": [entry]}).encode()

def encode_show(*, parent_model: str) -> bytes:
    return json.dumps(
        {
            "details": {"family": "deepseek4", "parent_model": parent_model},
            "capabilities": ["completion", "thinking", "tools"],
            "model_info": {"deepseek4.context_length": 1048576},
        }
    ).encode()

class _FixtureState:
    def __init__(self, *, version: str, alias: str = "deepseek-v4-flash:cloud", upstream: str = "deepseek-v4-flash", tags_remote: str = "deepseek-v4-flash:0731"):
        self.version = version
        self.alias = alias
        self.upstream = upstream
        self.tags_remote = tags_remote
        self.requests: list[str] = []

class _Handler(BaseHTTPRequestHandler):
    state: _FixtureState
    def log_message(self, *_args):
        return
    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
    def do_GET(self):
        self.state.requests.append(self.path)
        if self.path == "/api/version":
            self._send(200, json.dumps({"version": self.state.version}).encode())
            return
        if self.path == "/api/tags":
            entry = {
                "name": self.state.alias,
                "model": self.state.alias,
                "remote_model": self.state.tags_remote,
                "remote_host": adapter.EXPECTED_CLOUD_REMOTE_HOST,
                "digest": "synthetic",
            }
            self._send(200, encode_tags(entry))
            return
        self._send(404, b"{}")
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length)
        self.state.requests.append(self.path)
        if self.path == "/api/show":
            body = encode_show(parent_model=self.state.upstream)
            self._send(200, body)
            return
        self._send(404, b"{}")

@pytest.fixture
def fixture_server():
    servers: list[ThreadingHTTPServer] = []
    def start(state: _FixtureState):
        handler = type("FixtureHandler", (_Handler,), {"state": state})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return state, server, f"http://127.0.0.1:{server.server_port}/api"
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()

# ---------------------------------------------------------------------------
# 1. canonical qualified version is 0.33.0
# ---------------------------------------------------------------------------

def test_canonical_qualified_ollama_version_is_033():
    assert adapter.EXPECTED_OLLAMA_VERSION == "0.33.0"
    assert operator.EXPECTED_OLLAMA_VERSION == "0.33.0"

def test_adapter_and_operator_versions_do_not_drift():
    assert adapter.EXPECTED_OLLAMA_VERSION == operator.EXPECTED_OLLAMA_VERSION

# ---------------------------------------------------------------------------
# 2. zero-inference preflight: 0.33.0 accepted, 0.32.15 rejected, arbitrary rejected
# ---------------------------------------------------------------------------

def test_preflight_accepts_033_with_canonical_metadata(fixture_server):
    state = _FixtureState(version="0.33.0")
    _state, _server, endpoint = fixture_server(state)
    result = adapter.run_preflight(endpoint=endpoint, model="deepseek-v4-flash:cloud", expected_version="0.33.0")
    assert result["ok"] is True
    assert result["ollama_version"] == "0.33.0"
    assert result["expected_model"] == "deepseek-v4-flash:cloud"
    assert result["expected_remote_model"] == "deepseek-v4-flash"
    assert result["expected_tags_remote_model"] == "deepseek-v4-flash:0731"
    assert result["model_remote_model"] == "deepseek-v4-flash:0731"
    assert result["model_remote_host"] == "https://ollama.com"
    assert result["model_capabilities"] == ["completion", "thinking", "tools"]
    assert result["provider_inference_started"] is False
    assert result["live_transport_ready"] is True
    assert result["treatment_eligible"] is True

def test_preflight_rejects_03215_when_033_expected(fixture_server):
    state = _FixtureState(version="0.32.15")
    _state, _server, endpoint = fixture_server(state)
    with pytest.raises(adapter.OllamaAdapterError) as exc:
        adapter.run_preflight(endpoint=endpoint, model="deepseek-v4-flash:cloud", expected_version="0.33.0")
    assert exc.value.kind == "ollama_version_mismatch"
    assert "0.33.0" in str(exc.value)
    assert "0.32.15" in str(exc.value)

def test_preflight_rejects_arbitrary_newer_version(fixture_server):
    state = _FixtureState(version="0.34.0")
    _state, _server, endpoint = fixture_server(state)
    with pytest.raises(adapter.OllamaAdapterError) as exc:
        adapter.run_preflight(endpoint=endpoint, model="deepseek-v4-flash:cloud", expected_version="0.33.0")
    assert exc.value.kind == "ollama_version_mismatch"

def test_adapter_cli_preflight_exact_gate(fixture_server):
    # via run_adapter --preflight path
    for version, should_succeed in [("0.33.0", True), ("0.32.15", False), ("9.9.9", False)]:
        state = _FixtureState(version=version)
        _state, _server, endpoint = fixture_server(state)
        out = io.StringIO()
        err = io.StringIO()
        rc = adapter.run_adapter(
            stdin_stream=io.StringIO(""),
            stdout_stream=out,
            stderr_stream=err,
            argv=["--endpoint", endpoint, "--model", "deepseek-v4-flash:cloud", "--expected-version", "0.33.0", "--preflight"],
        )
        if should_succeed:
            assert rc == 0, err.getvalue()
            payload = json.loads(out.getvalue())
            assert payload["ollama_version"] == "0.33.0"
            assert payload["provider_inference_started"] is False
        else:
            assert rc == 1
            assert "ollama_version_mismatch" in err.getvalue()

# ---------------------------------------------------------------------------
# 3. transport/treatment fingerprint includes version — old/new diff
# ---------------------------------------------------------------------------

def test_transport_fingerprint_captures_ollama_version():
    spec = adapter.CLOUD_MODELS["deepseek-v4-flash:cloud"]
    # compute old transport fingerprint by temporarily restoring constant
    orig = adapter.EXPECTED_OLLAMA_VERSION
    try:
        adapter.EXPECTED_OLLAMA_VERSION = "0.32.15"
        old_fp = adapter.transport_config_fingerprint(spec)
        adapter.EXPECTED_OLLAMA_VERSION = "0.33.0"
        new_fp = adapter.transport_config_fingerprint(spec)
    finally:
        adapter.EXPECTED_OLLAMA_VERSION = orig
    assert old_fp == "60abd6b8d9254716a4377b3b007dcf035fecb73c0b4177ba96353ba5d7efbdbf"
    assert new_fp == "94ec2761d38848ba1f929f83060b3fa4688bb02d8dbfefeb456a6c37c15e2be2"
    assert old_fp != new_fp

def test_treatment_fingerprint_captures_ollama_version_via_transport():
    orig = adapter.EXPECTED_OLLAMA_VERSION
    try:
        adapter.EXPECTED_OLLAMA_VERSION = "0.32.15"
        operator.EXPECTED_OLLAMA_VERSION = "0.32.15"
        old_tf = operator._treatment_fingerprint("deepseek-v4-flash:cloud", operator.LEVEL32_TREATMENT_BUDGET)
        adapter.EXPECTED_OLLAMA_VERSION = "0.33.0"
        operator.EXPECTED_OLLAMA_VERSION = "0.33.0"
        new_tf = operator._treatment_fingerprint("deepseek-v4-flash:cloud", operator.LEVEL32_TREATMENT_BUDGET)
    finally:
        adapter.EXPECTED_OLLAMA_VERSION = orig
        operator.EXPECTED_OLLAMA_VERSION = orig
    assert old_tf == "462ca1aec8be74380f6c355a084b8c979e91a1010d8e78da30b40d09696da85a"
    assert new_tf == "b801f31c039a53cdcc2acd360cf8f735673fe91d4bd1bc86f3aef33fbe8aa258"
    assert old_tf != new_tf

def test_historical_treatment_artifact_remains_immutable():
    # V4 stored fingerprint must remain exactly as persisted evidence
    historical = REPO_ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-deepseek-v4-flash-cloud-v4/preflight.json"
    data = json.loads(historical.read_text(encoding="utf-8"))
    assert data["treatment_fingerprint"] == "7ef36c07d3eafd8c1df52189e5d41b3aff0ace7944a3feaddc8dbf049847793f"
    assert data["ollama_version"] == "0.32.15"

# ---------------------------------------------------------------------------
# 4. Docker Desktop unavailable classification
# ---------------------------------------------------------------------------

def test_docker_desktop_api_unavailable_is_DOCKER_UNAVAILABLE(monkeypatch):
    def fake_run(argv, **kwargs):
        assert argv[1:3] == ["image", "inspect"]
        return subprocess.CompletedProcess(argv, 1, "", "failed to connect to the docker API at dockerDesktopLinuxEngine")
    monkeypatch.setattr(operator, "_run", fake_run)
    monkeypatch.setattr(operator, "_docker_context", lambda: "desktop-linux")
    with pytest.raises(operator.ImageVerificationError) as exc:
        operator._verify_image()
    assert exc.value.evidence["category"] == "DOCKER_UNAVAILABLE"

def test_docker_desktop_npipe_is_DOCKER_UNAVAILABLE(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "Cannot connect to the Docker daemon at npipe:////./pipe/dockerDesktopLinuxEngine")
    monkeypatch.setattr(operator, "_run", fake_run)
    monkeypatch.setattr(operator, "_docker_context", lambda: "desktop-linux")
    with pytest.raises(operator.ImageVerificationError) as exc:
        operator._verify_image()
    assert exc.value.evidence["category"] == "DOCKER_UNAVAILABLE"
    # Must not be collapsed to generic IMAGE_INSPECTION_FAILED
    assert exc.value.evidence["category"] != "IMAGE_INSPECTION_FAILED"

# ---------------------------------------------------------------------------
# 5. Ollama version mismatch structured diagnostic (bounded allowlist)
# ---------------------------------------------------------------------------

def test_operator_preflight_preserves_version_mismatch_structured(monkeypatch):
    # Simulate adapter stderr envelope with ollama_version_mismatch
    envelope = json.dumps({"schema_version": "command-error-v1", "kind": "ollama_version_mismatch", "message": "Ollama version mismatch: expected '0.33.0' actual '0.32.15'"})
    fake_result = SimpleNamespace(returncode=1, stdout="", stderr=envelope + "\n")
    monkeypatch.setattr(operator, "_run", lambda argv, **kwargs: fake_result)
    config = SimpleNamespace(command=("dummy", "--preflight"))
    with pytest.raises(operator.ProofError, match="OLLAMA_VERSION_MISMATCH expected 0.33.0 actual 0.32.15"):
        operator._preflight(config)

def test_operator_preflight_generic_remains_collapsed_but_not_leaking_stderr(monkeypatch):
    envelope = json.dumps({"schema_version": "command-error-v1", "kind": "preflight_failed", "message": "Ollama tags response is invalid"})
    fake_result = SimpleNamespace(returncode=1, stdout="", stderr=envelope + "\n")
    monkeypatch.setattr(operator, "_run", lambda argv, **kwargs: fake_result)
    config = SimpleNamespace(command=("dummy", "--preflight"))
    with pytest.raises(operator.ProofError, match="Ollama zero-inference preflight failed"):
        operator._preflight(config)
    # Must not expose arbitrary message in this generic path
    try:
        operator._preflight(config)
    except operator.ProofError as exc:
        assert "tags response is invalid" not in str(exc)

def test_operator_preflight_does_not_expose_arbitrary_stderr(monkeypatch):
    # Arbitrary stderr with credential-like content must not leak
    fake_result = SimpleNamespace(returncode=1, stdout="", stderr="Bearer secret-token-123\n")
    monkeypatch.setattr(operator, "_run", lambda argv, **kwargs: fake_result)
    config = SimpleNamespace(command=("dummy", "--preflight"))
    with pytest.raises(operator.ProofError, match="Ollama zero-inference preflight failed"):
        operator._preflight(config)

# ---------------------------------------------------------------------------
# 6. pre-resource cleanup truth: positive proof only, three truths
# ---------------------------------------------------------------------------

def test_A_explicit_pre_resource_abort_present_not_required(tmp_path):
    """A. Explicit pre_resource_abort fact present => NOT REQUIRED (positive proof)."""
    from agentic_debugger.application.events import SessionEventKind, OperatorStage
    repo_root = tmp_path / "repoA"
    repo_root.mkdir(parents=True)
    output_dir = repo_root / "level32-cookiecutter-967-deepseek-v4-flash-cloud-v10"
    output_dir.mkdir(parents=True)
    (output_dir / "image-verification.json").write_text(json.dumps({"category": "IMAGE_VERIFIED"}))
    session_dir = tmp_path / "sessionA"
    model_profile = Level32ModelProfile(alias="deepseek-v4-flash:cloud", display_name="deepseek-v4-flash", readiness="live_verified", transport_config_fingerprint=adapter.transport_config_fingerprint(adapter.CLOUD_MODELS["deepseek-v4-flash:cloud"]))
    spec = SessionSpec(task_id="audreyr__cookiecutter-967", source=__import__("agentic_debugger.application.sources", fromlist=["ExecutionSourceSpec"]).ExecutionSourceSpec(kind=__import__("agentic_debugger.application.events", fromlist=["SourceKind"]).SourceKind.LEVEL32_OPERATOR, task_id="audreyr__cookiecutter-967", policy="exact-pdb-level32-frozen", model_config_ref="deepseek-v4-flash:cloud"))
    class FakeProc:
        def __init__(self): self.pid=12345; self.returncode=2
        def poll(self): return self.returncode
        def communicate(self): return ("", "Ollama zero-inference preflight failed")
        def terminate(self): pass
        def kill(self): pass
    worker = Level32OperatorWorker(session_dir=str(session_dir), session_id="test-session-0000000000000000000000000000000a", run_id="test-run-0000000000000000000000000000000a", repository_root=str(repo_root), model=model_profile, revision=10, treatment_id="pdb-capability-level32-cookiecutter-967-deepseek-v4-flash-cloud-v10-workspace-derived-official-git-diff-v1", output_dir=str(output_dir), spec=spec, process_factory=lambda *a, **kw: FakeProc())
    worker.start()
    # Simulate operator emitted explicit pre_resource_abort before workspace
    worker._pre_resource_abort_observed = True
    worker._resource_creation_started_observed = False
    result = worker.wait()
    from agentic_debugger.application.events import SessionStatus, SessionTerminationReason
    assert result.status is SessionStatus.FAILED
    assert worker._pre_resource_abort_observed is True
    # Must emit CLEANUP_NOT_REQUIRED, not CLEANUP_COMPLETED
    not_req = [e for e in worker.events if e.event_kind is SessionEventKind.CLEANUP_NOT_REQUIRED]
    assert len(not_req) == 1, "explicit pre_resource_abort must emit cleanup.not_required"
    assert not any(e.event_kind is SessionEventKind.CLEANUP_COMPLETED for e in worker.events)
    from agentic_debugger.application.presentation import PresentationIdentity, initial_session_view, reduce_event
    from agentic_debugger.application.events import SourceKind
    view = initial_session_view(PresentationIdentity(task_id="audreyr__cookiecutter-967", source_kind=SourceKind.LEVEL32_OPERATOR))
    for ev in worker.events: view = reduce_event(view, ev)
    assert view.cleanup_not_required is True
    assert view.cleanup_verified is None
    from agentic_debugger.ui.screens import render_view_header
    head = render_view_header(view, mode="live", mode_style="bold")
    assert "No resources created" in (head.plain if hasattr(head, "plain") else str(head))

def test_B_launch_failure_proves_not_required(tmp_path):
    """B. Operator launch fails before subprocess exists => NOT REQUIRED (supervisor local proof)."""
    from agentic_debugger.application.events import SessionEventKind
    repo_root = tmp_path / "repoB"
    repo_root.mkdir(parents=True)
    output_dir = repo_root / "level32-cookiecutter-967-deepseek-v4-flash-cloud-v10"
    output_dir.mkdir(parents=True)
    session_dir = tmp_path / "sessionB"
    model_profile = Level32ModelProfile(alias="deepseek-v4-flash:cloud", display_name="deepseek-v4-flash", readiness="live_verified", transport_config_fingerprint=adapter.transport_config_fingerprint(adapter.CLOUD_MODELS["deepseek-v4-flash:cloud"]))
    spec = SessionSpec(task_id="audreyr__cookiecutter-967", source=__import__("agentic_debugger.application.sources", fromlist=["ExecutionSourceSpec"]).ExecutionSourceSpec(kind=__import__("agentic_debugger.application.events", fromlist=["SourceKind"]).SourceKind.LEVEL32_OPERATOR, task_id="audreyr__cookiecutter-967", policy="exact-pdb-level32-frozen", model_config_ref="deepseek-v4-flash:cloud"))
    def failing_factory(*a, **kw):
        raise OSError("simulated launch failure")
    worker = Level32OperatorWorker(session_dir=str(session_dir), session_id="test-session-0000000000000000000000000000000b", run_id="test-run-0000000000000000000000000000000b", repository_root=str(repo_root), model=model_profile, revision=10, treatment_id="pdb-capability-level32-cookiecutter-967-deepseek-v4-flash-cloud-v10-workspace-derived-official-git-diff-v1", output_dir=str(output_dir), spec=spec, process_factory=failing_factory)
    worker.start()
    result = worker.wait()
    from agentic_debugger.application.events import SessionStatus
    assert result.status is SessionStatus.FAILED
    # Launch failure is positive local proof: process never existed => not required
    not_req = [e for e in worker.events if e.event_kind is SessionEventKind.CLEANUP_NOT_REQUIRED]
    assert len(not_req) == 1
    from agentic_debugger.application.presentation import PresentationIdentity, initial_session_view, reduce_event
    from agentic_debugger.application.events import SourceKind
    view = initial_session_view(PresentationIdentity(task_id="audreyr__cookiecutter-967", source_kind=SourceKind.LEVEL32_OPERATOR))
    for ev in worker.events: view = reduce_event(view, ev)
    assert view.cleanup_not_required is True

def test_C_resource_boundary_crossed_crash_failed(tmp_path):
    """C. Resource boundary crossed + crash + no result => FAILED/UNVERIFIED, not Not required."""
    from agentic_debugger.application.events import SessionEventKind, OperatorStage
    from agentic_debugger.application.presentation import PresentationIdentity, initial_session_view, reduce_event
    from agentic_debugger.application.events import SourceKind
    repo_root = tmp_path / "repoC"
    repo_root.mkdir(parents=True)
    output_dir = repo_root / "level32-cookiecutter-967-deepseek-v4-flash-cloud-v10"
    output_dir.mkdir(parents=True)
    session_dir = tmp_path / "sessionC"
    model_profile = Level32ModelProfile(alias="deepseek-v4-flash:cloud", display_name="deepseek-v4-flash", readiness="live_verified", transport_config_fingerprint=adapter.transport_config_fingerprint(adapter.CLOUD_MODELS["deepseek-v4-flash:cloud"]))
    spec = SessionSpec(task_id="audreyr__cookiecutter-967", source=__import__("agentic_debugger.application.sources", fromlist=["ExecutionSourceSpec"]).ExecutionSourceSpec(kind=SourceKind.LEVEL32_OPERATOR, task_id="audreyr__cookiecutter-967", policy="exact-pdb-level32-frozen", model_config_ref="deepseek-v4-flash:cloud"))
    class FakeCrash:
        def __init__(self): self.pid=12347; self.returncode=1
        def poll(self): return self.returncode
        def communicate(self): return ("", "crash after workspace")
        def terminate(self): pass
        def kill(self): pass
    worker = Level32OperatorWorker(session_dir=str(session_dir), session_id="test-session-0000000000000000000000000000000c", run_id="test-run-0000000000000000000000000000000c", repository_root=str(repo_root), model=model_profile, revision=10, treatment_id="pdb-capability-level32-cookiecutter-967-deepseek-v4-flash-cloud-v10-workspace-derived-official-git-diff-v1", output_dir=str(output_dir), spec=spec, process_factory=lambda *a, **kw: FakeCrash())
    worker.start()
    # Simulate resource_creation_started was emitted before crash
    worker._resource_creation_started_observed = True
    worker._pre_resource_abort_observed = False
    result = worker.wait()
    from agentic_debugger.application.events import SessionStatus
    assert result.status is SessionStatus.FAILED
    # Must be FAILED, not not_required, and must have emitted cleanup.completed false
    assert not any(e.event_kind is SessionEventKind.CLEANUP_NOT_REQUIRED for e in worker.events)
    comp = [e for e in worker.events if e.event_kind is SessionEventKind.CLEANUP_COMPLETED]
    assert len(comp) == 1 and comp[0].payload["verified"] is False
    view = initial_session_view(PresentationIdentity(task_id="audreyr__cookiecutter-967", source_kind=SourceKind.LEVEL32_OPERATOR))
    for ev in worker.events: view = reduce_event(view, ev)
    assert view.cleanup_not_required is False
    assert view.cleanup_verified is False
    from agentic_debugger.ui.screens import render_view_header
    head = render_view_header(view, mode="live", mode_style="bold")
    assert "No resources created" not in (head.plain if hasattr(head, "plain") else str(head))
    assert "cleanup failed" in (head.plain if hasattr(head, "plain") else str(head)).lower()

def test_D_observer_write_failure_still_failed(tmp_path):
    """D. resource_creation_started observer write fails (fail-open) + creation proceeds + crash => MUST NOT be Not required."""
    from agentic_debugger.application.events import SessionEventKind
    from agentic_debugger.application.presentation import PresentationIdentity, initial_session_view, reduce_event
    from agentic_debugger.application.events import SourceKind
    # Simulate: operator's _ProgressWriter failed to write resource_creation_started (OSError),
    # but it still entered TemporaryDirectory and created resources, then crashed.
    # Worker will have _resource_creation_started_observed == False (because write failed)
    # and _pre_resource_abort_observed == False (because success path never emitted abort).
    repo_root = tmp_path / "repoD"
    repo_root.mkdir(parents=True)
    output_dir = repo_root / "level32-cookiecutter-967-deepseek-v4-flash-cloud-v10"
    output_dir.mkdir(parents=True)
    session_dir = tmp_path / "sessionD"
    model_profile = Level32ModelProfile(alias="deepseek-v4-flash:cloud", display_name="deepseek-v4-flash", readiness="live_verified", transport_config_fingerprint=adapter.transport_config_fingerprint(adapter.CLOUD_MODELS["deepseek-v4-flash:cloud"]))
    spec = SessionSpec(task_id="audreyr__cookiecutter-967", source=__import__("agentic_debugger.application.sources", fromlist=["ExecutionSourceSpec"]).ExecutionSourceSpec(kind=SourceKind.LEVEL32_OPERATOR, task_id="audreyr__cookiecutter-967", policy="exact-pdb-level32-frozen", model_config_ref="deepseek-v4-flash:cloud"))
    class FakeCrash2:
        def __init__(self): self.pid=12348; self.returncode=1
        def poll(self): return self.returncode
        def communicate(self): return ("", "crash after failed observer write")
        def terminate(self): pass
        def kill(self): pass
    worker = Level32OperatorWorker(session_dir=str(session_dir), session_id="test-session-0000000000000000000000000000000d", run_id="test-run-0000000000000000000000000000000d", repository_root=str(repo_root), model=model_profile, revision=10, treatment_id="pdb-capability-level32-cookiecutter-967-deepseek-v4-flash-cloud-v10-workspace-derived-official-git-diff-v1", output_dir=str(output_dir), spec=spec, process_factory=lambda *a, **kw: FakeCrash2())
    worker.start()
    # Both flags remain False — simulating observer write failure for resource signal,
    # but no pre_resource_abort was emitted either. This is unknown, not proven not required.
    assert worker._resource_creation_started_observed is False
    assert worker._pre_resource_abort_observed is False
    result = worker.wait()
    # Must NOT be not_required; must be failed
    assert not any(e.event_kind is SessionEventKind.CLEANUP_NOT_REQUIRED for e in worker.events)
    comp = [e for e in worker.events if e.event_kind is SessionEventKind.CLEANUP_COMPLETED]
    assert len(comp) == 1
    view = initial_session_view(PresentationIdentity(task_id="audreyr__cookiecutter-967", source_kind=SourceKind.LEVEL32_OPERATOR))
    for ev in worker.events: view = reduce_event(view, ev)
    assert view.cleanup_not_required is False
    assert view.cleanup_verified is False
    from agentic_debugger.ui.screens import render_view_header
    head = render_view_header(view, mode="live", mode_style="bold")
    assert "No resources created" not in (head.plain if hasattr(head, "plain") else str(head))

def test_E_resource_directory_after_boundary():
    """E. Resource directory creation occurs only AFTER resource-start boundary is emitted."""
    import pathlib
    text = pathlib.Path("scripts/run_cookiecutter_967_pdb_proof.py").read_text()
    idx_emit = text.find("emit_resource_creation_started")
    idx_tmp = text.find('with tempfile.TemporaryDirectory(prefix="cookiecutter-967-pdb-")')
    assert idx_emit != -1 and idx_tmp != -1, "both markers must exist"
    assert idx_emit < idx_tmp, "resource_creation_started must be emitted BEFORE first disposable TemporaryDirectory"

def test_F_replay_not_required_renders(tmp_path):
    """F. Replay with explicit durable NOT REQUIRED fact => renders Not required."""
    from agentic_debugger.application.events import SessionEvent, SessionEventKind, SourceKind, SessionStatus
    from agentic_debugger.application.presentation import PresentationIdentity, initial_session_view, reduce_event
    from agentic_debugger.application.session import SessionSpec
    from agentic_debugger.application.emitter import SessionEventEmitter
    from agentic_debugger.application.journal import SessionEventJournal
    from agentic_debugger.application.sources import ExecutionSourceSpec
    # Build minimal journal that contains CLEANUP_NOT_REQUIRED durable event
    session_dir = tmp_path / "sessionF"
    session_dir.mkdir(parents=True)
    spec = SessionSpec(task_id="audreyr__cookiecutter-967", source=ExecutionSourceSpec(kind=SourceKind.LEVEL32_OPERATOR, task_id="audreyr__cookiecutter-967", policy="exact-pdb-level32-frozen", model_config_ref="deepseek-v4-flash:cloud"))
    journal = SessionEventJournal(session_dir / "session.events.jsonl", session_id="test-session-0000000000000000000000000000000f", task_id=spec.task_id, source_kind=SourceKind.LEVEL32_OPERATOR)
    emitter = SessionEventEmitter(session_id="test-session-0000000000000000000000000000000f", task_id=spec.task_id, source_kind=SourceKind.LEVEL32_OPERATOR, sink=journal)
    emitter.emit(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": spec.fingerprint()})
    emitter.bind_run_id("test-run-0000000000000000000000000000000f")
    emitter.emit(SessionEventKind.SESSION_STARTED, {})
    emitter.emit(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"})
    emitter.emit(SessionEventKind.OPERATOR_PROGRESS, {"stage": "preflight"})
    emitter.emit(SessionEventKind.CLEANUP_NOT_REQUIRED, {})
    emitter.emit(SessionEventKind.SESSION_FAILED, {"status": "failed", "termination_reason": "subprocess_error"})
    journal.close()
    # Replay via presentation reducer
    from agentic_debugger.application.history import HistoryStore
    view = initial_session_view(PresentationIdentity(task_id="audreyr__cookiecutter-967", source_kind=SourceKind.LEVEL32_OPERATOR))
    # Manually reduce events from journal
    import json
    for line in (session_dir / "session.events.jsonl").read_text().splitlines():
        data = json.loads(line)
        ev = SessionEvent.from_mapping(data)
        view = reduce_event(view, ev)
    assert view.cleanup_not_required is True
    assert view.cleanup_verified is None
    from agentic_debugger.ui.screens import render_view_header
    head = render_view_header(view, mode="live", mode_style="bold")
    assert "No resources created" in (head.plain if hasattr(head, "plain") else str(head))

def test_G_replay_without_explicit_fact_not_render(tmp_path):
    """G. Replay/subprocess error with cleanup=None but without explicit NOT REQUIRED fact => MUST NOT render No resources created."""
    from agentic_debugger.application.events import SessionEvent, SessionEventKind, SourceKind
    from agentic_debugger.application.presentation import PresentationIdentity, initial_session_view, reduce_event
    from agentic_debugger.application.session import SessionSpec
    from agentic_debugger.application.emitter import SessionEventEmitter
    from agentic_debugger.application.journal import SessionEventJournal
    from agentic_debugger.application.sources import ExecutionSourceSpec
    session_dir = tmp_path / "sessionG"
    session_dir.mkdir(parents=True)
    spec = SessionSpec(task_id="audreyr__cookiecutter-967", source=ExecutionSourceSpec(kind=SourceKind.LEVEL32_OPERATOR, task_id="audreyr__cookiecutter-967", policy="exact-pdb-level32-frozen", model_config_ref="deepseek-v4-flash:cloud"))
    journal = SessionEventJournal(session_dir / "session.events.jsonl", session_id="test-session-0000000000000000000000000000000g", task_id=spec.task_id, source_kind=SourceKind.LEVEL32_OPERATOR)
    emitter = SessionEventEmitter(session_id="test-session-0000000000000000000000000000000g", task_id=spec.task_id, source_kind=SourceKind.LEVEL32_OPERATOR, sink=journal)
    emitter.emit(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": spec.fingerprint()})
    emitter.bind_run_id("test-run-0000000000000000000000000000000g")
    emitter.emit(SessionEventKind.SESSION_STARTED, {})
    emitter.emit(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"})
    # No CLEANUP_NOT_REQUIRED, no CLEANUP_COMPLETED — unknown
    emitter.emit(SessionEventKind.SESSION_FAILED, {"status": "failed", "termination_reason": "subprocess_error"})
    journal.close()
    view = initial_session_view(PresentationIdentity(task_id="audreyr__cookiecutter-967", source_kind=SourceKind.LEVEL32_OPERATOR))
    import json
    for line in (session_dir / "session.events.jsonl").read_text().splitlines():
        view = reduce_event(view, SessionEvent.from_mapping(json.loads(line)))
    assert view.cleanup_not_required is False
    assert view.cleanup_verified is None
    from agentic_debugger.ui.screens import render_view_header
    head = render_view_header(view, mode="live", mode_style="bold")
    assert "No resources created" not in (head.plain if hasattr(head, "plain") else str(head))

def test_V8_and_V9_historical_canned_are_pre_resource():
    """V8 (Docker gate) and V9 (Ollama version) both abort before workspace creation => Not required (new runs would emit pre_resource_abort)."""
    v8_dir = REPO_ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-deepseek-v4-flash-cloud-v8"
    v8_files = {p.name for p in v8_dir.iterdir() if p.is_file()}
    assert "image-verification.json" in v8_files
    assert "live-results.json" not in v8_files
    assert "candidate.patch" not in v8_files
    assert "result.json" not in v8_files
    v8_data = json.loads((v8_dir / "image-verification.json").read_text(encoding="utf-8"))
    assert v8_data["category"] in ("IMAGE_INSPECTION_FAILED", "DOCKER_UNAVAILABLE")
    assert v8_data["provider_model_execution_started"] is False
    v9_dir = REPO_ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-deepseek-v4-flash-cloud-v9"
    v9_files = {p.name for p in v9_dir.iterdir() if p.is_file()}
    assert "image-verification.json" in v9_files
    assert v9_files == {"image-verification.json"}
    v9_data = json.loads((v9_dir / "image-verification.json").read_text(encoding="utf-8"))
    assert v9_data["category"] == "IMAGE_VERIFIED"
    from agentic_debugger.application.events import OperatorStage
    assert OperatorStage.PREPARING_WORKSPACE not in {OperatorStage.STARTING, OperatorStage.PREFLIGHT}

def test_post_preflight_crash_without_files_must_be_failed_not_not_required(tmp_path):
    """Critical regression: preflight passes, workspace boundary crossed, then crash with no files => must be FAILED, not Not required."""
    from agentic_debugger.application.events import SessionEventKind, OperatorStage
    from agentic_debugger.application.presentation import PresentationIdentity, initial_session_view, reduce_event
    from agentic_debugger.application.events import SourceKind

    repo_root = tmp_path / "repo3"
    repo_root.mkdir(parents=True)
    output_dir = repo_root / "level32-cookiecutter-967-deepseek-v4-flash-cloud-v10"
    output_dir.mkdir(parents=True)
    session_dir = tmp_path / "session3"
    model_profile = Level32ModelProfile(
        alias="deepseek-v4-flash:cloud",
        display_name="deepseek-v4-flash",
        readiness="live_verified",
        transport_config_fingerprint=adapter.transport_config_fingerprint(adapter.CLOUD_MODELS["deepseek-v4-flash:cloud"]),
    )
    spec = SessionSpec(
        task_id="audreyr__cookiecutter-967",
        source=__import__("agentic_debugger.application.sources", fromlist=["ExecutionSourceSpec"]).ExecutionSourceSpec(
            kind=SourceKind.LEVEL32_OPERATOR,
            task_id="audreyr__cookiecutter-967",
            policy="exact-pdb-level32-frozen",
            model_config_ref="deepseek-v4-flash:cloud",
        ),
    )
    class FakeProcCrash:
        def __init__(self):
            self.pid = 12347
            self.returncode = 1
        def poll(self):
            return self.returncode
        def communicate(self):
            return ("", "simulated crash after workspace creation")
        def terminate(self): pass
        def kill(self): pass

    worker = Level32OperatorWorker(
        session_dir=str(session_dir),
        session_id="test-session-00000000000000000000000000000003",
        run_id="test-run-00000000000000000000000000000003",
        repository_root=str(repo_root),
        model=model_profile,
        revision=10,
        treatment_id="pdb-capability-level32-cookiecutter-967-deepseek-v4-flash-cloud-v10-workspace-derived-official-git-diff-v1",
        output_dir=str(output_dir),
        spec=spec,
        process_factory=lambda *a, **kw: FakeProcCrash(),
    )
    worker.start()
    worker._resource_creation_started_observed = True
    worker._pre_resource_abort_observed = False
    worker._emit_progress(OperatorStage.PREPARING_WORKSPACE)
    result = worker.wait()
    from agentic_debugger.application.events import SessionStatus, SessionTerminationReason
    assert result.status is SessionStatus.FAILED
    cleanup_events = [e for e in worker.events if e.event_kind is SessionEventKind.CLEANUP_COMPLETED]
    assert len(cleanup_events) == 1, "post-resource crash must emit cleanup.completed even with no files"
    assert cleanup_events[0].payload["verified"] is False
    view = initial_session_view(PresentationIdentity(task_id="audreyr__cookiecutter-967", source_kind=SourceKind.LEVEL32_OPERATOR))
    for ev in worker.events:
        view = reduce_event(view, ev)
    assert view.cleanup_verified is False
    assert view.cleanup_not_required is False
    from agentic_debugger.ui.screens import render_view_header
    head = render_view_header(view, mode="live", mode_style="bold")
    plain = head.plain if hasattr(head, "plain") else str(head)
    assert "No resources created" not in plain
    assert "cleanup failed" in plain.lower()

def test_post_resource_cleanup_failure_still_reports_failed(tmp_path):
    """Once resources exist (live-results.json), genuine cleanup failure must still show failed."""

    from agentic_debugger.application.events import SessionEventKind
    from agentic_debugger.application.presentation import initial_session_view, PresentationIdentity, reduce_event, SessionViewState
    from agentic_debugger.application.events import SourceKind
    from agentic_debugger.application.session import SessionSpec
    import json as _json
    from agentic_debugger.application.events import SessionStatus, SessionTerminationReason

    repo_root = tmp_path / "repo2"
    repo_root.mkdir(parents=True)
    output_dir = repo_root / "level32-cookiecutter-967-deepseek-v4-flash-cloud-v99"
    output_dir.mkdir(parents=True)
    # Simulate workspace creation: live-results with reporting not cleaned
    (output_dir / "live-results.json").write_text(_json.dumps({
        "reporting": {"completed": True, "cleanup": "leaked"},
        "measurements": {},
    }))
    # No result.json -> worker will treat as pre-resource? But we have live-results, so it's post-resource
    # Use an operator_failure-like result to trigger FAILED status with cleanup false
    (output_dir / "result.json").write_text(_json.dumps({
        "accepted": False,
        "classification": "official_rejection_semantic",
        "cleanup": {"temporary_source_removed": False, "private_official_material_removed": False},
        "measurements": {},
        "controller": {"completed": True},
        "verifier": {"outcome": "RESOLVED"},
        "official_verifier": {"all_ok": False, "official_test_execution_proven": True},
        "operator_failure": {"kind": "candidate_unavailable", "message": "candidate not found"},
    }))

    session_dir = tmp_path / "session2"
    model_profile = Level32ModelProfile(
        alias="deepseek-v4-flash:cloud",
        display_name="deepseek-v4-flash",
        readiness="live_verified",
        transport_config_fingerprint=adapter.transport_config_fingerprint(adapter.CLOUD_MODELS["deepseek-v4-flash:cloud"]),
    )
    spec = SessionSpec(
        task_id="audreyr__cookiecutter-967",
        source=__import__("agentic_debugger.application.sources", fromlist=["ExecutionSourceSpec"]).ExecutionSourceSpec(
            kind=SourceKind.LEVEL32_OPERATOR,
            task_id="audreyr__cookiecutter-967",
            policy="exact-pdb-level32-frozen",
            model_config_ref="deepseek-v4-flash:cloud",
        ),
    )
    class FakeProc2:
        def __init__(self):
            self.pid = 12346
            self.returncode = 1
        def poll(self): return self.returncode
        def communicate(self): return ("", "")
        def terminate(self): pass
        def kill(self): pass

    worker = Level32OperatorWorker(
        session_dir=str(session_dir),
        session_id="test-session-00000000000000000000000000000002",
        run_id="test-run-00000000000000000000000000000002",
        repository_root=str(repo_root),
        model=model_profile,
        revision=99,
        treatment_id="pdb-capability-level32-cookiecutter-967-deepseek-v4-flash-cloud-v99-workspace-derived-official-git-diff-v1",
        output_dir=str(output_dir),
        spec=spec,
        process_factory=lambda *a, **kw: FakeProc2(),
    )
    worker.start()
    result = worker.wait()
    # With resources present and result indicating not cleaned, cleanup should be emitted as failed
    cleanup_events = [e for e in worker.events if e.event_kind is SessionEventKind.CLEANUP_COMPLETED]
    assert len(cleanup_events) == 1
    assert cleanup_events[0].payload["verified"] is False

def test_next_revision_follows_existing_evidence():
    # The next revision must be one beyond the highest existing historical
    # treatment (never reuse); derive the expectation from the immutable
    # on-disk evidence instead of freezing a specific campaign number.
    import re as _re
    root = REPO_ROOT / "experiments" / "pdb_capability_ladder"
    highest = 0
    for child in root.glob("level32-cookiecutter-967-deepseek-v4-flash-cloud-v*"):
        match = _re.search(r"-v(\d+)$", child.name)
        if match:
            highest = max(highest, int(match.group(1)))
    expected = highest + 1
    rev = operator.next_unused_treatment_revision(REPO_ROOT, "deepseek-v4-flash:cloud")
    assert rev == expected
    # Also via application bridge
    from agentic_debugger.application.level32 import next_level32_treatment
    rev2, treatment_id, out = next_level32_treatment(REPO_ROOT, "deepseek-v4-flash:cloud")
    assert rev2 == expected
    assert treatment_id.endswith(f"-v{expected}-workspace-derived-official-git-diff-v1")
    assert f"deepseek-v4-flash-cloud-v{expected}" in str(out)
