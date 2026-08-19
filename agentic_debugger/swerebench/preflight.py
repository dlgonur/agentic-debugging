"""Non-provider infrastructure preflight for frozen SWE-rebench tasks."""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from agentic_debugger.swerebench.authority import (
    DEFAULT_EXTERNAL_ROOT,
    PARENT_BASELINE,
    repository_root,
)
from agentic_debugger.application.command_config import CommandModelConfigStore
from agentic_debugger.swerebench.official_eval import run_official_infrastructure_gate
from agentic_debugger.swerebench.provenance import harness_identity
from agentic_debugger.swerebench.provenance import (
    current_git_head,
    require_harness_match,
    working_tree_dirty,
)
from agentic_debugger.swerebench.hashing import (
    canonical_json_bytes,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from agentic_debugger.swerebench.isolation import (
    assert_model_facing_isolated,
    hidden_needles_from_private,
)
from agentic_debugger.swerebench.mapping import (
    build_model_task,
    build_verifier_task,
    production_write_paths,
    product_task_id,
)
from agentic_debugger.swerebench.materialize import (
    MaterializationError,
    default_external_root,
    load_repo_cache_index,
    materialize_base_commit,
)
from agentic_debugger.swerebench.pdb_readiness import classify_pdb_readiness
from agentic_debugger.swerebench.execution import (
    build_docker_execution_context,
    inspect_external_root_target,
)
from agentic_debugger.swerebench.records import OfficialInstanceBundle
from agentic_debugger.swerebench.selection import OrderedTask


FROZEN_SELECTION_HASHES = {
    "population.json": "36bd31d1470b86db982235153372793455a850ae1fe9c1669bdf8c0e7e68ab8f",
    "full_ordering.json": "599a07b6a527b4f8dffda4120be8e3c524ad608929bb048ea98286f80e0f5061",
    "pilot10_manifest.json": "4b9b17f8f897e56263f0394e35c06261bc613097f38a1b2e157d4d9a215a963f",
}

PREFLIGHT_BUNDLE_SCHEMA_VERSION = "gpt-oss-swerebench-v2-preflight-bundle-v1"


def _record_filename(instance_id: str) -> str:
    """Return the only accepted bounded filename for a task record."""

    if (
        type(instance_id) is not str
        or not instance_id
        or Path(instance_id).name != instance_id
        or instance_id in {".", ".."}
        or any(character in instance_id for character in "\\/")
    ):
        raise ValueError("preflight instance_id cannot be used as a record filename")
    return f"{instance_id}.json"


def _bundle_fingerprint(summary: Mapping[str, Any]) -> str:
    """Hash summary metadata and the exact record-file hashes it names."""

    unsigned = dict(summary)
    unsigned.pop("evidence_fingerprint", None)
    return sha256_bytes(canonical_json_bytes(unsigned))


def write_preflight_bundle(
    root: Path,
    *,
    summary: Mapping[str, Any],
    records: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Persist bounded per-task records and a hash-bound summary."""

    if len(records) != 10:
        raise ValueError("a preflight evidence bundle must contain exactly ten records")
    record_dir = root / "records"
    record_dir.mkdir(parents=True, exist_ok=True)
    for stale in record_dir.glob("*.json"):
        stale.unlink()
    record_files: list[dict[str, str]] = []
    for record in records:
        instance_id = record.get("instance_id")
        if type(instance_id) is not str:
            raise ValueError("preflight record is missing instance_id")
        filename = _record_filename(instance_id)
        path = record_dir / filename
        write_json(path, record)
        record_files.append(
            {
                "instance_id": instance_id,
                "path": f"records/{filename}",
                "sha256": sha256_file(path),
            }
        )
    bound = dict(summary)
    bound["schema_version"] = PREFLIGHT_BUNDLE_SCHEMA_VERSION
    bound["record_files"] = record_files
    bound["evidence_fingerprint"] = _bundle_fingerprint(bound)
    write_json(root / "summary.json", bound)
    return bound


def load_preflight_bundle(
    summary_path: Path,
    *,
    record_dir: Path,
    expected_instance_ids: list[str] | tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    """Load and verify the exact external readiness evidence bundle."""

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise ValueError("preflight summary must be a JSON object")
    if summary.get("schema_version") != PREFLIGHT_BUNDLE_SCHEMA_VERSION:
        raise ValueError("preflight summary is not a bound evidence bundle")
    file_entries = summary.get("record_files")
    if not isinstance(file_entries, list) or len(file_entries) != 10:
        raise ValueError("preflight evidence bundle must name exactly ten record files")
    expected = list(expected_instance_ids) if expected_instance_ids is not None else None
    records: dict[str, dict[str, Any]] = {}
    normalized_entries: list[dict[str, str]] = []
    bundle_root = record_dir.resolve()
    for index, entry in enumerate(file_entries):
        if not isinstance(entry, Mapping):
            raise ValueError("preflight record binding is not an object")
        instance_id = entry.get("instance_id")
        relative = entry.get("path")
        expected_hash = entry.get("sha256")
        if type(instance_id) is not str or type(relative) is not str:
            raise ValueError("preflight record binding has invalid identity/path")
        if type(expected_hash) is not str or len(expected_hash) != 64:
            raise ValueError("preflight record binding has an invalid SHA-256")
        if expected is not None and instance_id != expected[index]:
            raise ValueError("preflight record order does not match the authorized selection")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("preflight record path escapes the evidence root")
        path = (summary_path.parent / relative_path).resolve()
        try:
            path.relative_to(bundle_root.parent)
        except ValueError as exc:
            raise ValueError("preflight record path is outside the evidence root") from exc
        if path.name != _record_filename(instance_id):
            raise ValueError("preflight record filename does not match instance_id")
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise ValueError(f"preflight record is missing or changed: {instance_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("instance_id") != instance_id:
            raise ValueError(f"preflight record identity mismatch: {instance_id}")
        records[instance_id] = record
        normalized_entries.append(
            {"instance_id": instance_id, "path": relative, "sha256": expected_hash}
        )
    if len(records) != 10:
        raise ValueError("preflight evidence bundle contains duplicate task identities")
    if expected is not None and list(records) != expected:
        raise ValueError("preflight evidence bundle task identities do not match selection")
    actual_fingerprint = _bundle_fingerprint(summary)
    if summary.get("evidence_fingerprint") != actual_fingerprint:
        raise ValueError("preflight evidence fingerprint mismatch")
    return summary, records, actual_fingerprint


def docker_readiness() -> dict[str, Any]:
    """Run the bounded, non-provider Docker daemon readiness check."""
    executable = shutil.which("docker")
    if executable is None:
        return {
            "executable_available": False,
            "daemon_reachable": False,
            "reason": "docker executable is unavailable",
        }
    try:
        completed = subprocess.run(
            [executable, "info"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "executable_available": True,
            "daemon_reachable": False,
            "reason": f"docker info failed: {type(exc).__name__}",
        }
    return {
        "executable_available": True,
        "daemon_reachable": completed.returncode == 0,
        "return_code": completed.returncode,
        "reason": None if completed.returncode == 0 else "docker daemon is unreachable",
    }


def run_zero_provider_authorization_preflight(
    *,
    frozen: Path,
    config_root: Path,
    profile_id: str,
    external_root: Path,
    preflight_summary: Path,
    expected_alias: str = "gpt-oss:20b-cloud",
    expected_upstream: str = "gpt-oss:20b",
    repository_path: Path | None = None,
    provider_metadata_preflight: Any | None = None,
    docker_readiness_probe: Any | None = None,
    selection_hashes: Mapping[str, str] | None = None,
    selection_files: Mapping[str, Path] | None = None,
    preflight_record_dir: Path | None = None,
    expected_preflight_instance_ids: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Check the exact future execution gate without model generation."""

    checks: dict[str, bool] = {}
    reasons: list[str] = []
    project = (repository_path or repository_root()).resolve()
    preflight_bundle_fingerprint: str | None = None
    bound_records: dict[str, dict[str, Any]] = {}
    try:
        expected_selection_hashes = selection_hashes or FROZEN_SELECTION_HASHES
        selected_files = selection_files or {
            name: frozen / name for name in expected_selection_hashes
        }
        for name, expected in expected_selection_hashes.items():
            require_sha256(selected_files[name], expected, label=name)
        checks["frozen_selection_hashes"] = True
    except Exception as exc:
        checks["frozen_selection_hashes"] = False
        reasons.append(str(exc))

    contract: dict[str, Any] = {}
    try:
        contract = json.loads((frozen / "execution_contract.json").read_text(encoding="utf-8"))
        expected_harness = contract["harness"]["harness_content_sha256"]
        require_harness_match(expected_harness, project)
        checks["harness_content"] = True
    except Exception as exc:
        checks["harness_content"] = False
        reasons.append(str(exc))

    head = current_git_head(project)
    recorded_head = head
    checks["runtime_head_recorded"] = bool(head)
    if not checks["runtime_head_recorded"]:
        reasons.append("actual Git HEAD could not be recorded")
    checks["clean_worktree"] = not working_tree_dirty(project)
    if not checks["clean_worktree"]:
        reasons.append("Git working tree is dirty")

    lifecycle = inspect_external_root_target(external_root, project_root=project)
    root = Path(lifecycle["resolved"])
    checks["safe_external_root"] = bool(lifecycle["authorized"])
    if not checks["safe_external_root"]:
        reasons.append(str(lifecycle["reason"] or "external root lifecycle is not authorized"))

    docker_probe = (
        docker_readiness_probe()
        if docker_readiness_probe is not None
        else docker_readiness()
    )
    if not isinstance(docker_probe, dict):
        docker_probe = {
            "executable_available": False,
            "daemon_reachable": False,
            "reason": "docker readiness probe returned an invalid result",
        }
    checks["docker_prerequisite"] = bool(docker_probe.get("executable_available"))
    checks["docker_daemon_reachable"] = bool(docker_probe.get("daemon_reachable"))
    if not checks["docker_prerequisite"] or not checks["docker_daemon_reachable"]:
        reasons.append(str(docker_probe.get("reason") or "docker daemon is unreachable"))

    profile_metadata: dict[str, Any] = {}
    try:
        profile = CommandModelConfigStore(config_root).get(profile_id)
        provider = contract["provider"]
        expected_adapter = (project / provider["adapter"]).resolve(strict=False)
        command = profile.live_command()
        model_index = command.index("--model") if "--model" in command else -1
        configured_alias = command[model_index + 1] if model_index >= 0 and model_index + 1 < len(command) else None
        adapter_script = next(
            (token for token in command if Path(token).name == "ollama_cloud_command_adapter.py"),
            None,
        )
        adapter_path = None
        if adapter_script is not None:
            adapter_path = Path(adapter_script)
            if not adapter_path.is_absolute():
                adapter_path = (Path(profile.cwd) if profile.cwd else project) / adapter_path
            adapter_path = adapter_path.resolve(strict=False)
        adapter_provenance_valid = (
            expected_adapter.is_file()
            and adapter_path is not None
            and adapter_path == expected_adapter
        )
        executable_path = Path(profile.executable)
        executable_ready = (
            executable_path.is_file()
            if executable_path.is_absolute()
            else shutil.which(profile.executable) is not None
        )
        adapter_namespace = runpy.run_path(
            str(project / "scripts" / "ollama_cloud_command_adapter.py")
        )
        resolve_cloud_model = adapter_namespace["resolve_cloud_model"]

        registry_spec = resolve_cloud_model(expected_alias)
        profile_metadata = {
            "profile_id": profile.profile_id,
            "display_name": profile.display_name,
            "configuration_fingerprint": profile.configuration_fingerprint,
            "protocol_version": profile.protocol_version,
            "configured_alias": configured_alias,
            "adapter_script": adapter_script,
            "configured_adapter_resolved": str(adapter_path) if adapter_path else None,
            "frozen_repository_adapter": str(expected_adapter),
            "adapter_provenance_valid": adapter_provenance_valid,
            "adapter_script_exists": bool(adapter_path and adapter_path.is_file()),
            "executable_ready": executable_ready,
            "registry_upstream": registry_spec.upstream_model,
        }
        checks["model_profile_fingerprint"] = bool(
            re.fullmatch(r"[0-9a-f]{64}", profile.configuration_fingerprint)
        )
        if not checks["model_profile_fingerprint"]:
            reasons.append("configured model profile fingerprint is invalid")
        checks["model_profile_metadata"] = (
            profile.profile_id == provider["profile_id"] == profile_id
            and profile.display_name == provider.get("display_name", profile.display_name)
            and profile.protocol_version == provider["protocol"]
            and configured_alias == expected_alias == provider["alias"]
            and adapter_script is not None
            and adapter_provenance_valid
            and executable_ready
            and registry_spec.local_alias == expected_alias
            and registry_spec.upstream_model == expected_upstream == provider["upstream"]
        )
        if not checks["model_profile_metadata"]:
            reasons.append("configured model profile alias/upstream metadata mismatch")
    except Exception as exc:
        checks["model_profile_metadata"] = False
        reasons.append(f"configured model profile preflight failed: {exc}")

    provider_probe: dict[str, Any] = {}
    if checks.get("model_profile_metadata"):
        try:
            probe = provider_metadata_preflight
            provider_probe = (
                probe(expected_alias, expected_upstream)
                if probe is not None
                else adapter_namespace["run_preflight"](model=expected_alias)
            )
            checks["provider_metadata_preflight"] = (
                isinstance(provider_probe, dict)
                and provider_probe.get("provider_inference_started") is False
                and provider_probe.get("cloud_inference_verified") is False
            )
            if not checks["provider_metadata_preflight"]:
                reasons.append("provider metadata preflight did not prove zero generation")
        except Exception as exc:
            checks["provider_metadata_preflight"] = False
            reasons.append(f"non-generative Ollama metadata preflight failed: {type(exc).__name__}: {exc}")
    else:
        checks["provider_metadata_preflight"] = False

    try:
        summary = json.loads(preflight_summary.read_text(encoding="utf-8"))
        if preflight_record_dir is not None:
            if not _repo_escapes_project(preflight_summary, project) or not _repo_escapes_project(
                preflight_record_dir, project
            ):
                raise ValueError("DEVQUAL preflight evidence must be outside the repository")
            summary, bound_records, preflight_bundle_fingerprint = load_preflight_bundle(
                preflight_summary,
                record_dir=preflight_record_dir,
                expected_instance_ids=expected_preflight_instance_ids,
            )
            checks["preflight_evidence_bundle"] = True
            checks["preflight_records_authorized"] = (
                len(bound_records) == 10
                and all(
                    record.get("instance_id") == instance_id
                    and record.get("authorization_status")
                    == "ready-for-authorized-execution"
                    and record.get("verifier_baseline_valid") is True
                    for instance_id, record in bound_records.items()
                )
            )
            if not checks["preflight_records_authorized"]:
                reasons.append("one or more bound preflight records is not authorized")
        else:
            checks["preflight_evidence_bundle"] = True
            checks["preflight_records_authorized"] = True
        checks["all_ten_authorized"] = (
            summary.get("n") == 10
            and summary.get("invalid") == []
            and summary.get("ready") == 10
        )
        records = summary.get("records") or []
        checks["model_hidden_data_boundary"] = (
            len(records) == 10
            and all(
                item.get("authorization_status") == "ready-for-authorized-execution"
                and item.get("model_facing_isolated") is True
                and item.get("model_side_runtime_ready") is True
                and item.get("verifier_environment_ready") is True
                and item.get("verifier_baseline_valid") is True
                and item.get("pdb_classification") == "PDB_DEFERRED_TO_SEPARATE_TREATMENT"
                for item in records
            )
        )
        if not checks["model_hidden_data_boundary"]:
            reasons.append("model-facing isolation or final PDB contract is not proven for all ten rows")
    except Exception as exc:
        checks["all_ten_authorized"] = False
        checks["model_hidden_data_boundary"] = False
        checks["preflight_evidence_bundle"] = False
        checks["preflight_records_authorized"] = False
        reasons.append(f"Pilot-10 authorization summary unavailable: {exc}")

    checks["provider_generation_calls"] = True
    ready = all(checks.values())
    return {
        "schema_version": "gpt-oss-swerebench-v2-zero-provider-preflight-v1",
        "ready": ready,
        "provider_generation_calls": 0,
        "checks": checks,
        "reasons": reasons,
        "runtime_head": head,
        "recorded_runtime_head": recorded_head,
        "runtime_head_policy": "record_at_execution",
        "harness_content_sha256": ((contract.get("harness") or {}).get("harness_content_sha256")),
        "profile_id": profile_id,
        "expected_alias": expected_alias,
        "expected_upstream": expected_upstream,
        "profile_metadata": profile_metadata,
        "provider_metadata_preflight": provider_probe,
        "docker_readiness": docker_probe,
        "external_root_lifecycle": lifecycle,
        "external_root": str(root),
        "preflight_summary": str(preflight_summary.resolve(strict=False)),
        "preflight_record_dir": (
            str(preflight_record_dir.resolve(strict=False))
            if preflight_record_dir is not None
            else None
        ),
        "preflight_evidence_fingerprint": preflight_bundle_fingerprint,
        "preflight_record_instance_ids": list(bound_records),
    }


def _repo_escapes_project(path: Path, project_root: Path | None = None) -> bool:
    project = (project_root or repository_root()).resolve()
    try:
        path.resolve().relative_to(project)
    except ValueError:
        return True
    return False


def run_task_preflight(
    ordered: OrderedTask,
    bundle: OfficialInstanceBundle,
    *,
    external_root: Path | None = None,
    cache_index: dict[str, Path] | None = None,
    keep_checkout: bool = False,
) -> dict[str, Any]:
    """Readiness record. Never writes gold or hidden tests into the result."""

    root = default_external_root() if external_root is None else external_root
    lifecycle = inspect_external_root_target(root)
    record: dict[str, Any] = {
        "instance_id": ordered.instance_id,
        "product_task_id": product_task_id(ordered.instance_id),
        "repo": ordered.repo,
        "repo_canonical": ordered.repo_canonical,
        "base_commit": ordered.base_commit,
        "order_index": ordered.order_index,
        "parent_baseline": PARENT_BASELINE,
        "harness": harness_identity(),
        "external_root": str(root),
        "external_root_lifecycle": lifecycle,
        "external_root_outside_repository": _repo_escapes_project(root),
        "source_materialized": False,
        "clean_disposable_workspace": False,
        "declared_metadata_valid": True,
        "public_problem_statement_sha256": bundle.public.problem_statement_sha256,
        "verifier_f2p_count": bundle.private.fail_to_pass_count
        if hasattr(bundle.private, "fail_to_pass_count")
        else len(bundle.private.fail_to_pass),
        "verifier_p2p_count": len(bundle.private.pass_to_pass),
        "verifier_tests_available": bool(bundle.private.fail_to_pass),
        "verifier_p2p_may_be_empty": len(bundle.private.pass_to_pass) == 0,
        "gold_patch_present_in_official_row": bundle.private.has_gold_patch,
        "test_patch_present_in_official_row": bundle.private.has_test_patch,
        "model_task_constructed": False,
        "verifier_task_constructed": False,
        "model_facing_isolated": False,
        "model_side_runtime_ready": False,
        "verifier_environment_ready": False,
        "verifier_baseline_valid": False,
        "verifier_gold_valid": False,
        "pdb_entry_capability": False,
        "pdb_treatment_contract_valid": False,
        "authorization_status": "not-authorized",
        "pdb": None,
        "official_eval": None,
        "infrastructure_status": "unknown",
        "exclusion_reason": None,
        "notes": [],
    }
    if not record["external_root_outside_repository"] or not lifecycle["authorized"]:
        record["infrastructure_status"] = "infrastructure-invalid"
        record["exclusion_reason"] = lifecycle["reason"] or "external_root_inside_repository"
        return record
    if keep_checkout:
        record["infrastructure_status"] = "infrastructure-invalid"
        record["exclusion_reason"] = (
            "keep_checkout is not available during preflight; the executor owns "
            "campaign-root creation and preservation"
        )
        return record
    if not record["verifier_tests_available"]:
        record["infrastructure_status"] = "infrastructure-invalid"
        record["exclusion_reason"] = "missing_official_f2p_or_p2p"
        return record

    checkout: Path | None = None
    probe_parent = Path(lifecycle["resolved"]).parent
    work_parent = Path(tempfile.mkdtemp(prefix="swr-preflight-", dir=str(probe_parent)))
    try:
        try:
            checkout = materialize_base_commit(
                instance_id=ordered.instance_id,
                repo=ordered.repo,
                repo_canonical=ordered.repo_canonical,
                base_commit=ordered.base_commit,
                dest_parent=work_parent,
                cache_index=cache_index,
            )
            record["source_materialized"] = True
            record["notes"].append(
                f"checked out {ordered.base_commit} via public GitHub single-commit fetch"
            )
        except MaterializationError as exc:
            record["infrastructure_status"] = "infrastructure-invalid"
            record["exclusion_reason"] = f"source_materialization_failed: {exc}"
            return record

        allowed = production_write_paths(checkout)
        record["allowed_write_paths"] = allowed
        # The materialized checkout itself is the disposable application
        # root.  Placeholder fixture directories made the previous readiness
        # record look runnable while the real Local Application could not
        # resolve them.
        rel_model = "."
        rel_verifier = "."
        try:
            model_task = build_model_task(
                ordered, bundle, fixture_path=rel_model, allowed_write_paths=allowed
            )
            verifier_task = build_verifier_task(
                ordered,
                bundle,
                fixture_path=rel_verifier,
                allowed_write_paths=allowed,
            )
            record["model_task_constructed"] = True
            record["verifier_task_constructed"] = True
        except Exception as exc:
            record["infrastructure_status"] = "infrastructure-invalid"
            record["exclusion_reason"] = f"debug_task_mapping_failed: {exc}"
            return record

        needles = hidden_needles_from_private(
            {
                "patch": bundle.gold_patch(),
                "test_patch": bundle.test_patch(),
                "FAIL_TO_PASS": list(bundle.hidden_tests()[0]),
                "PASS_TO_PASS": list(bundle.hidden_tests()[1]),
            }
        )
        try:
            assert_model_facing_isolated(
                model_task.agent_visible_mapping(), hidden_needles=needles
            )
            assert_model_facing_isolated(
                verifier_task.agent_visible_mapping(), hidden_needles=needles
            )
            record["model_facing_isolated"] = True
        except ValueError as exc:
            record["infrastructure_status"] = "infrastructure-invalid"
            record["exclusion_reason"] = f"leakage: {exc}"
            return record

        record["pdb"] = classify_pdb_readiness(
            ordered.instance_id,
            has_official_fail_to_pass=bool(bundle.private.fail_to_pass),
        ).to_mapping()
        try:
            build_docker_execution_context(
                bundle=bundle,
                external_root=work_parent,
                instance_id=ordered.instance_id,
                manifest_fingerprint=ordered.assignment_key,
                authority_revision=ordered.base_commit,
                project=ordered.repo,
                bug_id=ordered.instance_id,
                buggy_revision=ordered.base_commit,
            )
            record["model_side_runtime_ready"] = True
        except Exception as exc:
            record["model_side_runtime_ready"] = False
            record["notes"].append(f"verified model runtime context failed: {exc}")
        record["pdb_entry_capability"] = False
        record["pdb_treatment_contract_valid"] = (
            record["pdb"]["classification"] == "PDB_DEFERRED_TO_SEPARATE_TREATMENT"
        )
        record["notes"].append(
            "model reproduction is not officially declared; the model must "
            "supply a public_target. Hidden tests stay verifier-private"
        )
        record["clean_disposable_workspace"] = True
        record["independent_verifier_mappable"] = True
        record["pytest_rootdir_safe"] = record["external_root_outside_repository"]
        official = run_official_infrastructure_gate(bundle, work_root=work_parent)
        record["official_eval"] = official
        record["verifier_environment_ready"] = bool(
            official.get("verifier_environment_ready")
        )
        record["verifier_baseline_valid"] = bool(official.get("verifier_baseline_valid"))
        record["verifier_gold_valid"] = bool(official.get("verifier_gold_valid"))
        if (
            record["source_materialized"]
            and record["model_facing_isolated"]
            and record["model_side_runtime_ready"]
            and record["verifier_environment_ready"]
            and record["verifier_baseline_valid"]
            and record["verifier_gold_valid"]
            and record["pdb_treatment_contract_valid"]
        ):
            record["infrastructure_status"] = "ready-for-authorized-execution"
            record["authorization_status"] = "ready-for-authorized-execution"
        else:
            record["infrastructure_status"] = "pre-inference-infrastructure-invalid"
            record["authorization_status"] = "not-authorized"
            record["exclusion_reason"] = official.get("reason") or "verifier_gate_incomplete"
        return record
    finally:
        shutil.rmtree(work_parent, ignore_errors=True)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
