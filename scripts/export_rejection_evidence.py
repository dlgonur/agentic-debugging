"""Export canonical live rejection evidence for review packages."""

from __future__ import annotations

import argparse

from agentic_debugger.evaluation.directive_observability import export_rejection_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-results", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args()
    export_rejection_evidence(args.live_results, args.output, index=args.index)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
