"""Tests for the in-process offline guard used by the Task 9 demonstration."""

from __future__ import annotations

import socket
import sys

import pytest

from agentic_debugger.demo.isolation import (
    PROVIDER_MODULE_ROOTS,
    OfflineGuard,
    OfflineLedger,
    OfflineViolationError,
    guard_scope_note,
)


class TestNetworkEnforcement:
    def test_connect_is_refused_and_counted(self) -> None:
        guard = OfflineGuard()
        with guard:
            with pytest.raises(OfflineViolationError):
                socket.socket().connect(("example.invalid", 80))
        assert guard.ledger.network_attempts == 1
        assert guard.ledger.network_targets == ["example.invalid:80"]

    def test_every_outbound_entry_point_is_covered(self) -> None:
        guard = OfflineGuard()
        with guard:
            for attempt in (
                lambda: socket.socket().connect(("a.invalid", 1)),
                lambda: socket.socket().connect_ex(("b.invalid", 2)),
                lambda: socket.create_connection(("c.invalid", 3)),
                lambda: socket.getaddrinfo("d.invalid", 4),
            ):
                with pytest.raises(OfflineViolationError):
                    attempt()
        assert guard.ledger.network_attempts == 4
        assert len(guard.ledger.network_targets) == 4

    def test_a_clean_scope_records_zero(self) -> None:
        guard = OfflineGuard()
        with guard:
            pass
        assert guard.ledger.network_attempts == 0
        assert guard.ledger.provider_attempts == 0


class TestProviderEnforcement:
    def test_a_recognised_provider_import_is_refused_and_counted(self) -> None:
        guard = OfflineGuard()
        with guard:
            with pytest.raises(OfflineViolationError):
                __import__("anthropic")
        assert guard.ledger.provider_attempts == 1
        assert guard.ledger.provider_modules == ["anthropic"]

    def test_a_provider_submodule_is_also_refused(self) -> None:
        guard = OfflineGuard()
        with guard:
            with pytest.raises(OfflineViolationError):
                __import__("openai.types")
        assert guard.ledger.provider_attempts == 1

    def test_unrelated_imports_are_untouched(self) -> None:
        guard = OfflineGuard()
        with guard:
            import difflib  # noqa: F401 - the import itself is the assertion

            assert difflib is not None
        assert guard.ledger.provider_attempts == 0

    def test_the_recognised_root_list_is_explicit(self) -> None:
        assert "anthropic" in PROVIDER_MODULE_ROOTS
        assert "openai" in PROVIDER_MODULE_ROOTS
        assert PROVIDER_MODULE_ROOTS == tuple(sorted(PROVIDER_MODULE_ROOTS))


class TestRestoration:
    def test_socket_and_meta_path_are_restored_on_exit(self) -> None:
        original = (
            socket.socket.connect,
            socket.socket.connect_ex,
            socket.create_connection,
            socket.getaddrinfo,
        )
        meta_path_length = len(sys.meta_path)
        with OfflineGuard():
            assert socket.socket.connect is not original[0]
            assert len(sys.meta_path) == meta_path_length + 1
        assert socket.socket.connect is original[0]
        assert socket.socket.connect_ex is original[1]
        assert socket.create_connection is original[2]
        assert socket.getaddrinfo is original[3]
        assert len(sys.meta_path) == meta_path_length

    def test_restoration_also_happens_when_the_body_raises(self) -> None:
        original = socket.getaddrinfo
        meta_path_length = len(sys.meta_path)
        with pytest.raises(RuntimeError):
            with OfflineGuard():
                raise RuntimeError("body failed")
        assert socket.getaddrinfo is original
        assert len(sys.meta_path) == meta_path_length


class TestLedgerReporting:
    def test_mapping_is_json_shaped_and_detached(self) -> None:
        ledger = OfflineLedger(network_attempts=2, provider_attempts=1)
        ledger.network_targets.append("host:1")
        mapping = ledger.to_mapping()
        assert mapping["network_attempts"] == 2
        assert mapping["provider_attempts"] == 1
        mapping["network_targets"].append("mutated")
        assert ledger.network_targets == ["host:1"]

    def test_scope_note_states_the_limits_of_the_measurement(self) -> None:
        note = guard_scope_note()
        assert "In-process only" in note
        assert "Child processes" in note
        assert "cannot prove" in note
