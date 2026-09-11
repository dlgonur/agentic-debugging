"""BugsInPy preflight gates and manifest/command validation.

This module owns the fail-closed preflight gate family (platform,
source provenance, license, Python version, dependencies, test-command
normalization, containment, PDB compatibility, cleanup, target symbols)
and the manifest/entry validation + reviewed
cwd/pythonpath/environment derivation beneath the adapter.
"""

from __future__ import annotations

import platform as platform_module
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from agentic_debugger.bugsinpy.contracts import (
    _SHA1,
    _SHA256,
    _SHELL_TOKENS,
    _UNKNOWN,
    GateName,
    GateResult,
    GateStatus,
    BugsInPyManifest,
    ManifestValidationError,
    NormalizedCommands,
    PreflightFacts,
    TaskMappingError,
    _command_with_nodes,
    _is_unknown,
    _nonempty_equal,
    _normalize_pytest_command,
    _reviewed_cwd,
    _reviewed_environment,
    _reviewed_pythonpath,
    _safe_relative,
    _validate_entry,
    _validate_manifest,
)
from agentic_debugger.bugsinpy.workspace_source import (
    _is_filesystem_root,
    _is_within,
)
from agentic_debugger.runtime.execution import VerifiedExecutionContext

def _pdb_argv_compatible(argv: Sequence[str], f2p: str, context: VerifiedExecutionContext) -> bool:
    try:
        bound = context.bind_argv(argv)
    except Exception:
        return False
    return bound[:3] == [context.environment.python_executable, "-m", "pytest"] and f2p in bound[3:]


def _platform_gate(facts: PreflightFacts) -> GateResult:
    actual = (facts.platform or platform_module.system()).lower()
    return _boolean_gate(GateName.SUPPORTED_PLATFORM, actual == "linux", f"reference platform is Linux; observed {actual}")


def _source_gate(manifest: BugsInPyManifest, entry: Mapping[str, Any], facts: PreflightFacts) -> GateResult:
    pinned = _SHA1.fullmatch(manifest.authority_revision) and _SHA1.fullmatch(entry["bugsinpy"]["buggy_revision"])
    if not pinned:
        return GateResult(GateName.PINNED_UPSTREAM_SOURCE, GateStatus.BLOCKED, "manifest revision is not a full pin")
    return _boolean_gate(GateName.PINNED_UPSTREAM_SOURCE, facts.pinned_source_verified, "source revision and identity must be verified before checkout")


def _license_gate(entry: Mapping[str, Any], facts: PreflightFacts) -> GateResult:
    license_data = entry["licensing"]
    cleared = facts.license_reviewed and license_data.get("status") not in {"gated", "unknown", "unverified"} and not _is_unknown(license_data.get("underlying_project_license"))
    return _boolean_gate(GateName.PROJECT_LICENSE_REVIEW, cleared, "project and BugsInPy license/notice review is not cleared")


def _python_gate(entry: Mapping[str, Any], facts: PreflightFacts) -> GateResult:
    expected = entry["environment"]["python_version"]
    context = facts.execution_context
    actual = context.environment.python_version if context else "unknown"
    reviewed_cwd = _reviewed_cwd(entry)
    reviewed_pythonpath = _reviewed_pythonpath(entry)
    reviewed_environment = _reviewed_environment(entry)
    ok = (
        context is not None
        and actual == expected
        and Path(context.environment.python_executable).is_file()
        and reviewed_cwd is not None
        and context.environment.project_cwd == reviewed_cwd
        and reviewed_pythonpath is not None
        and context.environment.pythonpath == reviewed_pythonpath
        and reviewed_environment is not None
        and dict(context.environment.environment) == reviewed_environment
    )
    return _boolean_gate(GateName.PYTHON_RUNTIME_AVAILABLE, ok, f"task requires Python {expected}; verified runtime is {actual}")


def _dependency_gate(manifest: BugsInPyManifest, pilot_task_id: str, entry: Mapping[str, Any], facts: PreflightFacts) -> GateResult:
    context = facts.execution_context
    prepared = context.environment.dependencies if context else None
    environment = entry["environment"]
    recipe = environment.get("official_dependency_recipe")
    recipe_sha256 = environment.get("official_dependency_recipe_sha256")
    expected = {
        "pilot_task_id": pilot_task_id,
        "manifest_fingerprint": manifest.fingerprint,
        "authority_revision": manifest.authority_revision,
        "project": str(entry["bugsinpy"]["project"]),
        "bug_id": str(entry["bugsinpy"]["bug_id"]),
        "buggy_revision": entry["bugsinpy"]["buggy_revision"],
        "recipe_path": recipe,
        "recipe_sha256": recipe_sha256,
    }
    ok = (
        prepared is not None
        and all(_nonempty_equal(getattr(prepared, name, None), value) for name, value in expected.items())
        and _SHA256.fullmatch(str(recipe_sha256 or "")) is not None
        and _SHA256.fullmatch(prepared.installed_fingerprint) is not None
        and prepared.status == "prepared"
        and prepared.network_disabled is True
    )
    return _boolean_gate(GateName.DEPENDENCY_INSTALL_BOUNDARY, ok, "dependency preparation is not bound to the selected task and manifest")


