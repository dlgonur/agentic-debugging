"""V2-01 real product-path execution-environment acceptance tests.

Exercises the product machinery itself — a real PDB worker spawn and a full
Local Project session through the real worker/source/controller/verifier
boundary with a fully local scripted model — and proves there that a
synthetic private provider session credential present in the worker process
never reaches a project command child, a product PDB target, or a verifier
command child, while benign project ambient variables keep working through
the LEGACY PROJECT AMBIENT bridge.

No external provider call; no real API key; synthetic values only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_debugger.application.events import SourceKind
from agentic_debugger.application.execution_environment import (
    ExecutionEnvironment,
    ExecutionRole,
)
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.journal import read_session_journal
from agentic_debugger.application.local_project import (
    cleanup_parent_tmpdir,
    create_isolated_worktree,
    validate_local_project,
)
from agentic_debugger.application.session import SessionBudgets, SessionSpec
from agentic_debugger.application.sources import ExecutionSourceSpec
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.workspace import TaskWorkspace

LOCAL_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "command_models" / "local_project_dummy.py"
)

SYNTHETIC_HOP_VAR = "AGENTIC_DEBUGGER_PROVIDER_T01_API_KEY"
SYNTHETIC_HOP_VALUE = "sk-synthetic-v201-hop-value-not-a-real-credential"
BENIGN_PROJECT_VAR = "V2_01_BENIGN_PROJECT_DSN"
BENIGN_PROJECT_VALUE = "service://synthetic/test-dsn"

GOOD_PATCH = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(a,b):
-    return a - b
+    return a + b
"""


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, f"{cmd} failed: {result.stderr}"
    return result.stdout.strip()


def _make_git_fixture(tmp_path: Path, name: str = "proj") -> Path:
    repo = tmp_path / name
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
    return repo


def _write_local_profile(root: Path, profile_id: str, patch_path: Path) -> None:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir = root / f"state-{profile_id}"
    data_file = root / f"data-{profile_id}.json"
    data_file.write_text(json.dumps({
        "symbol": "add",
        "file": "calculator.py",
        "hypothesis_id": "h1",
        "statement": "add returns a - b instead of a + b",
        "patch_file": str(patch_path),
        "expressions": [],
    }), encoding="utf-8")
    (config_dir / "command-models.json").write_text(json.dumps({
        "schema_version": "command-models-v1",
        "profiles": [{
            "profile_id": profile_id,
            "display_name": "V2-01 env fixture",
            "executable": sys.executable,
            "argv": [str(LOCAL_FIXTURE), "--state-dir", str(state_dir), "--data", str(data_file)],
            "request_timeout_seconds": 10,
        }],
    }), encoding="utf-8")


