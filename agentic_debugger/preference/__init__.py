"""Deterministic preference-pair export from verifier-backed attempt records.

Consumes normalized comparison attempt records and produces strict
``preference-pair-v1`` pairs (JSONL) plus an audit summary.  This package
prepares future DPO work; it never performs training, DPO or RLHF.
"""

from agentic_debugger.preference.schema import (
    MAX_PAIR_RESPONSE_BYTES,
    PREFERENCE_PAIR_SCHEMA_VERSION,
    AttemptRef,
    PreferenceError,
    PreferenceInputError,
    PreferenceInvariantError,
    PreferencePair,
    bound_response,
    canonical_json,
)

__all__ = [
    "MAX_PAIR_RESPONSE_BYTES",
    "PREFERENCE_PAIR_SCHEMA_VERSION",
    "AttemptRef",
    "PreferenceError",
    "PreferenceInputError",
    "PreferenceInvariantError",
    "PreferencePair",
    "bound_response",
    "canonical_json",
]
