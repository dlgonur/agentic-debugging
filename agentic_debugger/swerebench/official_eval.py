"""Verifier-private official SWE-rebench image evaluation.

Gold patches, test patches, and hidden test identities stay in an external
workspace. Tracked readiness records receive only counts and booleans.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from agentic_debugger.swerebench.authority import DEFAULT_EXTERNAL_ROOT
from agentic_debugger.swerebench.records import OfficialInstanceBundle

OFFICIAL_EVALUATOR_COMMIT = "c71902a8cf8d2b725f63d51f199f4d3e56f68d2d"
OFFICIAL_EVALUATOR_URL = "https://github.com/SWE-rebench/SWE-rebench-V2.git"
NOOP_PATCH = """diff --git a/.swr-baseline-noop b/.swr-baseline-noop
new file mode 100644
--- /dev/null
+++ b/.swr-baseline-noop
@@ -0,0 +1 @@
+baseline-noop
"""


def official_evaluator_root() -> Path:
    return DEFAULT_EXTERNAL_ROOT / "official-evaluator"


def ensure_official_evaluator() -> Path:
    root = official_evaluator_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        _run(
            [
                "git",
                "-c",
                "credential.helper=",
                "clone",
                "--no-tags",
                "--filter=blob:none",
                OFFICIAL_EVALUATOR_URL,
                str(root),
            ],
            root.parent,
        )
    _run(
        [
            "git",
            "fetch",
            "--depth",
            "1",
            "origin",
            OFFICIAL_EVALUATOR_COMMIT,
        ],
        root,
    )
    _run(["git", "checkout", "--detach", OFFICIAL_EVALUATOR_COMMIT], root)
    head = _run(["git", "rev-parse", "HEAD"], root).strip()
    if head != OFFICIAL_EVALUATOR_COMMIT:
        raise RuntimeError(f"official evaluator HEAD {head} != {OFFICIAL_EVALUATOR_COMMIT}")
    return root


def _run(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"{' '.join(args)} failed: {detail[:400]}")
    return completed.stdout


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _docker_pull(image: str) -> tuple[bool, str]:
    completed = subprocess.run(
        ["docker", "pull", image],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0, (completed.stderr or completed.stdout or "")[-400:]


def _write_isolated_spec(
    dest: Path,
    bundle: OfficialInstanceBundle,
    *,
    use_gold: bool,
    candidate_patch: str | None = None,
) -> Path:
    if candidate_patch is not None and use_gold:
        raise ValueError("candidate patch and gold patch are mutually exclusive")
    spec = {
        "instance_id": bundle.public.instance_id,
        "repo": bundle.public.repo,
        "base_commit": bundle.public.base_commit,
        "image_name": bundle.image_name(),
        "patch": (
            bundle.gold_patch()
            if use_gold
            else candidate_patch if candidate_patch is not None else NOOP_PATCH
        ),
        "test_patch": bundle.test_patch(),
        "FAIL_TO_PASS": list(bundle.hidden_tests()[0]),
        "PASS_TO_PASS": list(bundle.hidden_tests()[1]),
        "install_config": bundle.install_config(),
        "problem_statement": bundle.public.problem_statement,
        "language": bundle.public.language,
        "license": bundle.public.license,
    }
    dest.write_text(json.dumps([spec], ensure_ascii=True), encoding="utf-8")
    return dest


def _run_official_eval(spec_path: Path, report_path: Path, workdir: Path) -> dict[str, Any]:
    evaluator = ensure_official_evaluator()
    script = evaluator / "scripts" / "eval.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--json",
            str(spec_path),
            "--max-workers",
            "1",
            "--golden-eval",
            "--report-json",
            str(report_path),
        ],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        check=False,
    )
    report: dict[str, Any] = {}
    report_error: str | None = None
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report_error = f"{type(exc).__name__}: {exc}"
    return {
        "exit_code": completed.returncode,
        "stdout_tail": (completed.stdout or "")[-600:],
        "stderr_tail": (completed.stderr or "")[-600:],
        "report": report,
        "report_error": report_error,
    }


def _summarize_item(
    item: dict[str, Any] | None,
    *,
    empty_p2p: bool,
    requested_instance_id: str | None = None,
    expected_f2p_count: int | None = None,
    expected_p2p_count: int | None = None,
) -> dict[str, Any]:
    """Reduce one official report item without weakening its semantics.

    The pinned evaluator returns process exit 1 for an ordinary mismatch.  Its
    usable result is the report item: ``passed_match`` is authoritative, while
    the private expected cardinalities provide a second guard against a
    partial F2P result being treated as a pass.
    """
    if not isinstance(item, dict):
        return {"available": False, "valid_result": False}
    f2p = item.get("from_fail_to_pass")
    failed_p2p = item.get("failed_from_pass_to_pass")
    identity_matches = (
        requested_instance_id is None or item.get("instance_id") == requested_instance_id
    )
    shape_valid = (
        (requested_instance_id is None or isinstance(item.get("instance_id"), str))
        and isinstance(f2p, list)
        and isinstance(failed_p2p, list)
        and (item.get("error") in {None, ""})
        and isinstance(item.get("passed_match"), bool)
    )
    f2p = f2p if isinstance(f2p, list) else []
    failed_p2p = failed_p2p if isinstance(failed_p2p, list) else []
    complete_f2p = (
        expected_f2p_count is not None and len(f2p) == expected_f2p_count
    )
    complete_p2p = (
        expected_p2p_count is not None and len(failed_p2p) == 0
    )
    authoritative_match = item.get("passed_match") is True
    passed_match = bool(
        shape_valid
        and identity_matches
        and authoritative_match
        and (complete_f2p if expected_f2p_count is not None else True)
        and (complete_p2p if expected_p2p_count is not None else len(failed_p2p) == 0)
    )
    return {
        "available": True,
        "valid_result": bool(shape_valid and identity_matches),
        "identity_matches": identity_matches,
        "error": item.get("error") or None,
        "f2p_now_passing_count": len(f2p),
        "f2p_required_count": expected_f2p_count,
        "p2p_failed_count": len(failed_p2p),
        "p2p_required_count": expected_p2p_count,
        "empty_official_p2p": empty_p2p,
        "official_passed_match": authoritative_match,
        "passed_match": passed_match,
    }


def _report_item(report: Any, requested_instance_id: str) -> dict[str, Any] | None:
    """Return the uniquely requested item from an official report."""
    if not isinstance(report, dict) or not isinstance(report.get("items"), list):
        return None
    matches = [
        item for item in report["items"]
        if isinstance(item, dict) and item.get("instance_id") == requested_instance_id
    ]
    return matches[0] if len(matches) == 1 else None


def run_official_infrastructure_gate(
    bundle: OfficialInstanceBundle,
    *,
    work_root: Path,
) -> dict[str, Any]:
    """Run official image eval privately. Never write gold into the repo."""

    result: dict[str, Any] = {
        "docker_available": _docker_available(),
        "image_name": bundle.image_name(),
        "image_pulled": False,
        "evaluator_commit": OFFICIAL_EVALUATOR_COMMIT,
        "baseline": {"ran": False},
        "gold": {"ran": False},
        "verifier_environment_ready": False,
        "verifier_baseline_valid": False,
        "verifier_gold_valid": False,
        "reason": None,
    }
    if not result["docker_available"]:
        result["reason"] = "docker_unavailable"
        return result
    image = bundle.image_name()
    if not image:
        result["reason"] = "missing_official_image_name"
        return result
    if not bundle.test_patch().strip() or not bundle.gold_patch().strip():
        result["reason"] = "missing_official_patch_or_test_patch"
        return result
    pulled, pull_detail = _docker_pull(image)
    result["image_pulled"] = pulled
    if not pulled:
        result["reason"] = f"docker_pull_failed: {pull_detail}"
        return result

    private = work_root / "official-eval-private"
    private.mkdir(parents=True, exist_ok=True)
    empty_p2p = len(bundle.hidden_tests()[1]) == 0
    try:
        ensure_official_evaluator()
        baseline_json = private / "baseline.json"
        gold_json = private / "gold.json"
        _write_isolated_spec(baseline_json, bundle, use_gold=False)
        _write_isolated_spec(gold_json, bundle, use_gold=True)
        baseline_report = private / "baseline_report.json"
        gold_report = private / "gold_report.json"
        baseline = _run_official_eval(baseline_json, baseline_report, private)
        gold = _run_official_eval(gold_json, gold_report, private)
        baseline_item = None
        gold_item = None
        baseline_item = _report_item(baseline.get("report"), bundle.public.instance_id)
        gold_item = _report_item(gold.get("report"), bundle.public.instance_id)
        result["baseline"] = {
            "ran": True,
            **_summarize_item(
                baseline_item,
                empty_p2p=empty_p2p,
                requested_instance_id=bundle.public.instance_id,
                expected_f2p_count=len(bundle.hidden_tests()[0]),
                expected_p2p_count=len(bundle.hidden_tests()[1]),
            ),
            "process_exit_code": baseline["exit_code"],
        }
        result["gold"] = {
            "ran": True,
            **_summarize_item(
                gold_item,
                empty_p2p=empty_p2p,
                requested_instance_id=bundle.public.instance_id,
                expected_f2p_count=len(bundle.hidden_tests()[0]),
                expected_p2p_count=len(bundle.hidden_tests()[1]),
            ),
            "process_exit_code": gold["exit_code"],
        }
        # Baseline is valid when hidden F2P are not all passing (bug still present)
        # and official P2P either empty or not all failed as an environment crash.
        baseline_f2p_passing = int(result["baseline"].get("f2p_now_passing_count") or 0)
        baseline_error = result["baseline"].get("error")
        gold_ok = bool(result["gold"].get("passed_match")) and not result["gold"].get("error")
        baseline_ok = (
            bool(result["baseline"].get("valid_result"))
            and baseline_error in {None, ""}
            and baseline_f2p_passing == 0
        )
        if empty_p2p:
            p2p_ok = True
        else:
            p2p_ok = int(result["baseline"].get("p2p_failed_count") or 0) == 0
        result["verifier_environment_ready"] = bool(
            result["baseline"].get("valid_result")
            and result["gold"].get("valid_result")
        )
        result["verifier_baseline_valid"] = bool(baseline_ok and p2p_ok)
        result["verifier_gold_valid"] = bool(gold_ok)
        if not result["verifier_baseline_valid"]:
            result["reason"] = "official_baseline_invalid"
        elif not result["verifier_gold_valid"]:
            result["reason"] = "official_gold_did_not_resolve"
        else:
            result["reason"] = None
    except Exception as exc:
        result["reason"] = f"official_eval_failed: {exc}"
    finally:
        subprocess.run(
            ["docker", "rmi", "-f", image],
            capture_output=True,
            check=False,
        )
    return result
