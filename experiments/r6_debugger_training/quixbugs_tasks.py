#!/usr/bin/env python3
"""R6 — QuixBugs DebugTask fixture builder (disjoint training/validation set).

Builds executable DebugTask fixtures from the QuixBugs repository at the
frozen revision 4257f44b (the S4-frozen acquisition path), each consisting
of:

- the buggy production module (``<algo>.py``) copied VERBATIM from
  ``python_programs/<algo>.py`` (the QuixBugs programs are standalone: no
  intra-package imports; ``node.py`` is copied alongside when the tests use
  it);
- the ORIGINAL QuixBugs pytest test module, mechanically rewritten ONLY in
  its import mechanics (the ``pytest.use_correct`` conditional becomes a
  direct ``from <algo> import <algo>``; the JSON testcase loader reads
  ``testdata/<algo>.json`` next to the fixture) — test semantics and cases
  are the authoritative QuixBugs ones;
- a ``task.json`` manifest in the accepted DebugTask schema;
- the gold reference repair (buggy -> correct unified diff), harness-only.

The fail-to-pass / pass-to-pass partition is established by REAL execution
of the tests against the buggy source (per-test timeout for hanging buggy
cases), the reproduction node is required to complete quickly (a hanging
reproduction is rejected — the r5 treatment needs a real fast baseline
failure), and every task must pass the independent EvaluationVerifier with
its gold repair (verifier-backed successful-repair evidence) before it is
admitted to the corpus.

Contamination boundary: QuixBugs is repository-disjoint from the five R5/R6
curated holdouts (curated-none-handling-001 .. curated-caller-callee-005);
those holdouts are never present in this data and never trained on.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.evaluation.runner import load_task  # noqa: E402
from agentic_debugger.evaluation.verifier import EvaluationVerifier  # noqa: E402

# Frozen QuixBugs identity (S4-frozen acquisition).
QUIXBUGS_REVISION = "4257f44b0ff1181dedaedee6a447e133219fcebf"
QUIXBUGS_URL = "https://github.com/jkoppel/QuixBugs.git"

# The frozen quix40 cohort manifest (accepted S4 cohort).
QUIX40_MANIFEST = (
    REPO_ROOT / "experiments/raw-pilot-v1.1/state/quix40-v1/pilot_manifest_frozen_v1.jsonl"
)

# Frozen split seed — task-disjoint train/validation, frozen before training.
SPLIT_SEED = "r6-debugger-sft-split-2026-08-12-v1"
VALIDATION_TASK_COUNT = 8

# The five R5/R6 curated HOLDOUT task ids — never present in this data.
CURATED_HOLDOUT_IDS = frozenset({
    "curated-none-handling-001",
    "curated-off-by-one-002",
    "curated-wrong-branch-003",
    "curated-mutation-alias-004",
    "curated-caller-callee-005",
})

# Per-test timeout for the builder's own execution runs (hanging buggy
# cases must not stall the whole run); reproduction-node hang check.
_BUILDER_TEST_TIMEOUT_SECONDS = 15
_REPRODUCTION_NODE_TIMEOUT_SECONDS = 15

_TASK_TITLE_OVERRIDES = {
    "find_first_in_sorted": "Return the first occurrence position",
    "find_in_sorted": "Return the position of a value in a sorted list",
    "is_valid_parenthesization": "Check balanced parentheses",
    "next_palindrome": "Return the next palindrome integer",
    "next_permutation": "Return the next permutation of a list",
    "rpn_eval": "Evaluate a reverse-Polish-notation expression",
    "shunting_yard": "Convert infix expression tokens to RPN",
    "shortest_path_lengths": "Return all-pairs shortest path lengths",
    "topological_ordering": "Return a topological ordering of a graph",
    "minimum_spanning_tree": "Return a minimum spanning tree",
    "detect_cycle": "Detect a cycle in a linked list",
    "depth_first_search": "Depth-first search over a graph",
    "breadth_first_search": "Breadth-first search over a graph",
    "reverse_linked_list": "Reverse a linked list",
    "to_base": "Convert an integer to a base-N digit list",
    "wrap": "Wrap a string to a maximum line width",
    "lcs_length": "Return the longest common subsequence length",
    "longest_common_subsequence": "Return the longest common subsequence",
    "max_sublist_sum": "Return the maximum sublist sum",
    "possible_change": "Count possible change combinations",
    "get_factors": "Return the prime factors of an integer",
    "powerset": "Return the powerset of a list",
    "subsequences": "Return all subsequences of a list",
    "knapsack": "Return the optimal knapsack value",
    "levenshtein": "Return the Levenshtein distance",
    "lis": "Return a longest increasing subsequence",
    "kheapsort": "Sort a list with a k-way heap",
    "mergesort": "Merge sort a list",
    "quicksort": "Quicksort a list",
    "bucketsort": "Bucket sort a list of floats",
    "bitcount": "Count set bits of an integer",
    "gcd": "Return the greatest common divisor",
    "hanoi": "Return the Tower of Hanoi moves",
    "kth": "Return the k-th smallest element",
    "flatten": "Flatten a nested list",
    "pascal": "Return a Pascal-triangle row",
    "sqrt": "Return the integer square root",
    "sieve": "Return primes up to n",
    "shortest_path_length": "Return the shortest path length between two nodes",
    "shortest_paths": "Return all shortest paths between two nodes",
}


class R6TaskError(ValueError):
    """Raised when a QuixBugs task cannot be materialized (fail closed)."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_quixbugs_repo(root: Path) -> Path:
    """Acquire (or reuse) the QuixBugs checkout at the frozen revision.

    Mirrors the frozen S4 acquisition path; fails closed unless HEAD equals
    the frozen revision and the working tree is the canonical LF form.
    """
    root = Path(root)
    repo = root / "QuixBugs"
    if repo.is_dir():
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0 and proc.stdout.strip() == QUIXBUGS_REVISION:
            return repo
        raise R6TaskError(f"existing QuixBugs checkout not at frozen revision: {repo}")
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", QUIXBUGS_URL, str(repo)],
        check=True, timeout=1200,
    )
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=str(repo), check=True)
    subprocess.run(["git", "checkout", "--detach", "-q", QUIXBUGS_REVISION], cwd=str(repo), check=True)
    subprocess.run(["git", "reset", "--hard", "-q", QUIXBUGS_REVISION], cwd=str(repo), check=True)
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, timeout=60, check=True,
    )
    if proc.stdout.strip() != QUIXBUGS_REVISION:
        raise R6TaskError("QuixBugs checkout HEAD != frozen revision")
    return repo


