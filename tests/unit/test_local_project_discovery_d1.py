"""D1 discovery recon tests — offline proving over temp Git repos + fixtures.

Covers proposal section 1 exactly: allowlist G1-G8/F1, hard caps, the
deterministic confidence rubric, the DiscoveryProposal schema, and refusals
R1-R8. No execution of project code, no providers, no network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.event_contracts import contains_credential_shape
from agentic_debugger.application.local_project import inventory_tracked_python_files
from agentic_debugger.application.local_project_discovery import (
    MAX_CHARS_PER_FILE,
    MAX_DEEP_FILES,
    MAX_HYPOTHESES,
    MAX_LOG_SUBJECTS,
    MAX_OPS,
    MAX_TEST_CANDIDATES,
    WALL_BUDGET_S,
    DiscoveryProposal,
    DiscoveryRefusalError,
    discover_local_project,
)
from agentic_debugger.application.local_project_git import assert_path_inside_workspace
from agentic_debugger.application.local_project_helpers import _split_command


def _run(cmd: list[str], cwd: Path, allow_empty: bool = False) -> str:
    r = subprocess.run(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=15,
    )
    assert r.returncode == 0, f"{cmd} failed: {r.stderr[:300]}"
    return r.stdout.strip()


def _git_init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "d1@test.com"], repo)
    _run(["git", "config", "user.name", "D1"], repo)


def _write(repo: Path, rel: str, text: str) -> Path:
    full = repo / rel.replace("/", __import__("os").sep)
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(text, encoding="utf-8")
    return full


def _commit(repo: Path, message: str, allow_empty: bool = False) -> None:
    _run(["git", "add", "."], repo)
    cmd = ["git", "commit", "-m", message]
    if allow_empty:
        cmd.append("--allow-empty")
    _run(cmd, repo)


def _make_repo(tmp_path: Path, name: str, files: dict[str, str], message: str = "initial") -> Path:
    repo = tmp_path / name
    _git_init(repo)
    for rel, text in files.items():
        _write(repo, rel, text)
    _commit(repo, message)
    return repo


def _assert_caps(proposal: DiscoveryProposal) -> None:
    summary = proposal.recon_summary
    assert summary is not None
    assert summary.steps_used <= MAX_OPS
    assert summary.ms_elapsed <= int(WALL_BUDGET_S * 1000)
    assert summary.log_scanned <= MAX_LOG_SUBJECTS
    assert summary.test_files <= MAX_TEST_CANDIDATES
    assert len(proposal.hypotheses) <= MAX_HYPOTHESES
    for hypo in proposal.hypotheses:
        assert len(hypo.statement) <= 500
        assert len(hypo.statement.encode("utf-8")) <= 4096
        assert hypo.statement.strip()
        assert not contains_credential_shape(hypo.statement)
    for cmd in (proposal.repro_candidate, proposal.verify_candidate):
        if cmd is not None:
            assert len(cmd.encode("utf-8")) <= 2048
            assert _split_command(cmd)
            assert not contains_credential_shape(cmd)
    if proposal.confidence_overall in ("medium", "low"):
        assert proposal.could_not_determine
    if proposal.confidence_overall == "low":
        assert list(proposal.hypotheses) == []


# -- R1: unresolvable / not-a-repo / HEAD-missing -------------------------------

def test_r1_not_a_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(DiscoveryRefusalError, match="not a Git repository"):
        discover_local_project(str(plain), str(tmp_path))


def test_r1_not_found(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryRefusalError, match="not found"):
        discover_local_project(str(tmp_path / "no_such_dir_xyz"), str(tmp_path))


def test_r1_not_a_dir(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(DiscoveryRefusalError, match="not a directory"):
        discover_local_project(str(target), str(tmp_path))


def test_r1_head_missing(tmp_path: Path) -> None:
    repo = tmp_path / "nohead"
    _git_init(repo)
    with pytest.raises(DiscoveryRefusalError, match="HEAD"):
        discover_local_project(str(repo), str(tmp_path))


# -- R2: dirty ------------------------------------------------------------------

def test_r2_dirty_tracked_change(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "dirty1", {"calc.py": "VALUE = 1\n"})
    (repo / "calc.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(DiscoveryRefusalError, match="commit or stash"):
        discover_local_project(str(repo), str(tmp_path))


def test_r2_dirty_untracked_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "dirty2", {"calc.py": "VALUE = 1\n"})
    (repo / "new_file.py").write_text("VALUE = 9\n", encoding="utf-8")
    with pytest.raises(DiscoveryRefusalError, match="commit or stash"):
        discover_local_project(str(repo), str(tmp_path))


# -- R3/R4: inventory bounds -----------------------------------------------------

def test_r3_no_python_files(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "nopy", {"README.md": "# nothing here\n"})
    with pytest.raises(DiscoveryRefusalError, match="No supported Python source files"):
        discover_local_project(str(repo), str(tmp_path))


def test_r4_too_many_files(tmp_path: Path) -> None:
    files = {f"mod_{i:03d}.py": f"VALUE_{i} = {i}\n" for i in range(201)}
    repo = _make_repo(tmp_path, "big", files)
    with pytest.raises(DiscoveryRefusalError, match="too large|200"):
        discover_local_project(str(repo), str(tmp_path))


# -- R5: oversize-skipped surfaced, never silent ----------------------------------

def test_r5_oversize_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "oversize"
    _git_init(repo)
    _write(repo, "normal.py", "def ok():\n    return 1\n")
    _write(repo, "big_blob.py", "x = 1\n" * 200000)
    assert (repo / "big_blob.py").stat().st_size > 1024 * 1024
    _commit(repo, "initial")
    proposal = discover_local_project(str(repo), str(tmp_path))
    assert proposal.recon_summary is not None
    assert proposal.recon_summary.py_files == 1
    joined = " ".join(proposal.could_not_determine)
    assert "1 MiB" in joined or "oversize" in joined
    _assert_caps(proposal)


# -- R6: no-tests + no-repro is Low with reasons, never a guessed bug -------------

def test_r6_no_tests_no_repro_is_low(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "notests", {"app.py": "def main():\n    return 0\n"})
    proposal = discover_local_project(str(repo), str(tmp_path))
    assert proposal.confidence_overall == "low"
    assert list(proposal.hypotheses) == []
    assert proposal.repro_candidate is None
    assert proposal.verify_candidate is None
    assert proposal.repro_reason
    assert proposal.verify_reason
    assert proposal.could_not_determine
    _assert_caps(proposal)


# -- Repro-present ---------------------------------------------------------------

def test_repro_present_with_marker_is_high(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "reprohigh", {
        "repro.py": "print('repro')\n",
        "app.py": "def run():\n    pass  # TODO fix this\n",
    })
    proposal = discover_local_project(str(repo), str(tmp_path))
    assert proposal.repro_candidate == "python repro.py"
    assert proposal.verify_candidate == "python repro.py"
    assert proposal.confidence_overall == "high"
    assert len(proposal.hypotheses) == 1
    hypo = proposal.hypotheses[0]
    assert hypo.target_file == "app.py"
    assert hypo.target_symbol == "run"
    assert hypo.confidence == "high"
    assert any("app.py:2" in e for e in hypo.evidence)
    _assert_caps(proposal)


def test_repro_present_without_marker_is_low_but_proposes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "reprolow", {
        "repro.py": "print('repro')\n",
        "app.py": "def run():\n    return 1\n",
    })
    proposal = discover_local_project(str(repo), str(tmp_path))
    assert proposal.repro_candidate == "python repro.py"
    assert proposal.confidence_overall == "low"
    assert list(proposal.hypotheses) == []
    assert proposal.could_not_determine
    _assert_caps(proposal)


# -- S2 log keywords ---------------------------------------------------------------

def test_log_keyword_flags_subject(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path, "logkw",
        {"calc.py": "def add(a, b):\n    return a - b\n",
         "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n"},
        message="fix calculator boundary",
    )
    proposal = discover_local_project(str(repo), str(tmp_path))
    assert proposal.recon_summary is not None
    assert proposal.recon_summary.log_scanned == 1
    # Single test + log keyword => Medium with the SHA7 as evidence.
    assert proposal.confidence_overall == "medium"
    assert len(proposal.hypotheses) == 1
    assert proposal.recon_summary.head7 in proposal.hypotheses[0].evidence
    assert proposal.repro_candidate == "python -m pytest tests/test_calc.py -q"
    _assert_caps(proposal)


def test_log_subject_without_keyword_word_is_ignored(tmp_path: Path) -> None:
    # "fixture" contains "fix" as a substring but is not failure language.
    repo = _make_repo(
        tmp_path, "logplain",
        {"calc.py": "def add(a, b):\n    return a - b\n",
         "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n"},
        message="fixture import",
    )
    proposal = discover_local_project(str(repo), str(tmp_path))
    assert proposal.confidence_overall == "low"
    assert list(proposal.hypotheses) == []
    # Repro still proposed (exact tracked match), but no bug guessed.
    assert proposal.repro_candidate == "python -m pytest tests/test_calc.py -q"
    _assert_caps(proposal)


def test_log_cap_20_subjects(tmp_path: Path) -> None:
    repo = tmp_path / "manycommits"
    _git_init(repo)
    _write(repo, "app.py", "VALUE = 1\n")
    _commit(repo, "initial")
    for i in range(24):
        _run(["git", "commit", "--allow-empty", "-m", f"chore step {i}"], repo)
    proposal = discover_local_project(str(repo), str(tmp_path))
    assert proposal.recon_summary is not None
    assert proposal.recon_summary.log_scanned == MAX_LOG_SUBJECTS == 20
    _assert_caps(proposal)


# -- S4 markers ---------------------------------------------------------------------

def test_marker_drives_medium(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "marker", {
        "calc.py": "def add(a, b):\n    return a - b  # FIXME wrong op\n",
        "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n",
    })
    proposal = discover_local_project(str(repo), str(tmp_path))
    assert proposal.confidence_overall == "medium"
    hypo = proposal.hypotheses[0]
    assert hypo.target_file == "calc.py"
    assert hypo.target_symbol == "add"
    assert any(e == "calc.py:2" for e in hypo.evidence)
    _assert_caps(proposal)


def test_marker_credential_line_not_echoed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "markcred", {
        # TODO marker line carries a credential shape: must be skipped, not echoed.
        "app.py": "def run():\n    pass  # TODO rotate password = hunter2-secret\n",
        "tests/test_app.py": "from app import run\ndef test_run():\n    assert run() is None\n",
    })
    proposal = discover_local_project(str(repo), str(tmp_path))
    for hypo in proposal.hypotheses:
        for item in list(hypo.evidence) + [hypo.statement]:
            assert "hunter2" not in item
    joined = " ".join(list(proposal.could_not_determine) + [
        h.statement for h in proposal.hypotheses
    ])
    assert "hunter2" not in joined
    _assert_caps(proposal)


def test_unparseable_source_yields_empty_symbol(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "badsrc", {
        "bad.py": "TODO fix this\nthis is (((( not python\n",
        "tests/test_bad.py": "def test_placeholder():\n    assert True\n",
    })
    proposal = discover_local_project(str(repo), str(tmp_path))
    assert proposal.confidence_overall == "medium"
    hypo = proposal.hypotheses[0]
    assert hypo.target_file == "bad.py"
    assert hypo.target_symbol == ""
    _assert_caps(proposal)


# -- R8: credential-shape / unparseable synthesis drops candidate ---------------------

def test_r8_credential_shape_drops_candidate(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "credshape", {
        "src.py": "VALUE = 1\n",
        "tests/test-token=abc.py": "def test_placeholder():\n    assert True\n",
    })
    proposal = discover_local_project(str(repo), str(tmp_path))
    assert proposal.repro_candidate is None
    assert proposal.repro_reason is not None
    assert "credential" in proposal.repro_reason
    assert proposal.could_not_determine
    _assert_caps(proposal)


def test_r8_unparseable_command_drops_candidate(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "unparse", {
        "src.py": "VALUE = 1\n",
        "tests/test 'quote.py": "def test_placeholder():\n    assert True\n",
    })
    proposal = discover_local_project(str(repo), str(tmp_path))
    assert proposal.repro_candidate is None
    assert proposal.repro_reason is not None
    assert "pars" in proposal.repro_reason
    _assert_caps(proposal)


# -- Caps --------------------------------------------------------------------------

def test_test_candidates_capped_at_20(tmp_path: Path) -> None:
    files = {"app.py": "VALUE = 1\n"}
    for i in range(25):
        files[f"tests/test_mod_{i:02d}.py"] = "def test_x():\n    assert True\n"
    repo = _make_repo(tmp_path, "manytests", files)
    proposal = discover_local_project(str(repo), str(tmp_path))
    assert proposal.recon_summary is not None
    assert proposal.recon_summary.test_files == MAX_TEST_CANDIDATES == 20
    # Multiple tests => bare suite command, never an invented single path.
    assert proposal.repro_candidate == "python -m pytest -q"
    _assert_caps(proposal)


def test_single_test_never_invents_paths(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "single", {
        "calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n",
    })
    proposal = discover_local_project(str(repo), str(tmp_path))
    assert proposal.repro_candidate == "python -m pytest tests/test_calc.py -q"
    assert proposal.verify_candidate == "python -m pytest tests/test_calc.py -q"
    tracked = inventory_tracked_python_files(repo)
    assert "tests/test_calc.py" in tracked
    _assert_caps(proposal)


def test_determinism(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "determ", {
        "calc.py": "def add(a, b):\n    return a - b  # TODO fix\n",
        "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n",
    })
    first = discover_local_project(str(repo), str(tmp_path)).to_mapping()
    second = discover_local_project(str(repo), str(tmp_path)).to_mapping()
    first["recon_summary"].pop("ms_elapsed")
    second["recon_summary"].pop("ms_elapsed")
    assert first == second


# -- Module hygiene: no Textual, no new git verbs, no contract touch ------------------

def test_no_textual_import() -> None:
    text = Path("agentic_debugger/application/local_project_discovery.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            assert "textual" not in stripped.lower()


def test_no_new_git_verbs_or_execution() -> None:
    import ast as _ast
    import re as _re
    text = Path("agentic_debugger/application/local_project_discovery.py").read_text(encoding="utf-8")
    # Direct `git` invocations use the ["git", "<verb>", ...] list form; the
    # docstring mentions forbidden verbs only as prose (no bracket form).
    verbs = _re.findall(r"\[\s*\"git\"\s*,\s*\"([a-z-]+)\"", text)
    assert verbs == ["log"], f"sole direct git verb must be G7 log, got {verbs}"
    # No execution primitives beyond the single subprocess.run(G7) call.
    tree = _ast.parse(text)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute):
            assert node.attr not in ("system", "popen", "Popen"), node.attr
        if isinstance(node, _ast.Call):
            func = node.func
            name = ""
            if isinstance(func, _ast.Attribute):
                name = func.attr
            elif isinstance(func, _ast.Name):
                name = func.id
            assert name not in ("system", "popen", "Popen", "check_output",
                                "check_call", "run_command"), name


def test_no_contract_verifier_journal_touch() -> None:
    import ast as _ast
    text = Path("agentic_debugger/application/local_project_discovery.py").read_text(encoding="utf-8")
    tree = _ast.parse(text)
    imported: list[str] = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, _ast.ImportFrom):
            imported.append(node.module or "")
    joined = "\n".join(imported).lower()
    assert "local_project_contracts" not in joined
    assert "local_project_verifier" not in joined
    assert "journal" not in joined
    assert "history" not in joined
    assert "textual" not in joined


# -- Curated fixtures: deterministic recon over fixture content ------------------------

CURATED_IDS = [
    "curated-caller-callee-005",
    "curated-mutation-alias-004",
    "curated-none-handling-001",
    "curated-off-by-one-002",
    "curated-wrong-branch-003",
    "pdb-required-boundary-006",
    "pdb-required-caller-callee-007",
    "pdb-required-multistage-units-008",
]


def _copy_curated_into_repo(fixture_id: str, repo: Path) -> None:
    root = Path("agentic_debugger/datasets/curated") / fixture_id
    assert root.is_dir(), f"missing curated fixture {fixture_id}"
    for child in sorted(root.iterdir()):
        if child.name == "__pycache__" or child.name.endswith(".pyc"):
            continue
        if child.is_file() and child.suffix == ".py":
            (repo / child.name).write_bytes(child.read_bytes())
    tests_src = root / "tests"
    assert tests_src.is_dir()
    for child in sorted(tests_src.iterdir()):
        if child.name == "__pycache__" or child.name.endswith(".pyc"):
            continue
        if child.is_file() and child.suffix == ".py":
            dest = repo / "tests" / child.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(child.read_bytes())


@pytest.mark.parametrize("fixture_id", CURATED_IDS)
def test_curated_fixture_recon(tmp_path: Path, fixture_id: str) -> None:
    repo = tmp_path / ("cur_" + fixture_id.replace("-", "_"))
    _git_init(repo)
    _copy_curated_into_repo(fixture_id, repo)
    _commit(repo, "curated content import")
    first = discover_local_project(str(repo), str(tmp_path))
    tracked = inventory_tracked_python_files(repo)
    assert tracked
    assert not any(f == "repro.py" for f in tracked)
    # repro.py-absent fixtures propose single-file-pytest, bare pytest, or None.
    if first.repro_candidate is not None:
        if first.repro_candidate.startswith("python -m pytest ") and first.repro_candidate != "python -m pytest -q":
            inner = first.repro_candidate[len("python -m pytest "):-len(" -q")]
            assert inner in tracked, f"invented path {inner} for {fixture_id}"
            assert_path_inside_workspace(repo, inner)
        else:
            assert first.repro_candidate in ("python repro.py", "python -m pytest -q"), first.repro_candidate
    for hypo in first.hypotheses:
        assert hypo.target_file in tracked
        assert_path_inside_workspace(repo, hypo.target_file)
    _assert_caps(first)
    # Deterministic: second run matches modulo wall measurement.
    second = discover_local_project(str(repo), str(tmp_path)).to_mapping()
    mapping = first.to_mapping()
    mapping["recon_summary"].pop("ms_elapsed")
    second["recon_summary"].pop("ms_elapsed")
    assert mapping == second
