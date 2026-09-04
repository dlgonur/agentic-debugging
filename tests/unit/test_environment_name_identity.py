"""V2-02/09 platform-aware environment-name identity acceptance tests.

Windows is a first-class operator platform: environment variable names are
case-insensitive there and case-sensitive on POSIX.  One explicit helper
authority (``canonical_env_name`` + ``is_platform_essential_name`` +
``ProjectRuntimeEnvironmentSpec.validate_for_platform``) governs:

- canonical identity (Windows uppercases; POSIX preserves);
- duplicate detection within/across all declaration categories;
- launch-snapshot lookup (Windows resolves across case variants; POSIX
  does not; conflicting Windows snapshot variants fail closed);
- platform-essential collision rejection at the declaration ingress
  (``PATH`` everywhere; ``Path``/``SystemRoot``-case variants on Windows).

Every platform-specific test passes an explicit ``platform`` argument, so
the suite is deterministic on non-Windows CI as well as on Windows.

Secret VALUES are never normalized or inspected anywhere here — only
NAMES participate in identity.
"""

from __future__ import annotations

import os
import sys

import pytest

from agentic_debugger.application.execution_environment import ExecutionEnvironment
from agentic_debugger.application.session_runtime import (
    ProjectEnvDeclaration,
    ProjectExplicitValue,
    ProjectRuntimeEnvironmentSpec,
    ProjectRuntimeError,
    SessionRuntimeError,
    canonical_env_name,
    is_platform_essential_name,
    materialize_project_runtime,
    resolve_env_name_platform,
)

WIN = "win32"
NIX = "posix"


def _inherit(*names: str, required: bool = True):
    return ProjectRuntimeEnvironmentSpec(
        inherit=tuple(ProjectEnvDeclaration(name, required) for name in names)
    )


def _secrets(*names: str, required: bool = True):
    return ProjectRuntimeEnvironmentSpec(
        secrets=tuple(ProjectEnvDeclaration(name, required) for name in names)
    )


# ---------------------------------------------------------------------------
# Canonical helper authority
# ---------------------------------------------------------------------------


def test_canonical_identity_is_platform_correct():
    assert canonical_env_name("foo", platform=WIN) == "FOO"
    assert canonical_env_name("MyProjectFlag", platform=WIN) == "MYPROJECTFLAG"
    assert canonical_env_name("foo", platform=NIX) == "foo"
    assert canonical_env_name("foo", platform="linux") == "foo"
    assert canonical_env_name("FOO", platform=NIX) == "FOO"


def test_canonical_defaults_to_live_platform():
    assert canonical_env_name("MiXeD") == canonical_env_name(
        "MiXeD", platform=sys.platform
    )
    assert resolve_env_name_platform(None) == sys.platform
    assert resolve_env_name_platform(WIN) == WIN


def test_canonical_rejects_bad_input():
    with pytest.raises(SessionRuntimeError):
        canonical_env_name("")
    with pytest.raises(SessionRuntimeError):
        canonical_env_name("HAS SPACE", platform=WIN)
    with pytest.raises(SessionRuntimeError):
        canonical_env_name("FOO", platform="")
    with pytest.raises(SessionRuntimeError):
        canonical_env_name("FOO", platform=123)


def test_essential_identity_is_platform_correct():
    assert is_platform_essential_name("PATH", platform=WIN)
    assert is_platform_essential_name("Path", platform=WIN)
    assert is_platform_essential_name("SystemRoot", platform=WIN)
    assert is_platform_essential_name("systemroot", platform=WIN)
    assert is_platform_essential_name("PATH", platform=NIX)
    assert is_platform_essential_name("SYSTEMROOT", platform=NIX)
    # On POSIX only the exact canonical spelling is essential: mixed-case
    # variants are genuinely different variables there.
    assert not is_platform_essential_name("Path", platform=NIX)
    assert not is_platform_essential_name("systemroot", platform=NIX)
    assert not is_platform_essential_name("SystemRoot", platform=NIX)
    assert not is_platform_essential_name("FOO", platform=WIN)
    assert not is_platform_essential_name("FOO", platform=NIX)
    assert not is_platform_essential_name("", platform=WIN)


# ---------------------------------------------------------------------------
# Declaration duplicates: Windows rejects, POSIX distinguishes
# ---------------------------------------------------------------------------


