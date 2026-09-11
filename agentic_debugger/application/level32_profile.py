"""Level-32 operator vocabulary, model profiles, and evidence helpers.

This module owns the static layer beneath the Level-32 operator worker:
the ladder task vocabulary (``LEVEL32_TASK_ID``, ``LADDER_TASKS``,
metadata/options/lookup), the Level-32 model profiles and next-treatment
resolution, the operator-process protocol with the default process
factory, the stateless evidence helpers (bounded safe text, hashing,
capped writes, official-verifier counts), and the
``build_level32_spec`` session-spec construction.

The stateful :class:`~agentic_debugger.application.level32.Level32OperatorWorker`
(one process/lifecycle authority) stays in
:mod:`agentic_debugger.application.level32`.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Tuple

from agentic_debugger.application.events import SourceKind, contains_credential_shape
from agentic_debugger.application.session import ExecutionSourceSpec, SessionBudgets, SessionSpec

LEVEL32_TASK_ID = "audreyr__cookiecutter-967"
LEVEL32_OPERATOR_SCRIPT = "scripts/run_cookiecutter_967_pdb_proof.py"

@dataclass(frozen=True)
class LadderTaskMetadata:
    """One canonical product label set for an accepted ladder rung."""

    title: str
    task_id: str
    debugger: str
    treatment: str
    evaluation: str


LADDER_TASKS: tuple[LadderTaskMetadata, ...] = (
    LadderTaskMetadata(
        "Level 6/100", "pdb-required-boundary-006", "Exact PDB required",
        "Accepted Level-6 contract", "Independent verifier",
    ),
    LadderTaskMetadata(
        "Level 12/100", "pdb-required-caller-callee-007", "Exact PDB required",
        "Accepted Level-12 contract", "Independent verifier",
    ),
    LadderTaskMetadata(
        "Level 18/100", "pdb-required-multistage-units-008", "Exact PDB required",
        "Accepted Level-18 contract", "Independent verifier",
    ),
    LadderTaskMetadata(
        "Level 32/100 — Cookiecutter #967", LEVEL32_TASK_ID, "Exact PDB required",
        "Frozen Level-32", "Official SWE-rebench",
    ),
)
LADDER_TASK_IDS = frozenset(item.task_id for item in LADDER_TASKS)


def ladder_task_options() -> tuple[tuple[str, str], ...]:
    """The four accepted product rungs, retaining their canonical IDs."""

    return tuple((f"{item.title} · {item.task_id}", item.task_id) for item in LADDER_TASKS)


def ladder_task_metadata(task_id: str) -> LadderTaskMetadata:
    """Return the immutable metadata for one accepted rung."""

    for item in LADDER_TASKS:
        if item.task_id == task_id:
            return item
    raise KeyError(task_id)


def is_ladder_task(task_id: Optional[str]) -> bool:
    return task_id in LADDER_TASK_IDS


@dataclass(frozen=True)
class Level32ModelProfile:
    """Safe UI projection of one canonical Ollama Cloud profile."""

    alias: str
    display_name: str
    readiness: str
    transport_config_fingerprint: str

    @property
    def profile_id(self) -> str:
        return self.alias


def level32_model_profiles() -> Tuple[Level32ModelProfile, ...]:
    """Return only canonical, live-verified Level-32-eligible profiles.

    Importing this registry is local and read-only.  In particular, this
    function never calls Ollama or sends an inference request; the operator's
    own preflight remains the final availability gate at Start time.
    """

    try:
        from scripts.ollama_cloud_command_adapter import (
            CLOUD_MODELS,
            is_treatment_eligible,
            transport_config_fingerprint,
        )
    except ModuleNotFoundError as exc:
        # The research operator lives in the source checkout, outside the
        # installable package.  A wheel-installed application must still open
        # cleanly; it simply omits operator-only cloud profiles.  Missing
        # dependencies *inside* an available adapter remain real errors.
        if exc.name not in {
            "scripts",
            "scripts.ollama_cloud_command_adapter",
        }:
            raise
        return ()

    return tuple(
        Level32ModelProfile(
            alias=spec.local_alias,
            display_name=spec.upstream_model,
            readiness=spec.readiness,
            transport_config_fingerprint=transport_config_fingerprint(spec),
        )
        for spec in sorted(CLOUD_MODELS.values(), key=lambda item: item.local_alias)
        if is_treatment_eligible(spec)
    )


ollama_cloud_model_profiles = level32_model_profiles


def next_level32_treatment(repository_root: str | Path, model: str) -> tuple[int, str, Path]:
    """Allocate the next unused revision using the operator's identity rules."""

    import scripts.run_cookiecutter_967_pdb_proof as operator

    revision = operator.next_unused_treatment_revision(repository_root, model)
    treatment_id = operator._treatment_id_for_model(model, revision)
    output_dir = (
        Path(repository_root).resolve()
        / operator._default_output_dir_for_model(model, revision)
    ).resolve()
    if output_dir.exists():
        raise RuntimeError(f"Level-32 treatment output already exists: {output_dir}")
    return revision, treatment_id, output_dir


class _OperatorProcess(Protocol):
    pid: int
    returncode: Optional[int]

    def communicate(self) -> tuple[str, str]: ...
    def poll(self) -> Optional[int]: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


ProcessFactory = Callable[..., _OperatorProcess]


def _default_process_factory(*args: Any, **kwargs: Any) -> _OperatorProcess:
    return subprocess.Popen(*args, **kwargs)  # type: ignore[return-value]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_text(value: Any, maximum: int = 4000) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    if len(text) > maximum:
        text = text[: maximum - 3] + "..."
    return "[redacted sensitive subprocess output]" if contains_credential_shape(text) else text


def _write_text(path: Path, value: Any, *, maximum: int = 8192) -> None:
    path.write_text(_safe_text(value, maximum), encoding="utf-8", newline="\n")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _official_verifier_counts(
    official: Mapping[str, Any],
) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """Project only validated, redacted aggregate counts from official output."""

    if official.get("official_test_execution_proven") is not True:
        return None, None, None, None

    values: dict[str, int] = {}
    for name in (
        "fail_to_pass_total",
        "fail_to_pass_passed",
        "pass_to_pass_total",
        "pass_to_pass_failed",
    ):
        value = official.get(name)
        if type(value) is not int or isinstance(value, bool) or value < 0:
            return None, None, None, None
        values[name] = value
    if values["fail_to_pass_passed"] > values["fail_to_pass_total"]:
        return None, None, None, None
    if values["pass_to_pass_failed"] > values["pass_to_pass_total"]:
        return None, None, None, None
    return (
        values["fail_to_pass_passed"],
        values["fail_to_pass_total"],
        values["pass_to_pass_total"] - values["pass_to_pass_failed"],
        values["pass_to_pass_total"],
    )


def build_level32_spec(model_alias: str) -> SessionSpec:
    return SessionSpec(
        task_id=LEVEL32_TASK_ID,
        source=ExecutionSourceSpec(
            kind=SourceKind.LEVEL32_OPERATOR,
            task_id=LEVEL32_TASK_ID,
            policy="exact-pdb-level32-frozen",
            model_config_ref=model_alias,
        ),
        budgets=SessionBudgets(),
    )
