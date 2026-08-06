"""Unified comparison harness for base / fine-tuned / RAG / agentic conditions.

This package compares imported generation artifacts and native agentic runs
through one normalized, verifier-backed schema (``comparison-v1``).  It
reuses the existing controller, demo runner, verifier, event, replay,
budget, workspace and patch contracts; it creates no second agent framework,
no second verifier and no second campaign format.

The package is offline by default: nothing here contacts a provider or the
network.  Imported artifacts are labeled ``offline-deterministic-demo`` when
synthetic; such artifacts are infrastructure evidence, never model
performance.
"""

from agentic_debugger.comparison.schema import (
    COMPARISON_SCHEMA_VERSION,
    AttemptRecord,
    ComparisonError,
    ComparisonExperiment,
    ComparisonInputError,
    ComparisonInvariantError,
    canonical_json,
)

__all__ = [
    "COMPARISON_SCHEMA_VERSION",
    "AttemptRecord",
    "ComparisonError",
    "ComparisonExperiment",
    "ComparisonInputError",
    "ComparisonInvariantError",
    "canonical_json",
]
