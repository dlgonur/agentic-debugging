"""R6 trace and index construction.

This module owns the deterministic trace/index construction layer of
the R6 professor trace export: model identity, ``build_trace_r6``
(one schema-validated trace per task from the frozen evidence records,
absent evidence exported with explicit NOT_RECORDED/NOT_APPLICABLE
semantics), ``build_index_r6``, stable JSON, logical identities, and
relative-path helpers.  Identical frozen evidence produces
byte-identical output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from agentic_debugger.evaluation.professor_trace import (
    _sha256,
    build_trace,
    validate_trace,
)
from agentic_debugger.evaluation.r6_evidence import (
    ADAPTER_CONFIG_SHA256,
    ADAPTER_MODEL_SHA256,
    BASE_REVISION,
    EVIDENCE_CONTRACT_SHA256,
    FINE_TUNED_CHECKPOINT,
    PYTHON_VERSION,
    SELECTED_ADAPTER_PATH,
    TRAINING_PROVENANCE,
)

SCHEMA_VERSION = "professor_debug_trace_v1"
TRACE_FILE_PREFIX = "professor_debug_trace_"

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Trace building for the R6 export
# ---------------------------------------------------------------------------


def _model_identity_r6() -> dict[str, Any]:
    return {
        "fine_tuned_checkpoint": FINE_TUNED_CHECKPOINT,
        "adapter_identity_sha256": ADAPTER_MODEL_SHA256,
        "training_provenance": TRAINING_PROVENANCE,
    }


def build_trace_r6(
    evidence: dict[str, Any],
    *,
    scope: str,
    model_identity: dict[str, Any],
) -> dict[str, Any]:
    """Build one professor_debug_trace_v1 trace for the R6 export.

    ``scope`` is ``validation`` or ``final_holdout_partial`` and drives the
    trace-level provenance fields.  The
    trace body is derived from the frozen evidence through the accepted
    ``professor_trace.build_trace`` primitives (same command/observation/
    localization/diagnosis/repair/verification mapping as the accepted
    shared trace builder), with these differences:

    - hidden per-test node ids are NEVER exported (verifier counts only);
    - ``evidence_scope``, debugger lifecycle, checkpoint/runtime identity,
      serialization note and workspace cleanup are added;
    """
    base = build_trace(evidence, model_identity)  # schema-validated base
    gate_chain = (evidence.get("gate_results") or {}).get("gate_chain") or {}
    controller = evidence.get("controller_result") or {}
    serialization = evidence.get("serialization_normalization") or {}
    cleanup = evidence.get("cleanup") or {}
    verifier = evidence.get("verifier") or {}

    r6_scope = scope in ("validation", "final_holdout_partial")

    # Hidden-test stack frames are protected by the clean-holdout policy.
    # The model-facing stack rendering was filtered to the target
    # production region (original source lines of the production module),
    # so the professor trace keeps exactly that region: no hidden test
    # frames, no appended harness driver frames.
    task = evidence.get("task") or {}
    module_path = task.get("module_path")
    driver_start = task.get("runtime_appended_driver_start_line")
    for entry in base["debugger_trace"]:
        frames = entry.get("frames")
        if isinstance(frames, list):
            kept = [
                f
                for f in frames
                if isinstance(f, dict)
                and f.get("file") == module_path
                and (
                    driver_start is None
                    or f.get("line") is None
                    or f["line"] < driver_start
                )
            ]
            if kept:
                entry["frames"] = kept
            else:
                entry.pop("frames", None)

    debugger_lifecycle: dict[str, Any] = {
        "entered": gate_chain.get("G1") is not None,
        "terminal_path": bool(gate_chain.get("terminal_path")),
        "production_exception_path": bool(gate_chain.get("production_exception_path")),
        "step_outside_region": bool(gate_chain.get("step_outside_region")),
        "pause_generations": {
            "G1": gate_chain.get("G1"),
            "G2": gate_chain.get("G2"),
        },
        "gate_passed": gate_chain.get("passed"),
        "gate_reason": gate_chain.get("reason"),
        "tool_observations": [
            obs.get("name")
            for line in (evidence.get("trajectory_jsonl") or "").splitlines()
            if line.strip()
            for obs in [_safe_event(line)]
            if isinstance(obs, dict)
            and obs.get("event_type") == "observation"
            and obs.get("name")
        ],
    }

    # Rebuild final verification WITHOUT hidden per-test node ids.  Counts
    # (e.g. f2p "1/1", p2p "1/2") carry the honest verifier record; the
    # model-facing clean-holdout policy protected node ids, so professor
    # JSON must not reintroduce them.
    final_verification = {
        "outcome": verifier.get("outcome"),
        "verifier_status": verifier.get("status"),
        "f2p": f"{verifier.get('f2p_passed', 0)}/{verifier.get('f2p_total', 0)}",
        "p2p": f"{verifier.get('p2p_passed', 0)}/{verifier.get('p2p_total', 0)}",
        "full_suite": (
            "PASS"
            if verifier.get("full_suite_consistent") is True
            else verifier.get("full_suite_consistent")
        ),
        "syntax_passed": verifier.get("syntax_passed"),
        "canonical_fixture_unchanged": verifier.get("canonical_fixture_unchanged"),
        "workspace_lifecycle": verifier.get("workspace_lifecycle"),
        "candidate_sha256": verifier.get("candidate_sha256"),
    }

    run_provenance = dict(base["run_provenance"])
    run_provenance.update(
        {
            "selected_adapter_path": SELECTED_ADAPTER_PATH if r6_scope else None,
            "adapter_model_sha256": ADAPTER_MODEL_SHA256 if r6_scope else None,
            "adapter_config_sha256": ADAPTER_CONFIG_SHA256 if r6_scope else None,
            "evaluator_python_version": PYTHON_VERSION if r6_scope else None,
            "controller_final_state": controller.get("final_state"),
            "controller_stop_reason": controller.get("stop_reason"),
            "model_calls": controller.get("model_calls"),
            "diagnosis_provenance": evidence.get("diagnosis_provenance"),
        }
    )

    trace = {
        "schema_version": base["schema_version"],
        "task_id": base["task_id"],
        # The evidence bug_category is copied verbatim from the fixture's
        # oracle field (oracle_bug_category), which the accepted
        # clean-holdout policy protects from the model and therefore from
        # professor-facing output.  The public task identity is the
        # task_id; the category is NOT_RECORDED for export.
        "bug_category": None,
        "evidence_scope": scope,
        "debugger_path": base["debugger_path"],
        "model": base["model"],
        "treatment": base["treatment"],
        "run_provenance": run_provenance,
        "failure_reproduction": base["failure_reproduction"],
        "debugger_lifecycle": debugger_lifecycle,
        "debugger_trace": base["debugger_trace"],
        "error_localization": base["error_localization"],
        "diagnosis": base["diagnosis"],
        "repair_attempts": base["repair_attempts"],
        "serialization_normalization": {
            "required": serialization.get("note") is not None,
            "note": serialization.get("note"),
            "verifier_input_sha256": serialization.get("verifier_input_sha256"),
            "patchmanager_input_sha256": serialization.get(
                "patchmanager_input_sha256"
            ),
        },
        "final_verification": final_verification,
        "workspace_cleanup": {
            "release_pdb": (cleanup.get("release_pdb") or [])[:],
            "workspace_cleanup": cleanup.get("workspace_cleanup"),
        },
        "claims_boundary": (
            "Derived deterministically from the real accepted R6 "
            "final-execution evidence (checkpoint-30, treatment contract "
            f"{EVIDENCE_CONTRACT_SHA256}).  No hidden test source, hidden "
            "test node id, oracle field, chain-of-thought, or fabricated "
            "localization is included; the debugger path distinguishes the "
            "production-exception path (G2=None) from the normal G2 path "
            "exactly as the accepted gate classified it."
        ),
    }
    validate_trace(trace)
    return trace


def _safe_event(line: str) -> Any:
    try:
        return json.loads(line)
    except ValueError:
        return None


def build_index_r6(
    traces: list[dict[str, Any]],
    trace_paths: dict[str, str],
    *,
    scope: str,
    source_commit_sha: str,
) -> dict[str, Any]:
    """One concise professor-facing index over one trace scope.

    ``validation_result`` is derived from the trace list (never hardcoded).
    Each ``trace_sha256`` is a CONTENT hash: SHA256 of the stable compact
    serialization of the trace object, which equals SHA256 of the same
    stable serialization of the written trace file content — so the index
    hash corresponds 1:1 to the trace file content regardless of file
    indentation.
    """
    entries = []
    for trace in traces:
        entries.append(
            {
                "task_id": trace["task_id"],
                "bug_category": trace.get("bug_category"),
                "evidence_scope": trace.get("evidence_scope"),
                "error_localization": trace["error_localization"],
                "debugger_path": trace.get("debugger_path"),
                "model_turns": len(trace["debugger_trace"]),
                "repair_attempts": len(trace["repair_attempts"]),
                "verifier_outcome": trace["final_verification"]["outcome"],
                "trace_path": trace_paths.get(trace["task_id"]),
                "trace_sha256": _sha256(
                    json.dumps(
                        trace, sort_keys=True, ensure_ascii=False, allow_nan=False
                    )
                ),
            }
        )
    index: dict[str, Any] = {
        "schema_version": f"{SCHEMA_VERSION}_index",
        "generated_from_source_commit": source_commit_sha,
        "evidence_scope": scope,
        "trace_count": len(traces),
        "traces": entries,
    }
    if scope == "validation":
        resolved = sum(
            1
            for trace in traces
            if trace["final_verification"]["outcome"] == "RESOLVED"
        )
        index["selected_fine_tuned_checkpoint"] = FINE_TUNED_CHECKPOINT
        index["base_model_revision"] = BASE_REVISION
        index["validation_cohort_identity"] = {
            "treatment_contract_sha256": EVIDENCE_CONTRACT_SHA256,
            "adapter_model_sha256": ADAPTER_MODEL_SHA256,
        }
        index["validation_result"] = f"{resolved}/{len(traces)} RESOLVED"
        index["holdout_used_for_checkpoint_selection"] = False
    return index


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _relative_posix(path: Path, base: Path) -> str:
    """Output-dir-relative POSIX path — keeps regeneration byte-identical
    regardless of the absolute output location."""
    return path.resolve().relative_to(base.resolve()).as_posix()


#: Validation stage per task — the accepted logical identity used in
#: professor-facing manifests (no machine-local capture paths).
VALIDATION_STAGE_BY_TASK: dict[str, str] = {
    "quixbugs-depth-first-search": "stage-a",
    "quixbugs-quicksort": "stage-b",
    "quixbugs-flatten": "stage-b",
    "quixbugs-find-in-sorted": "stage-c",
    "quixbugs-rpn-eval": "stage-c",
    "quixbugs-shortest-path-length": "stage-c",
    "quixbugs-reverse-linked-list": "stage-c",
    "quixbugs-kth": "stage-c",
}


def _logical_identity(
    scope: str,
    task_id: str,
    capsule_manifest: Optional[dict[str, Any]] = None,
) -> str:
    """Stable logical evidence identity for professor-facing manifests."""
    if scope == "validation":
        stage = VALIDATION_STAGE_BY_TASK.get(task_id, "validation")
        return f"validation/{stage}/{task_id}"
    if capsule_manifest is not None:
        entry = (capsule_manifest.get("evidence") or {}).get(
            f"{scope}:{task_id}"
        ) or {}
        logical = entry.get("logical_identity")
        if logical:
            return logical
    return f"final_holdout_partial/{task_id}"


def _write_json(path: Path, obj: Any) -> None:
    text = json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False)
    text += "\n"
    path.write_text(text, encoding="utf-8")
