"""Local Project execution-source input helpers and task adaptation.

This module owns the bounded input/command surface beneath the Local
Project execution source: scenario-param validation (known keys, bounds,
credential-shape rejection, provider/model/profile coherence), the
Windows-compatible single-line command splitter, bounded command output,
the environment-aware tracked-file inventory translation, the honest
``LocalProjectTask`` adapter (no invented DebugTask facts), PDB probe
preparation, and the path-sandboxed isolated workspace view used by the
tool context and the session runner.

Dependency rule: imported by :mod:`agentic_debugger.application.local_project_tools`
and :mod:`agentic_debugger.application.local_project_source`; never
imports either (no cycles).
"""

from __future__ import annotations

import os
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional

from agentic_debugger.agent.controller_policy import PdbPolicy
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.events import contains_credential_shape
from agentic_debugger.application.local_project import assert_path_inside_workspace
from agentic_debugger.application.worker_scenarios import ScenarioInputError
from agentic_debugger.cancellation import CancellationError
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.task_schema import Constraints

LOCAL_PROJECT_SOURCE_NAME = "local_project"
_KNOWN_PARAMS = frozenset({"project_repo_path","project_head","isolated_workspace","bug_description","reproduction_command","verification_command","config_root","profile_id","expected_fingerprint","parent_tmpdir","policy","is_ollama","ollama_alias","provider","model_id","project_runtime_spec"})
_PROVIDER_KINDS = frozenset({"ollama_cloud","opencode_go","commandcode_goat","configured"})
_MAX_CMD_CHARS=2048
_MAX_BUG_CHARS=4096
#: Task 44: historical finite defaults retained as provenance constants
#: only.  Generic Local Project execution is unbounded (None/0) and
#: never consults these values.
_DEFAULT_MAX_CONTROLLER_STEPS=64
_DEFAULT_MAX_RETRIES=2
#: Unbounded sentinel for the provider-adapter logical ceiling.
_UNBOUNDED_LOGICAL_CEILING=0

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
            from agentic_debugger.application.model_gateway import ModelGateway
            is_valid_provider = provider in _PROVIDER_KINDS or ModelGateway.is_known_provider(provider)
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