def quix40_algorithms(manifest_path: Path = QUIX40_MANIFEST) -> list[str]:
    """The frozen quix40 cohort algorithm names (slot order)."""
    if not manifest_path.is_file():
        raise R6TaskError(f"quix40 manifest missing: {manifest_path}")
    algorithms = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        algorithms.append(row["program"])
    return algorithms


# ---------------------------------------------------------------------------
# Mechanical test-module rewrite (import mechanics only; semantics untouched)
# ---------------------------------------------------------------------------

_USE_CORRECT_RE = re.compile(
    r"if\s+pytest\.use_correct\s*:\s*\n"
    r"(?P<correct>[^\n]*)\n"
    r"else\s*:\s*\n"
    r"(?P<buggy>[^\n]*)\n",
    re.MULTILINE,
)


def rewrite_test_module(original: str, algo: str) -> str:
    """Rewrite ONLY the import mechanics of the original QuixBugs test module.

    - the ``pytest.use_correct`` conditional becomes a direct
      ``from <algo> import <algo>``;
    - the ``load_testdata`` import is removed;
    - ``testdata = load_json_testcases(...)`` becomes an inline deterministic
      loader reading ``testdata/<algo>.json`` next to the fixture root.
    All test functions, cases, and assertions remain the authoritative
    QuixBugs content.
    """
    source = original
    match = _USE_CORRECT_RE.search(source)
    if match is None:
        raise R6TaskError(f"{algo}: test module has no pytest.use_correct block")
    import_line = match.group("buggy").strip()
    if not import_line.startswith("from python_programs."):
        raise R6TaskError(f"{algo}: unexpected buggy import line {import_line!r}")
    source = _USE_CORRECT_RE.sub(f"from {algo} import {algo}\n", source, count=1)
    source = source.replace(
        "from load_testdata import load_json_testcases\n", ""
    )
    loader = (
        "import json as _r6_json\n"
        "from pathlib import Path as _r6_path\n"
        f"testdata = [_r6_json.loads(_r6_line) for _r6_line in "
        f"(_r6_path(__file__).resolve().parent.parent / \"testdata\" / "
        f"\"{algo}.json\").read_text(encoding=\"utf-8\").splitlines() "
        f"if _r6_line.strip()]\n"
    )
    source, count = re.subn(
        r"testdata\s*=\s*load_json_testcases\([^)]*\)\s*\n", loader, source, count=1
    )
    # Inline-data tasks (e.g. detect_cycle, shortest_paths) carry their test
    # data in the test module itself and never call the JSON loader.
    if count == 0:
        source = source.rstrip() + "\n"
    return source


