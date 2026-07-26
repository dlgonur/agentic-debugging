"""In-process offline enforcement for the Task 9 demonstration.

The demonstration claims to run without a model provider and without network
access.  This module turns that claim into a *measurement* instead of a
promise: while a case runs, outbound socket use and imports of known
model-provider SDKs are intercepted, counted and refused.

Scope, stated plainly:

* The guard is in-process only.  Child processes (the pytest subprocesses and
  the PDB worker) are separate interpreters and are not covered by it.  Those
  children run only curated fixture test suites and the bundled worker.
* The provider guard recognises a fixed list of remote-provider SDK module
  names.  It cannot prove that no provider was reached by some other means; it
  proves that none of the recognised SDKs was imported.

Both limits are reported alongside the counts rather than papered over.
"""

from __future__ import annotations

import socket
import sys
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Iterator, Optional, Sequence

#: Remote model-provider SDK module roots the guard refuses and counts.
PROVIDER_MODULE_ROOTS: tuple[str, ...] = (
    "anthropic",
    "cohere",
    "google.generativeai",
    "litellm",
    "mistralai",
    "ollama",
    "openai",
    "replicate",
    "together",
    "vertexai",
)


class OfflineViolationError(RuntimeError):
    """Raised when the demonstration attempts network or provider access."""


@dataclass
class OfflineLedger:
    """Counts of refused offline violations observed during a run."""

    network_attempts: int = 0
    provider_attempts: int = 0
    network_targets: list[str] = field(default_factory=list)
    provider_modules: list[str] = field(default_factory=list)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "network_attempts": self.network_attempts,
            "provider_attempts": self.provider_attempts,
            "network_targets": list(self.network_targets),
            "provider_modules": list(self.provider_modules),
        }


class _ProviderImportBlocker:
    """A ``sys.meta_path`` finder that refuses recognised provider SDKs."""

    def __init__(self, ledger: OfflineLedger, roots: Sequence[str]) -> None:
        self._ledger = ledger
        self._roots = tuple(roots)

    def find_module(self, fullname: str, path: Optional[Sequence[str]] = None) -> None:
        # Legacy hook kept for interpreters that still consult it.
        self.find_spec(fullname, path)
        return None

    def find_spec(
        self,
        fullname: str,
        path: Optional[Sequence[str]] = None,
        target: Optional[ModuleType] = None,
    ) -> None:
        for root in self._roots:
            if fullname == root or fullname.startswith(root + "."):
                self._ledger.provider_attempts += 1
                if fullname not in self._ledger.provider_modules:
                    self._ledger.provider_modules.append(fullname)
                raise OfflineViolationError(
                    f"model-provider import refused by the offline guard: {fullname}"
                )
        return None


class OfflineGuard:
    """Context manager that enforces and measures offline execution."""

    def __init__(self, *, provider_roots: Sequence[str] = PROVIDER_MODULE_ROOTS) -> None:
        self.ledger = OfflineLedger()
        self._provider_roots = tuple(provider_roots)
        self._finder: Optional[_ProviderImportBlocker] = None
        self._saved: dict[str, Any] = {}

    def __enter__(self) -> "OfflineGuard":
        ledger = self.ledger

        def _refuse(target: Any) -> None:
            ledger.network_attempts += 1
            description = _describe(target)
            if description not in ledger.network_targets:
                ledger.network_targets.append(description)
            raise OfflineViolationError(
                f"network access refused by the offline guard: {description}"
            )

        def blocked_connect(self_socket: Any, address: Any) -> None:
            _refuse(address)

        def blocked_connect_ex(self_socket: Any, address: Any) -> None:
            _refuse(address)

        def blocked_create_connection(address: Any, *args: Any, **kwargs: Any) -> None:
            _refuse(address)

        def blocked_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> None:
            _refuse((host, port))

        self._saved = {
            "connect": socket.socket.connect,
            "connect_ex": socket.socket.connect_ex,
            "create_connection": socket.create_connection,
            "getaddrinfo": socket.getaddrinfo,
        }
        socket.socket.connect = blocked_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = blocked_connect_ex  # type: ignore[method-assign]
        socket.create_connection = blocked_create_connection  # type: ignore[assignment]
        socket.getaddrinfo = blocked_getaddrinfo  # type: ignore[assignment]

        self._finder = _ProviderImportBlocker(ledger, self._provider_roots)
        sys.meta_path.insert(0, self._finder)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if self._finder is not None:
            try:
                sys.meta_path.remove(self._finder)
            except ValueError:
                pass
            self._finder = None
        if self._saved:
            socket.socket.connect = self._saved["connect"]  # type: ignore[method-assign]
            socket.socket.connect_ex = self._saved["connect_ex"]  # type: ignore[method-assign]
            socket.create_connection = self._saved["create_connection"]  # type: ignore[assignment]
            socket.getaddrinfo = self._saved["getaddrinfo"]  # type: ignore[assignment]
            self._saved = {}
        return False


def _describe(target: Any) -> str:
    if isinstance(target, tuple) and target:
        return ":".join(str(item) for item in target[:2])
    return str(target)


def guard_scope_note() -> str:
    """Return the honest scope statement for the offline guard."""

    return (
        "In-process only. Child processes (pytest subprocesses and the PDB "
        "worker) run in separate interpreters and are not covered. The "
        "provider guard recognises a fixed list of remote-provider SDK module "
        "roots and cannot prove that no provider was reached by other means."
    )


__all__ = [
    "PROVIDER_MODULE_ROOTS",
    "OfflineGuard",
    "OfflineLedger",
    "OfflineViolationError",
    "guard_scope_note",
]
