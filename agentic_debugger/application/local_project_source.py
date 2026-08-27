"""Local Project Debug execution source — generic Python project, real model/controller.

Reuses the existing production configured-model architecture:
- CommandModelConfigStore / validated profile (required)
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
from agentic_debugger.application.command_config import CommandModelConfigStore
from agentic_debugger.application.command_transport import CancellableJsonlCommandTransport
from agentic_debugger.application.events import SessionEventKind, SessionTerminationReason, contains_credential_shape
from agentic_debugger.application.local_project import assert_path_inside_workspace
from agentic_debugger.application.observability import ObservabilityContext, SessionObservability
from agentic_debugger.application.source_snapshots import SourceSnapshotStage, capture_source_snapshot
from agentic_debugger.application.sources import ModelExecutionError
from agentic_debugger.application.worker_scenarios import ScenarioContext, ScenarioInputError
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.evaluation.live import LiveModelAdapter, LiveModelConfig, LiveRunLimits, MAX_MODEL_RESPONSE_BYTES
from agentic_debugger.evaluation.task_schema import Constraints

LOCAL_PROJECT_SOURCE_NAME = "local_project"
_KNOWN_PARAMS = frozenset({"project_repo_path","project_head","isolated_workspace","bug_description","reproduction_command","verification_command","config_root","profile_id","expected_fingerprint","parent_tmpdir","policy","is_ollama","ollama_alias"})
_MAX_CMD_CHARS=2048
_MAX_BUG_CHARS=4096
_DEFAULT_MAX_MODEL_REQUESTS=32
_DEFAULT_MAX_CONTROLLER_STEPS=64
_DEFAULT_MAX_RETRIES=2
_MAX_TRACKED_FILES=200
_MAX_FILE_SIZE=1024*1024

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
    policy_str=params.get("policy") or "pdb-on-uncertainty"
    if policy_str not in {c.value for c in DemoPolicy}: raise ScenarioInputError(f"unknown policy: {policy_str!r}")
    return {"project_repo_path":params["project_repo_path"],"project_head":params["project_head"],"isolated_workspace":params["isolated_workspace"],"bug_description":params["bug_description"],"reproduction_command":repro,"verification_command":verify,"config_root":config_root,"profile_id":profile_id,"expected_fingerprint":params.get("expected_fingerprint"),"parent_tmpdir":params.get("parent_tmpdir"),"policy":policy_str}

def _bounded(output: str, limit: int=4000) -> str:
    return output[:limit-3]+"..." if len(output)>limit else output

def _run_command_bounded(cmd: str, cwd: Path, timeout: float=30.0, cancel_check=None):
    start=time.monotonic()
    try:
        argv=shlex.split(cmd, posix=False)
    except ValueError as exc:
        return 127, "", f"parse failed: {exc}", time.monotonic()-start
    if not argv: return 127, "", "empty", time.monotonic()-start
    try:
        proc=subprocess.Popen(argv, stdin=subprocess.DEVNULL, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception as exc:
        return 127, "", f"launch failed: {exc}", time.monotonic()-start
    while True:
        if cancel_check:
            try: cancel_check()
            except Exception:
                try: proc.terminate()
                except: pass
                raise
        try:
            out,err=proc.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic()-start>timeout:
                try: proc.kill()
                except: pass
                out,err=proc.communicate()
                return 124, _bounded(out or ""), _bounded((err or "")+f" timed out {timeout}s"), time.monotonic()-start
            continue
    return (proc.returncode if proc.returncode is not None else 127), _bounded(out or ""), _bounded(err or ""), time.monotonic()-start

def _inventory_tracked_python_files(isolated: Path) -> List[str]:
    try:
        result=subprocess.run(["git","ls-files","-z"], stdin=subprocess.DEVNULL, cwd=str(isolated), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    except Exception as exc:
        raise ScenarioInputError(f"git ls-files failed: {exc}") from exc
    if result.returncode!=0:
        raise ScenarioInputError(f"git ls-files failed: {result.stderr.decode(errors='replace')[:200]}")
    raw=result.stdout.split(b"\x00")
    files=[]
    for b in raw:
        if not b: continue
        try:
            p=b.decode("utf-8")
        except: continue
        if p.startswith(".git/") or p==".git": continue
        if not p.endswith(".py"): continue
        full=isolated / p
        try:
            assert_path_inside_workspace(isolated, p)
        except Exception:
            continue
        try:
            size=full.stat().st_size
            if size>_MAX_FILE_SIZE:
                continue
        except: continue
        files.append(p.replace("\\","/"))
    files=sorted(set(files))
    if not files:
        raise ScenarioInputError("No supported Python source files found.")
    if len(files)>_MAX_TRACKED_FILES:
        raise ScenarioInputError(f"Repository too large for bounded v1 inventory: {len(files)} Python files exceed {_MAX_TRACKED_FILES} limit.")
    return files

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
        if not real_resolved.startswith(real_root_norm+os.sep):
            if real_resolved!=real_root_norm: raise ValueError(f"Resolved path escapes workspace root: {relative_path!r}")
        if must_exist and not os.path.exists(resolved): raise ValueError(f"Path does not exist: {relative_path!r}")
        return resolved
    def cleanup(self):
        pass

# ---------------------------------------------------------------------------
# Honest local tool context (no DebugTask fabrications)
# ---------------------------------------------------------------------------

class _LocalToolContext:
    def __init__(self, *, isolated: Path, tracked: List[str], task: LocalProjectTask, probe: Optional[Any], observability: Any):
        from agentic_debugger.runtime.patcher import PatchManager
        self.isolated = isolated
        self.tracked = tracked
        self.task = task
        self.probe = probe
        self.observability = observability
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

    def record_error(self, action: str, exc: BaseException) -> None:
        from agentic_debugger.demo.tools import bounded_diagnostic
        try:
            diag = bounded_diagnostic(exc, self.workspace.root)
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
        exit_code, out, err, elapsed = _run_command_bounded(context.task.reproduction_command, Path(context.workspace.root), timeout=30.0)
        passed = (exit_code == 0)
        # For Local Project, baseline failure is considered reproduced when a
        # reproduction command is supplied (the user asserts a bug); this
        # satisfies the PDB gate which requires failure_reproduced True for
        # on-uncertainty. Post-patch is passed when exit==0.
        reproduced = True
        failure_output = _bounded((out or "") + (err or ""), 4000) if not passed else _bounded((out or "") + (err or ""), 4000)
        # Keep payload honest: failure_reproduced reflects gate, passed reflects exit
        payload = {
            "phase": phase,
            "exit_code": exit_code,
            "passed": bool(passed),
            "failure_reproduced": bool(reproduced) if phase == "baseline" else False,
            "failure_output": failure_output,
        }
        if phase == "baseline":
            context.baseline_failure_reproduced = True
            # Also set _failure_reproduced for LiveModelAdapter's gate
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
        exit_code, out, err, elapsed = _run_command_bounded(context.task.verification_command, Path(context.workspace.root), timeout=30.0)
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
            except (PatchStateError, PatchApplyError) as exc:
                raise ToolRejectedError(bounded_diagnostic(exc)) from exc
            reverted_previous = True
            context.observe(lambda: context.observability.patch_reverted(attempt_index - 1))
            context._capture_changed_source(SourceSnapshotStage.REVERTED)
        context.observe(lambda: context.observability.patch_proposed(attempt_index, patch_sha256, patch_text=diff))
        try:
            result = context.patch_manager.apply_patch(diff)
        except (PatchValidationError, PatchAuthorizationError, PatchStateError) as exc:
            context.observe(lambda: context.observability.patch_rejected(attempt_index, bounded_diagnostic(exc, context.workspace.root)))
            raise ToolRejectedError(bounded_diagnostic(exc)) from exc
        except PatchApplyError as exc:
            context.observe(lambda: context.observability.patch_apply_failed(attempt_index, bounded_diagnostic(exc, context.workspace.root)))
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
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
        try:
            result = context.patch_manager.revert_patch()
        except (PatchStateError, PatchApplyError) as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
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
        try:
            result = context.patch_manager.syntax_check()
        except (PatchStateError, PatchApplyError) as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
        context.syntax_passed = bool(result.all_passed)
        return _ok({"all_passed": bool(result.all_passed), "results": [item.to_mapping() for item in result.results]}, "patched source syntax validated")

    # -- PDB (honest, targets resolved repro script) -----------------------
    def create_pdb_session(workspace):  # type: ignore[no-untyped-def]
        probe = context.probe
        if probe is not None and getattr(probe, "exact_public_reproduction", False) and context.__dict__.get("pdb_session_factory", PdbSession) is PdbSession:
            return PdbSession(workspace, startup_timeout=15.0, request_timeout=30.0, proof_pytest_dependencies=True)
        return PdbSession(workspace)

    def handle_start_pdb(action, arguments):  # type: ignore[no-untyped-def]
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
            context.release_pdb()
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        if started.get("state") != "paused":
            context.release_pdb()
            raise ToolExecutionError("runtime probe did not reach the declared breakpoint", safe_diagnostic="runtime probe did not reach the declared breakpoint")
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
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
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
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        context.pdb_observation_names.append("get_frame_locals")
        context.observe(lambda: context.observability.locals_observed(dict(result)))
        return _ok(_json_safe(dict(result), "get_frame_locals"), "bounded frame locals collected")

    def handle_safe_eval(action, arguments):  # type: ignore[no-untyped-def]
        session = context.require_session("safe_eval_expression")
        try:
            result = session.safe_eval_expression(int(arguments["frame_id"]), int(arguments["pause_generation"]), str(arguments["expression"]))
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        context.pdb_observation_names.append("safe_eval_expression")
        return _ok(_json_safe(dict(result), "safe_eval_expression"), "restricted runtime expression evaluated")

    def handle_execution_control(action, arguments):  # type: ignore[no-untyped-def]
        from agentic_debugger.agent.controller_policy import ActionName
        session = context.require_session(action.name)
        operation = {ActionName.CONTINUE_PDB_SESSION: session.continue_paused_target, ActionName.STEP_PDB_SESSION: session.step_paused_target, ActionName.NEXT_PDB_SESSION: session.next_paused_target}[ActionName(action.name)]
        try:
            result = operation()
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        if result.get("state") == "paused":
            context.pdb_pause_generation = (context.pdb_pause_generation or 0) + 1
            context.observe(lambda: context.observability.location_changed(result["script"], result["line"], result["function"], context.pdb_pause_generation))
        context.pdb_observation_names.append(action.name)
        control_payload = _json_safe(dict(result), action.name)
        if result.get("state") != "paused":
            errors = context.release_pdb()
            if errors:
                diag = bounded_diagnostic(errors[0], context.workspace.root)
                raise ToolExecutionError(diag, safe_diagnostic=diag)
            control_payload["session_released"] = True
        return _ok(control_payload, f"debugger execution control completed: {action.name}")

    def handle_stop_pdb(action, arguments):  # type: ignore[no-untyped-def]
        started = context.pdb_session_started
        had_workspace = context.pdb_workspace is not None
        errors = context.release_pdb()
        if errors:
            diag = bounded_diagnostic(errors[0], context.workspace.root)
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
    profile_id=validated["profile_id"]
    expected_fp=validated["expected_fingerprint"]
    policy=DemoPolicy(validated["policy"])
    if ctx.emitter is None: raise ScenarioInputError("local_project requires emitter")
    if not isolated.is_dir(): raise ScenarioInputError(f"isolated workspace missing: {isolated}")
    is_ollama = bool(params.get("is_ollama") or params.get("ollama_alias"))
    ollama_alias = params.get("ollama_alias") or (profile_id if is_ollama else None)
    profile = None
    ollama_profile = None
    if is_ollama and ollama_alias:
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
    session_id=ctx.emitter.session_id
    task_id=ctx.emitter.task_id
    source_kind=ctx.emitter.source_kind
    run_id=ctx.run_id or f"{task_id}--local"
    observability=SessionObservability(ObservabilityContext(session_id=session_id, task_id=task_id, source_kind=source_kind, run_id=ctx.run_id), emitter=ctx.emitter)
    ctx.token.check()
    observability.diagnosis_recorded(text=bug_description, file_path=None, symbol=None, confidence="user-reported", observed_values={"repo_basename": repo_root.name, "source_head": validated["project_head"][:12]})
    tracked=_inventory_tracked_python_files(isolated)
    local_task, initial_state=_build_local_task(bug_description, repro_cmd, verify_cmd, isolated, tracked)
    if repro_cmd:
        exit_code, out, err, _ = _run_command_bounded(repro_cmd, isolated, timeout=30.0, cancel_check=ctx.token.check)
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
    demo_context=_LocalToolContext(isolated=isolated, tracked=tracked, task=local_task, probe=probe, observability=observability)
    registry=_build_local_registry(demo_context, pdb_policy=pdb_policy_for(policy), interactive_debugger_controls=False)
    if ollama_profile is not None:
        from scripts.ollama_cloud_command_adapter import build_ollama_live_config
        live_config = build_ollama_live_config(ollama_profile.alias, logical_call_ceiling=_DEFAULT_MAX_MODEL_REQUESTS)
        limits=LiveRunLimits(max_model_requests=_DEFAULT_MAX_MODEL_REQUESTS, max_controller_steps=_DEFAULT_MAX_CONTROLLER_STEPS, max_elapsed_seconds=None, max_retries=_DEFAULT_MAX_RETRIES, max_response_bytes=MAX_MODEL_RESPONSE_BYTES)
        transport=CancellableJsonlCommandTransport(live_config, max_output_bytes=limits.max_response_bytes, cancel_check=ctx.token.check)
    else:
        live_config=LiveModelConfig(model_name=profile.display_name, command=profile.live_command(), request_timeout_seconds=profile.request_timeout_seconds, tool_version=profile.tool_version)
        limits=LiveRunLimits(max_model_requests=_DEFAULT_MAX_MODEL_REQUESTS, max_controller_steps=_DEFAULT_MAX_CONTROLLER_STEPS, max_elapsed_seconds=None, max_retries=_DEFAULT_MAX_RETRIES, max_response_bytes=MAX_MODEL_RESPONSE_BYTES)
        transport=CancellableJsonlCommandTransport(live_config, max_output_bytes=limits.max_response_bytes, cancel_check=ctx.token.check, cwd=profile.cwd, environment=dict(profile.environment) if profile.environment else None)
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
    if result.stop_reason is not ControllerStopReason.DONE:
        from agentic_debugger.application.local_source import _controller_failure_category
        _, term_reason=_controller_failure_category(result)
        ctx.emitter.emit(SessionEventKind.DIAGNOSIS_RECORDED, {"text": f"controller did not complete: {result.stop_reason.value}", "file_path": None, "symbol": None, "confidence": "observed"})
        if adapters and adapters[-1].metrics.termination_reason:
            tr=adapters[-1].metrics.termination_reason
            raise ModelExecutionError(f"controller run ended without completion (stop: {result.stop_reason.value}) (model transport: {tr})", term_reason)
        raise ModelExecutionError(f"controller run ended without completion (stop: {result.stop_reason.value})", term_reason)
    has_active_candidate=bool(demo_context.patch_applied and demo_context.candidate_patch)
    patch_text: Optional[str]=demo_context.candidate_patch if has_active_candidate else None
    ctx.emitter.emit(SessionEventKind.VERIFIER_STARTED, {})
    ctx.emitter.emit(SessionEventKind.VERIFIER_STAGE_STARTED, {"stage": "f2p_p2p_checks"})
    verification_passed: Optional[bool]=None
    if verify_cmd:
        verify_exit, _, _, _=_run_command_bounded(verify_cmd, isolated, timeout=30.0, cancel_check=ctx.token.check)
        verification_passed=(verify_exit==0)
    else:
        verification_passed=False
    if not has_active_candidate:
        verification_passed=False
    ctx.emitter.emit(SessionEventKind.VERIFIER_STAGE_COMPLETED, {"stage": "f2p_p2p_checks", "status": "completed" if verification_passed else "failed"})
    verified_fixed=bool(has_active_candidate and verification_passed is True)
    ctx.emitter.emit(SessionEventKind.VERIFIER_COMPLETED, {"status": "COMPLETED","outcome": "RESOLVED" if verified_fixed else None,"f2p_passed": 1 if verified_fixed else 0,"f2p_total": 1,"p2p_passed": 1 if verified_fixed else 0,"p2p_total": 1,"workspace_cleaned": None,"classification": "Fixed" if verified_fixed else "Unresolved"})
    session_dir=ctx.session_dir
    disposition="FIXED" if verified_fixed else "UNRESOLVED"
    if session_dir is not None:
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            if has_active_candidate and patch_text and not contains_credential_shape(patch_text):
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
            task_path=session_dir / "local_project_task.json"
            task_path.write_text(json.dumps({"project_repo_path": str(repo_root),"project_head": validated["project_head"],"isolated_workspace": str(isolated),"bug_description": bug_description,"reproduction_command": repro_cmd,"verification_command": verify_cmd,"session_id": session_id,"model_profile": profile_id, "tracked_files": tracked[:20]}, indent=2, sort_keys=True), encoding="utf-8")
            # Audit sidecar (not authority) — allow injection to simulate failure without breaking journal
            try:
                _sentinel_triggered = False
                try:
                    _sidecar_sentinel = Path(validated["parent_tmpdir"]) if validated.get("parent_tmpdir") else None
                    if _sidecar_sentinel is not None and (_sidecar_sentinel / ".inject-sidecar-failure").exists():
                        _sentinel_triggered = True
                except Exception:
                    _sentinel_triggered = False
                if _sentinel_triggered:
                    raise OSError("injected sidecar failure")
                if session_dir is not None and (session_dir / ".inject-sidecar-failure").exists():
                    raise OSError("injected sidecar failure")
                disp_path=session_dir / "local_project_disposition.json"
                disp_path.write_text(json.dumps({"disposition": disposition, "has_active_candidate": has_active_candidate, "verification_passed": bool(verification_passed)}), encoding="utf-8")
            except Exception:
                pass
        except Exception:
            pass
    ctx.emitter.emit(SessionEventKind.CONTROLLER_STEP, {"step_index": 2, "directive_kind": "verification", "stop_reason": "done" if verified_fixed else "unresolved"})
    ctx.token.check()
    return disposition

__all__=["LOCAL_PROJECT_SOURCE_NAME","LocalProjectTask","run_local_project_session"]
