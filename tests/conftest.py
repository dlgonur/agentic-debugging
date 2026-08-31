"""Global test isolation fixtures for agentic-debugger test suite.

Ensures that no automated test or render harness mutates the operator's
real configuration or OS Credential Manager store.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

import pytest

from agentic_debugger.application import provider_connections as pc


@pytest.fixture(autouse=True)
def _global_provider_test_isolation(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Isolate provider configuration path, OS credentials, and session keys for every test."""
    isolated_config_dir = tmp_path / "agentic_debugger_test_config"
    isolated_config_dir.mkdir(parents=True, exist_ok=True)
    isolated_config_file = isolated_config_dir / "provider-configurations.json"

    # Set environment variables for config isolation
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(isolated_config_dir))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(isolated_config_file))

    # In-memory mock for OS secure store so tests never touch real Windows Credential Manager
    # unless a test is explicitly marked for native secure store execution.
    if not request.node.get_closest_marker("native_secure_store"):
        _secure_store: Dict[str, str] = {}
        monkeypatch.setattr(
            pc,
            "save_secure_credential",
            lambda kind, val: _secure_store.__setitem__(kind, val) or True,
        )
        monkeypatch.setattr(
            pc,
            "load_secure_credential",
            lambda kind: _secure_store.get(kind),
        )
        monkeypatch.setattr(
            pc,
            "has_secure_credential",
            lambda kind: kind in _secure_store,
        )
        monkeypatch.setattr(
            pc,
            "delete_secure_credential",
            lambda kind: _secure_store.pop(kind, None) is not None,
        )

    pc.clear_all_session_keys()
    yield
    pc.clear_all_session_keys()