def test_windows_rejects_case_variant_duplicates_across_categories():
    spec = ProjectRuntimeEnvironmentSpec(
        inherit=(ProjectEnvDeclaration("FOO"),),
        secrets=(ProjectEnvDeclaration("foo"),),
    )
    with pytest.raises(SessionRuntimeError) as excinfo:
        spec.validate_for_platform(WIN)
    assert "declared more than once" in str(excinfo.value)


def test_windows_rejects_value_vs_inherit_case_collision():
    spec = ProjectRuntimeEnvironmentSpec(
        values=(ProjectExplicitValue("Foo", "plain"),),
        inherit=(ProjectEnvDeclaration("FOO"),),
    )
    with pytest.raises(SessionRuntimeError):
        spec.validate_for_platform(WIN)


def test_posix_keeps_case_variants_distinct():
    spec = ProjectRuntimeEnvironmentSpec(
        inherit=(ProjectEnvDeclaration("FOO"),),
        secrets=(ProjectEnvDeclaration("foo"),),
    )
    spec.validate_for_platform(NIX)
    assert spec.declared_names() == ("FOO", "foo")


def test_exact_duplicates_rejected_on_every_platform():
    with pytest.raises(SessionRuntimeError):
        ProjectRuntimeEnvironmentSpec(
            inherit=(
                ProjectEnvDeclaration("FOO"),
                ProjectEnvDeclaration("FOO"),
            )
        )


# ---------------------------------------------------------------------------
# Platform essentials fail at the ingress, not later
# ---------------------------------------------------------------------------


def test_essentials_rejected_at_declaration_time():
    with pytest.raises(SessionRuntimeError) as excinfo:
        _inherit("PATH").validate_for_platform(NIX)
    assert "platform essential" in str(excinfo.value)
    with pytest.raises(SessionRuntimeError):
        _inherit("PATH").validate_for_platform(WIN)
    with pytest.raises(SessionRuntimeError):
        _inherit("Path").validate_for_platform(WIN)
    with pytest.raises(SessionRuntimeError):
        _inherit("SystemRoot").validate_for_platform(WIN)
    with pytest.raises(SessionRuntimeError):
        _inherit("SYSTEMROOT").validate_for_platform(NIX)


def test_posix_case_variants_of_essentials_are_declarable():
    # On POSIX `Path` is a genuinely different variable from `PATH`
    # (nothing consumes the mixed-case spelling there).
    _inherit("Path").validate_for_platform(NIX)
    _secrets("systemroot").validate_for_platform(NIX)
    _inherit("SystemRoot").validate_for_platform(NIX)


def test_environment_authority_rejects_essentials_early():
    from agentic_debugger.application.execution_environment import (
        ExecutionEnvironmentError,
    )

    with pytest.raises(ExecutionEnvironmentError) as excinfo:
        ExecutionEnvironment.for_local_project(
            {"PATH": "/usr/bin"}, _inherit("PATH"), platform=NIX
        )
    assert "platform essential" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Materialization lookup follows platform identity
# ---------------------------------------------------------------------------


def test_windows_lookup_resolves_across_case():
    spec = _inherit("MyProjectFlag")
    materialization = materialize_project_runtime(
        spec, {"MYPROJECTFLAG": "resolved-value"}, platform=WIN
    )
    child = materialization.to_child_mapping()
    assert child == {"MYPROJECTFLAG": "resolved-value"}


def test_posix_lookup_is_case_exact():
    spec = _inherit("MyProjectFlag")
    with pytest.raises(ProjectRuntimeError) as excinfo:
        materialize_project_runtime(
            spec, {"MYPROJECTFLAG": "resolved-value"}, platform=NIX
        )
    assert "MyProjectFlag" in str(excinfo.value)


def test_windows_conflicting_snapshot_variants_fail_closed():
    spec = _inherit("FOO")
    with pytest.raises(ProjectRuntimeError) as excinfo:
        materialize_project_runtime(
            spec,
            {"FOO": "synthetic-value-one", "foo": "synthetic-value-two"},
            platform=WIN,
        )
    message = str(excinfo.value)
    assert "FOO" in message
    assert "synthetic-value-one" not in message
    assert "synthetic-value-two" not in message


def test_posix_distinct_variants_coexist():
    inherit = _inherit("FOO")
    materialization = materialize_project_runtime(
        inherit, {"FOO": "upper", "foo": "lower"}, platform=NIX
    )
    assert materialization.to_child_mapping() == {"FOO": "upper"}


