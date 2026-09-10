"""Task 43 — Level-32 provider-owned request size regression.

Reproduces the owner-observed defect shape against the real configured
Level-32 runtime (Candidate 48 Level-32: interactive, non-official,
CONFIGURED_MODEL, real Cookiecutter materialization, bound Python
interpreter, independent verifier):

* a real ``run_configured_session`` for ``LEVEL32_TASK_ID`` with the
  hermetic Cookiecutter source, the real ``DeterministicController``,
  and the real ``CancellableJsonlCommandTransport``;
* a deterministic fake model subprocess standing in for the provider.

The fake is NOT a monkeypatched model boundary: it reads the exact stdin
line the transport sent, records its byte size, and parses it through the
REAL production stdin seam (``commandcode_goat_adapter.read_request``) —
the seam that raised the owner-observed ``request exceeds the public
request ceiling`` (``request_too_large``) at Model Request #8 on
Candidate 48.  On Candidate 48 this test dies at the crossing request;
after Task 43 the complete request is accepted and execution continues.

Growth past the old 32,768-byte ceiling comes from real controller
behavior only: baseline reproduction, real Cookiecutter source
inspection (find/get_source_window), and real recorded hypotheses whose
observations accumulate in every subsequent request.  No external
provider is contacted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agentic_debugger.application.configured_source import run_configured_session
from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.events import SessionEventKind, SourceKind
from agentic_debugger.application.journal import SessionEventJournal
from agentic_debugger.application.level32 import LEVEL32_TASK_ID
from agentic_debugger.application.level32_materialization import (
    LEVEL32_INTERNAL_TASK_ID,
    LEVEL32_SOURCE_SHA256,
    materialize_level32_task,
    sha256_file,
)
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

REPO_ROOT = Path(__file__).resolve().parents[2]

# The exact Candidate-48 ceiling behind the owner-observed failure
# ("request exceeds the public request ceiling" via the CommandCode stdin
# seam, which reads at most ollama_adapter.MAX_PUBLIC_REQUEST_BYTES).
OLD_COMMANDCODE_REQUEST_CEILING = 32_768

_STATEMENT = ("nested configuration merge behavior requires recursive handling " * 60).strip()


def _growth_directives() -> list[dict[str, Any]]:
    """Scripted legal directives driving real Level-32 source inspection.

    Every entry is a legal directive for the state the real controller is
    in when it is served (REPRODUCE once, then UNDERSTAND source
    inspection/hypotheses).  The final entry is deliberately malformed so
    the run ends with the honest scripted controller failure — after the
    old ceiling has been crossed and execution has continued past it.
    """
    seq: list[dict[str, Any]] = [
        {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
        {"kind": "transition", "target_state": "Understand", "reason": "baseline reproduced failure"},
        {"kind": "action", "name": "find_function", "arguments": {"name": "get_config", "path": "cookiecutter/config.py"}},
        {"kind": "action", "name": "get_source_window", "arguments": {"path": "cookiecutter/config.py", "line": 54}},
        {"kind": "action", "name": "get_source_window", "arguments": {"path": "cookiecutter/config.py", "line": 112}},
        {"kind": "add_hypothesis", "hypothesis_id": "h1", "statement": _STATEMENT, "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": False},
        {"kind": "action", "name": "get_source_window", "arguments": {"path": "cookiecutter/config.py", "line": 1}},
        {"kind": "action", "name": "find_function", "arguments": {"name": "get_user_config", "path": "cookiecutter/config.py"}},
        {"kind": "action", "name": "get_source_window", "arguments": {"path": "cookiecutter/config.py", "line": 139}},
        {"kind": "add_hypothesis", "hypothesis_id": "h2", "statement": _STATEMENT, "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": False},
        {"kind": "action", "name": "get_source_window", "arguments": {"path": "cookiecutter/config.py", "line": 30}},
        {"kind": "action", "name": "find_function", "arguments": {"name": "_expand_path", "path": "cookiecutter/config.py"}},
        {"kind": "action", "name": "get_source_window", "arguments": {"path": "cookiecutter/config.py", "line": 80}},
        {"kind": "add_hypothesis", "hypothesis_id": "h3", "statement": _STATEMENT, "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": False},
        {"kind": "action", "name": "get_source_window", "arguments": {"path": "cookiecutter/config.py", "line": 160}},
        # Deliberately malformed terminal directive (mirrors the accepted
        # Level-32 harness): bounded directive repairs, then the honest
        # scripted controller failure.
        {"kind": "transition", "state": "FAILED"},
    ]
    return seq


def test_level32_growth_run_continues_past_old_request_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    directives = _growth_directives()
    assert len(_STATEMENT.encode("utf-8")) <= 4096

    fake_model_path = tmp_path / "fake_growth_model.py"
    fake_model_path.write_text(
        "from __future__ import annotations\n"
        "import io\n"
        "import json\n"
        "import sys\n"
        "import types\n"
        f"REPO_ROOT = {str(REPO_ROOT)!r}\n"
        f"SIZES = {str(sizes_path)!r}\n"
        f"COUNTER = {str(counter_path)!r}\n"
        f"DIRECTIVES = {json.dumps(directives)!r}\n"
        "sys.path.insert(0, REPO_ROOT)\n"
        "sys.path.insert(0, REPO_ROOT + '/scripts')\n"
        "raw = sys.stdin.buffer.readline()\n"
        "with open(SIZES, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(str(len(raw)) + chr(10))\n"
        "# Real production adapter seam: parses exactly like the deployed\n"
        "# CommandCode route.  On Candidate 48 this raises\n"
        "# request_too_large past the old ceiling; after Task 43 it accepts.\n"
        "from commandcode_goat_adapter import read_request as real_read_request\n"
        "stream = types.SimpleNamespace(buffer=io.BytesIO(raw))\n"
        "try:\n"
        "    real_read_request(stream)\n"
        "except Exception as exc:\n"
        "    sys.stderr.write(json.dumps({'schema_version': 'command-error-v1',\n"
        "        'kind': getattr(exc, 'kind', 'adapter_error'),\n"
        "        'message': str(exc)[:400].replace(chr(10), ' ')}) + chr(10))\n"
        "    sys.stderr.flush()\n"
        "    raise SystemExit(1)\n"
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
            model_name="growth-model",
            command=(sys.executable, str(fake_model_path)),
            request_timeout_seconds=60,
            tool_version="test-v1",
        )
        return (
            cfg,
            {
                "display_name": "Growth Model",
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
        session_id="sess-l32-growth",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    emitter = SessionEventEmitter(
        sink=journal,
        session_id="sess-l32-growth",
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

    with pytest.raises(ModelExecutionError, match="controller run ended without completion") as excinfo:
        run_configured_session(
            ctx,
            {
                "provider": "commandcode_goat",
                "model_id": "muse/muse-spark-1.3",
                "policy": "pdb-on-uncertainty",
            },
        )

    # The scripted ending must not be a size gate: no internal ceiling
    # wording may appear in the terminal failure, and the terminal
    # transport reason must be the scripted directive rejection — never a
    # provider/transport error (the Candidate-48 death shape).
    terminal = str(excinfo.value)
    assert "request_too_large" not in terminal
    assert "public request ceiling" not in terminal
    assert "public-evidence" not in terminal
    assert "provider_or_transport_error" not in terminal
    assert "directive_rejected" in terminal

    sizes = [
        int(line.strip())
        for line in sizes_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # Owner-observed shape: the run reaches (and passes) 8 model requests.
    assert len(sizes) >= 8, f"growth run stopped early: {sizes}"
    # The old threshold is crossed by real accumulated context.
    crossing = [i for i, size in enumerate(sizes) if size > OLD_COMMANDCODE_REQUEST_CEILING]
    assert crossing, f"no request crossed the old ceiling: max={max(sizes)}"
    # Execution CONTINUED past the old death point: more transport
    # invocations followed the first over-ceiling request.
    assert len(sizes) > crossing[0] + 1, (
        f"execution stopped at the old ceiling: {sizes}"
    )
    # The full scripted directive sequence was served (all 15 scripted
    # directives reached a parsed provider answer): on Candidate 48 the
    # run dies at the crossing request and never serves the tail.
    served = int(counter_path.read_text(encoding="utf-8").strip())
    assert served >= len(directives), f"only {served} scripted directives served"

    events = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    kinds = [e["event_kind"] for e in events]
    assert SessionEventKind.MODEL_CONFIGURED.value in kinds
    # Every transport invocation belongs to a logical model request: the
    # journal's logical request events never exceed the observed transport
    # invocations (directive-repair retries reuse the logical call), and
    # the owner-observed shape (8+ logical requests) is reached.
    started = kinds.count(SessionEventKind.MODEL_REQUEST_STARTED.value)
    completed = kinds.count(SessionEventKind.MODEL_REQUEST_COMPLETED.value)
    assert started == completed
    assert started >= 8
    assert len(sizes) >= started

    staging_fixture = (
        work_dir
        / "level32_staging"
        / "agentic_debugger"
        / "datasets"
        / "curated"
        / LEVEL32_INTERNAL_TASK_ID
    )
    assert (staging_fixture / "cookiecutter" / "config.py").is_file()
