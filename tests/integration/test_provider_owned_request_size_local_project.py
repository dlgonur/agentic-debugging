"""Task 43 — Local Project provider-owned request size regression.

The same runtime invariant as the Level-32 regression, verified for Local
Project Debug: a large intended model request must not be blocked by
Agentic Debugger before reaching its configured transport.

A real ``run_local_project_session`` drives a real local git fixture
through the real ``DeterministicController`` + ``LiveModelAdapter`` +
gateway-materialized ``CancellableJsonlCommandTransport``.  The
deterministic fake command model stands in for the provider: it reads the
exact stdin line the transport sent, records its byte size, and parses it
through the REAL production stdin seam
(``opencode_provider_adapter.read_request``) — the OpenCode-route seam
that raised ``request exceeds the public request ceiling``
(``request_too_large``) past its old 25,000-byte ceiling on Candidate 48.

Growth past the old ceiling comes from real controller behavior only
(bug description, source inspection, recorded diagnoses accumulating in
every subsequent request).  No external provider is contacted.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.events import SessionEventKind, SourceKind
from agentic_debugger.application.journal import SessionEventJournal
from agentic_debugger.application.local_project import (
    create_isolated_worktree,
    validate_local_project,
)
from agentic_debugger.application.local_project_source import run_local_project_session
from agentic_debugger.application.session_runtime import (
    ProjectRuntimeEnvironmentSpec,
    spec_to_param,
)
from agentic_debugger.application.sources import ModelExecutionError
from agentic_debugger.application.worker_scenarios import ScenarioContext
from agentic_debugger.cancellation import CancellationToken

REPO_ROOT = Path(__file__).resolve().parents[2]

# The exact Candidate-48 ceiling on the OpenCode-route stdin seam used
# here (opencode_provider_adapter reads at most
# frozen.MAX_PUBLIC_REQUEST_BYTES).
OLD_OPENCODE_REQUEST_CEILING = 25_000

_BUG_DESCRIPTION = ("The add function returns a - b instead of a + b. " * 80).strip()
_DIAGNOSIS = ("Root cause: the add function subtracts instead of adding. " * 55).strip()


def _run(cmd: list[str], cwd: Path) -> None:
    result = subprocess.run(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, f"{cmd} failed: {result.stderr}"


def _growth_directives() -> list[dict[str, Any]]:
    seq: list[dict[str, Any]] = [
        {"kind": "action", "name": "find_function", "arguments": {"name": "add", "path": "calculator.py"}},
        {"kind": "action", "name": "get_source_window", "arguments": {"path": "calculator.py", "line": 1}},
        {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": "h1", "statement": _DIAGNOSIS, "target_file": "calculator.py", "target_symbol": "add", "confidence": "low"}},
        {"kind": "action", "name": "get_source_window", "arguments": {"path": "test_calculator.py", "line": 1}},
        {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": "h2", "statement": _DIAGNOSIS, "target_file": "calculator.py", "target_symbol": "add", "confidence": "low"}},
        {"kind": "action", "name": "find_function", "arguments": {"name": "test_add", "path": "test_calculator.py"}},
        {"kind": "action", "name": "get_source_window", "arguments": {"path": "calculator.py", "line": 2}},
        {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": "h3", "statement": _DIAGNOSIS, "target_file": "calculator.py", "target_symbol": "add", "confidence": "low"}},
        {"kind": "action", "name": "get_source_window", "arguments": {"path": "test_calculator.py", "line": 2}},
        {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": "h4", "statement": _DIAGNOSIS, "target_file": "calculator.py", "target_symbol": "add", "confidence": "low"}},
        {"kind": "action", "name": "find_function", "arguments": {"name": "add", "path": "calculator.py"}},
        {"kind": "action", "name": "get_source_window", "arguments": {"path": "calculator.py", "line": 1}},
        {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": "h5", "statement": _DIAGNOSIS, "target_file": "calculator.py", "target_symbol": "add", "confidence": "low"}},
        # Deliberately malformed terminal directive: bounded directive
        # repairs, then the honest scripted controller failure.
        {"kind": "transition", "state": "FAILED"},
    ]
    return seq


def test_local_project_growth_run_continues_past_old_request_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert len(_BUG_DESCRIPTION.encode("utf-8")) <= 4096

    repo = tmp_path / "proj"
    repo.mkdir()
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "test@test.com"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    (repo / "calculator.py").write_text("def add(a,b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calculator.py").write_text(
        "from calculator import add\ndef test_add():\n    assert add(1,2)==3\n",
        encoding="utf-8",
    )
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-m", "initial"], repo)
    validated = validate_local_project(str(repo), launch_cwd=tmp_path)
    wt = create_isolated_worktree(validated.repo_root, validated.head_commit)

    sizes_path = tmp_path / "lp_request_sizes.txt"
    counter_path = tmp_path / "lp_model_calls.txt"
    counter_path.write_text("0", encoding="utf-8")
    directives = _growth_directives()

    fake_model_path = tmp_path / "fake_lp_model.py"
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
        "# OpenCode route.  On Candidate 48 this raises request_too_large\n"
        "# past the old ceiling; after Task 43 it accepts.\n"
        "from opencode_provider_adapter import read_request as real_read_request\n"
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
        "print(json.dumps({'directive': json.loads(content),\n"
        "    'usage': {'prompt_tokens': 20, 'completion_tokens': 15, 'total_tokens': 35}}))\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )

    config_root = tmp_path / "lp-config"
    (config_root / "config").mkdir(parents=True, exist_ok=True)
    (config_root / "config" / "command-models.json").write_text(
        json.dumps({
            "schema_version": "command-models-v1",
            "profiles": [{
                "profile_id": "lp-growth-profile",
                "display_name": "LP growth fixture",
                "executable": sys.executable,
                "argv": [str(fake_model_path)],
                "request_timeout_seconds": 30,
            }],
        }),
        encoding="utf-8",
    )

    journal_path = tmp_path / "lp-journal.events.jsonl"
    journal = SessionEventJournal(
        journal_path,
        session_id="sess-lp-growth",
        task_id="local-project-debug",
        source_kind=SourceKind.LOCAL_PROJECT,
    )
    emitter = SessionEventEmitter(
        sink=journal,
        session_id="sess-lp-growth",
        task_id="local-project-debug",
        source_kind=SourceKind.LOCAL_PROJECT,
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    ctx = ScenarioContext(
        work_dir=work_dir,
        emitter=emitter,
        token=CancellationToken(),
    )

    try:
        with pytest.raises(ModelExecutionError, match="controller run ended without completion") as excinfo:
            run_local_project_session(
                ctx,
                {
                    "project_repo_path": str(repo),
                    "project_head": validated.head_commit,
                    "isolated_workspace": str(wt.isolated_path),
                    "bug_description": _BUG_DESCRIPTION,
                    "config_root": str(config_root),
                    "profile_id": "lp-growth-profile",
                    "policy": "pdb-on-uncertainty",
                    "project_runtime_spec": spec_to_param(ProjectRuntimeEnvironmentSpec()),
                },
            )
    finally:
        from agentic_debugger.application.local_project import cleanup_parent_tmpdir

        try:
            cleanup_parent_tmpdir(wt.parent_tmpdir)
        except Exception:
            pass

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
    assert len(sizes) >= 8, f"growth run stopped early: {sizes}"
    crossing = [i for i, size in enumerate(sizes) if size > OLD_OPENCODE_REQUEST_CEILING]
    assert crossing, f"no request crossed the old ceiling: max={max(sizes)}"
    assert len(sizes) > crossing[0] + 1, (
        f"execution stopped at the old ceiling: {sizes}"
    )
    served = int(counter_path.read_text(encoding="utf-8").strip())
    assert served >= len(directives), f"only {served} scripted directives served"

    events = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    kinds = [e["event_kind"] for e in events]
    started = kinds.count(SessionEventKind.MODEL_REQUEST_STARTED.value)
    completed = kinds.count(SessionEventKind.MODEL_REQUEST_COMPLETED.value)
    assert started == completed
    assert started >= 8
    assert len(sizes) >= started
