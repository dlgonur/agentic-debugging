"""Public versus verifier-private views of official SWE-rebench rows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from agentic_debugger.swerebench.authority import (
    CANONICAL_DATASET_ID,
    CANONICAL_DATASET_REVISION,
    CANONICAL_PARQUET_SHA256,
    default_parquet_blob,
    default_parquet_path,
)
from agentic_debugger.swerebench.hashing import require_sha256, sha256_text

PUBLIC_ROW_FIELDS = (
    "instance_id",
    "repo",
    "base_commit",
    "problem_statement",
    "language",
    "license",
    "created_at",
)

PRIVATE_ROW_FIELDS = (
    "patch",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "interface",
    "pr_description",
    "image_name",
    "install_config",
    "meta",
)


class SweRebenchRecordError(ValueError):
    """Official instance metadata is missing or not isolatable."""


@dataclass(frozen=True)
class PublicInstanceRecord:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    language: str
    license: str
    created_at: str
    problem_statement_sha256: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "problem_statement": self.problem_statement,
            "language": self.language,
            "license": self.license,
            "created_at": self.created_at,
            "problem_statement_sha256": self.problem_statement_sha256,
        }


@dataclass(frozen=True)
class VerifierPrivateRecord:
    instance_id: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    test_cmd: str | None
    image_name: str | None
    python_version: str | None
    has_gold_patch: bool
    has_test_patch: bool
    gold_patch_sha256: str | None
    test_patch_sha256: str | None

    def to_safe_mapping(self) -> dict[str, Any]:
        """Operator/readiness view. Never includes gold or test patch bodies."""

        return {
            "instance_id": self.instance_id,
            "fail_to_pass_count": len(self.fail_to_pass),
            "pass_to_pass_count": len(self.pass_to_pass),
            "test_cmd_present": bool(self.test_cmd),
            "image_name_present": bool(self.image_name),
            "python_version": self.python_version,
            "has_gold_patch": self.has_gold_patch,
            "has_test_patch": self.has_test_patch,
            "gold_patch_sha256": self.gold_patch_sha256,
            "test_patch_sha256": self.test_patch_sha256,
        }


@dataclass(frozen=True)
class OfficialInstanceBundle:
    public: PublicInstanceRecord
    private: VerifierPrivateRecord
    _gold_patch: str
    _test_patch: str
    _fail_to_pass: tuple[str, ...]
    _pass_to_pass: tuple[str, ...]
    _test_cmd: str | None
    _install_config: dict[str, Any]
    _image_name: str | None

    def gold_patch(self) -> str:
        return self._gold_patch

    def test_patch(self) -> str:
        return self._test_patch

    def hidden_tests(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return self._fail_to_pass, self._pass_to_pass

    def test_cmd(self) -> str | None:
        return self._test_cmd

    def install_config(self) -> dict[str, Any]:
        return dict(self._install_config)

    def image_name(self) -> str | None:
        return self._image_name


def resolve_parquet_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    snapshot = default_parquet_path()
    if snapshot.is_file():
        return snapshot
    blob = default_parquet_blob()
    if blob.is_file():
        return blob
    raise SweRebenchRecordError(
        "official SWE-rebench V2 parquet is not available locally; "
        f"expected {snapshot} or {blob}"
    )


def _as_str_tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return tuple(str(item) for item in parsed if str(item))
        return (text,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item))
    raise SweRebenchRecordError(f"{label} has unsupported type {type(value)!r}")


def _cell(row: Mapping[str, Any], name: str) -> Any:
    if name in row:
        return row[name]
    return None


def _bundle_from_row(row: Mapping[str, Any]) -> OfficialInstanceBundle:
    instance_id = str(_cell(row, "instance_id") or "")
    repo = str(_cell(row, "repo") or "")
    base_commit = str(_cell(row, "base_commit") or "")
    problem = str(_cell(row, "problem_statement") or "")
    language = str(_cell(row, "language") or "")
    license_name = str(_cell(row, "license") or "")
    created_at = str(_cell(row, "created_at") or "")
    if not instance_id or not repo or not base_commit or not problem:
        raise SweRebenchRecordError(
            f"incomplete public fields for {instance_id or '<missing>'}"
        )
    if language and language.lower() != "python":
        raise SweRebenchRecordError(
            f"{instance_id} language is {language!r}, not python"
        )
    gold = str(_cell(row, "patch") or "")
    test_patch = str(_cell(row, "test_patch") or "")
    f2p = _as_str_tuple(_cell(row, "FAIL_TO_PASS"), "FAIL_TO_PASS")
    p2p = _as_str_tuple(_cell(row, "PASS_TO_PASS"), "PASS_TO_PASS")
    install = _cell(row, "install_config") or {}
    test_cmd = None
    python_version = None
    if isinstance(install, Mapping):
        raw_cmd = install.get("test_cmd")
        if isinstance(raw_cmd, str) and raw_cmd.strip():
            test_cmd = raw_cmd.strip()
        raw_py = install.get("docker_specs")
        if isinstance(raw_py, Mapping):
            version = raw_py.get("python_version")
            if isinstance(version, str) and version.strip():
                python_version = version.strip()
    image_name = _cell(row, "image_name")
    public = PublicInstanceRecord(
        instance_id=instance_id,
        repo=repo,
        base_commit=base_commit,
        problem_statement=problem,
        language=language or "python",
        license=license_name,
        created_at=created_at,
        problem_statement_sha256=sha256_text(problem),
    )
    private = VerifierPrivateRecord(
        instance_id=instance_id,
        fail_to_pass=f2p,
        pass_to_pass=p2p,
        test_cmd=test_cmd,
        image_name=str(image_name) if image_name else None,
        python_version=python_version,
        has_gold_patch=bool(gold.strip()),
        has_test_patch=bool(test_patch.strip()),
        gold_patch_sha256=sha256_text(gold) if gold.strip() else None,
        test_patch_sha256=sha256_text(test_patch) if test_patch.strip() else None,
    )
    return OfficialInstanceBundle(
        public=public,
        private=private,
        _gold_patch=gold,
        _test_patch=test_patch,
        _fail_to_pass=f2p,
        _pass_to_pass=p2p,
        _test_cmd=test_cmd,
        _install_config=dict(install) if isinstance(install, Mapping) else {},
        _image_name=str(image_name) if image_name else None,
    )


def load_official_bundles(
    instance_ids: Iterable[str],
    *,
    parquet_path: Path | None = None,
    verify_sha256: bool = True,
) -> dict[str, OfficialInstanceBundle]:
    wanted = list(instance_ids)
    if not wanted:
        return {}
    path = resolve_parquet_path(parquet_path)
    if verify_sha256:
        require_sha256(path, CANONICAL_PARQUET_SHA256, label="SWE-rebench V2 parquet")
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SweRebenchRecordError(
            "pyarrow is required to read the official SWE-rebench parquet"
        ) from exc

    wanted_set = set(wanted)
    table = pq.read_table(
        path,
        columns=list(dict.fromkeys(PUBLIC_ROW_FIELDS + PRIVATE_ROW_FIELDS)),
        filters=[("instance_id", "in", list(wanted_set))],
    )
    found: dict[str, OfficialInstanceBundle] = {}
    for row in table.to_pylist():
        instance_id = row.get("instance_id")
        if instance_id in wanted_set and instance_id not in found:
            found[str(instance_id)] = _bundle_from_row(row)
    missing = [item for item in wanted if item not in found]
    if missing:
        raise SweRebenchRecordError(
            "official parquet is missing instance ids: " + ", ".join(missing[:8])
        )
    return found


def parquet_identity() -> dict[str, str]:
    return {
        "dataset_id": CANONICAL_DATASET_ID,
        "dataset_revision": CANONICAL_DATASET_REVISION,
        "parquet_sha256": CANONICAL_PARQUET_SHA256,
    }
