"""Freeze, validate, and preflight the GPT-OSS SWE-rebench V2 Pilot-10 path.

The default commands never start provider inference.  The explicit
``execute --provider-authorized`` path is fail-closed behind the zero-provider
authorization gate and is reserved for a separately authorized task.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from agentic_debugger.swerebench.authority import (
    EXPERIMENT_ID,
    DEFAULT_EXTERNAL_ROOT,
    PARENT_BASELINE,
    frozen_dir,
    repository_root,
)
from agentic_debugger.swerebench.freeze import EXECUTION_CONTRACT, freeze_population_and_order
from agentic_debugger.swerebench.hashing import sha256_file
from agentic_debugger.swerebench.preflight import (
    load_preflight_bundle,
    run_task_preflight,
    write_json,
)
from agentic_debugger.swerebench.preflight import run_zero_provider_authorization_preflight
from agentic_debugger.swerebench.mapping import build_model_task, product_task_id, production_write_paths
from agentic_debugger.swerebench.materialize import materialize_base_commit
from agentic_debugger.swerebench.execution import (
    OfficialSWERebenchVerifier,
    build_docker_execution_context,
    create_external_execution_root,
    inspect_external_root_target,
    write_private_bundle,
)
from agentic_debugger.application.events import SourceKind
from agentic_debugger.application.command_config import CommandModelConfigStore
from agentic_debugger.application.session import SessionBudgets, SessionSpec
from agentic_debugger.application.sources import ExecutionSourceSpec
from agentic_debugger.application.worker_process import SessionWorkerProcess
from agentic_debugger.swerebench.records import load_official_bundles, parquet_identity
from agentic_debugger.swerebench.isolation import (
    assert_model_facing_isolated,
    hidden_needles_from_private,
)
from agentic_debugger.swerebench.mapping import build_verifier_task
from agentic_debugger.swerebench.provenance import HARNESS_PATHS
from agentic_debugger.swerebench.schema import validate_pilot_result
from agentic_debugger.swerebench.schema import (
    classify_execution_result,
    empty_result_template,
)
from agentic_debugger.swerebench.provenance import current_git_head, harness_content_sha256
from agentic_debugger.swerebench.provenance import working_tree_dirty
from agentic_debugger.swerebench.result_rows import durable_session_evidence


PROFILE_ID = "ollama-cloud-gpt-oss-20b"
PROFILE_DISPLAY_NAME = "Ollama Cloud GPT-OSS 20B"
MODEL_ALIAS = "gpt-oss:20b-cloud"
UPSTREAM_MODEL = "gpt-oss:20b"
PROTOCOL_VERSION = "1.3"


def _path_inside_repository(path: Path, project: Path | None = None) -> bool:
    candidate = path.resolve(strict=False)
    root = (project or repository_root()).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def authorization_evidence_path(
    config_root: Path,
    explicit_path: Path | None = None,
    *,
    project: Path | None = None,
) -> Path:
    """Resolve app-owned authorization evidence and reject repository paths."""

    path = (
        explicit_path
        if explicit_path is not None
        else config_root / "authorization" / f"{EXPERIMENT_ID}.json"
    ).resolve(strict=False)
    if _path_inside_repository(path, project):
        raise ValueError(
            "authorization evidence must resolve outside the repository"
        )
    return path


def provider_execution_truth(rows: list[dict]) -> dict[str, int | bool]:
    """Project provider execution only from durable generation-boundary evidence."""

    transport_attempts = sum(
        int((row.get("runtime") or {}).get("transport_attempts") or 0)
        for row in rows
    )
    tasks_with_transport_attempts = sum(
        int(((row.get("runtime") or {}).get("transport_attempts") or 0) > 0)
        for row in rows
    )
    provider_generation_calls = sum(
        int((row.get("runtime") or {}).get("provider_generation_calls") or 0)
        for row in rows
    )
    return {
        "provider_execution_authorized": True,
        "provider_inference_started": provider_generation_calls > 0,
        "tasks_with_transport_attempts": tasks_with_transport_attempts,
        "transport_attempts": transport_attempts,
        "provider_generation_calls": provider_generation_calls,
    }


def _run_authorization_gate(
    *,
    frozen: Path,
    config_root: Path,
    profile_id: str,
    external_root: Path,
    preflight_summary: Path,
    expected_alias: str = MODEL_ALIAS,
    expected_upstream: str = UPSTREAM_MODEL,
    repository_path: Path | None = None,
    provider_metadata_preflight=None,
    docker_readiness_probe=None,
) -> dict:
    return run_zero_provider_authorization_preflight(
        frozen=frozen,
        config_root=config_root,
        profile_id=profile_id,
        external_root=external_root,
        preflight_summary=preflight_summary,
        expected_alias=expected_alias,
        expected_upstream=expected_upstream,
        repository_path=repository_path,
        provider_metadata_preflight=provider_metadata_preflight,
        docker_readiness_probe=docker_readiness_probe,
    )


def _cmd_freeze(args: argparse.Namespace) -> int:
    result = freeze_population_and_order(Path(args.output_dir) if args.output_dir else None)
    print(json.dumps({"status": "frozen", "hashes": result["hashes"]}, indent=2))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    output = Path(args.output_dir) if args.output_dir else frozen_dir()
    first = freeze_population_and_order(output)
    second = freeze_population_and_order(output)
    if first["hashes"] != second["hashes"]:
        raise SystemExit("selection is not reproducible")
    pilot = json.loads((output / "pilot10_manifest.json").read_text(encoding="utf-8"))
    if len(pilot["selected_instance_ids"]) != 10:
        raise SystemExit("Pilot-10 does not contain 10 tasks")
    if len(set(pilot["selected_repos"])) != 10:
        raise SystemExit("Pilot-10 is not repository-diverse")
    if HISTORICAL := "curated-none-handling-001":
        if HISTORICAL in pilot["selected_instance_ids"]:
            raise SystemExit("historical curated product task leaked into Pilot-10")
    template = {
        "schema_version": "gpt-oss-swerebench-v2-pilot-result-v1",
        "identity": {
            "task_id": "swr-example",
            "instance_id": "example__repo-1",
            "repository": "example/repo",
            "base_commit": "0" * 40,
            "manifest_order_index": 1,
            "harness_commit": "9a47001",
            "model_profile_id": "ollama-cloud-gpt-oss-20b",
            "model_alias": "gpt-oss:20b-cloud",
            "upstream_model": "gpt-oss:20b",
            "policy": "pdb-on-uncertainty",
            "protocol": "1.3",
        },
        "runtime": {
            "session_id": None,
            "wall_clock_seconds": None,
            "logical_model_calls": None,
            "transport_attempts": None,
            "adapter_retry_count": 0,
            "fallback_count": 0,
            "token_usage": None,
            "provider_failures": None,
        },
        "trajectory": {
            "baseline_reproduced": None,
            "understand_reached": None,
            "hypotheses": None,
            "source_operations": None,
            "test_operations": None,
            "patch_attempts": None,
            "patch_rejections": None,
            "candidate_applied": None,
            "validate_sequence": None,
            "terminal_reason": None,
        },
        "pdb": {
            "pdb_eligible": True,
            "pdb_gate_opened": False,
            "pdb_entered": False,
            "debugger_actions": 0,
            "debugger_observations": 0,
            "runtime_evidence_preceded_patch": False,
            "pdb_not_exercised": True,
            "classification": "pdb_unavailable_by_treatment_contract",
        },
        "verification": {
            "verifier_ran": False,
            "verifier_infrastructure_valid": True,
            "baseline_valid": None,
            "fail_to_pass": None,
            "pass_to_pass": None,
            "full_suite": None,
            "verifier_outcome": None,
            "cleanup": None,
        },
        "science": {
            "admissible_model_result": False,
            "infrastructure_invalid": False,
            "contaminated": False,
            "provider_invalid": False,
            "resolved": False,
            "unresolved": True,
            "debugger_assisted_resolved": False,
            "execution_classification": "admissible_model_failure",
            "classification": "admissible_unresolved",
        },
    }
    try:
        validate_pilot_result(template)
    except Exception as exc:
        raise SystemExit(f"result schema self-check failed: {exc}") from exc
    bad = json.loads(json.dumps(template))
    bad["science"]["resolved"] = True
    bad["science"]["debugger_assisted_resolved"] = True
    try:
        validate_pilot_result(bad)
    except Exception:
        pass
    else:
        raise SystemExit("result schema failed to reject debugger-assisted RESOLVED without PDB")
    print(
        json.dumps(
            {
                "status": "validated",
                "experiment_id": EXPERIMENT_ID,
                "pilot10": pilot["selected_instance_ids"],
                "hashes": first["hashes"],
                "execution_contract_policy": EXECUTION_CONTRACT["controller"]["policy"],
            },
            indent=2,
        )
    )
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    output = Path(args.output_dir) if args.output_dir else frozen_dir()
    freeze_population_and_order(output)
    pilot = json.loads((output / "pilot10_manifest.json").read_text(encoding="utf-8"))
    from agentic_debugger.swerebench.selection import OrderedTask

    tasks = [OrderedTask(**row) for row in pilot["tasks"]]
    bundles = load_official_bundles(item.instance_id for item in tasks)
    records = []
    for task in tasks:
        record = run_task_preflight(
            task,
            bundles[task.instance_id],
            external_root=Path(args.external_root) if args.external_root else None,
        )
        records.append(record)
        write_json(output / "preflight" / f"{task.instance_id}.json", record)
    summary = {
        "experiment_id": EXPERIMENT_ID,
        "parquet": parquet_identity(),
        "n": len(records),
        "ready": sum(
            1
            for item in records
            if item.get("authorization_status") == "ready-for-authorized-execution"
        ),
        "invalid": [
            {
                "instance_id": item["instance_id"],
                "reason": item.get("exclusion_reason"),
                "authorization_status": item.get("authorization_status"),
                "verifier_environment_ready": item.get("verifier_environment_ready"),
                "verifier_baseline_valid": item.get("verifier_baseline_valid"),
                "verifier_gold_valid": item.get("verifier_gold_valid"),
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
        "pdb_classifications": [
            {
                "instance_id": item["instance_id"],
                "classification": (item.get("pdb") or {}).get("classification"),
            }
            for item in records
        ],
        "external_root": args.external_root or str(DEFAULT_EXTERNAL_ROOT),
    }
    write_json(output / "preflight" / "summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0 if not summary["invalid"] else 2


def _cmd_execute(args: argparse.Namespace) -> int:
    if not args.provider_authorized:
        raise SystemExit(
            "provider inference is fail-closed; pass --provider-authorized only "
            "from a separately authorized execution task"
        )
    if not args.config_root:
        raise SystemExit("--config-root is required for explicit provider execution")
    output = Path(args.output_dir) if args.output_dir else frozen_dir()
    gate = _run_authorization_gate(
        frozen=output,
        config_root=Path(args.config_root),
        profile_id=args.profile_id,
        external_root=Path(args.external_root) if args.external_root else DEFAULT_EXTERNAL_ROOT,
        preflight_summary=Path(args.preflight_summary) if args.preflight_summary else output / "preflight" / "summary.json",
        expected_alias=args.expected_alias,
        expected_upstream=args.expected_upstream,
    )
    if not gate["ready"]:
        raise SystemExit("zero-provider authorization preflight failed: " + "; ".join(gate["reasons"]))
    profile_fingerprint = (gate.get("profile_metadata") or {}).get(
        "configuration_fingerprint"
    )
    if not isinstance(profile_fingerprint, str):
        raise SystemExit("authorization did not provide a configured profile fingerprint")
    return _run_authorized_pilot10(args, output, profile_fingerprint=profile_fingerprint)


def _run_authorized_pilot10(
    args: argparse.Namespace,
    frozen: Path,
    *,
    profile_fingerprint: str,
    preflight_record_dir: Path | None = None,
    preflight_evidence_fingerprint: str | None = None,
    expected_preflight_instance_ids: list[str] | tuple[str, ...] | None = None,
    run_id_prefix: str = "pilot10",
    rows_filename: str = "pilot10_rows.json",
    campaign_metadata: dict | None = None,
    readiness_mode: str = "preflight",
) -> int:
    """Run the frozen rows through the real Local Application worker path.

    This function is reachable only through the explicit provider flag after
    the zero-provider gate.  It is intentionally not called by this repair.
    """

    from agentic_debugger.swerebench.selection import OrderedTask

    if readiness_mode not in {"preflight", "direct"}:
        raise ValueError("readiness_mode must be 'preflight' or 'direct'")
    if readiness_mode == "direct" and (
        preflight_record_dir is not None or preflight_evidence_fingerprint is not None
        or expected_preflight_instance_ids is not None
    ):
        raise ValueError("direct readiness mode does not accept preflight evidence")
    pilot = json.loads((frozen / "pilot10_manifest.json").read_text(encoding="utf-8"))
    tasks = [OrderedTask(**row) for row in pilot["tasks"]]
    bundles = load_official_bundles(item.instance_id for item in tasks)
    authorized_records: dict[str, dict] = {}
    if readiness_mode == "preflight" and preflight_record_dir is not None:
        summary_path = preflight_record_dir.parent / "summary.json"
        try:
            summary, authorized_records, actual_fingerprint = load_preflight_bundle(
                summary_path,
                record_dir=preflight_record_dir,
                expected_instance_ids=expected_preflight_instance_ids
                or [task.instance_id for task in tasks],
            )
        except Exception as exc:
            raise SystemExit(f"external DEVQUAL preflight evidence is invalid: {exc}") from exc
        if preflight_evidence_fingerprint != actual_fingerprint:
            raise SystemExit("external DEVQUAL preflight evidence fingerprint changed")
        for task in tasks:
            record = authorized_records.get(task.instance_id)
            if not isinstance(record, dict):
                raise SystemExit(f"external DEVQUAL preflight record is missing: {task.instance_id}")
            if record.get("instance_id") != task.instance_id:
                raise SystemExit(f"external DEVQUAL preflight identity mismatch: {task.instance_id}")
            if record.get("verifier_baseline_valid") is not True:
                raise SystemExit(f"external DEVQUAL preflight is not verifier-ready: {task.instance_id}")
    root = Path(args.external_root) if args.external_root else DEFAULT_EXTERNAL_ROOT
    try:
        root = create_external_execution_root(root)
    except Exception as exc:
        raise SystemExit(f"external root lifecycle failed: {exc}") from exc
    sources = root / "sources"
    metadata = root / "metadata"
    sessions = root / "sessions"
    if campaign_metadata is not None:
        write_json(root / "campaign_metadata.json", campaign_metadata)
    for path in (sources, metadata, sessions):
        path.mkdir()
    rows: list[dict] = []
    try:
        for ordered in tasks:
            print(f"START {ordered.order_index}/10 {ordered.instance_id}", flush=True)
            task_id = product_task_id(ordered.instance_id)
            try:
                # This try block is deliberately limited to pre-worker setup.
                # Its exception path may only produce a zero-call setup row.
                bundle = bundles[ordered.instance_id]
                source_parent = sources / ordered.instance_id
                source_parent.mkdir()
                checkout = materialize_base_commit(
                    instance_id=ordered.instance_id,
                    repo=ordered.repo,
                    repo_canonical=ordered.repo_canonical,
                    base_commit=ordered.base_commit,
                    dest_parent=source_parent,
                )
                allowed = production_write_paths(checkout)
                model_task = build_model_task(
                    ordered, bundle, fixture_path=".", allowed_write_paths=allowed
                )
                assert_model_facing_isolated(
                    model_task.agent_visible_mapping(),
                    hidden_needles=hidden_needles_from_private({
                        "patch": bundle.gold_patch(),
                        "test_patch": bundle.test_patch(),
                        "FAIL_TO_PASS": list(bundle.hidden_tests()[0]),
                        "PASS_TO_PASS": list(bundle.hidden_tests()[1]),
                    }),
                )
                task_path = metadata / f"{ordered.instance_id}.task.json"
                bundle_path = metadata / f"{ordered.instance_id}.private.json"
                task_path.write_text(
                    json.dumps(model_task.to_mapping(), ensure_ascii=True, sort_keys=True),
                    encoding="utf-8",
                )
                write_private_bundle(bundle_path, bundle)
                session_dir = sessions / task_id
                spec = SessionSpec(
                    task_id=task_id,
                    source=ExecutionSourceSpec(
                        kind=SourceKind.CONFIGURED_MODEL,
                        task_id=task_id,
                        policy="pdb-on-uncertainty",
                        model_config_ref=args.profile_id,
                    ),
                    budgets=SessionBudgets(
                        max_model_calls=64,
                        max_controller_steps=64,
                        max_elapsed_seconds=1800,
                    ),
                    artifact_destination=str(session_dir),
                )
                session_id = "sess-20260818-" + uuid.uuid4().hex[:16]
                params = {
                    "config_root": str(args.config_root),
                    "profile_id": args.profile_id,
                    "expected_fingerprint": profile_fingerprint,
                    "policy": "pdb-on-uncertainty",
                    "external_task_path": str(task_path),
                    "external_repository_root": str(checkout),
                    "external_root": str(root),
                    "external_bundle_path": str(bundle_path),
                    "external_readiness_mode": readiness_mode,
                    "external_instance_id": ordered.instance_id,
                    "external_manifest_fingerprint": ordered.assignment_key,
                    "external_authority_revision": ordered.base_commit,
                    "external_project": ordered.repo,
                    "external_bug_id": ordered.instance_id,
                    "external_buggy_revision": ordered.base_commit,
                }
                if readiness_mode == "preflight":
                    params["external_preflight_path"] = str(
                        (preflight_record_dir / f"{ordered.instance_id}.json")
                        if preflight_record_dir is not None
                        else frozen / "preflight" / f"{ordered.instance_id}.json"
                    )
                worker = SessionWorkerProcess(
                    session_dir=session_dir,
                    session_id=session_id,
                    spec=spec,
                    run_id=f"{run_id_prefix}-{ordered.order_index}",
                    scenario="configured_command_model",
                    scenario_params=params,
                    max_elapsed_seconds=1800,
                )
            except Exception as exc:
                if readiness_mode != "direct":
                    raise
                row = _direct_setup_failure_row(ordered, task_id, exc)
            else:
                # Worker startup, model execution, durable evidence loading,
                # and row projection are intentionally outside the setup
                # exception boundary.  An unexpected post-start exception
                # must fail closed rather than becoming a fabricated
                # zero-provider setup row.
                try:
                    start_result = worker.start()
                    session_result = start_result if start_result is not None else worker.wait()
                    result_mapping = session_result.to_mapping()
                finally:
                    worker.close()
                evaluation = {}
                evaluation_path = session_dir / "evaluation.json"
                if evaluation_path.is_file():
                    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
                row = _pilot_row(ordered, task_id, session_id, result_mapping, evaluation, session_dir)
            rows.append(row)
            runtime = row.get("runtime") or {}
            science = row.get("science") or {}
            print(
                f"END {ordered.order_index}/10 {ordered.instance_id} "
                f"classification={science.get('classification')} "
                f"resolved={science.get('resolved')} "
                f"model_calls={runtime.get('logical_model_calls') or 0} "
                f"transport_attempts={runtime.get('transport_attempts') or 0} "
                f"elapsed_seconds={runtime.get('wall_clock_seconds') or 0}",
                flush=True,
            )
        write_json(root / rows_filename, {"rows": rows})
        print(
            json.dumps(
                {
                    "status": "completed",
                    "rows": len(rows),
                    **provider_execution_truth(rows),
                },
                indent=2,
            )
        )
        return 0
    finally:
        # Session artifacts remain durable under the explicitly external root;
        # source checkouts and private metadata are not model result records.
        shutil.rmtree(sources, ignore_errors=True)
        shutil.rmtree(metadata, ignore_errors=True)


def _direct_setup_failure_row(ordered, task_id: str, error: Exception) -> dict:
    """Persist an honest infrastructure row when direct task setup fails."""

    row = empty_result_template()
    row["identity"].update(
        task_id=task_id,
        instance_id=ordered.instance_id,
        repository=ordered.repo,
        base_commit=ordered.base_commit,
        manifest_order_index=ordered.order_index,
        harness_commit=current_git_head(repository_root()),
        model_profile_id=PROFILE_ID,
        model_alias=MODEL_ALIAS,
        upstream_model=UPSTREAM_MODEL,
        policy="pdb-on-uncertainty",
        protocol=PROTOCOL_VERSION,
    )
    row["runtime"].update(
        session_id=None,
        logical_model_calls=0,
        transport_attempts=0,
        provider_generation_calls=0,
        adapter_retry_count=0,
        fallback_count=0,
        provider_failures=0,
    )
    row["pdb"].update(
        pdb_eligible=False,
        pdb_gate_opened=False,
        pdb_entered=False,
        debugger_actions=0,
        debugger_observations=0,
        runtime_evidence_preceded_patch=False,
        pdb_not_exercised=True,
        classification="pdb_unavailable_by_treatment_contract",
    )
    row["verification"].update(
        verifier_ran=False,
        verifier_infrastructure_valid=False,
        baseline_valid=None,
        cleanup=None,
    )
    row["science"].update(
        admissible_model_result=False,
        infrastructure_invalid=True,
        contaminated=False,
        provider_invalid=False,
        resolved=False,
        unresolved=False,
        debugger_assisted_resolved=False,
        execution_classification="infrastructure_invalid",
        classification="infrastructure_invalid",
    )
    row["notes"] = [f"direct task setup failed: {type(error).__name__}: {str(error)[:400]}"]
    validate_pilot_result(row)
    return row


def _pilot_row(ordered, task_id, session_id, session_result, evaluation, session_dir):
    session_result = session_result if isinstance(session_result, dict) else {}
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    durable = durable_session_evidence(session_dir, session_result)
    runtime_evidence = durable["runtime"]
    trajectory_evidence = durable["trajectory"]
    candidate = bool(
        trajectory_evidence["candidate_applied"]
        or (session_dir / "candidate.patch").is_file()
    )
    verifier_ran = bool(evaluation.get("verifier_ran"))
    infra_valid = (
        bool(evaluation.get("verifier_infrastructure_valid"))
        if verifier_ran or "verifier_infrastructure_valid" in evaluation
        else True
    )
    verifier_resolved = bool(
        verifier_ran and infra_valid and evaluation.get("resolved") is True
    )
    completed = session_result.get("status") in {"succeeded", "completed"}
    provider_invalid = bool(durable["provider_invalid"])
    execution_classification = classify_execution_result(
        controller_completed=completed,
        candidate_produced=candidate,
        verifier_ran=verifier_ran,
        verifier_resolved=verifier_resolved,
        verifier_infrastructure_valid=infra_valid,
        provider_invalid=provider_invalid,
        runtime_infrastructure_invalid=bool(
            durable["infrastructure_invalid"] or durable["cleanup_invalid"]
        ),
    )
    row = empty_result_template()
    row["identity"].update(
        task_id=task_id,
        instance_id=ordered.instance_id,
        repository=ordered.repo,
        base_commit=ordered.base_commit,
        manifest_order_index=ordered.order_index,
        harness_commit=current_git_head(repository_root()),
        model_profile_id="ollama-cloud-gpt-oss-20b",
        model_alias="gpt-oss:20b-cloud",
        upstream_model="gpt-oss:20b",
        policy="pdb-on-uncertainty",
        protocol="1.3",
    )
    row["runtime"].update(session_id=session_id, **runtime_evidence)
    row["trajectory"].update(**trajectory_evidence)
    row["pdb"].update(
        pdb_eligible=False,
        pdb_gate_opened=False,
        pdb_entered=False,
        debugger_actions=0,
        debugger_observations=0,
        runtime_evidence_preceded_patch=False,
        pdb_not_exercised=True,
        classification="pdb_unavailable_by_treatment_contract",
    )
    row["verification"].update(
        verifier_ran=verifier_ran,
        verifier_infrastructure_valid=infra_valid,
        baseline_valid=evaluation.get("baseline_valid"),
        fail_to_pass=evaluation.get("fail_to_pass"),
        pass_to_pass=evaluation.get("pass_to_pass"),
        full_suite=evaluation.get("full_suite"),
        verifier_outcome=evaluation.get("verifier_outcome"),
        cleanup=(
            durable["cleanup_verified"]
            if durable["cleanup_verified"] is not None
            else evaluation.get("cleanup")
        ),
        official_process_exit_code=evaluation.get("official_process_exit_code"),
    )
    admissible = execution_classification not in {
        "infrastructure_invalid",
        "provider_invalid",
        "contaminated",
    }
    resolved = execution_classification == "independent_verifier_resolved"
    science_classification = {
        "infrastructure_invalid": "infrastructure_invalid",
        "provider_invalid": "provider_invalid",
        "contaminated": "contaminated",
        "independent_verifier_resolved": "admissible_resolved",
    }.get(execution_classification, "admissible_unresolved")
    row["science"].update(
        admissible_model_result=admissible,
        infrastructure_invalid=execution_classification == "infrastructure_invalid",
        contaminated=execution_classification == "contaminated",
        provider_invalid=execution_classification == "provider_invalid",
        resolved=resolved,
        unresolved=admissible and not resolved,
        debugger_assisted_resolved=False,
        execution_classification=execution_classification,
        classification=science_classification,
    )
    validate_pilot_result(row)
    return row


def _cmd_authorize(args: argparse.Namespace) -> int:
    output = Path(args.output_dir) if args.output_dir else frozen_dir()
    config_root = Path(args.config_root)
    evidence = authorization_evidence_path(
        config_root,
        Path(args.authorization_evidence) if args.authorization_evidence else None,
    )
    result = _run_authorization_gate(
        frozen=output,
        config_root=config_root,
        profile_id=args.profile_id,
        external_root=Path(args.external_root) if args.external_root else DEFAULT_EXTERNAL_ROOT,
        preflight_summary=Path(args.preflight_summary) if args.preflight_summary else output / "preflight" / "summary.json",
        expected_alias=args.expected_alias,
        expected_upstream=args.expected_upstream,
    )
    write_json(evidence, result)
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 2


def _cmd_configure_profile(args: argparse.Namespace) -> int:
    """Install or deterministically replace only the accepted local profile."""

    config_root = Path(args.config_root).resolve()
    if _path_inside_repository(config_root):
        raise SystemExit("model configuration root must resolve outside the repository")
    store = CommandModelConfigStore(config_root)
    existing = list(store.load())
    adapter = (repository_root() / "scripts" / "ollama_cloud_command_adapter.py").resolve()
    if not adapter.is_file():
        raise SystemExit(f"accepted repository adapter is missing: {adapter}")
    replacement = {
        "profile_id": PROFILE_ID,
        "display_name": PROFILE_DISPLAY_NAME,
        "executable": sys.executable,
        "argv": [str(adapter), "--model", MODEL_ALIAS],
        "cwd": str(repository_root()),
        "request_timeout_seconds": 60,
        "protocol_version": PROTOCOL_VERSION,
    }
    profiles = []
    for profile in existing:
        if profile.profile_id == PROFILE_ID:
            continue
        mapping = profile.to_mapping()
        mapping.pop("schema_version", None)
        profiles.append(mapping)
    profiles.append(replacement)
    write_json(
        store.config_path,
        {"schema_version": "command-models-v1", "profiles": profiles},
    )
    resolved = CommandModelConfigStore(config_root).get(PROFILE_ID)
    command = resolved.live_command()
    print(
        json.dumps(
            {
                "status": "configured",
                "config_path": str(store.config_path),
                "profile_id": resolved.profile_id,
                "display_name": resolved.display_name,
                "adapter": command[1] if len(command) > 1 else None,
                "alias": MODEL_ALIAS,
                "protocol": resolved.protocol_version,
                "provider_inference_started": False,
            },
            indent=2,
        )
    )
    return 0


def _make_disposable_clean_repo(destination: Path) -> Path:
    """Copy only runtime-critical source into a disposable committed repo."""

    repo = destination / "candidate-repo"
    repo.mkdir(parents=True, exist_ok=False)
    source_root = repository_root()
    for relative in HARNESS_PATHS:
        source = source_root / relative
        target = repo / relative
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    frozen_source = frozen_dir()
    shutil.copytree(
        frozen_source,
        repo / "experiments" / EXPERIMENT_ID / "frozen",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    # This is a disposable zero-provider smoke checkout, not the historical
    # V1 identity.  Bind only the copied contract to the copied current
    # harness so the smoke can exercise the authorization path without
    # mutating the tracked historical frozen artifact.
    smoke_contract_path = repo / "experiments" / EXPERIMENT_ID / "frozen" / "execution_contract.json"
    smoke_contract = json.loads(smoke_contract_path.read_text(encoding="utf-8"))
    smoke_contract["harness"]["harness_content_sha256"] = harness_content_sha256(repo)
    write_json(smoke_contract_path, smoke_contract)
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "smoke@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "zero-provider-smoke"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "disposable candidate smoke"], cwd=repo, check=True)
    return repo


def _write_smoke_profile(config_root: Path, adapter_root: Path) -> None:
    write_json(
        config_root / "config" / "command-models.json",
        {
            "schema_version": "command-models-v1",
            "profiles": [
                {
                    "profile_id": PROFILE_ID,
                    "display_name": PROFILE_DISPLAY_NAME,
                    "executable": sys.executable,
                    "argv": [
                        str(adapter_root / "scripts" / "ollama_cloud_command_adapter.py"),
                        "--model",
                        MODEL_ALIAS,
                    ],
                    "request_timeout_seconds": 60,
                    "protocol_version": "1.3",
                }
            ],
        },
    )


def _cmd_smoke(args: argparse.Namespace) -> int:
    """Run the real deterministic top-level path and stop before generation."""

    output = Path(args.output_path) if args.output_path else (
        Path(args.output_dir) if args.output_dir else frozen_dir()
    ) / "zero_provider_top_level_smoke.json"
    with tempfile.TemporaryDirectory(prefix="swr-top-level-smoke-") as temporary:
        temp = Path(temporary)
        clean_repo = _make_disposable_clean_repo(temp)
        clean_frozen = clean_repo / "experiments" / EXPERIMENT_ID / "frozen"
        config_root = temp / "config-root"
        _write_smoke_profile(config_root, clean_repo)
        execution_target = temp / "external-execution-root"
        pilot = json.loads((frozen_dir() / "pilot10_manifest.json").read_text(encoding="utf-8"))
        from agentic_debugger.swerebench.selection import OrderedTask

        tasks = [OrderedTask(**row) for row in pilot["tasks"]]
        bundles = load_official_bundles(item.instance_id for item in tasks)
        frozen_before = {
            path.relative_to(clean_frozen).as_posix(): sha256_file(path)
            for path in clean_frozen.rglob("*")
            if path.is_file()
        }
        summary_path = clean_frozen / "preflight" / "summary.json"

        def fake_metadata_preflight(alias: str, upstream: str) -> dict[str, object]:
            return {
                "schema_version": "ollama-cloud-preflight-v1",
                "expected_model": alias,
                "expected_remote_model": upstream,
                "provider_inference_started": False,
                "cloud_inference_verified": False,
            }

        def synthetic_docker_readiness() -> dict[str, object]:
            # This disposable smoke proves the authorization chain without
            # claiming that the owner's Docker daemon is live in this agent
            # environment.  The real authorize command uses docker info.
            return {
                "executable_available": True,
                "daemon_reachable": True,
                "synthetic": True,
                "reason": None,
            }

        git_clean_before = not working_tree_dirty(clean_repo)
        gate = _run_authorization_gate(
            frozen=clean_frozen,
            config_root=config_root,
            profile_id=PROFILE_ID,
            external_root=execution_target,
            preflight_summary=summary_path,
            repository_path=clean_repo,
            provider_metadata_preflight=fake_metadata_preflight,
            docker_readiness_probe=synthetic_docker_readiness,
        )
        if not gate["ready"]:
            raise SystemExit(
                "top-level zero-provider smoke authorization failed: "
                + "; ".join(gate["reasons"])
            )
        evidence_path = authorization_evidence_path(config_root, project=clean_repo)
        write_json(evidence_path, gate)
        git_clean_after_authorize = not working_tree_dirty(clean_repo)
        root_after_authorize = inspect_external_root_target(
            execution_target, project_root=clean_repo
        )
        second_gate = _run_authorization_gate(
            frozen=clean_frozen,
            config_root=config_root,
            profile_id=PROFILE_ID,
            external_root=execution_target,
            preflight_summary=summary_path,
            repository_path=clean_repo,
            provider_metadata_preflight=fake_metadata_preflight,
            docker_readiness_probe=synthetic_docker_readiness,
        )
        created_root = create_external_execution_root(execution_target, project_root=clean_repo)
        first = tasks[0]
        first_bundle = bundles[first.instance_id]
        first_model = build_model_task(
            first, first_bundle, fixture_path=".", allowed_write_paths=["src"]
        )
        assert_model_facing_isolated(
            first_model.agent_visible_mapping(),
            hidden_needles=hidden_needles_from_private(
                {
                    "patch": first_bundle.gold_patch(),
                    "test_patch": first_bundle.test_patch(),
                    "FAIL_TO_PASS": list(first_bundle.hidden_tests()[0]),
                    "PASS_TO_PASS": list(first_bundle.hidden_tests()[1]),
                }
            ),
        )
        build_docker_execution_context(
            bundle=first_bundle,
            external_root=created_root,
            instance_id=first.instance_id,
            manifest_fingerprint=first.assignment_key,
            authority_revision=first.base_commit,
            project=first.repo,
            bug_id=first.instance_id,
            buggy_revision=first.base_commit,
        )
        OfficialSWERebenchVerifier(
            first_bundle,
            work_root=created_root,
            baseline_valid=True,
        )
        frozen_after = {
            path.relative_to(clean_frozen).as_posix(): sha256_file(path)
            for path in clean_frozen.rglob("*")
            if path.is_file()
        }
        if not git_clean_before or not git_clean_after_authorize:
            raise SystemExit("zero-provider authorization dirtied the disposable repository")
        if root_after_authorize["state"] != "nonexistent_target":
            raise SystemExit("authorization created or found the campaign root")
        if not second_gate["ready"]:
            raise SystemExit(
                "second zero-provider authorization gate failed: "
                + "; ".join(second_gate["reasons"])
            )
        if frozen_before != frozen_after:
            raise SystemExit("authorization/runtime preparation mutated frozen inputs")
        evidence = {
            "schema_version": "gpt-oss-swerebench-v2-zero-provider-top-level-smoke-v1",
            "authorization_ready": True,
            "runtime_head": gate["runtime_head"],
            "parent_baseline": PARENT_BASELINE,
            "runtime_head_differs_from_parent_baseline": gate["runtime_head"] != PARENT_BASELINE,
            "runtime_head_policy": gate["runtime_head_policy"],
            "clean_disposable_repository": git_clean_before,
            "git_clean_before_authorize": git_clean_before,
            "git_clean_after_authorize": git_clean_after_authorize,
            "authorization_evidence_path": str(evidence_path),
            "authorization_evidence_outside_repository": not _path_inside_repository(
                evidence_path, clean_repo
            ),
            "external_root_target_state_before_executor": "nonexistent_target",
            "external_root_target_state_after_authorize": root_after_authorize["state"],
            "external_root_created_by_executor": created_root.is_dir(),
            "external_root_created_exactly_once": created_root == execution_target.resolve(),
            "second_authorization_ready": second_gate["ready"],
            "frozen_inputs_unchanged": frozen_before == frozen_after,
            "first_task_mapping_instantiated": True,
            "docker_runtime_selected": True,
            "official_verifier_selected": True,
            "hidden_gold_data_model_visible": False,
            "reached_provider_inference_boundary": True,
            "provider_inference_started": False,
            "generation_calls": 0,
            "api_chat_calls": 0,
            "stopped_before_provider_generation": True,
            "profile_metadata": gate["profile_metadata"],
            "provider_metadata_preflight": gate["provider_metadata_preflight"],
        }
    write_json(output, evidence)
    print(json.dumps(evidence, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze")
    freeze.set_defaults(func=_cmd_freeze)
    validate = sub.add_parser("validate")
    validate.set_defaults(func=_cmd_validate)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--external-root", default=None)
    preflight.set_defaults(func=_cmd_preflight)
    execute = sub.add_parser("execute")
    execute.add_argument("--provider-authorized", action="store_true")
    execute.add_argument("--config-root", default=None)
    execute.add_argument("--profile-id", default="ollama-cloud-gpt-oss-20b")
    execute.add_argument("--external-root", default=None)
    execute.add_argument("--preflight-summary", default=None)
    execute.add_argument("--expected-alias", default="gpt-oss:20b-cloud")
    execute.add_argument("--expected-upstream", default="gpt-oss:20b")
    execute.set_defaults(func=_cmd_execute)
    authorize = sub.add_parser("authorize")
    authorize.add_argument("--config-root", required=True)
    authorize.add_argument("--profile-id", default="ollama-cloud-gpt-oss-20b")
    authorize.add_argument("--external-root", default=None)
    authorize.add_argument("--preflight-summary", default=None)
    authorize.add_argument("--expected-alias", default="gpt-oss:20b-cloud")
    authorize.add_argument("--expected-upstream", default="gpt-oss:20b")
    authorize.add_argument("--authorization-evidence", default=None)
    authorize.set_defaults(func=_cmd_authorize)
    configure = sub.add_parser("configure-profile")
    configure.add_argument("--config-root", required=True)
    configure.set_defaults(func=_cmd_configure_profile)
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--output-path", default=None)
    smoke.set_defaults(func=_cmd_smoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
