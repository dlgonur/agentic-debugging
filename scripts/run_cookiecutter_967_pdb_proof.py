"""Run one PDB-required GPT-OSS repair and the private SWE-rebench verifier.

This is intentionally a one-instance operator path.  The model sees the public
issue, one public reproduction, and the pinned pre-fix production source.  The
official SWE-rebench test patch and test identities are loaded only after the
model run and are deleted with the verifier-private workspace.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from agentic_debugger.demo.catalog import (
    DemoScenario,
    LocalizationClaim,
    ReferenceRepair,
    RuntimeProbe,
)
from agentic_debugger.agent.proof_gate import PROOF_ROLE_SELECTION_POLICY
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import (
    DIRECTIVE_NORMALIZATION_POLICY,
    DIRECTIVE_NORMALIZATION_POLICY_ID,
    DIRECTIVE_NORMALIZATION_SCHEMA_VERSION,
    PDB_BREAKPOINT_SELECTION_POLICY,
    PDB_BREAKPOINT_SELECTION_POLICY_ID,
    PDB_BREAKPOINT_SELECTION_SCHEMA_VERSION,
    JsonlCommandTransport,
    LiveCaseStatus,
    LiveModelConfig,
    LiveRunLimits,
    LiveTreatmentBudget,
    run_live_case,
)
from agentic_debugger.events.replay import replay_events
from agentic_debugger.application.events import contains_credential_shape
from agentic_debugger.runtime.patcher import (
    CanonicalPatchArtifact,
    materialize_and_canonicalize_patch,
)


INSTANCE_ID = "audreyr__cookiecutter-967"
TASK_ID = "swr-audreyr-cookiecutter-967-pdb"
BASE_COMMIT = "ba5ba8c78e97f5dc7fb4e16c588d7be037e6e5e7"
IMAGE = "docker.io/swerebenchv2/audreyr-cookiecutter:967-ba5ba8c"
IMAGE_ID = "sha256:0bad37ac1e0a6d692a9ef417c05753b5ad45dfa8c32fd52b0f3ecabf722af8eb"
SOURCE_SHA256 = "71de7ea915fee31e4e9104b89259deaa1c83ae0c8d3cbe249c878f5adbd5f6ee"
DATASET_REVISION = "475dd5e8703bb5fb22dd3c60b5d038b019eba1e0"
PARQUET_SHA256 = "0e0bf9355f892ad74ae98d4e1c404f39fd6654a8e351ee3e6ab162e4a64cd3ad"
EVALUATOR_COMMIT = "c71902a8cf8d2b725f63d51f199f4d3e56f68d2d"
MODEL = "gpt-oss:20b-cloud"
DEFAULT_MODEL = MODEL
EXPECTED_OLLAMA_VERSION = "0.32.15"
IMAGE_GATE_OBSERVABILITY_POLICY_ID = "evaluator-image-gate-observability-v1"
IMAGE_GATE_EVIDENCE_SCHEMA_VERSION = "level32-image-verification-v1"
IMAGE_INSPECT_FORMAT = (
    "{\"id\":{{json .Id}},\"repo_tags\":{{json .RepoTags}},"
    "\"repo_digests\":{{json .RepoDigests}},\"created\":{{json .Created}},"
    "\"os\":{{json .Os}},\"architecture\":{{json .Architecture}},"
    "\"labels\":{{json .Config.Labels}}}"
)
IMAGE_DIAGNOSTIC_LIMIT = 2048
CANDIDATE_TRANSPORT_ID = "workspace-derived-official-git-diff-v1"
FROZEN_TREATMENT_ID_LEGACY = "pdb-capability-level32-cookiecutter-967-v3"
TREATMENT_ID = f"pdb-capability-level32-cookiecutter-967-{CANDIDATE_TRANSPORT_ID}"
# New treatments allow one bounded transport/directive retry. Historical v1
# artifacts retain their original zero-retry identity; this changed budget is
# intentionally captured by the treatment fingerprint for fresh reruns.
LEVEL32_TREATMENT_BUDGET = LiveTreatmentBudget(max_retries=1)
PREPARED_TREATMENT_REVISIONS = {
    "qwen3.5:cloud": 2,
    "kimi-k2.6:cloud": 7,
    "kimi-k2.7-code:cloud": 4,
    "kimi-k3:cloud": 4,
    "gemma4:31b-cloud": 3,
}


class _ProgressWriter:
    """Optional observer-only JSONL channel for the application worker."""

    def __init__(self, path: str | None) -> None:
        self._path = Path(path).resolve() if path else None
        self._last: tuple[str, str | None] | None = None

    def emit(self, stage: str, detail: str | None = None) -> None:
        if self._path is None:
            return
        current = (stage, detail)
        if current == self._last:
            return
        self._last = current
        payload: dict[str, Any] = {"schema_version": "operator-progress-v1", "stage": stage}
        if detail:
            payload["detail"] = detail
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(payload, sort_keys=True) + "\n")
                stream.flush()
        except OSError:
            # Observability is fail-open. The authoritative operator must not
            # change its scientific result because the UI is unavailable.
            return


def _load_ollama_adapter_module(module_name: str) -> Any:
    """Load the sibling adapter safely when the script is run by path."""

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        module_name,
        str(Path(__file__).with_name("ollama_cloud_command_adapter.py")),
    )
    if spec is None or spec.loader is None:
        raise ImportError("Ollama Cloud adapter module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves the defining module through sys.modules while the
    # module executes under Python 3.14 and later.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _treatment_id_for_model(model: str, revision: int = 1) -> str:
    if type(revision) is not int or revision < 1:
        raise ProofError("treatment revision must be a positive integer")
    if model == "gpt-oss:20b-cloud" and revision == 1:
        return TREATMENT_ID
    slug = model.replace(":", "-").replace("/", "-")
    if model == "gpt-oss:120b-cloud":
        slug = "gpt-oss-120b"
    return f"pdb-capability-level32-cookiecutter-967-{slug}-v{revision}-{CANDIDATE_TRANSPORT_ID}"


def next_unused_treatment_revision(repository_root: str | Path, model: str) -> int:
    """Return the next unused Level-32 revision without touching a run."""

    root = Path(repository_root).resolve()
    slug = model.replace(":", "-").replace("/", "-")
    legacy_slug = (
        "gpt-oss-120b"
        if model == "gpt-oss:120b-cloud"
        else "gpt-oss"
        if model == "gpt-oss:20b-cloud"
        else slug.removesuffix("-cloud")
    )
    prefixes = {
        f"level32-cookiecutter-967-{slug}-v",
        f"level32-cookiecutter-967-{legacy_slug}-v",
    }
    highest = 0
    experiment_root = root / "experiments" / "pdb_capability_ladder"
    if experiment_root.is_dir():
        for entry in experiment_root.iterdir():
            if not entry.is_dir():
                continue
            for prefix in prefixes:
                if entry.name.startswith(prefix):
                    suffix = entry.name[len(prefix):].split("-", 1)[0]
                    if suffix.isdigit():
                        highest = max(highest, int(suffix))
                    break
    return highest + 1


def _resolve_model_or_fail(model: str) -> tuple[str, Any]:
    try:
        from scripts.ollama_cloud_command_adapter import resolve_cloud_model as _resolve
    except ImportError:
        mod = _load_ollama_adapter_module("ollama_cloud_command_adapter")
        _resolve = mod.resolve_cloud_model  # type: ignore[assignment]
    resolved = _resolve(model)
    return model, resolved


def _require_treatment_eligible(model: str) -> Any:
    try:
        from scripts.ollama_cloud_command_adapter import is_treatment_eligible as _eligible
        from scripts.ollama_cloud_command_adapter import resolve_cloud_model as _rc
    except ImportError:
        mod2 = _load_ollama_adapter_module("ollama_cloud_command_adapter2")
        _eligible = mod2.is_treatment_eligible  # type: ignore[assignment]
        _rc = mod2.resolve_cloud_model  # type: ignore[assignment]
    resolved = _rc(model)
    if not _eligible(resolved):
        hint = (
            f"model {model!r} is not yet live-transport eligible for Level-32. "
            f"Readiness={resolved.readiness!r}. "
            "Qualify it first with: python -m agentic_debugger.evaluation.transport_qualification "
            f"--endpoint http://127.0.0.1:11434/api --model {model} --confirm-live --json"
        )
        raise ProofError(hint)
    return resolved


def _default_output_dir_for_model(model: str, revision: int = 1) -> Path:
    slug = model.replace(":", "-").replace("/", "-")
    return Path(f"experiments/pdb_capability_ladder/level32-cookiecutter-967-{slug}-v{revision}")
F2P_COUNT = 5
P2P_COUNT = 9
PUBLIC_F2P = "tests/test_pdb_public_config_merge.py::test_builtin_abbreviations_survive_custom_config"
PUBLIC_P2P = "tests/test_pdb_public_config_merge.py::test_scalar_override_preserves_other_defaults"
INTEGRITY_GATE_SCHEMA_VERSION = "level32-integrity-gate-v1"


class ProofError(RuntimeError):
    pass


class ImageVerificationError(ProofError):
    """Fail-closed image-gate error carrying bounded audit evidence."""

    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_readonly_tree(path: Path) -> None:
    """Remove a task-owned Docker export whose Git objects may be read-only."""

    def make_writable(function: Any, target: str, _error: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onexc=make_writable)


def _run(argv: list[str], *, cwd: Path | None = None, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProofError(f"command failed before completion: {argv[0]} ({type(exc).__name__})") from exc


def _parquet_path() -> Path:
    return (
        Path.home()
        / "AppData/Local/agentic-debugging/swe_rebench_v2_census_cache"
        / "datasets--nebius--SWE-rebench-V2/snapshots"
        / DATASET_REVISION
        / "data/train-00000-of-00001.parquet"
    )


def _load_official_row() -> dict[str, Any]:
    path = _parquet_path()
    if not path.is_file() or _sha256(path) != PARQUET_SHA256:
        raise ProofError("pinned SWE-rebench parquet is missing or has the wrong SHA-256")
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ProofError("pyarrow is required for the pinned task row") from exc
    table = pq.read_table(path, filters=[("instance_id", "=", INSTANCE_ID)])
    rows = table.to_pylist()
    if len(rows) != 1:
        raise ProofError("pinned SWE-rebench task row is not unique")
    row = dict(rows[0])
    checks = {
        "base_commit": BASE_COMMIT,
        "image_name": IMAGE,
        "instance_id": INSTANCE_ID,
        "repo": "audreyr/cookiecutter",
    }
    if any(row.get(key) != value for key, value in checks.items()):
        raise ProofError("pinned SWE-rebench task identity does not match the contract")
    if len(row.get("FAIL_TO_PASS") or ()) != F2P_COUNT or len(row.get("PASS_TO_PASS") or ()) != P2P_COUNT:
        raise ProofError("official hidden-test counts do not match the frozen contract")
    if not row.get("patch") or not row.get("test_patch"):
        raise ProofError("official verifier row is incomplete")
    return row


def _bounded_diagnostic(value: str | None) -> str:
    text = (value or "").replace("\x00", "\\u0000")
    text = re.sub(
        r"(?i)(authorization|password|token|secret|api[_-]?key)(\s*[:=]\s*)\S+",
        r"\1\2<redacted>",
        text,
    )
    if len(text) > IMAGE_DIAGNOSTIC_LIMIT:
        return text[:IMAGE_DIAGNOSTIC_LIMIT] + "...[truncated]"
    return text


def _bounded_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth >= 2:
        return _bounded_diagnostic(str(value))
    if isinstance(value, dict):
        items = list(value.items())[:32]
        return {str(key): _bounded_metadata(item, depth=depth + 1) for key, item in items}
    if isinstance(value, list):
        return [_bounded_metadata(item, depth=depth + 1) for item in value[:32]]
    if isinstance(value, str):
        return _bounded_diagnostic(value)
    return value


def _docker_context() -> str | None:
    configured = os.environ.get("DOCKER_CONTEXT")
    try:
        result = _run(["docker", "context", "show"], timeout=10)
    except ProofError:
        return configured or None
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()[0]
    return configured or None


def _image_evidence(
    *,
    category: str,
    command: list[str],
    result: Any = None,
    actual_image_id: str | None = None,
    image_metadata: dict[str, Any] | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    metadata = image_metadata or {}
    return {
        "schema_version": IMAGE_GATE_EVIDENCE_SCHEMA_VERSION,
        "observability_policy_id": IMAGE_GATE_OBSERVABILITY_POLICY_ID,
        "category": category,
        "image": IMAGE,
        "expected_image_id": IMAGE_ID,
        "actual_image_id": actual_image_id,
        "command": command,
        "return_code": getattr(result, "returncode", None),
        "stdout": _bounded_diagnostic(getattr(result, "stdout", None)),
        "stderr": _bounded_diagnostic(getattr(result, "stderr", None)),
        "detail": detail,
        "repo_tags": _bounded_metadata(metadata.get("repo_tags")),
        "repo_digests": _bounded_metadata(metadata.get("repo_digests")),
        "created": _bounded_metadata(metadata.get("created")),
        "os": _bounded_metadata(metadata.get("os")),
        "architecture": _bounded_metadata(metadata.get("architecture")),
        "labels": _bounded_metadata(metadata.get("labels")),
        "docker_context": _docker_context(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider_model_execution_started": False,
    }


def _inspect_failure_category(result: Any) -> str:
    diagnostic = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}".lower()
    missing_markers = (
        "no such image",
        "unable to find image",
        "image not found",
        "reference does not exist",
    )
    unavailable_markers = (
        "cannot connect to the docker daemon",
        "is the docker daemon running",
        "permission denied while trying to connect",
        "docker daemon is not running",
        "context deadline exceeded",
    )
    if any(marker in diagnostic for marker in missing_markers):
        return "IMAGE_ABSENT"
    if any(marker in diagnostic for marker in unavailable_markers):
        return "DOCKER_UNAVAILABLE"
    return "IMAGE_INSPECTION_FAILED"


def _verify_image() -> dict[str, Any]:
    command = ["docker", "image", "inspect", IMAGE, "--format", IMAGE_INSPECT_FORMAT]
    try:
        inspected = _run(command, timeout=20)
    except ProofError as exc:
        evidence = _image_evidence(
            category="DOCKER_UNAVAILABLE",
            command=command,
            detail=str(exc),
        )
        raise ImageVerificationError(
            "pinned SWE-rebench Docker image inspection was unavailable",
            evidence,
        ) from exc

    if inspected.returncode != 0:
        category = _inspect_failure_category(inspected)
        evidence = _image_evidence(category=category, command=command, result=inspected)
        messages = {
            "IMAGE_ABSENT": "pinned SWE-rebench Docker image is absent",
            "DOCKER_UNAVAILABLE": "Docker was unavailable during pinned image inspection",
            "IMAGE_INSPECTION_FAILED": "pinned SWE-rebench Docker image inspection failed",
        }
        raise ImageVerificationError(messages[category], evidence)

    raw = inspected.stdout.strip()
    metadata: dict[str, Any]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        metadata = parsed
        actual_image_id = parsed.get("id")
    elif raw.startswith("sha256:") and "\n" not in raw:
        # Keep compatibility with the old one-field inspect output in tests or
        # an older operator wrapper, while production retains full provenance.
        metadata = {"id": raw}
        actual_image_id = raw
    else:
        evidence = _image_evidence(
            category="IMAGE_INSPECTION_INVALID",
            command=command,
            result=inspected,
            detail="Docker inspect returned empty or malformed structured output",
        )
        raise ImageVerificationError(
            "pinned image inspection returned malformed output",
            evidence,
        )

    if not isinstance(actual_image_id, str) or not actual_image_id:
        evidence = _image_evidence(
            category="IMAGE_INSPECTION_INVALID",
            command=command,
            result=inspected,
            image_metadata=metadata,
            detail="Docker inspect did not provide an image ID",
        )
        raise ImageVerificationError(
            "pinned image inspection did not provide an image ID",
            evidence,
        )

    if actual_image_id != IMAGE_ID:
        evidence = _image_evidence(
            category="IMAGE_IDENTITY_MISMATCH",
            command=command,
            result=inspected,
            actual_image_id=actual_image_id,
            image_metadata=metadata,
        )
        raise ImageVerificationError(
            "pinned SWE-rebench Docker image has the wrong identity",
            evidence,
        )

    evidence = _image_evidence(
        category="IMAGE_VERIFIED",
        command=command,
        result=inspected,
        actual_image_id=actual_image_id,
        image_metadata=metadata,
    )
    commit_command = [
        "docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh",
        IMAGE, "-lc", "git rev-parse HEAD && test -z \"$(git status --short)\"",
    ]
    try:
        commit = _run(commit_command, timeout=30)
    except ProofError as exc:
        evidence.update(
            {
                "category": "IMAGE_BASE_CHECK_FAILED",
                "base_check_command": commit_command,
                "base_check_detail": str(exc),
            }
        )
        raise ImageVerificationError(
            "pinned image base-revision check was unavailable",
            evidence,
        ) from exc
    evidence.update(
        {
            "base_check_command": commit_command,
            "base_check_return_code": commit.returncode,
            "base_check_stdout": _bounded_diagnostic(commit.stdout),
            "base_check_stderr": _bounded_diagnostic(commit.stderr),
        }
    )
    if commit.returncode != 0 or commit.stdout.splitlines()[:1] != [BASE_COMMIT]:
        evidence["category"] = "IMAGE_BASE_CHECK_FAILED"
        raise ImageVerificationError(
            "pinned image does not contain the clean base revision",
            evidence,
        )
    return evidence


def _write_image_verification(output: Path, evidence: dict[str, Any]) -> None:
    (output / "image-verification.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _verify_image_and_record(output: Path) -> dict[str, Any] | None:
    try:
        evidence = _verify_image()
    except ImageVerificationError as exc:
        _write_image_verification(output, exc.evidence)
        raise
    if evidence is not None:
        _write_image_verification(output, evidence)
    return evidence


def _copy_image_source(fixture: Path) -> None:
    fixture.parent.mkdir(parents=True, exist_ok=True)
    created = _run(
        ["docker", "create", "--network", "none", "--entrypoint", "/bin/true", IMAGE],
        timeout=30,
    )
    if created.returncode != 0 or not created.stdout.strip():
        raise ProofError("could not create the source-export container")
    container_id = created.stdout.strip()
    try:
        copied = _run(["docker", "cp", f"{container_id}:/cookiecutter/.", str(fixture)], timeout=120)
        if copied.returncode != 0:
            raise ProofError("could not export the pinned source from Docker")
    finally:
        _run(["docker", "rm", "-f", container_id], timeout=30)
    git_dir = fixture / ".git"
    if git_dir.exists():
        _remove_readonly_tree(git_dir)
    source = fixture / "cookiecutter/config.py"
    if not source.is_file() or _sha256(source) != SOURCE_SHA256:
        raise ProofError("exported production source does not match the pinned base blob")


def _write_public_scaffold(fixture: Path, problem_statement: str) -> None:
    # The old dependency is already present in the official image.  This tiny
    # public compatibility shim lets the host-side PDB proof use the installed
    # PyYAML parser without installing or changing a global environment.
    (fixture / "poyo.py").write_text(
        "import yaml\n\n"
        "class PoyoException(Exception):\n    pass\n\n"
        "class exceptions:\n    PoyoException = PoyoException\n\n"
        "def parse_string(text):\n"
        "    try:\n        return yaml.safe_load(text) or {}\n"
        "    except yaml.YAMLError as exc:\n        raise PoyoException(str(exc))\n",
        encoding="utf-8",
        newline="\n",
    )
    test_path = fixture / "tests/test_pdb_public_config_merge.py"
    test_path.write_text(
        "from cookiecutter import config\n\n\n"
        "def test_builtin_abbreviations_survive_custom_config(tmp_path):\n"
        "    path = tmp_path / 'cookiecutter.yaml'\n"
        "    path.write_text(\"abbreviations:\\n  local: https://example.invalid/{0}.git\\n\", encoding='utf-8')\n"
        "    loaded = config.get_config(str(path))\n"
        "    assert loaded['abbreviations']['local'] == 'https://example.invalid/{0}.git'\n"
        "    assert loaded['abbreviations']['gh'] == 'https://github.com/{0}.git'\n"
        "    defaults = {\n"
        "        'abbreviations': {'gh': 'https://github.com/{0}.git'},\n"
        "        'default_context': {'project': 'cookiecutter'},\n"
        "    }\n"
        "    overrides = {\n"
        "        'abbreviations': {'local': 'https://example.invalid/{0}.git'},\n"
        "        'default_context': {'owner': 'onur'},\n"
        "    }\n"
        "    merged = config.merge_configs(defaults, overrides)\n"
        "    assert merged['abbreviations']['gh'] == 'https://github.com/{0}.git'\n"
        "    assert merged['abbreviations']['local'] == 'https://example.invalid/{0}.git'\n"
        "    assert merged['default_context']['project'] == 'cookiecutter'\n"
        "    assert merged['default_context']['owner'] == 'onur'\n\n\n"
        "def test_scalar_override_preserves_other_defaults(tmp_path):\n"
        "    path = tmp_path / 'cookiecutter.yaml'\n"
        "    path.write_text(\"replay_dir: ./replays\\n\", encoding='utf-8')\n"
        "    loaded = config.get_config(str(path))\n"
        "    assert loaded['replay_dir'].endswith('replays')\n"
        "    assert loaded['cookiecutters_dir']\n",
        encoding="utf-8",
        newline="\n",
    )
    task = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "title": "Recursively preserve nested Cookiecutter configuration",
        "description": problem_statement + "\n\nPublic contract: configuration overlays must recursively preserve unrelated keys in nested mappings while allowing user values to override defaults. The reusable merge behavior is part of the public module contract.",
        "language": "python",
        "fixture_path": f"agentic_debugger/datasets/curated/{TASK_ID}",
        "reproduction": {
            "argv": [
                "python", "-m", "pytest", PUBLIC_F2P, "-q", "-p", "no:cacheprovider",
                "-o", "addopts=",
            ],
            "cwd": ".",
            "timeout_seconds": 20,
            "expected_exit_code": 1,
        },
        "tests": {
            "fail_to_pass": [PUBLIC_F2P],
            "pass_to_pass": [PUBLIC_P2P],
            "full_suite_argv": [
                "python", "-m", "pytest", "tests/test_pdb_public_config_merge.py",
                "-q", "-p", "no:cacheprovider", "-o", "addopts=",
            ],
            "timeout_seconds": 30,
        },
        "constraints": {
            "allowed_write_paths": ["cookiecutter/config.py"],
            "denied_write_paths": ["tests", "task.json"],
            "network_allowed": False,
            "external_services_allowed": False,
            "max_patch_attempts": 2,
            "max_test_runs": 5,
            "max_pdb_observations": 6,
        },
        "oracle": {
            "bug_category": "nested configuration merge",
            "target_files": ["cookiecutter/config.py"],
            "target_symbols": ["get_config", "merge_configs"],
            "root_cause_summary": "Nested user configuration requires recursive merge behavior rather than a shallow update.",
            "runtime_evidence_hint": "The get_config frame exposes the parsed override and the resulting nested mapping.",
        },
        "tags": ["swe-rebench-v2", "pdb-required", "capability-ladder-32", "oracle-localized"],
    }
    (fixture / "task.json").write_text(
        json.dumps(task, indent=2, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n"
    )


def _scenario() -> DemoScenario:
    return DemoScenario(
        task_id=TASK_ID,
        hypothesis_id="cookiecutter-967-runtime-hypothesis",
        root_cause_statement="The reproduced nested configuration behavior requires runtime inspection.",
        localization=LocalizationClaim("cookiecutter/config.py", "get_config"),
        reference_repair=ReferenceRepair(
            "cookiecutter/config.py",
            "config_dict = copy.copy(DEFAULT_CONFIG)",
            "config_dict = copy.deepcopy(DEFAULT_CONFIG)",
        ),
        runtime_probe=RuntimeProbe(
            module_path="cookiecutter/config.py",
            focus_function="get_config",
            call_source="get_config('unused-public-driver-path')",
            anchor="config_dict.update(yaml_dict)",
            inspect_expressions=("yaml_dict", "config_dict"),
            exact_public_reproduction=True,
            breakpoint_line=54,
        ),
    )


def _adapter_config(root: Path, *, model: str | None = None, logical_decision_ceiling: int = 25) -> LiveModelConfig:
    resolved_model = model or MODEL
    _resolve_model_or_fail(resolved_model)
    # Thinking level is part of the treatment fingerprint.  For verified
    # profiles it is pinned; for catalog-only models the adapter omits
    # ``think`` until a transport profile is promoted to verified.
    try:
        from scripts.ollama_cloud_command_adapter import resolve_cloud_model as _rc

        spec = _rc(resolved_model)
        thinking = spec.thinking_level
        idle_timeout = spec.idle_timeout_seconds
        request_timeout = spec.request_timeout_seconds
    except Exception:
        mod = _load_ollama_adapter_module("ollama_cloud_command_adapter_config")
        spec = mod.resolve_cloud_model(resolved_model)
        thinking = spec.thinking_level
        idle_timeout = spec.idle_timeout_seconds
        request_timeout = spec.request_timeout_seconds
    argv: tuple[str, ...] = (
        sys.executable,
        str(root / "scripts/ollama_cloud_command_adapter.py"),
        "--model",
        resolved_model,
        "--timeout",
        str(int(idle_timeout)),
        "--max-logical-model-calls",
        str(logical_decision_ceiling),
        "--expected-version",
        EXPECTED_OLLAMA_VERSION,
    )
    # Keep the adapter subprocess's outer deadline aligned with the canonical
    # model profile.  Without this flag the adapter defaults its request
    # deadline to the inactivity watchdog, collapsing DeepSeek's evidence-
    # backed 300/3600 profile back to a 300-second total request.
    if request_timeout != idle_timeout:
        argv = (*argv, "--request-timeout", str(int(request_timeout)))
    if thinking is not None:
        argv = (*argv, "--thinking-level", thinking)
    return LiveModelConfig(
        resolved_model,
        argv,
        request_timeout_seconds=request_timeout,
        tool_version="ollama-cloud-command-adapter-v1.3-stream-idle",
    )


def _preflight(config: LiveModelConfig) -> dict[str, Any]:
    result = _run([*config.command, "--preflight"], timeout=90)
    if result.returncode != 0:
        raise ProofError("Ollama zero-inference preflight failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProofError("Ollama preflight did not return JSON") from exc
    if payload.get("provider_inference_started") is not False:
        raise ProofError("Ollama preflight did not remain zero-inference")
    return payload


def _candidate_patch_record(case: dict[str, Any]) -> dict[str, Any]:
    """Select the final active patch from replayed tool-success evidence.

    Directive acceptance means only that a provider response passed protocol
    and semantic parsing. Candidate identity must instead follow the tool
    observation that actually mutated the disposable workspace. Successful
    reverts clear the active candidate; later successful applies replace it.
    """

    events_jsonl = case.get("events_jsonl")
    if not isinstance(events_jsonl, str) or not events_jsonl.strip():
        raise ProofError("the live case has no replayable candidate evidence")
    replay = replay_events(events_jsonl)
    actions: dict[str, tuple[str, int]] = {}
    active: dict[str, Any] | None = None
    for event in replay.events:
        event_type = event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type)
        if event_type == "action":
            action = event.payload.get("action")
            if not isinstance(action, dict) or action.get("name") != "apply_patch":
                continue
            action_id = action.get("action_id")
            patch = action.get("arguments", {}).get("patch")
            if isinstance(action_id, str) and isinstance(patch, str) and patch.strip():
                actions[action_id] = (patch, event.sequence)
            continue
        if event_type != "observation":
            continue
        observation = event.payload.get("observation")
        if not isinstance(observation, dict) or observation.get("status") != "ok":
            continue
        name = observation.get("name")
        payload = observation.get("payload")
        if not isinstance(payload, dict):
            continue
        if name == "revert_patch" and payload.get("reverted") is True:
            active = None
            continue
        if name != "apply_patch" or payload.get("applied") is not True:
            continue
        action_id = observation.get("action_id")
        source = actions.get(action_id) if isinstance(action_id, str) else None
        if source is None:
            raise ProofError("successful patch observation has no matching action")
        patch, action_sequence = source
        patch_sha256 = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        if payload.get("patch_sha256") != patch_sha256:
            raise ProofError("successful patch observation disagrees with action bytes")
        active = {
            "schema_version": "level32-candidate-provenance-v1",
            "patch": patch,
            "patch_sha256": patch_sha256,
            "tool_accepted": True,
            "action_id": action_id,
            "observation_id": observation.get("observation_id"),
            "action_event_sequence": action_sequence,
            "observation_event_sequence": event.sequence,
        }
    if active is None:
        raise ProofError("the live case did not retain a tool-accepted active candidate patch")
    return active


def _candidate_patch(case: dict[str, Any]) -> str:
    return str(_candidate_patch_record(case)["patch"])


def _validate_official_row(row: dict[str, Any]) -> None:
    """Validate the complete public identity used to invoke the evaluator.

    The evaluator accepts a row-shaped object, so checking only its instance
    id is insufficient: a row from another revision or image could otherwise
    produce a plausible aggregate result.
    """

    if not isinstance(row, dict):
        raise ProofError("official evaluator task row is not an object")
    expected = {
        "instance_id": INSTANCE_ID,
        "repo": "audreyr/cookiecutter",
        "base_commit": BASE_COMMIT,
        "image_name": IMAGE,
    }
    for field, value in expected.items():
        if row.get(field) != value:
            raise ProofError(f"official evaluator task identity mismatch: {field}")
    for field, count in (("FAIL_TO_PASS", F2P_COUNT), ("PASS_TO_PASS", P2P_COUNT)):
        tests = row.get(field)
        if not isinstance(tests, (list, tuple)) or len(tests) != count:
            raise ProofError(f"official evaluator task contract mismatch: {field}")
        if any(type(test) is not str or not test.strip() for test in tests):
            raise ProofError(f"official evaluator task contract has malformed {field}")
        if len(set(tests)) != len(tests):
            raise ProofError(f"official evaluator task contract has duplicate {field}")
    for field in ("patch", "test_patch", "install_config", "problem_statement", "language", "license"):
        if not row.get(field):
            raise ProofError(f"official evaluator task row is missing {field}")


def _normalize_candidate_patch_for_official(patch: str) -> tuple[str, str]:
    if type(patch) is not str:
        raise ProofError("candidate patch must be an exact string")
    if "\x00" in patch:
        raise ProofError("candidate patch contains a NUL byte")
    if len(patch) > 100_000:
        raise ProofError("candidate patch exceeds the official evaluator limit")
    if not patch:
        return patch, "none"
    if patch.endswith("\n"):
        return patch, "none"
    return patch + "\n", "terminal-newline-added"


def _canonicalize_level32_candidate(
    pristine_source: Path,
    raw_patch: str,
    *,
    parent_dir: Path | None = None,
) -> CanonicalPatchArtifact:
    """Materialize the raw model patch and prove its official Git artifact."""

    try:
        return materialize_and_canonicalize_patch(
            str(pristine_source),
            raw_patch,
            ["cookiecutter/config.py"],
            ["tests", "task.json"],
            parent_dir=str(parent_dir) if parent_dir is not None else None,
        )
    except Exception as exc:
        raise ProofError(
            f"candidate canonicalization failed closed: {type(exc).__name__}: {exc}"
        ) from exc


def _write_candidate_artifacts(
    output: Path,
    raw_patch: str,
    artifact: CanonicalPatchArtifact,
) -> dict[str, Any]:
    """Persist raw provenance and the distinct canonical official artifact."""

    # candidate.patch remains the exact model-authored text for historical
    # compatibility.  The evaluator never consumes this file in the repaired
    # treatment.
    (output / "candidate.patch").write_text(raw_patch, encoding="utf-8", newline="\n")
    (output / "candidate-official.patch").write_text(
        artifact.patch,
        encoding="utf-8",
        newline="\n",
    )
    mapping = artifact.to_mapping()
    mapping.pop("patch", None)
    mapping["official_patch_path"] = "candidate-official.patch"
    (output / "candidate-artifact.json").write_text(
        json.dumps(mapping, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return mapping


def _redacted_test_summary(
    item: dict[str, Any], *, f2p_total: int, p2p_total: int,
    candidate_patch_application_failure: bool = False,
) -> dict[str, Any]:
    """Project evaluator test results without retaining hidden test names."""

    error_present = bool(item.get("error"))
    error_class = (
        "evaluator_error"
        if error_present
        else "candidate_patch_application_failure"
        if candidate_patch_application_failure or item.get("exit_code") == 128
        else None
    )
    status_is_error = error_class is not None
    f2p_passed = min(len(item.get("from_fail_to_pass") or ()), f2p_total)
    p2p_failed = min(len(item.get("failed_from_pass_to_pass") or ()), p2p_total)

    return {
        "schema_version": "level32-redacted-test-summary-v1",
        "identity_retained": False,
        "index_semantics": "not retained; only aggregate status counts are authoritative",
        "fail_to_pass": {
            "total": f2p_total,
            "passed": f2p_passed if not status_is_error else 0,
            "failed": f2p_total - f2p_passed if not status_is_error else 0,
            "error": f2p_total if status_is_error else 0,
        },
        "pass_to_pass": {
            "total": p2p_total,
            "passed": p2p_total - p2p_failed if not status_is_error else 0,
            "failed": p2p_failed if not status_is_error else 0,
            "error": p2p_total if status_is_error else 0,
        },
        "error_present": error_present,
        "error_class": error_class,
    }


def _official_evaluator_root() -> Path:
    return (
        Path.home()
        / "AppData/Local/agentic-debugging/gpt-oss-swerebench-v2-pilot10/official-evaluator"
    )


def _official_evaluate(
    row: dict[str, Any],
    patch: str,
    private_dir: Path,
    *,
    expose_candidate_hashes: bool = True,
    raw_patch: str | None = None,
    candidate_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_official_row(row)
    evaluator_root = (
        _official_evaluator_root()
    )
    head = _run(["git", "-C", str(evaluator_root), "rev-parse", "HEAD"], timeout=20)
    if head.returncode != 0 or head.stdout.strip() != EVALUATOR_COMMIT:
        raise ProofError("the pinned official SWE-rebench evaluator is unavailable")
    evaluated_patch, normalization = _normalize_candidate_patch_for_official(patch)
    spec = {
        "instance_id": INSTANCE_ID,
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "image_name": row["image_name"],
        "patch": evaluated_patch,
        "test_patch": row["test_patch"],
        "FAIL_TO_PASS": list(row["FAIL_TO_PASS"]),
        "PASS_TO_PASS": list(row["PASS_TO_PASS"]),
        "install_config": row["install_config"],
        "problem_statement": row["problem_statement"],
        "language": row["language"],
        "license": row["license"],
    }
    spec_path = private_dir / "official-private-spec.json"
    report_path = private_dir / "official-private-report.json"
    spec_path.write_text(json.dumps([spec], ensure_ascii=True), encoding="utf-8")
    started = time.monotonic()
    result = _run(
        [
            sys.executable,
            str(evaluator_root / "scripts/eval.py"),
            "--json", str(spec_path),
            "--max-workers", "1",
            "--golden-eval",
            "--report-json", str(report_path),
        ],
        cwd=private_dir,
        timeout=360,
    )
    elapsed = time.monotonic() - started
    if not report_path.is_file():
        raise ProofError("official evaluator completed without a report")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    items = report.get("items") or []
    item = items[0] if len(items) == 1 and isinstance(items[0], dict) else {}
    if len(items) != 1 or item.get("instance_id") != INSTANCE_ID:
        raise ProofError("official evaluator returned a mismatched task result")
    patch_application_failure = False
    log_name = item.get("log_path")
    if isinstance(log_name, str) and log_name:
        log_path = (private_dir / log_name).resolve()
        private_root = private_dir.resolve()
        if log_path == private_root or private_root not in log_path.parents:
            raise ProofError("official evaluator returned an unsafe log path")
        if log_path.is_file():
            log_text = log_path.read_text(encoding="utf-8", errors="replace").lower()
            patch_application_failure = any(
                marker in log_text
                for marker in (
                    "patch does not apply",
                    "patch failed:",
                    "corrupt patch",
                )
            )
    # The pinned evaluator exits before pytest with 128 when candidate patch
    # application fails. Keep this top-level flag aligned with the redacted
    # result projection even when the evaluator log path is unavailable.
    patch_application_failure = patch_application_failure or item.get("exit_code") == 128
    redacted_tests = _redacted_test_summary(
        item,
        f2p_total=F2P_COUNT,
        p2p_total=P2P_COUNT,
        candidate_patch_application_failure=patch_application_failure,
    )
    test_execution_proven = bool(
        not patch_application_failure
        and not item.get("error")
        and "from_fail_to_pass" in item
        and "failed_from_pass_to_pass" in item
    )
    safe = {
        "schema_version": "level32-official-result-v2",
        "authority": "official-swerebench-v2-docker-evaluator",
        "instance_id": INSTANCE_ID,
        "repo": "audreyr/cookiecutter",
        "base_commit": BASE_COMMIT,
        "evaluator_commit": EVALUATOR_COMMIT,
        "image": IMAGE,
        "image_id": IMAGE_ID,
        "dataset_revision": DATASET_REVISION,
        "dataset_parquet_sha256": PARQUET_SHA256,
        "process_exit_code": result.returncode,
        "elapsed_seconds": elapsed,
        "report_total": report.get("total"),
        "all_ok": report.get("all_ok"),
        "passed_match": item.get("passed_match"),
        "container_exit_code": item.get("exit_code"),
        "fail_to_pass_total": F2P_COUNT,
        "fail_to_pass_passed": len(item.get("from_fail_to_pass") or ()),
        "pass_to_pass_total": P2P_COUNT,
        "pass_to_pass_failed": len(item.get("failed_from_pass_to_pass") or ()),
        "error_present": bool(item.get("error")),
        "candidate_patch_application_failure": patch_application_failure,
        "official_test_execution_proven": test_execution_proven,
        "candidate_patch_artifact": "candidate-official.patch" if candidate_artifact else None,
        "redacted_test_summary": redacted_tests,
        "private_artifacts_removed": True,
        "candidate_patch_normalization": normalization,
    }
    if expose_candidate_hashes:
        safe.update(
            {
                "raw_candidate_patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
                "evaluated_candidate_patch_sha256": hashlib.sha256(evaluated_patch.encode("utf-8")).hexdigest(),
            }
        )
        if raw_patch is not None:
            safe["raw_candidate_patch_sha256"] = hashlib.sha256(raw_patch.encode("utf-8")).hexdigest()
    return safe


def _classify_level32_case(case: dict[str, Any], official: dict[str, Any] | None = None) -> str:
    """Classify observed execution boundaries without inferring model success."""

    if case.get("candidate_materialization_failure"):
        return "candidate_not_materialized"
    if case.get("candidate_canonicalization_failure"):
        return "canonical_official_patch_unavailable"
    measurements = case.get("measurements") or {}
    kinds = set(measurements.get("provider_error_kinds") or ())
    requests = measurements.get("model_request_count")
    responses = measurements.get("model_response_count")
    if kinds and (requests is None or responses is None or responses < requests):
        return "incomplete_provider_model_transport_failure"
    if kinds:
        return "provider_parser_rejection"
    if official and official.get("error_present"):
        return "official_evaluator_failure"
    if official and official.get("candidate_patch_application_failure"):
        return "official_candidate_patch_application_failure"
    if official and official.get("all_ok") is False and not official.get("error_present"):
        if official.get("official_test_execution_proven") is not True:
            return "official_test_execution_unproven"
        return "official_rejection_semantic"
    if case.get("status") not in {LiveCaseStatus.RESOLVED.value}:
        return "incomplete_run"
    return "unclassified"


def _known_broken_control_patch(row: dict[str, Any]) -> str:
    """Derive a valid, intentionally broken patch in evaluator-private memory."""

    reference = row.get("patch")
    if type(reference) is not str or "deepcopy" not in reference:
        raise ProofError("the pinned reference patch cannot produce a known-broken control")
    broken = reference.replace("deepcopy", "copy.copy", 1)
    if broken == reference:
        raise ProofError("the known-broken control did not differ from the reference patch")
    return broken


def _integrity_control_passes(control: dict[str, Any], kind: str) -> bool:
    if control.get("instance_id") != INSTANCE_ID or control.get("base_commit") != BASE_COMMIT:
        return False
    if kind == "reference":
        return bool(
            control.get("process_exit_code") == 0
            and control.get("all_ok") is True
            and control.get("passed_match") is True
            and control.get("fail_to_pass_passed") == F2P_COUNT
            and control.get("pass_to_pass_failed") == 0
            and not control.get("error_present")
        )
    if kind == "baseline":
        return bool(
            control.get("process_exit_code") != 0
            and control.get("all_ok") is False
            and control.get("fail_to_pass_passed") == 0
            and type(control.get("pass_to_pass_failed")) is int
            and 0 <= control.get("pass_to_pass_failed") <= P2P_COUNT
            and not control.get("error_present")
            and control.get("redacted_test_summary", {}).get("fail_to_pass", {}).get("failed") == F2P_COUNT
        )
    if kind == "intentionally_bad":
        return bool(
            control.get("process_exit_code") != 0
            and control.get("all_ok") is False
            and control.get("fail_to_pass_passed") == 0
            and (
                control.get("error_present") is True
                or control.get("pass_to_pass_failed") == P2P_COUNT
            )
        )
    return False


def _run_integrity_gate(output: Path) -> int:
    """Run provider-free baseline/reference/bad controls through the official path."""

    output.mkdir(parents=True, exist_ok=False)
    payload: dict[str, Any] = {
        "schema_version": INTEGRITY_GATE_SCHEMA_VERSION,
        "status": "BLOCKED",
        "provider_model_execution_started": False,
        "controls": {},
    }
    try:
        image = _verify_image()
        row = _load_official_row()
        payload["identity"] = {
            "instance_id": INSTANCE_ID,
            "repo": "audreyr/cookiecutter",
            "base_commit": BASE_COMMIT,
            "image": IMAGE,
            "image_id": IMAGE_ID,
            "evaluator_commit": EVALUATOR_COMMIT,
            "dataset_revision": DATASET_REVISION,
            "dataset_parquet_sha256": PARQUET_SHA256,
            "image_gate_category": image.get("category"),
        }
        with tempfile.TemporaryDirectory(prefix="cookiecutter-967-integrity-") as private:
            private_dir = Path(private)
            payload["controls"]["baseline"] = _official_evaluate(
                row, _known_broken_control_patch(row), private_dir
            )
            payload["controls"]["reference"] = _official_evaluate(
                row, row["patch"], private_dir, expose_candidate_hashes=False
            )
            payload["controls"]["intentionally_bad"] = _official_evaluate(
                row, "this is intentionally not a unified diff", private_dir
            )
        payload["control_acceptance"] = {
            name: _integrity_control_passes(control, name)
            for name, control in payload["controls"].items()
        }
        payload["status"] = (
            "PASS" if all(payload["control_acceptance"].values()) else "INTEGRITY_FAILURE"
        )
        payload["private_artifacts_removed"] = True
    except (ImageVerificationError, ProofError, OSError, json.JSONDecodeError) as exc:
        payload["status"] = "BLOCKED"
        payload["blocked_reason"] = str(exc)
    (output / "integrity-gate.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "PASS" else (1 if payload["status"] == "INTEGRITY_FAILURE" else 2)


def _accepted(case: dict[str, Any], official: dict[str, Any]) -> bool:
    measurements = case.get("measurements", {})
    verifier = case.get("verifier", {})
    return all(
        (
            case.get("status") == LiveCaseStatus.RESOLVED.value,
            case.get("controller", {}).get("completed") is True,
            verifier.get("outcome") == "RESOLVED",
            verifier.get("workspace_cleaned") is True,
            verifier.get("canonical_fixture_unchanged") is True,
            measurements.get("successful_pdb_observation_count", 0) >= 3,
            measurements.get("failed_pdb_observation_count") == 0,
            measurements.get("retry_count") == 0,
            official.get("process_exit_code") == 0,
            official.get("all_ok") is True,
            official.get("passed_match") is True,
            official.get("fail_to_pass_passed") == F2P_COUNT,
            official.get("pass_to_pass_failed") == 0,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One-task Cookiecutter 967 exact-PDB proof")
    parser.add_argument("--repository-root", default=".")
    parser.add_argument("--output-dir", required=False, default=None)
    parser.add_argument("--model", default=None, help="Ollama Cloud alias (default gpt-oss:20b-cloud)")
    parser.add_argument(
        "--treatment-revision",
        type=int,
        default=None,
        help="fresh treatment revision (prepared model defaults: Qwen v2, Kimi v7, Gemma v3; otherwise v1)",
    )
    parser.add_argument("--list-models", action="store_true", help="list selectable aliases and exit")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-live-model-access", action="store_true")
    parser.add_argument("--progress-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--integrity-gate",
        action="store_true",
        help="run provider-free official baseline/reference/bad controls and exit",
    )
    parser.add_argument(
        "--recover-existing",
        action="store_true",
        help="rebuild verification/summary from an immutable provider-complete source",
    )
    parser.add_argument(
        "--recovery-source-dir",
        default=None,
        help="immutable historical directory used as the source for --recover-existing",
    )
    return parser


def _model_provenance(model: str) -> dict[str, Any]:
    try:
        from scripts.ollama_cloud_command_adapter import resolve_cloud_model as _rc
        from scripts.ollama_cloud_command_adapter import transport_config_fingerprint as _tcf
    except Exception:
        mod = _load_ollama_adapter_module("ollama_cloud_command_adapter_provenance")
        _rc = mod.resolve_cloud_model
        _tcf = mod.transport_config_fingerprint
    spec = _rc(model)
    return {
        "requested_model": spec.local_alias,
        "upstream_model": spec.upstream_model,
        "effective_tags_remote_model": spec.effective_tags_remote_model,
        "family": spec.family,
        "transport_verified": spec.transport_verified,
        "thinking_level": spec.thinking_level,
        "transport_config_fingerprint": _tcf(spec),
    }


def _treatment_fingerprint(model: str, budget: LiveTreatmentBudget) -> str:
    provenance = _model_provenance(model)
    payload = {
        "schema_version": "level32-treatment-fingerprint-v1",
        "model_provenance": provenance,
        "treatment_budget": budget.to_mapping(),
        "model_visible_budget_policy": {
            "max_patch_attempts": budget.max_patch_attempts,
            "max_test_runs": budget.max_test_runs,
            "max_pdb_observations": budget.max_pdb_observations,
            "max_active_hypotheses": 3,
            "max_source_observations": budget.max_source_observations,
        },
        "model_visible_task_projection": {
            "schema_version": "task-schema-1.0-resource-overlay-v1",
            "resource_fields": [
                "max_patch_attempts",
                "max_test_runs",
                "max_pdb_observations",
            ],
            "semantic_fields_unchanged": True,
        },
        "protocol_version": "1.3",
        "provider_completion_envelope_schema": "provider-completion-v1",
        "directive_observability_schema": "directive-observability-v1",
        "directive_normalization": DIRECTIVE_NORMALIZATION_POLICY,
        "directive_normalization_schema_version": DIRECTIVE_NORMALIZATION_SCHEMA_VERSION,
        "directive_normalization_policy_id": DIRECTIVE_NORMALIZATION_POLICY_ID,
        "pdb_breakpoint_selection": PDB_BREAKPOINT_SELECTION_POLICY,
        "pdb_breakpoint_selection_schema_version": PDB_BREAKPOINT_SELECTION_SCHEMA_VERSION,
        "pdb_breakpoint_selection_policy_id": PDB_BREAKPOINT_SELECTION_POLICY_ID,
        "proof_role_selection": PROOF_ROLE_SELECTION_POLICY,
        "candidate_transport_id": CANDIDATE_TRANSPORT_ID,
        "candidate_transport_contract": {
            "raw_patch_preserved": True,
            "official_patch_source": "pristine-to-patched-workspace-git-diff",
            "semantic_equivalence_required": True,
            "strict_git_apply_required": True,
        },
        "retry_count": 0,
        "fallback_count": 0,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result_summary(
    case: dict[str, Any],
    official: dict[str, Any],
    patch: str,
    *,
    model: str | None = None,
    treatment_id: str | None = None,
    candidate_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    replay = replay_events(case["events_jsonl"])
    terminal_state: str | None = None
    if replay.events:
        state = replay.events[-1].state
        terminal_state = state.value if hasattr(state, "value") else str(state)
    resolved_model = model or MODEL
    resolved_treatment = treatment_id or TREATMENT_ID
    provenance = _model_provenance(resolved_model)
    treatment_budget = None if resolved_treatment == FROZEN_TREATMENT_ID_LEGACY else LEVEL32_TREATMENT_BUDGET
    return {
        "schema_version": "pdb-capability-level32-result-v4" if treatment_budget is not None else "pdb-capability-level32-result-v3",
        "accepted": _accepted(case, official),
        "task": {
            "instance_id": INSTANCE_ID,
            "task_id": TASK_ID,
            "difficulty": 32,
            "base_commit": BASE_COMMIT,
            "treatment": "oracle-localized public reproducer plus exact PDB; official hidden Docker verification",
            "treatment_id": resolved_treatment,
        },
        "model": resolved_model,
        "model_provenance": provenance,
        "treatment_id": resolved_treatment,
        "transport_config_fingerprint": provenance.get("transport_config_fingerprint"),
        "treatment_budget": treatment_budget.to_mapping() if treatment_budget is not None else None,
        "treatment_fingerprint": _treatment_fingerprint(resolved_model, treatment_budget) if treatment_budget is not None else None,
        "live_status": case["status"],
        "classification": _classify_level32_case(case, official),
        "controller_completed": case["controller"]["completed"],
        "measurements": case["measurements"],
        "local_verifier": case["verifier"],
        "official_verifier": official,
        "candidate_patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "candidate_patch_provenance": candidate_provenance,
        "replay_event_count": len(replay.events),
        "replay_terminal_state": terminal_state,
        "cleanup": {
            "temporary_source_removed": True,
            "private_official_material_removed": True,
        },
    }


def _safe_operator_error(value: Any) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    if len(text) > 512:
        text = text[:509] + "..."
    return "[redacted sensitive operator error]" if contains_credential_shape(text) else text


def _exact_pdb_proof_summary(case: dict[str, Any]) -> dict[str, Any]:
    """Project PDB observation only from an exact structured proof record."""

    measurements = case.get("measurements")
    count = (
        measurements.get("successful_pdb_observation_count")
        if isinstance(measurements, dict)
        else 0
    )
    summary: dict[str, Any] = {
        "observed": False,
        "successful_observation_count": count if type(count) is int and count >= 0 else 0,
        "script": None,
        "breakpoint_line": None,
    }
    events_jsonl = case.get("events_jsonl")
    if not isinstance(events_jsonl, str) or not isinstance(count, int) or count <= 0:
        return summary
    for line_text in events_jsonl.splitlines():
        try:
            event = json.loads(line_text)
        except (TypeError, json.JSONDecodeError):
            continue
        payload = event.get("payload") if isinstance(event, dict) else None
        observation = payload.get("observation") if isinstance(payload, dict) else None
        observation_payload = (
            observation.get("payload") if isinstance(observation, dict) else None
        )
        proof = (
            observation_payload.get("proof")
            if isinstance(observation_payload, dict)
            else None
        )
        if not isinstance(proof, dict) or proof.get("exact_reproduction") is not True:
            continue
        script = proof.get("production_file") or observation_payload.get("script")
        breakpoint_line = proof.get("breakpoint_line") or observation_payload.get(
            "breakpoint_line"
        )
        if isinstance(script, str) and type(breakpoint_line) is int and breakpoint_line > 0:
            summary.update(
                {
                    "observed": True,
                    "script": script,
                    "breakpoint_line": breakpoint_line,
                }
            )
            return summary
    return summary


def _operator_failure_summary(
    case: dict[str, Any],
    *,
    model: str,
    treatment_id: str,
    failure_kind: str,
    failure: Any,
) -> dict[str, Any]:
    """Persist a bounded operator outcome when treatment cannot reach verify."""

    summary = _result_summary(
        case,
        {},
        "",
        model=model,
        treatment_id=treatment_id,
    )
    cleaned = bool(
        isinstance(case.get("reporting"), dict)
        and case["reporting"].get("completed") is True
        and case["reporting"].get("cleanup") == "cleaned"
    )
    summary.update(
        {
            "official_verifier": None,
            "candidate_patch_sha256": None,
            "candidate_patch_provenance": None,
            "pdb_proof": _exact_pdb_proof_summary(case),
            "operator_failure": {
                "kind": failure_kind,
                "message": _safe_operator_error(failure),
                "process_exit_code": 2,
            },
            "cleanup": {
                "temporary_source_removed": cleaned,
                "private_official_material_removed": cleaned,
            },
        }
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    progress = _ProgressWriter(args.progress_file)
    progress.emit("starting")
    if args.integrity_gate:
        if args.live or args.confirm_live_model_access or args.recover_existing:
            raise ProofError("integrity gate cannot be combined with live or recovery execution")
        output = Path(args.output_dir).resolve() if args.output_dir else (
            Path(args.repository_root).resolve() / "outputs/level32-integrity-gate-v1"
        )
        if output.exists():
            raise ProofError("integrity gate output directory already exists")
        return _run_integrity_gate(output)
    if args.list_models:
        try:
            from scripts.ollama_cloud_command_adapter import CLOUD_MODELS

            for alias in sorted(CLOUD_MODELS):
                spec = CLOUD_MODELS[alias]
                flag = spec.readiness
                print(f"{alias:30} -> {spec.upstream_model:25} [{flag}]")
        except Exception:
            pass
        return 0
    requested_model = args.model or MODEL
    _resolve_model_or_fail(requested_model)
    if not args.list_models and not args.recover_existing:
        _require_treatment_eligible(requested_model)
    prepared_revision = args.treatment_revision if args.treatment_revision is not None else PREPARED_TREATMENT_REVISIONS.get(requested_model, 1)
    treatment_id = _treatment_id_for_model(requested_model, prepared_revision)
    treatment_budget = None if treatment_id == FROZEN_TREATMENT_ID_LEGACY else LEVEL32_TREATMENT_BUDGET
    root = Path(args.repository_root).resolve()
    if args.output_dir is not None:
        output = Path(args.output_dir).resolve()
    else:
        # No output-dir supplied: use the canonical per-model directory
        # under the repository.  This keeps treatment identity tied to the
        # requested model without requiring a second harness copy.
        output = (root / _default_output_dir_for_model(requested_model, prepared_revision)).resolve()
    if args.recover_existing:
        if args.live or args.confirm_live_model_access:
            raise ProofError("recovery must remain provider-free")
        if args.recovery_source_dir is None:
            raise ProofError("recovery requires --recovery-source-dir and a fresh --output-dir")
        if args.output_dir is None:
            raise ProofError("recovery requires --recovery-source-dir and a fresh --output-dir")
        recovery_source = Path(args.recovery_source_dir).resolve()
        if recovery_source == output:
            raise ProofError("recovery source and destination must be different paths")
        if output.exists():
            raise ProofError("recovery destination directory already exists; choose a fresh output directory")
        live_path = recovery_source / "live-results.json"
        patch_path = recovery_source / "candidate.patch"
        if not recovery_source.is_dir() or not live_path.is_file() or not patch_path.is_file():
            raise ProofError("provider-complete recovery artifacts are missing")
        row = _load_official_row()
        try:
            live_bytes = live_path.read_bytes()
            raw_patch_bytes = patch_path.read_bytes()
            case = json.loads(live_bytes.decode("utf-8"))
            source_patch = raw_patch_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProofError(f"provider-complete recovery artifacts are unreadable: {exc}") from exc
        candidate = _candidate_patch_record(case)
        patch = candidate["patch"]
        if source_patch != patch:
            raise ProofError("historical candidate.patch disagrees with replayed tool-success evidence")
        output.mkdir(parents=True)
        # Preserve the historical live case as source provenance, but never
        # mutate the source directory with repaired-treatment artifacts.
        (output / "live-results.json").write_bytes(live_bytes)
        image_verification = _verify_image_and_record(output)
        # Keep the disposable export path short enough for the pinned image's
        # long fixture paths on Windows before Git indexes the baseline.
        with tempfile.TemporaryDirectory(prefix="l32-r-") as source_temp:
            fixture = Path(source_temp) / "fixture"
            _copy_image_source(fixture)
            _write_public_scaffold(fixture, str(row["problem_statement"]))
            artifact = _canonicalize_level32_candidate(fixture, patch, parent_dir=Path(source_temp))
            artifact_mapping = _write_candidate_artifacts(output, patch, artifact)
            with tempfile.TemporaryDirectory(prefix="cookiecutter-967-private-eval-") as private:
                official = _official_evaluate(
                    row,
                    artifact.patch,
                    Path(private),
                    raw_patch=patch,
                    candidate_artifact=artifact_mapping,
                )
        (output / "official-verifier-summary.json").write_text(
            json.dumps(official, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        summary = _result_summary(
            case,
            official,
            patch,
            model=requested_model,
            treatment_id=treatment_id,
            candidate_provenance={
                **{key: value for key, value in candidate.items() if key != "patch"},
                "candidate_artifact": artifact_mapping,
            },
        )
        (output / "result.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if summary["accepted"] else 1
    if output.exists():
        raise ProofError("output directory already exists")
    output.mkdir(parents=True)
    if not args.live or not args.confirm_live_model_access:
        raise ProofError("live selection and explicit model-access confirmation are required")

    row = _load_official_row()
    progress.emit("preflight")
    image_verification = _verify_image_and_record(output)
    config = _adapter_config(root, model=requested_model, logical_decision_ceiling=treatment_budget.logical_decision_ceiling if treatment_budget is not None else 25)
    preflight = _preflight(config)
    # Provenance retains the exact requested alias and the resolved
    # transport fingerprint so a later config change cannot silently reuse
    # the same scientific treatment identity.
    preflight["requested_model"] = requested_model
    preflight["treatment_id"] = treatment_id
    preflight["transport_config_fingerprint"] = _model_provenance(requested_model).get(
        "transport_config_fingerprint"
    )
    preflight["prepared_treatment_revisions"] = PREPARED_TREATMENT_REVISIONS
    preflight["treatment_budget"] = treatment_budget.to_mapping() if treatment_budget is not None else None
    preflight["treatment_fingerprint"] = _treatment_fingerprint(requested_model, treatment_budget) if treatment_budget is not None else None
    preflight["image_verification"] = image_verification
    (output / "preflight.json").write_text(
        json.dumps(preflight, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with tempfile.TemporaryDirectory(prefix="cookiecutter-967-pdb-") as temporary:
        progress.emit("preparing_workspace")
        staging_root = Path(temporary) / "repository"
        fixture = staging_root / "agentic_debugger/datasets/curated" / TASK_ID
        _copy_image_source(fixture)
        _write_public_scaffold(fixture, str(row["problem_statement"]))
        case_parent = Path(temporary) / "cases"
        case_parent.mkdir()
        transport = JsonlCommandTransport(config)
        live = run_live_case(
            repository_root=str(staging_root),
            task_id=TASK_ID,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            repetition=1,
            workspace_parent=str(case_parent),
            config=config,
            limits=LiveRunLimits(
                max_model_requests=treatment_budget.max_model_requests if treatment_budget is not None else 25,
                max_controller_steps=treatment_budget.max_controller_steps if treatment_budget is not None else 25,
                max_model_phase_seconds=3600,
                max_retries=treatment_budget.max_retries if treatment_budget is not None else 0,
                continue_on_task_failure=False,
                treatment_budget=treatment_budget,
            ),
            transport=transport,
            evaluation_id=treatment_id,
            retain_observable_model_directives=True,
            scenario_override=_scenario(),
            progress_observer=progress.emit,
        )
        case = live.to_mapping()
        (output / "live-results.json").write_text(
            json.dumps(case, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            candidate = _candidate_patch_record(case)
        except ProofError as exc:
            summary = _operator_failure_summary(
                case,
                model=requested_model,
                treatment_id=treatment_id,
                failure_kind="candidate_unavailable",
                failure=exc,
            )
            progress.emit("cleanup")
            (output / "result.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            # The operator has written its result, but the process has not
            # exited yet.  ``completed`` is reserved for the supervising
            # application after process exit, result parsing, and cleanup.
            progress.emit("finalizing")
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 2
        patch = candidate["patch"]
        progress.emit("candidate")
        artifact = _canonicalize_level32_candidate(fixture, patch, parent_dir=Path(temporary))
        artifact_mapping = _write_candidate_artifacts(output, patch, artifact)
        with tempfile.TemporaryDirectory(prefix="cookiecutter-967-private-eval-") as private:
            progress.emit("official_verification_preparing")
            progress.emit("official_evaluator_started")
            official = _official_evaluate(
                row,
                artifact.patch,
                Path(private),
                raw_patch=patch,
                candidate_artifact=artifact_mapping,
            )
            progress.emit("official_evaluator_completed")
        (output / "official-verifier-summary.json").write_text(
            json.dumps(official, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    progress.emit("cleanup")
    summary = _result_summary(
        case,
        official,
        patch,
        model=requested_model,
        treatment_id=treatment_id,
        candidate_provenance={
            **{key: value for key, value in candidate.items() if key != "patch"},
            "candidate_artifact": artifact_mapping,
        },
    )
    (output / "result.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # The operator has written a result but is still inside ``main``.  The
    # application supervisor emits ``completed`` only after process exit and
    # authoritative result/cleanup projection.
    progress.emit("finalizing")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["accepted"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProofError as exc:
        print(f"COOKIECUTTER 967 PDB PROOF: NOT ACCEPTED ({exc})", file=sys.stderr)
        raise SystemExit(2)
