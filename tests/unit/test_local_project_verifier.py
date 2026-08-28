from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from agentic_debugger.evaluation.local_project_verifier import (
    LocalProjectEvaluationPlan,
    LocalProjectVerifier,
)
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome
from agentic_debugger.evaluation.runner import EvaluationStatus


GOOD_PATCH = """--- a/calculator.py
+++ b/calculator.py
@@ -1,5 +1,5 @@
 def add(a, b):
-    return a - b
+    return a + b
 
 def stable():
     return 7
"""

BREAKING_PATCH = """--- a/calculator.py
+++ b/calculator.py
@@ -1,5 +1,5 @@
 def add(a, b):
-    return a - b
+    return a + b
 
 def stable():
-    return 7
+    return 8
"""

SYNTAX_PATCH = """--- a/calculator.py
+++ b/calculator.py
@@ -1,5 +1,5 @@
 def add(a, b):
-    return a - b
+    return (
 
 def stable():
     return 7
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Verifier Test")
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n\ndef stable():\n    return 7\n",
        encoding="utf-8",
    )
    (repo / "reproduce.py").write_text(
        "from calculator import add\nraise SystemExit(0 if add(2, 3) == 5 else 1)\n",
        encoding="utf-8",
    )
    (repo / "regression.py").write_text(
        "from calculator import stable\nraise SystemExit(0 if stable() == 7 else 1)\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _plan(
    repo: Path,
    head: str,
    patch: str = GOOD_PATCH,
    *,
    reproduction: tuple[str, ...] | None = None,
    regression: tuple[str, ...] | None = None,
    workspace_parent: Path | None = None,
) -> LocalProjectEvaluationPlan:
    return LocalProjectEvaluationPlan(
        source_repo_path=str(repo),
        source_head_commit=head,
        candidate_patch=patch,
        reproduction_argv=(
            reproduction
            if reproduction is not None
            else (sys.executable, "reproduce.py")
        ),
        regression_argv=(
            regression
            if regression is not None
            else (sys.executable, "regression.py")
        ),
        allowed_paths=("calculator.py",),
        denied_paths=("tests", "task.json"),
        workspace_parent=str(workspace_parent) if workspace_parent else None,
    )


def test_independent_verifier_resolves_only_from_fresh_evidence(tmp_path: Path) -> None:
    repo, head = _repo(tmp_path)
    parent = tmp_path / "verifier-workspaces"
    parent.mkdir()

    result = LocalProjectVerifier().evaluate(
        _plan(repo, head, workspace_parent=parent)
    )

    assert result.status is EvaluationStatus.COMPLETED
    assert result.outcome is SemanticOutcome.RESOLVED
    assert result.resolved is True
    assert result.baseline_reproduction is not None
    assert result.baseline_reproduction.exit_code == 1
    assert result.post_patch_reproduction is not None
    assert result.post_patch_reproduction.exit_code == 0
    assert result.regression is not None
    assert result.regression.exit_code == 0
    assert result.workspace.cleaned is True
    assert result.workspace.canonical_fixture_unchanged is True
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain") == ""
    assert list(parent.iterdir()) == []


def test_baseline_pass_cannot_be_promoted_to_resolved(tmp_path: Path) -> None:
    repo, head = _repo(tmp_path)
    result = LocalProjectVerifier().evaluate(
        _plan(
            repo,
            head,
            reproduction=(sys.executable, "-c", "print('not a failure')"),
        )
    )
    assert result.status is EvaluationStatus.BASELINE_INVALID
    assert result.stop_reason == "baseline_reproduction_not_genuine_failure"
    assert result.outcome is None
    assert result.f2p_total == 0


def test_regression_failure_is_breaking_resolved_not_fixed(tmp_path: Path) -> None:
    repo, head = _repo(tmp_path)
    result = LocalProjectVerifier().evaluate(_plan(repo, head, BREAKING_PATCH))
    assert result.status is EvaluationStatus.COMPLETED
    assert result.outcome is SemanticOutcome.BREAKING_RESOLVED
    assert result.f2p_passed == 1
    assert result.p2p_passed == 0
    assert result.resolved is False


def test_stateful_baseline_side_effect_cannot_manufacture_f2p_pass(
    tmp_path: Path,
) -> None:
    repo, head = _repo(tmp_path)
    (repo / "stateful_reproduce.py").write_text(
        """from pathlib import Path
flag = Path('baseline-side-effect.flag')
if flag.exists():
    raise SystemExit(0)
flag.write_text('created', encoding='utf-8')
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    _git(repo, "add", "stateful_reproduce.py")
    _git(repo, "commit", "-m", "stateful reproduction")
    head = _git(repo, "rev-parse", "HEAD")

    result = LocalProjectVerifier().evaluate(
        _plan(
            repo,
            head,
            reproduction=(sys.executable, "stateful_reproduce.py"),
        )
    )

    assert result.status is EvaluationStatus.COMPLETED
    assert result.outcome is SemanticOutcome.NO_OP
    assert result.post_patch_reproduction is not None
    assert result.post_patch_reproduction.exit_code == 1
    assert result.workspace.cleaned is True


def test_regression_must_pass_on_clean_baseline_before_it_counts_as_p2p(
    tmp_path: Path,
) -> None:
    repo, head = _repo(tmp_path)

    result = LocalProjectVerifier().evaluate(
        _plan(
            repo,
            head,
            regression=(sys.executable, "reproduce.py"),
        )
    )

    assert result.status is EvaluationStatus.BASELINE_INVALID
    assert result.stop_reason == "baseline_regression_not_passing"
    assert result.outcome is None
    assert result.baseline_regression is not None
    assert result.baseline_regression.exit_code == 1
    assert result.p2p_total == 0


def test_missing_commands_fail_closed(tmp_path: Path) -> None:
    repo, head = _repo(tmp_path)
    missing_reproduction = LocalProjectEvaluationPlan(
        source_repo_path=str(repo),
        source_head_commit=head,
        candidate_patch=GOOD_PATCH,
        reproduction_argv=None,
        regression_argv=(sys.executable, "regression.py"),
        allowed_paths=("calculator.py",),
        denied_paths=(),
    )
    baseline = LocalProjectVerifier().evaluate(missing_reproduction)
    assert baseline.status is EvaluationStatus.BASELINE_INVALID
    assert baseline.outcome is None

    missing_regression = LocalProjectEvaluationPlan(
        source_repo_path=str(repo),
        source_head_commit=head,
        candidate_patch=GOOD_PATCH,
        reproduction_argv=(sys.executable, "reproduce.py"),
        regression_argv=None,
        allowed_paths=("calculator.py",),
        denied_paths=(),
    )
    regression = LocalProjectVerifier().evaluate(missing_regression)
    assert regression.status is EvaluationStatus.TEST_EXECUTION_FAILED
    assert regression.stop_reason == "regression_command_missing"
    assert regression.outcome is None


def test_syntax_failure_and_head_mismatch_fail_closed(tmp_path: Path) -> None:
    repo, head = _repo(tmp_path)
    syntax = LocalProjectVerifier().evaluate(_plan(repo, head, SYNTAX_PATCH))
    assert syntax.status is EvaluationStatus.SYNTAX_FAILED
    assert syntax.outcome is None
    assert _git(repo, "status", "--porcelain") == ""

    mismatch = LocalProjectVerifier().evaluate(
        _plan(repo, "0" * 40)
    )
    assert mismatch.status is not EvaluationStatus.COMPLETED
    assert mismatch.outcome is None
    assert mismatch.workspace.canonical_fixture_unchanged is False
