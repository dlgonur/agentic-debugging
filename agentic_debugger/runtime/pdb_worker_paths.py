"""Workspace/script path helpers for the PDB worker.

Pure path canonicalization used by both target execution and paused-target
inspection. No worker state, no I/O beyond package-marker probes.
"""
from __future__ import annotations

import os
import posixpath
from typing import Optional

def _has_raw_dotdot(script: str) -> bool:
    parts = script.replace('\\', '/').split('/')
    return '..' in parts


def _canonic(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))



def _package_context_for_script(
    script_normalized: str,
    workspace_root: str,
) -> Optional[str]:
    """Return the import package for a workspace script, when unambiguous.

    PDB still executes the target as ``__main__`` so script entry points run,
    but package modules need ``__package__`` populated for relative imports.
    Only a conventional chain of identifier-named directories containing
    ``__init__.py`` is accepted; ordinary top-level scripts retain the legacy
    ``None`` package context.
    """
    parent = posixpath.dirname(script_normalized.replace('\\', '/'))
    if not parent:
        return None
    parts = parent.split('/')
    if any(not part.isidentifier() for part in parts):
        return None

    current = workspace_root
    for part in parts:
        current = os.path.join(current, part)
        if not os.path.isfile(os.path.join(current, '__init__.py')):
            return None
    return '.'.join(parts)
