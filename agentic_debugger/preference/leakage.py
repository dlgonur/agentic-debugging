"""Leakage guards for preference-pair export.

Two guards are enforced:

* **held-out task exclusion** — tasks in the declared held-out set never
  produce pairs;
* **oracle-answer contamination rejection** — a response that contains an
  evaluator-only *answer* (the root-cause summary or the runtime-evidence
  hint) verbatim is rejected for pairing.  Rejection is fail-closed: the
  pair is refused, never sanitized.

Oracle *identity* fields (target files and target symbols) are also reported
as spans, but they do **not** reject a pair: any legitimate patch must name
the defective file and symbol, so their presence in a response is expected
and carries no answer leakage.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

from agentic_debugger.evaluation.task_schema import Oracle

#: Oracle answer fields whose verbatim values must never appear in a response
#: (these reject the pair).
ORACLE_ANSWER_FIELDS: Tuple[str, ...] = (
    "root_cause_summary",
    "runtime_evidence_hint",
)

#: Oracle identity fields; reported as spans but never pair-rejecting.
ORACLE_IDENTITY_FIELDS: Tuple[str, ...] = (
    "target_files",
    "target_symbols",
)

#: Provenance field carrying the evaluator-only fixed revision.
FIXED_REVISION_FIELD = "fixed_revision"

_ANSWER_FIELDS: Tuple[str, ...] = ORACLE_ANSWER_FIELDS
_IDENTITY_FIELDS: Tuple[str, ...] = ORACLE_IDENTITY_FIELDS


def contamination_spans(text: str, oracle: Oracle) -> List[Dict[str, Any]]:
    """Verbatim oracle-value occurrences inside ``text`` (no normalization).

    Returns answer spans (``rejecting: true``) and identity spans
    (``rejecting: false``) so the audit can distinguish them.
    """

    if type(text) is not str or not isinstance(oracle, Oracle):
        raise ValueError("contamination_spans requires a string and an Oracle")
    spans: List[Dict[str, Any]] = []
    for field in _ANSWER_FIELDS:
        value = getattr(oracle, field, None)
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if not stripped:
            continue
        index = text.find(stripped)
        if index >= 0:
            spans.append(
                {"field": field, "start": index, "length": len(stripped),
                 "rejecting": True}
            )
    for field, values in (
        ("target_files", oracle.target_files),
        ("target_symbols", oracle.target_symbols),
    ):
        for item in values:
            if item and item in text:
                spans.append(
                    {"field": field, "start": text.find(item), "length": len(item),
                     "rejecting": False}
                )
    return spans


def is_contaminated(text: str, oracle: Oracle) -> bool:
    """True when an evaluator-only oracle *answer* appears verbatim."""

    return any(span["rejecting"] for span in contamination_spans(text, oracle))


__all__ = [
    "ORACLE_ANSWER_FIELDS",
    "ORACLE_IDENTITY_FIELDS",
    "FIXED_REVISION_FIELD",
    "contamination_spans",
    "is_contaminated",
]
