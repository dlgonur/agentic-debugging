"""Unit and synthetic integration tests for OpenCode Go DeepSeek V4 Pro command adapter.

Strictly synthetic: ZERO network, ZERO paid inference, ZERO external provider calls.
Uses local test doubles, deterministic synthetic subprocess fixtures, and the
verified fake-launcher/native-executable fixture to validate all adapter gates,
including: the Local-Application request ceiling, the 25-logical-call micro-run
envelope, the fail-closed wildcard permission isolation, the memory-only
credential boundary, the explicit executable identity contract, and the
external-cancellation containment helpers.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scripts import opencode_go_command_adapter as adapter
from scripts import opencode_go_synthetic_executable as synthetic
from agentic_debugger.application.command_transport import CancellableJsonlCommandTransport
from agentic_debugger.evaluation.live import LiveModelConfig

SYNTHETIC_SCRIPT = REPO_ROOT / "scripts" / "opencode_go_synthetic_executable.py"


# --- Fixtures ---------------------------------------------------------------

def sample_request(
    state: str = "Reproduce",
    allowed_actions: tuple[str, ...] = ("run_reproduction",),
    legal_transition_targets: tuple[str, ...] = ("Understand", "Failed"),
    directive_schema: tuple[str, ...] = ("action", "transition"),
    task_id: str = "curated-none-handling-001",
    logical_call_index: int = 1,
    protocol: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
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
                "properties": {
                    "phase": {
                        "type": "string",
                        "enum": ["baseline", "post_patch"],
                    }
                },
                "required": ["phase"],
                "additional_properties": False,
            },
            "apply_patch": {
                "properties": {
                    "patch": {
                        "type": "string",
                        "min_length": 1,
                    }
                },
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
    return request


@pytest.fixture
def auth_file(tmp_path: Path) -> dict[str, Any]:
    """A fixture auth store carrying a unique secret marker."""
    marker = f"auth-marker-{os.getpid()}-{time.time_ns()}"
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps({"opencode-go": {"type": "api", "key": marker}}),
        encoding="utf-8",
    )
    return {"path": path, "marker": marker}


@pytest.fixture
def fake_launcher(tmp_path: Path) -> dict[str, str]:
    """A trusted fake npm-package layout: opencode.cmd launcher + fake native
    opencode.exe (compiled forwarder into the synthetic CLI)."""
    fake_bin = tmp_path / "fake-launcher"
    fake_bin.mkdir(parents=True, exist_ok=True)
    launcher = fake_bin / "opencode.cmd"
    launcher.write_text(
        "@echo off\r\n" + f'"{sys.executable}" "{SYNTHETIC_SCRIPT}" %*\r\n',
        encoding="utf-8",
    )
    native_bin = fake_bin / "node_modules" / "opencode-ai" / "bin"
    native_bin.mkdir(parents=True, exist_ok=True)
    synthetic.build_fake_native_executable(native_bin, target_script=SYNTHETIC_SCRIPT)
    return {"launcher": str(launcher), "native": str(native_bin / "opencode.exe"), "bin": str(fake_bin)}


def adapter_argv(fake_launcher: dict[str, str], auth_file: dict[str, Any], **extra: str) -> list[str]:
    argv = [
        "--executable", fake_launcher["launcher"],
        "--auth-file", str(auth_file["path"]),
        "--model", "deepseek-v4-pro",
    ]
    for flag, value in extra.items():
        argv.extend([f"--{flag.replace('_', '-')}", value])
    return argv


def run_adapter_real(
    fake_launcher: dict[str, str],
    auth_file: dict[str, Any],
    request: Mapping[str, Any],
    *,
    extra_argv: list[str] | None = None,
) -> tuple[int, str, str]:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "opencode_go_command_adapter.py"),
        *adapter_argv(fake_launcher, auth_file),
        *(extra_argv or []),
    ]
    completed = subprocess.run(
        command,
        input=(json.dumps(request) + "\n").encode("utf-8"),
        capture_output=True,
        timeout=90,
        check=False,
    )
    return completed.returncode, completed.stdout.decode("utf-8", "replace"), completed.stderr.decode("utf-8", "replace")


# --- 1. Request Acceptance & Prompt Construction (Blocker A) ---------------

def test_build_protocol_message_valid() -> None:
    req = sample_request()
    msg = adapter.build_protocol_message(req)
    assert adapter.PUBLIC_REQUEST_START in msg
    assert adapter.PUBLIC_REQUEST_END in msg
    assert "You are the debugging decision model" in msg
    assert "curated-none-handling-001" in msg


def test_local_application_ceiling_is_distinct_and_admits_measured_trajectory() -> None:
    # The historical QuixBugs campaign budget (20,000) must not be silently
    # inherited; the Local Application ceiling must exceed the measured
    # curated-none-handling-001 reference trajectory maximum (23,824).
    assert adapter.MAX_PUBLIC_REQUEST_BYTES == 25_000
    assert adapter.MAX_PUBLIC_REQUEST_BYTES > 23_824
    assert adapter.MAX_PUBLIC_REQUEST_BYTES < adapter.MAX_NATIVE_COMMAND_LINE_CHARS


def test_build_protocol_message_exceeds_ceiling() -> None:
    req = sample_request()
    req["bloat"] = "x" * 25_000
    with pytest.raises(ValueError, match="canonical public request exceeds the Local Application ceiling"):
        adapter.build_protocol_message(req)


def test_ceiling_plus_one_fails_closed() -> None:
    base = sample_request()
    target = adapter.MAX_PUBLIC_REQUEST_BYTES + 1
    # Pad until exactly ceiling + 1 canonical bytes (the padding key itself
    # contributes fixed JSON overhead, so the pad length is resolved
    # iteratively): the request must fail closed, proving the configured
    # limit itself is the boundary.
    req = dict(base)
    req["_pad"] = ""
    current = len(adapter.canonical_public_request(req).encode("utf-8"))
    req["_pad"] = "x" * (target - current)
    assert len(adapter.canonical_public_request(req).encode("utf-8")) == target
    with pytest.raises(ValueError, match="exceeds the Local Application ceiling"):
        adapter.build_protocol_message(req)
    # And the exact ceiling itself is admitted.
    req_exact = dict(base)
    req_exact["_pad"] = ""
    current_exact = len(adapter.canonical_public_request(req_exact).encode("utf-8"))
    req_exact["_pad"] = "x" * (adapter.MAX_PUBLIC_REQUEST_BYTES - current_exact)
    assert len(adapter.canonical_public_request(req_exact).encode("utf-8")) == adapter.MAX_PUBLIC_REQUEST_BYTES
    adapter.build_protocol_message(req_exact)


def test_run_adapter_empty_stdin() -> None:
    stdin = io.StringIO("")
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = adapter.run_adapter(stdin_stream=stdin, stdout_stream=stdout, stderr_stream=stderr, argv=["--executable", "placeholder"])
    assert rc == 1
    assert "empty request" in stderr.getvalue()
    assert stdout.getvalue() == ""


def test_run_adapter_invalid_json_stdin() -> None:
    stdin = io.StringIO("{not-valid-json")
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = adapter.run_adapter(stdin_stream=stdin, stdout_stream=stdout, stderr_stream=stderr, argv=["--executable", "placeholder"])
    assert rc == 1
    assert "failed to parse JSON request" in stderr.getvalue()
    assert stdout.getvalue() == ""


# --- 2. Model Identity Enforcement -----------------------------------------

def test_model_identity_allowed() -> None:
    assert "deepseek-v4-pro" in adapter.ALLOWED_MODEL_IDENTIFIERS
    assert "opencode-go/deepseek-v4-pro" in adapter.ALLOWED_MODEL_IDENTIFIERS


@pytest.mark.parametrize("invalid_model", [
    "gpt-4",
    "claude-3-5-sonnet",
    "opencode/deepseek-v4-flash-free",
    "opencode-go/deepseek-v4-flash",
    "deepseek-v3",
])
def test_run_adapter_rejects_unauthorized_models(invalid_model: str) -> None:
    req = sample_request()
    stdin = io.StringIO(json.dumps(req) + "\n")
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = adapter.run_adapter(
        stdin_stream=stdin,
        stdout_stream=stdout,
        stderr_stream=stderr,
        argv=["--executable", "placeholder", "--model", invalid_model],
    )
    assert rc == 1
    assert "unsupported model" in stderr.getvalue()
    assert stdout.getvalue() == ""


# --- 3. Strict Directive Extraction ----------------------------------------

def test_extract_directive_valid_action() -> None:
    req = sample_request()
    raw = json.dumps({"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}})
    res = adapter.extract_directive(raw, req)
    assert res == {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}


def test_extract_directive_valid_transition() -> None:
    req = sample_request()
    raw = json.dumps({"kind": "transition", "target_state": "Understand", "reason": "reproduced baseline defect"})
    res = adapter.extract_directive(raw, req)
    assert res["kind"] == "transition"
    assert res["target_state"] == "Understand"


def test_extract_directive_code_fenced() -> None:
    req = sample_request()
    raw = "```json\n" + json.dumps({"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}) + "\n```"
    res = adapter.extract_directive(raw, req)
    assert res["kind"] == "action"


def test_extract_directive_prose_only() -> None:
    req = sample_request()
    raw = "I reviewed the code and I recommend running the reproduction step now."
    with pytest.raises(ValueError, match="did not contain any JSON object"):
        adapter.extract_directive(raw, req)


def test_extract_directive_malformed_json() -> None:
    req = sample_request()
    raw = '{"kind": "action", "name": "run_reproduction", "arguments":'
    with pytest.raises(ValueError, match="did not contain any JSON object"):
        adapter.extract_directive(raw, req)


def test_extract_directive_zero_valid_candidates() -> None:
    req = sample_request()
    raw = json.dumps({"unknown_field": "some_value"})
    with pytest.raises(ValueError, match="no valid protocol directive found"):
        adapter.extract_directive(raw, req)


def test_extract_directive_two_valid_candidates() -> None:
    req = sample_request()
    d1 = {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
    d2 = {"kind": "transition", "target_state": "Understand", "reason": "failure verified"}
    raw = json.dumps(d1) + "\n" + json.dumps(d2)
    with pytest.raises(ValueError, match="multiple valid protocol directives"):
        adapter.extract_directive(raw, req)


def test_extract_directive_wrong_fields() -> None:
    req = sample_request()
    raw = json.dumps({
        "kind": "action",
        "name": "run_reproduction",
        "arguments": {"phase": "baseline"},
        "extra_field": "not_allowed",
    })
    with pytest.raises(ValueError, match="unknown top-level field"):
        adapter.extract_directive(raw, req)


def test_extract_directive_illegal_action() -> None:
    req = sample_request(state="Reproduce", allowed_actions=("run_reproduction",))
    raw = json.dumps({
        "kind": "action",
        "name": "apply_patch",
        "arguments": {"patch": "--- a\n+++ b\n"},
    })
    with pytest.raises(ValueError, match="action 'apply_patch' is not allowed"):
        adapter.extract_directive(raw, req)


def test_extract_directive_illegal_transition() -> None:
    req = sample_request(state="Reproduce", legal_transition_targets=("Understand", "Failed"))
    raw = json.dumps({
        "kind": "transition",
        "target_state": "Done",
        "reason": "jumping to done",
    })
    with pytest.raises(ValueError, match="'Done' is not reachable"):
        adapter.extract_directive(raw, req)


# --- 4. Redaction & Credential Safety --------------------------------------

def test_redact_secrets() -> None:
    sample_text = "Bearer sk-live-abcdef1234567890 occurred with api_key=secret_12345"
    redacted = adapter.redact(sample_text)
    assert "sk-live" not in redacted
    assert "secret_12345" not in redacted
    assert "<redacted_secret>" in redacted


# --- 5. Micro-run Envelope (Blocker F) -------------------------------------

def test_first_allowed_logical_call_index() -> None:
    assert adapter.validate_logical_call_index(sample_request(logical_call_index=1), 25) is None


def test_last_allowed_logical_call_index() -> None:
    assert adapter.validate_logical_call_index(sample_request(logical_call_index=25), 25) is None


def test_one_beyond_allowed_logical_call_index_rejected() -> None:
    failure = adapter.validate_logical_call_index(sample_request(logical_call_index=26), 25)
    assert failure is not None
    assert "exceeds the micro-run envelope" in failure


@pytest.mark.parametrize("index", [0, -1, 100])
def test_out_of_envelope_indexes_rejected(index: int) -> None:
    assert adapter.validate_logical_call_index(sample_request(logical_call_index=index), 25) is not None


@pytest.mark.parametrize("protocol", [
    None,
    {},
    {"logical_model_call_index": None},
    {"logical_model_call_index": "3"},
    {"logical_model_call_index": 1.5},
    {"logical_model_call_index": True},
])
def test_malformed_or_missing_logical_call_metadata_rejected(protocol: Any) -> None:
    req = sample_request()
    req["protocol"] = protocol
    assert adapter.validate_logical_call_index(req, 25) is not None


def test_run_adapter_rejects_index_beyond_envelope_end_to_end(fake_launcher, auth_file) -> None:
    req = sample_request(logical_call_index=26)
    rc, stdout, stderr = run_adapter_real(fake_launcher, auth_file, req)
    assert rc == 1
    assert "exceeds the micro-run envelope" in stderr
    assert stdout == ""


def test_run_adapter_rejects_missing_protocol_end_to_end(fake_launcher, auth_file) -> None:
    req = sample_request()
    del req["protocol"]
    rc, stdout, stderr = run_adapter_real(fake_launcher, auth_file, req)
    assert rc == 1
    assert "protocol metadata" in stderr
    assert stdout == ""


# --- 6. Executable Identity (Blocker E) ------------------------------------

def test_bare_executable_name_rejected(fake_launcher, auth_file, tmp_path) -> None:
    # A bare PATH lookup must never run during a real run: resolution fails
    # closed before any process is spawned.
    req = sample_request()
    rc, stdout, stderr = run_adapter_real(fake_launcher, auth_file, req, extra_argv=["--executable", "opencode"])
    assert rc == 1
    assert "executable identity" in stderr
    assert stdout == ""


def test_relative_executable_path_rejected(fake_launcher, auth_file, tmp_path) -> None:
    req = sample_request()
    rc, stdout, stderr = run_adapter_real(fake_launcher, auth_file, req, extra_argv=["--executable", "fake-launcher/opencode.cmd"])
    assert rc == 1
    assert "must be absolute" in stderr
    assert stdout == ""


def test_wrong_nonexistent_executable_path_rejected(fake_launcher, auth_file, tmp_path) -> None:
    req = sample_request()
    rc, stdout, stderr = run_adapter_real(
        fake_launcher, auth_file, req,
        extra_argv=["--executable", str(tmp_path / "no-such" / "opencode.cmd")],
    )
    assert rc == 1
    assert "not a regular file" in stderr or "identity" in stderr
    assert stdout == ""


def test_wrong_executable_file_name_rejected(fake_launcher, auth_file, tmp_path) -> None:
    # An absolute path that is not the opencode.cmd launcher is rejected on
    # Windows: only the trusted launcher begins the npm-package resolution.
    req = sample_request()
    not_launcher = tmp_path / "opencode.bat"
    not_launcher.write_text("@echo off\r\n", encoding="utf-8")
    rc, stdout, stderr = run_adapter_real(
        fake_launcher, auth_file, req,
        extra_argv=["--executable", str(not_launcher)],
    )
    assert rc == 1
    assert "must be an absolute opencode.cmd path" in stderr
    assert stdout == ""


def test_trusted_absolute_launcher_resolution_accepted(fake_launcher, auth_file, tmp_path) -> None:
    identity = adapter.executable_identity.resolve_verified_opencode_executable(fake_launcher["launcher"])
    assert identity["resolution_strategy"] == "npm-package-layout"
    assert identity["native_executable"] == fake_launcher["native"]
    assert identity["package_relative_path"].replace("\\", "/") == "bin/opencode.exe"
    assert identity["version_matches_launcher"] is True
    assert identity["regular_file"] is True
    assert identity["root_containment"] is True


def test_native_launcher_version_drift_rejected(fake_launcher, tmp_path, monkeypatch) -> None:
    import opencode_executable_identity as identity_module

    def drifted_version(executable: str, environment: Mapping[str, str]) -> str:
        if Path(executable).name.lower() == "opencode.cmd":
            return "1.0.0"
        return "9.9.9"

    monkeypatch.setattr(identity_module, "_run_version", drifted_version)
    with pytest.raises(RuntimeError, match="version drift"):
        identity_module.resolve_verified_opencode_executable(fake_launcher["launcher"])


def test_native_path_escape_rejected(fake_launcher, tmp_path) -> None:
    import opencode_executable_identity as identity_module

    if sys.platform == "win32":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "opencode.exe").write_bytes(b"escaped-binary")
        package_bin = Path(fake_launcher["bin"]) / "node_modules" / "opencode-ai" / "bin"
        escaped = package_bin / "opencode.exe"
        os.unlink(escaped)
        os.rmdir(package_bin)
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(package_bin), str(outside)],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        with pytest.raises(RuntimeError, match="escapes the trusted npm package root"):
            identity_module.resolve_verified_opencode_executable(fake_launcher["launcher"])
    else:
        pytest.skip("junction escape fixture is Windows-only; POSIX uses the verified-absolute contract")


def test_posix_absolute_executable_contract(tmp_path) -> None:
    import opencode_executable_identity as identity_module

    if sys.platform == "win32":
        pytest.skip("POSIX executable contract is exercised on non-Windows platforms")
    executable = tmp_path / "opencode"
    shutil.copy2(SYNTHETIC_SCRIPT, executable)
    # LF-normalize: the repo checkout may carry CRLF endings, which would
    # break the shebang (/usr/bin/env: 'python3\r').
    executable.write_bytes(executable.read_bytes().replace(b"\r\n", b"\n"))
    executable.chmod(0o755)
    identity = identity_module.resolve_verified_opencode_executable(str(executable))
    assert identity["resolution_strategy"] == "verified-absolute-executable"
    assert identity["native_version"] == "1.0.0"


def test_posix_bare_and_relative_rejected(tmp_path) -> None:
    import opencode_executable_identity as identity_module

    if sys.platform == "win32":
        pytest.skip("POSIX executable contract is exercised on non-Windows platforms")
    with pytest.raises(RuntimeError, match="must be absolute"):
        identity_module.resolve_verified_opencode_executable("opencode")
    with pytest.raises(RuntimeError, match="must be absolute"):
        identity_module.resolve_verified_opencode_executable("bin/opencode")


# --- 7. Fail-Closed Permission Isolation & Effective Config (Blocker B) ----

def test_isolation_config_has_authoritative_wildcard_deny() -> None:
    config = adapter.isolation_config()
    permission = config["permission"]
    assert permission.get("*") == "deny"
    # The wildcard is the FIRST key: it is authoritative and visible.
    assert list(permission)[0] == "*"
    # Every currently known permission name is also explicitly denied.
    known = {
        "read", "write", "edit", "bash", "glob", "grep", "list", "terminal",
        "browser", "task", "webfetch", "websearch", "skill", "lsp",
        "question", "external_directory",
    }
    for name in known:
        assert permission.get(name) == "deny", name
    assert config["mcp"] == {"*": {"enabled": False}}
    assert config["plugin"] == []
    assert config["instructions"] == []
    assert config["share"] == "disabled"
    assert config["enabled_providers"] == ["opencode-go"]
    assert config["autoupdate"] is False


def test_known_permission_keys_cannot_escape_wildcard() -> None:
    # Known permission names are denied twice: by the authoritative wildcard
    # catch-all AND by their explicit named entries.  An effective config in
    # which a known name escapes the named denials fails closed as drift.
    config = adapter.isolation_config()
    for name in ("read", "write", "edit", "bash", "webfetch", "websearch", "external_directory"):
        drifted = json.loads(json.dumps(config))
        del drifted["permission"][name]
        with pytest.raises(RuntimeError, match="required permission"):
            adapter.validate_effective_config(drifted)
    # A config whose permission object dropped the wildcard entirely fails.
    drifted = json.loads(json.dumps(config))
    del drifted["permission"]["*"]
    with pytest.raises(RuntimeError, match="wildcard catch-all"):
        adapter.validate_effective_config(drifted)


def test_unknown_future_permission_names_denied_by_catch_all() -> None:
    # A future/unknown permission name is not configured at all; the wildcard
    # catch-all denies it.  The isolation config never needs a new named entry
    # to remain closed.
    config = adapter.isolation_config()
    assert config["permission"].get("future_unknown_tool") is None
    adapter.validate_effective_config(json.loads(json.dumps(config)))
    # Any drift that weakens the wildcard fails closed.
    drifted = json.loads(json.dumps(config))
    drifted["permission"]["*"] = "ask"
    with pytest.raises(RuntimeError, match="wildcard catch-all"):
        adapter.validate_effective_config(drifted)
    drifted = json.loads(json.dumps(config))
    drifted["permission"]["*"] = "allow"
    with pytest.raises(RuntimeError, match="wildcard catch-all"):
        adapter.validate_effective_config(drifted)


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c["permission"].update({"bash": "allow"}), "required permission"),
    (lambda c: c["mcp"].update({"server-x": {"enabled": True}}), "MCP server"),
    (lambda c: c.update({"plugin": ["some-plugin"]}), "plugin"),
    (lambda c: c.update({"instructions": ["do something"]}), "instructions"),
    (lambda c: c.update({"share": "enabled"}), "sharing"),
    (lambda c: c.update({"enabled_providers": ["opencode-go", "opencode"]}), "enabled provider"),
    (lambda c: c.update({"enabled_providers": ["opencode"]}), "enabled provider"),
    (lambda c: c.update({"autoupdate": True}), "autoupdate"),
])
def test_effective_config_drift_fails_closed(mutate, message) -> None:
    config = adapter.isolation_config()
    drifted = json.loads(json.dumps(config))
    mutate(drifted)
    with pytest.raises(RuntimeError, match=message):
        adapter.validate_effective_config(drifted)


def test_observe_effective_config_via_synthetic_cli(fake_launcher, tmp_path) -> None:
    root = tmp_path / "isolation"
    isolation = adapter.prepare_isolation(root)
    evidence = adapter.observe_effective_config(
        isolation["environment"], root, fake_launcher["native"]
    )
    assert evidence["permission_wildcard_denied"] is True
    assert evidence["mcp_servers_disabled"] is True
    assert evidence["plugins_empty"] is True
    assert evidence["instructions_empty"] is True
    assert evidence["sharing_disabled"] is True
    assert evidence["enabled_providers"] == ["opencode-go"]
    assert evidence["autoupdate_disabled"] is True
    shutil.rmtree(root, ignore_errors=True)


def test_preflight_mode_passes_with_zero_inference(fake_launcher, auth_file, tmp_path) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "opencode_go_command_adapter.py"),
        "--preflight",
        "--executable", fake_launcher["launcher"],
    ]
    completed = subprocess.run(command, capture_output=True, timeout=90, check=False)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    payload = json.loads(completed.stdout.decode("utf-8", "replace"))
    assert payload["preflight"] == "passed"
    assert payload["provider_inference_started"] is False
    assert payload["identity"]["resolution_strategy"] == "npm-package-layout"
    assert payload["effective_config"]["permission_wildcard_denied"] is True


def test_preflight_mode_blocks_on_identity_failure(tmp_path) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "opencode_go_command_adapter.py"),
        "--preflight",
        "--executable", "opencode",
    ]
    completed = subprocess.run(command, capture_output=True, timeout=90, check=False)
    assert completed.returncode == 1
    payload = json.loads(completed.stdout.decode("utf-8", "replace"))
    assert payload["preflight"] == "blocked"
    assert payload["provider_inference_started"] is False


def test_preflight_mode_blocks_on_config_drift(fake_launcher, tmp_path, monkeypatch) -> None:
    def drifted_observe(environment, cwd, native_executable):
        raise RuntimeError("OpenCode effective configuration does not deny permissions by default (wildcard catch-all missing)")

    monkeypatch.setattr(adapter, "observe_effective_config", drifted_observe)
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = adapter.run_adapter(
        stdin_stream=io.StringIO(""),
        stdout_stream=stdout,
        stderr_stream=stderr,
        argv=["--preflight", "--executable", fake_launcher["launcher"]],
    )
    assert rc == 1
    payload = json.loads(stdout.getvalue())
    assert payload["preflight"] == "blocked"
    assert "wildcard catch-all" in payload["error"]


# --- 8. Memory-Only Credential Boundary (Blocker C) ------------------------

_AUTH_SECRET_MARKER = "SECRET_MARKER_auth-store-leak-test-9f3c2a1b"


def _auth_object_bytes(size: int) -> bytes:
    """Return a valid JSON-object encoding of exactly ``size`` UTF-8 bytes."""
    prefix = b'{"k":"'
    suffix = b'"}'
    pad = size - len(prefix) - len(suffix)
    if pad < 0:
        raise AssertionError(f"requested auth-object size {size} is too small")
    return prefix + (b"x" * pad) + suffix


def test_resolve_auth_store_from_fixture(auth_file: dict[str, Any]) -> None:
    content = adapter.resolve_auth_store(str(auth_file["path"]))
    assert auth_file["marker"] in content
    parsed = json.loads(content)
    assert parsed["opencode-go"]["type"] == "api"


def test_resolve_auth_store_missing_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="authentication state is unavailable") as excinfo:
        adapter.resolve_auth_store(str(tmp_path / "missing.json"))
    assert _AUTH_SECRET_MARKER not in str(excinfo.value)


def test_resolve_auth_store_relative_path_rejected(tmp_path: Path, auth_file: Path) -> None:
    with pytest.raises(RuntimeError, match="must be absolute"):
        adapter.resolve_auth_store("auth.json")


def test_resolve_auth_store_directory_or_read_failure_is_safe(tmp_path: Path) -> None:
    directory = tmp_path / "auth-dir"
    directory.mkdir()
    with pytest.raises(RuntimeError, match="authentication state is unavailable") as excinfo:
        adapter.resolve_auth_store(str(directory))
    assert _AUTH_SECRET_MARKER not in str(excinfo.value)


def test_resolve_auth_store_malformed_utf8_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_bytes(b'{"k":"\xff' + _AUTH_SECRET_MARKER.encode("ascii") + b'"}')
    with pytest.raises(RuntimeError, match="not valid UTF-8") as excinfo:
        adapter.resolve_auth_store(str(path))
    assert _AUTH_SECRET_MARKER not in str(excinfo.value)


def test_resolve_auth_store_malformed_json_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text('{"opencode-go": {"key": "' + _AUTH_SECRET_MARKER + '"', encoding="utf-8")
    with pytest.raises(RuntimeError, match="not valid JSON") as excinfo:
        adapter.resolve_auth_store(str(path))
    assert _AUTH_SECRET_MARKER not in str(excinfo.value)


def test_resolve_auth_store_non_object_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a JSON object") as excinfo:
        adapter.resolve_auth_store(str(path))
    assert _AUTH_SECRET_MARKER not in str(excinfo.value)
    path.write_text('"' + _AUTH_SECRET_MARKER + '"', encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a JSON object") as excinfo:
        adapter.resolve_auth_store(str(path))
    assert _AUTH_SECRET_MARKER not in str(excinfo.value)


def test_resolve_auth_store_exactly_at_bound_succeeds(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    raw = _auth_object_bytes(adapter.MAX_AUTH_STORE_BYTES)
    assert len(raw) == adapter.MAX_AUTH_STORE_BYTES
    path.write_bytes(raw)
    content = adapter.resolve_auth_store(str(path))
    parsed = json.loads(content)
    assert isinstance(parsed, dict)
    assert "k" in parsed


def test_resolve_auth_store_bound_plus_one_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    raw = _auth_object_bytes(adapter.MAX_AUTH_STORE_BYTES + 1)
    assert len(raw) == adapter.MAX_AUTH_STORE_BYTES + 1
    path.write_bytes(raw)
    with pytest.raises(RuntimeError, match="exceeds the bound") as excinfo:
        adapter.resolve_auth_store(str(path))
    assert _AUTH_SECRET_MARKER not in str(excinfo.value)


def test_resolve_auth_store_does_not_use_stat_or_is_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "auth.json"

    def forbidden_stat(self, *args, **kwargs):
        raise AssertionError("auth-store read must not call Path.stat (no pre-read authority)")

    def forbidden_is_file(self, *args, **kwargs):
        raise AssertionError("auth-store read must not call Path.is_file (no preflight)")

    monkeypatch.setattr(Path, "stat", forbidden_stat)
    monkeypatch.setattr(Path, "is_file", forbidden_is_file)

    path.write_bytes(_auth_object_bytes(64))
    parsed = json.loads(adapter.resolve_auth_store(str(path)))
    assert parsed["k"] == "x" * (64 - len(b'{"k":""}'))

    path.write_bytes(_auth_object_bytes(adapter.MAX_AUTH_STORE_BYTES + 1))
    with pytest.raises(RuntimeError, match="exceeds the bound"):
        adapter.resolve_auth_store(str(path))


def test_resolve_auth_store_stale_small_stat_cannot_permit_oversized_read(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "auth.json"
    path.write_bytes(_auth_object_bytes(64))
    oversized = _auth_object_bytes(adapter.MAX_AUTH_STORE_BYTES + 1024)
    real_open = Path.open

    def fake_open(self, *args, **kwargs):
        if self == path:
            return io.BytesIO(oversized)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)
    with pytest.raises(RuntimeError, match="exceeds the bound") as excinfo:
        adapter.resolve_auth_store(str(path))
    assert _AUTH_SECRET_MARKER not in str(excinfo.value)


def test_resolve_auth_store_secret_marker_never_enters_error_text(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_bytes(
        b'{"opencode-go":{"type":"api","key":"'
        + _AUTH_SECRET_MARKER.encode("ascii")
        + b'"'
    )
    with pytest.raises(RuntimeError) as excinfo:
        adapter.resolve_auth_store(str(path))
    assert _AUTH_SECRET_MARKER not in str(excinfo.value)
    assert _AUTH_SECRET_MARKER not in repr(excinfo.value)


def test_prepare_isolation_creates_no_auth_copy(tmp_path: Path) -> None:
    root = tmp_path / "isolation"
    isolation = adapter.prepare_isolation(root, auth_content='{"opencode-go": {"type": "api", "key": "marker"}}')
    # The credential bytes exist only in the child environment; no adapter-owned
    # plaintext credential artifact exists anywhere under the isolation root.
    for artifact in root.rglob("auth.json"):
        raise AssertionError(f"adapter-owned auth artifact found: {artifact}")
    assert isolation["environment"].get("OPENCODE_AUTH_CONTENT") == '{"opencode-go": {"type": "api", "key": "marker"}}'
    shutil.rmtree(root, ignore_errors=True)


def test_auth_content_reaches_child_environment_end_to_end(fake_launcher, auth_file, tmp_path) -> None:
    # The synthetic CLI probes its own environment: the injected auth content
    # is present and matches the marker, while the marker never appears in the
    # adapter's stdout/stderr (the probe reports booleans only).
    req = sample_request()
    req["synthetic_scenario"] = "auth-env-probe"
    req["synthetic_marker"] = auth_file["marker"]
    rc, stdout, stderr = run_adapter_real(fake_launcher, auth_file, req)
    assert rc == 0, stderr
    assert auth_file["marker"] not in stdout
    assert auth_file["marker"] not in stderr
    response = json.loads(stdout.strip())
    assert response["directive"]["kind"] == "action"
    assert response["directive"]["name"] == "run_reproduction"


def test_durability_fake_credential_survives_forced_termination_nowhere(fake_launcher, auth_file, tmp_path) -> None:
    """Emulate the REAL dangerous case: external parent/Local-Application
    termination while inference is active.  The adapter process is killed
    before any finally cleanup; the secret marker must not remain in any
    adapter-created temp/isolation location, stdout, stderr, or evidence."""
    marker = auth_file["marker"]
    work_root = tmp_path / "work-root"
    work_root.mkdir()
    evidence_file = tmp_path / "evidence.jsonl"
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "opencode_go_command_adapter.py"),
        "--executable", fake_launcher["launcher"],
        "--auth-file", str(auth_file["path"]),
        "--model", "deepseek-v4-pro",
        "--timeout", "60",
        "--work-root", str(work_root),
        "--evidence-file", str(evidence_file),
    ]
    req = sample_request()
    req["synthetic_scenario"] = "timeout"
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    process.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
    process.stdin.flush()
    process.stdin.close()

    # Wait (bounded, marker-driven) until the adapter created its disposable
    # work directory and the synthetic inference is actually active.
    deadline = time.monotonic() + 30.0
    work_dir: Path | None = None
    while time.monotonic() < deadline:
        candidates = [p for p in work_root.iterdir() if p.is_dir() and p.name.startswith("opencode-go-run-")]
        if candidates:
            work_dir = candidates[0]
            break
        time.sleep(0.1)
    assert work_dir is not None, "adapter never created its disposable work directory"

    # External termination: the adapter process is killed (Windows taskkill
    # /T /F mirrors the accepted escalation; POSIX SIGKILL mirrors a forced
    # supervisor kill).  The adapter's finally block cannot run.
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
    else:
        os.kill(process.pid, 9)
    stdout_bytes, stderr_bytes = process.communicate(timeout=30)

    # The secret marker must not remain anywhere in adapter-owned storage.
    for artifact in work_root.rglob("*"):
        if artifact.is_file():
            content = artifact.read_text(encoding="utf-8", errors="replace")
            assert marker not in content, f"secret marker leaked into {artifact}"
    assert not any(p.name == "auth.json" for p in work_root.rglob("auth.json")), (
        "adapter-owned auth artifact survived forced termination"
    )
    # ... and never in stdout/stderr/evidence.
    assert marker not in stdout_bytes.decode("utf-8", "replace")
    assert marker not in stderr_bytes.decode("utf-8", "replace")
    if evidence_file.is_file():
        assert marker not in evidence_file.read_text(encoding="utf-8", errors="replace")


# --- 9. Full Subprocess Integration with CancellableJsonlCommandTransport --

def test_cancellable_command_transport_with_adapter_fixture(fake_launcher, auth_file, tmp_path: Path) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "opencode_go_command_adapter.py"),
        *adapter_argv(fake_launcher, auth_file),
    ]
    config = LiveModelConfig(
        model_name="OpenCode Go DeepSeek V4 Pro",
        command=command,
        request_timeout_seconds=10.0,
    )
    transport = CancellableJsonlCommandTransport(config, max_output_bytes=65536)

    req = sample_request()
    req["synthetic_scenario"] = "state-legal"
    response = transport.request(req, timeout_seconds=10.0)

    assert isinstance(response, Mapping)
    assert "directive" in response
    assert response["directive"]["kind"] == "action"
    assert response["directive"]["name"] == "run_reproduction"
    assert response.get("usage") == {"prompt_tokens": 11, "completion_tokens": 5, "cost": 0.0042}


# --- 10. Synthetic Executable Scenarios (Zero Network) ---------------------

@pytest.mark.parametrize("scenario,expected_kind", [
    ("state-legal", "action"),
    ("copied-request-plus-valid", "action"),
    ("tool-call-text", "action"),
])
def test_adapter_with_synthetic_executable_valid_scenarios(
    fake_launcher, auth_file, tmp_path, scenario: str, expected_kind: str
) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "opencode_go_command_adapter.py"),
        *adapter_argv(fake_launcher, auth_file, timeout="10"),
    ]
    config = LiveModelConfig(
        model_name="OpenCode Go DeepSeek V4 Pro Synthetic",
        command=command,
        request_timeout_seconds=10.0,
    )
    transport = CancellableJsonlCommandTransport(config, max_output_bytes=65536)

    req = sample_request(state="Reproduce", allowed_actions=("run_reproduction",))
    req["synthetic_scenario"] = scenario
    response = transport.request(req, timeout_seconds=10.0)

    assert isinstance(response, Mapping)
    assert "directive" in response
    assert response["directive"]["kind"] == expected_kind
    assert response.get("usage", {}).get("cost") == 0.0042


def test_adapter_with_synthetic_executable_nonzero_exit(fake_launcher, auth_file, tmp_path) -> None:
    from agentic_debugger.evaluation.live import LiveTransportError

    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "opencode_go_command_adapter.py"),
        *adapter_argv(fake_launcher, auth_file, timeout="10"),
    ]
    config = LiveModelConfig(
        model_name="OpenCode Go DeepSeek V4 Pro Synthetic",
        command=command,
        request_timeout_seconds=10.0,
    )
    transport = CancellableJsonlCommandTransport(config, max_output_bytes=65536)

    req = sample_request()
    req["synthetic_scenario"] = "nonzero-exit"
    with pytest.raises(LiveTransportError, match="model command failed"):
        transport.request(req, timeout_seconds=5.0)


def test_adapter_with_synthetic_executable_timeout(fake_launcher, auth_file, tmp_path) -> None:
    from agentic_debugger.evaluation.live import LiveTransportError

    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "opencode_go_command_adapter.py"),
        *adapter_argv(fake_launcher, auth_file, timeout="0.5"),
    ]
    config = LiveModelConfig(
        model_name="OpenCode Go DeepSeek V4 Pro Synthetic",
        command=command,
        request_timeout_seconds=1.0,
    )
    transport = CancellableJsonlCommandTransport(config, max_output_bytes=65536)

    req = sample_request()
    req["synthetic_scenario"] = "timeout"
    with pytest.raises(LiveTransportError) as exc_info:
        transport.request(req, timeout_seconds=1.0)
    assert exc_info.value.kind in ("process_error", "request_timeout")


def test_adapter_single_invocation_guarantee(fake_launcher, auth_file, tmp_path) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "opencode_go_command_adapter.py"),
        *adapter_argv(fake_launcher, auth_file),
    ]
    config = LiveModelConfig(
        model_name="OpenCode Go DeepSeek V4 Pro",
        command=command,
        request_timeout_seconds=10.0,
    )
    transport = CancellableJsonlCommandTransport(config, max_output_bytes=65536)
    req = sample_request()
    req["synthetic_scenario"] = "state-legal"
    response = transport.request(req, timeout_seconds=10.0)
    assert response["directive"]["kind"] == "action"


# --- 11. POSIX external-cancellation helpers (Blocker D) -------------------

def test_child_group_registry_round_trip() -> None:
    if sys.platform == "win32":
        pytest.skip("child-group registry is a POSIX-only mechanism")
    adapter.register_inflight_child_group(4242)
    assert 4242 in adapter._snapshot_inflight_child_groups()
    adapter.unregister_inflight_child_group(4242)
    assert 4242 not in adapter._snapshot_inflight_child_groups()
    adapter.unregister_inflight_child_group(4242)  # idempotent


def test_child_group_registry_refuses_invalid_ids() -> None:
    if sys.platform == "win32":
        pytest.skip("child-group registry is a POSIX-only mechanism")
    for bad in (0, -1, True, "5"):
        adapter.register_inflight_child_group(bad)  # type: ignore[arg-type]
    assert adapter._snapshot_inflight_child_groups() == []


def test_inflight_registry_cleanup_is_reentrant() -> None:
    """Cleanup must re-acquire the registry lock on the holding thread.

    A POSIX SIGTERM/SIGINT handler runs on the main thread.  If it arrives
    while register/unregister already hold the lock, a non-reentrant Lock
    deadlocks and the detached OpenCode group is never killed.  The
    invocation runs on a worker so a regression cannot hang the suite.
    """
    finished = threading.Event()
    errors: list[BaseException] = []

    def invoke_under_held_lock() -> None:
        try:
            with adapter._INFLIGHT_CHILD_GROUPS_LOCK:
                adapter._terminate_inflight_child_groups()
        except BaseException as exc:  # noqa: BLE001 - the test classifies outcomes
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=invoke_under_held_lock, daemon=True)
    worker.start()
    assert finished.wait(2.0), "registry cleanup deadlocked on a non-reentrant lock"
    worker.join(timeout=1.0)
    assert not errors, f"reentrant cleanup raised: {errors!r}"
