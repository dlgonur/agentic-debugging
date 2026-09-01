"""Bounded native Windows Credential Manager smoke for durable provider credentials.

Exercises the real OS secure store with a generated fake credential only: no
real provider is contacted, the fake secret is never printed or rendered, and
cleanup always runs in a finally path.  Deselected automatically on non-Windows
platforms where the native store path does not exist.
"""

from __future__ import annotations

import secrets
import sys

import pytest

pytestmark = [
    pytest.mark.native_secure_store,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager smoke"),
]


def test_windows_credential_manager_round_trip_and_saved_source_after_restart():
    """Save, restart-simulate, read back exactly, then delete one fake credential."""
    from agentic_debugger.application import provider_connections as pc

    pid = f"wincred_smoke_{secrets.token_hex(6)}"
    fake_secret = f"smoke-{secrets.token_hex(24)}"  # generated fake; never printed

    try:
        # Production path: Add Provider with an API key persists through CredWriteW
        cfg = pc.add_provider_config(
            name="Wincred Smoke",
            base_url="https://wincred-smoke.invalid/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id=pid,
            api_key=fake_secret,
        )
        assert cfg.provider_id == pid

        # Simulated restart: drop all process/session credential state
        pc.clear_all_session_keys()
        assert pc.has_session_key(pid) is False

        # Exact round trip through the OS store (compared in memory, never printed)
        loaded = pc.load_secure_credential(pid)
        assert loaded == fake_secret
        assert pc.has_secure_credential(pid) is True

        # Provider application contract reports durable "saved", not session-only
        assert pc.credential_source_for(pid) == pc.CREDENTIAL_SOURCE_SAVED
        assert pc.resolve_runtime_credential(pid) == fake_secret

        # Delete the fake credential and verify deletion
        assert pc.delete_secure_credential(pid) is True
        assert pc.load_secure_credential(pid) is None
        assert pc.has_secure_credential(pid) is False
    finally:
        pc.delete_secure_credential(pid)
        pc.clear_all_session_keys()
