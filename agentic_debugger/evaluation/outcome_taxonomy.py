from __future__ import annotations

from enum import Enum
from typing import Sequence


class OutcomeInputError(ValueError):
    """Raised when an outcome matrix input is not a valid test vector."""


class SemanticOutcome(str, Enum):
    RESOLVED = "RESOLVED"
    BREAKING_RESOLVED = "BREAKING_RESOLVED"
    PARTIALLY_RESOLVED = "PARTIALLY_RESOLVED"
    WORK_IN_PROGRESS = "WORK_IN_PROGRESS"
    NO_OP = "NO_OP"
    REGRESSION = "REGRESSION"


Outcome = SemanticOutcome


def classify_outcome(f2p_passed: Sequence[bool], p2p_passed: Sequence[bool]) -> SemanticOutcome:
    """Classify only from the post-patch individual F2P/P2P vectors."""
    _validate_vector(f2p_passed, "f2p_passed")
    _validate_vector(p2p_passed, "p2p_passed")
    all_f2p = all(f2p_passed)
    some_f2p = any(f2p_passed)
    all_p2p = all(p2p_passed)
    if all_f2p and all_p2p:
        return SemanticOutcome.RESOLVED
    if all_f2p and not all_p2p:
        return SemanticOutcome.BREAKING_RESOLVED
    if some_f2p and all_p2p:
        return SemanticOutcome.PARTIALLY_RESOLVED
    if some_f2p and not all_p2p:
        return SemanticOutcome.WORK_IN_PROGRESS
    if all_p2p:
        return SemanticOutcome.NO_OP
    return SemanticOutcome.REGRESSION


def _validate_vector(values: Sequence[bool], label: str) -> None:
    if not isinstance(values, (list, tuple)):
        raise OutcomeInputError(f"{label} must be a list or tuple")
    if not values:
        raise OutcomeInputError(f"{label} must not be empty")
    if any(type(value) is not bool for value in values):
        raise OutcomeInputError(f"{label} must contain only booleans")
