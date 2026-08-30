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
    """Raised when a patch fails syntax or validation checks."""

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        hunk_index: int | None = None,
        line_number: int | None = None,
        expected: str | None = None,
        actual: str | None = None,
        current_source_window: str | None = None,
        error_kind: str = "validation_error",
        recoverable: bool = True,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.hunk_index = hunk_index
        self.line_number = line_number
        self.expected = expected
        self.actual = actual
        self.current_source_window = current_source_window
        self.error_kind = error_kind
        self.recoverable = recoverable


class PatchAuthorizationError(PatchValidationError):
    """Raised when a patch targets unauthorized paths or violates security boundaries."""

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        hunk_index: int | None = None,
        line_number: int | None = None,
        expected: str | None = None,
        actual: str | None = None,
        current_source_window: str | None = None,
        error_kind: str = "authorization_error",
        recoverable: bool = False,
    ) -> None:
        super().__init__(
            message,
            path=path,
            hunk_index=hunk_index,
            line_number=line_number,
            expected=expected,
            actual=actual,
            current_source_window=current_source_window,
            error_kind=error_kind,
            recoverable=recoverable,
        )


class PatchApplyError(RuntimeError):
    """Raised when patch application fails."""

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        hunk_index: int | None = None,
        line_number: int | None = None,
        expected: str | None = None,
        actual: str | None = None,
        current_source_window: str | None = None,
        error_kind: str = "apply_error",
        recoverable: bool = False,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.hunk_index = hunk_index
        self.line_number = line_number
        self.expected = expected
        self.actual = actual
        self.current_source_window = current_source_window
        self.error_kind = error_kind
        self.recoverable = recoverable


class PatchStateError(RuntimeError):
    """Raised when patch manager state is invalid."""

    def __init__(
        self,
        message: str,
        *,
        error_kind: str = "state_error",
        recoverable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.recoverable = recoverable


class PatchRevertError(RuntimeError):
    """Raised when patch revert fails."""

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        hunk_index: int | None = None,
        line_number: int | None = None,
        expected: str | None = None,
        actual: str | None = None,
        current_source_window: str | None = None,
        error_kind: str = "revert_failure",
        recoverable: bool = False,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.hunk_index = hunk_index
        self.line_number = line_number
        self.expected = expected
        self.actual = actual
        self.current_source_window = current_source_window
        self.error_kind = error_kind
        self.recoverable = recoverable


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
