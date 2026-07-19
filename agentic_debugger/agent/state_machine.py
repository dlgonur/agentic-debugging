from enum import Enum
from typing import Set


class ControllerState(Enum):
    REPRODUCE = "Reproduce"
    UNDERSTAND = "Understand"
    RUNTIME_EVIDENCE = "RuntimeEvidence"
    PATCH = "Patch"
    VALIDATE = "Validate"
    DONE = "Done"
    FAILED = "Failed"


TRANSITION_GRAPH: dict[ControllerState, set[ControllerState]] = {
    ControllerState.REPRODUCE: {ControllerState.UNDERSTAND, ControllerState.FAILED},
    ControllerState.UNDERSTAND: {
        ControllerState.UNDERSTAND,
        ControllerState.RUNTIME_EVIDENCE,
        ControllerState.PATCH,
        ControllerState.FAILED,
    },
    ControllerState.RUNTIME_EVIDENCE: {
        ControllerState.UNDERSTAND,
        ControllerState.RUNTIME_EVIDENCE,
        ControllerState.PATCH,
        ControllerState.FAILED,
    },
    ControllerState.PATCH: {
        ControllerState.PATCH,
        ControllerState.UNDERSTAND,
        ControllerState.VALIDATE,
        ControllerState.FAILED,
    },
    ControllerState.VALIDATE: {
        ControllerState.DONE,
        ControllerState.UNDERSTAND,
        ControllerState.RUNTIME_EVIDENCE,
        ControllerState.PATCH,
        ControllerState.FAILED,
    },
    ControllerState.DONE: set(),
    ControllerState.FAILED: set(),
}


def is_transition_allowed(current: ControllerState, target: ControllerState) -> bool:
    return target in TRANSITION_GRAPH[current]