def _seed_session_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic provider session hop + benign project ambient state.

    The real worker process is spawned from this process, so the worker's
    product ambient environment contains both — exactly the active V2-01
    defect geometry."""
    monkeypatch.setenv(SYNTHETIC_HOP_VAR, SYNTHETIC_HOP_VALUE)
    monkeypatch.setenv(BENIGN_PROJECT_VAR, BENIGN_PROJECT_VALUE)


# ---------------------------------------------------------------------------
# Real product PDB spawn: the debug target cannot observe the credential
# ---------------------------------------------------------------------------

def test_product_pdb_target_cannot_observe_provider_credential(tmp_path, monkeypatch):
    _seed_session_environment(monkeypatch)
    authority = ExecutionEnvironment.snapshot_process()
    src = tmp_path / "src"
    src.mkdir()
    (src / "target.py").write_text(
        "import os\n"
        "leaked = ('" + SYNTHETIC_HOP_VAR + "' in os.environ)\n"
        "benign = ('" + BENIGN_PROJECT_VAR + "' in os.environ)\n"
        "with open('env_facts.txt', 'w', encoding='ascii') as handle:\n"
        "    handle.write('%s|%s' % (leaked, benign))\n"
        "x = 1\n"
        "y = 2\n",
        encoding="utf-8",
    )
    with TaskWorkspace(str(src)) as workspace:
        session = PdbSession(
            workspace,
            startup_timeout=20.0,
            request_timeout=30.0,
            worker_environment=dict(authority.role_environment(ExecutionRole.PRODUCT_PDB)),
        )
        try:
            session.start()
            started = session.start_paused_target("target.py", [6])
            assert started.get("state") == "paused"
            facts = (Path(workspace.root) / "env_facts.txt").read_text(encoding="ascii")
            assert facts == "False|True", facts
        finally:
            session.stop()


# ---------------------------------------------------------------------------
# Full Local Project session through the real worker machinery
# ---------------------------------------------------------------------------

def test_local_project_worker_children_have_clean_environment(tmp_path, monkeypatch):
    _seed_session_environment(monkeypatch)
    repo = _make_git_fixture(tmp_path, "proj")
    patch_path = tmp_path / "good.patch"
    patch_path.write_text(GOOD_PATCH, encoding="utf-8")
    store_root = tmp_path / "history"
    store_root.mkdir()
    store = HistoryStore(store_root)
    _write_local_profile(store_root, "v201-env-profile", patch_path)

    validated = validate_local_project(str(repo), launch_cwd=tmp_path)
    wt = create_isolated_worktree(validated.repo_root, validated.head_commit)
    repro = (
        'python -c "import os, sys; '
        f"env_clean = ('{SYNTHETIC_HOP_VAR}' not in os.environ) and ('{BENIGN_PROJECT_VAR}' in os.environ); "
        'from calculator import add; '
        'sys.exit(0 if (env_clean and add(1,2)==3) else (3 if not env_clean else 1))"'
    )
    verify = (
        'python -c "import os, sys; '
        f"env_clean = ('{SYNTHETIC_HOP_VAR}' not in os.environ) and ('{BENIGN_PROJECT_VAR}' in os.environ); "
        'from calculator import add; '
        'sys.exit(0 if (env_clean and add(0,0)==0) else (3 if not env_clean else 1))"'
    )
    session_id = "sess-v201-execution-environment"
    from agentic_debugger.application.local_project import LocalProjectTaskSpec
    from agentic_debugger.application.worker_process import SessionWorkerProcess

    worker = SessionWorkerProcess(
        session_dir=store.session_dir(session_id),
        session_id=session_id,
        spec=SessionSpec(
            task_id="local-project-debug",
            source=ExecutionSourceSpec(
                kind=SourceKind.LOCAL_PROJECT,
                task_id="local-project-debug",
                model_config_ref="v201-env-profile",
            ),
        ),
        run_id=f"run-{session_id}",
        scenario="local_project",
        scenario_params={
            "project_repo_path": str(repo),
            "project_head": validated.head_commit,
            "isolated_workspace": str(wt.isolated_path),
            "bug_description": "add returns a - b",
            "reproduction_command": repro,
            "verification_command": verify,
            "config_root": str(store.root),
            "profile_id": "v201-env-profile",
            "expected_fingerprint": None,
            "parent_tmpdir": str(wt.parent_tmpdir),
            "policy": "pdb-on-uncertainty",
        },
        cooperative_grace_seconds=5.0,
        ready_timeout_seconds=30.0,
        max_elapsed_seconds=180,
    )
    # Mirror the production pre-write (ui/app.py): the canonical
    # LocalProjectTaskSpec artifact must exist before the worker starts.
    worker.session_dir.mkdir(parents=True, exist_ok=True)
    local_spec = LocalProjectTaskSpec(
        session_id=session_id,
        source_repo_path=str(repo),
        source_head_commit=validated.head_commit,
        isolated_workspace_path=str(wt.isolated_path),
        bug_description="add returns a - b",
        reproduction_command=repro,
        verification_command=verify,
        model_runtime="v201-env-profile",
        budgets=SessionBudgets(max_elapsed_seconds=180),
        created_at_utc="2026-09-03T00:00:00Z",
    )
    (worker.session_dir / "local_project_task.json").write_text(
        json.dumps(local_spec.to_mapping(), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    try:
        assert worker.start() is None
        result = worker.wait()
        # FIXED requires the independent verifier to have re-executed the
        # reproduction and regression commands itself (four command
        # children across two workspaces) with clean environments: any
        # child observing the synthetic provider hop exits 3, which breaks
        # the F2P/P2P evidence and the session cannot be FIXED.  The FIXED
        # disposition surfaces as the worker terminal succeeded/done.
        assert result.status.value == "succeeded", getattr(result, "detail", None)
        assert result.termination_reason.value == "done"

        journal = read_session_journal(
            store.session_dir(session_id) / "session.events.jsonl"
        )
        # The initial reproduction child ran on the buggy baseline with a
        # clean environment: the honest failure exit (1), not the leak
        # marker (3).
        diagnosis_texts = [
            str(event.payload.get("text", ""))
            for event in journal.events
            if event.event_kind.value == "diagnosis.recorded"
        ]
        assert any("reproduction result exit 1" in text for text in diagnosis_texts)
        assert not any("reproduction result exit 3" in text for text in diagnosis_texts)
        # The synthetic credential value itself must never appear anywhere
        # in the durable journal.
        journal_text = "\n".join(
            json.dumps(event.payload, sort_keys=True, default=str)
            for event in journal.events
        )
        assert SYNTHETIC_HOP_VALUE not in journal_text
    finally:
        try:
            worker.close()
        except Exception:
            pass
        try:
            cleanup_parent_tmpdir(wt.parent_tmpdir, validated.repo_root)
        except Exception:
            pass
