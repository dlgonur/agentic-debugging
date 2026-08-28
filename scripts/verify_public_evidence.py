"""Fail-closed public evidence gate for the repository's accepted claims.

The gate deliberately proves two different things without conflating them:

* the deterministic offline product path can reproduce, repair, independently
  verify, replay, and clean one representative curated case; and
* the tracked frozen R6 capsule still regenerates the professor-facing trace
  set byte-for-byte and passes its leakage audit.

Neither check contacts a provider.  The offline demonstration uses the
repository's scripted reference-repair policy, so it is infrastructure proof,
not evidence of live-model repair ability.
"""

from __future__ import annotations

import argparse
import json
import re
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

# Running ``python scripts/verify_public_evidence.py`` places ``scripts/`` on
# sys.path, not necessarily the checkout root required by the ``experiments``
# namespace used by the accepted leakage authority.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agentic_debugger.demo.policies import DEMO_POLICIES
from agentic_debugger.demo.runner import DemoCaseStatus, run_demo
from agentic_debugger.evaluation.professor_trace_r6 import (
    EvidenceResolver,
    FROZEN_HOLDOUT_EVIDENCE,
    FROZEN_VALIDATION_EVIDENCE,
    export_professor_traces_r6,
    verify_evidence,
)


SCHEMA_VERSION = "public-evidence-attestation-v1"
DEMO_TASK_ID = "curated-off-by-one-002"
R6_SOURCE_COMMIT = "4610785713832daaba6aa133374506a2d200391a"

CAPSULE_ROOT = (
    REPOSITORY_ROOT
    / "experiments"
    / "r6_debugger_training"
    / "runs"
    / "frozen"
)
PROFESSOR_TRACE_ROOT = REPOSITORY_ROOT / "docs" / "professor_traces"

_MACHINE_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]", re.IGNORECASE),
    re.compile(r"\\\\wsl(?:\.localhost)?\\", re.IGNORECASE),
    re.compile(r"/home/[^/]+/", re.IGNORECASE),
    re.compile(r"/Users/[^/]+/", re.IGNORECASE),
    re.compile(r"/tmp/", re.IGNORECASE),
)


def _json_files(root: Path, *, omit_schema: bool = False) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in sorted(root.rglob("*.json")):
        if omit_schema and path.name == "professor_debug_trace_schema_v1.json":
            continue
        files[path.relative_to(root).as_posix()] = path
    return files


def _assert_byte_identical_export(generated_root: Path) -> tuple[str, ...]:
    generated = _json_files(generated_root)
    accepted = _json_files(PROFESSOR_TRACE_ROOT, omit_schema=True)
    if generated.keys() != accepted.keys():
        missing = sorted(accepted.keys() - generated.keys())
        extra = sorted(generated.keys() - accepted.keys())
        raise RuntimeError(
            f"professor trace file set mismatch; missing={missing}, extra={extra}"
        )
    mismatched = [
        relative
        for relative in generated
        if generated[relative].read_bytes() != accepted[relative].read_bytes()
    ]
    if mismatched:
        raise RuntimeError(
            f"professor trace byte comparison failed: {mismatched}"
        )
    return tuple(generated)


def _assert_portable_public_json(paths: Sequence[Path]) -> None:
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for pattern in _MACHINE_PATH_PATTERNS:
            if pattern.search(text):
                raise RuntimeError(
                    f"machine-local path leaked into public evidence: {path.name}"
                )


