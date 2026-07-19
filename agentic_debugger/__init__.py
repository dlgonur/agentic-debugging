__version__ = "0.1.0"


class AgenticDebuggerError(Exception):
    """Base exception for all agentic_debugger errors."""


class SchemaValidationError(AgenticDebuggerError):
    """Raised when schema validation fails."""
