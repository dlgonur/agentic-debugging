"""S1-P — frozen patch serialization normalization diagnostic (post-hoc).

Question under test:

    Would the ORIGINAL frozen model-produced repair from S1-P Live Run 1
    satisfy the SAME frozen model-generated regression test and the
    independent EvaluationVerifier after ONLY deterministic
    patch-serialization normalization?

This is NOT a new model attempt, NOT a fix request, NOT prompt tuning,
NOT a new generated test, and NOT semantic patch editing. No model call.
The normalizer (``serialization_normalizer``) only removes/re-derives
serialization metadata that PatchManager does not support; semantic hunk
content is proven byte-identical before vs after.

Pipeline (all deterministic, offline):

    1. verify the original live evidence identity and hashes
       (task, source commit, model, frozen test SHA-256, raw response
       SHA-256, candidate patch SHA-256, contract SHA-256);
    2. replay the ORIGINAL live rejection on the raw candidate patch
       (PatchManager: "Git metadata lines are not supported");
    3. record the N1-only intermediate (git metadata removed but hunk
       header counts untouched) and its remaining PatchManager error;
    4. normalize the ORIGINAL candidate patch (N1 + N2), recording every
       operation and the semantic-hunk identity hash before/after;
    5. apply ONLY the normalized patch in a disposable workspace and run
       the SAME frozen generated test (``generated_test_runner.run_fixed``);
    6. independently run EvaluationVerifier on the normalized patch;
    7. record canonical-fixture immutability and workspace cleanup.

The original live-run evidence file is never modified; this diagnostic
writes a unique task-owned evidence directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.runtime.patcher import PatchValidationError, _parse_unified_diff

from experiments.model_generated_test_probe import probe as probe_mod
from experiments.model_generated_test_probe import test_generation as tg
from experiments.model_generated_test_probe.generated_test_runner import run_fixed
from experiments.model_generated_test_probe.serialization_normalizer import (
    NormalizationError,
    normalize_patch,
    semantic_hunk_lines,
    strip_git_metadata_only,
)

CONTRACT_PATH = THIS_FILE.with_name("experiment_contract.json")
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"
TASK_ID = "curated-none-handling-001"
FIXTURE_DIR = CURATED_ROOT / TASK_ID

# ---------------------------------------------------------------------------
# Frozen identity of S1-P Live Run 1 (verified against the live evidence.json
# and the frozen experiment contract; see TASK 1 of the task prompt).
# ---------------------------------------------------------------------------

EXPECTED_SOURCE_COMMIT_SHA = "c47be60e6919626b6f431cd337d1d847a97f0722"
EXPECTED_TASK_ID = "curated-none-handling-001"
EXPECTED_MODEL_BASE_REPOSITORY = "Qwen/Qwen2.5-Coder-7B-Instruct"
EXPECTED_MODEL_BASE_REVISION = "c03e6d358207e414f1eca0bb1891e29f1db0e242"
EXPECTED_MODEL_CONDITION = "RAW_BASE"
EXPECTED_CONTRACT_SHA256 = "3d7c7e8d5ab17f523e4a03d20282363c6801dec8ef0a2a44bdd4b75934685bd0"
EXPECTED_FROZEN_TEST_SHA256 = "713c2b800a568a092a4e6bf2aecbd614ebf83e8b104bbe0786a9791ebadc5737"
EXPECTED_CANDIDATE_PATCH_SHA256 = "81b0aa096d6dad00cf15f9b105d28e6969d44077d28ade7ef065e253ecf90f2b"
EXPECTED_RAW_FIX_RESPONSE_SHA256 = "a3a8d0bb1c1ef042ac170b24a6e63b213d046af69cb3bbeda8c00562b788542f"

DEFAULT_EVIDENCE_PATH = (
    REPO_ROOT.parent / "agentic-debugging-internship" / "AI_REVIEW"
    / "s1p_model_generated_test_probe_2026-08-10" / "live-run-1" / "evidence.json"
)

_LIVE_REJECTION = "PatchValidationError: Git metadata lines are not supported"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Evidence identity verification (TASK 1 gate)
# ---------------------------------------------------------------------------


def verify_evidence(evidence: Dict[str, Any], contract: Dict[str, Any]) -> Dict[str, Any]:
    """Verify the internal run identity and hashes of the live evidence.

    Returns a list of checks: {name, expected, actual, ok}. Any ``ok=False``
    means the diagnostic must STOP (do not reconstruct evidence).
    """

    identity = evidence.get("run_identity", {})
    mfc = evidence.get("model_fixed_code") or {}
    frozen = (evidence.get("test_generation") or {}).get("frozen_test") or {}
    raw_patch = mfc.get("candidate_patch") or ""
    raw_fix_response = mfc.get("raw_model_response") or ""
    frozen_source = frozen.get("source") or ""

    checks: list[Dict[str, Any]] = []
    add = lambda name, expected, actual: checks.append({
        "name": name, "expected": expected, "actual": actual,
        "ok": expected == actual,
    })

    add("task_id", EXPECTED_TASK_ID, identity.get("task_id"))
    add("source_commit_sha", EXPECTED_SOURCE_COMMIT_SHA, identity.get("source_commit_sha"))
    add("model_condition", EXPECTED_MODEL_CONDITION, identity.get("model_condition"))
    add("model_base_repository", EXPECTED_MODEL_BASE_REPOSITORY,
        identity.get("base_repository"))
    add("model_base_revision", EXPECTED_MODEL_BASE_REVISION,
        identity.get("base_revision"))
    add("adapter_applied_false", False, identity.get("adapter_applied"))
    add("rag_enabled_false", False, identity.get("rag_enabled"))
    add("experiment_contract_sha256", EXPECTED_CONTRACT_SHA256,
        identity.get("experiment_contract_sha256"))
    add("experiment_contract_sha256_recomputed",
        probe_mod._contract_sha256(contract), EXPECTED_CONTRACT_SHA256)
    add("frozen_test_sha256_recorded", EXPECTED_FROZEN_TEST_SHA256,
        frozen.get("sha256"))
    add("frozen_test_sha256_recomputed", EXPECTED_FROZEN_TEST_SHA256,
        _sha256(frozen_source))
    add("candidate_patch_sha256_recorded", EXPECTED_CANDIDATE_PATCH_SHA256,
        mfc.get("candidate_patch_sha256"))
    add("candidate_patch_sha256_recomputed", EXPECTED_CANDIDATE_PATCH_SHA256,
        _sha256(raw_patch))
    add("raw_fix_response_sha256_recorded", EXPECTED_RAW_FIX_RESPONSE_SHA256,
        mfc.get("raw_response_sha256"))
    add("raw_fix_response_sha256_recomputed", EXPECTED_RAW_FIX_RESPONSE_SHA256,
        _sha256(raw_fix_response))
    add("behavior_spec_sha256", contract.get("behavior_spec", {}).get("sha256"),
        identity.get("behavior_spec_sha256"))
    add("system_prompt_generation_sha256",
        contract.get("prompts", {}).get("system_prompt_generation_sha256"),
        identity.get("system_prompt_generation_sha256"))
    add("system_prompt_fix_sha256",
        contract.get("prompts", {}).get("system_prompt_fix_sha256"),
        identity.get("system_prompt_fix_sha256"))
    add("fixture_tree_sha256", contract.get("task", {}).get("fixture_tree_sha256"),
        probe_mod._fixture_tree_sha256(FIXTURE_DIR))
    add("generated_test_eval_used_frozen_test",
        EXPECTED_FROZEN_TEST_SHA256,
        (evidence.get("generated_test_eval") or {}).get("frozen_test_sha256"))
    add("buggy_run_used_frozen_test", EXPECTED_FROZEN_TEST_SHA256,
        (evidence.get("buggy_run") or {}).get("frozen_test_sha256"))

    ok = all(c["ok"] for c in checks)
    return {"all_ok": ok, "checks": checks}


# ---------------------------------------------------------------------------
# Deterministic normalization provenance
# ---------------------------------------------------------------------------


def _operation_records(
    result: Any, n1_only_parse_error: Optional[str],
) -> Dict[str, Any]:
    return {
        "operations": [
            {
                "kind": op.kind,
                "line_index": op.line_index,
                "original_line": op.original_line,
                "replacement": op.replacement,
            }
            for op in result.operations
        ],
        "removed_lines": list(result.removed_lines),
        "semantic_hunk_before_sha256": result.semantic_hunk_before_sha256,
        "semantic_hunk_after_sha256": result.semantic_hunk_after_sha256,
        "semantic_hunks_identical": result.semantic_hunks_identical,
        "n1_only_remaining_parser_error": n1_only_parse_error,
        "normalized_patch_sha256": _sha256(result.normalized_patch),
    }


def _corrected_combined_usage(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Correct the known arithmetic slip in the live handoff totals.

    Live evidence per-request usage: generation 329+98=427, fix 447+83=530.
    Correct combined totals: prompt 776, completion 181, total 957.
    The original evidence file is NOT altered; the correction is recorded
    only in this diagnostic's reports.
    """

    gen = (evidence.get("test_generation") or {}).get("frozen_test") or {}
    fix = evidence.get("model_fixed_code") or {}
    g = gen.get("usage") or {}
    f = fix.get("usage") or {}
    prompt = int(g.get("prompt_tokens") or 0) + int(f.get("prompt_tokens") or 0)
    completion = int(g.get("completion_tokens") or 0) + int(f.get("completion_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "generation_usage": g,
        "fix_usage": f,
        "note": (
            "corrected from the live handoff arithmetic slip "
            "(handoff printed '854 total' for 776 prompt + 181 completion); "
            "the original evidence file was not altered"
        ),
    }


# ---------------------------------------------------------------------------
# Diagnostic execution (TASK 3)
# ---------------------------------------------------------------------------


def run_diagnostic(
    evidence: Dict[str, Any],
    output_dir: Path,
    *,
    timeout_seconds: int = 20,
) -> Dict[str, Any]:
    """Execute the post-hoc serialization diagnostic; write all evidence.

    Deterministic and offline. No model call. The original evidence file
    is never modified.
    """

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    contract = probe_mod._load_contract()
    verification = verify_evidence(evidence, contract)
    if not verification["all_ok"]:
        raise RuntimeError(
            "live evidence identity verification FAILED; diagnostic stopped "
            "(do not reconstruct evidence)"
        )

    frozen = evidence["test_generation"]["frozen_test"]
    mfc = evidence["model_fixed_code"]
    raw_patch: str = mfc["candidate_patch"]

    # 1. Replay the ORIGINAL live rejection (deterministic).
    live_rejection: Optional[str] = None
    try:
        _parse_unified_diff(raw_patch)
        live_rejection = "unexpected: raw patch parsed"
    except PatchValidationError as exc:
        live_rejection = f"PatchValidationError: {exc}"

    # 2. N1-only intermediate: git metadata removed, hunk headers untouched.
    n1_text, _n1_removed = strip_git_metadata_only(raw_patch)
    n1_parse_error: Optional[str] = None
    try:
        _parse_unified_diff(n1_text)
    except PatchValidationError as exc:
        n1_parse_error = f"PatchValidationError: {exc}"

    # 3. Deterministic normalization (N1 + N2).
    try:
        norm = normalize_patch(raw_patch)
    except NormalizationError as exc:
        raise RuntimeError(f"normalization insufficient: {exc}") from exc

    # 4. Frozen-test object (reconstructed exactly from live evidence).
    attempt = evidence["test_generation"]["attempts"][frozen["attempt_index"]]
    frozen_test = tg.FrozenTest(
        source=frozen["source"],
        sha256=frozen["sha256"],
        attempt_index=frozen["attempt_index"],
        raw_response_text=attempt["raw_response_text"],
        raw_response_sha256=frozen["raw_response_sha256"],
        system_prompt_sha256=evidence["test_generation"]["system_prompt_sha256"],
        user_prompt_sha256=frozen["user_prompt_sha256"],
        transport_error_category=attempt.get("transport_error_category"),
        usage=frozen["usage"],
        executability=frozen["executability"],
    )

    fixture_before = probe_mod._fixture_tree_sha256(FIXTURE_DIR)
    task = load_task(str(FIXTURE_DIR / "task.json"))

    case_parent = Path(tempfile.mkdtemp(prefix="s1p-diagnostic-"))
    case_dir = case_parent.resolve() / f"case-{TASK_ID}"
    case_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 5. Apply ONLY the normalized patch; run the SAME frozen test.
        fixed_run = run_fixed(
            frozen_test, norm.normalized_patch, FIXTURE_DIR.resolve(), case_dir,
            timeout_seconds=timeout_seconds,
        )
        fixed_run_dict = probe_mod._run_result_to_dict(fixed_run)

        # 6. Independent EvaluationVerifier (correctness authority).
        evaluation = EvaluationVerifier(
            str(REPO_ROOT), workspace_parent=str(case_dir)
        ).evaluate(task, norm.normalized_patch)
        verifier_dict = probe_mod._serialize_verifier(evaluation)
    finally:
        shutil.rmtree(case_parent, ignore_errors=True)

    fixture_after = probe_mod._fixture_tree_sha256(FIXTURE_DIR)

    # 7. Corrected token arithmetic (known handoff slip).
    corrected_usage = _corrected_combined_usage(evidence)

    summary: Dict[str, Any] = {
        "schema_version": "s1p-serialization-diagnostic-v1",
        "source_commit_sha": EXPECTED_SOURCE_COMMIT_SHA,
        "original_live_result": {
            "raw_patch": raw_patch,
            "raw_patch_sha256": mfc["candidate_patch_sha256"],
            "patch_manager_rejection": live_rejection,
            "resolved": False,
        },
        "post_hoc_serialization_diagnostic": {
            "normalized_patch": norm.normalized_patch,
            "normalized_patch_sha256": _sha256(norm.normalized_patch),
            "normalization": _operation_records(norm, n1_parse_error),
            "apply_result": {
                "patch_applied": fixed_run_dict["patch_applied"],
                "patch_error": fixed_run_dict["patch_error"],
                "status": fixed_run_dict["status"],
            },
            "frozen_generated_test": {
                "sha256": frozen_test.sha256,
                "expected_sha256": EXPECTED_FROZEN_TEST_SHA256,
                "sha_matches_live_run_1": frozen_test.sha256 == EXPECTED_FROZEN_TEST_SHA256,
                "result": {
                    "status": fixed_run_dict["status"],
                    "exit_code": fixed_run_dict["exit_code"],
                    "counts": fixed_run_dict["counts"],
                    "reason": fixed_run_dict["reason"],
                },
            },
            "verifier": verifier_dict,
            "verifier_f2p": {
                "f2p_total": verifier_dict.get("f2p_total"),
                "f2p_passed": verifier_dict.get("f2p_passed"),
                "p2p_total": verifier_dict.get("p2p_total"),
                "p2p_passed": verifier_dict.get("p2p_passed"),
                "outcome": verifier_dict.get("outcome"),
                "resolved": verifier_dict.get("outcome") == "RESOLVED",
            },
            "canonical_fixture_unchanged": fixture_before == fixture_after,
            "fixture_tree_sha256_before": fixture_before,
            "fixture_tree_sha256_after": fixture_after,
            "workspace_cleaned": bool(fixed_run_dict["workspace_cleaned"]),
            "verifier_workspace_cleaned": bool(verifier_dict.get("workspace_cleaned")),
        },
        "evidence_verification": verification,
        "corrected_combined_usage": corrected_usage,
    }

    _write_diagnostic_files(output_dir, evidence, norm, frozen_test,
                            fixed_run_dict, verifier_dict, summary,
                            n1_parse_error)
    return summary


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )


def _write_diagnostic_files(
    output_dir: Path,
    evidence: Dict[str, Any],
    norm: Any,
    frozen_test: tg.FrozenTest,
    fixed_run_dict: Dict[str, Any],
    verifier_dict: Dict[str, Any],
    summary: Dict[str, Any],
    n1_parse_error: Optional[str],
) -> None:
    identity = {
        "run_identity": evidence["run_identity"],
        "frozen_test": {
            "source": evidence["test_generation"]["frozen_test"]["source"],
            "sha256": evidence["test_generation"]["frozen_test"]["sha256"],
        },
        "model_fixed_code": {
            "raw_model_response": evidence["model_fixed_code"]["raw_model_response"],
            "raw_response_sha256": evidence["model_fixed_code"]["raw_response_sha256"],
            "candidate_patch_sha256": evidence["model_fixed_code"]["candidate_patch_sha256"],
            "usage": evidence["model_fixed_code"]["usage"],
        },
    }
    _write_json(output_dir / "original-evidence-summary.json", identity)
    _write_text(output_dir / "raw_candidate_patch.patch",
                evidence["model_fixed_code"]["candidate_patch"])
    _write_text(output_dir / "normalized_candidate_patch.patch",
                norm.normalized_patch)
    _write_json(output_dir / "normalization-operations.json",
                _operation_records(norm, n1_parse_error))
    _write_text(output_dir / "semantic-hunk-before.txt",
                "\n".join(semantic_hunk_lines(evidence["model_fixed_code"]["candidate_patch"])))
    _write_text(output_dir / "semantic-hunk-after.txt",
                "\n".join(semantic_hunk_lines(norm.normalized_patch)))
    _write_text(output_dir / "frozen_generated_test.py", frozen_test.source)
    _write_json(output_dir / "frozen-test-run.json", fixed_run_dict)
    _write_json(output_dir / "verifier-result.json", verifier_dict)
    _write_json(output_dir / "diagnostic-summary.json", summary)
    _write_text(output_dir / "diagnostic-report.md",
                _render_report(summary))