# ---------------------------------------------------------------------------
# Real-execution classification
# ---------------------------------------------------------------------------


def _run_pytest_collect_only(cwd: Path) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q",
         "-p", "no:cacheprovider", "--rootdir", str(cwd.resolve())],
        cwd=str(cwd), capture_output=True, text=True, timeout=120,
    )
    node_ids = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        # Node-id lines start with "tests/" and contain "::"; parametrized
        # ids may contain spaces and brackets (e.g. [[17, 0]-17]).
        if line.startswith("tests/") and "::" in line:
            if "error" in line.lower() or "warning" in line.lower():
                continue
            node_ids.append(line)
    return node_ids


def _run_pytest_results(cwd: Path, node_ids: list[str]) -> dict[str, str]:
    """Real execution of the generated tests against the buggy source.

    Per-test timeout via the installed pytest-timeout plugin so hanging
    buggy cases (e.g. the bitcount infinite loop) are classified FAILED
    instead of stalling the run.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "--tb=no",
         "-p", "no:cacheprovider", "--rootdir", str(cwd.resolve()),
         f"--timeout={_BUILDER_TEST_TIMEOUT_SECONDS}", "--timeout-method=thread",
         "tests"] + node_ids,
        cwd=str(cwd), capture_output=True, text=True, timeout=300,
    )
    results: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("tests/") or "::" not in line:
            continue
        line = re.sub(r"\s*\[\s*\d+%\s*\]\s*$", "", line)
        node, _, status = line.rpartition(" ")
        status = status.strip()
        if status in ("PASSED", "FAILED", "ERROR", "SKIPPED"):
            results[node.strip()] = status
        elif "Timeout" in line:
            # pytest-timeout: a hanging buggy case is a failing test (the
            # verbose line shape is "node +++...+++ Timeout +++...+++").
            results[node.strip()] = "FAILED"
    return results


def _node_completes_fast(node_id: str, cwd: Path) -> bool:
    """The reproduction node must complete (fail) quickly — a hanging
    reproduction (infinite loop on the buggy source) is rejected: the r5
    treatment needs a real fast baseline failure."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", node_id, "-q", "--tb=no",
             "-p", "no:cacheprovider", "--rootdir", str(cwd.resolve())],
            cwd=str(cwd), capture_output=True, text=True,
            timeout=_REPRODUCTION_NODE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 1


def _gold_diff(original: str, corrected: str, module_path: str) -> str:
    diff = "".join(difflib.unified_diff(
        [line + "\n" for line in original.splitlines()],
        [line + "\n" for line in corrected.splitlines()],
        fromfile=f"a/{module_path}",
        tofile=f"b/{module_path}",
        lineterm="\n", n=3,
    ))
    if not diff.strip():
        raise R6TaskError("gold repair produced an empty diff")
    return diff


