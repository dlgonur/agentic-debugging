from agentic_debugger import AgenticDebuggerError


class RuntimeError(AgenticDebuggerError):
    """Base exception for runtime workspace/command errors."""


class WorkspaceError(RuntimeError):
    """Raised when workspace creation, cleanup, or path resolution fails."""


class CommandRequestError(RuntimeError):
    """Raised when a command request has invalid arguments."""


class CommandExecutionError(RuntimeError):
    """Raised when a command cannot be launched or suffers an infrastructure failure."""
