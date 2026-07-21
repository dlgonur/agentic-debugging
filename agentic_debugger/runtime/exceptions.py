from agentic_debugger import AgenticDebuggerError


class RuntimeError(AgenticDebuggerError):
    """Base exception for runtime workspace/command errors."""


class WorkspaceError(RuntimeError):
    """Raised when workspace creation, cleanup, or path resolution fails."""


class CommandRequestError(RuntimeError):
    """Raised when a command request has invalid arguments."""


class CommandExecutionError(RuntimeError):
    """Raised when a command cannot be launched or suffers an infrastructure failure."""


class SourceInspectionError(RuntimeError):
    """Base exception for source inspection errors."""


class SourceDecodeError(SourceInspectionError):
    """Raised when source file decoding fails."""


class SourceParseError(SourceInspectionError):
    """Raised when AST parsing fails."""


class PatchValidationError(RuntimeError):
    """Raised when a patch fails validation."""


class PatchAuthorizationError(PatchValidationError):
    """Raised when a patch targets unauthorized paths."""


class PatchApplyError(RuntimeError):
    """Raised when patch application fails."""


class PatchStateError(RuntimeError):
    """Raised when patch manager state is invalid."""


class PatchRevertError(RuntimeError):
    """Raised when patch revert fails."""


class PdbProtocolError(RuntimeError):
    """Raised for PDB protocol-level errors."""


class PdbSessionError(RuntimeError):
    """Base exception for PDB session errors."""


class PdbSessionStateError(PdbSessionError):
    """Raised when a session operation is invalid for the current state."""


class PdbSessionTimeoutError(PdbSessionError):
    """Raised when a session operation times out."""


class PdbWorkerExitedError(PdbSessionError):
    """Raised when the worker process exits unexpectedly."""
