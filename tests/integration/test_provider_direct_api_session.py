"""End-to-end proof: one general session executes through the provider
direct-API route.

The real Local Project scenario runs in-process against the real
transport (``CancellableJsonlCommandTransport``), the real
``provider_direct_api_adapter.py`` subprocess, the real protocol
parsing, controller, and verifier — with ONLY the provider endpoint
faked by a local scripted HTTP server (no real provider contact, no
generation spend).  The registry is wrapped solely to point the
adapter's evaluation-only ``--base-url`` at the fake endpoint.

Also proves: the worker's fail-closed behavior when a provider has no
usable credential source (no network attempt is possible).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui_support import run_headless  # noqa: E402

from agentic_debugger.application import model_providers as mp  # noqa: E402
from agentic_debugger.application import provider_connections as pc  # noqa: E402
from agentic_debugger.application.events import (  # noqa: E402
    SessionEventKind,
    SourceKind,
    validate_session_event_stream,
)
from agentic_debugger.application.history import HistoryStore  # noqa: E402
from agentic_debugger.application.journal import (  # noqa: E402
    JournalReadState,
    SessionEventJournal,
    read_session_journal,
)
from agentic_debugger.application.emitter import SessionEventEmitter  # noqa: E402
from agentic_debugger.application.local_project import (  # noqa: E402
    create_isolated_worktree,
    validate_local_project,
)
from agentic_debugger.application.local_project_source import (  # noqa: E402
    run_local_project_session,
)
from agentic_debugger.application.worker import ScenarioContext  # noqa: E402
from agentic_debugger.application.worker_process import (  # noqa: E402
    SessionWorkerProcess,
)
from agentic_debugger.application.session import (  # noqa: E402
    SessionBudgets,
    SessionSpec,
)
from agentic_debugger.application.sources import ExecutionSourceSpec  # noqa: E402
from agentic_debugger.cancellation import CancellationToken  # noqa: E402
from agentic_debugger.ui.app import LocalApplicationV1  # noqa: E402

SECRET = "e2e-session-credential-not-real"

_PATCH = (
    "--- a/calculator.py\n"
    "+++ b/calculator.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a,b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)


@pytest.fixture(autouse=True)
def _hermetic_provider_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Normal integration tests never observe operator auth or env state."""

    pc.clear_all_session_keys()
    isolated_home = tmp_path / "operator-state-hidden"
    isolated_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("USERPROFILE", str(isolated_home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    for name in (
        "OPENCODE_API_KEY",
        "COMMAND_CODE_API_KEY",
        "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    pc.clear_all_session_keys()


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    def _run(argv: List[str]) -> None:
        subprocess.run(argv, cwd=str(repo), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (repo / "calculator.py").write_text("def add(a,b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calculator.py").write_text(
        "from calculator import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    # Pass-to-pass witness: green on the buggy baseline AND after the fix
    # (add(x, 0) == x for both the buggy and the fixed implementation).
    (repo / "test_regression.py").write_text(
        "from calculator import add\n\ndef test_identity():\n    assert add(1, 0) == 1\n",
        encoding="utf-8",
    )
    (repo / "repro.py").write_text(
        "from calculator import add\nimport sys\nprint(add(1, 2))\n"
        "sys.exit(0 if add(1, 2) == 3 else 1)\n",
        encoding="utf-8",
    )
    _run(["git", "init"])
    _run(["git", "config", "user.email", "t@example.test"])
    _run(["git", "config", "user.name", "t"])
    _run(["git", "add", "."])
    _run(["git", "commit", "-m", "initial"])
    return repo


class _ScriptedChatServer:
    """State-driven fake of a CommandCode /chat/completions endpoint.

    Each request carries the bounded protocol context; the responder
    selects the next legal directive from the controller state the same
    way the accepted deterministic demo fixtures do.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.validate_calls = 0
        self.patch_calls = 0
        self._lock = threading.Lock()

    def respond(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        prompt = payload["messages"][-1]["content"]
        from opencode_go_command_adapter import PUBLIC_REQUEST_END, PUBLIC_REQUEST_START

        between = prompt.split(PUBLIC_REQUEST_START, 1)[1].split(PUBLIC_REQUEST_END, 1)[0]
        context = json.loads(between.strip())
        state = context["controller"]["state"]
        content: Optional[str] = None
        if state == "Reproduce":
            content = json.dumps(
                {
                    "kind": "transition",
                    "target_state": "Understand",
                    "reason": "baseline reproduced; inspect and repair",
                }
            )
        elif state == "Understand":
            content = json.dumps(
                {
                    "kind": "transition",
                    "target_state": "Patch",
                    "reason": "bug localized from the description: sub instead of add",
                }
            )
        elif state == "Patch":
            with self._lock:
                self.patch_calls += 1
                patch_call = self.patch_calls
            if patch_call == 1:
                content = json.dumps(
                    {
                        "kind": "action",
                        "name": "apply_patch",
                        "arguments": {"patch": _PATCH},
                    }
                )
            else:
                content = json.dumps(
                    {
                        "kind": "transition",
                        "target_state": "Validate",
                        "reason": "candidate applied; validate against the repro",
                    }
                )
        elif state == "Validate":
            with self._lock:
                self.validate_calls += 1
                validate_call = self.validate_calls
            if validate_call == 1:
                content = json.dumps(
                    {
                        "kind": "action",
                        "name": "run_reproduction",
                        "arguments": {"phase": "post_patch"},
                    }
                )
            elif validate_call == 2:
                content = json.dumps(
                    {"kind": "action", "name": "run_regression_tests", "arguments": {}}
                )
            else:
                content = json.dumps(
                    {
                        "kind": "action",
                        "name": "classify_outcome",
                        "arguments": {},
                    }
                )
        if content is None:
            content = json.dumps(
                {
                    "kind": "transition",
                    "target_state": "Failed",
                    "reason": "script exhausted",
                }
            )
        return 200, {
            "id": "chatcmpl-e2e",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        }

    @property
    def request_count(self) -> int:
        return len(self.calls)

    def __enter__(self) -> "_ScriptedChatServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                with outer._lock:
                    outer.calls.append(
                        {
                            "path": self.path,
                            "authorization": self.headers.get("Authorization"),
                            "payload": body,
                        }
                    )
                status, response = outer.respond(body)
                encoded = json.dumps(response).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: Any) -> None:  # silence
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server = server
        self.base_url = f"http://127.0.0.1:{server.server_port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()


def _wrap_registry_for_fake_endpoint(monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    """Redirect ONLY the adapter's evaluation-only endpoint flag.

    Everything else (route decision, transport construction, provenance)
    stays the production path.
    """
    real_resolve = mp.resolve_provider_live_config

    def wrapped(kind, model_id, **kwargs):
        config, provenance = real_resolve(kind, model_id, **kwargs)
        command = list(config.command) + [
            "--base-url",
            base_url,
            "--engine",
            "stdlib",
        ]
        return (
            type(config)(
                model_name=config.model_name,
                command=tuple(command),
                request_timeout_seconds=config.request_timeout_seconds,
                tool_version=config.tool_version,
            ),
            provenance,
        )

    monkeypatch.setattr(mp, "resolve_provider_live_config", wrapped)


def _local_params(
    repo: Path,
    head: str,
    isolated: Path,
    parent_tmpdir: Path,
    *,
    provider: str = "commandcode_goat",
    model_id: str = "deepseek/deepseek-v4-flash",
) -> dict:
    return {
        "project_repo_path": str(repo),
        "project_head": head,
        "isolated_workspace": str(isolated),
        "bug_description": "add returns a - b instead of a + b",
        "reproduction_command": "python repro.py",
        "verification_command": "python -m pytest -q test_regression.py",
        "parent_tmpdir": str(parent_tmpdir),
        "policy": "static-baseline",
        "config_root": str(repo / "config"),
        "profile_id": model_id,
        "provider": provider,
        "model_id": model_id,
    }


def _install_worker_fake_endpoint(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """Keep the production worker boundary while adding the adapter's
    evaluation-only local endpoint flags inside that fresh process."""

    project_root = str(REPO_ROOT).replace("\\", "/")
    wrapped_source = """
def _wrapped(kind, model_id, protocol, *, logical_call_ceiling, request_timeout_seconds):
    config, provenance = _real(
        kind,
        model_id,
        protocol,
        logical_call_ceiling=logical_call_ceiling,
        request_timeout_seconds=request_timeout_seconds,
    )
    command = tuple(config.command) + ("--base-url", _base_url, "--engine", "stdlib")
    return type(config)(
        model_name=config.model_name,
        command=command,
        request_timeout_seconds=config.request_timeout_seconds,
        tool_version=config.tool_version,
    ), provenance
"""

    def worker_argv(_self: SessionWorkerProcess) -> List[str]:
        bootstrap = (
            "import runpy, sys; "
            f"sys.path.insert(0, {project_root!r}); "
            "from agentic_debugger.application import model_providers as _mp; "
            "_real = _mp._direct_api_live_config; "
            f"_base_url = {base_url!r}; "
            f"exec({wrapped_source!r}); "
            "_mp._direct_api_live_config = _wrapped; "
            "runpy.run_module('agentic_debugger.application.worker', run_name='__main__')"
        )
        return [sys.executable, "-I", "-u", "-c", bootstrap]

    monkeypatch.setattr(SessionWorkerProcess, "_worker_argv", worker_argv)


def test_commandcode_local_project_session_executes_through_direct_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pc.clear_all_session_keys()
    repo = _make_git_repo(tmp_path)
    validated = validate_local_project(str(repo), launch_cwd=tmp_path)
    wt = create_isolated_worktree(validated.repo_root, validated.head_commit)
    try:
        with _ScriptedChatServer() as server:
            _wrap_registry_for_fake_endpoint(monkeypatch, server.base_url)
            monkeypatch.setattr(
                pc, "credential_source_for", lambda kind: "session_key"
            )
            pc.set_session_key("commandcode_goat", SECRET)
            session_id = "sess-e2e-direct-api-001"
            journal_path = tmp_path / "session" / "session.events.jsonl"
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            journal = SessionEventJournal(
                journal_path,
                session_id=session_id,
                task_id="local-project-debug",
                source_kind=SourceKind.LOCAL_PROJECT,
            )
            emitter = SessionEventEmitter(
                session_id=session_id,
                task_id="local-project-debug",
                source_kind=SourceKind.LOCAL_PROJECT,
                sink=journal,
            )
            emitter.emit(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64})
            emitter.bind_run_id("run-e2e")
            emitter.emit(SessionEventKind.SESSION_STARTED, {})
            work_dir = tmp_path / "work"
            ctx = ScenarioContext(
                work_dir=work_dir,
                token=CancellationToken(),
                journal=journal,
                emitter=emitter,
                run_id="run-e2e",
                session_dir=journal_path.parent,
            )
            disposition = run_local_project_session(
                ctx, _local_params(repo, validated.head_commit, wt.isolated_path, wt.parent_tmpdir)
            )
            # The worker emits the durable terminal cycle after the
            # scenario returns; mirror that contract for a complete journal.
            emitter.emit(SessionEventKind.CLEANUP_STARTED, {})
            emitter.emit(
                SessionEventKind.CLEANUP_COMPLETED,
                {"verified": True},
            )
            emitter.emit(
                SessionEventKind.SESSION_COMPLETED,
                {
                    "status": "succeeded",
                    "termination_reason": "done",
                },
            )

        # The typed disposition is honest and the scripted candidate was
        # applied and verified.
        assert disposition == "FIXED"

        # The direct-API route served the session: every inference was one
        # POST to the fake provider's chat/completions endpoint.
        assert server.request_count >= 6
        for record in server.calls:
            assert record["path"] == "/chat/completions"
            assert record["authorization"] == f"Bearer {SECRET}"

        # Durable provenance distinguishes provider, model, route, and
        # protocol — credential-free.
        read = read_session_journal(journal_path)
        assert read.state is JournalReadState.COMPLETE
        validate_session_event_stream(read.events)
        provenance = next(
            event.payload
            for event in read.events
            if event.event_kind is SessionEventKind.MODEL_CONFIGURED
        )
        assert provenance["provider"] == "commandcode_goat"
        assert provenance["profile_id"] == "deepseek/deepseek-v4-flash"
        assert provenance["route"] == "direct_api"
        assert provenance["api_protocol"] == "chat_completions"
        assert provenance["provider_model_id"] == "deepseek/deepseek-v4-flash"
        assert provenance["endpoint"] == "https://api.commandcode.ai/provider/v1"
        assert SECRET not in json.dumps(provenance)
        assert SECRET not in journal_path.read_text(encoding="utf-8")

        # The verifier independently evaluated the candidate.
        verifier = next(
            event.payload
            for event in read.events
            if event.event_kind is SessionEventKind.VERIFIER_COMPLETED
        )
        assert verifier["status"] == "COMPLETED"
    finally:
        from agentic_debugger.application.local_project import cleanup_parent_tmpdir

        cleanup_parent_tmpdir(wt.parent_tmpdir, repo)


def test_opencode_ui_app_worker_session_key_reaches_direct_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full memory-only hop: app store -> worker environment -> worker route
    -> minimal adapter environment -> Authorization header."""

    repo = _make_git_repo(tmp_path)
    app = LocalApplicationV1(history_store=HistoryStore(tmp_path / "history"))
    pc.set_session_key("opencode_go", SECRET)
    captured: dict[str, Any] = {}
    with _ScriptedChatServer() as server:
        _install_worker_fake_endpoint(monkeypatch, server.base_url)

        async def actions(pilot):
            pilot.app.start_local_project_session(
                project_path=str(repo),
                bug_description="add returns a - b instead of a + b",
                reproduction_command="python repro.py",
                verification_command="python -m pytest -q test_regression.py",
                profile_id="opencode-go/deepseek-v4-flash",
                model_provider="opencode_go",
                max_elapsed_seconds=120,
            )
            runner = pilot.app.live_runner
            assert runner is not None
            captured["runner"] = runner
            assert runner.worker._child_environment == {
                "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY": SECRET
            }
            assert SECRET not in " ".join(runner.worker._worker_argv())

            deadline = time.monotonic() + 120
            while runner.terminal is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert runner.terminal is not None
            assert runner.terminal.status.value == "succeeded"

        run_headless(app, actions, size=(120, 32))
        runner = captured["runner"]

        assert server.request_count >= 6
        for record in server.calls:
            assert record["path"] == "/chat/completions"
            assert record["authorization"] == f"Bearer {SECRET}"

    journal_path = runner.worker.journal_path
    read = read_session_journal(journal_path)
    assert read.state is JournalReadState.COMPLETE
    validate_session_event_stream(read.events)
    provenance = next(
        event.payload
        for event in read.events
        if event.event_kind is SessionEventKind.MODEL_CONFIGURED
    )
    assert provenance["provider"] == "opencode_go"
    assert provenance["profile_id"] == "opencode-go/deepseek-v4-flash"
    assert provenance["route"] == "direct_api"
    assert provenance["api_protocol"] == "chat_completions"
    assert provenance["provider_model_id"] == "deepseek-v4-flash"
    assert SECRET not in json.dumps(provenance)
    assert SECRET not in journal_path.read_text(encoding="utf-8")


def test_worker_fails_closed_without_credential_and_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider connection with no usable credential source fails
    closed inside the worker — before any executable launch, with no
    way to reach a network endpoint."""
    repo = _make_git_repo(tmp_path)
    validated = validate_local_project(str(repo), launch_cwd=tmp_path)
    wt = create_isolated_worktree(validated.repo_root, validated.head_commit)
    store = HistoryStore(tmp_path / "hist")
    try:
        monkeypatch.delenv("COMMAND_CODE_API_KEY", raising=False)
        spec = SessionSpec(
            task_id="local-project-debug",
            source=ExecutionSourceSpec(
                kind=SourceKind.LOCAL_PROJECT,
                task_id="local-project-debug",
                model_config_ref="commandcode_goat:deepseek/deepseek-v4-flash",
            ),
            budgets=SessionBudgets(),
        )
        worker = SessionWorkerProcess(
            session_dir=store.session_dir("sess-e2e-nocred-001"),
            session_id="sess-e2e-nocred-001",
            spec=spec,
            run_id="run-nocred",
            scenario="local_project",
            scenario_params=_local_params(
                repo, validated.head_commit, wt.isolated_path, wt.parent_tmpdir
            ),
            cooperative_grace_seconds=10,
            ready_timeout_seconds=60,
        )
        try:
            assert worker.start() is None
            result = worker.wait()
            assert result.status.value == "failed"
            assert result.termination_reason is not None
            read = read_session_journal(
                store.session_dir("sess-e2e-nocred-001") / "session.events.jsonl"
            )
            kinds = [event.event_kind for event in read.events]
            assert SessionEventKind.MODEL_CONFIGURED not in kinds
            diagnostics = " ".join(result.diagnostics or [])
            assert "credential" in diagnostics.lower()
        finally:
            worker.close()
    finally:
        from agentic_debugger.application.local_project import cleanup_parent_tmpdir

        cleanup_parent_tmpdir(wt.parent_tmpdir, repo)