def test_missing_required_names_safe_error_per_platform():
    for plat in (WIN, NIX):
        with pytest.raises(ProjectRuntimeError) as excinfo:
            materialize_project_runtime(
                _inherit("V2_09_REQUIRED_MISSING"), {"PATH": "/usr/bin"}, platform=plat
            )
        assert "V2_09_REQUIRED_MISSING" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Role environments follow platform identity
# ---------------------------------------------------------------------------


def test_role_environments_resolve_windows_case_insensitively():
    from agentic_debugger.application.execution_environment import ExecutionRole

    spec = _inherit("MyProjectFlag")
    authority = ExecutionEnvironment.for_local_project(
        {"MYPROJECTFLAG": "v", "PATH": "/usr/bin"}, spec, platform=WIN
    )
    assert not authority.uses_legacy_bridge
    for role in (
        ExecutionRole.PROJECT_COMMAND,
        ExecutionRole.PRODUCT_PDB,
        ExecutionRole.VERIFIER,
    ):
        assert authority.role_environment(role)["MYPROJECTFLAG"] == "v"


def test_posix_lowercase_path_is_not_an_essential():
    from agentic_debugger.application.execution_environment import ExecutionRole

    authority = ExecutionEnvironment.for_local_project(
        {"path": "lower", "PATH": "/usr/bin"},
        ProjectRuntimeEnvironmentSpec(),
        platform=NIX,
    )
    env = authority.role_environment(ExecutionRole.PROJECT_COMMAND)
    assert env["PATH"] == "/usr/bin"
    assert "path" not in env


def test_windows_preserves_snapshot_spelling_of_essentials():
    from agentic_debugger.application.execution_environment import ExecutionRole

    authority = ExecutionEnvironment.for_local_project(
        {"Path": "C:/windows"},
        ProjectRuntimeEnvironmentSpec(),
        platform=WIN,
    )
    env = authority.role_environment(ExecutionRole.PROJECT_COMMAND)
    assert env["Path"] == "C:/windows"


def test_windows_conflicting_snapshot_variants_fail_closed():
    from agentic_debugger.application.execution_environment import (
        ExecutionEnvironmentError,
        ExecutionRole,
    )

    # A real Windows snapshot can never hold both spellings (the OS store
    # is case-insensitive); an artificial one fails closed, name-only.
    with pytest.raises(ExecutionEnvironmentError) as excinfo:
        ExecutionEnvironment.for_local_project(
            {"Path": "C:/one", "PATH": "/other"},
            ProjectRuntimeEnvironmentSpec(),
            platform=WIN,
        )
    message = str(excinfo.value)
    assert "PATH" in message
    assert "C:/one" not in message
    assert "/other" not in message


# ---------------------------------------------------------------------------
# UI/parser parity: same authority, deterministic everywhere
# ---------------------------------------------------------------------------


def test_ui_parser_defers_to_application_authority():
    from agentic_debugger.ui.session_config import parse_project_env_declarations

    # Platform-neutral facts hold on every host.
    assert parse_project_env_declarations("FOO, BAR?") == (
        (("FOO", True), ("BAR", False)),
        (),
    )
    for bad in ("PATH", "OPENCODE_API_KEY", "AGENTIC_DEBUGGER_X", "FOO=bar"):
        with pytest.raises(ValueError):
            parse_project_env_declarations(bad)
    # Live-platform duplicates follow the same rule as the worker: on
    # Windows this rejects, on POSIX it parses (the worker agrees either
    # way because it re-validates with its own platform).
    from agentic_debugger.application.session_runtime import resolve_env_name_platform

    if resolve_env_name_platform() == "win32":
        with pytest.raises(ValueError):
            parse_project_env_declarations("FOO, foo")
    else:
        assert parse_project_env_declarations("FOO, foo") == (
            (("FOO", True), ("foo", True)),
            (),
        )


def test_secret_values_never_participate_in_identity():
    secret = "synthetic-v209-identity-secret-not-a-credential"
    spec = _secrets("V2_09_IDENTITY_SECRET")
    materialization = materialize_project_runtime(
        spec, {"V2_09_IDENTITY_SECRET": secret}, platform=WIN
    )
    assert materialization.to_child_mapping() == {"V2_09_IDENTITY_SECRET": secret}
    assert secret not in repr(materialization)
    assert secret not in repr(spec)
