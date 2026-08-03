"""Task-bound QuixBugs facts provider for the OpenCode Go live-wire path.

This is the small operator facts-provider module selected through the
adapter's explicit ``--facts-provider module:callable`` flag.  It reuses the
already accepted read-only QuixBugs WSL/Bubblewrap environment preparation
(``_verify_environment_ready`` from
``scripts/quixbugs_gcd_pdb_reachability_case.py``) and the accepted
``create_verified_context``/``DependencyPreparation`` wiring; it does not
duplicate the WSL execution architecture and never installs, clones,
resets, cleans, or downloads anything.

The provider is task-bound: ``provide(manifest_path)`` loads the exact
selected task manifest and builds ``QuixBugsPreflightFacts`` whose
``DependencyPreparation`` is bound to that manifest's task ID, fingerprint,
authority revision, algorithm, and pinned recipe.  A missing pinned source
or missing pinned Python environment fails closed with
:class:`ReadinessError` (the accepted readiness gate) rather than
acquiring either.

``describe_environment()`` returns the existing repository root and
sources parent needed to materialize the ``quixbugs-environment.json``
artifact the ``live-wire`` CLI consumes.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.bugsinpy.wsl import (  # noqa: E402
    create_verified_context,
    wsl_unc_path,
)
from agentic_debugger.quixbugs.adapter import (  # noqa: E402
    QuixBugsAdapter,
    QuixBugsPreflightFacts,
)
from agentic_debugger.runtime.execution import DependencyPreparation  # noqa: E402
from scripts.quixbugs_gcd_pdb_reachability_case import (  # noqa: E402
    EXTERNAL_ROOT_POSIX,
    PYTHON_VERSION,
    _verify_environment_ready,
)

DISTRO = "Ubuntu-22.04"


def describe_environment() -> dict[str, str]:
    """The existing repository root and sources parent needed to materialize
    ``quixbugs-environment.json``.

    ``sources_parent`` is the WSL UNC host path of the already-acquired
    pinned QuixBugs source; ``repository_root`` is this repository.  Nothing
    here verifies or acquires anything -- the operator artifact is
    materialized from these values and the case runner re-verifies the pin.
    """
    return {
        "repository_root": str(REPO_ROOT),
        "sources_parent": wsl_unc_path(f"{EXTERNAL_ROOT_POSIX}/sources", DISTRO),
    }


def provide(manifest_path: str) -> QuixBugsPreflightFacts:
    """Task-bound verified facts for the exact selected manifest.

    Reuses the accepted read-only readiness gate (pinned source present,
    pinned venv/pytest present, Bubblewrap self-tests and resource-isolation
    gate open).  The returned facts are bound to the selected manifest's task
    ID, fingerprint, authority revision, algorithm, and pinned pytest recipe,
    so the QuixBugs dependency gate can never pass them for another task.
    """
    if not isinstance(manifest_path, str) or not manifest_path:
        raise ValueError("manifest_path is required")
    adapter = QuixBugsAdapter.from_manifest(manifest_path)

    runner, root_host, venv_posix, env_fingerprint = _verify_environment_ready()

    recipe = f"pytest=={adapter.manifest.environment['pinned_packages']['pytest']}"
    dependency = DependencyPreparation(
        pilot_task_id=adapter.manifest.task_id,
        manifest_fingerprint=adapter.manifest.fingerprint,
        authority_revision=adapter.manifest.authority_revision,
        project="quixbugs",
        bug_id=adapter.manifest.algorithm,
        buggy_revision=adapter.manifest.authority_revision,
        recipe_path=recipe,
        recipe_sha256=hashlib.sha256(recipe.encode("utf-8")).hexdigest(),
        installed_fingerprint=env_fingerprint,
    )
    context = create_verified_context(
        root_host=root_host,
        python_root_posix=venv_posix,
        python_executable_posix=f"{venv_posix}/bin/python",
        python_version=PYTHON_VERSION,
        project_cwd=".",
        pythonpath=(),
        reviewed_environment={},
        dependencies=dependency,
        runner=runner,
    )
    return QuixBugsPreflightFacts(
        platform="linux",
        pinned_source_verified=True,
        license_reviewed=True,
        dependency_install_boundary_ready=True,
        test_command_available=True,
        workspace_cleanup_ready=True,
        target_annotation_reviewed=True,
        external_parent=wsl_unc_path(f"{EXTERNAL_ROOT_POSIX}/runs", DISTRO),
        execution_context=context,
    )


__all__ = ["describe_environment", "provide"]
