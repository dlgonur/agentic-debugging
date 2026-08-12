"""Test bootstrap: make the repository root importable.

Mirrors the convention in ``experiments/cp118_rag_definitive/tests/unit/conftest.py``
so ``experiments.local_inference_perf.*`` imports resolve when running tests
from anywhere.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))