def _test_command_gate(commands: NormalizedCommands, facts: PreflightFacts) -> GateResult:
    ok = bool(commands.baseline_argv and commands.fail_to_pass and commands.pass_to_pass and facts.test_command_available and facts.execution_context is not None)
    return _boolean_gate(GateName.TEST_COMMAND_AVAILABILITY, ok, "normalized pytest command is not verified available in the task environment")


def _containment_gate(facts: PreflightFacts, repository_root: Optional[str]) -> GateResult:
    context = facts.execution_context
    parent = Path(facts.external_parent).resolve() if facts.external_parent else None
    root = Path(context.containment.root).resolve() if context else None
    repository = Path(repository_root).resolve() if repository_root else None
    ok = (
        context is not None
        and parent is not None
        and root is not None
        and not _is_filesystem_root(root)
        and _is_within(parent, root)
        and repository is not None
        and not _is_within(repository, root)
        and context.runner is not None
        and context.runner.runner_id == context.containment.runner_id
        and dict(getattr(context.runner, "boundary_guarantee", {})) == context.containment.to_mapping()
    )
    return _boolean_gate(GateName.CONTAINMENT_READY, ok, "external parent, repository, and containment runner relationships are not verified")


def _pdb_gate(entry: Mapping[str, Any], commands: NormalizedCommands, facts: PreflightFacts) -> GateResult:
    context = facts.execution_context
    plan = facts.pdb_launch_plan
    review = entry["debugger_relevance"]
    candidates = {str(item).replace("\\", "/") for item in review.get("candidate_changed_files", []) if isinstance(item, str)}
    reviewed_driver = review.get("pdb_driver")
    reviewed_breakpoints = review.get("reviewed_breakpoints")
    f2p = commands.fail_to_pass[0] if commands.fail_to_pass else None
    plan_nodes = [item for item in plan.argv if isinstance(item, str) and "::" in item] if plan else []
    ok = (
        context is not None
        and plan is not None
        and isinstance(reviewed_driver, str)
        and _safe_relative(reviewed_driver) == plan.driver
        and isinstance(reviewed_breakpoints, list)
        and tuple(reviewed_breakpoints) == plan.breakpoints
        and plan.target in candidates
        and plan.python_executable == context.environment.python_executable
        and plan.cwd == context.environment.project_cwd
        and dict(plan.environment) == dict(context.environment.environment)
        and f2p is not None
        and plan_nodes == [f2p]
        and _pdb_argv_compatible(plan.argv, f2p, context)
    )
    return _boolean_gate(GateName.PDB_PLANNING, ok, "PDB launch plan is not bound to the selected task and reviewed F2P plan")


def _cleanup_gate(facts: PreflightFacts) -> GateResult:
    parent = Path(facts.external_parent).resolve() if facts.external_parent else None
    ok = facts.workspace_cleanup_ready and parent is not None and parent.is_dir()
    return _boolean_gate(GateName.WORKSPACE_CLEANUP_READY, ok, "owned external workspace parent and cleanup proof are not ready")


def _target_gate(entry: Mapping[str, Any], facts: PreflightFacts, target_symbols: Optional[Sequence[str]]) -> GateResult:
    symbols = target_symbols or ()
    known = isinstance(entry["debugger_relevance"].get("target_symbols"), str) and not entry["debugger_relevance"]["target_symbols"].lower().startswith("unknown")
    ok = facts.target_annotation_reviewed and (known or bool(symbols))
    return _boolean_gate(GateName.TARGET_ANNOTATION_REVIEW, ok, "target symbols/breakpoint plan require review before execution")


def _reviewed_symbols(entry: Mapping[str, Any], target_symbols: Optional[Sequence[str]]) -> tuple[str, ...]:
    supplied = tuple(target_symbols or ())
    if supplied and all(isinstance(item, str) and item for item in supplied):
        return supplied
    raw = entry["debugger_relevance"].get("target_symbols", "")
    if isinstance(raw, str) and not raw.lower().startswith("unknown"):
        return (raw,)
    raise TaskMappingError("reviewed target_symbols are required to map this entry into DebugTask")


def _boolean_gate(name: GateName, ok: bool, reason: str) -> GateResult:
    return GateResult(name, GateStatus.PASS if ok else GateStatus.BLOCKED, reason if not ok else "explicitly cleared")
