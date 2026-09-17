"""End-to-end: a scripted model narrows a suite-wide repro and the run FIXES.

A Local Project session starts with ``python -m pytest -q`` over two test
files.  The scripted model revises the repro to the single failing file,
reproduces, patches, and the deterministic pipeline plus the independent
verifier (bound to the revised command) reach FIXED.  No provider, no
network: the model is a local scripted JSON-lines command profile.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentic_debugger.application.events import SourceKind
from agentic_debugger.application.journal import SessionEventJournal
from agentic_debugger.application.local_project import (
    cleanup_parent_tmpdir,
    create_isolated_worktree,
    validate_local_project,
)
from agentic_debugger.application.local_project_source import run_local_project_session
from agentic_debugger.application.observability import SessionEventEmitter
from agentic_debugger.application.session_runtime import (
    ProjectRuntimeEnvironmentSpec,
    spec_to_param,
)
from agentic_debugger.application.worker_scenarios import ScenarioContext
from agentic_debugger.cancellation import CancellationToken

CALC_BUG = "def add(a,b):\n    return a - b\n"
CALC_TEST = "from calc import add\ndef test_add():\n    assert add(1,2)==3\n"
EXTRA_TEST = "def test_extra():\n    assert True\n"
CALC_DIFF = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a,b):
-    return a - b
+    return a + b
"""
HYPOTHESIS = "add in calculator returns a - b instead of a + b"
SINGLE = "python -m pytest tests/test_calc.py -q"
# P2P must pass on the clean baseline, so verify is the PASSING file —
# never the failing repro (the regression gate requires baseline PASS).
VERIFY = "python -m pytest tests/test_extra.py -q"


def _run(cmd: list[str], cwd: Path) -> None:
    result = subprocess.run(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, f"{cmd} failed: {result.stderr}"


def _directives() -> list[dict[str, Any]]:
    return [
        {"kind": "action", "name": "revise_test_command",
         "arguments": {"which": "repro", "command": SINGLE}},
        {"kind": "action", "name": "run_reproduction",
         "arguments": {"phase": "baseline"}},
        {"kind": "transition", "target_state": "Understand", "reason": "failure reproduced"},
        {"kind": "action", "name": "express_root_cause_hypothesis",
         "arguments": {"hypothesis_id": "h1", "statement": HYPOTHESIS,
                       "target_file": "calc.py", "target_symbol": "add",
                       "confidence": "low"}},
        {"kind": "transition", "target_state": "Patch", "reason": "diagnosed"},
        {"kind": "action", "name": "apply_patch", "arguments": {"patch": CALC_DIFF}},
        {"kind": "transition", "target_state": "Validate", "reason": "patched"},
        {"kind": "action", "name": "run_reproduction",
         "arguments": {"phase": "post_patch"}},
        {"kind": "action", "name": "run_regression_tests", "arguments": {}},
        {"kind": "action", "name": "classify_outcome", "arguments": {}},
    ]


def test_revise_then_fixed_end_to_end(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "test@test.com"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    (repo / "calc.py").write_text(CALC_BUG, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(CALC_TEST, encoding="utf-8")
    (repo / "tests" / "test_extra.py").write_text(EXTRA_TEST, encoding="utf-8")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-m", "initial"], repo)
    validated = validate_local_project(str(repo), launch_cwd=tmp_path)
    wt = create_isolated_worktree(validated.repo_root, validated.head_commit)

    counter_path = tmp_path / "calls.txt"
    counter_path.write_text("0", encoding="utf-8")
    directives_path = tmp_path / "directives.json"
    directives_path.write_text(json.dumps(_directives()), encoding="utf-8")
    fake_model_path = tmp_path / "fake_revise_model.py"
    fake_model_path.write_text(
        "import json, sys\n"
        "counter = sys.argv[sys.argv.index('--counter') + 1]\n"
        "script = sys.argv[sys.argv.index('--script') + 1]\n"
        "sys.stdin.buffer.readline()\n"
        "directives = json.load(open(script, encoding='utf-8'))\n"
        "count = int(open(counter, encoding='utf-8').read().strip() or '0')\n"
        "open(counter, 'w', encoding='utf-8').write(str(count + 1))\n"
        "choice = directives[count] if count < len(directives) else directives[-1]\n"
        "sys.stdout.write(json.dumps(choice) + chr(10))\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )

    config_root = tmp_path / "lp-config"
    (config_root / "config").mkdir(parents=True, exist_ok=True)
    (config_root / "config" / "command-models.json").write_text(
        json.dumps({
            "schema_version": "command-models-v1",
            "profiles": [{
                "profile_id": "lp-revise-profile",
                "display_name": "LP revise fixture",
                "executable": sys.executable,
                "argv": [str(fake_model_path), "--counter", str(counter_path),
                         "--script", str(directives_path)],
                "request_timeout_seconds": 30,
            }],
        }),
        encoding="utf-8",
    )

    session_id = "sess-lp-revise-e2e"
    journal_path = tmp_path / "session" / "session.events.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal = SessionEventJournal(
        journal_path, session_id=session_id, task_id="local-project-debug",
        source_kind=SourceKind.LOCAL_PROJECT,
    )
    emitter = SessionEventEmitter(
        sink=journal, session_id=session_id, task_id="local-project-debug",
        source_kind=SourceKind.LOCAL_PROJECT,
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    ctx = ScenarioContext(work_dir=work_dir, emitter=emitter, token=CancellationToken())

    try:
        disposition = run_local_project_session(
            ctx,
            {
                "project_repo_path": str(repo),
                "project_head": validated.head_commit,
                "isolated_workspace": str(wt.isolated_path),
                "bug_description": "add returns a - b instead of a + b",
                "reproduction_command": "python -m pytest -q",
                "verification_command": VERIFY,
                "config_root": str(config_root),
                "profile_id": "lp-revise-profile",
                "policy": "pdb-on-uncertainty",
                "project_runtime_spec": spec_to_param(ProjectRuntimeEnvironmentSpec()),
            },
        )
    finally:
        try:
            cleanup_parent_tmpdir(wt.parent_tmpdir)
        except Exception:
            pass

    assert disposition == "FIXED"

    # The revision lineage is journal truth: a recorded diagnosis carries
    # the old → new repro commands (existing event kind, no schema change),
    # so history-reopen shows what actually ran.
    found = False
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event_kind") != "diagnosis.recorded":
            continue
        text = ((event.get("payload") or {}).get("text") or "")
        if "repro command revised" in text and SINGLE in text:
            found = True
            break
    assert found, "no journaled repro revision to the narrowed command"
