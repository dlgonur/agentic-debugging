"""GPT-OSS SWE-rebench DEVQUAL-10 V9 transport qualification.

V9 is the next treatment after immutable V8. It keeps the first-ten order,
model, prompt, controller, PatchManager, hidden-data boundary, and one
attempt-per-task policy unchanged while making the timeout and stream
evidence contract explicit: metadata has its own bound, generation has a
fresh absolute bound, streaming has an idle/progress bound, and the official
evaluator has a separate outer watchdog above its 300-second task budget.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from agentic_debugger.application.command_config import CommandModelConfigStore
from agentic_debugger.swerebench.authority import repository_root
from agentic_debugger.swerebench.devqual_v9 import (
    DEVQUAL_EXPERIMENT_ID,
    DEVQUAL_EXTERNAL_ROOT,
    DEVQUAL_FROZEN_DIR,
    PARENT_EXPERIMENT_ID,
    load_devqual_contract,
    validate_devqual_identity,
)
from agentic_debugger.swerebench.execution import inspect_external_root_target
from agentic_debugger.swerebench.provenance import current_git_head, harness_content_sha256, working_tree_dirty

try:
    from scripts.gpt_oss_swerebench_v2_pilot10 import (
        MODEL_ALIAS, PROFILE_ID, PROTOCOL_VERSION, UPSTREAM_MODEL,
        _run_authorized_pilot10,
    )
except ModuleNotFoundError:
    from gpt_oss_swerebench_v2_pilot10 import (  # type: ignore[no-redef]
        MODEL_ALIAS, PROFILE_ID, PROTOCOL_VERSION, UPSTREAM_MODEL,
        _run_authorized_pilot10,
    )

try:
    from scripts import ollama_cloud_command_adapter as ollama_adapter
except ImportError:
    import ollama_cloud_command_adapter as ollama_adapter  # type: ignore[no-redef]


REASONING_EFFORT = "high"
METADATA_TIMEOUT_SECONDS = 60
GENERATION_TIMEOUT_SECONDS = 240
STREAM_IDLE_TIMEOUT_SECONDS = 45
OUTER_TIMEOUT_SECONDS = 360


def _inside_repository(path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(repository_root().resolve())
    except ValueError:
        return False
    return True


def _path_arg(args: argparse.Namespace, name: str, label: str) -> Path:
    value = getattr(args, name, None)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"DEVQUAL V9 {label} path is required")
    return Path(value).resolve()


def _validate_profile(args: argparse.Namespace, contract: dict) -> str:
    profile = CommandModelConfigStore(_path_arg(args, "config_root", "configuration root")).get(args.profile_id)
    provider = contract["provider"]
    command = profile.live_command()
    if profile.profile_id != PROFILE_ID or args.profile_id != PROFILE_ID:
        raise SystemExit("V9 configured profile identity does not match the frozen GPT-OSS profile")
    if profile.protocol_version != PROTOCOL_VERSION:
        raise SystemExit("V9 configured protocol does not match protocol 1.3")
    if profile.request_timeout_seconds != OUTER_TIMEOUT_SECONDS:
        raise SystemExit("V9 configured outer request timeout must be 360 seconds")
    model_index = command.index("--model") if "--model" in command else -1
    configured_alias = command[model_index + 1] if model_index >= 0 and model_index + 1 < len(command) else None
    if configured_alias != MODEL_ALIAS or configured_alias != provider["alias"]:
        raise SystemExit("V9 configured model alias does not match the frozen profile")
    if "--reasoning-effort" not in command:
        raise SystemExit("V9 configured profile must explicitly set reasoning_effort=high")
    reasoning_index = command.index("--reasoning-effort")
    if reasoning_index + 1 >= len(command) or command[reasoning_index + 1] != REASONING_EFFORT:
        raise SystemExit("V9 configured profile must explicitly set reasoning_effort=high")
    required_timeout_args = {
        "--metadata-timeout": str(METADATA_TIMEOUT_SECONDS),
        "--generation-timeout": str(GENERATION_TIMEOUT_SECONDS),
        "--stream-idle-timeout": str(STREAM_IDLE_TIMEOUT_SECONDS),
    }
    for flag, expected in required_timeout_args.items():
        if flag not in command or command[command.index(flag) + 1] != expected:
            raise SystemExit(f"V9 configured profile must explicitly set {flag}={expected}")
    if "--stream" not in command:
        raise SystemExit("V9 configured profile must explicitly enable Ollama streaming")
    if provider["upstream"] != UPSTREAM_MODEL or provider["protocol"] != PROTOCOL_VERSION:
        raise SystemExit("V9 frozen provider identity is inconsistent")
    registry = ollama_adapter.resolve_cloud_model(configured_alias)
    if registry.upstream_model != UPSTREAM_MODEL:
        raise SystemExit("V9 configured alias resolves to the wrong upstream model")
    fingerprint = profile.configuration_fingerprint
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise SystemExit("V9 configured profile fingerprint is invalid")
    return fingerprint


def _cheap_guards(args: argparse.Namespace) -> tuple[dict, str]:
    identity = validate_devqual_identity(project=repository_root())
    contract = load_devqual_contract()
    head = current_git_head(repository_root())
    if not head:
        raise SystemExit("V9 current Git HEAD could not be recorded")
    if working_tree_dirty(repository_root()):
        raise SystemExit("V9 requires a clean worktree")
    campaign = _path_arg(args, "external_root", "campaign root")
    if _inside_repository(campaign):
        raise SystemExit("V9 campaign root must be outside the repository")
    lifecycle = inspect_external_root_target(campaign, project_root=repository_root())
    if not lifecycle.get("authorized"):
        raise SystemExit(str(lifecycle.get("reason") or "V9 campaign root is not a fresh safe target"))
    fingerprint = _validate_profile(args, contract)
    envelope = contract["request_envelope"]
    expected_envelope = {
        "canonical_public_request_bytes": ollama_adapter.MAX_PUBLIC_REQUEST_BYTES,
        "stdin_request_bytes": ollama_adapter.MAX_STDIN_REQUEST_BYTES,
        "http_request_body_bytes": ollama_adapter.MAX_HTTP_REQUEST_BODY_BYTES,
        "raw_response_bytes": ollama_adapter.MAX_RAW_RESPONSE_BYTES,
        "stream_response_bytes": ollama_adapter.DEFAULT_STREAM_RESPONSE_BYTES,
        "stream_wire_bytes": ollama_adapter.DEFAULT_STREAM_WIRE_BYTES,
        "stream_line_bytes": ollama_adapter.DEFAULT_STREAM_LINE_BYTES,
        "stream_retained_content_bytes": ollama_adapter.DEFAULT_STREAM_RETAINED_CONTENT_BYTES,
    }
    if envelope != expected_envelope:
        raise SystemExit("V9 request-envelope constants differ from the frozen contract")
    timeout_semantics = contract.get("timeout_semantics")
    if timeout_semantics != {
        "metadata_deadline_seconds": METADATA_TIMEOUT_SECONDS,
        "generation_absolute_deadline_seconds": GENERATION_TIMEOUT_SECONDS,
        "stream_idle_timeout_seconds": STREAM_IDLE_TIMEOUT_SECONDS,
        "outer_process_deadline_seconds": OUTER_TIMEOUT_SECONDS,
        "official_git_timeout_seconds": 90,
        "official_docker_command_timeout_seconds": 300,
        "official_evaluator_watchdog_seconds": 600,
        "official_evaluator_timeout_semantics": "pinned evaluator has no container/test timeout; outer watchdog is hang safety only",
        "source_git_subprocess_deadline_seconds": 90,
        "provider_timeout_retries": False,
        "other_transport_retries_max": 2,
    }:
        raise SystemExit("V9 timeout semantics differ from the frozen contract")
    return {
        "identity": identity,
        "runtime_git_head": head,
        "working_tree_dirty": False,
        "campaign_root_lifecycle": lifecycle,
        "profile_fingerprint": fingerprint,
        "harness_sha256": harness_content_sha256(repository_root()),
        "provider_inference_status": "not_started_by_validation",
        "execution_mode": "direct",
    }, fingerprint


def _cmd_validate(_args: argparse.Namespace) -> int:
    identity = validate_devqual_identity(project=repository_root())
    print(json.dumps({"status": "validated", **identity, "reasoning_effort": REASONING_EFFORT, "provider_inference_status": "not_started_by_validation"}, indent=2))
    return 0


def _cmd_execute(args: argparse.Namespace) -> int:
    if not args.live:
        raise SystemExit("provider inference is fail-closed; pass --live to authorize V9 provider calls")
    guards, fingerprint = _cheap_guards(args)
    campaign = _path_arg(args, "external_root", "campaign root")
    metadata = {
        "experiment_id": DEVQUAL_EXPERIMENT_ID,
        "parent_experiment_id": PARENT_EXPERIMENT_ID,
        "status": "DEVELOPMENT_QUALIFICATION_ONLY",
        "execution_mode": "direct",
        "reasoning_effort": REASONING_EFFORT,
        "metadata_timeout_seconds": METADATA_TIMEOUT_SECONDS,
        "generation_timeout_seconds": GENERATION_TIMEOUT_SECONDS,
        "stream_idle_timeout_seconds": STREAM_IDLE_TIMEOUT_SECONDS,
        "outer_request_timeout_seconds": OUTER_TIMEOUT_SECONDS,
        "official_git_timeout_seconds": 90,
        "official_docker_command_timeout_seconds": 300,
        "official_evaluator_watchdog_seconds": 600,
        "official_evaluator_timeout_semantics": "pinned evaluator has no container/test timeout; outer watchdog is hang safety only",
        "streaming": True,
        "profile_id": args.profile_id,
        "profile_alias": MODEL_ALIAS,
        "profile_upstream": UPSTREAM_MODEL,
        "profile_protocol": PROTOCOL_VERSION,
        "profile_fingerprint": fingerprint,
        "harness_sha256": guards["harness_sha256"],
        "runtime_git_head": guards["runtime_git_head"],
        "provider_inference_started": True,
        "provider_generation_calls": None,
        "provider_generation_calls_source": "durable_session_rows_pending",
        "repair_provider_inference_allowed": True,
        "cheap_guards": guards,
    }
    return _run_authorized_pilot10(
        args, DEVQUAL_FROZEN_DIR, profile_fingerprint=fingerprint,
        run_id_prefix="devqual10-v9", rows_filename="devqual10_v9_rows.json",
        campaign_metadata=metadata, readiness_mode="direct",
    )


def _cmd_configure_profile(args: argparse.Namespace) -> int:
    root = _path_arg(args, "config_root", "configuration root")
    if _inside_repository(root):
        raise SystemExit("model configuration root must resolve outside the repository")
    store = CommandModelConfigStore(root)
    profiles = [profile.to_mapping() for profile in store.load() if profile.profile_id != PROFILE_ID]
    adapter = (repository_root() / "scripts" / "ollama_cloud_command_adapter.py").resolve()
    profiles.append({
        "profile_id": PROFILE_ID,
        "display_name": "Ollama Cloud GPT-OSS 20B",
        "executable": sys.executable,
        "argv": [
            str(adapter), "--model", MODEL_ALIAS, "--reasoning-effort", REASONING_EFFORT,
            "--metadata-timeout", str(METADATA_TIMEOUT_SECONDS),
            "--generation-timeout", str(GENERATION_TIMEOUT_SECONDS),
            "--stream-idle-timeout", str(STREAM_IDLE_TIMEOUT_SECONDS), "--stream",
        ],
        "cwd": str(repository_root()), "request_timeout_seconds": OUTER_TIMEOUT_SECONDS, "protocol_version": PROTOCOL_VERSION,
    })
    store.config_path.parent.mkdir(parents=True, exist_ok=True)
    store.config_path.write_text(json.dumps({"schema_version": "command-models-v1", "profiles": profiles}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "configured", "profile_id": PROFILE_ID, "reasoning_effort": REASONING_EFFORT, "metadata_timeout_seconds": METADATA_TIMEOUT_SECONDS, "generation_timeout_seconds": GENERATION_TIMEOUT_SECONDS, "stream_idle_timeout_seconds": STREAM_IDLE_TIMEOUT_SECONDS, "outer_request_timeout_seconds": OUTER_TIMEOUT_SECONDS, "streaming": True, "provider_inference_started": False}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.set_defaults(func=_cmd_validate)
    execute = sub.add_parser("execute")
    execute.add_argument("--live", action="store_true")
    execute.add_argument("--config-root", required=True)
    execute.add_argument("--profile-id", default=PROFILE_ID)
    execute.add_argument("--external-root", default=str(DEVQUAL_EXTERNAL_ROOT))
    execute.set_defaults(func=_cmd_execute)
    configure = sub.add_parser("configure-profile")
    configure.add_argument("--config-root", required=True)
    configure.set_defaults(func=_cmd_configure_profile)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
