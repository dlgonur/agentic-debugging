"""Operator entry point for the single no-model BugsInPy smoke."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentic_debugger.bugsinpy.adapter import (
    BugsInPyAdapter,
    GitSourceAcquirer,
    NoModelSmokeRunner,
    PreflightFacts,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one gated BugsInPy gold-patch smoke")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--external-parent", required=True)
    parser.add_argument(
        "--facts-json",
        help="unsupported metadata-only input; concrete execution contexts cannot be reconstructed from JSON",
    )
    parser.add_argument("--target-symbol", action="append", default=[])
    args = parser.parse_args()

    if args.facts_json:
        print(
            json.dumps(
                {
                    "verdict": "REAL_SMOKE_BLOCKED",
                    "reason": "CLI is metadata-preflight-only: --facts-json cannot construct a verified execution context or containment runner",
                },
                sort_keys=True,
                indent=2,
            )
        )
        return 2
    facts = PreflightFacts()
    adapter = BugsInPyAdapter.from_manifest(args.manifest)
    evidence = NoModelSmokeRunner(adapter, GitSourceAcquirer()).run(
        args.task,
        facts=facts,
        external_parent=args.external_parent,
        repository_root=str(Path.cwd()),
        target_symbols=args.target_symbol or None,
    )
    print(json.dumps(evidence.to_mapping(), sort_keys=True, indent=2))
    return 0 if evidence.verdict == "REAL_SMOKE_PASSED" else 2 if evidence.verdict == "REAL_SMOKE_BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
