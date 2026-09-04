"""V2-02 logical Executor seam over the existing execution machinery.

No new process: :class:`ProductExecutor` is an in-process façade that one
Local Project session builds once from its fixed
:class:`~agentic_debugger.application.execution_environment.ExecutionEnvironment`
and :class:`~agentic_debugger.application.session_runtime.EffectiveSessionCapabilities`.
It establishes the product execution boundary (control never touches
filesystem/process APIs except through it) while the existing runtime
modules (``CommandRunner``, ``PdbSession``) stay the implementation
underneath.

Project-secret egress seal: :meth:`ProductExecutor.run_project_command`
redacts raw materialized project-secret values from the child's textual
output through the session's ONE
:class:`~agentic_debugger.application.execution_environment.ProjectSecretRedactor`
before the :class:`~agentic_debugger.runtime.command_runner.CommandResult`
crosses back into the Local Project/control plane.  The child itself
receives the exact raw secret via the fixed role environment and executes
normally; exit code, timeout state, argv, cwd, and truncation flags are
preserved untouched.  The generic CommandRunner behavior is unchanged.

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

from dataclasses import replace as _dataclass_replace
from typing import Any, Callable, List, Mapping, Optional

from agentic_debugger.agent.tool_registry import ToolRejectedError
from agentic_debugger.application.execution_environment import (
    ExecutionEnvironment,
    ExecutionRole,
    ProjectSecretRedactor,
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

    def project_secret_redactor(self) -> Optional[ProjectSecretRedactor]:
        """The session's one project-secret redaction authority (or ``None``).

        Delegated to the fixed ExecutionEnvironment so there is exactly one
        authority per session; never a second, independently resolvable one.
        """
        return self._environment.project_secret_redactor()

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

        Egress seal: after the runner returns, raw materialized
        project-secret values in the child's stdout/stderr are replaced
        through the session's one redaction authority before the result
        crosses back into the Local Project/control plane.  Everything
        else (exit code, timeout state, argv, cwd, duration, truncation
        flags) is preserved exactly.
        """
        self.require(SessionCapability.PROJECT_COMMAND)
        runner = CommandRunner(
            workspace,
            environment=self._environment.role_environment(
                ExecutionRole.PROJECT_COMMAND
            ),
        )
        result = runner.run(argv, ".", timeout_seconds, cancel_check=cancel_check)
        redactor = self._environment.project_secret_redactor()
        if redactor is not None and (result.stdout or result.stderr):
            result = _dataclass_replace(
                result,
                stdout=redactor.redact(result.stdout),
                stderr=redactor.redact(result.stderr),
            )
        return result

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