def _repository_git_state() -> dict[str, Any]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=True,
        ).stdout.decode("ascii", "strict").strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=True,
        ).stdout.decode("utf-8", "strict")
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise RuntimeError(f"repository Git identity could not be read: {exc}") from exc
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise RuntimeError("public evidence must be bound to a valid Git HEAD")
    return {
        "git_head": head,
        "git_working_tree_dirty": bool(status),
        "git_status_digest": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def _verify_offline_product_path(workspace_parent: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    workspace_parent.mkdir(parents=True, exist_ok=False)
    report = run_demo(
        repository_root=REPOSITORY_ROOT,
        workspace_parent=workspace_parent,
        task_ids=(DEMO_TASK_ID,),
        policies=DEMO_POLICIES,
    )
    cases = tuple(report.cases)
    expected_case_count = len(DEMO_POLICIES)
    if len(cases) != expected_case_count:
        raise RuntimeError(
            f"offline demo case count mismatch: expected {expected_case_count}, got {len(cases)}"
        )

    failures: list[str] = []
    for case in cases:
        verifier = case.verifier
        trajectory = case.trajectory
        offline = case.offline
        valid = (
            case.status is DemoCaseStatus.COMPLETED
            and verifier.get("status") == "COMPLETED"
            and verifier.get("outcome") == "RESOLVED"
            and verifier.get("baseline_valid") is True
            and verifier.get("f2p_passed") == verifier.get("f2p_total")
            and verifier.get("p2p_passed") == verifier.get("p2p_total")
            and verifier.get("workspace_cleaned") is True
            and verifier.get("canonical_fixture_unchanged") is True
            and trajectory.get("replay_valid") is True
            and offline.get("canonical_fixture_unchanged_by_controller") is True
            and offline.get("network_attempts") == 0
            and offline.get("provider_attempts") == 0
        )
        if not valid:
            failures.append(f"{case.task_id}:{case.policy}")
    if failures:
        raise RuntimeError(f"offline evidence cases failed: {failures}")

    product_record = {
        "task_id": DEMO_TASK_ID,
        "case_count": len(cases),
        "policies": [case.policy for case in cases],
        "authoritative_outcomes": [case.verifier["outcome"] for case in cases],
        "fail_to_pass": [
            f"{case.verifier['f2p_passed']}/{case.verifier['f2p_total']}"
            for case in cases
        ],
        "pass_to_pass": [
            f"{case.verifier['p2p_passed']}/{case.verifier['p2p_total']}"
            for case in cases
        ],
        "trajectory_replay_valid": all(
            case.trajectory["replay_valid"] is True for case in cases
        ),
        "workspace_cleanup_proven": all(
            case.verifier["workspace_cleaned"] is True for case in cases
        ),
        "canonical_fixture_unchanged": all(
            case.verifier["canonical_fixture_unchanged"] is True for case in cases
        ),
        "observed_network_attempts": sum(
            int(case.offline["network_attempts"]) for case in cases
        ),
        "observed_provider_attempts": sum(
            int(case.offline["provider_attempts"]) for case in cases
        ),
    }
    environment = {
        "git_head": report.environment.get("git_head"),
        "git_working_tree_dirty": report.environment.get("git_working_tree_dirty"),
        "git_status_digest": report.environment.get("git_status_digest"),
        "source_tree_sha256": report.environment.get("source_tree_sha256"),
        "python_version": report.environment.get("python_version"),
        "platform": report.environment.get("platform"),
    }
    return product_record, environment


def verify_public_evidence(*, allow_dirty: bool = False) -> dict[str, Any]:
    initial_git = _repository_git_state()
    if initial_git["git_working_tree_dirty"] and not allow_dirty:
        raise RuntimeError(
            "repository has uncommitted changes; commit or pass --allow-dirty "
            "for a non-release development attestation"
        )
    if not CAPSULE_ROOT.is_dir():
        raise RuntimeError(f"tracked frozen R6 capsule is missing: {CAPSULE_ROOT}")
    if not PROFESSOR_TRACE_ROOT.is_dir():
        raise RuntimeError(
            f"checked-in professor trace set is missing: {PROFESSOR_TRACE_ROOT}"
        )

    resolver = EvidenceResolver(CAPSULE_ROOT)
    evidence_paths = verify_evidence(resolver, include_holdout=True)

    with tempfile.TemporaryDirectory(prefix="agentic-public-evidence-") as temp_text:
        temporary_root = Path(temp_text)
        generated_root = temporary_root / "professor-traces"
        artifacts = export_professor_traces_r6(
            CAPSULE_ROOT,
            generated_root,
            include_holdout=True,
            source_commit_sha=R6_SOURCE_COMMIT,
        )
        matched_files = _assert_byte_identical_export(generated_root)
        generated_files = _json_files(generated_root)
        _assert_portable_public_json(
            [CAPSULE_ROOT / "capsule_manifest.json", *generated_files.values()]
        )

        audit = json.loads(
            Path(artifacts["audit_report"]).read_text(encoding="utf-8")
        )
        if audit.get("passed") is not True or audit.get("total_findings") != 0:
            raise RuntimeError("professor-safe leakage audit did not pass cleanly")

        product_record, environment = _verify_offline_product_path(
            temporary_root / "demo-workspaces"
        )
        final_git = _repository_git_state()
        if final_git != initial_git:
            raise RuntimeError("repository Git state changed while evidence was verified")
        if environment.get("git_head") != initial_git["git_head"]:
            raise RuntimeError("offline demo Git HEAD disagrees with the evidence gate")
        dirty = initial_git["git_working_tree_dirty"]
        if environment.get("git_working_tree_dirty") is not dirty:
            raise RuntimeError("repository dirty state was not recorded")
        environment.update(initial_git)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "VERIFIED_DIRTY" if dirty else "VERIFIED",
        "release_eligible": not dirty,
        "repository": environment,
        "offline_product_path": product_record,
        "frozen_r6_evidence": {
            "evidence_records_verified": len(evidence_paths),
            "validation_records": len(FROZEN_VALIDATION_EVIDENCE),
            "partial_holdout_records": len(FROZEN_HOLDOUT_EVIDENCE),
            "professor_json_files_byte_identical": len(matched_files),
            "leakage_audit_passed": True,
            "leakage_findings": 0,
            "professor_exports_and_capsule_manifest_machine_paths_absent": True,
        },
        "claim": (
            "The checked repository state demonstrates the offline repair "
            "infrastructure chain and preserves the accepted frozen R6 "
            "evidence chain of custody."
        ),
        "limitations": [
            (
                "The offline product check uses a deterministic scripted "
                "reference-repair policy; it is infrastructure proof, not "
                "live-model performance evidence."
            ),
            (
                "The R6 check verifies and regenerates accepted frozen "
                "evidence; it does not rerun the external model campaign."
            ),
            (
                "Raw frozen ancillary records retain their original capture "
                "provenance, including machine-local paths. Portability is "
                "asserted only for the capsule manifest and professor-facing "
                "exports."
            ),
            (
                "The in-process offline guard records provider and network "
                "attempts in the demo process; it is not a system-wide "
                "network-isolation proof."
            ),
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the public offline repair path and frozen R6 evidence "
            "chain without contacting a model provider."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="optional path for the JSON attestation; the document is always printed",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "allow an explicitly non-release attestation from a dirty checkout; "
            "the status becomes VERIFIED_DIRTY"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        attestation = verify_public_evidence(allow_dirty=args.allow_dirty)
        document = (
            json.dumps(
                attestation,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        if args.output is not None:
            output = args.output.resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(document, encoding="utf-8", newline="\n")
        print(document, end="")
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI converts all gate failures
        failure = {
            "schema_version": SCHEMA_VERSION,
            "status": "FAILED",
            "error_type": type(exc).__name__,
            "diagnostic": str(exc),
        }
        print(
            json.dumps(failure, ensure_ascii=False, allow_nan=False, sort_keys=True),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
