"""Task 44 — Unbounded Session Progress v1 regressions.

Proves the repository-wide runtime rule against the REAL configured
Level-32 runtime (no monkeypatched controller/model boundary):

> Interactive/configured sessions do not have Agentic-Debugger-owned
> total model-request, directive, or controller-step execution ceilings.
> Progress counters are telemetry only.

Coverage:

* ``test_configured_level32_continues_past_old_25_request_ceiling`` —
  mandatory real regression: drives the real ``run_configured_session``
  for ``LEVEL32_TASK_ID`` with a fake provider subprocess through more
  than the old 25-request ceiling (>= 30 successful model requests).
  Fails against Candidate 51 (dies at request #25 via
  ``controller budget exhausted; directive exhausted``).
* ``test_configured_level32_continues_past_historical_40_step_ceiling`` —
  crosses the historical 40 controller-step ceiling (>= 41 valid
  progression steps, here 45) with the same real runtime.
* ``test_real_local_project_product_path_continues_past_32`` — REAL
  Local Project product path (``build_local_project_launch`` →
  SessionLaunch ModelBinding → ``run_local_project_session`` →
  ``ModelGateway.create_transport`` → LiveModelAdapter →
  DeterministicController → scripted loopback provider) driven past the
  historical Local Project 32-call default.  Fails on Candidate 52 with
  the SessionLaunch/transport fingerprint drift.

All tests mock the EXTERNAL provider response only (fake subprocess /
fake transport / loopback HTTP); the controller/model execution boundary
(``run_local_session``, ``run_local_project_session``,
``build_local_project_launch``, ``ModelGateway.create_transport``,
``DeterministicController.run``, ``LiveModelAdapter`` progression
authority) is never monkeypatched.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from agentic_debugger.application.configured_source import run_configured_session
from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.events import SessionEventKind, SourceKind
from agentic_debugger.application.journal import SessionEventJournal
from agentic_debugger.application.level32 import LEVEL32_TASK_ID
from agentic_debugger.application.level32_materialization import LEVEL32_INTERNAL_TASK_ID
from agentic_debugger.application.model_gateway import provider_runtime_identity
from agentic_debugger.application import provider_connections as pc
from agentic_debugger.application.sources import ModelExecutionError
from agentic_debugger.application.worker_scenarios import ScenarioContext
from agentic_debugger.cancellation import CancellationToken
from agentic_debugger.evaluation.live import LiveModelConfig

from test_level32_configured_runtime import (
    _seed_hermetic_level32_source,
    _setup_test_providers,
)
from test_provider_direct_api_session import _make_git_repo

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

LP_PROVIDER_ID = "commandcode_goat"
LP_MODEL_ID = "deepseek/deepseek-v4-flash"
LP_SECRET = "lp-unbounded-credential-not-real"
# Historical Local Project default (Task-26); generic execution is
# unbounded past it.
OLD_LOCAL_PROJECT_DEFAULT = 32
# UNDERSTAND self-loops after the opening REPRODUCE→UNDERSTAND step.
LP_CHURN_TARGET = 33

# Exact Candidate-51 historical ceilings (Task-42 configured Level-32).
OLD_LEVEL32_MODEL_REQUEST_CEILING = 25
OLD_LEVEL32_CONTROLLER_STEP_CEILING = 25
# Historical treatment/UI ceiling (LiveTreatmentBudget 40/STEP /40).
HISTORICAL_TREATMENT_STEP_CEILING = 40


def _hypothesis_churn(n_pairs: int, start: int = 1) -> list[dict[str, Any]]:
    """Build ``n_pairs`` add/support pairs staying within tool budgets.

    Every directive is legal in UNDERSTAND, consumes NO patch/test/pdb/
    source budget, and keeps at most one active hypothesis — so the run
    can continue indefinitely without tripping the retained per-action
    tool bounds.  Each pair is two model requests / two controller steps.
    """
    seq: list[dict[str, Any]] = []
    counter = start
    for _ in range(n_pairs):
        hid = f"h{counter}"
        seq.append(
            {
                "kind": "add_hypothesis",
                "hypothesis_id": hid,
                "statement": f"hypothesis {counter} observes shallow merge in get_config",
                "confidence": "low",
                "evidence_refs": [],
                "requires_runtime_evidence": False,
            }
        )
        seq.append(
            {
                "kind": "set_hypothesis_status",
                "hypothesis_id": hid,
                "status": "supported",
            }
        )
        counter += 1
    return seq


def _level32_directives(n_successful: int) -> list[dict[str, Any]]:
    """Scripted legal directives: 1 repro + 1 transition + hypothesis churn.

    ``n_successful`` counts the well-formed directives; one deliberately
    malformed terminal is appended so the run ends with the honest
    scripted controller failure AFTER crossing old ceilings (never via
    a count budget).
    """
    assert n_successful >= 3
    seq: list[dict[str, Any]] = [
        {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
        {"kind": "transition", "target_state": "Understand", "reason": "baseline reproduced failure"},
    ]
    remaining = n_successful - len(seq)
    n_pairs = remaining // 2
    seq.extend(_hypothesis_churn(n_pairs))
    if len(seq) < n_successful:
        hid = f"h-tail-{len(seq)}"
        seq.append(
            {
                "kind": "add_hypothesis",
                "hypothesis_id": hid,
                "statement": "tail hypothesis observes shallow merge in get_config",
                "confidence": "low",
                "evidence_refs": [],
                "requires_runtime_evidence": False,
            }
        )
    assert len(seq) == n_successful
    seq.append({"kind": "transition", "state": "FAILED"})
    return seq


def _run_level32_with_fake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directives: list[dict[str, Any]],
) -> dict[str, Any]:
    """Drive the REAL configured Level-32 runtime with a fake provider.

    Returns evidence: sizes, served count, journal kinds, terminal text,
    adapter metrics, provider provenance.
    """
    _setup_test_providers(tmp_path, monkeypatch)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nonexistent_localappdata"))
    hermetic_cache = tmp_path / "hermetic_cache" / "cookiecutter-967-base-source"
    _seed_hermetic_level32_source(hermetic_cache)
    monkeypatch.setattr(
        "agentic_debugger.application.level32_materialization.default_base_source_cache_dir",
        lambda: hermetic_cache,
    )

    sizes_path = tmp_path / "request_sizes.jsonl"
    counter_path = tmp_path / "model_calls.txt"
    counter_path.write_text("0", encoding="utf-8")

    fake_model_path = tmp_path / "fake_unbounded_model.py"
    fake_model_path.write_text(
        "from __future__ import annotations\n"
        "import json\n"
        "import sys\n"
        f"REPO_ROOT = {str(REPO_ROOT)!r}\n"
        f"SIZES = {str(sizes_path)!r}\n"
        f"COUNTER = {str(counter_path)!r}\n"
        f"DIRECTIVES = {json.dumps(directives)!r}\n"
        "raw = sys.stdin.buffer.readline()\n"
        "with open(SIZES, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(str(len(raw)) + chr(10))\n"
        "count = int(open(COUNTER, encoding='utf-8').read().strip() or '0')\n"
        "open(COUNTER, 'w', encoding='utf-8').write(str(count + 1))\n"
        "directives = json.loads(DIRECTIVES)\n"
        "content = json.dumps(directives[count] if count < len(directives) else directives[-1])\n"
        "response = {'provider_completion_schema_version': 'provider-completion-v1',\n"
        "    'directive_content': content,\n"
        "    'usage': {'prompt_tokens': 20, 'completion_tokens': 15, 'total_tokens': 35}}\n"
        "print(json.dumps(response))\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )

    import agentic_debugger.application.configured_source as cs

    c = pc.get_provider_config("commandcode_goat")
    authority = provider_runtime_identity(c)

    def wrapped_resolve(provider: str, model_id: str, **kwargs: Any):
        cfg = LiveModelConfig(
            model_name="unbounded-model",
            command=(sys.executable, str(fake_model_path)),
            request_timeout_seconds=60,
            tool_version="test-v1",
        )
        return (
            cfg,
            {
                "display_name": "Unbounded Model",
                "route": "direct_api",
                "api_protocol": "chat_completions",
                "provider_model_id": model_id,
                "endpoint": "https://api.commandcode.ai/provider/v1",
                "provider_runtime_identity": authority,
                "protocol_version": "1.3",
            },
            "f" * 64,
        )

    monkeypatch.setattr(cs, "_resolve_registry_model", wrapped_resolve)

    journal_path = tmp_path / "journal.events.jsonl"
    journal = SessionEventJournal(
        journal_path,
        session_id="sess-l32-unbounded",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    emitter = SessionEventEmitter(
        sink=journal,
        session_id="sess-l32-unbounded",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    ctx = ScenarioContext(
        work_dir=work_dir,
        emitter=emitter,
        token=CancellationToken(),
    )

    terminal = ""
    try:
        run_configured_session(
            ctx,
            {
                "provider": "commandcode_goat",
                "model_id": "muse/muse-spark-1.3",
                "policy": "pdb-on-uncertainty",
            },
        )
    except ModelExecutionError as exc:
        terminal = str(exc)
        # Re-raise shape is asserted by callers; capture metrics first.
        pass
    else:
        pytest.fail("scripted run should end with the honest malformed-directive failure")

    sizes = [
        int(line.strip())
        for line in sizes_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    served = int(counter_path.read_text(encoding="utf-8").strip())
    events = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    kinds = [e["event_kind"] for e in events]
    provenance = next(
        (e["payload"] for e in events if e["event_kind"] == SessionEventKind.MODEL_CONFIGURED.value),
        {},
    )
    return {
        "terminal": terminal,
        "sizes": sizes,
        "served": served,
        "kinds": kinds,
        "provenance": provenance,
        "journal_path": journal_path,
        "work_dir": work_dir,
    }


def test_configured_level32_continues_past_old_25_request_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mandatory Task-44 regression: real Level-32 runtime past request #25.

    Drives the REAL ``run_configured_session`` for ``LEVEL32_TASK_ID``
    with fake provider responses through 32 successful model requests
    (> old 25 ceiling).  Fails against Candidate 51 at request #25.
    """
    n_successful = 32
    directives = _level32_directives(n_successful)
    evidence = _run_level32_with_fake(tmp_path, monkeypatch, directives)

    # 1-3: execution continued beyond the old ceiling.
    assert len(evidence["sizes"]) > OLD_LEVEL32_MODEL_REQUEST_CEILING, (
        f"stopped at old ceiling: {evidence['sizes']}"
    )
    # Request #25 (index 24) and #26 (index 25) were actually sent.
    assert len(evidence["sizes"]) >= 26
    # 4-6: no count-derived terminal taxonomy in the new run.
    assert "directive exhausted" not in evidence["terminal"]
    assert "directive_exhausted" not in evidence["terminal"]
    assert "controller budget exhausted" not in evidence["terminal"]
    assert "model request limit" not in evidence["terminal"]
    assert "model_request_limit" not in evidence["terminal"]
    assert "model-request-budget" not in evidence["terminal"]
    # The scripted ending is the honest directive rejection, never a
    # provider/transport error (the Candidate-51 death shape).
    assert "directive_rejected" in evidence["terminal"]
    assert "provider_or_transport_error" not in evidence["terminal"]
    # 7: provider/model identity remains correct.
    assert evidence["provenance"].get("provider") == "commandcode_goat"
    assert evidence["provenance"].get("provider_model_id") == "muse/muse-spark-1.3"
    # 8: counters kept recording accurate totals (one transport per
    # logical request until the malformed tail; tail adds repair retries).
    assert evidence["served"] >= n_successful
    started = evidence["kinds"].count(SessionEventKind.MODEL_REQUEST_STARTED.value)
    completed = evidence["kinds"].count(SessionEventKind.MODEL_REQUEST_COMPLETED.value)
    assert started == completed
    assert started >= n_successful
    assert len(evidence["sizes"]) >= started
    assert SessionEventKind.MODEL_CONFIGURED.value in evidence["kinds"]
    staging_fixture = (
        evidence["work_dir"]
        / "level32_staging"
        / "agentic_debugger"
        / "datasets"
        / "curated"
        / LEVEL32_INTERNAL_TASK_ID
    )
    assert (staging_fixture / "cookiecutter" / "config.py").is_file()


