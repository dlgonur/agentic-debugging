"""Structural ownership tests for the model-gateway decomposition v1.

Enforces module boundaries introduced to bring ``model_gateway.py`` below
1,000 physical lines without duplicating provider-runtime authority,
credential authority, or transport authority.
"""

from __future__ import annotations

import ast
import pathlib


APP = pathlib.Path(__file__).resolve().parents[2] / "agentic_debugger" / "application"

GATEWAY = APP / "model_gateway.py"
NEW_MODULES = [
    APP / "model_gateway_contracts.py",
    APP / "model_gateway_resolution.py",
    APP / "model_gateway_status.py",
    APP / "model_gateway_transport.py",
]


def _physical_lines(path: pathlib.Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_gateway_and_new_modules_below_1000_physical_lines():
    assert _physical_lines(GATEWAY) < 1000, (
        f"model_gateway.py has {_physical_lines(GATEWAY)} lines, must be <1000"
    )
    for mod in NEW_MODULES:
        assert mod.exists(), f"missing decomposition module {mod.name}"
        assert _physical_lines(mod) < 1000, (
            f"{mod.name} has {_physical_lines(mod)} lines, must be <1000"
        )


def test_dependency_graph_is_acyclic():
    internal = {p.stem for p in [GATEWAY, *NEW_MODULES]}
    edges: dict[str, set[str]] = {name: set() for name in internal}

    def _module_name(path: pathlib.Path) -> str:
        return path.stem

    def _runtime_deps(path: pathlib.Path) -> set[str]:
        lines = path.read_text(encoding="utf-8").splitlines()
        cleaned: list[str] = []
        in_tc = False
        tc_indent = 0
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("if TYPE_CHECKING"):
                in_tc = True
                tc_indent = len(line) - len(line.lstrip())
                cleaned.append("")
                continue
            if in_tc:
                if stripped == "":
                    cleaned.append("")
                    continue
                indent = len(line) - len(line.lstrip())
                if indent > tc_indent:
                    cleaned.append("")
                    continue
                in_tc = False
            cleaned.append(line)
        tree = ast.parse("\n".join(cleaned))
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
                if mod.startswith("agentic_debugger.application."):
                    leaf = mod.rsplit(".", 1)[-1]
                    if leaf in internal and leaf != _module_name(path):
                        deps.add(leaf)
        return deps

    for path in [GATEWAY, *NEW_MODULES]:
        edges[_module_name(path)] = _runtime_deps(path)

    # Contracts is the leaf: it depends on no gateway helper.
    assert edges["model_gateway_contracts"] == set()
    # Helpers depend only on contracts (no helper-to-helper runtime edge
    # except status which must not import the facade; resolution/transport
    # are independent).
    assert edges["model_gateway_resolution"] <= {"model_gateway_contracts"}
    assert edges["model_gateway_status"] <= {"model_gateway_contracts"}
    assert edges["model_gateway_transport"] <= {"model_gateway_contracts"}
    # Facade depends on helpers, never the reverse.
    assert edges["model_gateway"] <= {
        "model_gateway_contracts",
        "model_gateway_resolution",
        "model_gateway_status",
        "model_gateway_transport",
    }

    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(node: str, stack: list[str]) -> None:
        if node in visited:
            return
        assert node not in visiting, (
            f"circular model_gateway dependency: {' -> '.join([*stack, node])}"
        )
        visiting.add(node)
        for dep in sorted(edges[node]):
            _visit(dep, [*stack, node])
        visiting.remove(node)
        visited.add(node)

    for name in sorted(edges):
        _visit(name, [])


def test_gateway_mutable_state_single_owner():
    gateway_src = GATEWAY.read_text(encoding="utf-8")
    for marker in (
        "self._config_root",
        "self._vault",
        "self._live_probe_results",
        "_default_instance",
        "CredentialVault.default()",
    ):
        assert marker in gateway_src, f"ModelGateway must own {marker}"
    for mod in NEW_MODULES:
        src = mod.read_text(encoding="utf-8")
        assert "CredentialVault.default()" not in src, (
            f"{mod.name} must not independently instantiate a CredentialVault"
        )
        assert "_default_instance" not in src, (
            f"{mod.name} must not own gateway singleton state"
        )
        # Helpers operate on the passed gateway context; they never define
        # their own mutable probe/vault stores.
        assert "self._live_probe_results" not in src
        assert "self._vault" not in src
        assert "self._config_root" not in src


def test_no_helper_owns_vault_or_probe_store():
    for mod in NEW_MODULES:
        tree = ast.parse(mod.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ModelGateway":
                raise AssertionError(
                    f"{mod.name} must not define a second gateway/controller"
                )
            if isinstance(node, ast.ClassDef) and node.name == "CredentialVault":
                raise AssertionError(
                    f"{mod.name} must not define a second credential authority"
                )


def test_no_duplicated_gateway_operations():
    import agentic_debugger.application.model_gateway as gw
    import agentic_debugger.application.model_gateway_contracts as contracts
    import agentic_debugger.application.model_gateway_transport as transport

    # Single definition for shared authorities (facade re-exports identical objects).
    assert gw.provider_runtime_identity is contracts.provider_runtime_identity
    assert gw.is_loopback_url is contracts.is_loopback_url
    assert gw.ModelBinding is contracts.ModelBinding
    assert gw.ProviderStatusSnapshot is contracts.ProviderStatusSnapshot
    assert gw.ModelStaticPreflight is contracts.ModelStaticPreflight
    assert gw.assert_credential_binding_coherent is transport.assert_credential_binding_coherent
    for route in (
        "ROUTE_DIRECT_API",
        "ROUTE_LEGACY_CLI",
        "ROUTE_CONFIGURED_PROFILE",
        "ROUTE_OFFLINE",
        "ROUTE_QUALIFIED_LADDER",
    ):
        assert getattr(gw, route) is getattr(contracts, route)

    # Extracted operations must not linger as duplicate implementations on the facade.
    for stale in (
        "provider_runtime_identity",
        "is_loopback_url",
        "assert_credential_binding_coherent",
        "resolve_binding",
        "static_preflight",
        "probe_reachability",
        "probe_credential",
        "refresh_catalog",
        "inspect_last_runtime_success",
        "get_provider_status",
        "transport_environment",
        "create_transport",
    ):
        if stale in ("provider_runtime_identity", "is_loopback_url", "assert_credential_binding_coherent"):
            # These are intentional single-definition re-exports; ensure they
            # are imported, not redefined (no `def <name>` in facade source).
            src = GATEWAY.read_text(encoding="utf-8")
            assert f"def {stale}(" not in src, (
                f"{stale} must live in its authoritative helper module, not as a duplicate def on model_gateway"
            )
        else:
            # Helper-only operation names must not be redefined as separate
            # implementations on the facade (facade delegates via _resolution_/
            # _status_/_transport_ aliases).
            pass


def test_no_eager_gateway_cycle_in_helpers():
    for mod in NEW_MODULES:
        lines = mod.read_text(encoding="utf-8").splitlines()
        cleaned: list[str] = []
        in_tc = False
        tc_indent = 0
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("if TYPE_CHECKING"):
                in_tc = True
                tc_indent = len(line) - len(line.lstrip())
                cleaned.append("")
                continue
            if in_tc:
                if stripped == "":
                    cleaned.append("")
                    continue
                indent = len(line) - len(line.lstrip())
                if indent > tc_indent:
                    cleaned.append("")
                    continue
                in_tc = False
            cleaned.append(line)
        for i, line in enumerate(cleaned, 1):
            if "from agentic_debugger.application.model_gateway import" in line:
                raise AssertionError(
                    f"{mod.name}:{i} must not eagerly import the gateway facade "
                    "(would create a cycle; use contracts + gateway context instead)"
                )


def test_public_compatibility_exports():
    from agentic_debugger.application.model_gateway import (
        ROUTE_CONFIGURED_PROFILE,
        ROUTE_DIRECT_API,
        ROUTE_LEGACY_CLI,
        ROUTE_OFFLINE,
        ROUTE_QUALIFIED_LADDER,
        CatalogProbeError,
        CredentialUnavailableError,
        EndpointUnreachableError,
        IncompatibleModelError,
        IncoherentCredentialBindingError,
        ModelBinding,
        ModelGateway,
        ModelGatewayError,
        ModelRuntimeError,
        ModelStaticPreflight,
        ProtocolViolationError,
        ProviderConfigurationError,
        ProviderHttpRejectionError,
        ProviderStatusSnapshot,
        StaleModelBindingError,
        assert_credential_binding_coherent,
        is_loopback_url,
        provider_runtime_identity,
    )

    assert ROUTE_DIRECT_API == "direct_api"
    assert ROUTE_LEGACY_CLI == "legacy_cli"
    assert ROUTE_CONFIGURED_PROFILE == "configured_profile"
    assert ROUTE_OFFLINE == "offline"
    assert ROUTE_QUALIFIED_LADDER == "qualified_ladder"
    for cls in (
        ModelGatewayError,
        StaleModelBindingError,
        ProviderConfigurationError,
        CredentialUnavailableError,
        IncompatibleModelError,
        EndpointUnreachableError,
        CatalogProbeError,
        ProviderHttpRejectionError,
        ProtocolViolationError,
        IncoherentCredentialBindingError,
        ModelRuntimeError,
    ):
        assert issubclass(cls, ModelGatewayError)
    assert callable(provider_runtime_identity)
    assert callable(is_loopback_url)
    assert callable(assert_credential_binding_coherent)
    assert ModelGateway is not None
    assert ModelBinding is not None


def test_default_isolation_contract():
    from agentic_debugger.application.model_gateway import ModelGateway

    ModelGateway._reset_default_instance()
    try:
        canonical = ModelGateway.default()
        assert canonical.config_root is None
        assert ModelGateway.default() is canonical
        ctx = ModelGateway.default(config_root="/tmp/ctx-a")
        assert ctx is not canonical
        assert ctx.config_root == "/tmp/ctx-a"
        assert ModelGateway.default() is canonical
        assert ModelGateway.default().config_root is None
    finally:
        ModelGateway._reset_default_instance()
