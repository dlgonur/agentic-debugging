"""Focused tests for the AGY Gemini 3.7 Flash command adapter.

Strictly synthetic: ZERO network, ZERO paid inference, ZERO real ``agy --print``.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scripts import agy_executable_identity as identity_module
from scripts import agy_gemini_command_adapter as adapter
from scripts import agy_gemini_synthetic_executable as synthetic
from agentic_debugger.application.command_transport import CancellableJsonlCommandTransport
from agentic_debugger.evaluation.live import LiveModelConfig, LiveTransportError

SYNTHETIC_SCRIPT = REPO_ROOT / "scripts" / "agy_gemini_synthetic_executable.py"


def sample_request(
    state: str = "Reproduce",
    allowed_actions: tuple[str, ...] = ("run_reproduction",),
    legal_transition_targets: tuple[str, ...] = ("Understand", "Failed"),
    directive_schema: tuple[str, ...] = ("action", "transition"),
    task_id: str = "curated-none-handling-001",
    logical_call_index: int = 1,
    protocol: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "protocol": protocol if protocol is not None else {
            "name": "agentic-debugger-live-jsonl",
            "version": "1.3",
            "request_id": "test-run:model-call:1:attempt:1:uuid123",
            "logical_model_call_index": logical_call_index,
            "transport_attempt_index": 1,
        },
        "identity": {
            "evaluation_id": "eval-1",
            "case_id": f"eval-1:{task_id}",
            "run_id": "test-run",
            "trajectory_id": "test-run",
        },
        "task": {
            "task_id": task_id,
            "instruction": "Fix None handling defect in display_name formatting",
        },
        "policy": "pdb-on-uncertainty",
        "directive_schema": list(directive_schema),
        "action_contracts": {
            "run_reproduction": {
                "properties": {"phase": {"type": "string", "enum": ["baseline", "post_patch"]}},
                "required": ["phase"],
                "additional_properties": False,
            },
            "apply_patch": {
                "properties": {"patch": {"type": "string", "min_length": 1}},
                "required": ["patch"],
                "additional_properties": False,
            },
        },
        "controller": {
            "state": state,
            "task_id": task_id,
            "model_call_index": 1,
            "allowed_actions": list(allowed_actions),
            "legal_transition_targets": list(legal_transition_targets),
            "budget_limits": {
                "max_patch_attempts": 3,
                "max_test_runs": 10,
                "max_pdb_observations": 15,
                "max_active_hypotheses": 3,
                "max_source_observations": 10,
            },
            "budget_state": {
                "patch_attempts": 0,
                "test_runs": 0,
                "pdb_observations": 0,
                "source_observations": 0,
            },
            "hypotheses": [],
            "last_observation": None,
        },
        "history": [],
        "directive_feedback": None,
        "instructions": "Return one directive JSON object.",
    }


def _posix_agy(tmp_path: Path) -> str:
    executable = tmp_path / "agy"
    shutil.copy2(SYNTHETIC_SCRIPT, executable)
    executable.write_bytes(executable.read_bytes().replace(b"\r\n", b"\n"))
    executable.chmod(0o755)
    return str(executable)


@pytest.fixture
def fake_agy(tmp_path: Path) -> dict[str, str]:
    if sys.platform == "win32":
        fake_bin = tmp_path / "fake-agy"
        fake_bin.mkdir(parents=True, exist_ok=True)
        native = synthetic.build_fake_agy_executable(fake_bin, target_script=SYNTHETIC_SCRIPT)
        return {"executable": str(native), "bin": str(fake_bin)}
    return {"executable": _posix_agy(tmp_path), "bin": str(tmp_path)}


def adapter_argv(fake_agy: dict[str, str], **extra: str) -> list[str]:
    argv = [
        "--executable", fake_agy["executable"],
        "--model", "gemini-3.7-flash-medium",
        "--expected-version", synthetic.SYNTHETIC_VERSION,
    ]
    for flag, value in extra.items():
        argv.extend([f"--{flag.replace('_', '-')}", value])
    return argv


def run_adapter_real(
    fake_agy: dict[str, str],
    request: Mapping[str, Any],
    *,
    extra_argv: list[str] | None = None,
) -> tuple[int, str, str]:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "agy_gemini_command_adapter.py"),
        *adapter_argv(fake_agy),
        *(extra_argv or []),
    ]
    completed = subprocess.run(
        command,
        input=(json.dumps(request) + "\n").encode("utf-8"),
        capture_output=True,
        timeout=90,
        check=False,
    )
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
    )


def _transport(fake_agy: dict[str, str], tmp_path: Path, *, timeout: str = "10") -> CancellableJsonlCommandTransport:
    work_root = tmp_path / "work-root"
    work_root.mkdir(exist_ok=True)
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "agy_gemini_command_adapter.py"),
        *adapter_argv(fake_agy, timeout=timeout, work_root=str(work_root)),
    ]
    config = LiveModelConfig(
        model_name="AGY Gemini 3.7 Flash Medium",
        command=command,
        request_timeout_seconds=float(timeout) + 5.0,
    )
    return CancellableJsonlCommandTransport(config, max_output_bytes=65536)


# --- Prompt / ceiling -------------------------------------------------------

def test_build_protocol_message_valid() -> None:
    msg = adapter.build_protocol_message(sample_request())
    assert adapter.PUBLIC_REQUEST_START in msg
    assert adapter.PUBLIC_REQUEST_END in msg
    assert "debugging decision model" in msg
    assert "Do not inspect the filesystem" in msg
    assert "Do not use tools" in msg
    assert str(REPO_ROOT) not in msg


def test_local_application_ceiling_and_command_line_bounds() -> None:
    assert adapter.MAX_PUBLIC_REQUEST_BYTES == 25_000
    assert adapter.MAX_NATIVE_COMMAND_LINE_CHARS == 30_000
    assert adapter.MAX_RAW_RESPONSE_BYTES == 64 * 1024
    assert adapter.DEFAULT_MAX_LOGICAL_MODEL_CALLS == 25
    assert adapter.DEFAULT_TIMEOUT_SECONDS == 20.0
    assert adapter.FIRST_RUN_MODEL_ID == "gemini-3.7-flash-medium"
    assert adapter.ALLOWED_MODEL_IDENTIFIERS == {
        "gemini-3.7-flash-low",
        "gemini-3.7-flash-medium",
        "gemini-3.7-flash-high",
    }
    assert adapter.ALLOWED_INTRINSIC_INIT_CAPABILITIES == {"ask_permission"}


def test_ceiling_plus_one_fails_closed() -> None:
    req = dict(sample_request())
    req["_pad"] = ""
    target = adapter.MAX_PUBLIC_REQUEST_BYTES + 1
    current = len(adapter.canonical_public_request(req).encode("utf-8"))
    req["_pad"] = "x" * (target - current)
    assert len(adapter.canonical_public_request(req).encode("utf-8")) == target
    with pytest.raises(ValueError, match="exceeds the Local Application ceiling"):
        adapter.build_protocol_message(req)


def test_prompt_contains_no_repository_path() -> None:
    msg = adapter.build_protocol_message(sample_request())
    assert "agentic-debugging-internship" not in msg
    assert "C:\\Users" not in msg
    assert "/Users/" not in msg


# --- Model allowlist / no fallback ------------------------------------------

def test_model_allowlist_is_exactly_gemini_37_flash_family() -> None:
    assert adapter.DEFAULT_MODEL_ID == "gemini-3.7-flash-medium"
    assert "gemini-3.6-flash-medium" not in adapter.ALLOWED_MODEL_IDENTIFIERS
    assert "gemini-3.1-pro-high" not in adapter.ALLOWED_MODEL_IDENTIFIERS


@pytest.mark.parametrize("invalid_model", [
    "gemini-3.6-flash-medium",
    "gemini-3.7-flash",
    "gemini-3.7-pro-high",
    "claude-sonnet-4-6",
    "deepseek-v4-pro",
])
def test_run_adapter_rejects_unauthorized_models(invalid_model: str) -> None:
    rc = adapter.run_adapter(
        stdin_stream=io.StringIO(json.dumps(sample_request()) + "\n"),
        stdout_stream=io.StringIO(),
        stderr_stream=io.StringIO(),
        argv=["--executable", "placeholder", "--model", invalid_model],
    )
    assert rc == 1


def test_assert_model_available_no_fallback() -> None:
    available = {"gemini-3.7-flash-low", "gemini-3.7-flash-high"}
    with pytest.raises(RuntimeError, match="unavailable"):
        adapter.assert_model_available(available, "gemini-3.7-flash-medium")


def test_parse_models_output_concatenated_real_format() -> None:
    text = (
        "Fetching available models...\n"
        "gemini-3.7-flash-highGemini 3.7 Flash (High)\n"
        "gemini-3.7-flash-mediumGemini 3.7 Flash (Medium)\n"
        "gemini-3.7-flash-lowGemini 3.7 Flash (Low)\n"
        "gemini-3.6-flash-mediumGemini 3.6 Flash (Medium)\n"
    )
    found = adapter.parse_models_output(text)
    assert found == adapter.ALLOWED_MODEL_IDENTIFIERS
    assert "gemini-3.6-flash-medium" not in found


# --- Executable identity ----------------------------------------------------

def test_bare_and_relative_executable_rejected() -> None:
    with pytest.raises(RuntimeError, match="must be absolute"):
        identity_module.resolve_verified_agy_executable("agy")
    with pytest.raises(RuntimeError, match="must be absolute"):
        identity_module.resolve_verified_agy_executable("bin/agy.exe")


def test_unexpected_executable_name_rejected(tmp_path: Path) -> None:
    unexpected = tmp_path / ("python.exe" if sys.platform == "win32" else "python")
    unexpected.write_bytes(b"not-agy")
    with pytest.raises(RuntimeError, match="unexpected executable"):
        identity_module.resolve_verified_agy_executable(str(unexpected))


def test_nonexistent_absolute_executable_rejected(tmp_path: Path) -> None:
    missing = tmp_path / ("agy.exe" if sys.platform == "win32" else "agy")
    with pytest.raises(RuntimeError, match="not a regular file"):
        identity_module.resolve_verified_agy_executable(str(missing))


def test_trusted_absolute_agy_identity(fake_agy: dict[str, str]) -> None:
    identity = identity_module.resolve_verified_agy_executable(
        fake_agy["executable"],
        expected_version=synthetic.SYNTHETIC_VERSION,
    )
    assert identity["native_version"] == "1.1.13"
    assert identity["version_matches_expected"] is True
    assert identity["regular_file"] is True
    assert Path(identity["native_executable"]).is_absolute()


def test_version_mismatch_rejected(fake_agy: dict[str, str]) -> None:
    with pytest.raises(RuntimeError, match="version mismatch"):
        identity_module.resolve_verified_agy_executable(
            fake_agy["executable"],
            expected_version="9.9.9",
        )


def test_run_adapter_rejects_bare_executable(fake_agy: dict[str, str]) -> None:
    rc, stdout, stderr = run_adapter_real(
        fake_agy, sample_request(), extra_argv=["--executable", "agy"]
    )
    assert rc == 1
    assert "must be absolute" in stderr
    assert stdout == ""


# --- Isolation / command shape ----------------------------------------------

def test_isolation_settings_are_strict_and_secret_free() -> None:
    settings = adapter.isolation_settings()
    assert settings["toolPermission"] == "strict"
    assert settings["enableTerminalSandbox"] is True
    assert settings["allowNonWorkspaceAccess"] is False
    deny = settings["permissions"]["deny"]
    for rule in (
        "command(*)", "write_file(*)", "read_file(*)",
        "read_url(*)", "unsandboxed(*)", "mcp(*)",
    ):
        assert rule in deny
    blob = json.dumps(settings)
    assert "oauth" not in blob.lower()
    assert "token" not in blob.lower()
    assert "api_key" not in blob.lower()


def test_prepare_isolation_does_not_copy_credentials_or_repo(tmp_path: Path) -> None:
    root = tmp_path / "iso"
    root.mkdir()
    isolation = adapter.prepare_isolation(root)
    assert isolation["workspace"] != REPO_ROOT
    assert REPO_ROOT not in isolation["workspace"].parents
    assert adapter.isolation_contains_secrets(root) == []
    settings_text = isolation["settings_path"].read_text(encoding="utf-8")
    assert "oauth_creds" not in settings_text
    env = isolation["environment"]
    assert env["HOME"] == str(isolation["home"])
    assert env["USERPROFILE"] == str(isolation["home"])
    assert not any("TOKEN" in key.upper() or "SECRET" in key.upper() for key in env)
    assert REPO_ROOT not in isolation["agent_path"].parents


def test_temporary_decision_agent_is_capability_free(tmp_path: Path) -> None:
    isolation = adapter.prepare_isolation(tmp_path / "iso")
    agent_path = isolation["agent_path"]
    assert agent_path == isolation["home"] / adapter.DECISION_AGENT_RELATIVE
    assert agent_path.is_file()
    text = agent_path.read_text(encoding="utf-8")
    assert text == adapter.DECISION_AGENT_MARKDOWN
    assert "name: local-application-decision" in text
    assert "tools: []" in text
    assert "subagent: false" in text
    assert "mainAgent: true" in text
    assert "commandExecutionPolicy: off" in text
    assert "mcpServers: []" in text
    assert "skills: []" in text
    assert "plugins: []" in text
    repo_agents = REPO_ROOT / ".gemini" / "config" / "agents" / adapter.DECISION_AGENT_NAME
    assert not repo_agents.exists()


def test_mcp_isolation_uses_current_agy_paths(tmp_path: Path) -> None:
    isolation = adapter.prepare_isolation(tmp_path / "iso")
    expected = {"mcpServers": {}}
    cli_mcp = isolation["home"] / adapter.MCP_CLI_RELATIVE
    workspace_mcp = isolation["workspace"] / adapter.MCP_WORKSPACE_RELATIVE
    assert json.loads(cli_mcp.read_text(encoding="utf-8")) == expected
    assert json.loads(workspace_mcp.read_text(encoding="utf-8")) == expected
    assert isolation["mcp_cli_path"] == cli_mcp
    assert isolation["mcp_workspace_path"] == workspace_mcp


def test_build_agy_command_has_required_safety_flags_and_no_forbidden() -> None:
    command = adapter.build_agy_command(
        r"C:\Users\benya\AppData\Local\agy\bin\agy.exe",
        "gemini-3.7-flash-medium",
        "prompt",
        r"C:\Temp\agy-gemini-run-abc\workspace\directive-schema.json",
        20.0,
    )
    adapter.assert_command_is_fresh_print(command)
    assert "--mode" in command and command[command.index("--mode") + 1] == "plan"
    assert "--sandbox" in command
    assert "--disable-slash-commands" in command
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--json-schema" in command
    assert command[command.index("--agent") + 1] == adapter.DECISION_AGENT_NAME
    assert command[command.index("--print-timeout") + 1] == "20s"
    assert "--continue" not in command
    assert "--conversation" not in command
    assert "--dangerously-skip-permissions" not in command
    assert "--add-dir" not in command
    assert "accept-edits" not in command


def test_build_agy_command_rejects_non_allowlisted_model() -> None:
    with pytest.raises(ValueError, match="unsupported model"):
        adapter.build_agy_command(
            r"C:\agy\agy.exe",
            "gemini-3.6-flash-medium",
            "prompt",
            r"C:\schema.json",
            20.0,
        )


# --- Directive validation remains authoritative -----------------------------

def test_accept_structured_directive_valid_action() -> None:
    req = sample_request()
    directive = adapter.accept_structured_directive(
        {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
        req,
    )
    assert directive["name"] == "run_reproduction"


def test_accept_structured_directive_valid_transition() -> None:
    req = sample_request()
    directive = adapter.accept_structured_directive(
        {"kind": "transition", "target_state": "Understand", "reason": "reproduced"},
        req,
    )
    assert directive["kind"] == "transition"


def test_accept_structured_directive_hypothesis() -> None:
    req = sample_request(
        state="Understand",
        allowed_actions=("add_hypothesis",),
        directive_schema=("action", "transition", "add_hypothesis"),
    )
    directive = adapter.accept_structured_directive(
        {
            "kind": "add_hypothesis",
            "hypothesis_id": "h-1",
            "statement": "None first argument is mishandled",
            "confidence": "medium",
            "evidence_refs": [],
            "requires_runtime_evidence": False,
        },
        req,
    )
    assert directive["kind"] == "add_hypothesis"


def test_accept_does_not_repair_wrong_field_names() -> None:
    req = sample_request()
    with pytest.raises(ValueError, match="no valid protocol directive"):
        adapter.accept_structured_directive(
            {"action": "run_reproduction", "params": {"phase": "baseline"}},
            req,
        )


def test_accept_rejects_illegal_action_and_transition() -> None:
    req = sample_request()
    with pytest.raises(ValueError, match="not allowed"):
        adapter.accept_structured_directive(
            {"kind": "action", "name": "apply_patch", "arguments": {"patch": "--- a\n+++ b\n"}},
            req,
        )
    with pytest.raises(ValueError, match="not reachable"):
        adapter.accept_structured_directive(
            {"kind": "transition", "target_state": "Done", "reason": "jump"},
            req,
        )


def test_json_schema_is_generated_from_request_contract() -> None:
    schema = adapter.build_directive_json_schema(sample_request())
    encoded = json.dumps(schema)
    assert "run_reproduction" in encoded
    assert "Understand" in encoded
    assert "oneOf" in schema


# --- Stream parsing / tool fail-closed --------------------------------------

def _stream(*events: dict[str, Any]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def test_parse_stream_accepts_reasoning_then_structured_result() -> None:
    directive = {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
    structured, usage = adapter.parse_agy_stream(_stream(
        {"event": "init", "init": {"cwd": "/tmp", "tools": ["ask_permission"], "agent": adapter.DECISION_AGENT_NAME}},
        {"event": "step_update", "step_update": {"step_type": "user_input", "state": "DONE"}},
        {"event": "step_update", "step_update": {"step_type": "reasoning", "state": "DONE"}},
        {"event": "result", "result": {
            "status": "SUCCESS",
            "structured_output": directive,
            "usage": {"input_tokens": 9, "output_tokens": 3},
        }},
    ))
    assert structured == directive
    assert usage == {"prompt_tokens": 9, "completion_tokens": 3}


@pytest.mark.parametrize("tools", [[], ["ask_permission"]])
def test_parse_stream_accepts_missing_or_audited_init_capabilities(tools: list[str]) -> None:
    directive = {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
    structured, _usage = adapter.parse_agy_stream(_stream(
        {"event": "init", "init": {"tools": tools}},
        {"event": "result", "result": {"status": "SUCCESS", "structured_output": directive}},
    ))
    assert structured == directive

    missing_tools, _usage = adapter.parse_agy_stream(_stream(
        {"event": "init", "init": {}},
        {"event": "result", "result": {"status": "SUCCESS", "structured_output": directive}},
    ))
    assert missing_tools == directive


@pytest.mark.parametrize("tools", [
    ["run_command"],
    ["view_file"],
    ["read_url"],
    ["invoke_subagent"],
    ["unknown_future_tool"],
    ["ask_permission", "run_command"],
])
def test_parse_stream_rejects_unapproved_init_capabilities(tools: list[str]) -> None:
    directive = {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
    with pytest.raises(ValueError, match="unapproved capability"):
        adapter.parse_agy_stream(_stream(
            {"event": "init", "init": {"tools": tools}},
            {"event": "result", "result": {"status": "SUCCESS", "structured_output": directive}},
        ))


def test_parse_stream_rejects_malformed_init_tools() -> None:
    with pytest.raises(ValueError, match="tools field is not a list"):
        adapter.parse_agy_stream(_stream(
            {"event": "init", "init": {"tools": "ask_permission"}},
            {"event": "result", "result": {"status": "SUCCESS", "structured_output": {}}},
        ))
    with pytest.raises(ValueError, match="tools entries are not strings"):
        adapter.parse_agy_stream(_stream(
            {"event": "init", "init": {"tools": ["ask_permission", 1]}},
            {"event": "result", "result": {"status": "SUCCESS", "structured_output": {}}},
        ))


def test_parse_stream_rejects_actual_ask_permission_event_even_with_valid_result() -> None:
    directive = {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
    with pytest.raises(ValueError, match="tool"):
        adapter.parse_agy_stream(_stream(
            {"event": "init", "init": {"tools": ["ask_permission"]}},
            {"event": "step_update", "step_update": {
                "step_type": "agent_response",
                "tool_info": {"name": "ask_permission"},
            }},
            {"event": "result", "result": {"status": "SUCCESS", "structured_output": directive}},
        ))


def test_parse_stream_rejects_tool_event_even_with_valid_result() -> None:
    directive = {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
    with pytest.raises(ValueError, match="tool"):
        adapter.parse_agy_stream(_stream(
            {"event": "init", "init": {}},
            {"event": "step_update", "step_update": {
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {"name": "run_command"},
            }},
            {"event": "result", "result": {"status": "SUCCESS", "structured_output": directive}},
        ))


def test_parse_stream_rejects_subagent_event() -> None:
    directive = {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
    with pytest.raises(ValueError, match="subagent"):
        adapter.parse_agy_stream(_stream(
            {"event": "step_update", "step_update": {
                "step_type": "agent_response",
                "subagent_info": {"subagents": [{"type_name": "explore"}]},
            }},
            {"event": "result", "result": {"status": "SUCCESS", "structured_output": directive}},
        ))


def test_parse_stream_rejects_malformed_missing_duplicate_and_unknown() -> None:
    directive = {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
    with pytest.raises(ValueError, match="not valid NDJSON"):
        adapter.parse_agy_stream("{not-json\n")
    with pytest.raises(ValueError, match="missing a terminal result"):
        adapter.parse_agy_stream(_stream({"event": "init", "init": {}}))
    with pytest.raises(ValueError, match="duplicate terminal result"):
        adapter.parse_agy_stream(_stream(
            {"event": "result", "result": {"status": "SUCCESS", "structured_output": directive}},
            {"event": "result", "result": {"status": "SUCCESS", "structured_output": directive}},
        ))
    with pytest.raises(ValueError, match="unknown unsafe event"):
        adapter.parse_agy_stream(_stream({"event": "mcp_call", "mcp_call": {}}))


def test_parse_stream_does_not_fabricate_usage() -> None:
    directive = {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
    structured, usage = adapter.parse_agy_stream(_stream(
        {"event": "result", "result": {"status": "SUCCESS", "structured_output": directive}},
    ))
    assert structured == directive
    assert usage is None


# --- Logical call guard -----------------------------------------------------

def test_first_and_last_logical_call_indexes_allowed() -> None:
    assert adapter.validate_logical_call_index(sample_request(logical_call_index=1), 25) is None
    assert adapter.validate_logical_call_index(sample_request(logical_call_index=25), 25) is None


def test_one_beyond_logical_call_index_rejected() -> None:
    failure = adapter.validate_logical_call_index(sample_request(logical_call_index=26), 25)
    assert failure is not None
    assert "exceeds the micro-run envelope" in failure


def test_run_adapter_rejects_index_beyond_envelope(fake_agy: dict[str, str]) -> None:
    rc, stdout, stderr = run_adapter_real(fake_agy, sample_request(logical_call_index=26))
    assert rc == 1
    assert "exceeds the micro-run envelope" in stderr
    assert stdout == ""


# --- End-to-end synthetic scenarios -----------------------------------------

def test_valid_structured_result_end_to_end(fake_agy: dict[str, str], tmp_path: Path) -> None:
    transport = _transport(fake_agy, tmp_path)
    req = sample_request()
    req["synthetic_scenario"] = "legal-action"
    response = transport.request(req, timeout_seconds=15.0)
    assert response["directive"] == {
        "kind": "action",
        "name": "run_reproduction",
        "arguments": {"phase": "baseline"},
    }
    assert response["usage"] == {"prompt_tokens": 11, "completion_tokens": 5}


def test_legal_transition_end_to_end(fake_agy: dict[str, str], tmp_path: Path) -> None:
    transport = _transport(fake_agy, tmp_path)
    req = sample_request()
    req["synthetic_scenario"] = "legal-transition"
    response = transport.request(req, timeout_seconds=15.0)
    assert response["directive"]["kind"] == "transition"
    assert response["directive"]["target_state"] == "Understand"


def test_hypothesis_directive_end_to_end(fake_agy: dict[str, str], tmp_path: Path) -> None:
    transport = _transport(fake_agy, tmp_path)
    req = sample_request(
        state="Understand",
        allowed_actions=("add_hypothesis",),
        legal_transition_targets=("RuntimeEvidence", "Failed"),
        directive_schema=("action", "transition", "add_hypothesis"),
    )
    req["action_contracts"]["add_hypothesis"] = {
        "properties": {
            "hypothesis_id": {"type": "string"},
            "statement": {"type": "string"},
            "confidence": {"type": "string"},
            "evidence_refs": {"type": "array"},
            "requires_runtime_evidence": {"type": "boolean"},
        },
        "required": ["hypothesis_id", "statement", "confidence", "evidence_refs", "requires_runtime_evidence"],
        "additional_properties": False,
    }
    req["synthetic_scenario"] = "hypothesis-directive"
    response = transport.request(req, timeout_seconds=15.0)
    assert response["directive"]["kind"] == "add_hypothesis"


@pytest.mark.parametrize("scenario,needle", [
    ("tool-event", "tool"),
    ("ask-permission-event", "tool"),
    ("subagent-event", "subagent"),
    ("init-run-command", "unapproved capability"),
    ("init-file-tool", "unapproved capability"),
    ("init-web-tool", "unapproved capability"),
    ("init-unknown-tool", "unapproved capability"),
    ("init-ask-permission-plus-run-command", "unapproved capability"),
    ("malformed-ndjson", "NDJSON"),
    ("missing-result", "missing a terminal result"),
    ("duplicate-result", "duplicate"),
    ("wrong-directive-schema", "no valid protocol directive"),
    ("illegal-action", "not allowed"),
    ("illegal-transition", "not reachable"),
    ("unknown-event", "unknown unsafe event"),
])
def test_synthetic_failure_scenarios_fail_closed(
    fake_agy: dict[str, str], tmp_path: Path, scenario: str, needle: str
) -> None:
    transport = _transport(fake_agy, tmp_path)
    req = sample_request()
    req["synthetic_scenario"] = scenario
    with pytest.raises(LiveTransportError):
        transport.request(req, timeout_seconds=15.0)


@pytest.mark.parametrize("scenario", ["init-empty-tools", "init-ask-permission-only", "legal-action"])
def test_synthetic_init_capability_acceptance_end_to_end(
    fake_agy: dict[str, str], tmp_path: Path, scenario: str
) -> None:
    transport = _transport(fake_agy, tmp_path)
    req = sample_request()
    req["synthetic_scenario"] = scenario
    response = transport.request(req, timeout_seconds=15.0)
    assert response["directive"]["kind"] == "action"


def test_tool_event_rejected_end_to_end(fake_agy: dict[str, str], tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    rc, stdout, stderr = run_adapter_real(
        fake_agy,
        {**sample_request(), "synthetic_scenario": "tool-event"},
        extra_argv=["--work-root", str(work)],
    )
    assert rc == 1
    assert "tool" in stderr.lower()
    assert stdout == ""


def test_nonzero_exit_and_timeout(fake_agy: dict[str, str], tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    rc, stdout, stderr = run_adapter_real(
        fake_agy,
        {**sample_request(), "synthetic_scenario": "nonzero-exit"},
        extra_argv=["--work-root", str(work)],
    )
    assert rc == 1
    assert stdout == ""
    assert "exited" in stderr.lower() or "failed" in stderr.lower()

    transport = _transport(fake_agy, tmp_path, timeout="0.5")
    req = sample_request()
    req["synthetic_scenario"] = "timeout"
    with pytest.raises(LiveTransportError) as exc_info:
        transport.request(req, timeout_seconds=2.0)
    assert exc_info.value.kind in ("process_error", "request_timeout")


def test_oversized_stream_rejected(fake_agy: dict[str, str], tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    rc, stdout, stderr = run_adapter_real(
        fake_agy,
        {**sample_request(), "synthetic_scenario": "oversized"},
        extra_argv=["--work-root", str(work), "--max-response-bytes", "4096"],
    )
    assert rc == 1
    assert "exceeded" in stderr.lower()
    assert stdout == ""


def test_credentials_absent_from_argv_output_and_config(
    fake_agy: dict[str, str], tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    rc, stdout, stderr = run_adapter_real(
        fake_agy,
        {**sample_request(), "synthetic_scenario": "credential-output"},
        extra_argv=["--work-root", str(work)],
    )
    assert rc == 0
    assert "super-secret-synthetic-value" not in stdout
    assert "super-secret-synthetic-value" not in stderr
    assert "<redacted_secret>" in stderr or "super-secret" not in stderr


def test_fresh_print_invocation_flags_and_isolated_cwd(
    fake_agy: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []
    real_popen = subprocess.Popen

    def wrapped_popen(command, *args, **kwargs):
        captured.append({
            "command": list(command),
            "cwd": kwargs.get("cwd"),
            "env": kwargs.get("env") or {},
        })
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(adapter.subprocess, "Popen", wrapped_popen)
    work = tmp_path / "work"
    work.mkdir()
    req = sample_request()
    req["synthetic_scenario"] = "legal-action"
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = adapter.run_adapter(
        stdin_stream=io.StringIO(json.dumps(req) + "\n"),
        stdout_stream=stdout,
        stderr_stream=stderr,
        argv=adapter_argv(fake_agy, timeout="10", work_root=str(work)),
    )
    assert rc == 0, stderr.getvalue()
    print_invocations = [item for item in captured if "--print" in item["command"]]
    assert len(print_invocations) == 1
    command = print_invocations[0]["command"]
    adapter.assert_command_is_fresh_print(command)
    assert command[command.index("--agent") + 1] == adapter.DECISION_AGENT_NAME
    cwd = Path(print_invocations[0]["cwd"])
    assert cwd.is_absolute()
    assert REPO_ROOT not in cwd.parents
    assert cwd != REPO_ROOT
    prompt = command[command.index("--print") + 1]
    assert str(REPO_ROOT) not in prompt
    env = print_invocations[0]["env"]
    assert "GOOGLE_API_KEY" not in env
    assert "GEMINI_API_KEY" not in env
    home = Path(env["USERPROFILE"])
    assert home != Path.home()
    assert not (home / ".gemini" / "oauth_creds.json").exists()


def test_isolated_agents_preflight_discovers_decision_agent(
    fake_agy: dict[str, str], tmp_path: Path
) -> None:
    isolation = adapter.prepare_isolation(tmp_path / "iso")
    names = adapter.list_available_agents(
        fake_agy["executable"],
        isolation["environment"],
        isolation["workspace"],
    )
    adapter.assert_decision_agent_available(names)
    assert names == {adapter.DECISION_AGENT_NAME}


def test_missing_decision_agent_fails_closed_no_default_fallback() -> None:
    with pytest.raises(RuntimeError, match="no default-agent fallback"):
        adapter.assert_decision_agent_available(set())
    with pytest.raises(RuntimeError, match="no default-agent fallback"):
        adapter.assert_decision_agent_available({"default", "explore"})


def test_missing_decision_agent_file_makes_agents_preflight_fail(
    fake_agy: dict[str, str], tmp_path: Path
) -> None:
    isolation = adapter.prepare_isolation(tmp_path / "iso")
    isolation["agent_path"].unlink()
    names = adapter.list_available_agents(
        fake_agy["executable"],
        isolation["environment"],
        isolation["workspace"],
    )
    assert adapter.DECISION_AGENT_NAME not in names
    with pytest.raises(RuntimeError, match="no default-agent fallback"):
        adapter.assert_decision_agent_available(names)


def test_adapter_spawns_exactly_one_print_and_does_not_respawn_on_failure(
    fake_agy: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[list[str]] = []
    real_popen = subprocess.Popen

    def wrapped_popen(command, *args, **kwargs):
        captured.append(list(command))
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(adapter.subprocess, "Popen", wrapped_popen)
    work = tmp_path / "work"
    work.mkdir()
    req = sample_request()
    req["synthetic_scenario"] = "malformed-ndjson"
    rc = adapter.run_adapter(
        stdin_stream=io.StringIO(json.dumps(req) + "\n"),
        stdout_stream=io.StringIO(),
        stderr_stream=io.StringIO(),
        argv=adapter_argv(fake_agy, timeout="10", work_root=str(work)),
    )
    assert rc == 1
    print_invocations = [cmd for cmd in captured if "--print" in cmd]
    assert len(print_invocations) == 1
    assert print_invocations[0][print_invocations[0].index("--model") + 1] == "gemini-3.7-flash-medium"


def test_preflight_is_zero_inference(fake_agy: dict[str, str], tmp_path: Path) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "agy_gemini_command_adapter.py"),
        "--preflight",
        *adapter_argv(fake_agy),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["preflight"] == "passed"
    assert payload["provider_inference_started"] is False
    assert payload["requested_model"] == "gemini-3.7-flash-medium"
    assert "gemini-3.7-flash-medium" in payload["available_models"]
    assert payload["decision_agent"] == adapter.DECISION_AGENT_NAME
    assert adapter.DECISION_AGENT_NAME in payload["available_agents"]


def test_cancellable_command_transport_with_adapter_fixture(
    fake_agy: dict[str, str], tmp_path: Path
) -> None:
    transport = _transport(fake_agy, tmp_path)
    req = sample_request()
    req["synthetic_scenario"] = "state-legal"
    response = transport.request(req, timeout_seconds=15.0)
    assert response["directive"]["kind"] == "action"
    assert response["directive"]["name"] == "run_reproduction"


def test_child_group_registry_round_trip() -> None:
    if sys.platform == "win32":
        pytest.skip("child-group registry is a POSIX-only mechanism")
    adapter.register_inflight_child_group(4242)
    assert 4242 in adapter._snapshot_inflight_child_groups()
    adapter.unregister_inflight_child_group(4242)
    assert 4242 not in adapter._snapshot_inflight_child_groups()


def test_inflight_registry_cleanup_is_reentrant() -> None:
    import threading
    import time as time_module

    started = threading.Event()
    finished = threading.Event()

    def hold() -> None:
        with adapter._INFLIGHT_CHILD_GROUPS_LOCK:
            started.set()
            time_module.sleep(0.05)
        finished.set()

    worker = threading.Thread(target=hold, daemon=True)
    worker.start()
    assert started.wait(2.0)
    adapter._terminate_inflight_child_groups()
    worker.join(timeout=2.0)
    assert finished.is_set()


def test_redact_secrets() -> None:
    text = adapter.redact("Bearer sk-live-abcdef1234567890 occurred with api_key=secret_12345")
    assert "sk-live" not in text
    assert "secret_12345" not in text
    assert "<redacted_secret>" in text
