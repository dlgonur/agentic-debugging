"""Offline revalidation of a previously captured contained-PDB reachability
result against the repaired, fail-closed acceptance predicate.

This performs **no execution** of any kind: no WSL/Bubblewrap, no controller
run, no model/provider contact. It only re-parses the ``events_jsonl``
already recorded inside a captured
``ContainedPdbReachabilityResult.to_mapping()`` JSON file (as produced by
``scripts/quixbugs_gcd_pdb_reachability_case.py``) and replays the exact same
``validate_events_jsonl``/``evaluate_reachability_sequence_from_events``
checks the repaired live acceptance predicate now requires.

This exists because the verdict-predicate repair changed what counts as
PASSED; the real WSL/Bubblewrap case that produced the original evidence is
not rerun (no new external execution is authorized for this repair) --
instead, this proves the already-captured real event trail still satisfies
the *stronger* fail-closed contract offline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.quixbugs.contained_pdb import (  # noqa: E402
    _GCD_RUNTIME_PROBE,
    determine_reachability_verdict,
    evaluate_reachability_sequence_from_events,
    validate_events_jsonl,
)

#: Tracked copy of the real captured evidence (also mirrored, byte-identical,
#: in the review package's evidence/ directory, which is not itself tracked
#: by git) -- kept under tests/ so this script and the regression suite can
#: both revalidate it from any fresh checkout.
DEFAULT_CAPTURED_RESULT = (
    REPO_ROOT / "tests" / "golden_trajectories" / "data"
    / "quixbugs-gcd-pdb-reachability-captured-result.json"
)


def revalidate(captured_result_path: Path) -> dict:
    captured = json.loads(captured_result_path.read_text(encoding="utf-8"))
    task_id = captured.get("task_id")
    events_jsonl = captured.get("events_jsonl", "")

    events_valid, events_reasons, parsed_events = validate_events_jsonl(events_jsonl, task_id=task_id)
    sequence_evidence = None
    if events_valid:
        sequence_evidence = evaluate_reachability_sequence_from_events(
            parsed_events,
            expected_script=_GCD_RUNTIME_PROBE.module_path,
            expected_function=_GCD_RUNTIME_PROBE.focus_function,
            expected_breakpoint_line=captured.get("launch_plan", {}).get("breakpoints", [None])[0],
        )

    original_verdict = captured.get("verdict")
    original_diagnostics = captured.get("diagnostics", [])
    original_cleanup_succeeded = captured.get("cleanup_succeeded")
    original_canonical_source_unchanged = captured.get("canonical_source_unchanged")
    original_contained_preflight_authorized = (captured.get("contained_preflight") or {}).get("authorized")
    original_quixbugs_preflight_authorized = (captured.get("quixbugs_preflight") or {}).get("authorized")
    original_gate_decisions = captured.get("gate_decisions", [])
    launch_plan = captured.get("launch_plan")
    bundle_hashes = captured.get("pdb_runtime_bundle_hashes")

    provenance_present = bool(launch_plan) and bool(bundle_hashes) and len(bundle_hashes) >= 4

    revalidated_verdict = determine_reachability_verdict(
        result_present=captured.get("controller_final_state") is not None,
        quixbugs_authorized=bool(original_quixbugs_preflight_authorized),
        contained_authorized=bool(original_contained_preflight_authorized),
        any_gate_allowed=any(decision.get("allowed") for decision in original_gate_decisions),
        sequence_ok=sequence_evidence is not None and sequence_evidence.ok,
        events_valid=events_valid,
        stop_reason_is_failed=captured.get("controller_stop_reason") == "failed",
        final_state_is_failed=captured.get("controller_final_state") == "Failed",
        cleanup_succeeded=bool(original_cleanup_succeeded),
        canonical_source_unchanged=original_canonical_source_unchanged is True,
        provenance_present=provenance_present,
        diagnostics_empty=not original_diagnostics,
    )

    return {
        "captured_result_path": str(captured_result_path),
        "original_verdict": original_verdict,
        "revalidated_verdict": revalidated_verdict,
        "verdicts_agree": original_verdict == revalidated_verdict,
        "events_valid": events_valid,
        "events_validation_reasons": list(events_reasons),
        "sequence_evidence": sequence_evidence.to_mapping() if sequence_evidence else None,
        "provenance_present": provenance_present,
    }


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CAPTURED_RESULT
    report = revalidate(path)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["revalidated_verdict"] == "REACHABILITY_CASE_PASSED" and report["verdicts_agree"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
