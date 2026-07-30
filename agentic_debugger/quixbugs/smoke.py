"""Operator entry point for the single no-model QuixBugs (gcd) smoke."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentic_debugger.quixbugs.adapter import (
    QuixBugsAdapter,
    QuixBugsPreflightFacts,
    QuixBugsSourceAcquirer,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one gated QuixBugs gcd gold-patch smoke")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--external-parent", required=True)
    parser.add_argument(
        "--facts-json",
        help="unsupported metadata-only input; concrete execution contexts cannot be reconstructed from JSON",
    )
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

    facts = QuixBugsPreflightFacts()
    adapter = QuixBugsAdapter.from_manifest(args.manifest)
    report = adapter.preflight(facts, repository_root=str(Path.cwd()))
    print(json.dumps(report.to_mapping(), sort_keys=True, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
