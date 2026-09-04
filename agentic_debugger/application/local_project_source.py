"""Local Project Debug execution source — generic Python project, real model/controller.

Model selection is one of two exclusive contracts:
- any enabled configured provider (``provider`` id + ``model_id``) resolves
  through the unified provider registry (``resolve_provider_live_config``),
  exactly like the configured/interactive source;
- otherwise the app-owned configured-command architecture applies:
  CommandModelConfigStore / validated profile (required).

Both reuse:
- CancellableJsonlCommandTransport + LiveModelAdapter (real JSONL)
- DeterministicController + ToolRegistry + PDB + PatchManager
- bounded snapshots, run_reproduction, PDB, apply_patch, syntax, verification

Generic inventory via `git ls-files` (tracked Python files, bounded, no .git).
No invented test/oracle facts; honest LocalProjectTask adapter is used.
Repro omitted -> start UNDERSTAND, no hidden placeholder.
Verify omitted -> UNRESOLVED (conservative).
PDB wired only when repro command resolves to an isolated python script.
Typed disposition returned for worker terminal authority (no sidecar file authority).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional

from agentic_debugger.agent.controller import ControllerRunConfig, ControllerStopReason, DeterministicController
from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger, PdbPolicy
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolExecutionError, ToolRegistry, ToolRejectedError, ToolResult, ToolSpec, ToolTimeoutError
from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.command_config import CommandModelConfigStore
from agentic_debugger.application.command_transport import CancellableJsonlCommandTransport
from agentic_debugger.application.events import SessionEventKind, SessionTerminationReason, contains_credential_shape
from agentic_debugger.application.local_project import assert_path_inside_workspace
from agentic_debugger.application.observability import ObservabilityContext, SessionObservability
from agentic_debugger.application.source_snapshots import SourceSnapshotStage, capture_source_snapshot
from agentic_debugger.application.sources import ModelExecutionError
from agentic_debugger.application.worker_scenarios import ScenarioContext, ScenarioInputError
from agentic_debugger.cancellation import CancellationError
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.evaluation.live import LiveModelAdapter, LiveModelConfig, LiveRunLimits, MAX_MODEL_RESPONSE_BYTES
from agentic_debugger.evaluation.task_schema import Constraints
from agentic_debugger.runtime.exceptions import (
    PatchApplyError,
    PatchAuthorizationError,
    PatchRevertError,
    PatchStateError,
    PatchValidationError,
)

LOCAL_PROJECT_SOURCE_NAME = "local_project"
_KNOWN_PARAMS = frozenset({"project_repo_path","project_head","isolated_workspace","bug_description","reproduction_command","verification_command","config_root","profile_id","expected_fingerprint","parent_tmpdir","policy","is_ollama","ollama_alias","provider","model_id","project_runtime_spec"})
_PROVIDER_KINDS = frozenset({"ollama_cloud","opencode_go","commandcode_goat","configured"})
_MAX_CMD_CHARS=2048
_MAX_BUG_CHARS=4096
_DEFAULT_MAX_MODEL_REQUESTS=32
_DEFAULT_MAX_CONTROLLER_STEPS=64
_DEFAULT_MAX_RETRIES=2

class LocalProjectSourceError(RuntimeError):
    pass

def _require_text(params: Mapping[str, Any], key: str, maximum: int) -> Optional[str]:
    v=params.get(key)
    if v is None: return None
    if type(v) is not str: raise ScenarioInputError(f"local_project param {key!r} must be string or null")
    if v=="": return None
    if len(v.encode("utf-8"))>maximum: raise ScenarioInputError(f"local_project param {key!r} exceeds bound")
    if contains_credential_shape(v): raise ScenarioInputError(f"local_project param {key!r} contains credential shape")
    return v

def _validate_params(params: Mapping[str, Any]) -> dict[str, Any]:
    extra=set(params.keys())-_KNOWN_PARAMS
    if extra: raise ScenarioInputError(f"unknown local_project params: {sorted(extra)}")
    for k in ("project_repo_path","project_head","isolated_workspace","bug_description"):
        if type(params.get(k)) is not str or not params.get(k): raise ScenarioInputError(f"{k} must be non-empty string")
    if len(params["project_head"])!=40: raise ScenarioInputError("project_head must be 40-char SHA")
    if len(params["bug_description"].encode("utf-8"))>_MAX_BUG_CHARS: raise ScenarioInputError("bug_description exceeds 4 KiB")
    profile_id=params.get("profile_id")
    if type(profile_id) is not str or not profile_id: raise ScenarioInputError("profile_id is required for Local Project Debug (model selection is required)")
    config_root=params.get("config_root")
    if type(config_root) is not str or not config_root: raise ScenarioInputError("config_root is required")
    repro=_require_text(params,"reproduction_command",_MAX_CMD_CHARS)
    verify=_require_text(params,"verification_command",_MAX_CMD_CHARS)
    for label, command in (
        ("reproduction_command", repro),
        ("verification_command", verify),
    ):
        if command is None:
            continue
        try:
            argv=_split_command(command)
        except ValueError as exc:
            raise ScenarioInputError(f"{label} cannot be parsed: {exc}") from exc
        if not argv:
            raise ScenarioInputError(f"{label} must contain an executable")
    policy_str=params.get("policy") or "pdb-on-uncertainty"
    if policy_str not in {c.value for c in DemoPolicy}: raise ScenarioInputError(f"unknown policy: {policy_str!r}")
    is_ollama=params.get("is_ollama", False)
    if type(is_ollama) is not bool: raise ScenarioInputError("is_ollama must be a boolean")
    ollama_alias=params.get("ollama_alias")
    if ollama_alias is not None:
        if type(ollama_alias) is not str or not ollama_alias: raise ScenarioInputError("ollama_alias must be a non-empty string or null")
        if len(ollama_alias.encode("utf-8"))>128: raise ScenarioInputError("ollama_alias exceeds bound")
        if contains_credential_shape(ollama_alias): raise ScenarioInputError("ollama_alias contains credential shape")
    provider=params.get("provider")
    if provider is not None:
        try:
            from agentic_debugger.application.provider_connections import is_known_provider
            is_valid_provider = provider in _PROVIDER_KINDS or is_known_provider(provider)
        except Exception:
            is_valid_provider = provider in _PROVIDER_KINDS
        if type(provider) is not str or not is_valid_provider:
            raise ScenarioInputError(
                "provider must be 'configured' or an explicitly configured "
                "provider id (manage providers in Model Providers)"
            )
    model_id=params.get("model_id")
    if model_id is not None:
        if type(model_id) is not str or not model_id: raise ScenarioInputError("model_id must be a non-empty string or null")
        if len(model_id.encode("utf-8"))>128: raise ScenarioInputError("model_id exceeds bound")
        if contains_credential_shape(model_id): raise ScenarioInputError("model_id contains credential shape")
    if provider and provider != "configured" and model_id is None:
        raise ScenarioInputError(f"provider {provider} requires model_id")
    # V2-02 explicit project runtime ingress (safe transport: NAMES,
    # required flags, and non-secret values only — never secret values).
    # Absent/empty means the empty spec: platform essentials alone.
    try:
        from agentic_debugger.application.session_runtime import spec_from_param
        project_runtime_spec = spec_from_param(params.get("project_runtime_spec"))
    except Exception as exc:
        raise ScenarioInputError(f"project runtime spec is invalid: {exc}") from exc
    return {"project_repo_path":params["project_repo_path"],"project_head":params["project_head"],"isolated_workspace":params["isolated_workspace"],"bug_description":params["bug_description"],"reproduction_command":repro,"verification_command":verify,"config_root":config_root,"profile_id":profile_id,"expected_fingerprint":params.get("expected_fingerprint"),"parent_tmpdir":params.get("parent_tmpdir"),"policy":policy_str,"is_ollama":is_ollama,"ollama_alias":ollama_alias,"provider":provider,"model_id":model_id,"project_runtime_spec":project_runtime_spec}

def _bounded(output: str, limit: int=4000) -> str:
    return output[:limit-3]+"..." if len(output)>limit else output

def _split_command(cmd: str) -> list[str]:
    """Split one single-line command into argv using Windows-compatible rules.

    ``shlex.split(..., posix=False)`` keeps quote characters inside tokens, so
    a quoted path (``python "my script.py"``) would reach ``Popen`` with the
    quotes as literal filename characters.  One balanced surrounding quote
    pair per token is stripped (the CreateProcess convention); embedded or
    unbalanced quotes are preserved verbatim and fail naturally at launch.
    """
    argv = shlex.split(cmd, posix=False)
    stripped: list[str] = []
    for token in argv:
        if (
            len(token) >= 2
            and token[0] == token[-1]
            and token[0] in ('"', "'")
        ):
            token = token[1:-1]
        if token:
            stripped.append(token)
    return stripped


def _run_command_bounded(cmd: str, cwd: Path, timeout: float=30.0, cancel_check=None, *, environment: Mapping[str, str]):
    """Run one user command in ``cwd`` through the accepted runtime runner.

    Uses ``runtime.command_runner.CommandRunner`` over the isolated workspace
    (reader threads, process-tree kill ladder, bounded incremental UTF-8
    decoding, cooperative cancellation).  This deliberately does not spawn and
    drain pipes inline: a descendant that inherits the output pipes can never
    wedge the worker.  Returns ``(exit_code, stdout, stderr, elapsed_seconds)``.

    ``environment`` is the explicit project-command child environment
    derived by the session execution authority (declarative project
    runtime).  The runner no longer decides the product environment by
    reading ``os.environ``; every Local Project call site passes the
    role mapping explicitly.
    """
    from agentic_debugger.runtime.command_runner import CommandRunner
    from agentic_debugger.runtime.exceptions import CommandExecutionError

    start = time.monotonic()
    try:
        argv = _split_command(cmd)
    except ValueError as exc:
        return 127, "", f"parse failed: {exc}", time.monotonic() - start
    if not argv:
        return 127, "", "empty", time.monotonic() - start
    runner = CommandRunner(_IsolatedWorkspace(cwd), environment=environment)
    try:
        result = runner.run(argv, ".", timeout, cancel_check=cancel_check)
    except CommandExecutionError as exc:
        return 127, "", f"launch failed: {exc}", time.monotonic() - start
    except CancellationError:
        raise
    if result.exit_code is not None:
        exit_code = result.exit_code
    elif result.timed_out:
        exit_code = 124
    else:
        exit_code = 127
    err = result.stderr or ""
    if result.timed_out:
        err = (err + f" timed out {timeout}s").strip()
    return (
        exit_code,
        _bounded(result.stdout or ""),
        _bounded(err),
        time.monotonic() - start,
    )

def _inventory_tracked_python_files(
    isolated: Path,
    *,
    environment: Optional[Mapping[str, str]] = None,
) -> List[str]:
    # One canonical bounded inventory (local_project.py); this boundary only
    # translates its input errors into the worker's scenario vocabulary.
    # ``environment`` is the explicit project-safe child mapping from the
    # session's V2 execution-environment authority; the real worker always
    # supplies it so the inventory Git child never implicitly inherits
    # worker control/model/provider state.
    from agentic_debugger.application.local_project import inventory_tracked_python_files

    try:
        return inventory_tracked_python_files(isolated, environment=environment)
    except ApplicationInputError as exc:
        raise ScenarioInputError(str(exc)) from exc

# ---------------------------------------------------------------------------
# Honest Local Project task adapter (no invented DebugTask facts)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LocalProjectTask:
    task_id: str = "local-project-debug"
    title: str = "Local Project Debug"
    description: str = ""
    language: str = "python"
    fixture_path: str = "isolated"
    constraints: Constraints = None  # type: ignore
    tracked_files: tuple[str, ...] = ()
    bug_description: str = ""
    reproduction_command: Optional[str] = None
    verification_command: Optional[str] = None

    def agent_visible_mapping(self, resource_limits=None):  # type: ignore[no-untyped-def]
        mapping: dict[str, Any] = {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "language": self.language,
            "fixture_path": self.fixture_path,
            "constraints": self.constraints.to_mapping() if self.constraints else {},
            "tracked_files": list(self.tracked_files)[:20],
            "bug_description": self.bug_description,
        }
        if self.reproduction_command:
            mapping["reproduction_command"] = self.reproduction_command
        if self.verification_command:
            mapping["verification_command"] = self.verification_command
        if resource_limits is not None:
            if not isinstance(resource_limits, Mapping):
                raise ValueError("resource_limits must be mapping")
            constraints = dict(mapping["constraints"])
            constraints.update(dict(resource_limits))
            mapping["constraints"] = constraints
        return mapping

def _build_local_task(bug_description: str, repro_cmd: Optional[str], verify_cmd: Optional[str], isolated: Path, tracked: List[str]) -> tuple[LocalProjectTask, ControllerState]:
    constraints=Constraints(allowed_write_paths=tracked, denied_write_paths=["tests","task.json"], network_allowed=False, external_services_allowed=False, max_patch_attempts=2, max_test_runs=10, max_pdb_observations=5)
    task=LocalProjectTask(
        task_id="local-project-debug",
        title="Local Project Debug",
        description=bug_description,
        language="python",
        fixture_path="isolated",
        constraints=constraints,
        tracked_files=tuple(tracked),
        bug_description=bug_description,
        reproduction_command=repro_cmd,
        verification_command=verify_cmd,
    )
    initial = ControllerState.REPRODUCE if repro_cmd else ControllerState.UNDERSTAND
    return task, initial

def _resolve_pdb_probe(repro_cmd: Optional[str], isolated: Path, pdb_policy) -> Optional[Any]:
    if repro_cmd is None:
        return None
    if pdb_policy is PdbPolicy.DISABLED:
        return None
    try:
        argv=shlex.split(repro_cmd, posix=False)
    except Exception:
        return None
    if len(argv) < 2:
        return None
    if argv[0] not in ("python", "python3"):
        return None
    script = argv[1]
    if not script.endswith(".py"):
        return None
    if script.startswith("/") or script.startswith("\\"):
        return None
    if ".." in script.replace("\\","/").split("/"):
        return None
    if len(script) >= 2 and script[1]==":" and script[0].isalpha():
        return None
    try:
        assert_path_inside_workspace(isolated, script)
    except Exception:
        return None
    full = isolated / script.replace("/", os.sep)
    try:
        if not full.is_file():
            return None
    except Exception:
        return None
    try:
        text=full.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    bp_line=1
    for idx, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("def "):
            bp_line=idx
            break
    try:
        from agentic_debugger.demo.tools import PdbProbe
        return PdbProbe(source_dir=isolated, parent_dir=isolated.parent, script=script, breakpoint_line=bp_line, focus_function=Path(script).stem, exact_public_reproduction=False)
    except Exception:
        return None

class _IsolatedWorkspace:
    def __init__(self, root: Path):
        import os
        self._root=str(root.resolve())
        self._real_root=os.path.realpath(self._root)
        self.root=self._root
    def resolve_path(self, relative_path: str, *, must_exist: bool=False) -> str:
        import os
        if not isinstance(relative_path, str) or not relative_path: raise ValueError("relative_path must be non-empty string")
        if len(relative_path)>=2 and relative_path[1]==":" and relative_path[0].isalpha(): raise ValueError(f"Absolute paths are rejected: {relative_path!r}")
        if relative_path.startswith("/") or relative_path.startswith("\\"): raise ValueError(f"Absolute paths are rejected: {relative_path!r}")
        native=relative_path.replace("/", os.sep).replace("\\", os.sep)
        parts=native.split(os.sep)
        if ".." in parts: raise ValueError(f"Path traversal is rejected: {relative_path!r}")
        resolved=os.path.normpath(os.path.join(self._root, native))
        real_resolved=os.path.realpath(resolved)
        real_root_norm=os.path.normpath(self._real_root)
        try:
            common=os.path.commonpath([real_root_norm, real_resolved])
        except ValueError:
            raise ValueError(f"Resolved path escapes workspace root: {relative_path!r}") from None
        # normcase matches the containment authority in local_project
        # (Windows is case-insensitive; POSIX normcase is a no-op).
        if os.path.normcase(common)!=os.path.normcase(real_root_norm):
            raise ValueError(f"Resolved path escapes workspace root: {relative_path!r}")
        if must_exist and not os.path.exists(resolved): raise ValueError(f"Path does not exist: {relative_path!r}")
        return resolved
    def cleanup(self):
        pass

# ---------------------------------------------------------------------------
# Honest local tool context (no DebugTask fabrications)
# ---------------------------------------------------------------------------

class _LocalToolContext:
    def __init__(self, *, isolated: Path, tracked: List[str], task: LocalProjectTask, probe: Optional[Any], observability: Any, command_environment: Mapping[str, str], pdb_worker_environment: Optional[Mapping[str, str]], executor: Optional[Any] = None, capabilities: Optional[Any] = None):
        from agentic_debugger.runtime.patcher import PatchManager
        self.isolated = isolated
        self.tracked = tracked
        self.task = task
        self.probe = probe
        self.observability = observability
        # V2-02 session execution authority: the fixed role environments
        # derived by the SessionLaunch (declarative project runtime), the
        # logical Executor seam over them, and the computed effective
        # capabilities.  Direct unit/harness construction may leave the
        # seam empty (legacy behavior through the explicit mappings);
        # the real worker/source path always sets it.
        self.command_environment = command_environment
        self.pdb_worker_environment = pdb_worker_environment
        self.executor = executor
        self.capabilities = capabilities
        # V2-02 project-secret egress seal: the session's ONE redaction
        # authority (derived by the SessionLaunch ExecutionEnvironment from
        # the same materialization as the role child environments).  Raw
        # materialized project-secret values are redacted here before any
        # product PDB response/observability payload crosses into the
        # controller/model/evidence domain.  Absent on legacy direct
        # harness construction (historical behavior preserved).
        self.secret_redaction = (
            executor.project_secret_redactor()
            if executor is not None
            and hasattr(executor, "project_secret_redactor")
            else None
        )
        self.workspace = _IsolatedWorkspace(isolated)
        self.patch_manager = PatchManager(self.workspace, list(tracked), ["tests", "task.json"])
        self.candidate_patch = ""
        self.patch_applied = False
        self.patch_changed_files: tuple[str, ...] = ()
        self.patch_attempt_index = 0
        self.tool_calls: list[str] = []
        self.tool_errors: list[dict[str, str]] = []
        self.baseline_failure_reproduced: Optional[bool] = None
        self.post_patch_f2p_passed: Optional[bool] = None
        self.regression_passed: Optional[bool] = None
        self.syntax_passed: Optional[bool] = None
        self.declared_localization: Optional[dict[str, str]] = None
        self.controller_outcome: Optional[str] = None
        self.pdb_session: Optional[Any] = None
        self.pdb_workspace: Optional[Any] = None
        self.pdb_pause_generation: Optional[int] = None
        self.pdb_observation_names: list[str] = []
        self.pdb_session_started = False
        self.interactive_pdb_session_started = False
        self.pdb_proof_contract: Optional[dict[str, Any]] = None
        self.pdb_proof_observations: dict[str, dict[str, Any]] = {}
        # initial snapshots
        if observability is not None:
            for rel in tracked[:3]:
                try:
                    assert_path_inside_workspace(isolated, rel)
                    snap=capture_source_snapshot(isolated, rel, SourceSnapshotStage.INITIAL)
                    observability.source_snapshot(snap)
                except Exception:
                    continue

    def observe(self, fn):  # type: ignore[no-untyped-def]
        if self.observability is None:
            return
        try:
            fn()
        except Exception:
            pass

    def redact_project_output(self, value):  # type: ignore[no-untyped-def]
        """Redact raw materialized project-secret values from one
        product-output structure (identity when the session carries no
        redaction authority).  One authority, never per-handler rules."""
        if self.secret_redaction is None:
            return value
        return self.secret_redaction.redact_structure(value)

    def safe_project_diagnostic(self, exc, workspace_root=None):  # type: ignore[no-untyped-def]
        """Product exception-diagnostic egress (redact BEFORE bounding).

        The FULL exception text crosses the one session redaction
        authority first — including application-created bounded-tail
        fragments (repair 11: the PDB worker marks its own bounded
        diagnostics, and Agentic Debugger-created cuts must never expose a
        raw secret fragment) — and only then takes the established
        ``MAX_DIAGNOSTIC_CHARS`` diagnostic bound.  Without a session
        redaction authority this is the historical ``bounded_diagnostic``
        exactly (legacy direct-harness behavior preserved).
        """
        if self.secret_redaction is None:
            from agentic_debugger.demo.tools import bounded_diagnostic
            return bounded_diagnostic(exc, workspace_root)
        from agentic_debugger.demo.tools import bounded_diagnostic_text
        try:
            text = f"{type(exc).__name__}: {exc}"
        except Exception:
            text = f"{type(exc).__name__}: <unprintable exception>"
        return bounded_diagnostic_text(
            self.secret_redaction.redact_bounded_text(text), workspace_root
        )

    def require_capability(self, capability):  # type: ignore[no-untyped-def]
        """Fail closed (tool-unavailable) when the session denies a capability.

        Direct harness construction without a computed authority keeps the
        historical ungated behavior; the real session path always carries
        the computed EffectiveSessionCapabilities and enforces them.
        """
        if self.capabilities is None:
            return
        try:
            self.capabilities.require(capability)
        except Exception as exc:
            raise ToolRejectedError(str(exc)) from exc

    def run_project_command(self, cmd: str, cwd: Path, timeout: float = 30.0, cancel_check=None):  # type: ignore[no-untyped-def]
        """Run one project command through the Executor seam when present."""
        from agentic_debugger.application.session_runtime import SessionCapability

        self.require_capability(SessionCapability.PROJECT_COMMAND)
        if self.executor is not None:
            from agentic_debugger.runtime.exceptions import CommandExecutionError

            start = time.monotonic()
            try:
                argv = _split_command(cmd)
            except ValueError as exc:
                return 127, "", f"parse failed: {exc}", time.monotonic() - start
            if not argv:
                return 127, "", "empty", time.monotonic() - start
            try:
                result = self.executor.run_project_command(
                    argv, _IsolatedWorkspace(cwd), timeout,
                    cancel_check=cancel_check,
                )
            except CommandExecutionError as exc:
                return 127, "", f"launch failed: {exc}", time.monotonic() - start
            except CancellationError:
                raise
            if result.exit_code is not None:
                exit_code = result.exit_code
            elif result.timed_out:
                exit_code = 124
            else:
                exit_code = 127
            err = result.stderr or ""
            if result.timed_out:
                err = (err + f" timed out {timeout}s").strip()
            return (
                exit_code,
                _bounded(result.stdout or ""),
                _bounded(err),
                time.monotonic() - start,
            )
        return _run_command_bounded(
            cmd, cwd, timeout, cancel_check=cancel_check,
            environment=self.command_environment,
        )

    def open_product_pdb(self, workspace):  # type: ignore[no-untyped-def]
        """Create one product PDB session through the Executor seam when present."""
        from agentic_debugger.application.session_runtime import SessionCapability
        from agentic_debugger.runtime.pdb_session import PdbSession

        self.require_capability(SessionCapability.PDB)
        if self.executor is not None:
            probe = self.probe
            if probe is not None and getattr(probe, "exact_public_reproduction", False):
                return self.executor.open_product_pdb(
                    workspace,
                    startup_timeout=15.0,
                    request_timeout=30.0,
                    proof_pytest_dependencies=True,
                )
            return self.executor.open_product_pdb(workspace)
        if self.probe is not None and getattr(self.probe, "exact_public_reproduction", False) and self.__dict__.get("pdb_session_factory", PdbSession) is PdbSession:
            return PdbSession(workspace, startup_timeout=15.0, request_timeout=30.0, proof_pytest_dependencies=True, worker_environment=self.pdb_worker_environment)
        return PdbSession(workspace, startup_timeout=15.0, request_timeout=60.0, worker_environment=self.pdb_worker_environment)

    def record_error(self, action: str, exc: BaseException) -> None:
        # Egress seal: tool_errors is a product surface; project-domain
        # text carried by an exception (e.g. a PDB worker diagnostic) is
        # redacted through the same session authority BEFORE the
        # diagnostic bound, never after it.
        try:
            diag = self.safe_project_diagnostic(exc, self.workspace.root)
        except Exception:
            diag = str(exc)[:400]
        self.tool_errors.append({"action": action, "diagnostic": diag})

    def require_session(self, action: str):  # type: ignore[no-untyped-def]
        if self.pdb_session is None:
            raise ToolRejectedError(f"{action} requires an active PDB session")
        return self.pdb_session

    def _capture_changed_source(self, stage):  # type: ignore[no-untyped-def]
        for path in self.patch_changed_files:
            try:
                snapshot = capture_source_snapshot(self.workspace.root, path, stage)
            except Exception:
                continue
            self.observe(lambda captured=snapshot: self.observability.source_snapshot(captured))

    def validation_evidence_ready(self) -> bool:
        return self.post_patch_f2p_passed is not None and self.regression_passed is not None

    def clear_validation_evidence(self) -> None:
        self.post_patch_f2p_passed = None
        self.regression_passed = None
        self.controller_outcome = None

    def release_pdb(self):  # type: ignore[no-untyped-def]
        errors: list[BaseException] = []
        session = self.pdb_session
        if session is not None:
            try:
                session.stop()
            except BaseException as exc:
                errors.append(exc)
            else:
                self.pdb_session = None
        workspace = self.pdb_workspace
        if workspace is not None:
            try:
                workspace.cleanup()
                if os.path.exists(workspace.root):
                    raise RuntimeError("PDB workspace root remains after cleanup")
            except BaseException as exc:
                errors.append(exc)
            else:
                self.pdb_workspace = None
        return errors

def _build_local_registry(context: _LocalToolContext, *, pdb_policy: Any = None, interactive_debugger_controls: bool = False) -> ToolRegistry:
    from agentic_debugger.agent.controller_policy import ActionName, HypothesisConfidence
    from agentic_debugger.runtime.pdb_session import PdbSession
    from agentic_debugger.runtime.workspace import TaskWorkspace
    from agentic_debugger.skills.file_skills import get_source_window
    from agentic_debugger.skills.search_skills import find_function
    from agentic_debugger.demo.tools import bounded_diagnostic, DemoToolError, _json_safe, _safe_rejection, MAX_DIAGNOSTIC_CHARS
    from agentic_debugger.evaluation.outcome_taxonomy import classify_outcome
    from agentic_debugger.runtime.exceptions import PatchApplyError, PatchAuthorizationError, PatchStateError, PatchValidationError, PdbSessionError, PdbSessionTimeoutError, SourceInspectionError, SourceParseError, WorkspaceError

    def spec(name, validator, handler):  # type: ignore[no-untyped-def]
        def guarded(action, arguments):  # type: ignore[no-untyped-def]
            context.tool_calls.append(action.name)
            try:
                return handler(action, arguments)
            except BaseException as exc:
                context.record_error(action.name, exc)
                raise
        from agentic_debugger.agent.tool_registry import ToolSpec
        return ToolSpec(name, validator, guarded, version="demo-1", argument_contract=getattr(validator, "argument_contract", {}))

    def _validator(required, optional=None, *, enums=None, minimums=None):  # type: ignore[no-untyped-def]
        optional = optional or {}
        enums = enums or {}
        minimums = minimums or {}
        known = set(required) | set(optional)
        def validate(arguments):  # type: ignore[no-untyped-def]
            if type(arguments) is not dict:
                raise ToolRejectedError("arguments must be a mapping")
            unknown = sorted(set(arguments) - known)
            if unknown:
                raise ToolRejectedError(f"unknown argument: {unknown[0]}")
            missing = sorted(set(required) - set(arguments))
            if missing:
                raise ToolRejectedError(f"missing argument: {missing[0]}")
            for name2, expected in {**required, **optional}.items():
                if name2 not in arguments:
                    continue
                value = arguments[name2]
                if type(value) is not expected:
                    raise ToolRejectedError(f"argument {name2} has the wrong type")
                if expected is str and not value:
                    raise ToolRejectedError(f"argument {name2} must be non-empty")
                if expected is int and value < 0:
                    raise ToolRejectedError(f"argument {name2} must be non-negative")
                if expected is int and name2 in minimums and value < minimums[name2]:
                    raise ToolRejectedError(f"argument {name2} must be at least {minimums[name2]}")
                if name2 in enums and value not in enums[name2]:
                    raise ToolRejectedError(f"argument {name2} has an unsupported value")
            return dict(arguments)
        def type_name(expected):  # type: ignore[no-untyped-def]
            return {str: "string", int: "integer", bool: "boolean"}.get(expected, expected.__name__)
        properties = {}
        for name2, expected in {**required, **optional}.items():
            constraint = {"type": type_name(expected)}
            if expected is str:
                constraint["min_length"] = 1
            if expected is int:
                constraint["minimum"] = minimums.get(name2, 0)
            if name2 in enums:
                constraint["enum"] = list(enums[name2])
            properties[name2] = constraint
        validate.argument_contract = {"required": list(required), "properties": properties, "additional_properties": False}  # type: ignore[attr-defined]
        return validate

    def _ok(payload, summary):  # type: ignore[no-untyped-def]
        from agentic_debugger.events.schema import ObservationStatus
        from agentic_debugger.agent.tool_registry import ToolResult
        return ToolResult(ObservationStatus.OK, payload, summary)

    # -- reproduction -------------------------------------------------------
    def handle_run_reproduction(action, arguments):  # type: ignore[no-untyped-def]
        from agentic_debugger.demo.tools import legal_reproduction_phases
        phase = arguments["phase"]
        if phase not in legal_reproduction_phases(action.state):
            raise ToolRejectedError("phase must be baseline or post_patch")
        if context.task.reproduction_command is None:
            raise ToolExecutionError("no reproduction command configured for this project")
        # Execute the honest reproduction command in the isolated workspace
        # through the session Executor seam (fixed role environment,
        # capability-gated).
        exit_code, out, err, elapsed = context.run_project_command(context.task.reproduction_command, Path(context.workspace.root), timeout=30.0)
        passed = (exit_code == 0)
        # Baseline truth comes from the command itself: a non-zero exit is
        # the observed failure; a zero exit means the reported bug did NOT
        # reproduce and must not satisfy any downstream gate (the user's
        # bug report is not reproduction proof).  Post-patch records
        # ``passed`` (exit==0) and reports no failure reproduction.
        baseline_reproduced = not passed
        failure_output = _bounded((out or "") + (err or ""), 4000)
        # Keep payload honest: failure_reproduced reflects the observed
        # command result, passed reflects the exit code.
        payload = {
            "phase": phase,
            "exit_code": exit_code,
            "passed": bool(passed),
            "failure_reproduced": bool(baseline_reproduced) if phase == "baseline" else False,
            "failure_output": failure_output,
        }
        if phase == "baseline":
            context.baseline_failure_reproduced = bool(baseline_reproduced)
            summary = "baseline reproduction executed"
        else:
            context.post_patch_f2p_passed = bool(passed)
            summary = "post-patch reproduction executed"
        return _ok(payload, summary)

    def handle_run_regression_tests(action, arguments):  # type: ignore[no-untyped-def]
        # Honest verification: run verification_command if present, else no regression
        if context.task.verification_command is None:
            # No verification configured -> conservatively mark regression as passed for controller flow;
            # external verifier will still mark UNRESOLVED.
            context.regression_passed = True
            return _ok({"exit_code": 0, "all_passed": True, "note": "no verification command"}, "no verification command; regression considered passed for controller")
        exit_code, out, err, elapsed = context.run_project_command(context.task.verification_command, Path(context.workspace.root), timeout=30.0)
        all_passed = (exit_code == 0)
        context.regression_passed = all_passed
        return _ok({"exit_code": exit_code, "all_passed": all_passed}, "verification command executed")

    def handle_classify_outcome(action, arguments):  # type: ignore[no-untyped-def]
        if not context.validation_evidence_ready():
            raise ToolExecutionError("validation evidence is incomplete")
        f2p = [context.post_patch_f2p_passed]
        p2p = [context.regression_passed]
        outcome = classify_outcome(f2p, p2p)
        context.controller_outcome = outcome.value
        return _ok({"outcome": outcome.value, "f2p_passed": f2p, "p2p_passed": p2p, "evidence_scope": "controller_validation"}, "controller validation outcome classified")

    def handle_find_function(action, arguments):  # type: ignore[no-untyped-def]
        try:
            match = find_function(context.workspace, arguments["name"], arguments["path"])
        except (SourceInspectionError, SourceParseError, WorkspaceError) as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
        if match is None:
            raise ToolExecutionError("declared symbol was not found in the declared file")
        return _ok(_json_safe(match.to_mapping(), "find_function"), "declared symbol located")

    def handle_get_source_window(action, arguments):  # type: ignore[no-untyped-def]
        line = arguments["line"]
        if line < 1:
            raise ToolRejectedError("line must be positive")
        try:
            window = get_source_window(context.workspace, arguments["path"], line, 6)
        except (SourceInspectionError, WorkspaceError) as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
        return _ok(_json_safe(window.to_mapping(), "get_source_window"), "source window retrieved")

    def handle_express_hypothesis(action, arguments):  # type: ignore[no-untyped-def]
        declared = {
            "hypothesis_id": str(arguments["hypothesis_id"]),
            "statement": str(arguments["statement"]),
            "target_file": str(arguments["target_file"]),
            "target_symbol": str(arguments["target_symbol"]),
            "confidence": str(arguments["confidence"]),
        }
        context.declared_localization = {"file_path": declared["target_file"], "symbol": declared["target_symbol"]}
        context.observe(lambda: context.observability.diagnosis_recorded(text=declared["statement"], file_path=declared["target_file"], symbol=declared["target_symbol"], confidence=declared["confidence"]))
        return _ok(declared, "root-cause hypothesis recorded")

    def handle_apply_patch(action, arguments):  # type: ignore[no-untyped-def]
        from agentic_debugger.application.session_runtime import SessionCapability
        context.require_capability(SessionCapability.PATCH)
        context.clear_validation_evidence()
        diff = arguments["patch"]
        if context.patch_manager.has_active_patch and context.candidate_patch == diff:
            raise ToolRejectedError("the candidate patch is already active")
        attempt_index = context.patch_attempt_index
        context.patch_attempt_index += 1
        import hashlib as _hashlib
        patch_sha256 = _hashlib.sha256(diff.encode("utf-8")).hexdigest()
        reverted_previous = False
        if context.patch_manager.has_active_patch:
            try:
                context.patch_manager.revert_patch()
            except (
                PatchStateError,
                PatchApplyError,
                PatchRevertError,
                Exception,
            ) as exc:
                from agentic_debugger.runtime.patcher import build_bounded_patch_failure_payload
                bounded_diag = bounded_diagnostic(exc, context.workspace.root)
                payload_data, recoverable, error_kind = build_bounded_patch_failure_payload(
                    exc, error_kind="revert_failure", recoverable=False
                )
                context.observe(lambda: context.observability.patch_apply_failed(attempt_index, bounded_diag))
                raise ToolExecutionError(
                    bounded_diag,
                    safe_diagnostic=bounded_diag,
                    recoverable=False,
                    payload_data=payload_data,
                ) from exc
            reverted_previous = True
            context.observe(lambda: context.observability.patch_reverted(attempt_index - 1))
            context._capture_changed_source(SourceSnapshotStage.REVERTED)
        context.observe(lambda: context.observability.patch_proposed(attempt_index, patch_sha256, patch_text=diff))
        try:
            result = context.patch_manager.apply_patch(diff)
        except (
            PatchValidationError,
            PatchAuthorizationError,
            PatchStateError,
            PatchApplyError,
            PatchRevertError,
            Exception,
        ) as exc:
            from agentic_debugger.runtime.patcher import build_bounded_patch_failure_payload
            bounded_diag = bounded_diagnostic(exc, context.workspace.root)
            payload_data, recoverable, error_kind = build_bounded_patch_failure_payload(exc)

            if isinstance(
                exc,
                (
                    PatchValidationError,
                    PatchAuthorizationError,
                    PatchStateError,
                ),
            ):
                context.observe(
                    lambda: context.observability.patch_rejected(
                        attempt_index, bounded_diag
                    )
                )
                raise ToolRejectedError(
                    bounded_diag,
                    safe_diagnostic=bounded_diag,
                    recoverable=recoverable,
                    payload_data=payload_data,
                ) from exc
            else:
                context.observe(
                    lambda: context.observability.patch_apply_failed(
                        attempt_index, bounded_diag
                    )
                )
                raise ToolExecutionError(
                    bounded_diag,
                    safe_diagnostic=bounded_diag,
                    recoverable=recoverable,
                    payload_data=payload_data,
                ) from exc
        context.candidate_patch = diff
        context.patch_applied = bool(result.success)
        context.patch_changed_files = tuple(sorted(item.path for item in result.changed_files))
        context.observe(lambda: context.observability.patch_applied(attempt_index, context.patch_changed_files, None))
        context._capture_changed_source(SourceSnapshotStage.APPLIED)
        payload = {
            "applied": bool(result.success),
            "changed_files": list(context.patch_changed_files),
            "hunk_count": result.hunk_count,
            "patch_sha256": _hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            "after_sha256": {key: result.after_sha256[key] for key in sorted(result.after_sha256)},
            "hunk_adjustments": [list(item) for item in result.hunk_adjustments],
            "reverted_previous": reverted_previous,
        }
        return _ok(_json_safe(payload, "apply_patch"), "candidate patch applied to the disposable workspace")

    def handle_revert_patch(action, arguments):  # type: ignore[no-untyped-def]
        from agentic_debugger.application.session_runtime import SessionCapability
        context.require_capability(SessionCapability.PATCH)
        try:
            result = context.patch_manager.revert_patch()
        except (
            PatchStateError,
            PatchApplyError,
            PatchRevertError,
            Exception,
        ) as exc:
            from agentic_debugger.runtime.patcher import build_bounded_patch_failure_payload
            bounded_diag = bounded_diagnostic(exc, context.workspace.root)
            payload_data, recoverable, error_kind = build_bounded_patch_failure_payload(
                exc, error_kind="revert_failure", recoverable=False
            )
            if isinstance(exc, PatchStateError):
                raise ToolRejectedError(
                    bounded_diag,
                    safe_diagnostic=bounded_diag,
                    recoverable=False,
                    payload_data=payload_data,
                ) from exc
            raise ToolExecutionError(
                bounded_diag,
                safe_diagnostic=bounded_diag,
                recoverable=False,
                payload_data=payload_data,
            ) from exc
        changed_files = tuple(sorted(item.path for item in result.changed_files))
        reverted_index = max(0, context.patch_attempt_index - 1)
        context.observe(lambda: context.observability.patch_reverted(reverted_index))
        for path in changed_files:
            try:
                snapshot = capture_source_snapshot(context.workspace.root, path, SourceSnapshotStage.REVERTED)
            except Exception:
                continue
            context.observe(lambda captured=snapshot: context.observability.source_snapshot(captured))
        context.candidate_patch = ""
        context.patch_applied = False
        context.patch_changed_files = ()
        context.syntax_passed = None
        context.clear_validation_evidence()
        return _ok({"reverted": True, "changed_files": list(changed_files)}, "accepted candidate patch reverted from the disposable workspace")

    def handle_syntax_check(action, arguments):  # type: ignore[no-untyped-def]
        from agentic_debugger.application.session_runtime import SessionCapability
        context.require_capability(SessionCapability.PATCH)
        try:
            result = context.patch_manager.syntax_check()
        except (
            PatchStateError,
            PatchApplyError,
            PatchRevertError,
            Exception,
        ) as exc:
            from agentic_debugger.runtime.patcher import build_bounded_patch_failure_payload
            bounded_diag = bounded_diagnostic(exc, context.workspace.root)
            payload_data, recoverable, error_kind = build_bounded_patch_failure_payload(
                exc, error_kind="syntax_check_failure", recoverable=False
            )
            raise ToolExecutionError(
                bounded_diag,
                safe_diagnostic=bounded_diag,
                recoverable=False,
                payload_data=payload_data,
            ) from exc
        context.syntax_passed = bool(result.all_passed)
        return _ok({"all_passed": bool(result.all_passed), "results": [item.to_mapping() for item in result.results]}, "patched source syntax validated")

    # -- PDB (honest, targets resolved repro script) -----------------------
    def create_pdb_session(workspace):  # type: ignore[no-untyped-def]
        # V2-02: the ordinary product PDB worker is created through the
        # session Executor seam (fixed PRODUCT_PDB role environment,
        # PDB-capability-gated); Windows venv identity still travels
        # through build_worker_env inside PdbSession.
        return context.open_product_pdb(workspace)

    def handle_start_pdb(action, arguments):  # type: ignore[no-untyped-def]
        from agentic_debugger.application.session_runtime import SessionCapability
        # Session capability first (the computed session authority), then
        # the existing task PDB policy: both fail closed as tool-unavailable.
        context.require_capability(SessionCapability.PDB)
        if pdb_policy is PdbPolicy.DISABLED:
            raise ToolRejectedError("PDB access is disabled by evaluation policy")
        probe = context.probe
        if probe is None:
            raise ToolRejectedError("no runtime probe is configured for this task")
        if context.pdb_session is not None:
            raise ToolRejectedError("a PDB session is already active")
        if interactive_debugger_controls and context.interactive_pdb_session_started and not getattr(probe, "exact_public_reproduction", False):
            raise ToolRejectedError("interactive debugger pilot permits one PDB session per case")
        try:
            workspace = TaskWorkspace(str(probe.source_dir), parent_dir=str(probe.parent_dir))
        except WorkspaceError as exc:
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        context.pdb_workspace = workspace
        session = create_pdb_session(workspace)
        context.pdb_session = session
        breakpoint_line = int(arguments["breakpoint_line"]) if interactive_debugger_controls else probe.breakpoint_line
        if breakpoint_line <= 0:
            context.release_pdb()
            raise ToolRejectedError("breakpoint_line must be positive")
        try:
            session.start()
            context.pdb_session_started = True
            started = session.start_paused_target(probe.script, [breakpoint_line])
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = context.safe_project_diagnostic(exc)
            context.release_pdb()
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        if started.get("state") != "paused":
            context.release_pdb()
            raise ToolExecutionError("runtime probe did not reach the declared breakpoint", safe_diagnostic="runtime probe did not reach the declared breakpoint")
        # Egress seal: one sanitized object feeds BOTH the observability
        # event and the model payload (never raw-then-sanitized).
        started = context.redact_project_output(started)
        if interactive_debugger_controls:
            context.interactive_pdb_session_started = True
        context.pdb_pause_generation = 1
        context.observe(lambda: context.observability.debugger_started(probe.script, [f"{probe.script}:{breakpoint_line}"]))
        context.observe(lambda: context.observability.location_changed(started["script"], started["line"], started["function"], 1))
        payload = {"state": "paused", "script": started["script"], "line": started["line"], "function": started["function"], "breakpoint_line": breakpoint_line}
        if not interactive_debugger_controls:
            payload["focus_function"] = probe.focus_function
        return _ok(payload, "runtime probe paused at the declared breakpoint")

    def handle_stack_summary(action, arguments):  # type: ignore[no-untyped-def]
        session = context.require_session("get_stack_summary")
        try:
            stack = session.get_stack_summary()
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = context.safe_project_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        # Egress seal: sanitize once; the SAME object is observed and returned.
        stack = context.redact_project_output(stack)
        generation = stack.get("pause_generation")
        if type(generation) is not int:
            raise ToolExecutionError("stack summary did not report a pause generation", safe_diagnostic="stack summary did not report a pause generation")
        context.pdb_pause_generation = generation
        context.pdb_observation_names.append("get_stack_summary")
        context.observe(lambda: context.observability.stack_observed(dict(stack)))
        return _ok(_json_safe(dict(stack), "get_stack_summary"), "bounded stack summary collected")

    def handle_frame_locals(action, arguments):  # type: ignore[no-untyped-def]
        session = context.require_session("get_frame_locals")
        try:
            result = session.get_frame_locals(int(arguments["frame_id"]), int(arguments["pause_generation"]))
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = context.safe_project_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        # Egress seal: sanitize once; the SAME object is observed and returned.
        result = context.redact_project_output(result)
        context.pdb_observation_names.append("get_frame_locals")
        context.observe(lambda: context.observability.locals_observed(dict(result)))
        return _ok(_json_safe(dict(result), "get_frame_locals"), "bounded frame locals collected")

    def handle_safe_eval(action, arguments):  # type: ignore[no-untyped-def]
        session = context.require_session("safe_eval_expression")
        try:
            result = session.safe_eval_expression(int(arguments["frame_id"]), int(arguments["pause_generation"]), str(arguments["expression"]))
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = context.safe_project_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        # Egress seal: evaluated runtime values are project-domain output.
        result = context.redact_project_output(result)
        context.pdb_observation_names.append("safe_eval_expression")
        return _ok(_json_safe(dict(result), "safe_eval_expression"), "restricted runtime expression evaluated")

    def handle_execution_control(action, arguments):  # type: ignore[no-untyped-def]
        from agentic_debugger.agent.controller_policy import ActionName
        session = context.require_session(action.name)
        operation = {ActionName.CONTINUE_PDB_SESSION: session.continue_paused_target, ActionName.STEP_PDB_SESSION: session.step_paused_target, ActionName.NEXT_PDB_SESSION: session.next_paused_target}[ActionName(action.name)]
        try:
            result = operation()
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = context.safe_project_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        # Egress seal: one sanitized object for observation and payload.
        result = context.redact_project_output(result)
        if result.get("state") == "paused":
            context.pdb_pause_generation = (context.pdb_pause_generation or 0) + 1
            context.observe(lambda: context.observability.location_changed(result["script"], result["line"], result["function"], context.pdb_pause_generation))
        context.pdb_observation_names.append(action.name)
        control_payload = _json_safe(dict(result), action.name)
        if result.get("state") != "paused":
            errors = context.release_pdb()
            if errors:
                diag = context.safe_project_diagnostic(
                    errors[0], context.workspace.root
                )
                raise ToolExecutionError(diag, safe_diagnostic=diag)
            control_payload["session_released"] = True
        return _ok(control_payload, f"debugger execution control completed: {action.name}")

    def handle_stop_pdb(action, arguments):  # type: ignore[no-untyped-def]
        started = context.pdb_session_started
        had_workspace = context.pdb_workspace is not None
        errors = context.release_pdb()
        if errors:
            diag = context.safe_project_diagnostic(
                errors[0], context.workspace.root
            )
            raise ToolExecutionError(diag, safe_diagnostic=diag)
        return _ok({"stopped": context.pdb_session is None, "session_started": started, "workspace_removed": had_workspace and context.pdb_workspace is None}, "PDB session stopped and its workspace released")

    diagnosis_required = {"hypothesis_id": str, "statement": str, "target_file": str, "target_symbol": str, "confidence": str}
    tool_specs = [
        spec(ActionName.RUN_REPRODUCTION, _validator({"phase": str}), handle_run_reproduction),
        spec(ActionName.RUN_REGRESSION_TESTS, _validator({}), handle_run_regression_tests),
        spec(ActionName.CLASSIFY_OUTCOME, _validator({}), handle_classify_outcome),
        spec(ActionName.FIND_FUNCTION, _validator({"name": str, "path": str}), handle_find_function),
        spec(ActionName.GET_SOURCE_WINDOW, _validator({"path": str, "line": int}, minimums={"line": 1}), handle_get_source_window),
        spec(ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS, _validator(diagnosis_required, enums={"confidence": tuple(item.value for item in HypothesisConfidence)}), handle_express_hypothesis),
        spec(ActionName.APPLY_PATCH, _validator({"patch": str}), handle_apply_patch),
        spec(ActionName.REVERT_PATCH, _validator({}), handle_revert_patch),
        spec(ActionName.SYNTAX_CHECK, _validator({}), handle_syntax_check),
        spec(ActionName.START_PDB_SESSION, _validator({"breakpoint_line": int} if interactive_debugger_controls else {}, minimums={"breakpoint_line": 1} if interactive_debugger_controls else None), handle_start_pdb),
        spec(ActionName.GET_STACK_SUMMARY, _validator({}), handle_stack_summary),
        spec(ActionName.GET_FRAME_LOCALS, _validator({"frame_id": int, "pause_generation": int}), handle_frame_locals),
        spec(ActionName.SAFE_EVAL_EXPRESSION, _validator({"frame_id": int, "pause_generation": int, "expression": str}), handle_safe_eval),
    ]
    if interactive_debugger_controls:
        control_validator = _validator({})
        tool_specs.extend([
            spec(ActionName.CONTINUE_PDB_SESSION, control_validator, handle_execution_control),
            spec(ActionName.STEP_PDB_SESSION, control_validator, handle_execution_control),
            spec(ActionName.NEXT_PDB_SESSION, control_validator, handle_execution_control),
        ])
    tool_specs.append(spec(ActionName.STOP_PDB_SESSION, _validator({}), handle_stop_pdb))
    return ToolRegistry(tuple(tool_specs))

def run_local_project_session(ctx: ScenarioContext, params: Mapping[str, Any]) -> str:
    """Returns disposition string: FIXED or UNRESOLVED (typed, not sidecar)."""
    validated=_validate_params(params)
    isolated=Path(validated["isolated_workspace"])
    repo_root=Path(validated["project_repo_path"])
    bug_description=validated["bug_description"]
    repro_cmd=validated["reproduction_command"]
    verify_cmd=validated["verification_command"]
    config_root=validated["config_root"]
    expected_fp=validated["expected_fingerprint"]
    if ctx.emitter is None: raise ScenarioInputError("local_project requires emitter")
    if not isolated.is_dir(): raise ScenarioInputError(f"isolated workspace missing: {isolated}")
    # V2-02 SessionLaunch authority: one launch binding per session.  The
    # worker builds it once before dispatch (see ``worker.run_worker``)
    # and carries it on the ScenarioContext so the source, its
    # project/PDB/verifier children, AND terminal worker cleanup all share
    # one authority without recomputing any session-start fact.  Direct
    # (non-worker) callers have no context launch, so the source builds
    # one here through the same factory as the narrow fallback.
    # Project/PDB/verifier children receive explicit derived role
    # environments (declarative project runtime: platform essentials plus
    # the fixed per-session materialization — never arbitrary ambient
    # inheritance); none of them inherits the worker process environment
    # implicitly, so Agentic Debugger control/model/provider channels
    # cannot leak into project execution.  Values stay inside these
    # mappings — they are never logged, journaled, or exposed to the
    # controller/model.
    from agentic_debugger.application.execution_environment import ExecutionEnvironment, ExecutionRole
    from agentic_debugger.application.executor import ProductExecutor
    from agentic_debugger.application.session_runtime import (
        SessionLaunch,
        build_local_project_launch,
    )
    _ctx_launch = getattr(ctx, "session_launch", None)
    if _ctx_launch is not None:
        if type(_ctx_launch) is not SessionLaunch:
            raise ScenarioInputError("local_project session_launch must be a SessionLaunch or None")
        if _ctx_launch.session_id != ctx.emitter.session_id:
            raise ScenarioInputError("session launch identity does not match the session")
        if _ctx_launch.task_id != ctx.emitter.task_id:
            raise ScenarioInputError("session launch task does not match the session")
        # Corroboration-only: legacy transport params may confirm the
        # authoritative launch but never override it.  Any contradiction
        # in a mirrored session-start fact fails closed here, before any
        # project/model execution.  Comparison is by safe
        # representation/fingerprint — never materialized values.
        from agentic_debugger.application.session_runtime import (
            check_launch_matches_params,
        )
        try:
            check_launch_matches_params(
                _ctx_launch,
                policy=validated["policy"],
                provider_id=validated["provider"],
                model_id=validated["model_id"],
                profile_id=validated["profile_id"],
                project_spec=validated["project_runtime_spec"],
            )
        except Exception as exc:
            raise ScenarioInputError(f"session launch mismatch: {exc}") from exc
        session_launch = _ctx_launch
    else:
        _ctx_authority = getattr(ctx, "product_environment", None)
        if _ctx_authority is not None and not isinstance(_ctx_authority, ExecutionEnvironment):
            raise ScenarioInputError("local_project product_environment must be an ExecutionEnvironment or None")
        if _ctx_authority is not None and _ctx_authority.uses_legacy_bridge:
            raise ScenarioInputError("local_project requires a declarative session authority")
        try:
            session_launch = build_local_project_launch(
                session_id=ctx.emitter.session_id,
                task_id=ctx.emitter.task_id,
                policy=validated["policy"],
                provider_id=validated["provider"],
                model_id=validated["model_id"],
                profile_id=validated["profile_id"],
                launch_snapshot=dict(os.environ),
                project_spec=validated["project_runtime_spec"],
            )
        except Exception as exc:
            raise ScenarioInputError(f"session launch failed: {exc}") from exc
    execution_environment = session_launch.execution_environment
    project_command_environment=execution_environment.role_environment(ExecutionRole.PROJECT_COMMAND)
    pdb_worker_environment=execution_environment.role_environment(ExecutionRole.PRODUCT_PDB)
    verifier_command_environment=execution_environment.role_environment(ExecutionRole.VERIFIER)
    session_executor = ProductExecutor(
        execution_environment=execution_environment,
        capabilities=session_launch.capabilities,
    )
    session_capabilities = session_launch.capabilities
    # Single-authority rebind: from here on, the session-start facts owned
    # by SessionLaunch come from the launch, never from the mirrored
    # validated transport params above (those were only its construction
    # input for the fallback, or corroboration for a supplied launch).
    # Genuinely source-specific facts (repository/worktree paths, bug
    # description, repro/verify commands, config root, legacy Ollama
    # routing markers) stay on the validated params.
    policy = DemoPolicy(session_launch.agent.controller_policy)
    provider = session_launch.agent.provider_id
    model_id = session_launch.agent.model_id
    profile_id = session_launch.profile_id
    is_ollama = validated["is_ollama"]
    ollama_alias = validated["ollama_alias"] or (
        profile_id if provider is None and is_ollama else None
    )
    profile = None
    ollama_profile = None
    provider_live_config = None
    provider_provenance = None
    if provider is not None and provider != "configured":
        # Registry-authority routing: ANY enabled configured provider —
        # including arbitrary user-configured direct-API providers —
        # resolves through the unified registry, so there is one validated
        # construction path, fail-closed availability, and no credential
        # material anywhere near the journal.  The special ``configured``
        # provider id remains the app-owned command-profile store contract
        # handled below; an unconfigured provider id fails closed here.
        from agentic_debugger.application.model_providers import (
            ProviderRegistryError,
            provider_transport_environment,
            resolve_provider_live_config,
        )
        try:
            provider_live_config, provider_provenance = resolve_provider_live_config(
                provider,
                model_id,
                logical_call_ceiling=_DEFAULT_MAX_MODEL_REQUESTS,
            )
        except ProviderRegistryError as exc:
            raise ScenarioInputError(f"provider model unavailable: {exc}") from exc
    elif provider is None and is_ollama and ollama_alias:
        try:
            from agentic_debugger.application.level32 import level32_model_profiles
            for m in level32_model_profiles():
                if m.alias == ollama_alias:
                    ollama_profile = m
                    break
            if ollama_profile is None:
                raise ScenarioInputError(f"Ollama model not in qualified roster: {ollama_alias}")
        except ScenarioInputError:
            raise
        except Exception as exc:
            raise ScenarioInputError(f"Ollama qualification failed: {exc}") from exc
    else:
        from agentic_debugger.application.command_config import CommandModelConfigStore, CommandConfigError
        try:
            store=CommandModelConfigStore(Path(config_root))
            profile=store.get(profile_id)
        except CommandConfigError as exc:
            raise ScenarioInputError(f"model profile unavailable: {exc}") from exc
        if expected_fp is not None and profile.configuration_fingerprint!=expected_fp:
            raise ScenarioInputError("model profile fingerprint mismatch")
    # Session/task identity likewise comes from the launch (proven equal
    # to the emitter binding by the checks above / by fallback
    # construction); run identity stays on the context (not launch-owned).
    session_id=session_launch.session_id
    task_id=session_launch.task_id
    source_kind=ctx.emitter.source_kind
    run_id=ctx.run_id or f"{task_id}--local"
    observability=SessionObservability(ObservabilityContext(session_id=session_id, task_id=task_id, source_kind=source_kind, run_id=ctx.run_id), emitter=ctx.emitter)
    ctx.token.check()
    observability.diagnosis_recorded(text=bug_description, file_path=None, symbol=None, confidence="user-reported", observed_values={"repo_basename": repo_root.name, "source_head": validated["project_head"][:12]})
    tracked=_inventory_tracked_python_files(isolated, environment=project_command_environment)
    local_task, initial_state=_build_local_task(bug_description, repro_cmd, verify_cmd, isolated, tracked)
    if repro_cmd:
        # Baseline reproduction runs through the session Executor seam
        # (fixed PROJECT_COMMAND role environment, capability-gated).
        from agentic_debugger.runtime.exceptions import CommandExecutionError as _InitialCommandError
        _start = time.monotonic()
        try:
            _initial_argv = _split_command(repro_cmd)
        except ValueError as exc:
            exit_code, out, err = 127, "", f"parse failed: {exc}"
        else:
            if not _initial_argv:
                exit_code, out, err = 127, "", "empty"
            else:
                try:
                    _initial_result = session_executor.run_project_command(
                        _initial_argv, _IsolatedWorkspace(isolated), 30.0,
                        cancel_check=ctx.token.check,
                    )
                except _InitialCommandError as exc:
                    exit_code, out, err = 127, "", f"launch failed: {exc}"
                else:
                    if _initial_result.exit_code is not None:
                        exit_code = _initial_result.exit_code
                    elif _initial_result.timed_out:
                        exit_code = 124
                    else:
                        exit_code = 127
                    out, err = _initial_result.stdout or "", _initial_result.stderr or ""
                    if _initial_result.timed_out:
                        err = (err + " timed out 30.0s").strip()
                    out, err = _bounded(out), _bounded(err)
        repro_output=_bounded(out+err, 2000)
        try:
            observability.diagnosis_recorded(text=f"reproduction result exit {exit_code}: {repro_output[:500]}", file_path=None, symbol=None, confidence="observed")
        except: pass
        initial_state=ControllerState.REPRODUCE
    else:
        initial_state=ControllerState.UNDERSTAND
        try:
            observability.diagnosis_recorded(text="no reproduction command supplied; starting from bug description and source inspection", file_path=None, symbol=None, confidence="observed")
        except: pass
    probe=_resolve_pdb_probe(repro_cmd, isolated, pdb_policy_for(policy))
    ctx.token.check()
    ctx.emitter.emit(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"})
    for rel in tracked[:3]:
        try:
            assert_path_inside_workspace(isolated, rel)
            snap=capture_source_snapshot(isolated, rel, SourceSnapshotStage.INITIAL)
            observability.source_snapshot(snap)
        except Exception:
            continue
    demo_context=_LocalToolContext(isolated=isolated, tracked=tracked, task=local_task, probe=probe, observability=observability, command_environment=project_command_environment, pdb_worker_environment=pdb_worker_environment, executor=session_executor, capabilities=session_capabilities)
    registry=_build_local_registry(demo_context, pdb_policy=pdb_policy_for(policy), interactive_debugger_controls=False)
    if provider_live_config is not None:
        live_config=provider_live_config
        limits=LiveRunLimits(max_model_requests=_DEFAULT_MAX_MODEL_REQUESTS, max_controller_steps=_DEFAULT_MAX_CONTROLLER_STEPS, max_elapsed_seconds=None, max_retries=_DEFAULT_MAX_RETRIES, max_directive_repairs=_DEFAULT_MAX_RETRIES, max_response_bytes=MAX_MODEL_RESPONSE_BYTES)
        # Direct-API routes receive exactly one bounded credential
        # override in the adapter child environment (never argv, never
        # evidence); legacy CLI routes read the operator auth store in
        # place and need no override.
        transport_environment = provider_transport_environment(provider)
        transport=CancellableJsonlCommandTransport(live_config, max_output_bytes=limits.max_response_bytes, cancel_check=ctx.token.check, activity_observer=ctx.liveness_reporter, environment=dict(transport_environment) if transport_environment else None)
    elif ollama_profile is not None:
        from scripts.ollama_cloud_command_adapter import build_ollama_live_config
        live_config = build_ollama_live_config(ollama_profile.alias, logical_call_ceiling=_DEFAULT_MAX_MODEL_REQUESTS)
        limits=LiveRunLimits(max_model_requests=_DEFAULT_MAX_MODEL_REQUESTS, max_controller_steps=_DEFAULT_MAX_CONTROLLER_STEPS, max_elapsed_seconds=None, max_retries=_DEFAULT_MAX_RETRIES, max_directive_repairs=_DEFAULT_MAX_RETRIES, max_response_bytes=MAX_MODEL_RESPONSE_BYTES)
        transport=CancellableJsonlCommandTransport(live_config, max_output_bytes=limits.max_response_bytes, cancel_check=ctx.token.check, activity_observer=ctx.liveness_reporter)
    else:
        live_config=LiveModelConfig(model_name=profile.display_name, command=profile.live_command(), request_timeout_seconds=profile.request_timeout_seconds, tool_version=profile.tool_version)
        limits=LiveRunLimits(max_model_requests=_DEFAULT_MAX_MODEL_REQUESTS, max_controller_steps=_DEFAULT_MAX_CONTROLLER_STEPS, max_elapsed_seconds=None, max_retries=_DEFAULT_MAX_RETRIES, max_directive_repairs=_DEFAULT_MAX_RETRIES, max_response_bytes=MAX_MODEL_RESPONSE_BYTES)
        transport=CancellableJsonlCommandTransport(live_config, max_output_bytes=limits.max_response_bytes, cancel_check=ctx.token.check, activity_observer=ctx.liveness_reporter, cwd=profile.cwd, environment=dict(profile.environment) if profile.environment else None)
    # Safe durable model provenance (same contract as the configured/ladder
    # sources): the journal records which model actually serves this Local
    # Project session, so replay needs no live workspace or sidecar file.
    # MODEL_CONFIGURED is mandatory durable provenance: if the authoritative
    # journal cannot record it, the emitter failure (EmitterFatalError)
    # propagates through the existing journal-fatal worker contract and no
    # model request may start.  The payload carries only profile identity
    # fields, never credentials.
    if provider_provenance is not None:
        model_provenance_payload=dict(provider_provenance)
        model_provenance_payload["config_fingerprint"]=live_config.configuration_fingerprint
    elif ollama_profile is not None:
        model_provenance_payload={
            "provider": "ollama_cloud",
            "profile_id": ollama_profile.alias,
            "config_fingerprint": ollama_profile.transport_config_fingerprint,
            "display_name": ollama_profile.display_name,
            "protocol_version": "1.3",
            "tool_version": live_config.tool_version,
        }
    else:
        model_provenance_payload={
            "provider": "configured",
            "profile_id": profile.profile_id,
            "config_fingerprint": profile.configuration_fingerprint,
            "display_name": profile.display_name,
            "protocol_version": profile.protocol_version,
            "tool_version": profile.tool_version,
        }
    ctx.emitter.emit(SessionEventKind.MODEL_CONFIGURED, model_provenance_payload)
    adapters: list[Any]=[]
    def _model_factory(dctx, reg):  # type: ignore[no-untyped-def]
        adapter=LiveModelAdapter(task=dctx.task, policy=policy, config=live_config, transport=transport, limits=limits, registry=reg, evaluation_id=session_id, case_id=f"{session_id}:{task_id}", run_id=run_id, trajectory_id=run_id)
        adapters.append(adapter)
        return adapter
    from agentic_debugger.application.controller_adapter import ControllerSessionEventAdapter, ControllerObservationContext
    controller_obs=ControllerSessionEventAdapter(ControllerObservationContext(session_id=session_id, task_id=task_id, source_kind=source_kind, run_id=ctx.run_id), emitter=ctx.emitter)
    snapshot=ControllerSnapshot(run_id=run_id, task_id=task_id, state=initial_state, model_call_index=0, budget_limits=ControllerBudgetLimits.from_task_constraints(local_task.constraints), budget_state=ControllerBudgetState(), hypotheses=HypothesisLedger())
    model=_model_factory(demo_context, registry)
    controller=DeterministicController(registry, model, ControllerRunConfig(max_model_calls=_DEFAULT_MAX_MODEL_REQUESTS, require_pdb_evidence_before_patch=False), observer=controller_obs)
    try:
        result=controller.run(snapshot, cancel_check=ctx.token.check)
    except ModelExecutionError:
        raise
    except Exception as exc:
        if adapters:
            tr=adapters[-1].metrics.termination_reason
            if tr:
                raise ModelExecutionError(f"{exc} (model transport: {tr})", SessionTerminationReason.MODEL_ERROR if tr=="model_error" else SessionTerminationReason.CONTROLLER_FAILED) from exc
        raise LocalProjectSourceError(f"controller failed: {exc}") from exc
    ctx.token.check()
    has_active_candidate=bool(
        demo_context.patch_applied and demo_context.candidate_patch
    )
    if result.stop_reason is not ControllerStopReason.DONE:
        # A controller classification is not the correctness authority.  If
        # it left an active, schema-valid candidate, retain the candidate and
        # let the independent verifier decide.  Transport failure or a run
        # with no candidate remains an operational controller failure.
        from agentic_debugger.application.local_source import _controller_failure_category
        _, term_reason=_controller_failure_category(result)
        transport_reason=(
            adapters[-1].metrics.termination_reason if adapters else None
        )
        if transport_reason:
            ctx.emitter.emit(SessionEventKind.DIAGNOSIS_RECORDED, {"text": f"controller did not complete: {result.stop_reason.value}", "file_path": None, "symbol": None, "confidence": "observed"})
            raise ModelExecutionError(f"controller run ended without completion (stop: {result.stop_reason.value}) (model transport: {transport_reason})", term_reason)
        if not has_active_candidate:
            ctx.emitter.emit(SessionEventKind.DIAGNOSIS_RECORDED, {"text": f"controller did not complete: {result.stop_reason.value}", "file_path": None, "symbol": None, "confidence": "observed"})
            raise ModelExecutionError(f"controller run ended without completion (stop: {result.stop_reason.value})", term_reason)
        ctx.emitter.emit(
            SessionEventKind.DIAGNOSIS_RECORDED,
            {
                "text": (
                    "controller stopped without a success claim; active candidate "
                    "retained for independent verification"
                ),
                "file_path": None,
                "symbol": None,
                "confidence": "system",
            },
        )
    patch_text: Optional[str]=demo_context.candidate_patch if has_active_candidate else None
    verification_result: Optional[Any]=None
    verified_fixed=False
    from agentic_debugger.application.session_runtime import SessionCapability as _VerifierCapability
    verifier_granted = session_capabilities.has(_VerifierCapability.VERIFIER)
    if has_active_candidate and patch_text is not None and verifier_granted:
        from agentic_debugger.application.verifier_observer import (
            VerifierSessionEventAdapter,
        )
        from agentic_debugger.evaluation.local_project_verifier import (
            LocalProjectEvaluationPlan,
            LocalProjectVerifier,
        )

        verifier_events=VerifierSessionEventAdapter(
            ObservabilityContext(
                session_id=session_id,
                task_id=task_id,
                source_kind=source_kind,
                run_id=ctx.run_id,
            ),
            emitter=ctx.emitter,
        )
        verifier_events.started()
        verification_plan=LocalProjectEvaluationPlan(
            source_repo_path=str(repo_root),
            source_head_commit=validated["project_head"],
            candidate_patch=patch_text,
            reproduction_argv=(tuple(_split_command(repro_cmd)) if repro_cmd else None),
            regression_argv=(tuple(_split_command(verify_cmd)) if verify_cmd else None),
            allowed_paths=tuple(tracked),
            denied_paths=("tests", "task.json"),
            timeout_seconds=30.0,
            workspace_parent=validated.get("parent_tmpdir"),
        )
        # V2-02 verifier environment seam (single fixed authority): the
        # session execution authority supplies ONE verifier-role mapping
        # (declarative project runtime — no Agentic Debugger
        # control/provider authority) plus the SAME per-session
        # project-secret redaction authority (derived by the launch
        # environment from the one materialization — never a second,
        # independently resolvable one).  The verifier copies the mapping
        # once and uses it for BOTH its CommandRunner children and its
        # owned Git subprocesses; the environment is never a model-selected
        # tool argument and cannot be mutated once verification begins.
        independent_verifier=LocalProjectVerifier(
            progress_observer=verifier_events,
            cancel_check=ctx.token.check,
            product_environment=dict(verifier_command_environment),
            product_secret_redactor=execution_environment.project_secret_redactor(),
        )
        verification_result=independent_verifier.evaluate(verification_plan)
        verifier_events.completed(verification_result)
        verified_fixed=verification_result.resolved
    elif has_active_candidate and patch_text is not None and not verifier_granted:
        ctx.emitter.emit(
            SessionEventKind.DIAGNOSIS_RECORDED,
            {
                "text": "independent verification not run: verifier capability is not available in this session",
                "file_path": None,
                "symbol": None,
                "confidence": "system",
            },
        )
    else:
        ctx.emitter.emit(
            SessionEventKind.DIAGNOSIS_RECORDED,
            {
                "text": "independent verification not run: no active candidate patch",
                "file_path": None,
                "symbol": None,
                "confidence": "system",
            },
        )
    session_dir=ctx.session_dir
    disposition="FIXED" if verified_fixed else "UNRESOLVED"
    artifact_failures: list[str]=[]
    if session_dir is not None:
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            artifact_failures.append(f"session directory unavailable: {exc}")
        if has_active_candidate and patch_text and not contains_credential_shape(patch_text):
            try:
                candidate_path=session_dir / "candidate.patch"
                candidate_path.write_text(patch_text, encoding="utf-8", newline="\n")
                sha=hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
                ctx.emitter.emit(SessionEventKind.ARTIFACT_WRITTEN, {"path": "candidate.patch", "sha256": sha})
                for rel in tracked[:1]:
                    try:
                        snap=capture_source_snapshot(isolated, rel, SourceSnapshotStage.APPLIED)
                        observability.source_snapshot(snap)
                        break
                    except Exception:
                        continue
            except Exception as exc:
                # The candidate artifact is required for Apply To Project;
                # its absence must be visible, not silent.
                artifact_failures.append(f"candidate.patch write failed: {exc}")
        if verification_result is not None:
            try:
                from agentic_debugger.application.local_project import (
                    LOCAL_PROJECT_VERIFICATION_FILE_NAME,
                    LocalProjectTaskSpec,
                    LocalProjectVerificationCertificate,
                    local_project_task_spec_sha256,
                )
                from agentic_debugger.evaluation.runner import TestRecordStatus

                task_spec = LocalProjectTaskSpec.from_mapping(
                    json.loads(
                        (session_dir / "local_project_task.json").read_text(
                            encoding="utf-8"
                        )
                    )
                )
                certificate=LocalProjectVerificationCertificate(
                    task_id=task_id,
                    session_id=task_spec.session_id,
                    task_spec_sha256=local_project_task_spec_sha256(task_spec),
                    source_head_commit=verification_result.source_head_commit,
                    candidate_sha256=verification_result.candidate_sha256,
                    status=verification_result.status.value,
                    outcome=(
                        verification_result.outcome.value
                        if verification_result.outcome is not None
                        else None
                    ),
                    baseline_failure_reproduced=bool(
                        verification_result.baseline_reproduction is not None
                        and verification_result.baseline_reproduction.status
                        is TestRecordStatus.FAIL
                    ),
                    baseline_regression_passed=bool(
                        verification_result.baseline_regression is not None
                        and verification_result.baseline_regression.passed
                    ),
                    post_patch_reproduction_passed=bool(
                        verification_result.post_patch_reproduction is not None
                        and verification_result.post_patch_reproduction.passed
                    ),
                    regression_passed=bool(
                        verification_result.regression is not None
                        and verification_result.regression.passed
                    ),
                    f2p_passed=verification_result.f2p_passed,
                    f2p_total=verification_result.f2p_total,
                    p2p_passed=verification_result.p2p_passed,
                    p2p_total=verification_result.p2p_total,
                    verifier_workspace_cleaned=verification_result.workspace.cleaned,
                    source_repo_unchanged=(
                        verification_result.workspace.canonical_fixture_unchanged
                    ),
                )
                certificate_path=session_dir / LOCAL_PROJECT_VERIFICATION_FILE_NAME
                certificate_path.write_text(
                    json.dumps(certificate.to_mapping(), indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                certificate_sha=hashlib.sha256(certificate_path.read_bytes()).hexdigest()
                ctx.emitter.emit(
                    SessionEventKind.ARTIFACT_WRITTEN,
                    {
                        "path": LOCAL_PROJECT_VERIFICATION_FILE_NAME,
                        "sha256": certificate_sha,
                    },
                )
            except Exception as exc:
                artifact_failures.append(
                    f"local_project_verification.json write failed: {exc}"
                )
        try:
            # Keep the canonical artifact authoritative.  The app pre-writes
            # this file as ``LocalProjectTaskSpec.to_mapping()`` before the
            # worker starts; the session must not replace it with a second,
            # incompatible schema (Apply To Project and history/reopen read
            # the canonical ``source_repo_path`` / ``source_head_commit``).
            # ``from_mapping`` is read strictly (unknown keys rejected) and
            # this writer is the same authority that produced the file, so a
            # failure here is an honest artifact failure, not a silent
            # rewrite of the contract.
            task_path=session_dir / "local_project_task.json"
            from agentic_debugger.application.local_project import (
                LocalProjectTaskSpec,
                SessionBudgets,
            )
            _spec=LocalProjectTaskSpec.from_mapping(json.loads(task_path.read_text(encoding="utf-8")))
            _new_spec=LocalProjectTaskSpec(
                session_id=_spec.session_id,
                source_repo_path=_spec.source_repo_path,
                source_head_commit=_spec.source_head_commit,
                isolated_workspace_path=_spec.isolated_workspace_path,
                bug_description=_spec.bug_description,
                reproduction_command=_spec.reproduction_command,
                verification_command=_spec.verification_command,
                model_runtime=_spec.model_runtime,
                budgets=SessionBudgets(**_spec.budgets.to_mapping()),
                created_at_utc=_spec.created_at_utc,
                project_runtime=dict(_spec.project_runtime),
            )
            task_path.write_text(
                json.dumps(_new_spec.to_mapping(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception as exc:
            artifact_failures.append(f"local_project_task.json write failed: {exc}")
        # Audit sidecar (not authority; the typed return is authoritative).
        try:
            disp_path=session_dir / "local_project_disposition.json"
            disp_path.write_text(
                json.dumps(
                    {
                        "disposition": disposition,
                        "has_active_candidate": has_active_candidate,
                        "verifier_status": (
                            verification_result.status.value
                            if verification_result is not None
                            else None
                        ),
                        "verifier_outcome": (
                            verification_result.outcome.value
                            if verification_result is not None
                            and verification_result.outcome is not None
                            else None
                        ),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            artifact_failures.append(f"local_project_disposition.json write failed: {exc}")
    for failure in artifact_failures[:4]:
        try:
            ctx.emitter.emit(SessionEventKind.DIAGNOSIS_RECORDED, {"text": _bounded(failure, 400), "file_path": None, "symbol": None, "confidence": "system"})
        except Exception:
            pass
    ctx.emitter.emit(SessionEventKind.CONTROLLER_STEP, {"step_index": 2, "directive_kind": "verification", "stop_reason": "done" if verified_fixed else "unresolved"})
    ctx.token.check()
    return disposition

__all__=["LOCAL_PROJECT_SOURCE_NAME","LocalProjectTask","run_local_project_session"]
