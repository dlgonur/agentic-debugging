"""Fail-closed validator entry point for the frozen QuixBugs paired pilot.

Validates every tracked supported campaign manifest version: the frozen v1
manifest (OpenCode Zen zero-price route), the derived v2 manifest (OpenCode
Go subscription route with DeepSeek V4 Flash and fail-closed subscription
billing), and the derived v3 manifest (same route, plus the
VALIDATION_NOT_REACHED terminal and candidate_provenance).  Exits 0 only
when every manifest validates.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from quixbugs_paired_pilot import (  # noqa: E402
    MANIFEST_PATH,
    MANIFEST_PATH_V2,
    MANIFEST_PATH_V3,
    PilotError,
    load_manifest,
    validate_manifest,
)

TRACKED_MANIFESTS = (MANIFEST_PATH, MANIFEST_PATH_V2, MANIFEST_PATH_V3)


def _validate(path: Path) -> int:
    try:
        manifest = load_manifest(path)
        manifest_hash = validate_manifest(manifest)
        print(json.dumps({
            "manifest": str(path.relative_to(REPO_ROOT)),
            "campaign_id": manifest["campaign_id"],
            "campaign_version": manifest["campaign_version"],
            "manifest_hash": manifest_hash,
            "valid": True,
        }, indent=2, sort_keys=True))
        return 0
    except PilotError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


def main() -> int:
    results = [_validate(path) for path in TRACKED_MANIFESTS]
    return 0 if all(code == 0 for code in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
