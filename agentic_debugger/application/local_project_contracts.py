"""Local Project task-spec and verification-certificate contracts.

This module owns the immutable Local Project Debug session/specification
records and the portable Apply-gate proof:

- :class:`LocalProjectTaskSpec` — the canonical persisted contract for
  ``local_project_task.json`` (app pre-writes it, the source preserves it
  through terminal completion, Apply To Project / history reopen read it
  back; no secrets are persisted);
- :func:`local_project_task_spec_sha256` — the canonical semantic task
  hash used for certificate binding;
- :class:`LocalProjectVerificationCertificate` — the fail-closed
  Apply-gate proof produced by the independent verifier, bound to exactly
  one source commit and candidate patch;
- :func:`check_verification_certificate` — binds Apply to one journal
  identity, task contract, HEAD, and patch.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.session import SessionBudgets

LOCAL_PROJECT_VERIFICATION_FILE_NAME = "local_project_verification.json"
LOCAL_PROJECT_VERIFICATION_SCHEMA_VERSION = 2
LOCAL_PROJECT_VERIFICATION_AUTHORITY = (
    "agentic-debugger.independent-local-project-verifier"
)


# ---------------------------------------------------------------------------
# Immutable task/session specification contract (#9)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LocalProjectTaskSpec:
    """Canonical Local Project Debug session specification (one schema).

    ``to_mapping`` / ``from_mapping`` are the single persisted contract for
    ``local_project_task.json``: the app pre-writes it before the worker
    starts, the source preserves it through terminal completion, and Apply
    To Project / history-reopen read it back.  No secrets are persisted.

    ``project_runtime`` carries the safe V2-02
    ``ProjectRuntimeEnvironmentSpec`` mapping (spec version, non-secret
    explicit values, inherited/secret NAMES with required flags) — secret
    values never exist here.
    """

    session_id: str
    source_repo_path: str
    source_head_commit: str
    isolated_workspace_path: str
    bug_description: str
    reproduction_command: Optional[str]
    verification_command: Optional[str]
    model_runtime: Optional[str]
    budgets: SessionBudgets
    created_at_utc: str
    project_runtime: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from agentic_debugger.application.events import validate_session_id, validate_utc_timestamp

        try:
            validate_session_id(self.session_id)
        except Exception as exc:
            raise ApplicationInputError(f"invalid session id: {exc}") from exc
        if type(self.source_repo_path) is not str or not self.source_repo_path:
            raise ApplicationInputError("source_repo_path must be a non-empty string")
        if type(self.source_head_commit) is not str or not re.fullmatch(r"[0-9a-f]{40}", self.source_head_commit):
            raise ApplicationInputError("source_head_commit must be a 40-hex SHA")
        if type(self.isolated_workspace_path) is not str or not self.isolated_workspace_path:
            raise ApplicationInputError("isolated_workspace_path must be a non-empty string")
        if type(self.bug_description) is not str or not self.bug_description.strip():
            raise ApplicationInputError("bug_description must be a non-empty string")
        if len(self.bug_description.encode("utf-8")) > 4096:
            raise ApplicationInputError("bug_description exceeds the 4 KiB bound")
        if self.reproduction_command is not None:
            if type(self.reproduction_command) is not str:
                raise ApplicationInputError("reproduction_command must be a string or null")
            if self.reproduction_command and len(self.reproduction_command.encode("utf-8")) > 2048:
                raise ApplicationInputError("reproduction_command exceeds the 2 KiB bound")
        if self.verification_command is not None:
            if type(self.verification_command) is not str:
                raise ApplicationInputError("verification_command must be a string or null")
            if self.verification_command and len(self.verification_command.encode("utf-8")) > 2048:
                raise ApplicationInputError("verification_command exceeds the 2 KiB bound")
        if self.model_runtime is not None and type(self.model_runtime) is not str:
            raise ApplicationInputError("model_runtime must be a string or null")
        if type(self.budgets) is not SessionBudgets:
            raise ApplicationInputError("budgets must be a SessionBudgets")
        if not isinstance(self.project_runtime, Mapping):
            raise ApplicationInputError("project_runtime must be a mapping")
        if self.project_runtime:
            # Safe declarations only (names, flags, non-secret values);
            # secret values can never be represented here.
            try:
                from agentic_debugger.application.session_runtime import (
                    ProjectRuntimeEnvironmentSpec,
                )

                ProjectRuntimeEnvironmentSpec.from_mapping(dict(self.project_runtime))
            except Exception as exc:
                raise ApplicationInputError(
                    f"project_runtime is invalid: {exc}"
                ) from exc
        try:
            validate_utc_timestamp(self.created_at_utc)
        except Exception as exc:
            raise ApplicationInputError(f"created_at_utc is invalid: {exc}") from exc

    def to_mapping(self) -> dict:
        return {
            "session_id": self.session_id,
            "source_repo_path": self.source_repo_path,
            "source_head_commit": self.source_head_commit,
            "isolated_workspace_path": self.isolated_workspace_path,
            "bug_description": self.bug_description,
            "reproduction_command": self.reproduction_command,
            "verification_command": self.verification_command,
            "model_runtime": self.model_runtime,
            "budgets": self.budgets.to_mapping(),
            "created_at_utc": self.created_at_utc,
            "project_runtime": dict(self.project_runtime),
        }

    @staticmethod
    def from_mapping(m: dict) -> "LocalProjectTaskSpec":
        if not isinstance(m, dict):
            raise ApplicationInputError("spec mapping must be a dict")
        required = {
            "session_id",
            "source_repo_path",
            "source_head_commit",
            "isolated_workspace_path",
            "bug_description",
            "reproduction_command",
            "verification_command",
            "model_runtime",
            "budgets",
            "created_at_utc",
        }
        if set(m) != required and set(m) != (required | {"project_runtime"}):
            raise ApplicationInputError("spec mapping fields are invalid")
        # Back-compat: artifacts written before the V2-02 ingress carry no
        # ``project_runtime`` key and read back as the empty declaration.
        project_runtime = m.get("project_runtime", {})
        if project_runtime is None:
            project_runtime = {}
        return LocalProjectTaskSpec(
            session_id=m["session_id"],
            source_repo_path=m["source_repo_path"],
            source_head_commit=m["source_head_commit"],
            isolated_workspace_path=m["isolated_workspace_path"],
            bug_description=m["bug_description"],
            reproduction_command=m.get("reproduction_command"),
            verification_command=m.get("verification_command"),
            model_runtime=m.get("model_runtime"),
            budgets=SessionBudgets(**m.get("budgets", {})),
            created_at_utc=m["created_at_utc"],
            project_runtime=project_runtime,
        )


def local_project_task_spec_sha256(spec: LocalProjectTaskSpec) -> str:
    """Hash the canonical semantic task contract for certificate binding."""
    if type(spec) is not LocalProjectTaskSpec:
        raise ApplicationInputError("spec must be a LocalProjectTaskSpec")
    encoded = json.dumps(
        spec.to_mapping(),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class LocalProjectVerificationCertificate:
    """Portable Apply-gate proof produced by the independent verifier.

    The certificate intentionally contains no command text, filesystem path,
    captured output, model claim, or controller classification.  It binds the
    verifier's fail-closed result to exactly one source commit and candidate
    patch while retaining only the facts required for an owner-facing Apply
    decision.
    """

    task_id: str
    session_id: str
    task_spec_sha256: str
    source_head_commit: str
    candidate_sha256: str
    status: str
    outcome: Optional[str]
    baseline_failure_reproduced: bool
    baseline_regression_passed: bool
    post_patch_reproduction_passed: bool
    regression_passed: bool
    f2p_passed: int
    f2p_total: int
    p2p_passed: int
    p2p_total: int
    verifier_workspace_cleaned: bool
    source_repo_unchanged: bool

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not self.task_id:
            raise ApplicationInputError("verification task_id must be non-empty")
        from agentic_debugger.application.events import validate_session_id

        try:
            validate_session_id(self.session_id)
        except Exception as exc:
            raise ApplicationInputError(
                f"verification session_id is invalid: {exc}"
            ) from exc
        if not re.fullmatch(r"[0-9a-f]{64}", self.task_spec_sha256):
            raise ApplicationInputError(
                "verification task_spec_sha256 must be a 64-hex SHA"
            )
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_head_commit):
            raise ApplicationInputError(
                "verification source_head_commit must be a 40-hex SHA"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.candidate_sha256):
            raise ApplicationInputError(
                "verification candidate_sha256 must be a 64-hex SHA"
            )
        if type(self.status) is not str or not self.status:
            raise ApplicationInputError("verification status must be non-empty")
        if self.outcome is not None and type(self.outcome) is not str:
            raise ApplicationInputError("verification outcome must be a string or null")
        for name in (
            "baseline_failure_reproduced",
            "baseline_regression_passed",
            "post_patch_reproduction_passed",
            "regression_passed",
            "verifier_workspace_cleaned",
            "source_repo_unchanged",
        ):
            if type(getattr(self, name)) is not bool:
                raise ApplicationInputError(f"verification {name} must be boolean")
        for name in ("f2p_passed", "f2p_total", "p2p_passed", "p2p_total"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ApplicationInputError(
                    f"verification {name} must be a non-negative integer"
                )
        if self.f2p_passed > self.f2p_total or self.p2p_passed > self.p2p_total:
            raise ApplicationInputError("verification passed counts exceed totals")

    @property
    def permits_apply(self) -> bool:
        """Whether this exact certificate proves a resolved, clean result."""
        return bool(
            self.status == "COMPLETED"
            and self.outcome == "RESOLVED"
            and self.baseline_failure_reproduced
            and self.baseline_regression_passed
            and self.post_patch_reproduction_passed
            and self.regression_passed
            and self.f2p_total == 1
            and self.f2p_passed == 1
            and self.p2p_total == 1
            and self.p2p_passed == 1
            and self.verifier_workspace_cleaned
            and self.source_repo_unchanged
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": LOCAL_PROJECT_VERIFICATION_SCHEMA_VERSION,
            "authority": LOCAL_PROJECT_VERIFICATION_AUTHORITY,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "task_spec_sha256": self.task_spec_sha256,
            "source_head_commit": self.source_head_commit,
            "candidate_sha256": self.candidate_sha256,
            "status": self.status,
            "outcome": self.outcome,
            "baseline_failure_reproduced": self.baseline_failure_reproduced,
            "baseline_regression_passed": self.baseline_regression_passed,
            "post_patch_reproduction_passed": self.post_patch_reproduction_passed,
            "regression_passed": self.regression_passed,
            "f2p_passed": self.f2p_passed,
            "f2p_total": self.f2p_total,
            "p2p_passed": self.p2p_passed,
            "p2p_total": self.p2p_total,
            "verifier_workspace_cleaned": self.verifier_workspace_cleaned,
            "source_repo_unchanged": self.source_repo_unchanged,
        }

    @staticmethod
    def from_mapping(value: Mapping[str, Any]) -> "LocalProjectVerificationCertificate":
        if not isinstance(value, Mapping):
            raise ApplicationInputError("verification certificate must be a mapping")
        required = {
            "schema_version",
            "authority",
            "task_id",
            "session_id",
            "task_spec_sha256",
            "source_head_commit",
            "candidate_sha256",
            "status",
            "outcome",
            "baseline_failure_reproduced",
            "baseline_regression_passed",
            "post_patch_reproduction_passed",
            "regression_passed",
            "f2p_passed",
            "f2p_total",
            "p2p_passed",
            "p2p_total",
            "verifier_workspace_cleaned",
            "source_repo_unchanged",
        }
        if set(value) != required:
            raise ApplicationInputError("verification certificate fields are invalid")
        if value["schema_version"] != LOCAL_PROJECT_VERIFICATION_SCHEMA_VERSION:
            raise ApplicationInputError("unsupported verification certificate version")
        if value["authority"] != LOCAL_PROJECT_VERIFICATION_AUTHORITY:
            raise ApplicationInputError("verification certificate authority is invalid")
        return LocalProjectVerificationCertificate(
            task_id=value["task_id"],
            session_id=value["session_id"],
            task_spec_sha256=value["task_spec_sha256"],
            source_head_commit=value["source_head_commit"],
            candidate_sha256=value["candidate_sha256"],
            status=value["status"],
            outcome=value["outcome"],
            baseline_failure_reproduced=value["baseline_failure_reproduced"],
            baseline_regression_passed=value["baseline_regression_passed"],
            post_patch_reproduction_passed=value["post_patch_reproduction_passed"],
            regression_passed=value["regression_passed"],
            f2p_passed=value["f2p_passed"],
            f2p_total=value["f2p_total"],
            p2p_passed=value["p2p_passed"],
            p2p_total=value["p2p_total"],
            verifier_workspace_cleaned=value["verifier_workspace_cleaned"],
            source_repo_unchanged=value["source_repo_unchanged"],
        )


def check_verification_certificate(
    certificate: LocalProjectVerificationCertificate,
    *,
    expected_task_id: str,
    expected_session_id: str,
    expected_task_spec_sha256: str,
    expected_head: str,
    patch_text: str,
) -> tuple[bool, str]:
    """Bind Apply to one journal identity, task contract, HEAD, and patch."""
    if type(certificate) is not LocalProjectVerificationCertificate:
        return False, "independent verification certificate is malformed"
    if certificate.task_id != expected_task_id:
        return False, "verification certificate belongs to a different task"
    if certificate.session_id != expected_session_id:
        return False, "verification certificate belongs to a different session"
    if certificate.task_spec_sha256 != expected_task_spec_sha256:
        return False, "verification certificate belongs to a different task contract"
    if certificate.source_head_commit != expected_head:
        return False, "verification certificate belongs to a different source commit"
    candidate_sha256 = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
    if certificate.candidate_sha256 != candidate_sha256:
        return False, "verification certificate belongs to a different candidate patch"
    if not certificate.permits_apply:
        return False, "candidate is not independently verified as RESOLVED"
    return True, "verified"
