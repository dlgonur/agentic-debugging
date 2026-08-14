"""UI-independent application/session contracts for the Local Application V1 surface.

This package establishes the application boundary only.  It defines:

- session specification, identity, lifecycle/status/result contracts;
- the versioned application-owned :class:`SessionEvent` model and its
  safe-data rules;
- execution-source (live versus replay) contracts;
- immutable presentation state and a pure event reducer.

Boundary rules (see ``docs/architecture/local-application-contracts-v1.md``):

- No Textual dependency and no UI code.
- Never executes or mutates controller, PDB, patch, verifier, demo,
  live-model, GPU, or experiment behavior.
- Never touches canonical ``RunEvent`` 1.0 trajectories, replay semantics,
  golden trajectories, or semantic projections.
"""

from agentic_debugger import AgenticDebuggerError


class ApplicationError(AgenticDebuggerError):
    """Base class for application-layer errors."""


class ApplicationInputError(ApplicationError):
    """Raised when an application-layer input is malformed."""


class ApplicationContractError(ApplicationError):
    """Raised when an application contract or invariant is violated."""


__all__ = [
    "ApplicationContractError",
    "ApplicationError",
    "ApplicationInputError",
]
