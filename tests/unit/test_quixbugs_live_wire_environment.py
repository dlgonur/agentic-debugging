"""Tests for the task-bound operator facts-provider module used by the
OpenCode Go live-wire path (``scripts/quixbugs_live_wire_environment.py``).

No WSL command, provider call, or real OpenCode command is executed: the
accepted readiness gate and context constructor are monkeypatched so the
test proves the module's own task-binding wiring.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import quixbugs_live_wire_environment as env_module

from agentic_debugger.quixbugs.adapter import QuixBugsAdapter, QuixBugsPreflightFacts

FIND_IN_SORTED_MANIFEST = REPO_ROOT / "research" / "quixbugs" / "FIND_IN_SORTED_SMOKE_MANIFEST_V1.json"
HANOI_MANIFEST = REPO_ROOT / "research" / "quixbugs" / "HANOI_SMOKE_MANIFEST_V1.json"


def _fake_ready():
    return SimpleNamespace(), "\\\\wsl.localhost\\Ubuntu-22.04\\root", "/root/venv", "f" * 64


def test_describe_environment_returns_repository_root_and_sources_parent() -> None:
    description = env_module.describe_environment()
    assert isinstance(description, dict)
    assert description["repository_root"] == str(REPO_ROOT)
    assert isinstance(description["sources_parent"], str)
    assert description["sources_parent"].startswith("\\\\wsl.localhost\\Ubuntu-22.04\\")
    assert "sources" in description["sources_parent"]


def test_provide_creates_task_bound_facts_for_the_selected_manifest(monkeypatch) -> None:
    monkeypatch.setattr(env_module, "_verify_environment_ready", lambda: _fake_ready())
    monkeypatch.setattr(
        env_module, "create_verified_context",
        lambda **kwargs: SimpleNamespace(environment=SimpleNamespace(dependencies=kwargs["dependencies"])),
    )
    facts = env_module.provide(str(FIND_IN_SORTED_MANIFEST))
    assert type(facts) is QuixBugsPreflightFacts
    adapter_obj = QuixBugsAdapter.from_manifest(FIND_IN_SORTED_MANIFEST)
    deps = facts.execution_context.environment.dependencies
    assert deps.pilot_task_id == "quixbugs-find-in-sorted-smoke-v1"
    assert deps.manifest_fingerprint == adapter_obj.manifest.fingerprint
    assert deps.authority_revision == adapter_obj.manifest.authority_revision
    assert deps.project == "quixbugs"
    assert deps.bug_id == "find_in_sorted"
    assert deps.buggy_revision == adapter_obj.manifest.authority_revision
    assert deps.recipe_path == "pytest==7.4.4"
    # The installed fingerprint comes from the verified readiness gate
    # (``_fake_ready``'s exact env fingerprint), not from the manifest's
    # separate expected fingerprint.
    assert deps.installed_fingerprint == "f" * 64
    assert facts.platform == "linux"
    assert facts.pinned_source_verified is True
    assert facts.dependency_install_boundary_ready is True
    assert facts.test_command_available is True
    assert facts.workspace_cleanup_ready is True
    assert facts.target_annotation_reviewed is True
    assert facts.external_parent.startswith("\\\\wsl.localhost\\Ubuntu-22.04\\")


def test_provide_is_task_bound_per_manifest(monkeypatch) -> None:
    monkeypatch.setattr(env_module, "_verify_environment_ready", lambda: _fake_ready())
    monkeypatch.setattr(
        env_module, "create_verified_context",
        lambda **kwargs: SimpleNamespace(environment=SimpleNamespace(dependencies=kwargs["dependencies"])),
    )
    find_facts = env_module.provide(str(FIND_IN_SORTED_MANIFEST))
    hanoi_facts = env_module.provide(str(HANOI_MANIFEST))
    find_deps = find_facts.execution_context.environment.dependencies
    hanoi_deps = hanoi_facts.execution_context.environment.dependencies
    assert find_deps.pilot_task_id == "quixbugs-find-in-sorted-smoke-v1"
    assert hanoi_deps.pilot_task_id == "quixbugs-hanoi-smoke-v1"
    assert find_deps.manifest_fingerprint != hanoi_deps.manifest_fingerprint
    assert find_deps.bug_id != hanoi_deps.bug_id


def test_provide_fails_closed_when_readiness_gate_blocks(monkeypatch) -> None:
    from scripts.quixbugs_gcd_pdb_reachability_case import ReadinessError

    def _blocked():
        raise ReadinessError("pinned QuixBugs source is not already acquired")

    monkeypatch.setattr(env_module, "_verify_environment_ready", _blocked)
    with pytest.raises(ReadinessError, match="not already acquired"):
        env_module.provide(str(FIND_IN_SORTED_MANIFEST))


def test_provide_requires_an_explicit_manifest_path() -> None:
    with pytest.raises(ValueError, match="manifest_path is required"):
        env_module.provide("")


def test_provide_rejects_an_unresolvable_manifest() -> None:
    with pytest.raises(ValueError):
        env_module.provide(str(REPO_ROOT / "does" / "not" / "exist.json"))