def test_configured_level32_continues_past_historical_40_step_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task-44 second regression: real Level-32 runtime past step #40.

    The historical 40 controller-step ceiling (LiveTreatmentBudget 40 /
    STEP /40 UI) must not terminate generic execution: at least 41 valid
    progression steps remain live/progressing (here 45 successful model
    requests through the same real runtime).
    """
    n_successful = 45
    directives = _level32_directives(n_successful)
    evidence = _run_level32_with_fake(tmp_path, monkeypatch, directives)

    assert len(evidence["sizes"]) > HISTORICAL_TREATMENT_STEP_CEILING, (
        f"stopped at historical 40 ceiling: {len(evidence['sizes'])}"
    )
    assert len(evidence["sizes"]) >= 41
    assert "directive exhausted" not in evidence["terminal"]
    assert "controller budget exhausted" not in evidence["terminal"]
    assert "model request limit" not in evidence["terminal"]
    assert "model_request_limit" not in evidence["terminal"]
    assert "directive_rejected" in evidence["terminal"]
    started = evidence["kinds"].count(SessionEventKind.MODEL_REQUEST_STARTED.value)
    assert started >= 41
    assert evidence["served"] >= n_successful


class _ChurnChatServer:
    """Loopback /chat/completions fake serving UNDERSTAND churn.

    Mirrors the accepted ``_ScriptedChatServer`` responder contract
    (reads the bounded protocol context out of the prompt, answers one
    directive per POST) but keeps the controller in UNDERSTAND with
    budget-free self-loop transitions so the run can proceed past the
    historical 32-call default: REPRODUCE → UNDERSTAND once, then
    ``LP_CHURN_TARGET`` self-loops, then the honest scripted FAILED
    terminal (never a count budget).
    """

    def __init__(self, churn_target: int = LP_CHURN_TARGET) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.understand_calls = 0
        self.churn_target = churn_target
        self._lock = threading.Lock()

    def respond(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        prompt = payload["messages"][-1]["content"]
        from opencode_go_command_adapter import PUBLIC_REQUEST_END, PUBLIC_REQUEST_START

        between = prompt.split(PUBLIC_REQUEST_START, 1)[1].split(PUBLIC_REQUEST_END, 1)[0]
        context = json.loads(between.strip())
        state = context["controller"]["state"]
        if state == "Reproduce":
            content: Optional[str] = json.dumps(
                {
                    "kind": "transition",
                    "target_state": "Understand",
                    "reason": "baseline reproduced; continue reasoning",
                }
            )
        elif state == "Understand":
            with self._lock:
                self.understand_calls += 1
                churn = self.understand_calls
            if churn <= self.churn_target:
                content = json.dumps(
                    {
                        "kind": "transition",
                        "target_state": "Understand",
                        "reason": f"churn {churn} continue reasoning past old ceiling",
                    }
                )
            else:
                content = json.dumps(
                    {
                        "kind": "transition",
                        "target_state": "Failed",
                        "reason": "scripted end after unbounded proof",
                    }
                )
        else:
            content = json.dumps(
                {
                    "kind": "transition",
                    "target_state": "Failed",
                    "reason": "script exhausted",
                }
            )
        return 200, {
            "id": "chatcmpl-lp-unbounded",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        }

    @property
    def request_count(self) -> int:
        return len(self.calls)

    def __enter__(self) -> "_ChurnChatServer":
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


def _isolate_lp_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Hermetic provider/credential isolation mirroring the accepted
    direct-API session harness: no operator ambient state may satisfy or
    block the loopback provider."""
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
    monkeypatch.setattr(
        pc, "opencode_auth_store_path", lambda: tmp_path / "missing-auth.json"
    )
    _vault: dict[str, str] = {}
    monkeypatch.setattr(
        pc, "save_secure_credential", lambda k, v: _vault.__setitem__(k, v) or True
    )
    monkeypatch.setattr("agentic_debugger.application.provider_credentials.load_secure_credential", lambda k: _vault.get(k))
    monkeypatch.setattr("agentic_debugger.application.provider_connections.load_secure_credential", lambda k: _vault.get(k))
    monkeypatch.setattr("agentic_debugger.application.provider_credentials.has_secure_credential", lambda k: k in _vault)
    monkeypatch.setattr("agentic_debugger.application.provider_connections.has_secure_credential", lambda k: k in _vault)
    monkeypatch.setattr(
        pc, "delete_secure_credential", lambda k: _vault.pop(k, None) is not None
    )
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id=LP_PROVIDER_ID,
        transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT,
    )