@dataclass(frozen=True)
class BuiltTask:
    task_id: str
    algo: str
    fixture_dir: Path
    module_path: str
    original_source_sha256: str
    gold_diff: str
    gold_diff_sha256: str
    f2p: list[str]
    p2p: list[str]
    reproduction_node: str
    bug_category: str
    root_cause_summary: str
    function_name: str


def build_task_fixture(
    quixbugs_repo: Path,
    output_root: Path,
    algo: str,
    *,
    force: bool = False,
) -> BuiltTask:
    """Materialize one QuixBugs DebugTask fixture and verify its gold repair."""
    repo = Path(quixbugs_repo)
    buggy_source = (repo / "python_programs" / f"{algo}.py").read_text(encoding="utf-8")
    correct_source = (repo / "correct_python_programs" / f"{algo}.py").read_text(encoding="utf-8")
    original_test = (repo / "python_testcases" / f"test_{algo}.py").read_text(encoding="utf-8")
    testdata_path = repo / "json_testcases" / f"{algo}.json"
    test_module = rewrite_test_module(original_test, algo)

    task_id = f"quixbugs-{algo.replace('_', '-')}"
    fixture_rel = f"agentic_debugger/datasets/curated/{task_id}"
    fixture_dir = output_root / task_id
    if fixture_dir.exists():
        if not force:
            raise R6TaskError(f"fixture already exists: {fixture_dir} (use force)")
        shutil.rmtree(fixture_dir)
    (fixture_dir / "tests").mkdir(parents=True, exist_ok=True)
    (fixture_dir / "testdata").mkdir(parents=True, exist_ok=True)
    (fixture_dir / f"{algo}.py").write_text(buggy_source, encoding="utf-8", newline="\n")
    has_node = (repo / "python_programs" / "node.py").is_file() and "from node import Node" in original_test
    if has_node:
        (fixture_dir / "node.py").write_text(
            (repo / "python_programs" / "node.py").read_text(encoding="utf-8"),
            encoding="utf-8", newline="\n",
        )
    (fixture_dir / "tests" / f"test_{algo}.py").write_text(
        test_module, encoding="utf-8", newline="\n"
    )
    if testdata_path.is_file():
        (fixture_dir / "testdata" / f"{algo}.json").write_text(
            testdata_path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
        )
    denied_write_paths = ["tests", "testdata", "task.json"]
    if has_node:
        denied_write_paths.append("node.py")

    node_ids = _run_pytest_collect_only(fixture_dir)
    if not node_ids:
        raise R6TaskError(f"{algo}: no tests collected")
    results = _run_pytest_results(fixture_dir, node_ids)
    f2p = [n for n in node_ids if results.get(n) == "FAILED"]
    p2p = [n for n in node_ids if results.get(n) == "PASSED"]
    if not f2p:
        raise R6TaskError(f"{algo}: no failing tests on the buggy source")
    if not p2p:
        raise R6TaskError(f"{algo}: no passing tests on the buggy source")

    # Reproduction node: the first failing node that completes fast.
    reproduction_node: Optional[str] = None
    for node in f2p:
        if _node_completes_fast(node, fixture_dir):
            reproduction_node = node
            break
    if reproduction_node is None:
        raise R6TaskError(f"{algo}: every failing test hangs on the buggy source")

    gold_diff = _gold_diff(buggy_source, correct_source, f"{algo}.py")
    gold_diff_sha256 = hashlib.sha256(gold_diff.encode("utf-8")).hexdigest()

    task_json: dict[str, Any] = {
        "schema_version": "1.0",
        "task_id": task_id,
        "title": _TASK_TITLE_OVERRIDES.get(algo, f"Fix {algo}"),
        "description": (
            f"A deterministic Python helper ({algo}) produces incorrect "
            "results for some inputs. Debug the real failure, diagnose the "
            "root cause from the runtime evidence, and repair the module."
        ),
        "language": "python",
        "fixture_path": fixture_rel,
        "source": {
            "kind": "curated",
            "path": fixture_rel,
            "provenance": {
                "dataset": "QuixBugs",
                "manifest_id": "quix40-v1",
                "manifest_fingerprint": "572082482a64adabc8c790293580a9869cdda485a3813505f14adec850577afd",
                "upstream_repository": QUIXBUGS_URL,
                "upstream_revision": QUIXBUGS_REVISION,
                "project": "QuixBugs",
                "bug_id": algo,
                "buggy_revision": QUIXBUGS_REVISION,
                "fixed_revision": QUIXBUGS_REVISION,
            },
        },
        "reproduction": {
            "argv": [
                "python", "-m", "pytest", reproduction_node, "-q",
                "-p", "no:cacheprovider",
            ],
            "cwd": ".",
            "timeout_seconds": 10,
            "expected_exit_code": 1,
        },
        "tests": {
            "fail_to_pass": f2p,
            "pass_to_pass": p2p,
            "full_suite_argv": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            "timeout_seconds": 20,
        },
        "constraints": {
            "allowed_write_paths": [f"{algo}.py"],
            "denied_write_paths": denied_write_paths,
            "network_allowed": False,
            "external_services_allowed": False,
            "max_patch_attempts": 2,
            "max_test_runs": 5,
            "max_pdb_observations": 8,
        },
        "oracle": {
            "bug_category": f"quixbugs-{algo}",
            "target_files": [f"{algo}.py"],
            "target_symbols": [algo],
            "root_cause_summary": (
                f"The {algo} function produces an incorrect result for some "
                "inputs (QuixBugs gold-repair delta)."
            ),
            "runtime_evidence_hint": (
                "The paused production frame exposes the function arguments "
                "and local state at the failure."
            ),
        },
        "tags": ["quixbugs", "r6-training", "disjoint-from-curated-holdouts"],
    }
    (fixture_dir / "task.json").write_text(
        json.dumps(task_json, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    # Gold reference repair stays OUTSIDE the fixture (harness-only; the
    # fixture tree is what the model-visible treatment and verifier copy).
    gold_dir = REPO_ROOT / "experiments/r6_debugger_training/gold"
    gold_dir.mkdir(parents=True, exist_ok=True)
    (gold_dir / f"{task_id}.patch").write_text(gold_diff, encoding="utf-8")

    # Verifier-backed gold verification (independent EvaluationVerifier).
    task = load_task(str(fixture_dir / "task.json"))
    try:
        evaluation = EvaluationVerifier(str(REPO_ROOT), workspace_parent=None).evaluate(
            task, gold_diff
        )
    except Exception as exc:
        raise R6TaskError(f"{algo}: gold verification raised {type(exc).__name__}: {exc}")
    if evaluation.status is None or evaluation.outcome is None:
        raise R6TaskError(
            f"{algo}: gold verification incomplete "
            f"(status={evaluation.status}, outcome={evaluation.outcome})"
        )
    if evaluation.status.value != "COMPLETED" or evaluation.outcome.value != "RESOLVED":
        raise R6TaskError(
            f"{algo}: gold repair not verifier-RESOLVED "
            f"({evaluation.status.value}/{evaluation.outcome.value})"
        )
    if evaluation.full_suite is None or evaluation.full_suite.status.value != "PASS":
        raise R6TaskError(f"{algo}: gold repair full suite not PASS")

    return BuiltTask(
        task_id=task_id,
        algo=algo,
        fixture_dir=fixture_dir,
        module_path=f"{algo}.py",
        original_source_sha256=sha256_bytes(buggy_source.encode("utf-8")),
        gold_diff=gold_diff,
        gold_diff_sha256=gold_diff_sha256,
        f2p=f2p,
        p2p=p2p,
        reproduction_node=reproduction_node,
        bug_category=f"quixbugs-{algo}",
        root_cause_summary=task_json["oracle"]["root_cause_summary"],
        function_name=algo,
    )


def split_algorithms(algorithms: list[str], seed: str = SPLIT_SEED) -> dict[str, list[str]]:
    """Deterministic task-disjoint train/validation split (frozen seed)."""
    import random
    rng = random.Random(seed)
    ordered = sorted(algorithms)
    rng.shuffle(ordered)
    validation = ordered[:VALIDATION_TASK_COUNT]
    train = ordered[VALIDATION_TASK_COUNT:]
    return {"train": train, "validation": validation}


def write_split_manifest(
    output_dir: Path,
    split: dict[str, list[str]],
    built: dict[str, BuiltTask],
) -> Path:
    """Frozen split manifest: task ids, source hashes, gold hashes."""
    manifest = {
        "schema_version": "r6-debugger-sft-split-v1",
        "seed": SPLIT_SEED,
        "validation_task_count": VALIDATION_TASK_COUNT,
        "quixbugs_revision": QUIXBUGS_REVISION,
        "quix40_manifest": str(QUIX40_MANIFEST),
        "holdout_excluded": sorted(CURATED_HOLDOUT_IDS),
        "train_tasks": [
            {
                "task_id": built[a].task_id,
                "algo": a,
                "source_sha256": built[a].original_source_sha256,
                "gold_diff_sha256": built[a].gold_diff_sha256,
                "f2p_count": len(built[a].f2p),
                "p2p_count": len(built[a].p2p),
                "reproduction_node": built[a].reproduction_node,
            }
            for a in split["train"]
        ],
        "validation_tasks": [
            {
                "task_id": built[a].task_id,
                "algo": a,
                "source_sha256": built[a].original_source_sha256,
                "gold_diff_sha256": built[a].gold_diff_sha256,
                "f2p_count": len(built[a].f2p),
                "p2p_count": len(built[a].p2p),
                "reproduction_node": built[a].reproduction_node,
            }
            for a in split["validation"]
        ],
    }
    manifest_text = json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False)
    manifest_path = output_dir / "split_manifest.json"
    manifest_path.write_text(manifest_text, encoding="utf-8")
    return manifest_path


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Build R6 QuixBugs DebugTask fixtures + frozen split"
    )
    parser.add_argument("--quixbugs-root", type=str, default=None,
                        help="parent dir for the QuixBugs checkout (default operator/r6-training)")
    parser.add_argument("--output-root", type=str, default=None,
                        help="fixture output root (default experiments/r6_debugger_training/tasks)")
    parser.add_argument("--force", action="store_true",
                        help="rebuild existing fixtures")
    args = parser.parse_args()

    if args.quixbugs_root:
        repo = ensure_quixbugs_repo(Path(args.quixbugs_root))
    else:
        repo = ensure_quixbugs_repo(REPO_ROOT / "operator" / "r6-training")

    output_root = Path(args.output_root) if args.output_root else (
        REPO_ROOT / "agentic_debugger" / "datasets" / "curated"
    )
    output_root.mkdir(parents=True, exist_ok=True)

    algorithms = quix40_algorithms()
    built: dict[str, BuiltTask] = {}
    failures: list[str] = []
    for algo in algorithms:
        try:
            built[algo] = build_task_fixture(repo, output_root, algo, force=args.force)
            print(f"OK   {algo}: f2p={len(built[algo].f2p)} p2p={len(built[algo].p2p)}")
        except R6TaskError as exc:
            failures.append(algo)
            print(f"FAIL {algo}: {exc}")
        except Exception as exc:
            failures.append(algo)
            print(f"ERROR {algo}: {type(exc).__name__}: {exc}")

    split = split_algorithms([a for a in algorithms if a in built])
    # Split manifest and summary live in the EXPERIMENT dir, never in the
    # curated fixture root.
    meta_dir = THIS_FILE.parent
    manifest_path = write_split_manifest(meta_dir, split, built)
    summary = {
        "built_tasks": len(built),
        "failed_tasks": failures,
        "split": {k: [built[a].task_id for a in v] for k, v in split.items()},
        "split_manifest": str(manifest_path),
        "fixture_root": str(output_root),
    }
    (meta_dir / "build_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
