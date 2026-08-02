"""Fail-closed validator entry point for the frozen QuixBugs pilot."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from quixbugs_paired_pilot import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(["validate", *sys.argv[1:]]))
