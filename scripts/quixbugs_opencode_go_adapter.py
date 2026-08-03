"""Fail-closed OpenCode Go execution adapter for the QuixBugs paired-pilot v2 live runner.

This module implements and validates the execution-adapter wiring that can
later be supplied to the accepted six-case live runner
(:mod:`quixbugs_live_runner_v2`, campaign
``research/quixbugs/PAIRED_PILOT_V2.json``, canonical manifest hash
``bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171``, live
protocol ``1.3``) after (1) a real operator authorization artifact exists,
(2) exact runtime route evidence passes preflight, (3) this adapter's accepted
commit is bound in that authorization, and (4) the operator explicitly
authorizes the real campaign.

Scope and hard rules:

* the adapter defines a strict, versioned execution-adapter configuration
  contract; unknown fields, missing fields, wrong types, string shell
  commands, empty argv elements, relative or ambiguous executable paths,
  shell metacharacters, authorization/manifest/protocol/commit/route/catalog/
  model mismatches, executables or working directories outside the accepted
  operator boundary, hidden environment inheritance, and credential values
  embedded in argv/logs/evidence/tracked configuration are rejected;
* the transport factory adapts the accepted protocol transport
  (:mod:`opencode_protocol_transport`) to the paired-pilot live runner with
  structured argv only, an explicit working directory, a bounded environment
  allowlist, bounded stdout/stderr/diagnostics, process-group-aware timeout
  and cleanup, zero automatic retries, zero automatic model/provider
  fallback, zero automatic catalog queries, and no reliance on any
  globally selected model or prior interactive OpenCode session state;
* no process is created before the authorization, the execution-commit
  contract, the route observation, the adapter configuration, and the
  output/attempt ownership gates all pass, and the binding is revalidated
  before every provider process attempt;
* the runtime model identity is the exact catalog-qualified identity
  established by validated route evidence — never the historical
  ``opencode/deepseek-v4-flash-free`` OpenCode Zen identifier — and
  configuration, authorization, route observation, and transport invocation
  must agree exactly (alias rewriting, catalog/version/variant/route-class/
  billing-route drift, and any observed Zen/free-tier/Ollama/alternate-
  provider/fallback state are rejected);
* the case-runner binding reuses the accepted QuixBugs live execution path
  (:func:`agentic_debugger.evaluation.live_quixbugs.run_live_quixbugs_case`),
  controller, model adapter, protocol parsing, containment, verifier, PDB
  gate, event/trajectory logging, cleanup, and source restoration; one fresh
  transport/process/session boundary per frozen case; no shared model
  conversation; static-baseline cases cannot use PDB; PDB-on-uncertainty
  receives the exact task-local ``RuntimeProbe`` built from the frozen
  inventory entry's reviewed ``runtime_probe`` fields for the selected task
  (never derived from corrected source, tests, model output, or runtime
  guesses) and uses only the accepted controller gate and budgets;
  model-visible inputs are the accepted path's public inputs (corrected
  source, gold patch, evaluator oracle, private qualification evidence, and
  private authorization/account evidence are never exposed); the case runner
  never bypasses the live runner's ledger, terminal commitment, authority
  checks, stop rules, or result validator, and route drift, transport
  failure, malformed-response exhaustion, budget exhaustion, containment
  failure, verifier failure, cleanup failure, and public/private evidence
  violations map to the existing typed stop/result contracts;
* the facts-provider contract is task-bound: the case runner requests facts
  separately for every frozen case with the exact task manifest path
  (``provide(manifest_path: str) -> QuixBugsPreflightFacts``), requires an
  exact ``QuixBugsPreflightFacts`` result whose dependency preparation is
  bound to the selected task manifest, and rejects zero-argument generic
  facts providers, wrong-task facts, and malformed results before any
  provider interaction.  ``scripts/quixbugs_live_wire_environment.py`` is the
  small operator facts-provider module: it reuses the accepted read-only
  WSL/Bubblewrap readiness verification, never installs/clones/resets/cleans/
  downloads, creates task-bound verified facts from the selected manifest,
  and exposes ``describe_environment()`` for materializing
  ``quixbugs-environment.json``.

Nothing in this module contacts a live provider, model catalog, entitlement
service, account, or paid endpoint, and the CLI never defaults into live
execution.  Tests and the self-test mode use only the synthetic executable
(:mod:`opencode_go_synthetic_executable`), temporary fixtures, and
deterministic transport doubles.

The operator preparation flow adds two focused operator-facing modes:

* ``route-capture`` — a read-only command that runs only local/non-model
  OpenCode inspection commands (``opencode.cmd --version`` and
  ``opencode.cmd models opencode-go --verbose --pure``), never invokes
  ``opencode run``, requires the exact operator-selected runtime model ID
  and variant, locates exactly one active catalog entry, records its
  observed status, variant availability, and finite pricing metadata,
  rejects the historical Zen/free-tier identity, requires explicit
  operator-supplied account status, subscription entitlement
  confirmation/reference, and a billing-route assertion, records every
  denial/fallback observation explicitly, and writes a strict
  ``quixbugs-route-evidence-v1`` artifact (accepted by the existing
  live-runner validator) with create-once semantics into the ignored
  ``operator/`` storage;
* ``operator-bundle`` — consumes the accepted route-evidence file and
  materializes the real ``quixbugs-paired-pilot-authorization-v1`` artifact
  and the real ``quixbugs-opencode-go-execution-adapter-v1`` configuration,
  both bound to the actual clean Git HEAD observed (read-only) when the
  operator runs the command after the task has been accepted and merged —
  never to a caller-supplied commit and never to the task baseline
  (:data:`TASK_BASELINE`, retained only as the minimum lineage prerequisite).
  The observed HEAD must exist, must descend from the accepted project
  baseline and from the task baseline, and must have a clean tracked working
  tree, a clean real index, and no non-ignored untracked files; HEAD and
  repository cleanliness are re-checked immediately before the artifacts are
  created and any drift fails closed with no active artifact written.  The
  artifacts are also bound to the frozen manifest hash, the exact six frozen
  case IDs in order, protocol ``1.3``, the exact observed OpenCode version,
  runtime model ID, variant, and catalog fingerprint, the account status and
  subscription billing route, one operator authorization ID, one fresh
  attempt identity and output root, an explicit bounded validity period, and
  the operator-resolved Python executable, repository wrapper path, working
  directory, and operator boundary root; dirty/staged source, drift, occupied
  targets, template values, route drift, unknown fields, malformed paths, and
  contradictory subscription/fallback assertions are rejected, and active
  operator artifacts are never committed.  Route capture stays independent of
  Git commit binding; the bundle performs the commit binding.

The deterministic catalog-entry fingerprint contract is implemented by
:func:`opencode_protocol_transport.catalog_entry_fingerprint`: the exact
selected catalog entry is serialized with the project's canonical JSON
rules and SHA-256 of that canonical representation is the fingerprint used
identically in route evidence, authorization, adapter configuration, and
wrapper verification.  The wrapper's OpenCode Go preflight independently
recomputes the selected entry fingerprint and compares it with the
authorization-bound expected fingerprint before any model process may run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import quixbugs_live_runner_v2 as runner  # noqa: E402
import quixbugs_paired_pilot as pilot  # noqa: E402
from scripts import opencode_protocol_transport as transport  # noqa: E402

ADAPTER_SCHEMA_VERSION = "quixbugs-opencode-go-execution-adapter-v1"
ADAPTER_IDENTITY = "quixbugs-opencode-go-execution-adapter-v1"
#: The exact accepted protocol wrapper this adapter must launch; a direct
#: OpenCode CLI command that bypasses the wrapper is rejected.
PROTOCOL_WRAPPER_RELATIVE_PATH = "scripts/opencode_protocol_transport.py"
PROTOCOL_WRAPPER_PATH = REPO_ROOT / PROTOCOL_WRAPPER_RELATIVE_PATH
#: The explicit subscription-route mode the wrapper must be launched with.
ADAPTER_ROUTE_MODE = "opencode-go"
#: The strict raw route-evidence schema version the operator route capture
#: produces and the accepted live-runner validator consumes.
ROUTE_EVIDENCE_SCHEMA_VERSION = "quixbugs-route-evidence-v1"
#: Version of the non-authoritative operator capture companion record.
CAPTURE_RECORD_SCHEMA_VERSION = "quixbugs-route-capture-record-v1"
#: The ignored operator storage root; route evidence, operator bundles, and
#: all active operator artifacts live here and are never committed.
OPERATOR_STORAGE = REPO_ROOT / "operator"
#: Relative operator-bundle storage directory under :data:`OPERATOR_STORAGE`.
OPERATOR_BUNDLES_RELATIVE_DIR = "quixbugs-operator-bundles-v1"
#: The minimum accepted lineage/task baseline of this task: the commit the
#: accepted task work descends from.  It is a lineage prerequisite only and
#: is NEVER used as the campaign execution commit: the operator bundle binds
#: authorization and adapter configuration to the actual clean Git HEAD
#: observed (read-only) when the operator runs the command after this task
#: has been accepted and merged.
TASK_BASELINE = "618c33ff186493892665ca1233c3edd8b2eec13f"
#: Bounded capture of OpenCode inspection command output.
MAX_CAPTURE_COMMAND_OUTPUT_BYTES = 1_000_000
#: Placeholder/template markers rejected in operator-supplied values.
_TEMPLATE_VALUE = re.compile(r"[<>]|placeholder", re.I)
TEMPLATE_NOTE = (
    "NON-EXECUTABLE TEMPLATE. This document is a schema reference only; it is not an active "
    "adapter configuration and must fail validation as one (template=true). An active "
    "configuration must be created by the operator outside tracked source (ignored operator/ "
    "location), must replace every placeholder with a genuinely observed value, must set "
    "template=false, and must pass the strict adapter validation plus the authorization/route/"
    "execution-commit binding before any provider process may be created."
)

#: The historical OpenCode Zen free-model identifier is never a valid runtime
#: execution identity for this adapter.
HISTORICAL_ZEN_MODEL_ID = "opencode/deepseek-v4-flash-free"
#: The exact catalog-qualified runtime identity prefix required in Go mode:
#: every Go runtime identity must use the ``opencode-go/`` provider prefix;
#: ``opencode/`` and any other provider is rejected before model execution.
GO_RUNTIME_ID_PREFIX = "opencode-go/"

#: Shell metacharacters rejected anywhere in structured argv elements (the
#: config is executed with shell=False; these characters would be
#: mis-executable in any shell-parsed context and are rejected fail-closed).
_SHELL_METACHARACTERS = set("&|<>;()$\"%'`^!%")
_CONTROL_CHARACTERS = frozenset(chr(value) for value in range(0x20)) | frozenset({"\x7f"})

_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?key|auth(?:orization)?|credential|password|secret|token|private[_-]?key|cookie)",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"(?i)\b(?:bearer|basic)\s+\S+|\b(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|token|cookie)\s*[:=]\s*\S+"
)
_SECRET_ARGUMENT = re.compile(r"^--?(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|private[_-]?key|token|cookie)(?:=|$)", re.I)
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEX40_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")

MAX_COMMAND_ARGUMENTS = 32
MAX_ALLOWLIST_ENTRIES = 32
MAX_CONFIGURATION_BYTES = 64 * 1024
MAX_EVIDENCE_BYTES = 1_000_000
MAX_PROCESS_STDERR_DIAGNOSTIC_BYTES = 16_384

DENIAL_FIELDS = (
    "deny_zen_route",
    "deny_free_tier_substitution",
    "deny_ollama_route",
    "deny_alternate_provider",
    "deny_model_substitution",
    "deny_metered_fallback",
    "deny_paid_overage",
    "deny_per_call_billing_fallback",
)

#: Strict adapter-configuration contract.  ``command`` is a structured argv
#: list whose first element must resolve to ``executable``; string shell
#: commands are rejected.
ADAPTER_CONFIGURATION_FIELDS = frozenset({
    "schema_version",
    "template",
    "adapter_identity",
    "campaign_id",
    "campaign_manifest_hash",
    "operator_authorization_id",
    "authorization_hash",
    "execution_commit",
    "executable",
    "command",
    "working_directory",
    "operator_boundary_root",
    "protocol_version",
    "provider",
    "model_family",
    "variant",
    "runtime_model_id",
    "opencode_version",
    "catalog_fingerprint",
    "route_class",
    "expected_account_status",
    "per_call_timeout_seconds",
    "total_case_timeout_seconds",
    "environment_allowlist",
    "max_stdout_bytes",
    "max_stderr_bytes",
    "max_diagnostic_bytes",
    "transport_retry_limit",
    "max_transport_attempts_per_logical_call",
    "no_automatic_route_discovery",
    "no_global_model_selection",
    "requires_active_authorization_binding",
    *DENIAL_FIELDS,
    "no_fallback_required",
})
ADAPTER_CONFIGURATION_STRING_FIELDS = frozenset({
    "schema_version", "adapter_identity", "campaign_id", "campaign_manifest_hash",
    "operator_authorization_id", "authorization_hash", "execution_commit",
    "executable", "working_directory", "operator_boundary_root", "protocol_version",
    "provider", "model_family", "variant", "runtime_model_id", "opencode_version",
    "catalog_fingerprint", "route_class", "expected_account_status",
})
ADAPTER_CONFIGURATION_BOOL_FIELDS = frozenset({
    "template", "no_automatic_route_discovery", "no_global_model_selection",
    "requires_active_authorization_binding", "no_fallback_required", *DENIAL_FIELDS,
})
ADAPTER_CONFIGURATION_INT_FIELDS = frozenset({
    "max_stdout_bytes", "max_stderr_bytes", "max_diagnostic_bytes",
    "transport_retry_limit", "max_transport_attempts_per_logical_call",
})
ADAPTER_CONFIGURATION_NUMBER_FIELDS = frozenset({
    "per_call_timeout_seconds", "total_case_timeout_seconds",
})
MAX_PER_CALL_TIMEOUT_SECONDS = 300.0
MAX_TOTAL_CASE_TIMEOUT_SECONDS = 3600.0
MIN_OUTPUT_BOUND_BYTES = 1024
MAX_OUTPUT_BOUND_BYTES = 4 * 1024 * 1024
MAX_DIAGNOSTIC_BOUND_BYTES = 1_000_000

#: Frozen configuration rejection reason codes.
CONFIGURATION_REJECTION_CODES = frozenset({
    "MISSING_CONFIGURATION", "NOT_AN_OBJECT", "NON_FINITE_VALUE", "TEMPLATE_IS_NOT_CONFIGURATION",
    "SCHEMA_VERSION_MISMATCH", "ADAPTER_IDENTITY_MISMATCH", "UNKNOWN_FIELDS", "MISSING_FIELDS",
    "WRONG_TYPE", "STRING_SHELL_COMMAND", "EMPTY_ARGV_ELEMENT", "ARGV_TOO_LONG",
    "EXECUTABLE_NOT_FIRST_ARGV", "RELATIVE_EXECUTABLE", "EXECUTABLE_OUTSIDE_BOUNDARY",
    "WORKING_DIRECTORY_OUTSIDE_BOUNDARY", "BOUNDARY_NOT_ABSOLUTE", "SHELL_METACHARACTER",
    "CREDENTIAL_IN_CONFIGURATION", "CAMPAIGN_IDENTITY_MISMATCH", "MANIFEST_HASH_MISMATCH",
    "PROTOCOL_MISMATCH", "COMMIT_MISMATCH", "AUTHORIZATION_HASH_MISMATCH",
    "OPERATOR_AUTHORIZATION_ID_MISMATCH", "PROVIDER_MISMATCH", "ROUTE_CLASS_MISMATCH",
    "VARIANT_MISMATCH", "OPENCODE_VERSION_MISMATCH", "CATALOG_FINGERPRINT_MISMATCH",
    "RUNTIME_MODEL_ID_MISMATCH", "MODEL_FAMILY_MISMATCH", "ACCOUNT_STATUS_MISMATCH",
    "HISTORICAL_ZEN_IDENTITY", "DENIAL_FLAG_NOT_TRUE", "FALLBACK_POLICY_MISMATCH",
    "AUTOMATIC_ROUTE_DISCOVERY_NOT_DENIED", "GLOBAL_MODEL_SELECTION_NOT_DENIED",
    "AUTHORIZATION_BINDING_NOT_REQUIRED", "ROUTE_OBSERVATION_NOT_ESTABLISHED",
    "BUDGET_CONTRADICTION", "TIMEOUT_CONTRADICTION", "ALLOWLIST_INVALID", "ALLOWLIST_SECRET_NAME",
    "EXECUTABLE_MISSING", "WORKING_DIRECTORY_MISSING", "ENVIRONMENT_ALLOWLIST_MISSING",
    "TRANSPORT_ACCOUNTING_CONTRADICTION",
    "WRAPPER_NOT_BOUND", "DIRECT_OPENCODE_COMMAND_REJECTED", "ROUTE_MODE_NOT_BOUND",
    "ROUTE_BINDING_FLAGS_MISSING",
})

#: Runtime identity drift categories (map to the accepted typed stop/result
#: contracts via :class:`quixbugs_live_runner_v2.RouteDriftError`).
DRIFT_CATEGORIES = frozenset({
    "PROVIDER_MISMATCH", "MODEL_SUBSTITUTION_OBSERVED", "RUNTIME_MODEL_ID_MISMATCH",
    "MODEL_MISMATCH", "VARIANT_MISMATCH", "PROTOCOL_MISMATCH", "OPENCODE_VERSION_MISMATCH",
    "CATALOG_PREFLIGHT_FAILED", "BILLING_ROUTE_MISMATCH", "ZEN_ROUTE_OBSERVED",
    "FREE_TIER_SUBSTITUTION", "OLLAMA_ROUTE_OBSERVED", "METERED_FALLBACK_REQUIRED",
    "PAID_OVERAGE_REQUIRED", "PER_CALL_BILLING_FALLBACK", "ALTERNATE_PROVIDER_REQUIRED",
    "AUTHORIZATION_BINDING_DRIFT", "EXECUTION_COMMIT_DRIFT",
})


class AdapterConfigurationError(ValueError):
    """A fail-closed adapter-configuration rejection; no process is created."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class OpenCodeGoAdapterError(ValueError):
    """A fail-closed adapter wiring error."""


class _BoundedCapture:
    def __init__(self, maximum_bytes: int) -> None:
        self.maximum_bytes = maximum_bytes
        self.data = bytearray()
        self.truncated = False
        self.lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        with self.lock:
            remaining = self.maximum_bytes - len(self.data)
            if remaining > 0:
                self.data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.truncated = True

    def text(self) -> str:
        with self.lock:
            return bytes(self.data).decode("utf-8", errors="replace")


def _read_pipe(pipe: Any, capture: _BoundedCapture) -> None:
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                return
            capture.add(chunk)
    except Exception:
        return


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    """Process-group-aware termination using existing runtime mechanisms.

    On Windows the whole process tree is terminated first (while the parent
    process still exists), then the direct process is terminated; a grandchild
    process cannot survive orphaned.
    """
    if os.name == "nt" and process.pid is not None:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
            )
        except Exception:
            pass
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _redact(value: Any) -> Any:
    from agentic_debugger.evaluation.live import redact_for_recording

    return redact_for_recording(value)


