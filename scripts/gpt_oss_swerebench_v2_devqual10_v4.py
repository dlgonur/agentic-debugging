"""Fail-closed, zero-provider runner for GPT-OSS SWE-rebench DEVQUAL-10 V4."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentic_debugger.application.command_config import CommandModelConfigStore
from agentic_debugger.swerebench.authority import repository_root
from agentic_debugger.swerebench.devqual_v4 import (
    DEVQUAL_EXPERIMENT_ID, DEVQUAL_EXTERNAL_ROOT, DEVQUAL_FROZEN_DIR,
    DEVQUAL_PREFLIGHT_ROOT, DEVQUAL_SELECTION_HASHES, devqual_manifest_path,
    load_devqual_contract, validate_devqual_identity,
)
from agentic_debugger.swerebench.hashing import sha256_file
from agentic_debugger.swerebench.preflight import (
    load_preflight_bundle, run_task_preflight,
    run_zero_provider_authorization_preflight, write_json, write_preflight_bundle,
)
from agentic_debugger.swerebench.records import load_official_bundles, parquet_identity
from agentic_debugger.swerebench.provenance import current_git_head, harness_content_sha256
from agentic_debugger.swerebench.selection import OrderedTask
from agentic_debugger.swerebench.execution import (
    OfficialSWERebenchVerifier, build_docker_execution_context,
    create_external_execution_root, inspect_external_root_target,
)
from agentic_debugger.swerebench.mapping import build_model_task
from agentic_debugger.swerebench.records import OfficialInstanceBundle, PublicInstanceRecord, VerifierPrivateRecord
from agentic_debugger.swerebench.authority import repository_root

try:
    from scripts.gpt_oss_swerebench_v2_pilot10 import (
        MODEL_ALIAS, PROFILE_ID, UPSTREAM_MODEL, authorization_evidence_path,
        _run_authorized_pilot10,
    )
except ModuleNotFoundError:
    from gpt_oss_swerebench_v2_pilot10 import (  # type: ignore[no-redef]
        MODEL_ALIAS, PROFILE_ID, UPSTREAM_MODEL, authorization_evidence_path,
        _run_authorized_pilot10,
    )


REASONING_EFFORT = "high"
PARENT_EXPERIMENT_ID = "gpt_oss_swerebench_v2_devqual10_v3"


def _load_tasks() -> list[OrderedTask]:
    payload = json.loads(devqual_manifest_path().read_text(encoding="utf-8"))
    return [OrderedTask(**row) for row in payload["tasks"]]


def _selection_files() -> dict[str, Path]:
    historical = repository_root() / "experiments" / "gpt_oss_swerebench_v2_pilot10" / "frozen"
    return {
        "population.json": historical / "population.json",
        "full_ordering.json": historical / "full_ordering.json",
        "pilot10_manifest.json": devqual_manifest_path(),
    }


def _path_arg(args: argparse.Namespace, name: str, label: str) -> Path:
    value = getattr(args, name, None)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"DEVQUAL {label} path is required")
    return Path(value).resolve()


def _preflight_output(args: argparse.Namespace) -> Path:
    return _path_arg(args, "output_summary", "preflight output")


def _authorize_preflight(args: argparse.Namespace) -> Path:
    return _path_arg(args, "preflight_summary", "authorization preflight summary")


def _record_dir(args: argparse.Namespace) -> Path:
    return _authorize_preflight(args).parent / "records"


def _campaign_root(args: argparse.Namespace) -> Path:
    return _path_arg(args, "external_root", "campaign root")


def _inside_repository(path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(repository_root().resolve())
    except ValueError:
        return False
    return True


def _ensure_distinct_roots(args: argparse.Namespace, readiness: Path) -> None:
    campaign = _campaign_root(args)
    if readiness == campaign or campaign.is_relative_to(readiness):
        raise SystemExit("DEVQUAL readiness evidence root must be distinct from and outside the campaign root")


def _selection_hashes() -> dict[str, str]:
    return {**DEVQUAL_SELECTION_HASHES, "pilot10_manifest.json": sha256_file(devqual_manifest_path())}


def _authorization_path(args: argparse.Namespace) -> Path:
    config_root = _path_arg(args, "config_root", "configuration root")
    explicit = getattr(args, "authorization_output", None)
    return authorization_evidence_path(config_root, Path(explicit) if explicit else None, project=repository_root())


def _authorize(args: argparse.Namespace) -> dict:
    summary = _authorize_preflight(args)
    _ensure_distinct_roots(args, summary.parent)
    if _inside_repository(summary) or _inside_repository(_campaign_root(args)):
        raise SystemExit("DEVQUAL readiness and campaign evidence must be outside the repository")
    identity = validate_devqual_identity(project=repository_root())
    tasks = _load_tasks()
    result = run_zero_provider_authorization_preflight(
        frozen=DEVQUAL_FROZEN_DIR,
        config_root=_path_arg(args, "config_root", "configuration root"),
        profile_id=args.profile_id,
        external_root=_campaign_root(args),
        preflight_summary=summary,
        expected_alias=args.expected_alias,
        expected_upstream=args.expected_upstream,
        selection_hashes=_selection_hashes(),
        selection_files=_selection_files(),
        preflight_record_dir=_record_dir(args),
        expected_preflight_instance_ids=[task.instance_id for task in tasks],
    )
    try:
        configured = CommandModelConfigStore(_path_arg(args, "config_root", "configuration root")).get(args.profile_id)
        command = configured.live_command()
        high_reasoning = "--reasoning-effort" in command and command[command.index("--reasoning-effort") + 1] == REASONING_EFFORT
    except Exception:
        high_reasoning = False
    if not high_reasoning:
        result["ready"] = False
        result.setdefault("reasons", []).append("V4 profile must explicitly configure --reasoning-effort high")
    result.update({
        "experiment_id": DEVQUAL_EXPERIMENT_ID,
        "parent_experiment_id": PARENT_EXPERIMENT_ID,
        "qualification_only": True,
        "reasoning_effort": REASONING_EFFORT,
        "provider_generation_calls": 0,
        "devqual_identity": identity,
    })
    return result


def _cmd_validate(_args: argparse.Namespace) -> int:
    identity = validate_devqual_identity(project=repository_root())
    print(json.dumps({"status": "validated", **identity, "reasoning_effort": REASONING_EFFORT, "provider_generation_calls": 0}, indent=2))
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    validate_devqual_identity(project=repository_root())
    output = _preflight_output(args)
    campaign = _campaign_root(args)
    _ensure_distinct_roots(args, output.parent)
    if _inside_repository(output) or _inside_repository(campaign):
        raise SystemExit("DEVQUAL preflight and campaign evidence must be outside the repository")
    tasks = _load_tasks()
    bundles = load_official_bundles(item.instance_id for item in tasks)
    records = [run_task_preflight(task, bundles[task.instance_id], external_root=campaign) for task in tasks]
    summary = {
        "experiment_id": DEVQUAL_EXPERIMENT_ID,
        "parent_experiment_id": PARENT_EXPERIMENT_ID,
        "parquet": parquet_identity(), "n": len(records),
        "ready": sum(item.get("authorization_status") == "ready-for-authorized-execution" for item in records),
        "invalid": [{"instance_id": item["instance_id"], "reason": item.get("exclusion_reason"), "authorization_status": item.get("authorization_status")} for item in records if item.get("authorization_status") != "ready-for-authorized-execution"],
        "records": [{"instance_id": item["instance_id"], "authorization_status": item.get("authorization_status"), "model_facing_isolated": bool(item.get("model_facing_isolated")), "model_side_runtime_ready": bool(item.get("model_side_runtime_ready")), "verifier_environment_ready": bool(item.get("verifier_environment_ready")), "verifier_baseline_valid": bool(item.get("verifier_baseline_valid")), "pdb_classification": (item.get("pdb") or {}).get("classification")} for item in records],
        "external_root": str(campaign), "provider_generation_calls": 0,
    }
    bound = write_preflight_bundle(output.parent, summary=summary, records=records)
    print(json.dumps(bound, indent=2))
    return 0 if not bound["invalid"] else 2


def _cmd_authorize(args: argparse.Namespace) -> int:
    result = _authorize(args)
    write_json(_authorization_path(args), result)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ready") else 2


def _cmd_configure_profile(args: argparse.Namespace) -> int:
    root = _path_arg(args, "config_root", "configuration root")
    if _inside_repository(root):
        raise SystemExit("model configuration root must be outside the repository")
    store = CommandModelConfigStore(root)
    profiles = [profile.to_mapping() for profile in store.load() if profile.profile_id != PROFILE_ID]
    adapter = (repository_root() / "scripts" / "ollama_cloud_command_adapter.py").resolve()
    profiles.append({"profile_id": PROFILE_ID, "display_name": "Ollama Cloud GPT-OSS 20B", "executable": sys.executable, "argv": [str(adapter), "--model", MODEL_ALIAS, "--reasoning-effort", REASONING_EFFORT], "cwd": str(repository_root()), "request_timeout_seconds": 60, "protocol_version": "1.3"})
    write_json(store.config_path, {"schema_version": "command-models-v1", "profiles": profiles})
    print(json.dumps({"status": "configured", "profile_id": PROFILE_ID, "reasoning_effort": REASONING_EFFORT, "provider_inference_started": False}, indent=2))
    return 0


def _cmd_execute(args: argparse.Namespace) -> int:
    if not args.provider_authorized:
        raise SystemExit("provider inference is fail-closed; pass --provider-authorized only from an explicitly authorized task")
    auth = _authorize(args)
    if not auth.get("ready"):
        raise SystemExit("V4 authorization failed")
    fingerprint = (auth.get("profile_metadata") or {}).get("configuration_fingerprint")
    if not isinstance(fingerprint, str):
        raise SystemExit("V4 authorization did not provide a profile fingerprint")
    return _run_authorized_pilot10(args, DEVQUAL_FROZEN_DIR, profile_fingerprint=fingerprint, preflight_record_dir=_record_dir(args), preflight_evidence_fingerprint=auth.get("preflight_evidence_fingerprint"), expected_preflight_instance_ids=[task.instance_id for task in _load_tasks()], run_id_prefix="devqual10-v4", rows_filename="devqual10_v4_rows.json", campaign_metadata={"experiment_id": DEVQUAL_EXPERIMENT_ID, "parent_experiment_id": PARENT_EXPERIMENT_ID, "status": "DEVELOPMENT_QUALIFICATION_ONLY", "reasoning_effort": REASONING_EFFORT, "profile_id": args.profile_id, "profile_alias": MODEL_ALIAS, "profile_fingerprint": fingerprint, "harness_sha256": harness_content_sha256(repository_root()), "runtime_git_head": current_git_head(repository_root()), "provider_generation_calls": 0})


def _cmd_smoke(args: argparse.Namespace) -> int:
    output = _path_arg(args, "output_path", "smoke output") if getattr(args, "output_path", None) else (DEVQUAL_PREFLIGHT_ROOT / "zero_provider_v4_smoke.json").resolve()
    if _inside_repository(output):
        raise SystemExit("V4 smoke evidence must be outside the repository")
    v1 = repository_root() / "experiments" / "gpt_oss_swerebench_v2_pilot10" / "frozen"
    v2 = repository_root() / "experiments" / "gpt_oss_swerebench_v2_devqual10_v2" / "frozen"
    v3 = repository_root() / "experiments" / "gpt_oss_swerebench_v2_devqual10_v3" / "frozen"
    before = {str(path): sha256_file(path) for root in (v1, v2, v3) for path in root.rglob("*") if path.is_file()}
    identity = validate_devqual_identity(project=repository_root())
    after = {str(path): sha256_file(path) for root in (v1, v2, v3) for path in root.rglob("*") if path.is_file()}
    evidence = {"schema_version": "gpt-oss-swerebench-v2-devqual10-v4-zero-provider-smoke-v1", "experiment_id": DEVQUAL_EXPERIMENT_ID, "parent_experiment_id": PARENT_EXPERIMENT_ID, "identity": identity, "first_ten_exact": identity["first_ten_instance_ids"] == [task.instance_id for task in _load_tasks()], "v1_v2_v3_frozen_files_unchanged": before == after, "preflight_authorize_smoke": True, "provider_inference_started": False, "provider_generation_calls": 0, "reasoning_effort": REASONING_EFFORT, "external_pdb": "unavailable_by_treatment_contract"}
    write_json(output, evidence)
    print(json.dumps(evidence, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate"); validate.set_defaults(func=_cmd_validate)
    preflight = sub.add_parser("preflight"); preflight.add_argument("--external-root", default=str(DEVQUAL_EXTERNAL_ROOT)); preflight.add_argument("--output-summary", default=str(DEVQUAL_PREFLIGHT_ROOT / "summary.json")); preflight.set_defaults(func=_cmd_preflight)
    for name, func in (("authorize", _cmd_authorize), ("execute", _cmd_execute)):
        command = sub.add_parser(name); command.add_argument("--config-root", required=True); command.add_argument("--profile-id", default=PROFILE_ID); command.add_argument("--external-root", default=str(DEVQUAL_EXTERNAL_ROOT)); command.add_argument("--preflight-summary", default=str(DEVQUAL_PREFLIGHT_ROOT / "summary.json")); command.add_argument("--authorization-output", default=None); command.add_argument("--expected-alias", default=MODEL_ALIAS); command.add_argument("--expected-upstream", default=UPSTREAM_MODEL)
        if name == "execute": command.add_argument("--provider-authorized", action="store_true"); command.add_argument("--output-dir", default=str(DEVQUAL_FROZEN_DIR / "outputs"))
        command.set_defaults(func=func)
    configure = sub.add_parser("configure-profile"); configure.add_argument("--config-root", required=True); configure.set_defaults(func=_cmd_configure_profile)
    smoke = sub.add_parser("smoke"); smoke.add_argument("--output-path", default=None); smoke.set_defaults(func=_cmd_smoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
