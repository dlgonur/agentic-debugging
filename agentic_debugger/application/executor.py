"""V2-02 logical Executor seam over the existing execution machinery.

No new process: :class:`ProductExecutor` is an in-process façade that one
Local Project session builds once from its fixed
:class:`~agentic_debugger.application.execution_environment.ExecutionEnvironment`
and :class:`~agentic_debugger.application.session_runtime.EffectiveSessionCapabilities`.
It establishes the product execution boundary (control never touches
filesystem/process APIs except through it) while the existing runtime
modules (``CommandRunner``, ``PdbSession``) stay the implementation
underneath.

Adopted operations in V2-02 (narrow on purpose — no speculative complete
API):

- :meth:`ProductExecutor.run_project_command` — project
  reproduction/regression command execution;
- :meth:`ProductExecutor.open_product_pdb` — product PDB worker creation
  with the fixed PDB role environment.

Patch application/revert/syntax and verifier invocation stay in their
existing modules for this stage (compatibility delegation), but they are
gated on the same session capabilities at their existing call sites, so
every V2-02 capability is really consumed.  Later stages may migrate more
operations behind this seam without changing its dependency direction.
"""

from __future__ import annotations

from typing import Any, Callable, List, Mapping, Optional

from agentic_debugger.agent.tool_registry import ToolRejectedError
from agentic_debugger.application.execution_environment import (
    ExecutionEnvironment,
    ExecutionRole,
)
from agentic_debugger.application.session_runtime import (
    EffectiveSessionCapabilities,
    SessionCapability,
    SessionRuntimeError,
)
from agentic_debugger.runtime.command_runner import CommandRunner, CommandResult


class ProductExecutor:
    """One session's fixed logical execution authority (in-process façade)."""

    def __init__(
        self,
        *,
        execution_environment: ExecutionEnvironment,
        capabilities: EffectiveSessionCapabilities,
    ) -> None:
        if type(execution_environment) is not ExecutionEnvironment:
            raise SessionRuntimeError(
                "execution_environment must be an ExecutionEnvironment"
            )
        if type(capabilities) is not EffectiveSessionCapabilities:
            raise SessionRuntimeError(
                "capabilities must be an EffectiveSessionCapabilities"
            )
        self._environment = execution_environment
        self._capabilities = capabilities

    @property
    def execution_environment(self) -> ExecutionEnvironment:
        return self._environment

    @property
    def capabilities(self) -> EffectiveSessionCapabilities:
        return self._capabilities

    def require(self, capability: SessionCapability) -> None:
        """Fail closed (tool-unavailable) when a capability was not granted."""
        try:
            self._capabilities.require(capability)
        except Exception as exc:
            raise ToolRejectedError(str(exc)) from exc

    def run_project_command(
        self,
        argv: List[str],
        workspace: Any,
        timeout_seconds: float,
        *,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> CommandResult:
        """Run one project command under the fixed PROJECT_COMMAND role.

        Uses the existing :class:`CommandRunner` behavior unchanged
        (reader threads, kill ladder, bounded output, cooperative
        cancellation); only the environment authority and the capability
        gate are new.  Creates no process of its own.
        """
        self.require(SessionCapability.PROJECT_COMMAND)
        runner = CommandRunner(
            workspace,
            environment=self._environment.role_environment(
                ExecutionRole.PROJECT_COMMAND
            ),
        )
        return runner.run(
            argv, ".", timeout_seconds, cancel_check=cancel_check
        )

    def open_product_pdb(
        self,
        workspace: Any,
        *,
        startup_timeout: float = 15.0,
        request_timeout: float = 60.0,
        proof_pytest_dependencies: bool = False,
    ) -> Any:
        """Create (not start) one product PDB session under the fixed PDB role.

        Returns an unstarted :class:`~agentic_debugger.runtime.pdb_session.PdbSession`
        carrying the explicit PRODUCT_PDB role environment; Windows venv
        identity still travels through the established ``build_worker_env``
        authority inside ``PdbSession``.  The caller keeps the existing
        start/handshake/target lifecycle unchanged.
        """
        from agentic_debugger.runtime.pdb_session import PdbSession

        self.require(SessionCapability.PDB)
        return PdbSession(
            workspace,
            startup_timeout=startup_timeout,
            request_timeout=request_timeout,
            proof_pytest_dependencies=proof_pytest_dependencies,
            worker_environment=dict(
                self._environment.role_environment(ExecutionRole.PRODUCT_PDB)
            ),
        )

    def verifier_environment(self) -> Mapping[str, str]:
        """The fixed VERIFIER role mapping (for the verifier seam call site)."""
        self.require(SessionCapability.VERIFIER)
        return self._environment.role_environment(ExecutionRole.VERIFIER)

    def cleanup_environment(self) -> Mapping[str, str]:
        """The least-authority CLEANUP role mapping (essentials only)."""
        return self._environment.role_environment(ExecutionRole.CLEANUP)

    def __repr__(self) -> str:
        return f"ProductExecutor({self._capabilities!r})"


__all__ = ["ProductExecutor"]