def test_real_local_project_product_path_continues_past_32(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task-44 repair F1: REAL Local Project product path past request #32.

    ``build_local_project_launch`` → SessionLaunch ModelBinding →
    ``run_local_project_session`` → ``ModelGateway.create_transport`` →
    LiveModelAdapter → DeterministicController → scripted loopback
    provider.  None of ``run_local_project_session``,
    ``build_local_project_launch``, or ``ModelGateway.create_transport``
    is monkeypatched; only external provider execution is faked.

    Requires request #33 to be sent with no fingerprint drift, no
    ``logical_call_limit``, no ``model_request_limit``, and no
    controller budget exhaustion.  Fails on Candidate 52 with
    ``model profile unavailable: ... configuration fingerprint
    drifted`` before any model request.
    """
    from agentic_debugger.application.emitter import SessionEventEmitter
    from agentic_debugger.application.journal import SessionEventJournal
    from agentic_debugger.application.local_project import (
        cleanup_parent_tmpdir,
        create_isolated_worktree,
        validate_local_project,
    )
    from agentic_debugger.application.local_project_source import (
        run_local_project_session,
    )
    from agentic_debugger.application.model_providers import (
        resolve_provider_live_config,
    )
    from agentic_debugger.application.session import SessionBudgets
    from agentic_debugger.application.session_runtime import (
        ProjectRuntimeEnvironmentSpec,
        build_local_project_launch,
    )
    from agentic_debugger.application.worker_scenarios import ScenarioContext
    from agentic_debugger.cancellation import CancellationToken

    _isolate_lp_provider(monkeypatch, tmp_path)
    repo = _make_git_repo(tmp_path)
    validated = validate_local_project(str(repo), launch_cwd=tmp_path)
    wt = create_isolated_worktree(validated.repo_root, validated.head_commit)
    try:
        with _ChurnChatServer() as server:
            # Genuine endpoint repoint BEFORE any session credential, so
            # parent, worker, and adapter agree byte-for-byte.
            pc.update_provider_config(LP_PROVIDER_ID, base_url=server.base_url)
            monkeypatch.setattr(pc, "credential_source_for", lambda kind: "session_key")
            pc.set_session_key(LP_PROVIDER_ID, LP_SECRET)

            session_id = "sess-lp-unbounded-001"
            launch = build_local_project_launch(
                session_id=session_id,
                task_id="local-project-debug",
                policy="static-baseline",
                provider_id=LP_PROVIDER_ID,
                model_id=LP_MODEL_ID,
                profile_id=LP_MODEL_ID,
                launch_snapshot={"PATH": "/usr/bin"},
                project_spec=ProjectRuntimeEnvironmentSpec(),
                budgets=SessionBudgets(),
                config_root=str(tmp_path / "cfg"),
            )
            # The SessionLaunch binding already resolves under the
            # unbounded authority: its fingerprint equals the
            # ceiling-0 resolution, not the historical 32.
            live0, _ = resolve_provider_live_config(
                LP_PROVIDER_ID, LP_MODEL_ID, logical_call_ceiling=0
            )
            assert launch.model_binding is not None
            assert launch.model_binding.config_fingerprint == live0.configuration_fingerprint

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
            emitter.bind_run_id("run-lp-unbounded")
            emitter.emit(SessionEventKind.SESSION_STARTED, {})
            ctx = ScenarioContext(
                work_dir=tmp_path / "work",
                token=CancellationToken(),
                journal=journal,
                emitter=emitter,
                run_id="run-lp-unbounded",
                session_dir=journal_path.parent,
                session_launch=launch,
            )
            with pytest.raises(ModelExecutionError) as excinfo:
                run_local_project_session(
                    ctx,
                    {
                        "project_repo_path": str(repo),
                        "project_head": validated.head_commit,
                        "isolated_workspace": str(wt.isolated_path),
                        "bug_description": "add returns a - b instead of a + b",
                        "reproduction_command": "python repro.py",
                        "verification_command": "python -m pytest -q test_regression.py",
                        "parent_tmpdir": str(wt.parent_tmpdir),
                        "policy": "static-baseline",
                        "config_root": str(repo / "config"),
                        "profile_id": LP_MODEL_ID,
                        "provider": LP_PROVIDER_ID,
                        "model_id": LP_MODEL_ID,
                    },
                )
            terminal = str(excinfo.value)
            # Honest scripted FAILED ending — never a count/fingerprint gate.
            assert "fingerprint drifted" not in terminal
            assert "logical_call_limit" not in terminal
            assert "logical model call" not in terminal
            assert "model_request_limit" not in terminal
            assert "model request limit" not in terminal
            assert "budget exhausted" not in terminal
            assert "directive exhausted" not in terminal
            # Request #33 was actually sent (1 opening + 33 churn + terminal).
            assert server.request_count >= OLD_LOCAL_PROJECT_DEFAULT + 1, (
                f"stopped before request #33: {server.request_count}"
            )
            events = [
                json.loads(line)
                for line in journal_path.read_text(encoding="utf-8").splitlines()
            ]
            kinds = [e["event_kind"] for e in events]
            started = kinds.count(SessionEventKind.MODEL_REQUEST_STARTED.value)
            completed = kinds.count(SessionEventKind.MODEL_REQUEST_COMPLETED.value)
            assert started == completed
            assert started >= OLD_LOCAL_PROJECT_DEFAULT + 1
            provenance = next(
                e["payload"]
                for e in events
                if e["event_kind"] == SessionEventKind.MODEL_CONFIGURED.value
            )
            assert provenance["provider"] == LP_PROVIDER_ID
            assert provenance["route"] == "direct_api"
    finally:
        cleanup_parent_tmpdir(wt.parent_tmpdir, repo)