def _render_report(summary: Dict[str, Any]) -> str:
    live = summary["original_live_result"]
    diag = summary["post_hoc_serialization_diagnostic"]
    frozen = diag["frozen_generated_test"]
    verifier = diag["verifier_f2p"]
    norm_ops = diag["normalization"]
    lines = [
        "# S1-P — Frozen Patch Serialization Normalization Diagnostic",
        "",
        f"Source experiment commit: `{summary['source_commit_sha']}`",
        "",
        "## ORIGINAL LIVE RESULT (S1-P Live Run 1)",
        "",
        f"- raw patch → PatchManager rejection: `{live['patch_manager_rejection']}`",
        f"- raw patch SHA-256: `{live['raw_patch_sha256']}`",
        "- **NOT RESOLVED**",
        "",
        "## POST-HOC SERIALIZATION DIAGNOSTIC",
        "",
        "### Normalization operations (exact)",
        "",
    ]
    for op in norm_ops["operations"]:
        if op["replacement"] is None:
            lines.append(f"- `{op['kind']}` (line {op['line_index']}): "
                         f"removed `{op['original_line']}`")
        else:
            lines.append(f"- `{op['kind']}` (line {op['line_index']}): "
                         f"`{op['original_line']}` → `{op['replacement']}`")
    if norm_ops["n1_only_remaining_parser_error"]:
        lines.append("")
        lines.append("Intermediate (N1 only, git metadata removed, hunk headers "
                     "untouched): still rejected by PatchManager — "
                     f"`{norm_ops['n1_only_remaining_parser_error']}`")
    lines += [
        "",
        f"- semantic-hunk-before SHA-256: `{norm_ops['semantic_hunk_before_sha256']}`",
        f"- semantic-hunk-after SHA-256:  `{norm_ops['semantic_hunk_after_sha256']}`",
        f"- semantic hunks identical: **{norm_ops['semantic_hunks_identical']}**",
        f"- normalized patch SHA-256: `{diag['normalized_patch_sha256']}`",
        "",
        "### Apply + frozen generated test (same as Live Run 1)",
        "",
        f"- normalized patch applied: `{diag['apply_result']['patch_applied']}`",
        f"  (error: `{diag['apply_result']['patch_error']}`)",
        f"- frozen generated test SHA-256: `{frozen['sha256']}`",
        f"  matches Live Run 1: **{frozen['sha_matches_live_run_1']}**",
        f"- frozen generated test result: **{frozen['result']['status']}** "
        f"(exit {frozen['result']['exit_code']}, counts "
        f"{json.dumps(frozen['result']['counts'])})",
        "",
        "### Independent EvaluationVerifier",
        "",
        f"- status: `{verifier.get('outcome') or diag['verifier'].get('status')}` "
        f"(verifier status `{diag['verifier'].get('status')}`, "
        f"stop_reason `{diag['verifier'].get('stop_reason')}`)",
        f"- F2P: {verifier['f2p_passed']}/{verifier['f2p_total']} "
        f"P2P: {verifier['p2p_passed']}/{verifier['p2p_total']}",
        f"- RESOLVED: **{verifier['resolved']}**",
        f"- canonical fixture unchanged: {diag['canonical_fixture_unchanged']}",
        f"- workspace cleaned: {diag['workspace_cleaned']}; "
        f"verifier workspace cleaned: {diag['verifier_workspace_cleaned']}",
        "",
        "## Provenance",
        "",
        f"- evidence verification all_ok: {summary['evidence_verification']['all_ok']}",
        f"- corrected combined usage: {json.dumps(summary['corrected_combined_usage'])}",
        "",
        "This is post-hoc auxiliary evidence. The original live-run evidence "
        "file was not modified.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="S1-P frozen patch serialization normalization diagnostic"
    )
    parser.add_argument(
        "--evidence", type=str, default=str(DEFAULT_EVIDENCE_PATH),
        help="Path to the S1-P Live Run 1 evidence.json",
    )
    parser.add_argument(
        "--output-dir", type=str, required=True,
        help="Task-owned scientific evidence directory",
    )
    args = parser.parse_args()

    evidence_path = Path(args.evidence).resolve()
    if not evidence_path.is_file():
        print(json.dumps({
            "status": "FAIL",
            "reason": f"evidence file not found: {evidence_path}",
        }, indent=2, ensure_ascii=False))
        return 1

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    try:
        summary = run_diagnostic(evidence, Path(args.output_dir))
    except Exception as exc:  # noqa: BLE001 — fail-closed reporting
        print(json.dumps({
            "status": "FAIL",
            "reason": f"{type(exc).__name__}: {exc}",
        }, indent=2, ensure_ascii=False))
        return 1

    print(json.dumps({
        "status": "COMPLETE",
        "original_live_result": summary["original_live_result"],
        "post_hoc": {
            "normalized_patch_sha256":
                summary["post_hoc_serialization_diagnostic"]["normalized_patch_sha256"],
            "frozen_test_status": summary["post_hoc_serialization_diagnostic"]
                ["frozen_generated_test"]["result"]["status"],
            "verifier_outcome": summary["post_hoc_serialization_diagnostic"]
                ["verifier_f2p"]["outcome"],
            "verifier_resolved": summary["post_hoc_serialization_diagnostic"]
                ["verifier_f2p"]["resolved"],
        },
        "output_dir": str(Path(args.output_dir).resolve()),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
