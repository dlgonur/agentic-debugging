"""Fail-closed, zero-provider runner for GPT-OSS SWE-rebench DEVQUAL-10 V2.

The historical Pilot-10 runner remains the execution implementation.  This
entry point supplies a separate experiment identity, manifest binding,
external root, and authorization contract so qualification cannot rewrite or
silently reuse the historical V1 runtime identity.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from agentic_debugger.application.command_config import CommandModelConfigStore
from agentic_debugger.swerebench.devqual import (
    DEVQUAL_EXPERIMENT_ID,
    DEVQUAL_DIR,
    DEVQUAL_EXTERNAL_ROOT,
    DEVQUAL_FROZEN_DIR,
    DEVQUAL_PREFLIGHT_ROOT,
    DEVQUAL_SELECTION_HASHES,
    devqual_manifest_path,
    load_devqual_contract,
    validate_devqual_identity,
)
from agentic_debugger.swerebench.hashing import sha256_file
from agentic_debugger.swerebench.provenance import current_git_head, harness_content_sha256
from agentic_debugger.swerebench.provenance import HARNESS_PATHS, working_tree_dirty
from agentic_debugger.swerebench.mapping import build_model_task
from agentic_debugger.swerebench.records import (
    OfficialInstanceBundle,
    PublicInstanceRecord,
    VerifierPrivateRecord,
)
from agentic_debugger.swerebench.execution import (
    OfficialSWERebenchVerifier,
    build_docker_execution_context,
    create_external_execution_root,
    inspect_external_root_target,
)
from agentic_debugger.swerebench.preflight import (
    load_preflight_bundle,
    run_task_preflight,
    run_zero_provider_authorization_preflight,
    write_preflight_bundle,
    write_json,
)
from agentic_debugger.swerebench.records import load_official_bundles, parquet_identity
from agentic_debugger.swerebench.selection import OrderedTask
from agentic_debugger.swerebench.authority import repository_root

try:
    from scripts.gpt_oss_swerebench_v2_pilot10 import (
        MODEL_ALIAS,
        PROFILE_ID,
        UPSTREAM_MODEL,
        authorization_evidence_path,
        _run_authorized_pilot10,
    )
except ModuleNotFoundError:  # direct ``python scripts/...`` invocation
    from gpt_oss_swerebench_v2_pilot10 import (  # type: ignore[no-redef]
        MODEL_ALIAS,
        PROFILE_ID,
        UPSTREAM_MODEL,
        authorization_evidence_path,
        _run_authorized_pilot10,
    )


def _selection_hashes() -> dict[str, str]:
    return {
        **DEVQUAL_SELECTION_HASHES,
        "pilot10_manifest.json": sha256_file(devqual_manifest_path()),
    }


def _selection_files() -> dict[str, Path]:
    historical = (
        repository_root()
        / "experiments"
        / "gpt_oss_swerebench_v2_pilot10"
        / "frozen"
    )
    return {
        "population.json": historical / "population.json",
        "full_ordering.json": historical / "full_ordering.json",
        "pilot10_manifest.json": devqual_manifest_path(),
    }


def _preflight_summary(args: argparse.Namespace) -> Path:
    value = getattr(args, "preflight_summary", None) or getattr(args, "output_summary", None)
    if not value:
        raise ValueError("DEVQUAL preflight summary path is required")
    return Path(value).resolve()


def _preflight_record_dir(args: argparse.Namespace) -> Path:
    return (_preflight_summary(args).parent / "records").resolve()


def _campaign_root(args: argparse.Namespace) -> Path:
    return Path(args.external_root).resolve()


def _authorization_path(args: argparse.Namespace) -> Path:
    return authorization_evidence_path(
        Path(args.config_root),
        Path(args.authorization_output) if args.authorization_output else None,
        project=repository_root(),
    )


def _inside_repository(path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(repository_root().resolve())
    except ValueError:
        return False
    return True


def _ensure_distinct_runtime_roots(args: argparse.Namespace) -> None:
    readiness = _preflight_summary(args).parent
    campaign = _campaign_root(args)
    if readiness == campaign or campaign.is_relative_to(readiness):
        raise SystemExit(
            "DEVQUAL readiness evidence root must be distinct from and outside the campaign root"
        )


def _load_tasks() -> list[OrderedTask]:
    payload = json.loads(devqual_manifest_path().read_text(encoding="utf-8"))
    return [OrderedTask(**row) for row in payload["tasks"]]


def _historical_v1_status(contract: object) -> str:
    if not isinstance(contract, dict):
        raise ValueError("DEVQUAL execution contract must be an object")
    historical_v1 = contract.get("historical_v1")
    if not isinstance(historical_v1, dict):
        raise ValueError("DEVQUAL execution contract historical_v1 must be an object")
    status = historical_v1.get("status")
    if not isinstance(status, str) or not status.strip():
        raise ValueError(
            "DEVQUAL execution contract historical_v1.status must be a non-empty string"
        )
    return status


def _authorize(args: argparse.Namespace) -> dict:
    _ensure_distinct_runtime_roots(args)
    identity = validate_devqual_identity(project=repository_root())
    contract = load_devqual_contract()
    config_root = Path(args.config_root)
    summary = _preflight_summary(args)
    record_dir = _preflight_record_dir(args)
    expected_ids = [task.instance_id for task in _load_tasks()]
    result = run_zero_provider_authorization_preflight(
        frozen=DEVQUAL_FROZEN_DIR,
        config_root=config_root,
        profile_id=args.profile_id,
        external_root=_campaign_root(args),
        preflight_summary=summary,
        expected_alias=args.expected_alias,
        expected_upstream=args.expected_upstream,
        selection_hashes=_selection_hashes(),
        selection_files=_selection_files(),
        preflight_record_dir=record_dir,
        expected_preflight_instance_ids=expected_ids,
    )
    result["experiment_id"] = DEVQUAL_EXPERIMENT_ID
    result["qualification_only"] = True
    result["historical_v1_status"] = _historical_v1_status(contract)
    result["devqual_identity"] = identity
    result["provider_generation_calls"] = 0
    return result


def _cmd_validate(_args: argparse.Namespace) -> int:
    identity = validate_devqual_identity(project=repository_root())
    contract = load_devqual_contract()
    if contract["experiment_id"] != DEVQUAL_EXPERIMENT_ID:
        raise SystemExit("DEVQUAL experiment identity mismatch")
    if contract["runtime_head_policy"] != "record_at_execution":
        raise SystemExit("DEVQUAL runtime HEAD policy is not record-at-execution")
    if contract["controller"]["attempts_per_task"] != 1:
        raise SystemExit("DEVQUAL must use one attempt per task")
    retry_layers = contract.get("provider", {}).get("retry_layers", {})
    if retry_layers.get("adapter_provider_retry") != 0 or retry_layers.get(
        "adapter_fallback"
    ) != 0 or retry_layers.get("configured_source_live_run_limits_max_retries") != 2:
        raise SystemExit("DEVQUAL retry layers are not explicitly bound")
    print(json.dumps({"status": "validated", **identity, "provider_generation_calls": 0}, indent=2))
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    validate_devqual_identity(project=repository_root())
    _ensure_distinct_runtime_roots(args)
    if _inside_repository(_preflight_summary(args).parent):
        raise SystemExit("DEVQUAL preflight evidence must be stored outside the repository")
    if _inside_repository(_campaign_root(args)):
        raise SystemExit("DEVQUAL campaign root must be outside the repository")
    tasks = _load_tasks()
    bundles = load_official_bundles(item.instance_id for item in tasks)
    records = [
        run_task_preflight(
            task,
            bundles[task.instance_id],
            external_root=_campaign_root(args),
        )
        for task in tasks
    ]
    summary = {
        "experiment_id": DEVQUAL_EXPERIMENT_ID,
        "parquet": parquet_identity(),
        "n": len(records),
        "ready": sum(
            item.get("authorization_status") == "ready-for-authorized-execution"
            for item in records
        ),
        "invalid": [
            {
                "instance_id": item["instance_id"],
                "reason": item.get("exclusion_reason"),
                "authorization_status": item.get("authorization_status"),
            }
            for item in records
            if item.get("authorization_status") != "ready-for-authorized-execution"
        ],
        "records": [
            {
                "instance_id": item["instance_id"],
                "authorization_status": item.get("authorization_status"),
                "model_facing_isolated": bool(item.get("model_facing_isolated")),
                "model_side_runtime_ready": bool(item.get("model_side_runtime_ready")),
                "verifier_environment_ready": bool(item.get("verifier_environment_ready")),
                "verifier_baseline_valid": bool(item.get("verifier_baseline_valid")),
                "pdb_classification": (item.get("pdb") or {}).get("classification"),
            }
            for item in records
        ],
        "external_root": str(Path(args.external_root)),
    }
    bound = write_preflight_bundle(
        _preflight_summary(args).parent,
        summary=summary,
        records=records,
    )
    print(json.dumps(bound, indent=2))
    return 0 if not bound["invalid"] else 2


def _cmd_authorize(args: argparse.Namespace) -> int:
    result = _authorize(args)
    output = _authorization_path(args)
    write_json(output, result)
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 2


def _cmd_execute(args: argparse.Namespace) -> int:
    if not args.provider_authorized:
        raise SystemExit(
            "provider inference is fail-closed; pass --provider-authorized only "
            "from a separately authorized development qualification task"
        )
    authorization_path = _authorization_path(args)
    if not authorization_path.is_file():
        raise SystemExit(f"DEVQUAL authorization evidence is missing: {authorization_path}")
    try:
        authorized = json.loads(authorization_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"DEVQUAL authorization evidence is invalid: {exc}") from exc
    result = _authorize(args)
    if not result["ready"]:
        raise SystemExit("DEVQUAL authorization failed: " + "; ".join(result["reasons"]))
    if authorized.get("ready") is not True:
        raise SystemExit("DEVQUAL authorization evidence is not ready")
    if authorized.get("preflight_evidence_fingerprint") != result.get(
        "preflight_evidence_fingerprint"
    ):
        raise SystemExit("DEVQUAL preflight evidence changed after authorization")
    authorized_profile = (authorized.get("profile_metadata") or {}).get(
        "configuration_fingerprint"
    )
    current_profile = (result.get("profile_metadata") or {}).get(
        "configuration_fingerprint"
    )
    if authorized_profile != current_profile:
        raise SystemExit("DEVQUAL model profile changed after authorization")
    fingerprint = (result.get("profile_metadata") or {}).get("configuration_fingerprint")
    if not isinstance(fingerprint, str):
        raise SystemExit("DEVQUAL authorization did not provide a profile fingerprint")
    return _run_authorized_pilot10(
        args,
        DEVQUAL_FROZEN_DIR,
        profile_fingerprint=fingerprint,
        preflight_record_dir=_preflight_record_dir(args),
        preflight_evidence_fingerprint=result["preflight_evidence_fingerprint"],
        expected_preflight_instance_ids=[task.instance_id for task in _load_tasks()],
        run_id_prefix="devqual10-v2",
        rows_filename="devqual10_rows.json",
        campaign_metadata={
            "experiment_id": DEVQUAL_EXPERIMENT_ID,
            "status": "DEVELOPMENT_QUALIFICATION_ONLY",
            "manifest_sha256": _selection_hashes()["pilot10_manifest.json"],
            "repaired_harness_sha256": harness_content_sha256(repository_root()),
            "runtime_git_head": current_git_head(repository_root()),
            "profile_id": args.profile_id,
            "profile_fingerprint": fingerprint,
            "preflight_evidence_fingerprint": result["preflight_evidence_fingerprint"],
            "qualification_only_boundary": (
                "Results cannot be promoted into an unseen benchmark capability score."
            ),
            "run_id_prefix": "devqual10-v2-<order>",
        },
    )


def _smoke_repo(destination: Path) -> Path:
    repo = destination / "candidate-repo"
    repo.mkdir()
    source_root = repository_root()
    for relative in HARNESS_PATHS:
        source = source_root / relative
        target = repo / relative
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for experiment_id in (
        "gpt_oss_swerebench_v2_pilot10",
        DEVQUAL_EXPERIMENT_ID,
    ):
        source = source_root / "experiments" / experiment_id / "frozen"
        target = repo / "experiments" / experiment_id / "frozen"
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    contract_path = repo / "experiments" / DEVQUAL_EXPERIMENT_ID / "frozen" / "execution_contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["harness"]["harness_content_sha256"] = harness_content_sha256(repo)
    write_json(contract_path, contract)
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "smoke@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "devqual-smoke"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "devqual zero-provider smoke"], cwd=repo, check=True)
    return repo


def _smoke_profile(config_root: Path, repo: Path) -> None:
    write_json(
        config_root / "config" / "command-models.json",
        {
            "schema_version": "command-models-v1",
            "profiles": [
                {
                    "profile_id": PROFILE_ID,
                    "display_name": "Ollama Cloud GPT-OSS 20B",
                    "executable": sys.executable,
                    "argv": [str(repo / "scripts" / "ollama_cloud_command_adapter.py"), "--model", MODEL_ALIAS],
                    "cwd": str(repo),
                    "request_timeout_seconds": 60,
                    "protocol_version": "1.3",
                }
            ],
        },
    )


def _smoke_records(tasks: list[OrderedTask], campaign_root: Path) -> list[dict]:
    return [
        {
            "schema_version": "gpt-oss-swerebench-v2-task-preflight-v1",
            "synthetic_smoke": True,
            "instance_id": task.instance_id,
            "product_task_id": f"swr-{task.instance_id}",
            "repo": task.repo,
            "repo_canonical": task.repo_canonical,
            "base_commit": task.base_commit,
            "order_index": task.order_index,
            "external_root": str(campaign_root),
            "model_facing_isolated": True,
            "model_side_runtime_ready": True,
            "verifier_environment_ready": True,
            "verifier_baseline_valid": True,
            "verifier_gold_valid": True,
            "authorization_status": "ready-for-authorized-execution",
            "pdb": {"classification": "PDB_DEFERRED_TO_SEPARATE_TREATMENT"},
        }
        for task in tasks
    ]


def _cmd_smoke(args: argparse.Namespace) -> int:
    """Exercise the DEVQUAL lifecycle with synthetic non-generative probes."""

    output = Path(args.output_path) if args.output_path else DEVQUAL_PREFLIGHT_ROOT / "zero_provider_top_level_smoke.json"
    if _inside_repository(output):
        raise SystemExit("DEVQUAL smoke evidence must be stored outside the repository")
    with tempfile.TemporaryDirectory(prefix="devqual10-v2-smoke-") as temporary:
        temp = Path(temporary)
        clean_repo = _smoke_repo(temp)
        clean_frozen = clean_repo / "experiments" / DEVQUAL_EXPERIMENT_ID / "frozen"
        config_root = temp / "config-root"
        _smoke_profile(config_root, clean_repo)
        readiness_root = temp / "readiness"
        campaign_root = temp / "campaign"
        tasks = _load_tasks()
        expected_ids = [task.instance_id for task in tasks]
        summary = {
            "experiment_id": DEVQUAL_EXPERIMENT_ID,
            "parquet": parquet_identity(),
            "n": 10,
            "ready": 10,
            "invalid": [],
            "records": [
                {
                    "instance_id": task.instance_id,
                    "authorization_status": "ready-for-authorized-execution",
                    "model_facing_isolated": True,
                    "model_side_runtime_ready": True,
                    "verifier_environment_ready": True,
                    "verifier_baseline_valid": True,
                    "pdb_classification": "PDB_DEFERRED_TO_SEPARATE_TREATMENT",
                }
                for task in tasks
            ],
            "external_root": str(campaign_root),
        }
        records = _smoke_records(tasks, campaign_root)
        summary_path = readiness_root / "summary.json"
        frozen_before = {
            path.relative_to(clean_frozen).as_posix(): sha256_file(path)
            for path in clean_frozen.rglob("*")
            if path.is_file()
        }
        if readiness_root.exists() or campaign_root.exists():
            raise SystemExit("DEVQUAL smoke roots were not initially absent")
        bound = write_preflight_bundle(readiness_root, summary=summary, records=records)

        def fake_provider_probe(alias: str, upstream: str) -> dict[str, object]:
            return {
                "schema_version": "ollama-cloud-preflight-v1",
                "expected_model": alias,
                "expected_remote_model": upstream,
                "provider_inference_started": False,
                "cloud_inference_verified": False,
            }

        def fake_docker() -> dict[str, object]:
            return {"executable_available": True, "daemon_reachable": True, "synthetic": True, "reason": None}

        selection_files = {
            "population.json": clean_repo / "experiments" / "gpt_oss_swerebench_v2_pilot10" / "frozen" / "population.json",
            "full_ordering.json": clean_repo / "experiments" / "gpt_oss_swerebench_v2_pilot10" / "frozen" / "full_ordering.json",
            "pilot10_manifest.json": clean_frozen / "pilot10_manifest.json",
        }
        gate_kwargs = {
            "frozen": clean_frozen,
            "config_root": config_root,
            "profile_id": PROFILE_ID,
            "external_root": campaign_root,
            "preflight_summary": summary_path,
            "repository_path": clean_repo,
            "provider_metadata_preflight": fake_provider_probe,
            "docker_readiness_probe": fake_docker,
            "selection_hashes": {
                **DEVQUAL_SELECTION_HASHES,
                "pilot10_manifest.json": sha256_file(selection_files["pilot10_manifest.json"]),
            },
            "selection_files": selection_files,
            "preflight_record_dir": readiness_root / "records",
            "expected_preflight_instance_ids": expected_ids,
        }
        clean_before = not working_tree_dirty(clean_repo)
        first_gate = run_zero_provider_authorization_preflight(**gate_kwargs)
        auth_path = authorization_evidence_path(config_root, project=clean_repo)
        write_json(auth_path, first_gate)
        authorized = json.loads(auth_path.read_text(encoding="utf-8"))
        authorization_bound = authorized.get("preflight_evidence_fingerprint") == first_gate.get(
            "preflight_evidence_fingerprint"
        )
        clean_after_authorize = not working_tree_dirty(clean_repo)
        root_before_execute = inspect_external_root_target(campaign_root, project_root=clean_repo)
        second_gate = run_zero_provider_authorization_preflight(**gate_kwargs)
        if not first_gate["ready"] or not second_gate["ready"]:
            raise SystemExit("DEVQUAL smoke authorization gate failed")
        if first_gate["preflight_evidence_fingerprint"] != second_gate["preflight_evidence_fingerprint"]:
            raise SystemExit("DEVQUAL smoke preflight fingerprint changed")
        created_root = create_external_execution_root(campaign_root, project_root=clean_repo)
        _summary, actual_records, fingerprint = load_preflight_bundle(
            summary_path,
            record_dir=readiness_root / "records",
            expected_instance_ids=expected_ids,
        )
        first_record = actual_records[expected_ids[0]]
        synthetic_public = PublicInstanceRecord(
            instance_id=tasks[0].instance_id,
            repo=tasks[0].repo,
            base_commit=tasks[0].base_commit,
            problem_statement="synthetic smoke public problem",
            language="python",
            license="synthetic",
            created_at="synthetic",
            problem_statement_sha256="0" * 64,
        )
        synthetic_private = VerifierPrivateRecord(
            instance_id=tasks[0].instance_id,
            fail_to_pass=("tests/test_public.py::test_public",),
            pass_to_pass=(),
            test_cmd="pytest -q",
            image_name="synthetic-image",
            python_version="3.11",
            has_gold_patch=True,
            has_test_patch=True,
            gold_patch_sha256="0" * 64,
            test_patch_sha256="0" * 64,
        )
        synthetic_bundle = OfficialInstanceBundle(
            public=synthetic_public,
            private=synthetic_private,
            _gold_patch="",
            _test_patch="",
            _fail_to_pass=synthetic_private.fail_to_pass,
            _pass_to_pass=(),
            _test_cmd="pytest -q",
            _install_config={},
            _image_name="synthetic-image",
        )
        build_model_task(tasks[0], synthetic_bundle, fixture_path=".", allowed_write_paths=["src"])
        build_docker_execution_context(
            bundle=synthetic_bundle,
            external_root=created_root,
            instance_id=tasks[0].instance_id,
            manifest_fingerprint=tasks[0].assignment_key,
            authority_revision=tasks[0].base_commit,
            project=tasks[0].repo,
            bug_id=tasks[0].instance_id,
            buggy_revision=tasks[0].base_commit,
        )
        OfficialSWERebenchVerifier(synthetic_bundle, work_root=created_root, baseline_valid=True)
        frozen_after = {
            path.relative_to(clean_frozen).as_posix(): sha256_file(path)
            for path in clean_frozen.rglob("*")
            if path.is_file()
        }
        evidence = {
            "schema_version": "gpt-oss-swerebench-v2-devqual-zero-provider-smoke-v1",
            "clean_disposable_candidate": clean_before,
            "preflight_root_initially_absent": True,
            "preflight_summary_written_outside_git": not _inside_repository(summary_path),
            "per_task_record_count": len(actual_records),
            "campaign_root_absent_before_executor": root_before_execute["state"] == "nonexistent_target",
            "git_clean_after_preflight": clean_before,
            "git_clean_after_authorize": clean_after_authorize,
            "authorization_evidence_outside_git": not _inside_repository(auth_path),
            "authorization_bound_to_preflight": authorization_bound,
            "second_authorization_gate_passed": second_gate["ready"],
            "preflight_evidence_fingerprint": fingerprint,
            "campaign_root_created_by_executor": created_root.is_dir(),
            "first_task_preflight_record_resolved": (
                first_record.get("instance_id") == expected_ids[0]
                and first_record.get("verifier_baseline_valid") is True
            ),
            "worker_session_runtime_verifier_preparation": True,
            "frozen_inputs_unchanged": frozen_before == frozen_after,
            "reached_provider_inference_boundary": True,
            "provider_execution_authorized": False,
            "provider_inference_started": False,
            "tasks_with_transport_attempts": 0,
            "transport_attempts": 0,
            "provider_generation_calls": 0,
            "stopped_before_provider_generation": True,
        }
    write_json(output, evidence)
    print(json.dumps(evidence, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.set_defaults(func=_cmd_validate)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--external-root", default=str(DEVQUAL_EXTERNAL_ROOT))
    preflight.add_argument("--output-summary", default=str(DEVQUAL_PREFLIGHT_ROOT / "summary.json"))
    preflight.set_defaults(func=_cmd_preflight)
    for name, func in (("authorize", _cmd_authorize), ("execute", _cmd_execute)):
        command = sub.add_parser(name)
        command.add_argument("--config-root", required=True)
        command.add_argument("--profile-id", default=PROFILE_ID)
        command.add_argument("--external-root", default=str(DEVQUAL_EXTERNAL_ROOT))
        command.add_argument("--preflight-summary", default=str(DEVQUAL_PREFLIGHT_ROOT / "summary.json"))
        command.add_argument(
            "--authorization-output",
            default=None,
        )
        command.add_argument("--expected-alias", default=MODEL_ALIAS)
        command.add_argument("--expected-upstream", default=UPSTREAM_MODEL)
        if name == "execute":
            command.add_argument("--provider-authorized", action="store_true")
            command.add_argument("--output-dir", default=str(DEVQUAL_DIR / "outputs"))
        command.set_defaults(func=func)
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--output-path", default=None)
    smoke.set_defaults(func=_cmd_smoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
