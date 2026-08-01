"""Bounded, metadata-only BugsInPy preflight command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentic_debugger.bugsinpy.metadata_preflight import (
    DEFAULT_GATE_PATH,
    DEFAULT_MANIFEST_PATH,
    BugsInPyMetadataPreflight,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default=None)
    parser.add_argument("--operation", default=None)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--gate", type=Path, default=DEFAULT_GATE_PATH)
    parser.add_argument("--operator-authorization", default="absent")
    parser.add_argument("--containment-ready", action="store_true")
    parser.add_argument("--dependencies-ready", action="store_true")
    parser.add_argument("--evidence-handling", default="unspecified")
    args = parser.parse_args()
    decision = BugsInPyMetadataPreflight(
        manifest_path=args.manifest,
        gate_path=args.gate,
    ).decide(
        args.task,
        args.operation,
        operator_authorization_state=args.operator_authorization,
        containment_readiness=args.containment_ready,
        dependency_readiness=args.dependencies_ready,
        evidence_handling=args.evidence_handling,
    )
    print(json.dumps(decision.to_mapping(), sort_keys=True, separators=(",", ":")))
    return 0 if decision.allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())
