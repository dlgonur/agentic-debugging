"""One-shot builder for the TRACKED frozen R6 professor-trace evidence capsule.

The professor-trace exporter
(``agentic_debugger/evaluation/professor_trace_r6``) must regenerate the
checked-in ``docs/professor_traces`` deliverable from a PRISTINE checkout
containing only tracked files — no ``_ai-review`` package, no
``C:/tmp/r6-bounded`` live run trees, no operator data.

This script produces exactly that minimum tracked frozen evidence:

    experiments/r6_debugger_training/runs/frozen/
      capsule_manifest.json          # chain of custody + per-file SHA256
      validation/<task_id>/evidence.json          (8)
      final_holdout_partial/<task_id>/evidence.json (2)
      ancillary/checkpoint_selection.json
      ancillary/stage_a_report.json
      ancillary/stage_b_report.json
      ancillary/stage_c_report.json
      ancillary/holdout_report.json   # INCOMPLETE_HARDWARE_STOP authority
      quixbugs_audit_needles/<task_id>.json (8)   # AUDIT-ONLY frozen needles

Chain of custody is explicit and fail-closed:

1. The RAW accepted evidence is read from the accepted review package /
   live run trees and each raw record's SHA256 must equal the accepted
   frozen identity embedded in the exporter registry
   (``agentic_debugger/evaluation/professor_trace_r6``).  Any mismatch
   aborts the build — no synthetic replacements are possible.
2. Only material the exporter actually consumes is captured.  The
   professor-facing raw-export fields are:
   - ``telemetry[*].request``: NEVER captured (user prompts carry hidden
     task descriptions — protected by the accepted clean-holdout policy).
   - ``translated_directive.arguments``: cleared (the patch payload is the
     model's own full repair body — answer-bearing).  The same content is
     already hashed in ``patch_identity`` and ``serialization_normalization``.
   - observation ``failure_output_raw`` and ``node_id``: cleared (raw
     hidden-test failure text and the hidden reproduction node id).
   - everything else is preserved byte-structurally (LF endings).
3. ``capsule_manifest.json`` records, per file: the accepted raw SHA256,
   the capsule SHA256, the captured/cleared field list, the source identity
   (logical identity + optional machine-local capture path), and the
   exporter verification status.  The raw SHA256 stays the scientific
   evidence identity; the capsule is a deterministic, schema-validated
   derived capsule of the same record.

The exporter's default root-resolution policy ALREADY prefers
``experiments/r6_debugger_training/runs/frozen``; the capsule registry
verification makes that preference the fail-closed authority for
regeneration.

No model weights, caches, telemetry CSVs, or huge run trees are captured.
The audit-only quixbugs needle capsules freeze the mechanically derived
forbidden content (hidden-test needles + oracle fields + gold-repair added
lines) so the anti-leakage audit can run from a clean clone where the
ignored quixbugs fixtures are absent.  The derivation verifies the fixture
source against the tracked ``split_manifest.json`` pins and the gold diff
against its tracked pin; both verifications fail closed.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
FROZEN_DIR = (
    REPO_ROOT / "experiments" / "r6_debugger_training" / "runs" / "frozen"
)

# Repository root import for direct ``python scripts/...`` execution.
sys.path.insert(0, str(REPO_ROOT))

_PKG = REPO_ROOT / "_ai-review" / "R6-HARDWARE-STOP"
_LIVE = Path("C:/tmp/r6-bounded")

_STAGE_TASKS = {
    "stage-a": ("quixbugs-depth-first-search",),
    "stage-b": ("quixbugs-quicksort", "quixbugs-flatten"),
    "stage-c": (
        "quixbugs-find-in-sorted",
        "quixbugs-rpn-eval",
        "quixbugs-shortest-path-length",
        "quixbugs-reverse-linked-list",
        "quixbugs-kth",
    ),
}

#: Accepted frozen evidence identities (kept in sync with the exporter
#: registry; asserted at import time so the builder can never drift).
from agentic_debugger.evaluation.professor_trace_r6 import (
    FROZEN_HOLDOUT_EVIDENCE,
    FROZEN_VALIDATION_EVIDENCE,
)

#: Fields cleared in the capsule because they carry protected material that
#: is neither consumed by the exporter nor professor-exportable.
#: Everything else is preserved byte-structurally.
CAPSULE_CLEARED_FIELDS: tuple[tuple[str, ...], ...] = (
    ("telemetry", "*", "request"),
    ("telemetry", "*", "translated_directive", "arguments"),
)

#: Minimum schema shape every capsule must satisfy (fail-closed).
CAPSULE_REQUIRED_TOP_KEYS = (
    "schema_version",
    "run_identity",
    "task",
    "trajectory_jsonl",
    "telemetry",
    "gate_results",
    "diagnosis_provenance",
    "verifier",
    "patch_identity",
    "serialization_normalization",
    "cleanup",
    "controller_result",
    "tool_errors",
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _logical_identity(scope: str, task_id: str) -> str:
    for stage, tasks in _STAGE_TASKS.items():
        if task_id in tasks:
            return f"validation/{stage}/{task_id}"
    return f"final_holdout_partial/{task_id}"


def _find_raw_validation_evidence(task_id: str) -> Optional[Path]:
    """Locate the accepted raw validation evidence (live trees, package)."""
    for stage_dir in ("v3c30-r68-a-7c9881", "v3c30-r68-b-7c9881",
                      "v3c30-r68-c-7c9881"):
        candidate = (
            _LIVE / stage_dir / "adapter-checkpoint-30" / task_id
            / "evidence.json"
        )
        if candidate.is_file():
            return candidate
    candidate = _PKG / "interrupted-holdout" / "completed-evidence" / f"{task_id}.json"
    if candidate.is_file():
        return candidate
    return None


def _find_raw_holdout_evidence(task_id: str) -> Optional[Path]:
    candidate = _PKG / "interrupted-holdout" / "completed-evidence" / f"{task_id}.json"
    if candidate.is_file():
        return candidate
    live = (
        _LIVE / "v3c30-r68-final-holdout-7c9881-f966dd"
        / "adapter-checkpoint-30" / task_id / "evidence.json"
    )
    if live.is_file():
        return live
    return None


def _clear_path(data: Any, path: tuple[Any, ...]) -> None:
    """Delete ``path`` from nested dict/list structure if present.

    ``"*"`` descends into every list item / dict value (containers only;
    the JSONL trajectory is cleared explicitly by ``_clear_trajectory``).
    """
    if not path:
        return
    head, rest = path[0], path[1:]
    if head == "*":
        if isinstance(data, list):
            for item in data:
                _clear_path(item, rest)
        elif isinstance(data, dict):
            for value in data.values():
                _clear_path(value, rest)
        return
    if isinstance(data, dict) and head in data:
        if not rest:
            del data[head]
        else:
            _clear_path(data[head], rest)


#: Protected observation fields removed from every trajectory event line.
_TRAJECTORY_CLEARED_FIELDS = ("failure_output_raw", "node_id")


def _clear_trajectory(trajectory_jsonl: str) -> str:
    """Remove protected observation fields from every trajectory event."""
    lines = []
    for line in trajectory_jsonl.splitlines():
        if not line.strip():
            lines.append(line)
            continue
        event = json.loads(line)
        if event.get("event_type") == "observation":
            obs = (event.get("payload") or {}).get("observation") or {}
            inner = obs.get("payload") or {}
            for field in _TRAJECTORY_CLEARED_FIELDS:
                inner.pop(field, None)
        lines.append(json.dumps(event, ensure_ascii=False, allow_nan=False))
    return "\n".join(lines)


def _validate_capsule_schema(capsule: dict[str, Any], task_id: str) -> None:
    for key in CAPSULE_REQUIRED_TOP_KEYS:
        if key not in capsule:
            raise RuntimeError(
                f"{task_id}: capsule missing required top key {key!r}"
            )
    if capsule.get("schema_version") != "debugger-interaction-v2-r5-evidence":
        raise RuntimeError(f"{task_id}: unexpected capsule schema_version")
    if capsule["task"].get("task_id") != task_id:
        raise RuntimeError(f"{task_id}: capsule task identity mismatch")
    if not isinstance(capsule.get("trajectory_jsonl"), str):
        raise RuntimeError(f"{task_id}: trajectory_jsonl is not a string")
    if not isinstance(capsule.get("telemetry"), list):
        raise RuntimeError(f"{task_id}: telemetry is not a list")
    # Protected material must be gone.
    for record in capsule["telemetry"]:
        if "request" in record:
            raise RuntimeError(f"{task_id}: telemetry.request survived capsule")
        directive = record.get("translated_directive") or {}
        if "arguments" in directive:
            raise RuntimeError(
                f"{task_id}: translated_directive.arguments survived capsule"
            )
    for line in capsule["trajectory_jsonl"].splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("event_type") != "observation":
            continue
        payload = (event.get("payload") or {}).get("observation") or {}
        inner = payload.get("payload") or {}
        if "failure_output_raw" in inner or "node_id" in inner:
            raise RuntimeError(
                f"{task_id}: protected observation field survived capsule"
            )


def build_capsule_record(
    raw_path: Path, scope: str, task_id: str
) -> dict[str, Any]:
    """Build one capsule record: verify raw identity, clear protected fields,
    validate, and return ``(capsule, manifest_entry)``."""
    raw_bytes = raw_path.read_bytes()
    raw_sha = _sha256_bytes(raw_bytes)
    expected_sha = (
        FROZEN_VALIDATION_EVIDENCE[task_id]
        if scope == "validation"
        else FROZEN_HOLDOUT_EVIDENCE[task_id]
    )
    if raw_sha != expected_sha:
        raise RuntimeError(
            f"{task_id}: raw evidence identity mismatch — expected "
            f"{expected_sha}, got {raw_sha} ({raw_path}). Build aborted; "
            f"no synthetic replacement is possible."
        )

    record = json.loads(raw_bytes.decode("utf-8"))
    for field_path in CAPSULE_CLEARED_FIELDS:
        _clear_path(record, field_path)
    record["trajectory_jsonl"] = _clear_trajectory(
        record.get("trajectory_jsonl") or ""
    )

    _validate_capsule_schema(record, task_id)

    capsule_text = json.dumps(
        record, indent=2, ensure_ascii=False, allow_nan=False
    )
    capsule_sha = _sha256_bytes(capsule_text.encode("utf-8"))

    return {
        "logical_identity": _logical_identity(scope, task_id),
        "task_id": task_id,
        "scope": scope,
        "raw_sha256": raw_sha,
        "capsule_sha256": capsule_sha,
        "capsule_bytes": len(capsule_text.encode("utf-8")),
        "raw_bytes": len(raw_bytes),
        "cleared_fields": [
            "/".join(str(part) for part in field_path)
            for field_path in CAPSULE_CLEARED_FIELDS
        ]
        + [
            "trajectory_jsonl/*/observation.payload.failure_output_raw",
            "trajectory_jsonl/*/observation.payload.node_id",
        ],
        "capture_source": str(raw_path),
        "capsule_path": f"{scope}/{task_id}/evidence.json",
        "capsule": capsule_text,
    }


def _resolve_ancillary(key: str, out_name: str) -> Optional[Path]:
    pkg_paths = {
        "checkpoint_selection": _PKG / "selection" / "checkpoint-selection.json",
        "stage_a_report": _PKG / "validation" / "stage-a" / "eval_report.json",
        "stage_b_report": _PKG / "validation" / "stage-b" / "eval_report.json",
        "stage_c_report": _PKG / "validation" / "stage-c" / "eval_report.json",
        "holdout_report": _PKG / "interrupted-holdout" / "eval_report.json",
    }
    path = pkg_paths.get(key)
    if path is not None and path.is_file():
        return path
    return None


def _resolve_gold_diff(task_id: str) -> Optional[Path]:
    tracked = REPO_ROOT / "experiments" / "r6_debugger_training" / "gold" / f"{task_id}.patch"
    if tracked.is_file():
        return tracked
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the tracked frozen R6 professor-trace evidence capsule"
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(FROZEN_DIR),
        help="output capsule root (default: experiments/r6_debugger_training/runs/frozen)",
    )
    args = parser.parse_args()

    output = Path(args.output)
    if output.exists():
        import shutil

        shutil.rmtree(output)
    (output / "validation").mkdir(parents=True)
    (output / "final_holdout_partial").mkdir(parents=True)
    (output / "ancillary").mkdir(parents=True)
    (output / "quixbugs_audit_needles").mkdir(parents=True)

    manifest: dict[str, Any] = {
        "schema_version": "r6-frozen-evidence-capsule-v1",
        "description": (
            "Minimum tracked frozen evidence for deterministic regeneration "
            "of docs/professor_traces from a pristine checkout.  Raw evidence "
            "identity (SHA256) is preserved; protected model-prompt / "
            "answer-bearing fields are cleared (see cleared_fields)."
        ),
        "source_identity": {
            "review_package": str(_PKG),
            "live_run_root": str(_LIVE),
        },
        "evidence": {},
        "ancillary": {},
        "audit_needles": {},
    }

    # --- validation evidence capsules ---------------------------------------
    for scope, expected in (
        ("validation", FROZEN_VALIDATION_EVIDENCE),
        ("final_holdout_partial", FROZEN_HOLDOUT_EVIDENCE),
    ):
        for task_id in sorted(expected):
            raw = (
                _find_raw_validation_evidence(task_id)
                if scope == "validation"
                else _find_raw_holdout_evidence(task_id)
            )
            if raw is None:
                raise RuntimeError(f"{task_id}: raw evidence not found")
            entry = build_capsule_record(raw, scope, task_id)
            capsule_dir = output / scope / task_id
            capsule_dir.mkdir(parents=True, exist_ok=True)
            (capsule_dir / "evidence.json").write_text(
                entry.pop("capsule"), encoding="utf-8", newline="\n"
            )
            del entry["capsule_path"]
            manifest["evidence"][f"{scope}:{task_id}"] = {
                k: v for k, v in entry.items() if k != "capsule"
            }
            # keep capsule_path recorded
            manifest["evidence"][f"{scope}:{task_id}"]["capsule_path"] = (
                f"{scope}/{task_id}/evidence.json"
            )
            print(
                f"{scope}:{task_id}  raw={entry['raw_sha256'][:12]} "
                f"capsule={entry['capsule_sha256'][:12]} "
                f"{entry['capsule_bytes']}B"
            )

    # --- ancillary authority -------------------------------------------------
    for key in (
        "checkpoint_selection", "stage_a_report", "stage_b_report",
        "stage_c_report", "holdout_report",
    ):
        path = _resolve_ancillary(key, key)
        if path is None:
            raise RuntimeError(f"ancillary record missing: {key}")
        data = path.read_bytes()
        out_path = output / "ancillary" / f"{key}.json"
        out_path.write_bytes(data)
        manifest["ancillary"][key] = {
            "logical_identity": f"ancillary/{key}",
            "source_path": str(path),
            "sha256": _sha256_bytes(data),
            "bytes": len(data),
        }
        print(f"ancillary:{key}  {len(data)}B")

    # --- quixbugs audit needle capsules (frozen forbidden content) -----------
    from agentic_debugger.evaluation.professor_trace_r6 import (
        derive_forbidden_content_scoped,
    )

    split_manifest = json.loads(
        (REPO_ROOT / "experiments" / "r6_debugger_training"
         / "split_manifest.json").read_text(encoding="utf-8")
    )
    source_pins = {
        entry["task_id"]: entry["source_sha256"]
        for entry in split_manifest["validation_tasks"]
    }
    gold_pins = {
        entry["task_id"]: entry["gold_diff_sha256"]
        for entry in split_manifest["validation_tasks"]
    }
    curated_root = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"
    for task_id in sorted(FROZEN_VALIDATION_EVIDENCE):
        fixture_dir = curated_root / task_id
        if not (fixture_dir / "task.json").is_file():
            raise RuntimeError(f"{task_id}: local quixbugs fixture missing")
        task_meta = json.loads(
            (fixture_dir / "task.json").read_text(encoding="utf-8")
        )
        module_path = (
            (task_meta.get("constraints") or {}).get("allowed_write_paths")
            or [""]
        )[0]
        source_text = (
            (fixture_dir / module_path).read_text(encoding="utf-8")
            .replace("\r\n", "\n")
        )
        source_sha = _sha256_bytes(source_text.encode("utf-8"))
        if source_sha != source_pins[task_id]:
            raise RuntimeError(
                f"{task_id}: fixture source does not match the tracked "
                f"split_manifest pin (fail closed)"
            )

        gold = _resolve_gold_diff(task_id)
        if gold is None:
            raise RuntimeError(f"{task_id}: gold diff missing")
        gold_text = gold.read_text(encoding="utf-8").replace("\r\n", "\n")
        gold_sha = _sha256_bytes(gold_text.encode("utf-8"))
        if gold_sha != gold_pins[task_id]:
            raise RuntimeError(
                f"{task_id}: gold diff does not match the tracked "
                f"split_manifest pin (fail closed)"
            )

        forbidden = derive_forbidden_content_scoped(
            task_id,
            fixture_dir,
            gold_diff_dir=gold.parent,
        )
        needle = {
            "schema_version": "r6-quixbugs-audit-needles-v2",
            "task_id": task_id,
            "derivation": (
                "Mechanically derived from the tracked split_manifest-pinned "
                "fixture source + hidden tests + oracle fields + gold repair "
                "diff (accepted anti_leakage derivation rules, AUDIT ONLY)."
            ),
            "source_pin_sha256": source_sha,
            "gold_diff_sha256": gold_sha,
            "forbidden_content": dataclasses.asdict(forbidden),
        }
        needle_path = output / "quixbugs_audit_needles" / f"{task_id}.json"
        needle_path.write_text(
            json.dumps(needle, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        manifest["audit_needles"][task_id] = {
            "logical_identity": f"quixbugs_audit_needles/{task_id}",
            "schema_version": needle["schema_version"],
            "source_pin_sha256": source_sha,
            "gold_diff_sha256": gold_sha,
            "needle_sha256": _sha256_file(needle_path),
        }
        print(
            f"audit:{task_id}  gold={gold_sha[:12]} "
            f"needles={len(forbidden.needles())}"
        )

    manifest_path = output / "capsule_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"\nCAPSULE COMPLETE: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