def _bounded_evidence(value: Any, limit: int = MAX_EVIDENCE_BYTES) -> str:
    """Serialize evidence strictly bounded and credential-redacted."""
    payload = json.dumps(_redact(value), ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(payload.encode("utf-8")) > limit:
        payload = json.dumps({
            "truncated": True,
            "original_character_count": len(payload),
            "record": _redact({"summary": "evidence truncated"}),
        }, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return payload


def _append_evidence(path: Path | None, record: Mapping[str, Any]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(_bounded_evidence(dict(record)) + "\n")
    except OSError:
        return


def _assert_finite_json(value: Any, path: str = "root") -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AdapterConfigurationError("NON_FINITE_VALUE", f"non-finite numeric value at {path}")
        return
    if isinstance(value, int):
        return
    if isinstance(value, str):
        return
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_finite_json(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_finite_json(item, f"{path}.{key}")
        return
    raise AdapterConfigurationError("NON_FINITE_VALUE", f"unsupported JSON value type at {path}: {type(value).__name__}")


def _has_shell_metacharacter(value: str) -> str | None:
    for char in value:
        if char in _SHELL_METACHARACTERS or char in _CONTROL_CHARACTERS:
            return char
    return None


def _contains_credential(value: str) -> bool:
    return bool(_SECRET_VALUE.search(value) or _SECRET_ARGUMENT.search(value))


def common_operator_boundary(paths: list[str | Path]) -> Path:
    """Resolve a common absolute operator boundary for a set of paths.

    Returns the deepest common ancestor directory of all resolved paths; used
    by synthetic fixtures so the synthetic executable and working directory
    are always inside the (synthetic) operator boundary.
    """
    resolved = [Path(path).resolve() for path in paths]
    try:
        common = os.path.commonpath([str(path) for path in resolved])
    except ValueError as exc:
        raise OpenCodeGoAdapterError(f"operator boundary could not be derived from paths: {exc}") from exc
    boundary = Path(common)
    if not boundary.is_dir():
        boundary = boundary.parent
    return boundary


def _reject(reason: str, detail: str) -> None:
    raise AdapterConfigurationError(reason, detail)


# ---- adapter configuration contract -----------------------------------------


def adapter_configuration_template() -> dict[str, Any]:
    """The non-executable adapter configuration template.

    Structurally complete, but carries ``template: true``, placeholder
    identities, and an explicit non-executable note; the strict validator
    rejects it as an active configuration.
    """
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "template": True,
        "adapter_identity": ADAPTER_IDENTITY,
        "campaign_id": pilot.CAMPAIGN_ID_V2,
        "campaign_manifest_hash": "bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171",
        "operator_authorization_id": "<operator authorization record ID bound by the authorization artifact>",
        "authorization_hash": "<64-hex SHA-256 of the exact authorization artifact JSON>",
        "execution_commit": "<40-hex commit whose code will execute the campaign; must equal the authorization-bound accepted_campaign_commit and the actual Git HEAD>",
        "executable": "<absolute path to the operator-resolved Python executable that launches the accepted protocol wrapper>",
        "command": [
            "<argv[0]: absolute operator-resolved Python executable>",
            "<repository>/scripts/opencode_protocol_transport.py",
            "--model",
            "<runtime model identity from validated route evidence>",
            "--variant",
            "max",
            "--route-mode",
            "opencode-go",
            "--expected-opencode-version",
            "<exact OpenCode runtime version from validated route evidence>",
            "--expected-catalog-fingerprint",
            "<64-hex catalog fingerprint from validated route evidence>",
            "--expected-runtime-model-id",
            "<exact catalog-qualified runtime model identity>",
            "--expected-account-status",
            "<required account/route status from authorization and route evidence>",
            "--expected-billing-route",
            "SUBSCRIPTION",
        ],
        "working_directory": "<absolute working directory for every provider process>",
        "operator_boundary_root": "<absolute operator-accepted boundary; executable and working directory must be inside it>",
        "protocol_version": runner.LIVE_PROTOCOL_VERSION,
        "provider": "OpenCode Go",
        "model_family": "deepseek-v4-flash",
        "variant": "max",
        "runtime_model_id": "<exact catalog-qualified runtime model identity from validated route evidence>",
        "opencode_version": "<exact OpenCode runtime version from validated route evidence>",
        "catalog_fingerprint": "<64-hex catalog fingerprint from validated route evidence>",
        "route_class": "SUBSCRIPTION",
        "expected_account_status": "<required account/route status from authorization and route evidence>",
        "per_call_timeout_seconds": 60.0,
        "total_case_timeout_seconds": 900.0,
        "environment_allowlist": ["PATH", "SystemRoot"],
        "max_stdout_bytes": 1048576,
        "max_stderr_bytes": 1048576,
        "max_diagnostic_bytes": 16384,
        "transport_retry_limit": 2,
        "max_transport_attempts_per_logical_call": 3,
        "no_automatic_route_discovery": True,
        "no_global_model_selection": True,
        "requires_active_authorization_binding": True,
        "deny_zen_route": True,
        "deny_free_tier_substitution": True,
        "deny_ollama_route": True,
        "deny_alternate_provider": True,
        "deny_model_substitution": True,
        "deny_metered_fallback": True,
        "deny_paid_overage": True,
        "deny_per_call_billing_fallback": True,
        "no_fallback_required": True,
        "_template_note": TEMPLATE_NOTE,
    }


def write_adapter_configuration_template(path: str | Path) -> Path:
    target = Path(path)
    if target.exists():
        raise OpenCodeGoAdapterError(f"adapter configuration template target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(adapter_configuration_template(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def load_adapter_configuration(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterConfigurationError("MISSING_CONFIGURATION", f"adapter configuration could not be loaded: {exc}")
    if not isinstance(value, Mapping):
        _reject("NOT_AN_OBJECT", "adapter configuration must be a JSON object")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_CONFIGURATION_BYTES:
        _reject("NON_FINITE_VALUE", "adapter configuration exceeds the size bound")
    return dict(value)


def _validate_wrapper_command_shape(value: Mapping[str, Any]) -> None:
    """Bind the exact accepted protocol wrapper and its structured argv.

    The active command must launch ``scripts/opencode_protocol_transport.py``
    (the accepted stdin protocol wrapper) with the exact authorization-bound
    model identity, the exact variant, the explicit OpenCode Go route mode,
    and the route-binding flags already validated by the outer
    authorization/preflight contract.  A direct OpenCode CLI command
    (``opencode run ...``) that bypasses the wrapper is rejected, and only
    the wrapper may own the ``--evidence-file`` argument.
    """
    command = value["command"]
    if len(command) < 2:
        _reject("WRAPPER_NOT_BOUND", "adapter configuration command must launch the accepted protocol wrapper")
    second = command[1]
    if type(second) is not str or not second.strip():
        _reject("WRAPPER_NOT_BOUND", "adapter configuration command argv[1] must be the accepted protocol wrapper path")
    try:
        resolved_second = Path(second).resolve()
    except (OSError, ValueError):
        _reject("WRAPPER_NOT_BOUND", "adapter configuration command argv[1] is not a resolvable wrapper path")
    wrapper = PROTOCOL_WRAPPER_PATH.resolve()
    if resolved_second != wrapper:
        if second in {"run", "debug", "models"} or (type(second) is str and "opencode" in Path(second).name.lower() and "protocol_transport" not in str(second)):
            _reject(
                "DIRECT_OPENCODE_COMMAND_REJECTED",
                "adapter configuration launches a direct OpenCode CLI command that bypasses the accepted protocol wrapper",
            )
        _reject("WRAPPER_NOT_BOUND", "adapter configuration command argv[1] must be the accepted protocol wrapper path")
    command_value = command
    pairs = {
        (command_value[index], command_value[index + 1])
        for index in range(len(command_value) - 1)
    }
    if ("--route-mode", ADAPTER_ROUTE_MODE) not in pairs:
        _reject("ROUTE_MODE_NOT_BOUND", "adapter configuration command must bind --route-mode opencode-go")
    required = {
        "--model": value["runtime_model_id"],
        "--variant": value["variant"],
        "--expected-opencode-version": value["opencode_version"],
        "--expected-catalog-fingerprint": value["catalog_fingerprint"],
        "--expected-runtime-model-id": value["runtime_model_id"],
        "--expected-account-status": value["expected_account_status"],
        "--expected-billing-route": value["route_class"],
    }
    for flag, expected in required.items():
        if (flag, expected) not in pairs:
            _reject("ROUTE_BINDING_FLAGS_MISSING", f"adapter configuration command must bind {flag} to {expected!r}")


def validate_adapter_configuration_structure(value: Mapping[str, Any]) -> dict[str, Any]:
    """Strict structural validation of the adapter configuration contract.

    Rejects unknown fields, missing fields, wrong types, non-finite values,
    string shell commands, empty argv elements, relative or ambiguous
    executable paths, shell metacharacters, credential-shaped content,
    invalid allowlists, and budget/timeout contradictions.  The template
    (``template: true``) is rejected as an active configuration.
    """
    if not isinstance(value, Mapping):
        _reject("NOT_AN_OBJECT", "adapter configuration must be an object")
    try:
        _assert_finite_json(value)
    except AdapterConfigurationError as exc:
        raise
    if value.get("template") is True:
        _reject("TEMPLATE_IS_NOT_CONFIGURATION", "adapter configuration carries template=true; it is a non-executable schema reference and cannot be an active configuration")
    if value.get("schema_version") != ADAPTER_SCHEMA_VERSION:
        _reject("SCHEMA_VERSION_MISMATCH", f"unsupported adapter configuration schema version: {value.get('schema_version')!r}")
    if value.get("adapter_identity") != ADAPTER_IDENTITY:
        _reject("ADAPTER_IDENTITY_MISMATCH", "adapter identity mismatch")
    if "command" in value and isinstance(value.get("command"), str):
        _reject("STRING_SHELL_COMMAND", "adapter configuration must use a structured argv command list, not a string shell command")
    unknown = set(value) - ADAPTER_CONFIGURATION_FIELDS - {"_template_note"}
    if unknown:
        _reject("UNKNOWN_FIELDS", f"adapter configuration carries unsupported fields: {sorted(unknown)}")
    missing = ADAPTER_CONFIGURATION_FIELDS - set(value)
    if missing:
        _reject("MISSING_FIELDS", f"adapter configuration is missing fields: {sorted(missing)}")
    for field in ADAPTER_CONFIGURATION_STRING_FIELDS:
        if type(value[field]) is not str:
            _reject("WRONG_TYPE", f"adapter configuration field {field} must be a string")
    for field in ADAPTER_CONFIGURATION_BOOL_FIELDS:
        if type(value[field]) is not bool:
            _reject("WRONG_TYPE", f"adapter configuration field {field} must be boolean")
    for field in ADAPTER_CONFIGURATION_INT_FIELDS:
        if type(value[field]) is not int or isinstance(value[field], bool):
            _reject("WRONG_TYPE", f"adapter configuration field {field} must be an integer")
    for field in ADAPTER_CONFIGURATION_NUMBER_FIELDS:
        number = value[field]
        if type(number) not in (int, float) or isinstance(number, bool):
            _reject("WRONG_TYPE", f"adapter configuration field {field} must be a number")
    if not _HEX64_PATTERN.fullmatch(value["authorization_hash"]):
        _reject("AUTHORIZATION_HASH_MISMATCH", "adapter configuration authorization_hash must be a 64-hex string")
    if not _HEX40_PATTERN.fullmatch(value["execution_commit"]):
        _reject("COMMIT_MISMATCH", "adapter configuration execution_commit must be a 40-hex string")
    if not _HEX64_PATTERN.fullmatch(value["catalog_fingerprint"]):
        _reject("CATALOG_FINGERPRINT_MISMATCH", "adapter configuration catalog_fingerprint must be a 64-hex string")
    if value["protocol_version"] != runner.LIVE_PROTOCOL_VERSION:
        _reject("PROTOCOL_MISMATCH", f"adapter configuration protocol_version must be {runner.LIVE_PROTOCOL_VERSION!r}")
    if value["route_class"] != pilot.AUTHORIZED_BILLING_ROUTE:
        _reject("ROUTE_CLASS_MISMATCH", "adapter configuration route_class must be SUBSCRIPTION")
    for field in ("provider", "model_family", "variant", "runtime_model_id", "opencode_version", "expected_account_status", "operator_authorization_id", "working_directory", "operator_boundary_root", "executable"):
        if not value[field].strip():
            _reject("MISSING_FIELDS", f"adapter configuration field {field} must be non-empty")
    if value["runtime_model_id"] == HISTORICAL_ZEN_MODEL_ID:
        _reject("HISTORICAL_ZEN_IDENTITY", "the historical OpenCode Zen free-model identity cannot be an OpenCode Go execution identity")
    if not value["runtime_model_id"].startswith(GO_RUNTIME_ID_PREFIX):
        _reject(
            "PROVIDER_MISMATCH",
            f"adapter configuration runtime_model_id must use the exact opencode-go/ provider prefix; rejected {value['runtime_model_id']!r}",
        )
    if _contains_credential(value["runtime_model_id"]) or _contains_credential(value["operator_authorization_id"]):
        _reject("CREDENTIAL_IN_CONFIGURATION", "adapter configuration carries credential-shaped identity content")

    boundary = Path(value["operator_boundary_root"])
    if not boundary.is_absolute():
        _reject("BOUNDARY_NOT_ABSOLUTE", "operator_boundary_root must be an absolute path")
    working = Path(value["working_directory"])
    executable_path = Path(value["executable"])
    if not executable_path.is_absolute():
        _reject("RELATIVE_EXECUTABLE", "the adapter executable must be an exact absolute operator-resolved path")
    if not working.is_absolute():
        _reject("WORKING_DIRECTORY_OUTSIDE_BOUNDARY", "the adapter working directory must be an absolute path")
    for field, path_value in (("executable", executable_path), ("working_directory", working)):
        try:
            path_value.resolve().relative_to(boundary.resolve())
        except ValueError:
            _reject(
                "EXECUTABLE_OUTSIDE_BOUNDARY" if field == "executable" else "WORKING_DIRECTORY_OUTSIDE_BOUNDARY",
                f"adapter {field} is outside the accepted operator boundary {boundary}",
            )

    command = value["command"]
    if not isinstance(command, list):
        _reject("WRONG_TYPE", "adapter configuration command must be a structured argv list")
    if not command or len(command) > MAX_COMMAND_ARGUMENTS:
        _reject("ARGV_TOO_LONG", f"adapter configuration command must contain between 1 and {MAX_COMMAND_ARGUMENTS} argv elements")
    if type(command[0]) is not str:
        _reject("WRONG_TYPE", "adapter configuration command argv[0] must be a string")
    resolved_executable = executable_path.resolve()
    if Path(command[0]).resolve() != resolved_executable:
        _reject("EXECUTABLE_NOT_FIRST_ARGV", "adapter configuration command argv[0] must resolve to the exact configured executable")
    for index, element in enumerate(command):
        if type(element) is not str or not element.strip():
            _reject("EMPTY_ARGV_ELEMENT", f"adapter configuration command element {index} is empty")
        metachar = _has_shell_metacharacter(element)
        if metachar is not None:
            _reject("SHELL_METACHARACTER", f"adapter configuration command element {index} contains shell metacharacter {metachar!r}")
        if _contains_credential(element):
            _reject("CREDENTIAL_IN_CONFIGURATION", f"adapter configuration command element {index} carries credential-shaped content")
    _validate_wrapper_command_shape(value)
    if not executable_path.is_file():
        _reject("EXECUTABLE_MISSING", f"adapter executable does not exist: {executable_path}")
    if not working.is_dir():
        _reject("WORKING_DIRECTORY_MISSING", f"adapter working directory does not exist: {working}")

    allowlist = value["environment_allowlist"]
    if not isinstance(allowlist, list) or not allowlist or len(allowlist) > MAX_ALLOWLIST_ENTRIES:
        _reject("ALLOWLIST_INVALID", "environment_allowlist must be a non-empty list of environment variable names")
    for name in allowlist:
        if type(name) is not str or not _ENV_NAME_PATTERN.fullmatch(name):
            _reject("ALLOWLIST_INVALID", f"environment_allowlist entry {name!r} is not a valid environment variable name")
        if _SECRET_KEY.search(name):
            _reject("ALLOWLIST_SECRET_NAME", f"environment_allowlist entry {name!r} is a credential-shaped name")

    per_call = float(value["per_call_timeout_seconds"])
    total_case = float(value["total_case_timeout_seconds"])
    if not 0 < per_call <= MAX_PER_CALL_TIMEOUT_SECONDS:
        _reject("TIMEOUT_CONTRADICTION", "per_call_timeout_seconds must be positive and within the accepted bound")
    if not 0 < total_case <= MAX_TOTAL_CASE_TIMEOUT_SECONDS:
        _reject("TIMEOUT_CONTRADICTION", "total_case_timeout_seconds must be positive and within the accepted bound")
    if per_call > total_case:
        _reject("TIMEOUT_CONTRADICTION", "per_call_timeout_seconds must not exceed total_case_timeout_seconds")
    for field, low, high in (
        ("max_stdout_bytes", MIN_OUTPUT_BOUND_BYTES, MAX_OUTPUT_BOUND_BYTES),
        ("max_stderr_bytes", MIN_OUTPUT_BOUND_BYTES, MAX_OUTPUT_BOUND_BYTES),
        ("max_diagnostic_bytes", MIN_OUTPUT_BOUND_BYTES, MAX_DIAGNOSTIC_BOUND_BYTES),
    ):
        if not low <= value[field] <= high:
            _reject("WRONG_TYPE", f"adapter configuration field {field} is outside the accepted bound")
    retry_limit = value["transport_retry_limit"]
    attempts_per_call = value["max_transport_attempts_per_logical_call"]
    if retry_limit < 0 or attempts_per_call < 1 or retry_limit + 1 > attempts_per_call:
        _reject("TRANSPORT_ACCOUNTING_CONTRADICTION", "transport_retry_limit must be below max_transport_attempts_per_logical_call")
    for flag in DENIAL_FIELDS:
        if value[flag] is not True:
            _reject("DENIAL_FLAG_NOT_TRUE", f"adapter configuration denial flag {flag} must be true")
    if value["no_fallback_required"] is not True:
        _reject("FALLBACK_POLICY_MISMATCH", "adapter configuration no_fallback_required must be true")
    if value["no_automatic_route_discovery"] is not True:
        _reject("AUTOMATIC_ROUTE_DISCOVERY_NOT_DENIED", "adapter configuration must deny automatic route discovery")
    if value["no_global_model_selection"] is not True:
        _reject("GLOBAL_MODEL_SELECTION_NOT_DENIED", "adapter configuration must deny global model selection")
    if value["requires_active_authorization_binding"] is not True:
        _reject("AUTHORIZATION_BINDING_NOT_REQUIRED", "adapter configuration must require the active authorization binding")
    return dict(value)


def bind_adapter_configuration(
    value: Mapping[str, Any],
    manifest: Mapping[str, Any],
    authorization: Mapping[str, Any],
    route_observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Cross-bind a validated configuration against the accepted authority.

    Requires the validated authorization, the verified execution commit, and
    the passed route observation; every identity field must agree exactly
    across configuration, authorization, and route observation.  Raises
    :class:`AdapterConfigurationError` on any mismatch.
    """
    validated = validate_adapter_configuration_structure(value)
    if authorization is None:
        _reject("AUTHORIZATION_HASH_MISMATCH", "binding requires a validated authorization artifact")
    if route_observation is None:
        _reject("ROUTE_OBSERVATION_NOT_ESTABLISHED", "binding requires a passed route observation")
    if route_observation.get("preflight_success") is not True:
        _reject("ROUTE_OBSERVATION_NOT_ESTABLISHED", "binding requires a preflight-passed route observation")

    manifest_hash = pilot.manifest_hash(manifest)
    if validated["campaign_id"] != pilot.CAMPAIGN_ID_V2 or validated["campaign_id"] != authorization.get("campaign_id"):
        _reject("CAMPAIGN_IDENTITY_MISMATCH", "adapter configuration campaign identity does not match the frozen v2 campaign")
    if validated["campaign_manifest_hash"] != manifest_hash or validated["campaign_manifest_hash"] != authorization.get("campaign_manifest_hash"):
        _reject("MANIFEST_HASH_MISMATCH", "adapter configuration manifest hash does not match the validated manifest/authorization")
    if validated["operator_authorization_id"] != authorization.get("operator_authorization_id"):
        _reject("OPERATOR_AUTHORIZATION_ID_MISMATCH", "adapter configuration operator authorization identity mismatch")
    if validated["authorization_hash"] != runner.authorization_hash(authorization):
        _reject("AUTHORIZATION_HASH_MISMATCH", "adapter configuration authorization hash mismatch")
    if validated["execution_commit"] != authorization.get("accepted_campaign_commit"):
        _reject("COMMIT_MISMATCH", "adapter configuration execution_commit must equal the authorization-bound accepted_campaign_commit")
    if validated["execution_commit"] != route_observation.get("execution_commit"):
        _reject("COMMIT_MISMATCH", "adapter configuration execution_commit must equal the route observation execution commit")

    if validated["provider"] != authorization.get("provider") or validated["provider"] != route_observation.get("provider") or validated["provider"] != pilot.SUBSCRIPTION_ROUTE_PROVIDER:
        _reject("PROVIDER_MISMATCH", "adapter configuration provider identity does not agree with the authorization and route observation")
    if validated["model_family"] != manifest["route"].get("model") or validated["model_family"] != authorization.get("model"):
        _reject("MODEL_FAMILY_MISMATCH", "adapter configuration model family does not agree with the manifest/authorization")
    if validated["variant"] != authorization.get("variant") or validated["variant"] != route_observation.get("variant"):
        _reject("VARIANT_MISMATCH", "adapter configuration variant does not agree with the authorization and route observation")
    if validated["opencode_version"] != authorization.get("expected_opencode_version") or validated["opencode_version"] != route_observation.get("opencode_version"):
        _reject("OPENCODE_VERSION_MISMATCH", "adapter configuration OpenCode version does not agree with the authorization and route observation")
    if validated["catalog_fingerprint"] != authorization.get("expected_catalog_fingerprint") or validated["catalog_fingerprint"] != route_observation.get("catalog_fingerprint"):
        _reject("CATALOG_FINGERPRINT_MISMATCH", "adapter configuration catalog fingerprint does not agree with the authorization and route observation")
    if validated["runtime_model_id"] != authorization.get("expected_runtime_model_id") or validated["runtime_model_id"] != route_observation.get("runtime_model_id"):
        _reject("RUNTIME_MODEL_ID_MISMATCH", "adapter configuration runtime model identity does not agree with the authorization and route observation")
    if validated["expected_account_status"] != authorization.get("expected_account_status") or validated["expected_account_status"] != route_observation.get("account_status"):
        _reject("ACCOUNT_STATUS_MISMATCH", "adapter configuration account status does not agree with the authorization and route observation")

    command_value = validated["command"]
    pairs = {
        (command_value[index], command_value[index + 1])
        for index in range(len(command_value) - 1)
    }
    if ("--model", validated["runtime_model_id"]) not in pairs or ("--variant", validated["variant"]) not in pairs:
        _reject("RUNTIME_MODEL_ID_MISMATCH", "adapter configuration command must bind --model to the runtime model identity and --variant to the variant")

    for flag in DENIAL_FIELDS:
        if authorization.get(flag) is not True:
            _reject("DENIAL_FLAG_NOT_TRUE", f"authorization denial flag {flag} must be true")
    if authorization.get("no_fallback_required") is not True:
        _reject("FALLBACK_POLICY_MISMATCH", "authorization no_fallback_required must be true")

    budgets = manifest["budgets"]
    if float(validated["per_call_timeout_seconds"]) > float(budgets["per_call_timeout_seconds"]):
        _reject("BUDGET_CONTRADICTION", "adapter per-call timeout exceeds the frozen manifest per-call timeout budget")
    if float(validated["total_case_timeout_seconds"]) > float(budgets["total_case_timeout_seconds"]):
        _reject("BUDGET_CONTRADICTION", "adapter total-case timeout exceeds the frozen manifest total-case timeout budget")
    if validated["max_transport_attempts_per_logical_call"] > budgets["max_transport_attempts_per_logical_call"]:
        _reject("BUDGET_CONTRADICTION", "adapter transport attempts per logical call exceed the frozen manifest budget")
    if validated["transport_retry_limit"] > budgets["max_transport_retries_per_logical_call"]:
        _reject("BUDGET_CONTRADICTION", "adapter transport retry limit exceeds the frozen manifest budget")
    return validated


# ---- runtime identity binding -------------------------------------------------


@dataclass(frozen=True)
class RuntimeIdentityBinding:
    """The exact runtime identity binding derived from validated authority.

    Every field is independently observed from the authorization and route
    observation and must agree with the adapter configuration.
    """

    provider: str
    model_family: str
    variant: str
    protocol: str
    opencode_version: str
    catalog_fingerprint: str
    runtime_model_id: str
    route_class: str
    account_status: str
    entitlement_confirmed: bool
    authorization_hash: str
    execution_commit: str
    denial_flags: tuple[str, ...]
    no_fallback_required: bool

    def fingerprint(self) -> str:
        from quixbugs_live_runner_v2 import canonical_json, sha256_text

        return sha256_text(canonical_json({
            "provider": self.provider,
            "model_family": self.model_family,
            "variant": self.variant,
            "protocol": self.protocol,
            "opencode_version": self.opencode_version,
            "catalog_fingerprint": self.catalog_fingerprint,
            "runtime_model_id": self.runtime_model_id,
            "route_class": self.route_class,
            "account_status": self.account_status,
            "entitlement_confirmed": self.entitlement_confirmed,
            "authorization_hash": self.authorization_hash,
            "execution_commit": self.execution_commit,
        }))


def build_runtime_identity_binding(
    authorization: Mapping[str, Any],
    route_observation: Mapping[str, Any],
    configuration: Mapping[str, Any],
) -> RuntimeIdentityBinding:
    """Derive the runtime identity binding and reject every prohibited state.

    Rejects the historical Zen model identity as the execution identity, model
    alias rewriting, catalog/version/variant/route-class/billing-route drift,
    and any observed Zen/free-tier/Ollama/alternate-provider/fallback state.
    """
    if route_observation.get("preflight_success") is not True:
        _reject("ROUTE_OBSERVATION_NOT_ESTABLISHED", "runtime identity requires a preflight-passed route observation")
    if route_observation.get("billing_route") != pilot.AUTHORIZED_BILLING_ROUTE:
        _reject("BILLING_ROUTE_MISMATCH", f"observed billing route {route_observation.get('billing_route')!r} is not the authorized subscription route")
    if route_observation.get("subscription_entitlement_confirmed") is not True:
        _reject("ROUTE_OBSERVATION_NOT_ESTABLISHED", "subscription entitlement is not confirmed by the route observation")
    if route_observation.get("runtime_model_id") == HISTORICAL_ZEN_MODEL_ID:
        _reject("HISTORICAL_ZEN_IDENTITY", "the route observation established the historical OpenCode Zen free-model identity")
    prohibited = {
        "zen_used": "ZEN_ROUTE_OBSERVED",
        "free_tier_used": "FREE_TIER_SUBSTITUTION",
        "ollama_used": "OLLAMA_ROUTE_OBSERVED",
        "paid_fallback_used": "METERED_FALLBACK_REQUIRED",
        "alternate_provider_used": "ALTERNATE_PROVIDER_REQUIRED",
        "metered_fallback_used": "METERED_FALLBACK_REQUIRED",
        "paid_overage_used": "PAID_OVERAGE_REQUIRED",
        "per_call_billing_used": "PER_CALL_BILLING_FALLBACK",
        "model_substitution_observed": "MODEL_SUBSTITUTION_OBSERVED",
    }
    for field, category in prohibited.items():
        if route_observation.get(field) is True:
            _reject(category, f"route observation records prohibited state {field}=true")
    observed_model = route_observation.get("model")
    if observed_model and observed_model != configuration.get("model_family"):
        _reject("MODEL_FAMILY_MISMATCH", "route observation model family differs from the adapter configuration")
    return RuntimeIdentityBinding(
        provider=str(route_observation["provider"]),
        model_family=str(route_observation["model"]),
        variant=str(route_observation["variant"]),
        protocol=str(route_observation["protocol"]),
        opencode_version=str(route_observation["opencode_version"]),
        catalog_fingerprint=str(route_observation["catalog_fingerprint"]),
        runtime_model_id=str(route_observation["runtime_model_id"]),
        route_class=str(route_observation["billing_route"]),
        account_status=str(route_observation["account_status"]),
        entitlement_confirmed=bool(route_observation["subscription_entitlement_confirmed"]),
        authorization_hash=runner.authorization_hash(authorization),
        execution_commit=str(route_observation.get("execution_commit") or authorization.get("accepted_campaign_commit")),
        denial_flags=tuple(DENIAL_FIELDS),
        no_fallback_required=bool(authorization.get("no_fallback_required")),
    )


def _assert_binding_unchanged(binding: RuntimeIdentityBinding, authorization: Mapping[str, Any], configuration: Mapping[str, Any]) -> None:
    """Revalidate the in-memory binding before a provider process attempt."""
    if runner.authorization_hash(authorization) != binding.authorization_hash:
        raise runner.RouteDriftError("AUTHORIZATION_BINDING_DRIFT", "authorization hash drifted from the runtime identity binding")
    if str(authorization.get("accepted_campaign_commit")) != binding.execution_commit:
        raise runner.RouteDriftError("EXECUTION_COMMIT_DRIFT", "execution commit drifted from the runtime identity binding")
    if configuration.get("runtime_model_id") != binding.runtime_model_id:
        raise runner.RouteDriftError("RUNTIME_MODEL_ID_MISMATCH", "configuration runtime model identity drifted from the binding")
    if configuration.get("opencode_version") != binding.opencode_version:
        raise runner.RouteDriftError("OPENCODE_VERSION_MISMATCH", "configuration OpenCode version drifted from the binding")
    if configuration.get("catalog_fingerprint") != binding.catalog_fingerprint:
        raise runner.RouteDriftError("CATALOG_PREFLIGHT_FAILED", "configuration catalog fingerprint drifted from the binding")
    if configuration.get("variant") != binding.variant:
        raise runner.RouteDriftError("VARIANT_MISMATCH", "configuration variant drifted from the binding")
    if configuration.get("route_class") != binding.route_class:
        raise runner.RouteDriftError("BILLING_ROUTE_MISMATCH", "configuration route class drifted from the binding")
    if configuration.get("provider") != binding.provider:
        raise runner.RouteDriftError("PROVIDER_MISMATCH", "configuration provider drifted from the binding")


def _observed_identity_drift(binding: RuntimeIdentityBinding, observed: Mapping[str, Any]) -> str | None:
    """Return the drift category for independently observed provider identity."""
    if observed.get("observed_model") is not None and str(observed["observed_model"]) != binding.runtime_model_id:
        if str(observed["observed_model"]) == HISTORICAL_ZEN_MODEL_ID:
            return "MODEL_SUBSTITUTION_OBSERVED"
        return "RUNTIME_MODEL_ID_MISMATCH"
    if observed.get("observed_billing_route") is not None:
        observed_route = str(observed["observed_billing_route"])
        if observed_route != binding.route_class:
            if observed_route == "ZEN":
                return "ZEN_ROUTE_OBSERVED"
            if observed_route == "FREE_TIER":
                return "FREE_TIER_SUBSTITUTION"
            if observed_route == "OLLAMA":
                return "OLLAMA_ROUTE_OBSERVED"
            if observed_route == "METERED":
                return "METERED_FALLBACK_REQUIRED"
            if observed_route == "PER_CALL":
                return "PER_CALL_BILLING_FALLBACK"
            return "BILLING_ROUTE_MISMATCH"
    if observed.get("observed_model_substitution") is True:
        return "MODEL_SUBSTITUTION_OBSERVED"
    return None


# ---- transport factory --------------------------------------------------------


class OpenCodeGoTransportFactory:
    """Explicit transport factory adapting the protocol transport to the
    paired-pilot live runner.

    Construction requires the already validated authorization, execution
    commit, route observation, adapter configuration, and the runtime identity
    binding; no process is created at construction.  :meth:`prepare` returns
    one fresh transport per frozen case after the output/attempt ownership
    gates are verified on disk; every provider process attempt revalidates
    the binding.
    """

    def __init__(
        self,
        *,
        authorization: Mapping[str, Any],
        execution_commit: str,
        route_observation: Mapping[str, Any],
        configuration: Mapping[str, Any],
        binding: RuntimeIdentityBinding,
        attempt_identity: str,
        output_root: str | Path,
        ledger_path: str | Path | None = None,
        evidence_dir: str | Path | None = None,
        environment_override: Mapping[str, str] | None = None,
    ) -> None:
        if type(authorization) is not dict:
            raise OpenCodeGoAdapterError("transport factory requires a validated authorization artifact")
        if route_observation is None or route_observation.get("preflight_success") is not True:
            raise OpenCodeGoAdapterError("transport factory requires a preflight-passed route observation")
        if configuration is None or type(configuration) is not dict:
            raise OpenCodeGoAdapterError("transport factory requires a validated adapter configuration")
        if binding is None or not isinstance(binding, RuntimeIdentityBinding):
            raise OpenCodeGoAdapterError("transport factory requires a runtime identity binding")
        if not isinstance(attempt_identity, str) or not attempt_identity:
            raise OpenCodeGoAdapterError("transport factory requires an attempt identity")
        if not runner.ATTEMPT_IDENTITY_PATTERN.fullmatch(attempt_identity):
            raise OpenCodeGoAdapterError("transport factory requires a valid campaign attempt identity")
        if authorization.get("schema_version") != runner.AUTHORIZATION_SCHEMA_VERSION:
            raise OpenCodeGoAdapterError("transport factory requires a validated authorization artifact")
        if authorization.get("template") is not False or authorization.get("authorize_live") is not True:
            raise OpenCodeGoAdapterError("transport factory requires an authorizing (non-template) authorization artifact")
        if not runner.ATTEMPT_IDENTITY_PATTERN.fullmatch(str(authorization.get("campaign_attempt_identity") or "")):
            raise OpenCodeGoAdapterError("transport factory requires a valid campaign attempt identity")
        if execution_commit != authorization.get("accepted_campaign_commit"):
            raise OpenCodeGoAdapterError("transport factory execution commit does not match the authorization-bound commit")
        if runner.authorization_hash(authorization) != configuration["authorization_hash"]:
            raise OpenCodeGoAdapterError("transport factory configuration authorization hash does not match the authorization")
        if configuration["execution_commit"] != execution_commit:
            raise OpenCodeGoAdapterError("transport factory configuration execution commit does not match")
        self.authorization = dict(authorization)
        self.execution_commit = str(execution_commit)
        self.route_observation = dict(route_observation)
        self.configuration = dict(configuration)
        self.binding = binding
        self.attempt_identity = str(attempt_identity)
        self.output_root = Path(output_root).resolve()
        self.ledger_path = Path(ledger_path) if ledger_path is not None else (self.output_root / "ledger.json")
        self.evidence_dir = Path(evidence_dir).resolve() if evidence_dir is not None else (self.output_root / "private")
        self.environment_override = dict(environment_override or {})
        for name in self.environment_override:
            if name not in self.configuration["environment_allowlist"]:
                raise OpenCodeGoAdapterError(
                    f"transport factory environment override {name!r} is not on the adapter environment allowlist"
                )
        self._active: OpenCodeGoTransport | None = None
        self.spawned_processes = 0

    # -- ownership gates ------------------------------------------------------

    def _verify_ownership_gates(self) -> None:
        owner_path = self.output_root / ".attempt-owner"
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OpenCodeGoAdapterError(
                f"output/attempt ownership gate not satisfied: {owner_path}: {exc}"
            ) from exc
        if not isinstance(owner, Mapping):
            raise OpenCodeGoAdapterError("output/attempt ownership gate record is malformed")
        if owner.get("attempt_identity") != self.attempt_identity:
            raise OpenCodeGoAdapterError("output/attempt ownership gate identity mismatch")
        if owner.get("authorization_hash") != self.binding.authorization_hash:
            raise OpenCodeGoAdapterError("output/attempt ownership gate authorization mismatch")
        try:
            ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OpenCodeGoAdapterError(f"attempt ledger gate not satisfied: {exc}") from exc
        entry = next(
            (item for item in ledger.values() if isinstance(item, Mapping) and item.get("attempt_identity") == self.attempt_identity),
            None,
        )
        if entry is None or entry.get("status") != "STARTED":
            raise OpenCodeGoAdapterError("attempt ledger gate not satisfied: no STARTED attempt entry for this identity")

    # -- per-case transport ---------------------------------------------------

    def prepare(self, case: Mapping[str, Any]) -> "OpenCodeGoTransport":
        """One fresh transport/process/session boundary per frozen case."""
        if not isinstance(case, Mapping) or not case.get("case_id"):
            raise OpenCodeGoAdapterError("transport factory requires a frozen case record")
        self._verify_ownership_gates()
        transport = OpenCodeGoTransport(
            factory=self,
            case_id=str(case["case_id"]),
            command=list(self.configuration["command"]),
            working_directory=Path(self.configuration["working_directory"]),
            environment_allowlist=list(self.configuration["environment_allowlist"]),
            max_stdout_bytes=int(self.configuration["max_stdout_bytes"]),
            max_stderr_bytes=int(self.configuration["max_stderr_bytes"]),
            max_diagnostic_bytes=int(self.configuration["max_diagnostic_bytes"]),
            per_call_timeout_seconds=float(self.configuration["per_call_timeout_seconds"]),
            environment_override=self.environment_override,
        )
        self._active = transport
        return transport

    @property
    def active_transport(self) -> "OpenCodeGoTransport | None":
        return self._active


class OpenCodeGoTransport:
    """One provider-process transport bound to one frozen case.

    Implements the accepted :class:`agentic_debugger.evaluation.live.ModelTransport`
    contract (``request(payload, timeout_seconds) -> Mapping``).  Every
    request revalidates the runtime binding before the provider process
    attempt, spawns the structured argv with an explicit working directory
    and a bounded environment allowlist, captures bounded stdout/stderr, and
    fails closed on timeout, non-zero exit, malformed JSON, non-finite
    provider metadata, or observed identity/route drift.
    """

    def __init__(
        self,
        *,
        factory: OpenCodeGoTransportFactory,
        case_id: str,
        command: list[str],
        working_directory: Path,
        environment_allowlist: list[str],
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        max_diagnostic_bytes: int,
        per_call_timeout_seconds: float,
        environment_override: Mapping[str, str] | None = None,
    ) -> None:
        self._factory = factory
        self.case_id = case_id
        self.command = list(command)
        self.working_directory = working_directory
        self.environment_allowlist = list(environment_allowlist)
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_diagnostic_bytes = max_diagnostic_bytes
        self.per_call_timeout_seconds = per_call_timeout_seconds
        self.environment_override = dict(environment_override or {})
        self.process_attempts = 0
        self.observed_identity: list[dict[str, Any]] = []
        self.observed_usage: list[dict[str, Any]] = []
        self.reported_costs: list[float] = []
        self.drift_category: str | None = None
        self.last_process_exit_code: int | None = None
        self.last_timed_out = False
        self.last_provider_error_category: str | None = None

    # -- evidence -------------------------------------------------------------

    def _evidence_path(self, kind: str) -> Path:
        return self._factory.evidence_dir / f"opencode-go-transport-{self.case_id.replace(':', '__')}-{kind}.jsonl"

    def _record(self, kind: str, record: Mapping[str, Any]) -> None:
        _append_evidence(self._evidence_path(kind), record)

    def _environment(self) -> dict[str, str]:
        environment: dict[str, str] = {}
        for name in self.environment_allowlist:
            value = os.environ.get(name)
            if value is not None:
                environment[name] = value
        for name, value in self.environment_override.items():
            if name not in self.environment_allowlist:
                raise OpenCodeGoAdapterError(
                    f"environment override {name!r} is not on the adapter environment allowlist"
                )
            if value is not None:
                environment[name] = value
        return environment

    def reported_cost_aggregate(self) -> float | None:
        """Aggregate of explicitly reported finite monetary costs.

        Only provider responses that explicitly report a finite monetary
        cost contribute; absent cost metadata stays absent (returns None),
        an explicitly reported zero stays zero, and subscription access
        never implies zero.
        """
        if not self.reported_costs:
            return None
        return round(sum(self.reported_costs), 6)

    # -- binding revalidation (before every provider process attempt) ---------

    def _revalidate(self) -> None:
        _assert_binding_unchanged(
            self._factory.binding,
            self._factory.authorization,
            self._factory.configuration,
        )
        self._factory._verify_ownership_gates()

    # -- provider process attempt ---------------------------------------------

    def request(self, payload: Mapping[str, Any], timeout_seconds: float) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            raise _transport_error("invalid_provider_request", "provider request payload must be an object")
        self._revalidate()
        bounded_timeout = min(float(timeout_seconds), self.per_call_timeout_seconds)
        if not math.isfinite(bounded_timeout) or bounded_timeout <= 0:
            raise _transport_error("invalid_timeout", "provider request timeout is invalid")
        request_bytes: bytes
        try:
            request_bytes = (json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise _transport_error("request_serialization", "provider request could not be serialized") from None
        self.process_attempts += 1
        self._factory.spawned_processes += 1
        evidence_dir = self._factory.evidence_dir
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_file = evidence_dir / f"opencode-go-transport-{self.case_id.replace(':', '__')}-provider-{self.process_attempts:03d}.jsonl"
        command = list(self.command)
        if command[-2:] != ["--evidence-file", command[-1]]:
            command = command + ["--evidence-file", str(evidence_file)]
        environment = self._environment()
        started = time.monotonic()
        stdout = _BoundedCapture(self.max_stdout_bytes)
        stderr = _BoundedCapture(self.max_stderr_bytes)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.working_directory),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
            )
        except (OSError, ValueError) as exc:
            self.last_provider_error_category = "launch_error"
            self._record("attempt", {
                "event": "provider_process_launch_failure",
                "case_id": self.case_id,
                "attempt": self.process_attempts,
                "error": f"{type(exc).__name__}: {exc}",
                "spawned": False,
            })
            raise _transport_error("launch_error", "provider process could not be launched") from None
        out_threads = [
            threading.Thread(target=_read_pipe, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=_read_pipe, args=(process.stderr, stderr), daemon=True),
        ]
        for thread in out_threads:
            thread.start()
        write_error: list[Exception] = []

        def write_request() -> None:
            try:
                assert process.stdin is not None
                process.stdin.write(request_bytes)
                process.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                write_error.append(exc)

        writer = threading.Thread(target=write_request, daemon=True)
        writer.start()
        deadline = started + bounded_timeout
        writer.join(timeout=max(0.0, deadline - time.monotonic()))
        if writer.is_alive():
            _terminate_process_tree(process)
            for thread in out_threads:
                thread.join(timeout=2)
            self.last_timed_out = True
            self.last_provider_error_category = "TIMEOUT"
            self._record("attempt", {
                "event": "provider_timeout", "case_id": self.case_id, "attempt": self.process_attempts,
                "timed_out": True, "provider_exit_code": process.returncode,
            })
            raise _transport_error("request_timeout", "provider request stdin write timed out", timed_out=True) from None
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            for thread in out_threads:
                thread.join(timeout=2)
            self.last_timed_out = True
            self.last_provider_error_category = "TIMEOUT"
            self._record("attempt", {
                "event": "provider_timeout", "case_id": self.case_id, "attempt": self.process_attempts,
                "timed_out": True, "provider_exit_code": process.returncode,
            })
            raise _transport_error("request_timeout", "provider request timed out", timed_out=True) from None
        for thread in out_threads:
            thread.join(timeout=2)
        elapsed = time.monotonic() - started
        self.last_process_exit_code = process.returncode
        raw_stdout = stdout.text()
        raw_stderr = stderr.text()
        diagnostics = self._bounded_diagnostics(raw_stdout, raw_stderr)
        if stdout.truncated or stderr.truncated:
            self.last_provider_error_category = "response_too_large"
            self._record("attempt", {
                "event": "provider_output_too_large", "case_id": self.case_id, "attempt": self.process_attempts,
                "stdout_truncated": stdout.truncated, "stderr_truncated": stderr.truncated,
                "provider_exit_code": process.returncode,
            })
            raise _transport_error("response_too_large", "provider output exceeded the configured bound")
        if process.returncode != 0:
            self.last_provider_error_category = "process_error"
            self._record("attempt", {
                "event": "provider_exit_failure", "case_id": self.case_id, "attempt": self.process_attempts,
                "provider_exit_code": process.returncode, "provider_stderr": diagnostics["provider_stderr"],
            })
            raise _transport_error("process_error", "provider process exited nonzero") from None
        response = self._parse_response(raw_stdout, raw_stderr, elapsed)
        self._record("attempt", {
            "event": "provider_response",
            "case_id": self.case_id,
            "attempt": self.process_attempts,
            "provider_exit_code": process.returncode,
            "elapsed_seconds": round(elapsed, 6),
            "observed_identity": self.observed_identity[-1] if self.observed_identity else None,
            "token_usage": self.observed_usage[-1] if self.observed_usage else None,
            "reported_cost": self.reported_costs[-1] if self.reported_costs else None,
            "cost_observed": bool(self.reported_costs),
        })
        return response

    def _bounded_diagnostics(self, raw_stdout: str, raw_stderr: str) -> dict[str, Any]:
        return {
            "provider_stdout": raw_stdout[: self.max_diagnostic_bytes],
            "provider_stderr": raw_stderr[: self.max_diagnostic_bytes],
            "stdout_truncated_for_diagnostics": len(raw_stdout) > self.max_diagnostic_bytes,
            "stderr_truncated_for_diagnostics": len(raw_stderr) > self.max_diagnostic_bytes,
        }

    def _parse_response(self, raw_stdout: str, raw_stderr: str, elapsed: float) -> Mapping[str, Any]:
        def _reject_constant(value: str) -> Any:
            raise ValueError(f"non-finite JSON constant: {value}")

        try:
            parsed = json.loads(raw_stdout, parse_constant=_reject_constant)
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            self.last_provider_error_category = "non_finite_metadata" if "non-finite" in str(exc) else "invalid_response"
            self._record("attempt", {
                "event": "provider_invalid_json", "case_id": self.case_id, "attempt": self.process_attempts,
                "error": f"{type(exc).__name__}: {exc}",
                "provider_stderr": self._bounded_diagnostics(raw_stdout, raw_stderr)["provider_stderr"],
            })
            raise _transport_error("invalid_response", "provider response was not strict finite JSON") from None
        if not isinstance(parsed, Mapping):
            self.last_provider_error_category = "invalid_response"
            raise _transport_error("invalid_response", "provider response was not an object")
        observed: dict[str, Any] = {}
        telemetry = parsed.get("provider_telemetry")
        if isinstance(telemetry, Mapping):
            for key in ("observed_model", "observed_billing_route", "observed_model_substitution"):
                if key in telemetry:
                    observed[key] = telemetry[key]
        self.observed_identity.append(dict(observed))
        drift = _observed_identity_drift(self._factory.binding, observed)
        if drift is not None:
            self.drift_category = drift
            self._record("attempt", {
                "event": "route_drift_observed", "case_id": self.case_id, "attempt": self.process_attempts,
                "drift_category": drift, "observed": observed,
            })
            raise runner.RouteDriftError(drift, f"provider output reported an identity outside the binding: {drift}")
        usage = parsed.get("usage")
        if isinstance(usage, Mapping):
            self._validate_usage(usage)
            self.observed_usage.append(dict(usage))
        telemetry_cost = telemetry.get("cost") if isinstance(telemetry, Mapping) else None
        if telemetry_cost is not None:
            if isinstance(telemetry_cost, (int, float)) and not isinstance(telemetry_cost, bool):
                if not math.isfinite(float(telemetry_cost)):
                    self.last_provider_error_category = "non_finite_metadata"
                    raise _transport_error("invalid_response", "provider cost metadata is non-finite")
                self.reported_costs.append(float(telemetry_cost))
            else:
                self.last_provider_error_category = "invalid_response"
                raise _transport_error("invalid_response", "provider cost metadata is malformed")
        return parsed

    def _validate_usage(self, usage: Mapping[str, Any]) -> None:
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if field in usage:
                number = usage[field]
                if type(number) is not int or number < 0:
                    self.last_provider_error_category = "invalid_response"
                    raise _transport_error("invalid_response", f"provider usage field {field} is malformed")


def _transport_error(kind: str, message: str, timed_out: bool = False):
    from agentic_debugger.evaluation.live import LiveTransportError

    return LiveTransportError(message, kind=kind, timed_out=timed_out)


# ---- case-runner binding -----------------------------------------------------


@dataclass(frozen=True)
class QuixBugsCaseEnvironment:
    """Explicit operator-supplied QuixBugs execution environment for the
    case-runner binding.  Nothing here may be defaulted.

    ``facts_provider`` is the task-bound facts contract:
    ``provide(manifest_path: str) -> QuixBugsPreflightFacts``.  The case
    runner calls it separately for every frozen case with the exact task
    manifest path, requires an exact ``QuixBugsPreflightFacts`` result, and
    rejects zero-argument generic providers, wrong-task facts, and malformed
    results before any provider interaction.
    """

    repository_root: str
    sources_parent: str
    facts_provider: Callable[[str], Any]
    manifest_path: str | None = None


@dataclass(frozen=True)
class OpenCodeGoCaseRunner:
    """Connects the six-case live runner to the accepted QuixBugs live path.

    ``live_executor`` defaults to the accepted
    :func:`agentic_debugger.evaluation.live_quixbugs.run_live_quixbugs_case`
    and is injectable for deterministic tests.  One fresh transport/session
    boundary per case comes from the transport factory; the runner owns the
    frozen case order, ledger, terminal commitment, authority checks, stop
    rules, and result validation.

    Task binding: every frozen case resolves its exact inventory entry; a
    ``pdb-on-uncertainty`` case receives the task-local
    :class:`agentic_debugger.demo.catalog.RuntimeProbe` built from that
    entry's frozen, reviewed ``runtime_probe`` fields (never from corrected
    source, tests, model output, or runtime guesses); missing, malformed,
    mismatched, or duplicate probe metadata is rejected before any provider
    interaction.  Facts are requested separately per case through the
    task-bound provider contract
    ``provide(manifest_path: str) -> QuixBugsPreflightFacts`` and must be an
    exact ``QuixBugsPreflightFacts`` whose dependency preparation matches the
    selected task manifest.
    """

    binding: RuntimeIdentityBinding
    configuration: Mapping[str, Any]
    factory: OpenCodeGoTransportFactory
    environment: QuixBugsCaseEnvironment
    manifest: Mapping[str, Any]
    live_executor: Callable[..., Any] | None = None
    policy_resolver: Callable[[str], Any] | None = None

    def __post_init__(self) -> None:
        if self.environment is None:
            raise OpenCodeGoAdapterError("case runner requires an explicit QuixBugs case environment")
        for field in ("repository_root", "sources_parent"):
            if not isinstance(getattr(self.environment, field), str) or not getattr(self.environment, field):
                raise OpenCodeGoAdapterError(f"case runner environment {field} is missing")
        if self.environment.manifest_path is not None and (
            not isinstance(self.environment.manifest_path, str) or not self.environment.manifest_path
        ):
            raise OpenCodeGoAdapterError("case runner environment manifest_path is invalid")
        self._validate_frozen_case_bindings()

    def _inventory_entry_for(self, task_id: str) -> Mapping[str, Any]:
        """Resolve the exact inventory entry for a frozen task ID.

        Exactly one entry must exist; a missing or duplicated entry is
        rejected before any provider interaction.
        """
        entries = [
            item for item in self.manifest.get("inventory", [])
            if isinstance(item, Mapping) and item.get("task_id") == task_id
        ]
        if not entries:
            raise OpenCodeGoAdapterError(f"case runner cannot resolve the frozen inventory entry for task {task_id!r}")
        if len(entries) > 1:
            raise OpenCodeGoAdapterError(f"case runner found duplicate inventory entries for task {task_id!r}")
        return entries[0]

    def _manifest_path_for(self, case: Mapping[str, Any]) -> str:
        if self.environment.manifest_path is not None:
            return self.environment.manifest_path
        entry = self._inventory_entry_for(str(case.get("task_id")))
        if not isinstance(entry.get("manifest_path"), str) or not entry["manifest_path"]:
            raise runner.LiveRunnerError(f"case runner cannot resolve the task manifest for case {case.get('case_id')}")
        return str((REPO_ROOT / entry["manifest_path"]).resolve())

    def _runtime_probe_from_inventory(self, entry: Mapping[str, Any], task_manifest: Mapping[str, Any]) -> Any:
        """Build the task-local ``RuntimeProbe`` from the frozen inventory
        entry's reviewed ``runtime_probe`` fields.

        The probe is never derived from corrected source, tests, model
        output, or runtime guesses: it comes exclusively from the frozen
        reviewed metadata, and the metadata itself is rejected when missing,
        malformed, or mismatched against the selected task manifest.
        """
        from agentic_debugger.demo.catalog import RuntimeProbe

        probe_value = entry.get("runtime_probe")
        if not isinstance(probe_value, Mapping) or not probe_value:
            raise OpenCodeGoAdapterError(
                f"inventory entry for {entry.get('task_id')} carries no frozen runtime_probe metadata"
            )
        expected_fields = frozenset({"module_path", "focus_function", "call_expression", "breakpoint_anchor", "inspect_names"})
        unknown = set(probe_value) - expected_fields
        if unknown:
            raise OpenCodeGoAdapterError(
                f"inventory runtime_probe for {entry.get('task_id')} carries unknown fields: {sorted(unknown)}"
            )
        missing = expected_fields - set(probe_value)
        if missing:
            raise OpenCodeGoAdapterError(
                f"inventory runtime_probe for {entry.get('task_id')} is missing fields: {sorted(missing)}"
            )
        module_path = probe_value["module_path"]
        focus_function = probe_value["focus_function"]
        call_expression = probe_value["call_expression"]
        breakpoint_anchor = probe_value["breakpoint_anchor"]
        inspect_names = probe_value["inspect_names"]
        for name, value in (
            ("module_path", module_path),
            ("focus_function", focus_function),
            ("call_expression", call_expression),
            ("breakpoint_anchor", breakpoint_anchor),
        ):
            if type(value) is not str or not value:
                raise OpenCodeGoAdapterError(
                    f"inventory runtime_probe {name} for {entry.get('task_id')} is malformed"
                )
        if not isinstance(inspect_names, list) or not inspect_names or not all(type(name) is str and name for name in inspect_names):
            raise OpenCodeGoAdapterError(
                f"inventory runtime_probe inspect_names for {entry.get('task_id')} is malformed"
            )
        if len(set(inspect_names)) != len(inspect_names):
            raise OpenCodeGoAdapterError(
                f"inventory runtime_probe inspect_names for {entry.get('task_id')} must be unique"
            )
        implementation_path = entry.get("implementation_path")
        if type(implementation_path) is not str or module_path != implementation_path:
            raise OpenCodeGoAdapterError(
                f"inventory runtime_probe module for {entry.get('task_id')} does not match its implementation_path"
            )
        target = task_manifest.get("target") if isinstance(task_manifest, Mapping) else None
        oracle = task_manifest.get("oracle") if isinstance(task_manifest, Mapping) else None
        if not isinstance(target, Mapping) or module_path != target.get("buggy_path"):
            raise OpenCodeGoAdapterError(
                f"inventory runtime_probe module for {entry.get('task_id')} does not match the frozen task manifest buggy path"
            )
        if module_path == target.get("corrected_path") or module_path == target.get("pytest_path") or module_path in target.get("support_paths", []):
            raise OpenCodeGoAdapterError(
                f"inventory runtime_probe for {entry.get('task_id')} points to corrected, test, or support material"
            )
        if not isinstance(oracle, Mapping) or not isinstance(oracle.get("target_symbols"), list) or focus_function not in oracle["target_symbols"]:
            raise OpenCodeGoAdapterError(
                f"inventory runtime_probe focus for {entry.get('task_id')} is not a reviewed target symbol"
            )
        try:
            return RuntimeProbe(
                module_path=module_path,
                focus_function=focus_function,
                call_source=call_expression,
                anchor=breakpoint_anchor,
                inspect_expressions=tuple(inspect_names),
            )
        except Exception as exc:
            raise OpenCodeGoAdapterError(
                f"inventory runtime_probe for {entry.get('task_id')} is not a valid RuntimeProbe: {type(exc).__name__}: {exc}"
            ) from exc

    def _validate_frozen_case_bindings(self) -> None:
        """Reject missing, malformed, mismatched, or duplicate probe metadata
        and unresolvable task manifests for all six frozen cases before any
        provider interaction (case-runner construction time)."""
        for case in self.manifest.get("case_order", []):
            if not isinstance(case, Mapping) or not case.get("case_id") or not case.get("task_id"):
                raise OpenCodeGoAdapterError("case runner found a malformed frozen case record")
            entry = self._inventory_entry_for(str(case["task_id"]))
            manifest_path = str((REPO_ROOT / entry["manifest_path"]).resolve()) if isinstance(entry.get("manifest_path"), str) and entry["manifest_path"] else None
            if manifest_path is None:
                raise OpenCodeGoAdapterError(
                    f"case runner cannot resolve the task manifest for {case['task_id']}"
                )
            if not Path(manifest_path).is_file():
                raise OpenCodeGoAdapterError(
                    f"case runner task manifest is missing: {manifest_path}"
                )
            task_manifest = pilot.load_manifest(Path(manifest_path))
            if str(case["policy"]) == "pdb-on-uncertainty":
                self._runtime_probe_from_inventory(entry, task_manifest)

    def _task_bound_facts(self, provider: Callable[[str], Any], manifest_path: str, task_id: str) -> Any:
        """Request facts for the exact task manifest and reject wrong-task,
        generic, or malformed facts before any provider interaction."""
        from agentic_debugger.quixbugs.adapter import QuixBugsAdapter, QuixBugsPreflightFacts

        try:
            value = provider(manifest_path)
        except TypeError as exc:
            raise OpenCodeGoAdapterError(
                f"facts provider rejected the exact manifest-path argument; the task-bound "
                f"provide(manifest_path: str) -> QuixBugsPreflightFacts contract is required: {exc}"
            ) from exc
        except Exception as exc:
            raise OpenCodeGoAdapterError(
                f"facts provider failed for {manifest_path}: {type(exc).__name__}: {exc}"
            ) from exc
        if type(value) is not QuixBugsPreflightFacts:
            raise OpenCodeGoAdapterError(
                "facts provider must return exactly QuixBugsPreflightFacts"
            )
        context = value.execution_context
        if context is None:
            raise OpenCodeGoAdapterError(
                "facts are not task-bound: no verified execution context was supplied"
            )
        dependencies = getattr(getattr(context, "environment", None), "dependencies", None)
        if dependencies is None:
            raise OpenCodeGoAdapterError(
                "facts are not task-bound: dependency preparation is missing"
            )
        adapter = QuixBugsAdapter.from_manifest(manifest_path)
        mismatches: list[str] = []
        if str(getattr(dependencies, "pilot_task_id", "")) != task_id:
            mismatches.append(f"pilot_task_id {getattr(dependencies, 'pilot_task_id', None)!r} != {task_id!r}")
        if str(getattr(dependencies, "pilot_task_id", "")) != adapter.manifest.task_id:
            mismatches.append("pilot_task_id does not match the selected task manifest task_id")
        if str(getattr(dependencies, "manifest_fingerprint", "")) != adapter.manifest.fingerprint:
            mismatches.append("manifest_fingerprint does not match the selected task manifest")
        if str(getattr(dependencies, "authority_revision", "")) != adapter.manifest.authority_revision:
            mismatches.append("authority_revision does not match the selected task manifest")
        if str(getattr(dependencies, "bug_id", "")) != adapter.manifest.algorithm:
            mismatches.append("bug_id does not match the selected task algorithm")
        if mismatches:
            raise OpenCodeGoAdapterError(
                "facts dependency preparation does not match the selected task manifest: " + "; ".join(mismatches)
            )
        return value

    def _resolved_policy(self, policy_value: str) -> Any:
        if self.policy_resolver is not None:
            return self.policy_resolver(policy_value)
        from agentic_debugger.demo.policies import DemoPolicy, policy_from_value

        return policy_from_value(policy_value)

    def __call__(
        self,
        case: Mapping[str, Any],
        *,
        attempt_identity: str,
        run_id: str,
        session_id: str,
        transport: Any,
        route_observation: Mapping[str, Any],
        budgets: Mapping[str, Any],
        clock: Callable[[], float] | None = None,
    ) -> dict[str, Any]:
        if self.factory.active_transport is None:
            raise runner.LiveRunnerError("case runner requires the per-case transport prepared by the transport factory")
        inner = self.factory.active_transport
        policy_value = str(case["policy"])
        policy = self._resolved_policy(policy_value)
        if policy_value == "static-baseline":
            pdb_binding = None
        else:
            pdb_binding = (
                self.binding.provider,
                self.binding.runtime_model_id,
                self.binding.variant,
            )
        from agentic_debugger.evaluation.live import LiveModelConfig, LiveRunLimits

        config = LiveModelConfig(
            model_name=self.binding.runtime_model_id,
            command=tuple(self.configuration["command"]),
            request_timeout_seconds=float(self.configuration["per_call_timeout_seconds"]),
            tool_version="opencode-go-execution-adapter-v1",
        )
        limits = LiveRunLimits(
            max_model_requests=int(budgets["max_logical_model_calls"]),
            max_controller_steps=int(budgets["max_logical_model_calls"]),
            max_model_phase_seconds=int(self.configuration["total_case_timeout_seconds"]),
            max_retries=int(self.configuration["transport_retry_limit"]),
            max_response_bytes=int(self.configuration["max_stdout_bytes"]),
            continue_on_task_failure=True,
        )
        executor = self.live_executor
        if executor is None:
            from agentic_debugger.evaluation.live_quixbugs import run_live_quixbugs_case

            executor = run_live_quixbugs_case
        manifest_path = self._manifest_path_for(case)
        task_id = str(case["task_id"])
        entry = self._inventory_entry_for(task_id)
        task_manifest = pilot.load_manifest(Path(manifest_path))
        runtime_probe = None
        if policy_value == "pdb-on-uncertainty":
            runtime_probe = self._runtime_probe_from_inventory(entry, task_manifest)
        facts = self._task_bound_facts(self.environment.facts_provider, manifest_path, task_id)
        executor_kwargs: dict[str, Any] = dict(
            repository_root=self.environment.repository_root,
            manifest_path=manifest_path,
            sources_parent=self.environment.sources_parent,
            facts=facts,
            config=config,
            limits=limits,
            transport=transport,
            evaluation_id=attempt_identity,
            repetition=1,
            policy=policy,
            pdb_identity_binding=pdb_binding,
        )
        if policy_value == "pdb-on-uncertainty":
            executor_kwargs["runtime_probe"] = runtime_probe
        live_result = executor(**executor_kwargs)
        if getattr(inner, "drift_category", None) is not None:
            raise runner.RouteDriftError(inner.drift_category, f"route drift observed during case execution: {inner.drift_category}")
        source_hash = str(entry["source_sha256"]) if isinstance(entry.get("source_sha256"), str) else None
        return _outcome_from_live_case(
            case, live_result, route_observation, inner,
            transport_attempts=inner.process_attempts,
            policy_value=policy_value,
            run_id=run_id,
            source_hash=source_hash,
        )


def _count_from_events(events_jsonl: str, predicate: Callable[[dict[str, Any]], bool]) -> int:
    count = 0
    for line in events_jsonl.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and predicate(event):
            count += 1
    return count


def _events_iter(events_jsonl: str):
    for line in events_jsonl.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _outcome_from_live_case(
    case: Mapping[str, Any],
    live_result: Any,
    route_observation: Mapping[str, Any],
    inner_transport: OpenCodeGoTransport,
    *,
    transport_attempts: int,
    policy_value: str,
    run_id: str,
    source_hash: str | None,
) -> dict[str, Any]:
    """Map the accepted LiveCaseResult into the frozen runner outcome contract."""
    from agentic_debugger.evaluation.live import LiveCaseStatus

    mapping = live_result.to_mapping() if hasattr(live_result, "to_mapping") else dict(live_result)
    status = str(mapping.get("status"))
    measurements = mapping.get("measurements") or {}
    token_usage = measurements.get("token_usage") or {}
    reporting = mapping.get("reporting") or {}
    controller = mapping.get("controller") or {}
    verifier = mapping.get("verifier") or {}
    evidence = mapping.get("evidence") if isinstance(mapping.get("evidence"), Mapping) else {}
    events_jsonl = str(mapping.get("events_jsonl") or "")
    termination_reason = measurements.get("termination_reason")

    model_requests = int(measurements.get("model_request_count", 0) or 0)
    model_responses = int(measurements.get("model_response_count", 0) or 0)
    retries = int(measurements.get("retry_count", 0) or 0)
    directive_rejections = evidence.get("directive_rejections")
    if not isinstance(directive_rejections, list):
        directive_rejections = []
    malformed = len(directive_rejections)
    valid_directives = max(0, model_responses - malformed)

    gate_decisions = evidence.get("pdb_gate_decisions")
    if not isinstance(gate_decisions, list):
        gate_decisions = []
    allowed_openings = sum(1 for decision in gate_decisions if isinstance(decision, Mapping) and decision.get("allowed") is True)
    rejected_decisions = len(gate_decisions) - allowed_openings
    sessions_started = _count_from_events(
        events_jsonl,
        lambda event: event.get("event_type") == "action" and event.get("name") == "start_pdb_session",
    )
    successful_observations = int(measurements.get("successful_pdb_observation_count", 0) or 0)
    failed_observations = int(measurements.get("failed_pdb_observation_count", 0) or 0)
    hypotheses_created = _count_from_events(
        events_jsonl,
        lambda event: isinstance(event.get("payload"), Mapping)
        and event.get("event_type") == "decision"
        and event.get("payload", {}).get("directive_kind") == "add_hypothesis",
    )
    verifier_runs = 1 if verifier.get("executed") is True else 0
    patch_submissions = 1 if (verifier.get("patch_application") is not None and verifier.get("executed") is True) else 0
    baseline_reproduction = False
    if _count_from_events(
        events_jsonl,
        lambda event: isinstance(event.get("payload"), Mapping)
        and event.get("event_type") == "observation"
        and event.get("name") == "run_reproduction"
        and event.get("payload", {}).get("observation", {}).get("payload", {}).get("phase") == "baseline"
        and event.get("payload", {}).get("observation", {}).get("payload", {}).get("failure_reproduced") is True,
    ):
        baseline_reproduction = True
    states_visited: list[str] = []
    for event in _events_iter(events_jsonl):
        state = event.get("state")
        if isinstance(state, str) and state not in states_visited:
            states_visited.append(state)

    prompt_tokens = token_usage.get("prompt_tokens") if isinstance(token_usage.get("prompt_tokens"), int) else 0
    completion_tokens = token_usage.get("completion_tokens") if isinstance(token_usage.get("completion_tokens"), int) else 0
    reasoning_tokens = 0
    reported_cost_aggregate = inner_transport.reported_cost_aggregate()
    provider_reported_cost_observed = reported_cost_aggregate is not None
    # The case execution cost is the aggregate of the finite monetary costs
    # explicitly reported by each provider response.  Absent cost metadata is
    # never replaced with a fabricated value (zero is the frozen schema's
    # absence representation and is never claimed as a reported zero); an
    # explicitly reported zero stays zero; subscription access never implies
    # zero.  The preflight route-observation cost is not used as the case
    # execution cost.
    provider_reported_cost = reported_cost_aggregate if reported_cost_aggregate is not None else 0.0
    wall_clock = float(measurements.get("case_elapsed_duration_ms", 0) or 0) / 1000.0
    public_evidence_bytes = len(events_jsonl.encode("utf-8"))

    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))

    terminal_status, terminal_reason, terminal_evidence = _terminal_mapping(
        status, termination_reason, gate_decisions, inner_transport, model_requests, events_jsonl,
    )
    aggregate = _aggregate_transport_evidence(terminal_status)
    infrastructure = _infrastructure_evidence(terminal_status, status, model_requests, reporting, run_id)
    blocked = {
        "block_kind": "none",
        "reason_code": "NONE",
        "confirmed": False,
        "evidence_reference": f"{run_id}:none",
    }
    candidate_hash = None
    patch_application = verifier.get("patch_application")
    if patch_submissions and patch_application is not None:
        candidate_hash = hashlib.sha256(pilot.canonical_json(patch_application).encode("utf-8")).hexdigest()
    public_request_hash = hashlib.sha256(
        pilot.canonical_json({"run_id": run_id, "events": events_jsonl}).encode("utf-8")
    ).hexdigest()

    return {
        "terminal_status": terminal_status,
        "terminal_reason_code": terminal_reason,
        "termination_reason": f"opencode-go adapter: {str(mapping.get('status'))}: {termination_reason or 'completed'}",
        "logical_model_calls": max(0, model_requests - retries),
        "provider_process_attempts": transport_attempts,
        "retries": retries,
        "valid_directives": valid_directives,
        "malformed_directive_rejections": malformed,
        "bounded_directive_feedback_events": malformed,
        "baseline_reproduction": baseline_reproduction,
        "controller_states_visited": states_visited,
        "hypotheses_created": hypotheses_created,
        "pdb_gate_decisions": list(gate_decisions),
        "pdb_counts": {
            "total_gate_decisions": len(gate_decisions),
            "allowed_gate_openings": allowed_openings,
            "rejected_gate_decisions": rejected_decisions,
            "sessions_started": sessions_started,
            "successful_observations": successful_observations,
            "failed_observations": failed_observations,
        },
        "pdb_sessions_started": sessions_started,
        "successful_pdb_observations": successful_observations,
        "failed_pdb_observations": failed_observations,
        "verifier_runs": verifier_runs,
        "patch_submissions": patch_submissions,
        "independent_verifier_result": {
            "status": verifier.get("status"),
            "outcome": verifier.get("outcome"),
            "lifecycle_succeeded": verifier.get("executed") is True,
        },
        "transport_evidence": aggregate,
        "terminal_transport_evidence": terminal_evidence,
        "blocked_evidence": blocked,
        "infrastructure_evidence": infrastructure,
        "preflight_failure_evidence": {field: None for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS},
        "campaign_stop_evidence": {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS},
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "provider_reported_cost": provider_reported_cost,
        "provider_reported_cost_observed": provider_reported_cost_observed,
        "provider_cost_report_count": len(inner_transport.reported_costs),
        "wall_clock_duration_seconds": wall_clock,
        "public_evidence_bytes": public_evidence_bytes,
        "canonical_source_restoration": True,
        "owned_workspace_cleanup": reporting.get("cleanup") == "cleaned",
        "evidence_consistency": True,
        "public_request_hash": public_request_hash,
        "source_hash": source_hash,
        "candidate_hash": candidate_hash,
        "repair_outcome": "RESOLVED" if terminal_status == "RESOLVED" else ("NO_CANDIDATE"),
        "resource_ids": {},
    }


def _terminal_mapping(
    status: str,
    termination_reason: str | None,
    gate_decisions: list[Any],
    inner_transport: OpenCodeGoTransport,
    model_requests: int,
    events_jsonl: str,
) -> tuple[str, str, dict[str, Any]]:
    """Map the accepted LiveCaseStatus to the frozen terminal contract."""
    reference = f"opencode-go:{inner_transport.case_id}:{inner_transport.process_attempts}"
    completed_response: dict[str, Any] = {
        "final_attempt_classification": "COMPLETED_RESPONSE",
        "process_exit_code": 0,
        "timed_out": False,
        "provider_error_category": None,
        "provider_completed_response": True,
        "evidence_reference": reference,
    }
    if status == "RESOLVED":
        return "RESOLVED", "RESOLVED_COMPLETED", completed_response
    if status == "UNRESOLVED":
        return "UNRESOLVED", "UNRESOLVED_COMPLETED", completed_response
    if status == "PDB_NOT_REACHED":
        if not gate_decisions:
            reason = "PDB_NOT_REACHED_NO_GATE"
        else:
            reason = "PDB_NOT_REACHED_GATE_REJECTED"
        return "PDB_NOT_REACHED", reason, completed_response
    if status == "INVALID_MODEL_RESPONSE" or termination_reason == "invalid_model_response":
        return "INVALID_MODEL_RESPONSE", "MALFORMED_RESPONSE", {
            "final_attempt_classification": "MALFORMED_RESPONSE",
            "process_exit_code": 0,
            "timed_out": False,
            "provider_error_category": None,
            "provider_completed_response": True,
            "evidence_reference": reference,
        }
    if status in {"TIMED_OUT", "PROVIDER_ERROR"} or termination_reason in {"request_timeout", "elapsed_time_limit", "provider_or_transport_error"}:
        timed_out = inner_transport.last_timed_out or termination_reason in {"request_timeout", "elapsed_time_limit"}
        category = "TIMEOUT" if timed_out else (inner_transport.last_provider_error_category or "PROVIDER_ERROR")
        return "PROVIDER_ERROR", category, {
            "final_attempt_classification": "TIMEOUT" if timed_out else "PROVIDER_ERROR",
            "process_exit_code": inner_transport.last_process_exit_code if inner_transport.last_process_exit_code is not None and not timed_out else None,
            "timed_out": timed_out,
            "provider_error_category": category if not timed_out else "TIMEOUT",
            "provider_completed_response": False,
            "evidence_reference": reference,
        }
    # All remaining statuses are infrastructure failures mapped below.
    return "INFRASTRUCTURE_ERROR", "INFRASTRUCTURE_FAILURE", {
        "final_attempt_classification": "INFRASTRUCTURE_FAILURE",
        "process_exit_code": None,
        "timed_out": False,
        "provider_error_category": None,
        "provider_completed_response": False,
        "evidence_reference": reference,
    }


def _aggregate_transport_evidence(terminal_status: str) -> dict[str, bool]:
    if terminal_status in {"RESOLVED", "UNRESOLVED", "PDB_NOT_REACHED", "INVALID_MODEL_RESPONSE"}:
        return {"completed_response": True, "malformed_response": terminal_status == "INVALID_MODEL_RESPONSE", "provider_error": False, "synthetic": False}
    if terminal_status == "PROVIDER_ERROR":
        return {"completed_response": False, "malformed_response": False, "provider_error": True, "synthetic": False}
    return {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False}


def _infrastructure_evidence(
    terminal_status: str,
    live_status: str,
    model_requests: int,
    reporting: Mapping[str, Any],
    run_id: str,
) -> dict[str, Any]:
    reference = f"{run_id}:infrastructure:{live_status}"
    base = {
        "stage": "none",
        "reason_code": "NONE",
        "confirmed_failure": False,
        "classification": "NONE",
        "terminal_classification": "NOT_APPLICABLE",
        "provider_attempt_index": None,
        "prior_lifecycle_completed": False,
        "source_mutation_observed": False,
        "expected_source_hash": None,
        "evidence_reference": reference,
    }
    if terminal_status != "INFRASTRUCTURE_ERROR":
        return base
    if live_status == "HARNESS_ERROR" or model_requests == 0:
        return {
            **base,
            "stage": "pre_provider",
            "reason_code": "WORKSPACE_FAILURE",
            "confirmed_failure": True,
            "classification": "PRE_PROVIDER",
            "terminal_classification": "INFRASTRUCTURE_FAILURE",
        }
    if live_status == "CONTROLLER_FAILED":
        return {
            **base,
            "stage": "controller",
            "reason_code": "CONTROLLER_FAILURE",
            "confirmed_failure": True,
            "classification": "CONTROLLER",
            "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "prior_lifecycle_completed": True,
        }
    if live_status == "CONTROLLER_REJECTED":
        return {
            **base,
            "stage": "controller",
            "reason_code": "RESULT_SCHEMA_INCONSISTENCY",
            "confirmed_failure": True,
            "classification": "CONTROLLER",
            "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "prior_lifecycle_completed": True,
        }
    if live_status == "VERIFIER_FAILED":
        return {
            **base,
            "stage": "verifier",
            "reason_code": "VERIFIER_FAILURE",
            "confirmed_failure": True,
            "classification": "VERIFIER",
            "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "prior_lifecycle_completed": True,
        }
    if live_status == "CLEANUP_FAILED":
        return {
            **base,
            "stage": "cleanup",
            "reason_code": "CLEANUP_FAILURE",
            "confirmed_failure": True,
            "classification": "CLEANUP",
            "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "prior_lifecycle_completed": True,
        }
    if live_status == "EVENT_REPORTING_FAILED":
        return {
            **base,
            "stage": "evidence_packaging",
            "reason_code": "EVIDENCE_PACKAGING_FAILURE",
            "confirmed_failure": True,
            "classification": "EVIDENCE_PACKAGING",
            "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "prior_lifecycle_completed": True,
        }
    return {
        **base,
        "stage": "controller",
        "reason_code": "CONTROLLER_FAILURE",
        "confirmed_failure": True,
        "classification": "CONTROLLER",
        "terminal_classification": "INFRASTRUCTURE_FAILURE",
        "prior_lifecycle_completed": model_requests > 0,
    }


# ---- CLI ---------------------------------------------------------------------


def _cli_require(condition: bool, message: str) -> None:
    if not condition:
        raise OpenCodeGoAdapterError(message)


def _cli_blocked(message: str) -> int:
    print(f"BLOCKED: {message}", file=sys.stderr)
    return 2


def _resolve_manifest(manifest_path: str | Path | None) -> dict[str, Any]:
    path = Path(manifest_path) if manifest_path is not None else pilot.MANIFEST_PATH_V2
    manifest = pilot.load_manifest(path)
    pilot.validate_manifest(manifest)
    if manifest["campaign_id"] != pilot.CAMPAIGN_ID_V2:
        raise OpenCodeGoAdapterError("the OpenCode Go adapter is bound to the frozen v2 campaign only")
    return manifest


def _load_authorization_and_evidence(
    authorization_path: str | Path | None,
    route_evidence_json: str | Path | None,
    manifest: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
    _cli_require(authorization_path is not None and Path(authorization_path).is_file(), "an explicit external authorization-artifact path is required")
    authorization = runner.load_authorization_artifact(authorization_path)
    runner.validate_authorization_artifact(authorization, manifest)
    evidence: Mapping[str, Any] | None = None
    if route_evidence_json is not None:
        path = Path(route_evidence_json)
        _cli_require(path.is_file(), f"route-evidence file is missing: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OpenCodeGoAdapterError(f"route-evidence file is invalid: {exc}") from exc
        _cli_require(isinstance(value, Mapping), "route-evidence file must contain a JSON object")
        evidence = value
    return authorization, evidence


def run_adapter_validate(
    manifest_path: str | Path | None,
    configuration_path: str | Path | None,
    authorization_path: str | Path | None = None,
    route_evidence_json: str | Path | None = None,
) -> dict[str, Any]:
    manifest = _resolve_manifest(manifest_path)
    _cli_require(configuration_path is not None and Path(configuration_path).is_file(), "an explicit adapter-configuration path is required")
    configuration = load_adapter_configuration(configuration_path)
    validated = validate_adapter_configuration_structure(configuration)
    result: dict[str, Any] = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "mode": "adapter-validate",
        "valid": True,
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "configuration_path": str(Path(configuration_path).resolve()),
        "runtime_model_id": validated["runtime_model_id"],
        "opencode_version": validated["opencode_version"],
        "catalog_fingerprint": validated["catalog_fingerprint"],
        "variant": validated["variant"],
        "route_class": validated["route_class"],
        "binding_checked": False,
    }
    if authorization_path is not None and route_evidence_json is not None:
        authorization, evidence = _load_authorization_and_evidence(authorization_path, route_evidence_json, manifest)
        execution_commit = authorization["accepted_campaign_commit"]
        verdict = runner.run_route_preflight(
            manifest, authorization, lambda: evidence,
            execution_commit=execution_commit,
        )
        _cli_require(verdict.passed, f"route preflight did not pass: {verdict.failure_category}")
        observation = verdict.route_observation
        bind_adapter_configuration(validated, manifest, authorization, observation)
        binding = build_runtime_identity_binding(authorization, observation, validated)
        result.update({
            "valid": True,
            "binding_checked": True,
            "runtime_identity_binding_fingerprint": binding.fingerprint(),
            "execution_commit": validated["execution_commit"],
            "authorization_hash": validated["authorization_hash"],
        })
    return result


def run_route_preflight_only(
    manifest_path: str | Path | None,
    authorization_path: str | Path | None,
    output_root: str | Path | None,
    route_evidence_json: str | Path | None,
    configuration_path: str | Path | None = None,
) -> dict[str, Any]:
    """Route/preflight-only mode: every gate completes with zero provider
    process creation; no transport factory is ever constructed here."""
    manifest = _resolve_manifest(manifest_path)
    _cli_require(output_root is not None, "route/preflight-only mode requires an explicit output/attempt root")
    authorization, evidence = _load_authorization_and_evidence(authorization_path, route_evidence_json, manifest)
    _cli_require(evidence is not None, "route/preflight-only mode requires an explicit route-evidence path")
    if configuration_path is not None:
        configuration = load_adapter_configuration(configuration_path)
        validated = validate_adapter_configuration_structure(configuration)
    else:
        validated = None
    record = runner.run_preflight_only(
        manifest,
        authorization=authorization,
        output_root=output_root,
        route_evidence_provider=lambda: evidence,
    )
    verdict = record.get("preflight")
    if isinstance(verdict, Mapping) and verdict.get("passed") is True and validated is not None:
        observation = verdict.get("route_observation")
        if isinstance(observation, Mapping):
            bind_adapter_configuration(validated, manifest, authorization, observation)
            build_runtime_identity_binding(authorization, observation, validated)
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "mode": "route-preflight-only",
        "preflight": verdict,
        "provider_processes_created": 0,
        "record": record,
    }


# ---- operator route capture ---------------------------------------------------


def _reject_template_value(value: str, label: str) -> None:
    if _TEMPLATE_VALUE.search(value):
        raise OpenCodeGoAdapterError(f"operator-supplied {label} carries a placeholder/template value: {value!r}")


def _reject_non_go_provider_identity(value: str, label: str) -> None:
    """Reject every runtime identity that does not use the ``opencode-go/``
    provider prefix (``opencode/``, including the historical Zen free-model
    identity, and any other provider)."""
    if not value.startswith(GO_RUNTIME_ID_PREFIX):
        raise OpenCodeGoAdapterError(
            f"{label} must use the exact opencode-go/ catalog-qualified provider prefix; rejected {value!r}"
        )


def _reject_evidence_template_values(value: Any, path: str = "evidence") -> None:
    if isinstance(value, str):
        _reject_template_value(value, path)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _reject_evidence_template_values(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_evidence_template_values(item, f"{path}[{index}]")


def _resolve_catalog_command() -> list[str]:
    """The single OpenCode Go catalog inspection command: exactly
    ``models opencode-go --verbose --pure`` (never ``models opencode``)."""
    return ["opencode.cmd", "models", "opencode-go", "--verbose", "--pure"]


def _parse_utc_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _run_catalog_inspection() -> str:
    """Run the single local/non-model catalog inspection command.

    This is the only OpenCode catalog command the operator route capture
    ever invokes; ``opencode run`` is never constructed or executed here.
    """
    completed = subprocess.run(
        _resolve_catalog_command(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", timeout=30, check=False,
    )
    if completed.returncode != 0:
        raise OpenCodeGoAdapterError(f"OpenCode model catalog inspection failed with exit code {completed.returncode}")
    if len(completed.stdout) > MAX_CAPTURE_COMMAND_OUTPUT_BYTES:
        raise OpenCodeGoAdapterError("OpenCode model catalog inspection output exceeded the bounded capture")
    return completed.stdout


def run_route_capture(
    runtime_model_id: str,
    variant: str,
    *,
    account_status: str,
    subscription_entitlement_confirmed: bool,
    entitlement_evidence_reference: str,
    billing_route_assertion: str,
    output: str | Path,
    manifest_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only operator route capture.

    Runs only local/non-model OpenCode inspection commands (launcher version
    and model catalog inspection); never invokes ``opencode run``.  Requires
    the exact operator-selected runtime model ID and variant, locates exactly
    one active catalog entry, records its observed status, variant
    availability, and finite pricing metadata, rejects the historical
    Zen/free-tier identity, requires explicit operator-supplied account
    status, subscription entitlement confirmation/reference, and a
    billing-route assertion, records every denial/fallback observation
    explicitly, and writes a strict ``quixbugs-route-evidence-v1`` artifact
    (accepted by the existing live-runner validator) with create-once
    semantics into the ignored ``operator/`` storage.  No credentials, auth
    tokens, cookies, or raw private account data are ever written.
    """
    observed_at = (now if now is not None else datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if not isinstance(runtime_model_id, str) or "/" not in runtime_model_id or not runtime_model_id.strip():
        raise OpenCodeGoAdapterError("route capture requires the exact catalog-qualified runtime model ID (provider/id)")
    _reject_template_value(runtime_model_id, "runtime model ID")
    if runtime_model_id == HISTORICAL_ZEN_MODEL_ID:
        raise OpenCodeGoAdapterError("the historical OpenCode Zen free-model identity is rejected as a runtime route identity")
    _reject_non_go_provider_identity(runtime_model_id, "runtime model ID")
    if not isinstance(variant, str) or not variant.strip():
        raise OpenCodeGoAdapterError("route capture requires the exact operator-selected variant")
    _reject_template_value(variant, "variant")
    if not isinstance(account_status, str) or not account_status.strip():
        raise OpenCodeGoAdapterError("route capture requires an explicit operator-supplied account status")
    _reject_template_value(account_status, "account status")
    if subscription_entitlement_confirmed is not True:
        raise OpenCodeGoAdapterError("route capture requires explicit operator confirmation of subscription entitlement")
    if not isinstance(entitlement_evidence_reference, str) or not entitlement_evidence_reference.strip():
        raise OpenCodeGoAdapterError("route capture requires an explicit operator-supplied subscription entitlement evidence reference")
    _reject_template_value(entitlement_evidence_reference, "entitlement evidence reference")
    if billing_route_assertion != pilot.AUTHORIZED_BILLING_ROUTE:
        raise OpenCodeGoAdapterError("route capture requires the explicit billing-route assertion SUBSCRIPTION")

    manifest = _resolve_manifest(manifest_path)
    _, model_family = runtime_model_id.split("/", 1)
    if model_family != pilot.SUBSCRIPTION_ROUTE_MODEL:
        raise OpenCodeGoAdapterError(
            f"runtime model identity {runtime_model_id!r} is not the frozen campaign model family {pilot.SUBSCRIPTION_ROUTE_MODEL!r}"
        )

    target = Path(output).resolve()
    storage_root = OPERATOR_STORAGE.resolve()
    try:
        target.relative_to(storage_root)
    except ValueError:
        raise OpenCodeGoAdapterError(f"route capture output must be inside the ignored operator storage {storage_root}")
    if target.exists():
        raise OpenCodeGoAdapterError(f"route capture target already exists (create-once): {target}")

    try:
        launcher = transport.verify_opencode_launcher()
        opencode_version = str(launcher["version"]).strip()
        catalog_stdout = _run_catalog_inspection()
        entry = transport.select_catalog_entry(catalog_stdout, runtime_model_id)
        facts = transport.catalog_entry_facts(entry, variant)
        fingerprint = transport.catalog_entry_fingerprint(entry)
    except RuntimeError as exc:
        raise OpenCodeGoAdapterError(f"route capture rejected: {exc}") from exc

    evidence: dict[str, Any] = {
        "schema_version": ROUTE_EVIDENCE_SCHEMA_VERSION,
        "provider": pilot.SUBSCRIPTION_ROUTE_PROVIDER,
        "model": model_family,
        "variant": variant,
        "protocol": runner.LIVE_PROTOCOL_VERSION,
        "opencode_version": opencode_version,
        "catalog_fingerprint": fingerprint,
        "runtime_model_id": runtime_model_id,
        "billing_route": billing_route_assertion,
        "subscription_entitlement_confirmed": True,
        "account_status": account_status,
        "active_model_status": facts["active_model_status"],
        "variant_available": facts["variant_available"],
        "input_price": facts["input_price"],
        "output_price": facts["output_price"],
        # No provider call is made during capture, so no cost was reported;
        # zero is the strict schema's explicit absence representation and is
        # recorded as such in the capture record.
        "provider_reported_cost": 0.0,
        "paid_fallback_used": False,
        "alternate_provider_used": False,
        "ollama_used": False,
        "zen_used": False,
        "free_tier_used": False,
        "metered_fallback_used": False,
        "paid_overage_used": False,
        "per_call_billing_used": False,
        "model_substitution_observed": False,
        "observed_at": observed_at,
    }
    runner.validate_raw_route_evidence(evidence, {"expected_account_status": account_status}, now=(now if now is not None else datetime.now(timezone.utc)))

    target.parent.mkdir(parents=True, exist_ok=True)
    runner.atomic_create_json(target, evidence)
    capture_record = _redact({
        "schema_version": CAPTURE_RECORD_SCHEMA_VERSION,
        "record_kind": "route-capture",
        "captured_at": observed_at,
        "runtime_model_id": runtime_model_id,
        "variant": variant,
        "model_family": model_family,
        "launcher": {"version": opencode_version, "resolved_path": launcher.get("resolved_path")},
        "catalog_entry": entry,
        "catalog_fingerprint": fingerprint,
        "observed_facts": facts,
        "operator_assertions": {
            "account_status": account_status,
            "subscription_entitlement_confirmed": True,
            "entitlement_evidence_reference": entitlement_evidence_reference,
            "billing_route_assertion": billing_route_assertion,
        },
        "denial_observations": {
            "zen_used": False, "free_tier_used": False, "ollama_used": False,
            "paid_fallback_used": False, "alternate_provider_used": False,
            "metered_fallback_used": False, "paid_overage_used": False,
            "per_call_billing_used": False, "model_substitution_observed": False,
            "note": "zero model/provider contact during capture; every denial/fallback observation is recorded explicitly as not used",
        },
        "provider_reported_cost_note": "no provider call occurred during capture; 0.0 is the strict schema's explicit absence representation",
        "provider_contact_proof": {
            "run_invoked": False,
            "model_requests": 0,
            "inspection_commands": [["opencode.cmd", "--version"], _resolve_catalog_command()],
        },
        "evidence_file": str(target),
        "evidence_sha256": hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest(),
    })
    capture_record_path = target.with_suffix(target.suffix + ".capture-record.json")
    try:
        runner.atomic_create_json(capture_record_path, capture_record)
    except runner.OutputIntegrityError:
        pass
    return {
        "schema_version": ROUTE_EVIDENCE_SCHEMA_VERSION,
        "mode": "route-capture",
        "captured": True,
        "evidence_path": str(target),
        "runtime_model_id": runtime_model_id,
        "variant": variant,
        "opencode_version": opencode_version,
        "catalog_fingerprint": fingerprint,
        "catalog_entry_id": str(entry.get("id")),
        "active_model_status": facts["active_model_status"],
        "variant_available": facts["variant_available"],
        "input_price": facts["input_price"],
        "output_price": facts["output_price"],
        "account_status": account_status,
        "billing_route": billing_route_assertion,
        "run_invoked": False,
        "model_requests": 0,
    }


# ---- operator bundle materialization ------------------------------------------


def _operator_bundle_paths(
    attempt_identity: str,
    output_root: str | Path,
    bundle_root: str | Path | None,
) -> tuple[Path, Path, Path, Path]:
    root = Path(output_root).resolve()
    bundle = Path(bundle_root).resolve() if bundle_root is not None else (OPERATOR_STORAGE / OPERATOR_BUNDLES_RELATIVE_DIR / attempt_identity).resolve()
    return root, bundle, bundle / "authorization.json", bundle / "adapter-config.json"


def _reject_occupied_target(path: Path, label: str) -> None:
    if not path.exists():
        return
    try:
        entries = sorted(path.iterdir())
    except OSError as exc:
        raise OpenCodeGoAdapterError(f"{label} could not be inspected: {exc}") from exc
    if entries:
        raise OpenCodeGoAdapterError(
            f"{label} is occupied (not absent or structurally empty): {', '.join(entry.name for entry in entries)}"
        )


def _git(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one read-only Git inspection command against the repository root.

    Never mutates the index, the working tree, or the repository state.
    """
    return subprocess.run(
        command, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
    )


def observe_bundle_execution_head(
    git_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Resolve the actual clean Git HEAD via read-only inspection.

    The execution commit for the operator bundle is never caller-supplied and
    is never the task baseline: it is the actual repository HEAD observed at
    bundle-materialization time.  The observed HEAD must be a valid existing
    commit, must descend from the accepted project baseline
    (:data:`runner.ACCEPTED_BASELINE`) and from the minimum task lineage
    baseline (:data:`TASK_BASELINE`), and the tracked working tree, the real
    Git index, and the untracked-file inventory (no non-ignored untracked
    files) must be clean.  Raises :class:`runner.RepositoryStateError` with a
    typed category on any violation.  ``git_runner`` is an injectable
    read-only Git command runner for deterministic tests.
    """
    run = git_runner if git_runner is not None else _git
    head_result = run(["git", "rev-parse", "HEAD"])
    head = (head_result.stdout or "").strip()
    if head_result.returncode != 0 or not _HEX40_PATTERN.fullmatch(head):
        raise runner.RepositoryStateError("REPOSITORY_STATE_UNVERIFIABLE", "Git HEAD could not be resolved")
    if run(["git", "cat-file", "-e", f"{head}^{{commit}}"]).returncode != 0:
        raise runner.RepositoryStateError("EXECUTION_COMMIT_NOT_FOUND", f"resolved Git HEAD {head} does not exist as a commit")
    if run(["git", "merge-base", "--is-ancestor", runner.ACCEPTED_BASELINE, head]).returncode != 0:
        raise runner.RepositoryStateError(
            "EXECUTION_COMMIT_ANCESTRY_FAILED",
            f"resolved Git HEAD {head} does not descend from the accepted project baseline {runner.ACCEPTED_BASELINE}",
        )
    if run(["git", "merge-base", "--is-ancestor", TASK_BASELINE, head]).returncode != 0:
        raise runner.RepositoryStateError(
            "EXECUTION_COMMIT_ANCESTRY_FAILED",
            f"resolved Git HEAD {head} does not descend from the minimum task lineage baseline {TASK_BASELINE}",
        )
    status_result = run(["git", "status", "--porcelain"])
    lines = status_result.stdout.splitlines() if status_result.returncode == 0 else []
    tracked_clean = True
    index_clean = True
    untracked_non_ignored: list[str] = []
    for line in lines:
        if not line or len(line) < 3:
            continue
        x, y = line[0], line[1]
        path = line[3:]
        if x == "?" and y == "?":
            if run(["git", "check-ignore", "-q", "--", path]).returncode != 0:
                untracked_non_ignored.append(path)
            continue
        tracked_clean = False
        if x != " ":
            index_clean = False
    if not tracked_clean or not index_clean or untracked_non_ignored:
        raise runner.RepositoryStateError(
            "TRACKED_STATE_DIRTY",
            f"tracked working tree, Git index, or untracked-file inventory is not clean; "
            f"untracked non-ignored: {sorted(untracked_non_ignored)}",
        )
    return head


def run_operator_bundle(
    manifest_path: str | Path | None,
    route_evidence_json: str | Path,
    *,
    operator_authorization_id: str,
    attempt_identity: str,
    output_root: str | Path,
    valid_until: str,
    entitlement_evidence_reference: str,
    python_executable: str | Path,
    working_directory: str | Path,
    operator_boundary_root: str | Path,
    bundle_root: str | Path | None = None,
    git_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Materialize the real operator bundle from the accepted route evidence.

    Consumes the strict ``quixbugs-route-evidence-v1`` file and creates the
    real ``quixbugs-paired-pilot-authorization-v1`` artifact and the real
    ``quixbugs-opencode-go-execution-adapter-v1`` configuration, both bound
    to the actual clean Git HEAD observed (read-only) when the operator runs
    this command after the task has been accepted and merged — never to a
    caller-supplied commit and never to the task baseline.  The observed HEAD
    must exist, must descend from the accepted project baseline and from the
    minimum task lineage baseline (:data:`TASK_BASELINE`), and must have a
    clean tracked working tree, a clean real index, and no non-ignored
    untracked files; HEAD and repository cleanliness are re-checked
    immediately before the artifacts are created, and any drift between
    observation and materialization fails closed with no active artifact
    written.  The same independently observed HEAD is used in the
    authorization's ``accepted_campaign_commit``, the adapter configuration's
    ``execution_commit``, the route-preflight execution binding, the runtime
    identity binding, and the returned record.

    The artifacts are also bound to the frozen manifest hash, the exact six
    frozen case IDs in order, protocol ``1.3``, the exact observed OpenCode
    version, runtime model ID, variant, and catalog fingerprint, the account
    status and subscription billing route, one operator authorization ID, one
    fresh attempt identity and output root, an explicit bounded validity
    period, and the operator-resolved Python executable, repository wrapper
    path, working directory, and operator boundary root.  Rejects dirty or
    staged source, drift, occupied targets, template values, route drift,
    unknown fields, malformed paths, and contradictory subscription/fallback
    assertions.  Active operator artifacts are never committed.
    """
    reference_time = now if now is not None else datetime.now(timezone.utc)
    created_at = reference_time.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    manifest = _resolve_manifest(manifest_path)
    if len(manifest["case_order"]) != 6:
        raise OpenCodeGoAdapterError("the operator bundle is bound to the frozen six-case v2 campaign only")

    for label, value in (
        ("operator authorization ID", operator_authorization_id),
        ("attempt identity", attempt_identity),
        ("entitlement evidence reference", entitlement_evidence_reference),
    ):
        if not isinstance(value, str) or not value.strip():
            raise OpenCodeGoAdapterError(f"operator bundle requires an explicit {label}")
        _reject_template_value(value, label)
    if not runner.ATTEMPT_IDENTITY_PATTERN.fullmatch(attempt_identity):
        raise OpenCodeGoAdapterError("operator bundle attempt identity is invalid")
    if not isinstance(valid_until, str) or not valid_until.strip():
        raise OpenCodeGoAdapterError("operator bundle requires an explicit bounded validity period")
    _reject_template_value(valid_until, "validity period")
    valid_until_dt = _parse_utc_timestamp(valid_until)
    if valid_until_dt is None:
        raise OpenCodeGoAdapterError("operator bundle validity period is not a parseable ISO-8601 UTC timestamp")
    if valid_until_dt <= reference_time:
        raise OpenCodeGoAdapterError("operator bundle validity period must be later than materialization time")

    evidence_path = Path(route_evidence_json)
    _cli_require(evidence_path.is_file(), f"route-evidence file is missing: {evidence_path}")
    try:
        raw = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenCodeGoAdapterError(f"route-evidence file is invalid: {exc}") from exc
    _cli_require(isinstance(raw, Mapping), "route-evidence file must contain a JSON object")
    _reject_evidence_template_values(raw)

    raw_runtime_model_id = str(raw.get("runtime_model_id") or "")
    if raw_runtime_model_id == HISTORICAL_ZEN_MODEL_ID:
        raise OpenCodeGoAdapterError("route evidence established the historical OpenCode Zen free-model identity")
    _reject_non_go_provider_identity(raw_runtime_model_id, "route evidence runtime model ID")
    if raw.get("billing_route") != pilot.AUTHORIZED_BILLING_ROUTE:
        raise OpenCodeGoAdapterError("route evidence billing route is not the authorized subscription route")
    if raw.get("subscription_entitlement_confirmed") is not True:
        raise OpenCodeGoAdapterError("route evidence does not confirm subscription entitlement; the bundle would be contradictory")
    if raw.get("model") != pilot.SUBSCRIPTION_ROUTE_MODEL or raw.get("provider") != pilot.SUBSCRIPTION_ROUTE_PROVIDER:
        raise OpenCodeGoAdapterError("route evidence model/provider drift from the frozen campaign route")

    runner.validate_raw_route_evidence(raw, {"expected_account_status": raw.get("account_status") or ""}, now=reference_time)

    execution_commit = observe_bundle_execution_head(git_runner)

    root, bundle, authorization_path, configuration_path = _operator_bundle_paths(attempt_identity, output_root, bundle_root)
    _reject_occupied_target(root, "output/attempt root")
    _reject_occupied_target(bundle, "operator bundle root")

    interpreter = Path(python_executable).resolve()
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise OpenCodeGoAdapterError("operator bundle requires an absolute operator-resolved Python executable that exists")
    working = Path(working_directory).resolve()
    if not working.is_absolute() or not working.is_dir():
        raise OpenCodeGoAdapterError("operator bundle requires an absolute operator-resolved working directory that exists")
    boundary = Path(operator_boundary_root).resolve()
    if not boundary.is_absolute():
        raise OpenCodeGoAdapterError("operator bundle requires an absolute operator boundary root")
    for label, path_value in (("executable", interpreter), ("working directory", working)):
        try:
            path_value.relative_to(boundary)
        except ValueError:
            raise OpenCodeGoAdapterError(f"operator {label} is outside the operator boundary {boundary}")

    authorization = {
        "schema_version": runner.AUTHORIZATION_SCHEMA_VERSION,
        "template": False,
        "authorize_live": True,
        "campaign_id": pilot.CAMPAIGN_ID_V2,
        "campaign_version": 2,
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": runner.ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "qualification_contract_hash": manifest["qualification_contract_hash"],
        "accepted_campaign_commit": execution_commit,
        "permitted_case_ids": [case["case_id"] for case in manifest["case_order"]],
        "provider": pilot.SUBSCRIPTION_ROUTE_PROVIDER,
        "model": pilot.SUBSCRIPTION_ROUTE_MODEL,
        "variant": str(raw["variant"]),
        "protocol": runner.LIVE_PROTOCOL_VERSION,
        "expected_opencode_version": str(raw["opencode_version"]),
        "expected_catalog_fingerprint": str(raw["catalog_fingerprint"]),
        "expected_runtime_model_id": raw_runtime_model_id,
        "subscription_route_required": True,
        "expected_billing_route": pilot.AUTHORIZED_BILLING_ROUTE,
        "subscription_entitlement_confirmed": True,
        "subscription_account_observation": {
            "entitlement_confirmed": True,
            "evidence_reference": entitlement_evidence_reference,
        },
        "expected_account_status": str(raw["account_status"]),
        "billing_route_classification": pilot.AUTHORIZED_BILLING_ROUTE,
        **{flag: True for flag in DENIAL_FIELDS},
        "no_fallback_required": True,
        "operator_authorization_id": operator_authorization_id,
        "authorization_created_at": created_at,
        "authorization_valid_until": valid_until,
        "output_root": str(root),
        "campaign_attempt_identity": attempt_identity,
        "single_frozen_six_case_campaign_confirmation": True,
    }
    runner.validate_authorization_artifact(authorization, manifest, expected_output_root=root, now=reference_time)

    verdict = runner.run_route_preflight(
        manifest, authorization, lambda: raw,
        now=reference_time, attempt_identity=attempt_identity,
        execution_commit=execution_commit,
    )
    if verdict.passed is not True:
        raise OpenCodeGoAdapterError(
            f"route evidence did not pass the pre-provider gate (route drift or contradiction): {verdict.failure_category}"
        )
    observation = verdict.route_observation

    command = [
        str(interpreter),
        str(PROTOCOL_WRAPPER_PATH.resolve()),
        "--model", raw_runtime_model_id,
        "--variant", str(raw["variant"]),
        "--route-mode", ADAPTER_ROUTE_MODE,
        "--expected-opencode-version", str(raw["opencode_version"]),
        "--expected-catalog-fingerprint", str(raw["catalog_fingerprint"]),
        "--expected-runtime-model-id", raw_runtime_model_id,
        "--expected-account-status", str(raw["account_status"]),
        "--expected-billing-route", pilot.AUTHORIZED_BILLING_ROUTE,
    ]
    configuration = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "template": False,
        "adapter_identity": ADAPTER_IDENTITY,
        "campaign_id": pilot.CAMPAIGN_ID_V2,
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "operator_authorization_id": operator_authorization_id,
        "authorization_hash": runner.authorization_hash(authorization),
        "execution_commit": execution_commit,
        "executable": str(interpreter),
        "command": command,
        "working_directory": str(working),
        "operator_boundary_root": str(boundary),
        "protocol_version": runner.LIVE_PROTOCOL_VERSION,
        "provider": pilot.SUBSCRIPTION_ROUTE_PROVIDER,
        "model_family": pilot.SUBSCRIPTION_ROUTE_MODEL,
        "variant": str(raw["variant"]),
        "runtime_model_id": raw_runtime_model_id,
        "opencode_version": str(raw["opencode_version"]),
        "catalog_fingerprint": str(raw["catalog_fingerprint"]),
        "route_class": pilot.AUTHORIZED_BILLING_ROUTE,
        "expected_account_status": str(raw["account_status"]),
        "per_call_timeout_seconds": float(manifest["budgets"]["per_call_timeout_seconds"]),
        "total_case_timeout_seconds": float(manifest["budgets"]["total_case_timeout_seconds"]),
        "environment_allowlist": ["PATH", "SystemRoot"],
        "max_stdout_bytes": 1048576,
        "max_stderr_bytes": 1048576,
        "max_diagnostic_bytes": 16384,
        "transport_retry_limit": int(manifest["budgets"]["max_transport_retries_per_logical_call"]),
        "max_transport_attempts_per_logical_call": int(manifest["budgets"]["max_transport_attempts_per_logical_call"]),
        "no_automatic_route_discovery": True,
        "no_global_model_selection": True,
        "requires_active_authorization_binding": True,
        **{flag: True for flag in DENIAL_FIELDS},
        "no_fallback_required": True,
    }
    validated = validate_adapter_configuration_structure(configuration)
    bind_adapter_configuration(validated, manifest, authorization, observation)
    binding = build_runtime_identity_binding(authorization, observation, validated)

    # Recheck HEAD and repository cleanliness immediately before the active
    # artifacts are created: any drift between the first observation and this
    # materialization gate fails closed and creates neither artifact.
    rechecked_head = observe_bundle_execution_head(git_runner)
    if rechecked_head != execution_commit:
        raise OpenCodeGoAdapterError(
            f"execution-commit drift: Git HEAD changed between observation ({execution_commit}) "
            f"and materialization ({rechecked_head}); no active artifact was created"
        )

    try:
        runner.atomic_create_json(authorization_path, authorization)
        runner.atomic_create_json(configuration_path, validated)
    except runner.OutputIntegrityError as exc:
        raise OpenCodeGoAdapterError(f"operator bundle target was already occupied (create-once): {exc}") from exc

    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "mode": "operator-bundle",
        "materialized": True,
        "operator_authorization_id": operator_authorization_id,
        "campaign_attempt_identity": attempt_identity,
        "execution_commit": execution_commit,
        "independently_observed_head": execution_commit,
        "task_baseline": TASK_BASELINE,
        "authorization_path": str(authorization_path),
        "configuration_path": str(configuration_path),
        "bundle_root": str(bundle),
        "output_root": str(root),
        "authorization_hash": runner.authorization_hash(authorization),
        "runtime_model_id": raw_runtime_model_id,
        "variant": str(raw["variant"]),
        "opencode_version": str(raw["opencode_version"]),
        "catalog_fingerprint": str(raw["catalog_fingerprint"]),
        "route_class": pilot.AUTHORIZED_BILLING_ROUTE,
        "account_status": str(raw["account_status"]),
        "valid_until": valid_until,
        "frozen_case_ids": [case["case_id"] for case in manifest["case_order"]],
        "runtime_identity_binding_fingerprint": binding.fingerprint(),
        "provider_processes_created": 0,
    }


def run_synthetic_selftest(
    output_root: str | Path,
    scenario: str | None = None,
) -> dict[str, Any]:
    """Synthetic adapter self-test: exercises the actual subprocess/protocol
    adapter against the synthetic executable only; no real executable,
    provider, catalog, or account is ever contacted."""
    from agentic_debugger.demo.policies import DemoPolicy
    from quixbugs_live_runner_v2 import GitRepositoryState

    out = Path(output_root)
    manifest = _resolve_manifest(None)
    synthetic_root = out / "selftest-operator-boundary"
    synthetic_root.mkdir(parents=True, exist_ok=True)
    synthetic_executable = REPO_ROOT / "scripts" / "opencode_go_synthetic_executable.py"
    _cli_require(synthetic_executable.is_file(), "synthetic executable is missing")
    _cli_require(PROTOCOL_WRAPPER_PATH.is_file(), "accepted protocol wrapper is missing")

    interpreter = sys.executable
    runtime_model_id = "opencode-go/synthetic-deepseek-v4-flash"
    from opencode_go_synthetic_executable import SYNTHETIC_CATALOG_ENTRIES

    synthetic_entry = next(
        (entry for entry in SYNTHETIC_CATALOG_ENTRIES if entry.get("id") == "synthetic-deepseek-v4-flash"),
        None,
    )
    _cli_require(synthetic_entry is not None, "synthetic catalog entry is missing")
    # The exact deterministic catalog fingerprint the fake OpenCode CLI
    # catalog produces; the real wrapper independently recomputes it during
    # its OpenCode Go preflight and must agree exactly.
    synthetic_fingerprint = transport.catalog_entry_fingerprint(synthetic_entry)
    # The active command launches the ACCEPTED protocol wrapper; the wrapper
    # itself invokes the fake ``opencode.cmd`` shim (which runs the synthetic
    # executable) from the bounded environment PATH.
    synthetic_command = [
        interpreter,
        str(PROTOCOL_WRAPPER_PATH),
        "--model", runtime_model_id,
        "--variant", "max",
        "--route-mode", ADAPTER_ROUTE_MODE,
        "--expected-opencode-version", "1.0.0",
        "--expected-catalog-fingerprint", synthetic_fingerprint,
        "--expected-runtime-model-id", runtime_model_id,
        "--expected-account-status", "ACTIVE",
        "--expected-billing-route", "SUBSCRIPTION",
    ]
    shim_dir = synthetic_root / "fake-bin"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "opencode.cmd"
    if not shim.is_file():
        shim.write_text(
            "@echo off\r\n"
            f'"{interpreter}" "{synthetic_executable}" %*\r\n',
            encoding="utf-8",
        )
    fake_profile = synthetic_root / "fake-profile"
    fake_auth = fake_profile / ".local" / "share" / "opencode" / "auth.json"
    fake_auth.parent.mkdir(parents=True, exist_ok=True)
    if not fake_auth.is_file():
        fake_auth.write_text("synthetic auth fixture", encoding="utf-8")
    operator_boundary = common_operator_boundary([interpreter, synthetic_root])
    configuration = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "template": False,
        "adapter_identity": ADAPTER_IDENTITY,
        "campaign_id": pilot.CAMPAIGN_ID_V2,
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "operator_authorization_id": "synthetic-selftest-operator-001",
        "authorization_hash": "a" * 64,
        "execution_commit": runner.ACCEPTED_BASELINE,
        "executable": interpreter,
        "command": synthetic_command,
        "working_directory": str(synthetic_root),
        "operator_boundary_root": str(operator_boundary),
        "protocol_version": runner.LIVE_PROTOCOL_VERSION,
        "provider": "OpenCode Go",
        "model_family": "deepseek-v4-flash",
        "variant": "max",
        "runtime_model_id": runtime_model_id,
        "opencode_version": "1.0.0",
        "catalog_fingerprint": synthetic_fingerprint,
        "route_class": "SUBSCRIPTION",
        "expected_account_status": "ACTIVE",
        "per_call_timeout_seconds": 20.0,
        "total_case_timeout_seconds": 30.0,
        "environment_allowlist": ["PATH", "SystemRoot", "USERPROFILE", "HOME"],
        "max_stdout_bytes": 262144,
        "max_stderr_bytes": 262144,
        "max_diagnostic_bytes": 16384,
        "transport_retry_limit": 0,
        "max_transport_attempts_per_logical_call": 1,
        "no_automatic_route_discovery": True,
        "no_global_model_selection": True,
        "requires_active_authorization_binding": True,
        "deny_zen_route": True,
        "deny_free_tier_substitution": True,
        "deny_ollama_route": True,
        "deny_alternate_provider": True,
        "deny_model_substitution": True,
        "deny_metered_fallback": True,
        "deny_paid_overage": True,
        "deny_per_call_billing_fallback": True,
        "no_fallback_required": True,
    }
    validated = validate_adapter_configuration_structure(configuration)
    authorization = {
        "schema_version": runner.AUTHORIZATION_SCHEMA_VERSION,
        "template": False,
        "authorize_live": True,
        "campaign_id": pilot.CAMPAIGN_ID_V2,
        "campaign_version": 2,
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": runner.ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "qualification_contract_hash": manifest["qualification_contract_hash"],
        "accepted_campaign_commit": runner.ACCEPTED_BASELINE,
        "permitted_case_ids": [case["case_id"] for case in manifest["case_order"]],
        "provider": "OpenCode Go",
        "model": "deepseek-v4-flash",
        "variant": "max",
        "protocol": "1.3",
        "expected_opencode_version": "1.0.0",
        "expected_catalog_fingerprint": synthetic_fingerprint,
        "expected_runtime_model_id": runtime_model_id,
        "subscription_route_required": True,
        "expected_billing_route": "SUBSCRIPTION",
        "subscription_entitlement_confirmed": True,
        "subscription_account_observation": {"entitlement_confirmed": True, "evidence_reference": "synthetic-selftest-account-001"},
        "expected_account_status": "ACTIVE",
        "billing_route_classification": "SUBSCRIPTION",
        "deny_zen_route": True,
        "deny_free_tier_substitution": True,
        "deny_ollama_route": True,
        "deny_alternate_provider": True,
        "deny_model_substitution": True,
        "deny_metered_fallback": True,
        "deny_paid_overage": True,
        "deny_per_call_billing_fallback": True,
        "no_fallback_required": True,
        "operator_authorization_id": "synthetic-selftest-operator-001",
        "authorization_created_at": "2026-08-02T00:00:00Z",
        "authorization_valid_until": None,
        "output_root": str(out),
        "campaign_attempt_identity": "quixbugs-paired-pilot-v2-attempt-" + "e" * 64,
        "single_frozen_six_case_campaign_confirmation": True,
    }
    authorization["campaign_manifest_hash"] = pilot.manifest_hash(manifest)
    authorization["accepted_baseline"] = runner.ACCEPTED_BASELINE
    validated["authorization_hash"] = runner.authorization_hash(authorization)
    validated["execution_commit"] = runner.ACCEPTED_BASELINE
    validated["operator_authorization_id"] = authorization["operator_authorization_id"]

    observed = {
        "provider": "OpenCode Go",
        "model": "deepseek-v4-flash",
        "variant": "max",
        "protocol": "1.3",
        "opencode_version": "1.0.0",
        "catalog_fingerprint": synthetic_fingerprint,
        "runtime_model_id": runtime_model_id,
        "billing_route": "SUBSCRIPTION",
        "subscription_entitlement_confirmed": True,
        "account_status": "ACTIVE",
        "active_model_status": "ACTIVE",
        "variant_available": True,
        "input_price": 0.1,
        "output_price": 0.2,
        "provider_reported_cost": 0.0042,
        "paid_fallback_used": False,
        "alternate_provider_used": False,
        "ollama_used": False,
        "zen_used": False,
        "free_tier_used": False,
        "metered_fallback_used": False,
        "paid_overage_used": False,
        "per_call_billing_used": False,
        "model_substitution_observed": False,
        "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "preflight_success": True,
        "execution_commit": runner.ACCEPTED_BASELINE,
    }
    bind_adapter_configuration(validated, manifest, authorization, observed)
    binding = build_runtime_identity_binding(authorization, observed, validated)

    claim = out / f"selftest-attempt-{uuid.uuid4().hex[:12]}"
    runner.claim_output_root(
        claim,
        attempt_identity=authorization["campaign_attempt_identity"],
        authorization_hash=runner.authorization_hash(authorization),
        campaign_manifest_hash=pilot.manifest_hash(manifest),
    )
    ledger = runner.AttemptLedger(claim / "ledger.json")
    ledger.claim({
        "attempt_identity": authorization["campaign_attempt_identity"],
        "authorization_hash": runner.authorization_hash(authorization),
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": runner.ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "execution_commit": runner.ACCEPTED_BASELINE,
        "case_ids": [case["case_id"] for case in manifest["case_order"]],
        "route_binding": {
            "provider": "OpenCode Go", "model": "deepseek-v4-flash",
            "variant": "max", "protocol": "1.3",
            "opencode_version": "1.0.0", "catalog_fingerprint": "f" * 64,
            "runtime_model_id": runtime_model_id, "billing_route": "SUBSCRIPTION",
            "execution_commit": runner.ACCEPTED_BASELINE,
        },
        "status": "STARTED",
        "created_at": observed["observed_at"],
        "updated_at": observed["observed_at"],
        "output_root": str(claim.resolve()),
    })
    factory = OpenCodeGoTransportFactory(
        authorization=authorization,
        execution_commit=runner.ACCEPTED_BASELINE,
        route_observation=observed,
        configuration=validated,
        binding=binding,
        attempt_identity=authorization["campaign_attempt_identity"],
        output_root=claim,
        ledger_path=claim / "ledger.json",
        evidence_dir=out / "selftest-evidence",
    )
    case = manifest["case_order"][0]
    transport = factory.prepare(case)
    prepare_gate_verified = transport is not None
    scenarios = [scenario] if scenario is not None else ["valid-usage", "valid-no-usage", "cost-zero", "malformed-always", "identity-mismatch", "route-drift", "credential-output", "nonzero-exit"]
    scenario_results: list[dict[str, Any]] = []
    environment_override = {
        "PATH": str(shim_dir) + os.pathsep + os.environ.get("PATH", ""),
        "USERPROFILE": str(fake_profile),
        "HOME": str(fake_profile),
    }
    for name in scenarios:
        scenario_transport = OpenCodeGoTransport(
            factory=factory,
            case_id=f"selftest-{name}",
            command=list(validated["command"]),
            working_directory=Path(validated["working_directory"]),
            environment_allowlist=list(validated["environment_allowlist"]),
            max_stdout_bytes=int(validated["max_stdout_bytes"]),
            max_stderr_bytes=int(validated["max_stderr_bytes"]),
            max_diagnostic_bytes=int(validated["max_diagnostic_bytes"]),
            per_call_timeout_seconds=float(validated["per_call_timeout_seconds"]),
            environment_override=environment_override,
        )
        payload = {
            "protocol": {"name": "agentic-debugger-live-jsonl", "version": "1.3"},
            "directive_feedback": None,
            "task": {"task_id": case["task_id"]},
            "synthetic_scenario": name,
        }
        result: dict[str, Any] = {"scenario": name, "process_attempts": 0, "outcome": "UNKNOWN"}
        try:
            response = scenario_transport.request(payload, 25.0)
            result.update({
                "outcome": "RESPONSE",
                "process_attempts": scenario_transport.process_attempts,
                "directive": response.get("directive"),
                "usage": response.get("usage"),
                "observed_identity": scenario_transport.observed_identity[-1] if scenario_transport.observed_identity else None,
            })
        except runner.RouteDriftError as exc:
            result.update({"outcome": "ROUTE_DRIFT", "process_attempts": scenario_transport.process_attempts, "drift_category": exc.category})
        except Exception as exc:
            from agentic_debugger.evaluation.live import LiveTransportError

            if isinstance(exc, LiveTransportError):
                result.update({"outcome": "TRANSPORT_ERROR", "process_attempts": scenario_transport.process_attempts, "kind": exc.kind, "timed_out": exc.timed_out})
            else:
                result.update({"outcome": "ERROR", "process_attempts": scenario_transport.process_attempts, "error": f"{type(exc).__name__}: {exc}"})
        scenario_results.append(result)
        _append_evidence(out / "selftest-evidence" / "selftest-scenarios.jsonl", {"scenario": name, **{k: v for k, v in result.items() if k != "scenario"}})
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "mode": "selftest",
        "synthetic_only": True,
        "real_executable_contacted": False,
        "ownership_gate_verified": prepare_gate_verified,
        "runtime_identity_binding_fingerprint": binding.fingerprint(),
        "scenarios": scenario_results,
        "evidence_dir": str((out / "selftest-evidence").resolve()),
    }


def _reject_zero_argument_facts_provider(provider_callable: Callable[..., Any]) -> None:
    """Reject a zero-argument generic facts provider before any case runs.

    The task-bound contract is ``provide(manifest_path: str) ->
    QuixBugsPreflightFacts``.  A provider whose signature is inspectable and
    declares no parameters cannot be task-bound and is rejected here; a
    non-inspectable callable is additionally wrapped by the case runner's
    per-case call gate (:meth:`OpenCodeGoCaseRunner._task_bound_facts`).
    """
    import inspect

    try:
        signature = inspect.signature(provider_callable)
    except (TypeError, ValueError):
        return
    required = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    if not required:
        raise OpenCodeGoAdapterError(
            "facts provider is not task-bound: the task-bound contract requires "
            "provide(manifest_path: str) -> QuixBugsPreflightFacts, not a zero-argument generic provider"
        )


def run_live_wire(
    manifest_path: str | Path | None,
    authorization_path: str | Path | None,
    route_evidence_json: str | Path | None,
    configuration_path: str | Path | None,
    output_root: str | Path | None,
    *,
    confirm_opencode_go_adapter: bool = False,
    quixbugs_environment_json: str | Path | None = None,
    facts_provider: str | None = None,
) -> dict[str, Any]:
    """Live wiring mode: requires every explicit gate and an actively
    validated configuration; without them no factory, transport, or process
    is created."""
    _cli_require(confirm_opencode_go_adapter is True, "live wiring requires explicit confirmation that the operator intends to configure the OpenCode Go adapter")
    manifest = _resolve_manifest(manifest_path)
    _cli_require(configuration_path is not None and Path(configuration_path).is_file(), "live wiring requires an explicit adapter-configuration path")
    configuration = load_adapter_configuration(configuration_path)
    validated = validate_adapter_configuration_structure(configuration)
    authorization, evidence = _load_authorization_and_evidence(authorization_path, route_evidence_json, manifest)
    _cli_require(evidence is not None, "live wiring requires an explicit route-evidence path")

    execution_commit = authorization["accepted_campaign_commit"]
    runner.verify_execution_repository_state(authorization)
    verdict = runner.run_route_preflight(
        manifest, authorization, lambda: evidence,
        execution_commit=execution_commit,
    )
    _cli_require(verdict.passed, f"route preflight did not pass: {verdict.failure_category}")
    observation = verdict.route_observation
    bind_adapter_configuration(validated, manifest, authorization, observation)
    binding = build_runtime_identity_binding(authorization, observation, validated)

    _cli_require(quixbugs_environment_json is not None and Path(quixbugs_environment_json).is_file(), "live wiring requires an explicit QuixBugs environment artifact")
    _cli_require(facts_provider is not None and ":" in facts_provider, "live wiring requires an explicit --facts-provider module:callable; no facts provider may be defaulted")
    try:
        environment_value = json.loads(Path(quixbugs_environment_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenCodeGoAdapterError(f"QuixBugs environment artifact is invalid: {exc}") from exc
    _cli_require(isinstance(environment_value, Mapping), "QuixBugs environment artifact must be a JSON object")
    for field in ("repository_root", "sources_parent"):
        _cli_require(isinstance(environment_value.get(field), str) and environment_value[field], f"QuixBugs environment artifact is missing {field}")
    if environment_value.get("manifest_path") is not None:
        _cli_require(isinstance(environment_value["manifest_path"], str) and environment_value["manifest_path"], "QuixBugs environment artifact manifest_path is invalid")
    module_name, _, attribute = facts_provider.partition(":")
    try:
        module = __import__(module_name, fromlist=[attribute])
        provider_callable = getattr(module, attribute)
    except Exception as exc:
        raise OpenCodeGoAdapterError(f"facts provider could not be resolved: {exc}") from exc
    _cli_require(callable(provider_callable), "facts provider must resolve to a callable")
    _reject_zero_argument_facts_provider(provider_callable)

    claim = Path(output_root)
    factory = OpenCodeGoTransportFactory(
        authorization=authorization,
        execution_commit=execution_commit,
        route_observation=observation,
        configuration=validated,
        binding=binding,
        attempt_identity=authorization["campaign_attempt_identity"],
        output_root=claim,
        ledger_path=claim / "ledger.json",
        evidence_dir=claim / "private",
    )
    environment = QuixBugsCaseEnvironment(
        repository_root=str(environment_value["repository_root"]),
        manifest_path=environment_value.get("manifest_path"),
        sources_parent=str(environment_value["sources_parent"]),
        facts_provider=provider_callable,
    )
    case_runner = OpenCodeGoCaseRunner(
        binding=binding,
        configuration=validated,
        factory=factory,
        environment=environment,
        manifest=manifest,
    )
    record = runner.run_campaign(
        manifest,
        authorization=authorization,
        output_root=claim,
        route_evidence_provider=lambda: evidence,
        transport_factory=lambda case: factory.prepare(case),
        case_runner=case_runner,
        git_state_provider=None,
    )
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "mode": "live-wire",
        "campaign": record,
        "runtime_identity_binding_fingerprint": binding.fingerprint(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed OpenCode Go execution adapter for the QuixBugs paired-pilot v2 live runner (adapter wiring only; no provider contact)")
    parser.add_argument("mode", choices=("adapter-template", "adapter-validate", "route-preflight-only", "route-capture", "operator-bundle", "selftest", "live-wire"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--adapter-config", type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--route-evidence-json", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scenario", type=str)
    parser.add_argument("--quixbugs-environment-json", type=Path)
    parser.add_argument("--facts-provider", type=str)
    parser.add_argument("--confirm-opencode-go-adapter", action="store_true")
    parser.add_argument("--runtime-model-id", type=str)
    parser.add_argument("--variant", type=str)
    parser.add_argument("--account-status", type=str)
    parser.add_argument("--subscription-entitlement-confirmed", action="store_true")
    parser.add_argument("--entitlement-evidence-reference", type=str)
    parser.add_argument("--billing-route-assertion", type=str)
    parser.add_argument("--operator-authorization-id", type=str)
    parser.add_argument("--attempt-identity", type=str)
    parser.add_argument("--valid-until", type=str)
    parser.add_argument("--python-executable", type=Path)
    parser.add_argument("--working-directory", type=Path)
    parser.add_argument("--operator-boundary-root", type=Path)
    parser.add_argument("--bundle-root", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.mode == "adapter-template":
            _cli_require(args.output is not None, "adapter-template mode requires --output")
            target = write_adapter_configuration_template(args.output)
            print(json.dumps({"template_written": str(target), "executable": False}, indent=2, sort_keys=True))
            return 0
        if args.mode == "adapter-validate":
            result = run_adapter_validate(
                args.manifest, args.adapter_config,
                authorization_path=args.authorization, route_evidence_json=args.route_evidence_json,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.mode == "route-preflight-only":
            result = run_route_preflight_only(
                args.manifest, args.authorization, args.output, args.route_evidence_json,
                configuration_path=args.adapter_config,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result.get("preflight", {}).get("passed") is True else 1
        if args.mode == "route-capture":
            _cli_require(args.output is not None, "route-capture mode requires --output (the route-evidence target inside operator/ storage)")
            result = run_route_capture(
                args.runtime_model_id, args.variant,
                account_status=args.account_status,
                subscription_entitlement_confirmed=args.subscription_entitlement_confirmed,
                entitlement_evidence_reference=args.entitlement_evidence_reference,
                billing_route_assertion=args.billing_route_assertion,
                output=args.output,
                manifest_path=args.manifest,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.mode == "operator-bundle":
            _cli_require(args.route_evidence_json is not None, "operator-bundle mode requires --route-evidence-json")
            _cli_require(args.output is not None, "operator-bundle mode requires --output (the fresh output/attempt root)")
            _cli_require(args.operator_authorization_id is not None, "operator-bundle mode requires --operator-authorization-id")
            _cli_require(args.attempt_identity is not None, "operator-bundle mode requires --attempt-identity")
            _cli_require(args.valid_until is not None, "operator-bundle mode requires --valid-until")
            _cli_require(args.entitlement_evidence_reference is not None, "operator-bundle mode requires --entitlement-evidence-reference")
            _cli_require(args.python_executable is not None, "operator-bundle mode requires --python-executable")
            _cli_require(args.working_directory is not None, "operator-bundle mode requires --working-directory")
            _cli_require(args.operator_boundary_root is not None, "operator-bundle mode requires --operator-boundary-root")
            result = run_operator_bundle(
                args.manifest, args.route_evidence_json,
                operator_authorization_id=args.operator_authorization_id,
                attempt_identity=args.attempt_identity,
                output_root=args.output,
                valid_until=args.valid_until,
                entitlement_evidence_reference=args.entitlement_evidence_reference,
                python_executable=args.python_executable,
                working_directory=args.working_directory,
                operator_boundary_root=args.operator_boundary_root,
                bundle_root=args.bundle_root,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.mode == "selftest":
            _cli_require(args.output is not None, "selftest mode requires --output")
            result = run_synthetic_selftest(args.output, scenario=args.scenario)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.mode == "live-wire":
            result = run_live_wire(
                args.manifest, args.authorization, args.route_evidence_json, args.adapter_config, args.output,
                confirm_opencode_go_adapter=args.confirm_opencode_go_adapter,
                quixbugs_environment_json=args.quixbugs_environment_json,
                facts_provider=args.facts_provider,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        return 2
    except AdapterConfigurationError as exc:
        return _cli_blocked(f"adapter configuration rejected: {exc.reason}: {exc.detail}")
    except OpenCodeGoAdapterError as exc:
        return _cli_blocked(str(exc))
    except runner.LiveRunnerError as exc:
        return _cli_blocked(f"live-runner rejection: {exc}")
    except pilot.PilotError as exc:
        return _cli_blocked(f"paired-pilot rejection: {exc}")


__all__ = [
    "ADAPTER_CONFIGURATION_FIELDS",
    "ADAPTER_IDENTITY",
    "ADAPTER_SCHEMA_VERSION",
    "TASK_BASELINE",
    "CAPTURE_RECORD_SCHEMA_VERSION",
    "CONFIGURATION_REJECTION_CODES",
    "DENIAL_FIELDS",
    "DRIFT_CATEGORIES",
    "GO_RUNTIME_ID_PREFIX",
    "HISTORICAL_ZEN_MODEL_ID",
    "OPERATOR_BUNDLES_RELATIVE_DIR",
    "OPERATOR_STORAGE",
    "ROUTE_EVIDENCE_SCHEMA_VERSION",
    "AdapterConfigurationError",
    "OpenCodeGoAdapterError",
    "OpenCodeGoCaseRunner",
    "OpenCodeGoTransport",
    "OpenCodeGoTransportFactory",
    "QuixBugsCaseEnvironment",
    "RuntimeIdentityBinding",
    "adapter_configuration_template",
    "bind_adapter_configuration",
    "build_runtime_identity_binding",
    "common_operator_boundary",
    "load_adapter_configuration",
    "main",
    "observe_bundle_execution_head",
    "run_adapter_validate",
    "run_live_wire",
    "run_operator_bundle",
    "run_route_capture",
    "run_route_preflight_only",
    "run_synthetic_selftest",
    "validate_adapter_configuration_structure",
    "write_adapter_configuration_template",
]


if __name__ == "__main__":
    raise SystemExit(main())
