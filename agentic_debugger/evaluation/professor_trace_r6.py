"""Professor-facing structured debugger trace export for the R6 evidence.

``professor_debug_trace_v1`` derives one clean, deterministic,
JSON-schema-validated trace per task from the REAL debugger executions of
the accepted R6 model evaluation.  This module is the deterministic exporter
behind ``docs/professor_traces``:

- PRIMARY set — the completed, accepted, contamination-safe checkpoint-30
  disjoint QuixBugs validation (8/8 independently verifier-confirmed
  RESOLVED).  This is the professor-facing R6 trace set.
- PARTIAL-HOLDOUT appendix — the two surviving completed rows of the final
  five-task curated holdout (RESOLVED and BREAKING_RESOLVED), exported under
  an explicit ``final_holdout_partial`` scope that is NEVER mixed into the
  primary success set.  The BREAKING_RESOLVED row is preserved honestly:
  F2P repaired, P2P regression remains, accepted outcome != RESOLVED.

Nothing is fabricated: every trace field is derived from the frozen
``debugger-interaction-v2-r5-evidence`` records (real model commands, real
tool actions, real stack/locals observations, real diagnoses, real candidate
hashes, real independent-verifier outcomes).  Absent evidence is exported
with explicit ``null`` / ``NOT_RECORDED`` / ``NOT_APPLICABLE`` semantics.

Anti-leakage: every exported trace passes an actual-output professor-safe
audit.  The audit derives forbidden content mechanically from the hidden
test assets (reusing the accepted ``anti_leakage`` authority) and scans the
exported trace JSON as if it were a prompt; any hidden test source, node id,
assertion expression, expected literal, oracle root cause, reference repair
snippet, or chain-of-thought reconstruction is a FAIL-CLOSED error.  Hidden
per-test node ids are therefore never exported (counts only).

Determinism: identical frozen evidence produces byte-identical traces and
indexes (stable key order, ``sort_keys=True``, ``allow_nan=False``).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Optional

from agentic_debugger.evaluation.professor_trace import (
    _sha256,
    build_trace,
    validate_trace,
)

try:  # accepted anti-leakage authority (AUDIT ONLY; never prompt-facing)
    from experiments.debugger_interaction_v2_r5.anti_leakage import (
        ForbiddenContent,
        _evidence_diagnosis_texts,
        _hidden_test_assets,
        _strip_lines,
        derive_forbidden_content,
        scan_prompt,
    )
except ImportError:  # pragma: no cover - repository layout guard
    ForbiddenContent = None  # type: ignore[assignment]
    _evidence_diagnosis_texts = None  # type: ignore[assignment]
    _hidden_test_assets = None  # type: ignore[assignment]
    _strip_lines = None  # type: ignore[assignment]
    derive_forbidden_content = None  # type: ignore[assignment]
    scan_prompt = None  # type: ignore[assignment]

from agentic_debugger.evaluation.r6_evidence import (
    ADAPTER_CONFIG_SHA256,
    ADAPTER_MODEL_SHA256,
    BASE_REPOSITORY,
    BASE_REVISION,
    CURATED_ROOT,
    EVIDENCE_CONTRACT_SHA256,
    FINE_TUNED_CHECKPOINT,
    GOLD_DIFF_DIR,
    PYTHON_VERSION,
    SELECTED_ADAPTER_PATH,
    TRAINING_PROVENANCE,
    FROZEN_ANCILLARY,
    FROZEN_HOLDOUT_EVIDENCE,
    FROZEN_REGISTRY,
    FROZEN_VALIDATION_EVIDENCE,
    HOLDOUT_OUTCOMES,
    VALIDATION_OUTCOMES,
    EvidenceRegistry,
    EvidenceResolver,
    _load_capsule_manifest,
    _registry_evidence_sha,
    _verify_capsule_chain_of_custody,
    resolve_default_evidence_root,
    verify_evidence,
)
from agentic_debugger.evaluation.r6_audit import (
    _audit_trace,
    _fixture_available,
    _fixture_dir_for,
    _frozen_forbidden_content,
    _gold_diff_added_lines,
    _observed_production_function_names,
    audit_exported_text,
    derive_forbidden_content_scoped,
)
from agentic_debugger.evaluation.r6_trace import (
    _logical_identity,
    _model_identity_r6,
    _relative_posix,
    _safe_event,
    _stable_json,
    _write_json,
    build_index_r6,
    build_trace_r6,
    VALIDATION_STAGE_BY_TASK,
)

SCHEMA_VERSION = "professor_debug_trace_v1"
TRACE_FILE_PREFIX = "professor_debug_trace_"

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_professor_traces_r6(
    evidence_root: Path,
    output_dir: Path,
    *,
    include_holdout: bool = True,
    source_commit_sha: str = "4610785713832daaba6aa133374506a2d200391a",
    registry: Optional[EvidenceRegistry] = None,
    curated_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Deterministically export the professor-facing R6 trace set.

    Steps, all fail-closed:
      1. verify frozen evidence identity (hashes, outcomes, contract,
         capsule chain of custody);
      2. build one schema-validated trace per task;
      3. professor-safe leakage audit over every exported trace text;
      4. write traces, indexes, and manifests deterministically with
         portable logical evidence identities (no machine-local paths).

    ``registry`` and ``curated_root`` are injection points for focused
    tests; production always uses the frozen accepted registry and the
    tracked curated fixtures.
    """
    registry = registry or FROZEN_REGISTRY
    curated_root = curated_root or CURATED_ROOT
    resolver = EvidenceResolver(evidence_root)
    evidence_paths = verify_evidence(
        resolver,
        include_holdout=include_holdout,
        registry=registry,
    )
    capsule_manifest = _load_capsule_manifest(resolver)
    # Accepted holdout status authority (verified ancillary record).
    holdout_report_path = resolver.ancillary_path("holdout_report")
    holdout_report = (
        json.loads(holdout_report_path.read_text(encoding="utf-8"))
        if holdout_report_path is not None
        else None
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    validation_dir = output_dir / "r6_validation"
    holdout_dir = output_dir / "r6_holdout_partial"
    for subdir in (validation_dir, holdout_dir):
        subdir.mkdir(parents=True, exist_ok=True)

    artifacts: dict[str, Any] = {"traces": {}, "indexes": {}, "audits": {}}
    audit_results: dict[str, dict[str, Any]] = {}

    # --- primary validation set ---------------------------------------------
    validation_traces: list[dict[str, Any]] = []
    validation_paths: dict[str, str] = {}
    for task_id in sorted(registry.validation):
        evidence = json.loads(
            evidence_paths[f"validation:{task_id}"].read_text(encoding="utf-8")
        )
        trace = build_trace_r6(
            evidence, scope="validation", model_identity=_model_identity_r6()
        )
        audit = _audit_trace(
            trace, task_id, evidence,
            curated_root=curated_root, resolver=resolver,
        )
        audit_results[f"validation:{task_id}"] = audit
        if not audit["passed"]:
            raise RuntimeError(
                f"professor-safe audit FAILED for {task_id!r}: "
                f"{json.dumps(audit['leakage_findings'])[:500]}"
            )
        path = validation_dir / f"{TRACE_FILE_PREFIX}{task_id}.json"
        _write_json(path, trace)
        validation_traces.append(trace)
        validation_paths[task_id] = _relative_posix(path, output_dir)
        artifacts["traces"][f"validation:{task_id}"] = str(path)
    index = build_index_r6(
        validation_traces,
        validation_paths,
        scope="validation",
        source_commit_sha=source_commit_sha,
    )
    index["final_holdout_status"] = "INCOMPLETE_HARDWARE_STOP"
    index["distinct_scopes"] = (
        "The 8 validation traces in r6_validation/ are the completed "
        "contamination-safe disjoint validation cohort.  The two partial "
        "final-holdout traces in r6_holdout_partial/ are a SEPARATE, "
        "hardware-interrupted scope and are not part of this success set."
    )
    index_path = output_dir / "r6_validation_index.json"
    _write_json(index_path, index)
    artifacts["indexes"]["validation"] = str(index_path)

    # --- partial holdout appendix --------------------------------------------
    holdout_traces: list[dict[str, Any]] = []
    holdout_paths: dict[str, str] = {}
    if include_holdout:
        for task_id in sorted(registry.final_holdout_partial):
            evidence = json.loads(
                evidence_paths[f"final_holdout_partial:{task_id}"].read_text(
                    encoding="utf-8"
                )
            )
            trace = build_trace_r6(
                evidence,
                scope="final_holdout_partial",
                model_identity=_model_identity_r6(),
            )
            audit = _audit_trace(
                trace, task_id, evidence,
                curated_root=curated_root, resolver=resolver,
            )
            audit_results[f"final_holdout_partial:{task_id}"] = audit
            if not audit["passed"]:
                raise RuntimeError(
                    f"professor-safe audit FAILED for {task_id!r}: "
                    f"{json.dumps(audit['leakage_findings'])[:500]}"
                )
            path = holdout_dir / f"{TRACE_FILE_PREFIX}{task_id}.json"
            _write_json(path, trace)
            holdout_traces.append(trace)
            holdout_paths[task_id] = _relative_posix(path, output_dir)
            artifacts["traces"][f"final_holdout_partial:{task_id}"] = str(path)
        holdout_index = build_index_r6(
            holdout_traces,
            holdout_paths,
            scope="final_holdout_partial",
            source_commit_sha=source_commit_sha,
        )
        holdout_index["final_holdout_status"] = "INCOMPLETE_HARDWARE_STOP"
        holdout_index["incomplete_tasks"] = {
            "curated-wrong-branch-003": (
                "interrupted during a model request (host power loss)"
            ),
            "curated-mutation-alias-004": "not started (host power loss)",
            "curated-caller-callee-005": "not started (host power loss)",
        }
        holdout_index["breaking_resolved_row"] = (
            "curated-off-by-one-002: fail-to-pass repaired (1/1), "
            "pass-to-pass regression remains (1/2); accepted verifier "
            "outcome BREAKING_RESOLVED != RESOLVED — the independent "
            "verifier rejected an apparently useful repair."
        )
        holdout_index_path = output_dir / "r6_holdout_partial_index.json"
        _write_json(holdout_index_path, holdout_index)
        artifacts["indexes"]["final_holdout_partial"] = str(holdout_index_path)

    # --- audit report -----------------------------------------------------------
    audit_report = {
        "schema_version": "professor_trace_leakage_audit_v1",
        "generated_from_source_commit": source_commit_sha,
        "audit_authority": (
            "experiments.debugger_interaction_v2_r5.anti_leakage "
            "(accepted actual-output scanner; fail closed)"
        ),
        "scanned_documents": len(audit_results),
        "total_findings": sum(
            len(a.get("leakage_findings") or []) for a in audit_results.values()
        ),
        "passed": all(a.get("passed") is True for a in audit_results.values()),
        "per_document": audit_results,
    }
    audit_path = output_dir / "professor_safe_audit.json"
    _write_json(audit_path, audit_report)
    artifacts["audit_report"] = str(audit_path)

    # --- source-evidence manifest (portable logical identities only) ----------
    manifest: dict[str, Any] = {
        "schema_version": "professor_trace_source_evidence_manifest_v1",
        "generated_from_source_commit": source_commit_sha,
        "evidence_scope": "r6-frozen-accepted-evidence",
        "treatment_contract_sha256": EVIDENCE_CONTRACT_SHA256,
        "selected_checkpoint": FINE_TUNED_CHECKPOINT,
        "adapter_model_sha256": ADAPTER_MODEL_SHA256,
        "adapter_config_sha256": ADAPTER_CONFIG_SHA256,
        "evidence_source": (
            "tracked frozen evidence capsule "
            "experiments/r6_debugger_training/runs/frozen "
            "(raw evidence SHA256 identities; machine-local capture paths "
            "are intentionally not exported)"
        ),
        "evidence": {},
        "ancillary": {},
    }
    for scope_group, group in (
        ("validation", registry.validation),
        ("final_holdout_partial", registry.final_holdout_partial),
    ):
        for task_id, expected in sorted(group.items()):
            if scope_group == "final_holdout_partial" and not include_holdout:
                continue
            manifest["evidence"][f"{scope_group}:{task_id}"] = {
                "logical_identity": _logical_identity(
                    scope_group, task_id, capsule_manifest
                ),
                "raw_evidence_sha256": expected,
            }
    for key, expected in sorted(registry.ancillary.items()):
        manifest["ancillary"][key] = {
            "logical_identity": f"ancillary/{key}",
            "sha256": expected,
        }
    if holdout_report is not None:
        holdout_pin = registry.ancillary.get("holdout_report")
        if holdout_pin:
            manifest["holdout_status_authority"] = {
                "logical_identity": "ancillary/holdout_report",
                "run_status": holdout_report.get("run_status"),
                "sha256": holdout_pin,
            }
    manifest_path = output_dir / "source_evidence_manifest.json"
    _write_json(manifest_path, manifest)
    artifacts["manifest"] = str(manifest_path)

    # --- trace SHA manifest -----------------------------------------------------
    sha_manifest: dict[str, Any] = {
        "schema_version": "professor_trace_sha_manifest_v1",
        "generated_from_source_commit": source_commit_sha,
        "traces": {
            key: hashlib.sha256(Path(path_text).read_bytes()).hexdigest()
            for key, path_text in artifacts["traces"].items()
        },
    }
    sha_manifest_path = output_dir / "trace_sha_manifest.json"
    _write_json(sha_manifest_path, sha_manifest)
    artifacts["sha_manifest"] = str(sha_manifest_path)

    return artifacts


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Export the professor-facing R6 debugger trace set "
            "(professor_debug_trace_v1) deterministically from frozen "
            "accepted evidence"
        )
    )
    parser.add_argument(
        "--evidence-root",
        type=str,
        default=None,
        help=(
            "accepted evidence root; default resolves the tracked frozen "
            "evidence capsule, then the accepted review package, then "
            "the live run trees"
        ),
    )
    parser.add_argument("--output-dir", type=str, required=True)
    parser.add_argument(
        "--no-holdout",
        action="store_true",
        help="skip the partial final-holdout appendix",
    )
    parser.add_argument(
        "--source-commit",
        type=str,
        default="4610785713832daaba6aa133374506a2d200391a",
    )
    args = parser.parse_args()

    evidence_root = (
        Path(args.evidence_root).resolve()
        if args.evidence_root
        else resolve_default_evidence_root()
    )
    artifacts = export_professor_traces_r6(
        evidence_root,
        Path(args.output_dir),
        include_holdout=not args.no_holdout,
        source_commit_sha=args.source_commit,
    )
    print(
        json.dumps(
            {
                "status": "COMPLETE",
                "validation_trace_count": len(FROZEN_VALIDATION_EVIDENCE),
                "holdout_trace_count": (
                    0 if args.no_holdout else len(FROZEN_HOLDOUT_EVIDENCE)
                ),
                "total_trace_count": (
                    len(FROZEN_VALIDATION_EVIDENCE)
                    + (0 if args.no_holdout else len(FROZEN_HOLDOUT_EVIDENCE))
                ),
                "output_dir": str(args.output_dir),
                "manifest": artifacts["manifest"],
                "sha_manifest": artifacts["sha_manifest"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
