"""Local Project discovery recon (D1) — bounded read-only propose, offline only.

D1 implements proposal ``_ai-review/local-project-discovery-plan/proposal.md``
section 1 only: deterministic recon over a clean Git worktree that proposes
Bug/Repro/Verify candidates without execution, providers, UI, persistence,
or contract change.

Allowlist (nothing else may run during recon):

- G1 ``git rev-parse --is-inside-work-tree`` (via validate, 10 s).
- G2 ``git rev-parse --show-toplevel`` (via validate, 10 s).
- G3 ``git rev-parse HEAD`` (via validate, 10 s; must be 40-hex or refuse).
- G4 ``git status --porcelain`` (via validate, 10 s; ANY output => refuse).
- G5 ``git ls-files -z`` tracked ``*.py`` inventory via
  :func:`inventory_tracked_python_files` ONLY (10 s; <=200 files, <=1 MiB
  each, ``.git`` excluded, symlink-escape screened).
- G6 ``git ls-files -- repro.py`` via :func:`has_tracked_root_repro` ONLY.
- G7 ``git log --oneline -20 --no-decorate`` (sole new verb, same read-only
  class; subjects only, never diffs/bodies; <=20 subjects).
- G8 test/config name enumeration as an IN-MEMORY filter of the G5 result
  (no new git call) plus bounded filesystem existence checks for the four
  config names (no content read).
- F1 bounded file reads via :func:`assert_path_inside_workspace`
  (<=4000 chars/file, <=3 deep files).

Total recon <=12 ops, <=60 s wall. FORBIDDEN in recon: mutation verbs,
``python``/``pytest`` execution, network/clone, writes anywhere, and any
persistence/hashing/journaling of proposal metadata (no such code path
exists here).

Failure honesty: invented paths forbidden (single-file pytest only on an
exact tracked match, else None+reason); unknown symbol is ``""``;
``could_not_determine`` is mandatory on Medium/Low; Low returns empty
hypotheses + reasons, never a guessed bug.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.event_contracts import contains_credential_shape
from agentic_debugger.application.local_project import (
    has_tracked_root_repro,
    inventory_tracked_python_files,
)
from agentic_debugger.application.local_project_git import (
    LocalProjectValidationError,
    assert_path_inside_workspace,
    resolve_project_path,
    validate_local_project,
)
from agentic_debugger.application.local_project_helpers import _split_command


MAX_OPS = 12
WALL_BUDGET_S = 60.0
MAX_CHARS_PER_FILE = 4000
MAX_DEEP_FILES = 3
MAX_HYPOTHESES = 3
MAX_LOG_SUBJECTS = 20
MAX_TEST_CANDIDATES = 20
MAX_MARKER_HITS = 5
MARKER_LINE_MAX = 200
HYPOTHESIS_STATEMENT_MAX = 500
MAX_CMD_BYTES = 2048
MAX_BUG_BYTES = 4096
ONE_MIB = 1024 * 1024

_LOG_KEYWORD_RE = re.compile(
    r"(?i)\b(?:fix(?:ed|es|ing)?|bug(?:s)?|fail(?:ed|ing|ure(?:s)?)?|"
    r"regress(?:ion(?:s)?|ed|ing)?|broken|error(?:s)?)\b"
)
_MARKER_RE = re.compile(r"\b(?:TODO|FIXME|XXX|HACK)\b")
_SHA7_RE = re.compile(r"^[0-9a-f]{7}$")
_CONFIG_NAMES = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
_CLEAN_GATE_MESSAGE = "Project has uncommitted changes \u2014 commit or stash them first."
_NO_PYTHON_MESSAGE = "No supported Python source files found."
_PDB_NO_PROBE_NOTE = (
    "repro command has no PDB probe expected under _resolve_pdb_probe logic "
    "(needs `python`/`python3` + inside-workspace `*.py` that exists); "
    "controller starts from UNDERSTAND when repro is None, else REPRODUCE"
)
_UNVERIFIED_NOTE = "proposed, unverified \u2014 verifier decides"


class DiscoveryRefusalError(ApplicationInputError):
    """Typed fail-closed refusal for local-project discovery (R1-R4)."""


@dataclass(frozen=True)
class DiscoveryHypothesis:
    """One ranked bug hypothesis (<=3 per proposal)."""

    id: str
    statement: str
    target_file: str
    target_symbol: str
    confidence: str
    evidence: tuple = field(default_factory=tuple)
    not_determined: tuple = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if type(self.id) is not str or not self.id:
            raise ApplicationInputError("hypothesis id must be a non-empty string")
        if type(self.statement) is not str or not self.statement.strip():
            raise ApplicationInputError("hypothesis statement must be non-empty")
        if len(self.statement) > HYPOTHESIS_STATEMENT_MAX:
            raise ApplicationInputError("hypothesis statement exceeds 500 chars")
        if len(self.statement.encode("utf-8")) > MAX_BUG_BYTES:
            raise ApplicationInputError("hypothesis statement exceeds the 4 KiB bound")
        if contains_credential_shape(self.statement):
            raise ApplicationInputError("hypothesis statement contains credential shape")
        if type(self.target_file) is not str or not self.target_file:
            raise ApplicationInputError("hypothesis target_file must be non-empty")
        if type(self.target_symbol) is not str:
            raise ApplicationInputError("hypothesis target_symbol must be a string")
        if self.confidence not in ("high", "medium", "low"):
            raise ApplicationInputError("hypothesis confidence must be high|medium|low")
        if not isinstance(self.evidence, (list, tuple)):
            raise ApplicationInputError("hypothesis evidence must be a list")
        if not isinstance(self.not_determined, (list, tuple)):
            raise ApplicationInputError("hypothesis not_determined must be a list")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "not_determined", tuple(self.not_determined))

    def to_mapping(self) -> dict:
        return {
            "id": self.id,
            "statement": self.statement,
            "target_file": self.target_file,
            "target_symbol": self.target_symbol,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "not_determined": list(self.not_determined),
        }


@dataclass(frozen=True)
class DiscoveryReconSummary:
    """Bounded recon accounting (measurement only, never evidence)."""

    py_files: int
    test_files: int
    log_scanned: int
    steps_used: int
    ms_elapsed: int
    head7: str

    def __post_init__(self) -> None:
        for name in ("py_files", "test_files", "log_scanned", "steps_used", "ms_elapsed"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ApplicationInputError(f"recon summary {name} must be a non-negative integer")
        if type(self.head7) is not str:
            raise ApplicationInputError("recon summary head7 must be a string")
        if self.head7 and not _SHA7_RE.match(self.head7):
            raise ApplicationInputError("recon summary head7 must be empty or 7-hex")
        if self.log_scanned > MAX_LOG_SUBJECTS:
            raise ApplicationInputError("recon summary log_scanned exceeds 20")
        if self.test_files > MAX_TEST_CANDIDATES:
            raise ApplicationInputError("recon summary test_files exceeds 20")
        if self.steps_used > MAX_OPS:
            raise ApplicationInputError("recon summary steps_used exceeds 12")

    def to_mapping(self) -> dict:
        return {
            "py_files": self.py_files,
            "test_files": self.test_files,
            "log_scanned": self.log_scanned,
            "steps_used": self.steps_used,
            "ms_elapsed": self.ms_elapsed,
            "head7": self.head7,
        }


@dataclass(frozen=True)
class DiscoveryProposal:
    """Ranked discovery output (UI-ephemeral; never persisted/hashed/journaled)."""

    hypotheses: tuple = field(default_factory=tuple)
    repro_candidate: Optional[str] = None
    repro_reason: Optional[str] = None
    verify_candidate: Optional[str] = None
    verify_reason: Optional[str] = None
    confidence_overall: str = "low"
    could_not_determine: tuple = field(default_factory=tuple)
    recon_summary: Optional[DiscoveryReconSummary] = None

    def __post_init__(self) -> None:
        if not isinstance(self.hypotheses, (list, tuple)):
            raise ApplicationInputError("hypotheses must be a list")
        object.__setattr__(self, "hypotheses", tuple(self.hypotheses))
        if len(self.hypotheses) > MAX_HYPOTHESES:
            raise ApplicationInputError("hypotheses exceed 3")
        for hypo in self.hypotheses:
            if type(hypo) is not DiscoveryHypothesis:
                raise ApplicationInputError("hypotheses must be DiscoveryHypothesis")
        for label in ("repro_candidate", "verify_candidate"):
            value = getattr(self, label)
            if value is not None and (type(value) is not str or not value):
                raise ApplicationInputError(f"{label} must be a non-empty string or null")
            if isinstance(value, str) and len(value.encode("utf-8")) > MAX_CMD_BYTES:
                raise ApplicationInputError(f"{label} exceeds the 2 KiB bound")
        for label in ("repro_reason", "verify_reason"):
            value = getattr(self, label)
            if value is not None and (type(value) is not str or not value):
                raise ApplicationInputError(f"{label} must be a non-empty string or null")
        if self.confidence_overall not in ("high", "medium", "low"):
            raise ApplicationInputError("confidence_overall must be high|medium|low")
        if not isinstance(self.could_not_determine, (list, tuple)):
            raise ApplicationInputError("could_not_determine must be a list")
        object.__setattr__(self, "could_not_determine", tuple(self.could_not_determine))
        if self.confidence_overall in ("medium", "low") and not self.could_not_determine:
            raise ApplicationInputError("could_not_determine is required on medium/low")
        if self.confidence_overall == "low" and self.hypotheses:
            raise ApplicationInputError("low confidence must carry empty hypotheses")
        if type(self.recon_summary) is not DiscoveryReconSummary:
            raise ApplicationInputError("recon_summary is required")

    def to_mapping(self) -> dict:
        return {
            "hypotheses": [h.to_mapping() for h in self.hypotheses],
            "repro_candidate": self.repro_candidate,
            "repro_reason": self.repro_reason,
            "verify_candidate": self.verify_candidate,
            "verify_reason": self.verify_reason,
            "confidence_overall": self.confidence_overall,
            "could_not_determine": list(self.could_not_determine),
            "recon_summary": self.recon_summary.to_mapping() if self.recon_summary else None,
        }


def _is_test_path(relative_posix: str) -> bool:
    """Whether a tracked ``*.py`` path counts as a test candidate (G8)."""
    if not relative_posix.endswith(".py"):
        return False
    lowered = relative_posix.replace("\\", "/")
    parts = lowered.split("/")
    base = parts[-1]
    if base == "conftest.py":
        return True
    if base.startswith("test_") or base.endswith("_test.py"):
        return True
    if parts[0] == "tests" or "tests" in parts:
        return True
    return False


def _first_def_symbol(bounded_text: str) -> str:
    """First top-level ``def`` name in bounded text, else ``""`` (never invented).

    Mirrors the controller ``FIND_FUNCTION``/``GET_SOURCE_WINDOW`` read path at
    a static level: an AST scan for the first function definition. Any parse
    failure, absence, or non-identifier yields ``""``.
    """
    try:
        tree = ast.parse(bounded_text)
    except Exception:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = getattr(node, "name", "")
            if type(name) is str and name.isidentifier():
                return name
    # Fallback line scan (same rule as the PDB probe helper): first `def `.
    try:
        for line in bounded_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("def "):
                token = stripped[4:].split("(")[0].split(":")[0].strip()
                if token.isidentifier():
                    return token
    except Exception:
        return ""
    return ""


def _pdb_probe_expected(command: str, repo_root: Path) -> bool:
    """Static PDB-eligibility prediction reusing ``_resolve_pdb_probe`` logic.

    Requires ``python``/``python3`` + a relative inside-workspace ``*.py``
    second token that exists. Never starts a session; never executes.
    """
    try:
        argv = _split_command(command)
    except Exception:
        return False
    if len(argv) < 2:
        return False
    if argv[0] not in ("python", "python3"):
        return False
    script = argv[1]
    if not script.endswith(".py"):
        return False
    if script.startswith("/") or script.startswith("\\"):
        return False
    if ".." in script.replace("\\", "/").split("/"):
        return False
    if len(script) >= 2 and script[1] == ":" and script[0].isalpha():
        return False
    try:
        assert_path_inside_workspace(repo_root, script)
    except Exception:
        return False
    try:
        full = repo_root / script.replace("/", os.sep)
        return full.is_file()
    except Exception:
        return False


def _validate_command_candidate(command: str) -> tuple[Optional[str], Optional[str]]:
    """Fail-closed command check: parse + 2 KiB + credential-shape.

    Returns (command, None) when proposable, else (None, reason) per R8.
    """
    if len(command.encode("utf-8")) > MAX_CMD_BYTES:
        return None, "candidate exceeds the 2 KiB bound"
    try:
        argv = _split_command(command)
    except ValueError as exc:
        return None, f"candidate cannot be parsed: {exc}"
    if not argv:
        return None, "candidate must contain an executable"
    if contains_credential_shape(command):
        return None, "candidate contains credential shape"
    return command, None


def _run_git_log(repo_root: Path) -> list[tuple[str, str]]:
    """G7: last <=20 oneline subjects (SHA7, subject). Never diffs/bodies."""
    result = subprocess.run(
        ["git", "log", "--oneline", "-20", "--no-decorate"],
        stdin=subprocess.DEVNULL,
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10.0,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise ApplicationInputError(
            f"git log failed: {(result.stderr.strip() or result.stdout.strip())[:200]}"
        )
    subjects: list[tuple[str, str]] = []
    for line in result.stdout.splitlines()[:MAX_LOG_SUBJECTS]:
        line = line.strip()
        if not line:
            continue
        sha, _, subject = line.partition(" ")
        sha7 = sha.strip()[:7]
        if not re.fullmatch(r"[0-9a-f]{7}", sha7):
            continue
        subjects.append((sha7, subject.strip()[:500]))
    return subjects


def _count_oversize_py_on_disk(repo_root: Path, tracked_py: list[str]) -> int:
    """R5 audit: bounded count of on-disk ``*.py`` above 1 MiB (F1-class read).

    Runs only on clean trees, so on-disk files are tracked-or-ignored; any
    oversize hit is reported as excluded from the tracked inventory rather
    than silently absorbed. Bounded: at most 1000 entries walked.
    """
    tracked = set(tracked_py)
    oversize = 0
    checked = 0
    try:
        for path in repo_root.rglob("*.py"):
            if checked >= 1000:
                break
            checked += 1
            try:
                rel = path.relative_to(repo_root).as_posix()
            except ValueError:
                continue
            if rel.startswith(".git/") or rel == ".git":
                continue
            try:
                if path.is_symlink():
                    continue
            except OSError:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > ONE_MIB:
                oversize += 1
    except Exception:
        return oversize
    return oversize


def discover_local_project(
    project_path: Union[str, os.PathLike],
    launch_cwd: Optional[Union[str, os.PathLike, Path]] = None,
) -> DiscoveryProposal:
    """Bounded read-only recon over one clean Git project (D1 pure function).

    Raises :class:`DiscoveryRefusalError` on R1-R4 (unresolvable/dirty/empty/
    too-large). Returns a Low proposal with reasons on R5-R8/budget stops;
    never executes, never writes, never persists/hashes/journals metadata.
    """
    t0 = time.monotonic()
    ops = 0

    def elapsed_ms() -> int:
        return int((time.monotonic() - t0) * 1000)

    def wall_exceeded() -> bool:
        return (time.monotonic() - t0) > WALL_BUDGET_S

    def low_fallback(
        *,
        head7: str = "",
        py_count: int = 0,
        test_count: int = 0,
        log_scanned: int = 0,
        reasons: Optional[list[str]] = None,
    ) -> DiscoveryProposal:
        could: list[str] = list(reasons or [])
        if not could:
            could = ["recon stopped before a bug could be proposed; manual form is the path"]
        summary = DiscoveryReconSummary(
            py_files=py_count,
            test_files=min(test_count, MAX_TEST_CANDIDATES),
            log_scanned=min(log_scanned, MAX_LOG_SUBJECTS),
            steps_used=min(ops, MAX_OPS),
            ms_elapsed=elapsed_ms(),
            head7=head7 if head7 and _SHA7_RE.match(head7) else "",
        )
        return DiscoveryProposal(
            hypotheses=[],
            repro_candidate=None,
            repro_reason="no repro candidate (recon stopped)",
            verify_candidate=None,
            verify_reason="no verify candidate (recon stopped)",
            confidence_overall="low",
            could_not_determine=tuple(could),
            recon_summary=summary,
        )

    # -- S0 gate (G1-G4 via validate: 4 ops) ---------------------------------
    launch: Optional[Path] = None
    if launch_cwd is not None:
        launch = Path(launch_cwd) if not isinstance(launch_cwd, Path) else launch_cwd
    try:
        if isinstance(project_path, Path):
            raw = str(project_path)
            if project_path.is_absolute():
                resolved_hint = project_path
            else:
                resolved_hint = resolve_project_path(raw, launch)
            validated = validate_local_project(resolved_hint, launch_cwd=launch)
        else:
            raw = str(project_path)
            resolve_project_path(raw, launch)
            validated = validate_local_project(raw, launch_cwd=launch)
    except DiscoveryRefusalError:
        raise
    except LocalProjectValidationError as exc:
        raise DiscoveryRefusalError(str(exc)) from exc
    except ApplicationInputError as exc:
        raise DiscoveryRefusalError(str(exc)) from exc
    ops += 4
    if wall_exceeded() or ops > MAX_OPS:
        return low_fallback(reasons=["budget exceeded before inventory (12 ops / 60 s)"])
    if validated.dirty:
        raise DiscoveryRefusalError(_CLEAN_GATE_MESSAGE)
    repo_root = validated.repo_root
    head = validated.head_commit
    head7 = head[:7]

    # -- S1 inventory (G5: 1 op) ----------------------------------------------
    try:
        py_files = inventory_tracked_python_files(repo_root)
    except ApplicationInputError as exc:
        message = str(exc)
        raise DiscoveryRefusalError(message) from exc
    ops += 1
    py_files = sorted(set(py_files))[:200]
    if wall_exceeded() or ops > MAX_OPS:
        return low_fallback(head7=head7, py_count=len(py_files),
                            reasons=["budget exceeded after inventory (12 ops / 60 s)"])

    could_not: list[str] = []

    # -- R5 oversize audit (F1-class read: 1 op) --------------------------------
    ops += 1
    try:
        oversize_count = _count_oversize_py_on_disk(repo_root, py_files)
    except Exception:
        oversize_count = 0
    if oversize_count > 0:
        could_not.append(
            f"{oversize_count} oversize Python file(s) above 1 MiB were excluded "
            "from inventory (bounded v1, not examined)"
        )
    if wall_exceeded() or ops > MAX_OPS:
        return low_fallback(head7=head7, py_count=len(py_files),
                            reasons=["budget exceeded after oversize audit (12 ops / 60 s)"]
                            + could_not)

    # -- S2 history (G7: 1 op) --------------------------------------------------
    try:
        log_subjects = _run_git_log(repo_root)
        ops += 1
    except ApplicationInputError as exc:
        log_subjects = []
        could_not.append(f"history signals not determined ({str(exc)[:160]})")
        ops += 1
    log_subjects = log_subjects[:MAX_LOG_SUBJECTS]
    keyword_hits = [(sha, subj) for sha, subj in log_subjects if _LOG_KEYWORD_RE.search(subj)]
    if wall_exceeded() or ops > MAX_OPS:
        return low_fallback(head7=head7, py_count=len(py_files),
                            log_scanned=len(log_subjects),
                            reasons=["budget exceeded after history (12 ops / 60 s)"] + could_not)

    # -- S3 test layout (G6: 1 op; G8 in-memory; config check: 1 op) -------------
    try:
        has_repro = bool(has_tracked_root_repro(repo_root))
    except Exception:
        has_repro = False
    ops += 1
    test_files = sorted([f for f in py_files if _is_test_path(f)])[:MAX_TEST_CANDIDATES]
    ops += 1  # bounded config-name existence checks (no content read)
    config_hits: list[str] = []
    try:
        for name in _CONFIG_NAMES:
            try:
                if (repo_root / name).is_file():
                    config_hits.append(name)
            except OSError:
                continue
    except Exception:
        pass
    if wall_exceeded() or ops > MAX_OPS:
        return low_fallback(head7=head7, py_count=len(py_files),
                            test_count=len(test_files), log_scanned=len(log_subjects),
                            reasons=["budget exceeded after test layout (12 ops / 60 s)"] + could_not)

    # -- S4 static markers (F1: <=3 ops, <=4000 chars/file, <=5 hits) ------------
    root_py = sorted([f for f in py_files if "/" not in f])
    main_py = sorted([f for f in py_files if f.endswith("__main__.py") and f not in root_py])
    rest_py = sorted([f for f in py_files if f not in root_py and f not in main_py])
    deep_files = (root_py + main_py + rest_py)[:MAX_DEEP_FILES]
    markers: list[tuple[str, int, str]] = []
    file_texts: dict[str, str] = {}
    for rel in deep_files:
        if len(markers) >= MAX_MARKER_HITS:
            break
        if wall_exceeded() or ops >= MAX_OPS:
            could_not.append("marker scan stopped at budget (12 ops / 60 s); remaining files not examined")
            break
        try:
            assert_path_inside_workspace(repo_root, rel)
        except ApplicationInputError:
            could_not.append(f"marker scan skipped escaping path {rel}")
            continue
        try:
            full = repo_root / rel.replace("/", os.sep)
            text = full.read_text(encoding="utf-8", errors="replace")[:MAX_CHARS_PER_FILE]
        except OSError as exc:
            could_not.append(f"marker scan could not read {rel} ({str(exc)[:120]})")
            continue
        ops += 1
        file_texts[rel] = text
        try:
            for lineno, line in enumerate(text.splitlines(), start=1):
                if len(markers) >= MAX_MARKER_HITS:
                    break
                if not _MARKER_RE.search(line):
                    continue
                if contains_credential_shape(line):
                    continue
                markers.append((rel, lineno, line.strip()[:MARKER_LINE_MAX]))
        except Exception:
            continue
    if wall_exceeded() or ops > MAX_OPS:
        return low_fallback(head7=head7, py_count=len(py_files),
                            test_count=len(test_files), log_scanned=len(log_subjects),
                            reasons=["budget exceeded after marker scan (12 ops / 60 s)"] + could_not)

    # -- S5 Repro/Verify synthesis (propose, never execute) ----------------------
    if has_repro:
        repro_raw: Optional[str] = "python repro.py"
        verify_raw: Optional[str] = "python repro.py"
        null_hint: Optional[str] = None
    elif len(test_files) == 1:
        repro_raw = f"python -m pytest {test_files[0]} -q"
        verify_raw = repro_raw
        null_hint = None
    elif len(test_files) > 1:
        repro_raw = "python -m pytest -q"
        verify_raw = repro_raw
        null_hint = None
    else:
        repro_raw = None
        verify_raw = None
        null_hint = "no tracked repro.py and no test files found"

    repro_candidate: Optional[str] = None
    repro_reason: Optional[str] = None
    verify_candidate: Optional[str] = None
    verify_reason: Optional[str] = None

    if repro_raw is None:
        repro_reason = null_hint or "no repro candidate"
        verify_reason = null_hint or "no verify candidate"
    else:
        # Sandbox check for single-file pytest (invented paths forbidden).
        needs_exact = repro_raw.startswith("python -m pytest ") and repro_raw.endswith(" -q") \
            and repro_raw != "python -m pytest -q"
        if needs_exact:
            inner = repro_raw[len("python -m pytest "):-len(" -q")]
            if inner not in py_files:
                repro_candidate, repro_reason = None, f"candidate path is not an exact tracked match ({inner})"
                verify_candidate, verify_reason = None, f"candidate path is not an exact tracked match ({inner})"
            else:
                try:
                    assert_path_inside_workspace(repo_root, inner)
                except ApplicationInputError as exc:
                    repro_candidate, repro_reason = None, f"candidate escapes workspace ({str(exc)[:120]})"
                    verify_candidate, verify_reason = None, f"candidate escapes workspace ({str(exc)[:120]})"
                else:
                    repro_candidate, repro_reason = _validate_command_candidate(repro_raw)
                    if repro_candidate is None:
                        verify_candidate, verify_reason = None, repro_reason
                    else:
                        verify_candidate, verify_reason = _validate_command_candidate(verify_raw or "")
        else:
            repro_candidate, repro_reason = _validate_command_candidate(repro_raw)
            if repro_candidate is None:
                verify_candidate, verify_reason = None, repro_reason
            else:
                verify_candidate, verify_reason = _validate_command_candidate(verify_raw or "")
        if repro_candidate is None and repro_reason:
            could_not.append(f"repro candidate dropped ({repro_reason[:160]})")
        if verify_candidate is None and verify_reason and verify_reason != repro_reason:
            could_not.append(f"verify candidate dropped ({verify_reason[:160]})")

    if repro_candidate is not None and not _pdb_probe_expected(repro_candidate, repo_root):
        could_not.append(_PDB_NO_PROBE_NOTE)

    # -- S6 hypothesis ranking (<=3, deterministic rubric) ------------------------
    has_marker = len(markers) > 0
    has_log_kw = len(keyword_hits) > 0
    has_tests = len(test_files) > 0

    def symbol_for(rel: str) -> str:
        text = file_texts.get(rel)
        if text is None:
            try:
                assert_path_inside_workspace(repo_root, rel)
                full = repo_root / rel.replace("/", os.sep)
                text = full.read_text(encoding="utf-8", errors="replace")[:MAX_CHARS_PER_FILE]
            except Exception:
                return ""
        return _first_def_symbol(text)

    hypotheses: list[DiscoveryHypothesis] = []
    confidence_overall = "low"

    if has_repro and has_marker:
        confidence_overall = "high"
        rel, lineno, _ = markers[0]
        marker_ref = f"{rel}:{lineno}"
        symbol = symbol_for(rel)
        statement = (
            f"Possible defect in {rel} near {symbol or 'unknown symbol'} indicated by "
            f"marker at {marker_ref}; tracked repro.py present; {_UNVERIFIED_NOTE}."
        )
        evidence = [marker_ref, "repro.py"]
        if has_tests:
            # Same-file corroboration only: include a test path solely when it
            # names the same module stem as the marker file.
            stem = Path(rel).stem
            same = sorted([t for t in test_files if stem in Path(t).stem or Path(t).stem in stem])
            if same:
                evidence.append(same[0])
        not_det = [
            "actual failing output not observed (no execution in recon)",
            f"whether {rel} is the true fault location",
            "controller FIND_FUNCTION/GET_SOURCE_WINDOW and verifier baseline decide",
        ]
        if len(statement) <= HYPOTHESIS_STATEMENT_MAX and statement.strip() \
                and not contains_credential_shape(statement):
            hypotheses.append(DiscoveryHypothesis(
                id="h1", statement=statement, target_file=rel,
                target_symbol=symbol, confidence="high",
                evidence=tuple(evidence), not_determined=tuple(not_det),
            ))
            could_not.append(_UNVERIFIED_NOTE)
        else:
            confidence_overall = "low"
            hypotheses = []
            could_not.append("candidate bug text failed display validation; no bug proposed")
    elif has_repro:
        confidence_overall = "low"
        could_not.append(
            "tracked repro.py present but no corroborating marker pointing at the same "
            "file; no bug proposed (Low confidence)"
        )
        could_not.append(
            f"checked: {len(py_files)} Python file(s), {len(test_files)} test file(s), "
            f"last {len(log_subjects)} commit(s), repro.py present"
        )
    elif len(test_files) == 1 and (has_marker or has_log_kw):
        confidence_overall = "medium"
        if has_marker:
            rel, lineno, _ = markers[0]
            marker_ref = f"{rel}:{lineno}"
            symbol = symbol_for(rel)
            statement = (
                f"Possible defect in {rel} near {symbol or 'unknown symbol'} indicated by "
                f"marker at {marker_ref} with test layout {test_files[0]}; "
                "needs confirm \u2014 verifier decides."
            )
            evidence = [marker_ref, test_files[0]]
        else:
            rel = test_files[0]
            symbol = symbol_for(rel)
            sha7 = keyword_hits[0][0]
            statement = (
                f"Possible defect near {rel} ({symbol or 'unknown symbol'}) suggested by "
                f"{test_files[0]} and recent log failure language ({sha7}); "
                "needs confirm \u2014 verifier decides."
            )
            evidence = [test_files[0], sha7]
        not_det = [
            "actual failing command output not observed (no execution in recon)",
            f"whether {rel} is the true fault location",
            "controller FIND_FUNCTION/GET_SOURCE_WINDOW and verifier baseline decide",
        ]
        if len(statement) <= HYPOTHESIS_STATEMENT_MAX and statement.strip() \
                and not contains_credential_shape(statement):
            hypotheses.append(DiscoveryHypothesis(
                id="h1", statement=statement, target_file=rel,
                target_symbol=symbol, confidence="medium",
                evidence=tuple(evidence), not_determined=tuple(not_det),
            ))
            could_not.append("needs confirm \u2014 verifier decides")
        else:
            confidence_overall = "low"
            hypotheses = []
            could_not.append("candidate bug text failed display validation; no bug proposed")
    elif len(test_files) > 1 and (has_marker or has_log_kw):
        confidence_overall = "medium"
        if has_marker:
            rel, lineno, _ = markers[0]
            marker_ref = f"{rel}:{lineno}"
            symbol = symbol_for(rel)
            statement = (
                f"Possible defect in {rel} near {symbol or 'unknown symbol'} indicated by "
                f"marker at {marker_ref} with {len(test_files)} test file(s); "
                "needs confirm \u2014 verifier decides."
            )
            evidence = [marker_ref, test_files[0]]
        else:
            rel = test_files[0]
            symbol = symbol_for(rel)
            sha7 = keyword_hits[0][0]
            statement = (
                f"Possible defect near {rel} ({symbol or 'unknown symbol'}) suggested by "
                f"{len(test_files)} test file(s) and recent log failure language ({sha7}); "
                "needs confirm \u2014 verifier decides."
            )
            evidence = [test_files[0], sha7]
        not_det = [
            "actual failing command output not observed (no execution in recon)",
            f"whether {rel} is the true fault location",
            "controller FIND_FUNCTION/GET_SOURCE_WINDOW and verifier baseline decide",
        ]
        if len(statement) <= HYPOTHESIS_STATEMENT_MAX and statement.strip() \
                and not contains_credential_shape(statement):
            hypotheses.append(DiscoveryHypothesis(
                id="h1", statement=statement, target_file=rel,
                target_symbol=symbol, confidence="medium",
                evidence=tuple(evidence), not_determined=tuple(not_det),
            ))
            could_not.append("needs confirm \u2014 verifier decides")
        else:
            confidence_overall = "low"
            hypotheses = []
            could_not.append("candidate bug text failed display validation; no bug proposed")
    else:
        confidence_overall = "low"
        if not has_repro and not has_tests:
            could_not.append(
                "no tracked repro.py and no test files found; session would be "
                "diagnosable but never Apply-eligible until Repro/Verify are filled"
            )
        elif not has_marker and not has_log_kw:
            could_not.append(
                "valid inventory but divergent/absent signals (no marker, no log "
                "failure language); no bug proposed"
            )
        could_not.append(
            f"checked: {len(py_files)} Python file(s), {len(test_files)} test file(s), "
            f"last {len(log_subjects)} commit(s), "
            f"repro.py {'present' if has_repro else 'absent'}"
        )

    # R6 explicit empty-Verify warning path (Apply-ineligible, not a refuse).
    if not has_repro and not has_tests:
        if verify_candidate is None and (verify_reason or "").find("Apply") < 0:
            could_not.append("Verify empty: needs 1/1 F2P + 1/1 P2P to become Apply-eligible")

    # Deduplicate could_not_determine deterministically, preserve order.
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in could_not:
        if entry not in seen:
            seen.add(entry)
            ordered.append(entry)
    could_not = ordered

    summary = DiscoveryReconSummary(
        py_files=len(py_files),
        test_files=len(test_files),
        log_scanned=len(log_subjects),
        steps_used=min(ops, MAX_OPS),
        ms_elapsed=elapsed_ms(),
        head7=head7,
    )
    # Final cap guard (R7): never emit over-cap output; fall back to Low.
    if (
        summary.steps_used > MAX_OPS
        or summary.ms_elapsed > int(WALL_BUDGET_S * 1000)
        or summary.log_scanned > MAX_LOG_SUBJECTS
        or summary.test_files > MAX_TEST_CANDIDATES
        or len(hypotheses) > MAX_HYPOTHESES
    ):
        return low_fallback(
            head7=head7, py_count=len(py_files),
            test_count=len(test_files), log_scanned=len(log_subjects),
            reasons=["budget exceeded at finalize (12 ops / 60 s / read caps); Low-or-empty"] + could_not,
        )

    return DiscoveryProposal(
        hypotheses=tuple(hypotheses),
        repro_candidate=repro_candidate,
        repro_reason=repro_reason,
        verify_candidate=verify_candidate,
        verify_reason=verify_reason,
        confidence_overall=confidence_overall,  # type: ignore[arg-type]
        could_not_determine=tuple(could_not),
        recon_summary=summary,
    )


__all__ = [
    "DiscoveryHypothesis",
    "DiscoveryProposal",
    "DiscoveryReconSummary",
    "DiscoveryRefusalError",
    "MAX_CHARS_PER_FILE",
    "MAX_DEEP_FILES",
    "MAX_HYPOTHESES",
    "MAX_LOG_SUBJECTS",
    "MAX_OPS",
    "MAX_TEST_CANDIDATES",
    "WALL_BUDGET_S",
    "discover_local_project",
]
