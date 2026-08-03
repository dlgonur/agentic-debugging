from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from scripts import opencode_protocol_transport as transport
from agentic_debugger.evaluation.live import JsonlCommandTransport


MODEL = "opencode/deepseek-v4-flash-free"
CATALOG = json.dumps({
    "id": "deepseek-v4-flash-free",
    "providerID": "opencode",
    "status": "active",
    "cost": {"input": 0, "output": 0, "cache": {"read": 0, "write": 0}},
    "variants": {"max": {"reasoningEffort": "max"}},
})
EFFECTIVE_CONFIG = json.dumps({
    **transport._isolation_config(),
    "agent": {},
    "mode": {},
    "command": {},
})

GO_MODEL = "opencode-go/test-deepseek-v4-flash"
GO_CATALOG = json.dumps({
    "id": "test-deepseek-v4-flash",
    "providerID": "opencode-go",
    "status": "active",
    "cost": {"input": 0.5, "output": 1.5, "cache": {"read": 0.25, "write": 0.25}},
    "variants": {"max": {"reasoningEffort": "max"}},
})
GO_ZERO_CATALOG = json.dumps({
    "id": "test-deepseek-v4-flash",
    "providerID": "opencode-go",
    "status": "active",
    "cost": {"input": 0, "output": 0, "cache": {"read": 0, "write": 0}},
    "variants": {"max": {"reasoningEffort": "max"}},
})
GO_CONFIG = json.dumps({
    **transport._isolation_config(route_mode="opencode-go"),
    "agent": {},
    "mode": {},
    "command": {},
})
GO_FINGERPRINT = transport.catalog_entry_fingerprint(json.loads(GO_CATALOG))


def _completed(command: list[str], stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def _run_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: subprocess.CompletedProcess[str] | Exception, request: dict | None = None) -> tuple[int, str, str, Path, dict]:
    evidence = tmp_path / "transport.jsonl"
    captured: dict[str, object] = {"calls": []}
    auth = tmp_path / "auth.json"
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    monkeypatch.setattr(transport, "_auth_state_path", lambda: auth)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str] | Exception:
        calls = captured["calls"]
        assert isinstance(calls, list)
        calls.append(command)
        if command == ["opencode.cmd", "--version"]:
            return _completed(command, stdout="1.18.10\n")
        if command[1:3] == ["models", "opencode"]:
            return _completed(command, stdout=CATALOG + "\n")
        if command[1:3] == ["debug", "config"]:
            return _completed(command, stdout=EFFECTIVE_CONFIG)
        captured["cwd"] = Path(str(kwargs["cwd"]))
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        captured["config"] = json.loads(Path(str(environment["OPENCODE_CONFIG"])).read_text(encoding="utf-8"))
        captured["agents_content"] = (Path(str(kwargs["cwd"])) / "AGENTS.md").read_text(encoding="utf-8")
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    monkeypatch.setattr(transport.shutil, "which", lambda name: r"C:\fake\opencode.cmd")
    payload = request or {"task": "public-gcd", "prompt": "public only"}
    monkeypatch.setattr(transport.sys, "stdin", io.StringIO(json.dumps(payload) + "\n"))
    rc = transport.main(["--model", MODEL, "--variant", "max", "--evidence-file", str(evidence)])
    captured_root = captured.get("cwd", tmp_path)
    assert isinstance(captured_root, Path)
    return rc, evidence.read_text(encoding="utf-8"), captured_root.as_posix(), evidence, captured


def _records(evidence: str) -> list[dict]:
    return [json.loads(line) for line in evidence.splitlines() if line.strip()]


def test_successful_directive_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directive = {"kind": "stop", "reason": "public failure reproduced"}
    raw = json.dumps({"type": "text", "part": {"text": json.dumps(directive)}}) + "\n"
    result = _completed(["opencode.cmd"], stdout=raw)
    rc, evidence, _, _, captured = _run_main(monkeypatch, tmp_path, result)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["directive"] == directive
    assert _records(evidence)[-1]["provider_exit_code"] == 0
    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("run") + 1].strip()


def test_nonzero_provider_exit_never_returns_directive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directive = {"kind": "stop", "reason": "must not be accepted"}
    result = _completed(["opencode.cmd"], stdout=json.dumps(directive), stderr="provider diagnostic", returncode=7)
    rc, evidence, _, _, _ = _run_main(monkeypatch, tmp_path, result)
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "code 7" in captured.err
    record = _records(evidence)[-1]
    assert record["event"] == "provider_exit_failure"
    assert record["provider_exit_code"] == 7
    assert record["provider_stderr"] == "provider diagnostic"


def test_timeout_is_transport_failure_and_cleans_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    timeout = subprocess.TimeoutExpired(["opencode.cmd"], 300, output="partial output", stderr="Bearer timeout-secret")
    rc, evidence, root, _, _ = _run_main(monkeypatch, tmp_path, timeout)
    assert rc == 1
    assert "TimeoutExpired" in capsys.readouterr().err
    record = _records(evidence)[-1]
    assert record["event"] == "provider_timeout"
    assert "timeout-secret" not in evidence
    assert not Path(root).exists()


def test_malformed_or_missing_directive_is_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = _completed(["opencode.cmd"], stdout='{"type":"text","part":{"text":"not a directive"}}\n')
    rc, evidence, _, _, _ = _run_main(monkeypatch, tmp_path, result)
    assert rc == 1
    assert "did not contain a directive" in capsys.readouterr().err
    assert "JsonExtractionError" in _records(evidence)[-1]["error"]


def test_opencode_telemetry_shape_is_retained_without_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directive = {"kind": "stop", "reason": "done"}
    events = [
        {"type": "text", "part": {"text": json.dumps(directive)}},
        {"type": "step_finish", "part": {"tokens": {"input": 11, "output": 5, "reasoning": 2, "cache": {"read": 3, "write": 1}}, "cost": 0.0}},
    ]
    result = _completed(["opencode.cmd"], stdout="\n".join(json.dumps(event) for event in events) + "\n")
    rc, evidence, _, _, _ = _run_main(monkeypatch, tmp_path, result)
    assert rc == 0
    response = json.loads(capsys.readouterr().out)
    assert response["provider_telemetry"] == {"input": 11, "output": 5, "reasoning": 2, "cache": {"read": 3, "write": 1}, "cost": 0.0}
    assert response["usage"] == {"prompt_tokens": 11, "completion_tokens": 5}
    assert _records(evidence)[-1]["provider_telemetry"] == response["provider_telemetry"]
    assert _records(evidence)[-1]["usage"] == response["usage"]


def test_missing_provider_telemetry_is_not_invented(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directive = {"kind": "stop", "reason": "done"}
    result = _completed(["opencode.cmd"], stdout=json.dumps({"type": "text", "part": {"text": json.dumps(directive)}}))
    rc, _, _, _, _ = _run_main(monkeypatch, tmp_path, result)
    assert rc == 0
    response = json.loads(capsys.readouterr().out)
    assert "usage" not in response
    assert "provider_telemetry" not in response


def test_oversized_evidence_remains_valid_bounded_jsonl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    directive = {"kind": "stop", "reason": "done"}
    oversized = "x" * (transport._MAX_EVIDENCE_FIELD_CHARS + 100)
    raw = json.dumps({"type": "text", "part": {"text": oversized + json.dumps(directive)}}) + "\n"
    result = _completed(["opencode.cmd"], stdout=raw)
    rc, evidence, _, evidence_path, _ = _run_main(monkeypatch, tmp_path, result)
    assert rc == 0
    record = next(item for item in _records(evidence) if "provider_stdout" in item)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    bounded = record["provider_stdout"]
    assert bounded["truncated"] is True
    assert bounded["original_character_count"] == len(raw)
    assert len(line) <= transport._MAX_EVIDENCE_CHARS


def test_secret_redaction_applies_to_request_and_diagnostics(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    request = {"authorization": "Bearer top-secret-value", "note": "token=another-secret"}
    result = _completed(["opencode.cmd"], stderr="password=stderr-secret", returncode=4)
    rc, evidence, _, _, _ = _run_main(monkeypatch, tmp_path, result, request=request)
    assert rc == 1
    assert "top-secret-value" not in evidence
    assert "another-secret" not in evidence
    assert "stderr-secret" not in evidence
    assert "<redacted>" in evidence


def test_opencode_cmd_launcher_selection_and_version_preflight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    command = transport.build_opencode_command(MODEL, "max", tmp_path, tmp_path / "public-request.json")
    assert command[0] == "opencode.cmd"
    assert command[command.index("--model") + 1] == MODEL
    assert command[command.index("--variant") + 1] == "max"
    assert command[command.index("run") + 1] == transport.PROTOCOL_INSTRUCTION
    assert command.index(transport.PROTOCOL_INSTRUCTION) < command.index("--file")
    assert command[command.index("--file") + 1] == str(tmp_path / "public-request.json")
    assert command[command.index("--file") + 2:] == []
    with pytest.raises(ValueError):
        transport.build_opencode_command(MODEL, "max", tmp_path, tmp_path / "request.json", message=" ")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _completed(argv, stdout="1.18.10\n")

    monkeypatch.setattr(transport.shutil, "which", lambda name: r"C:\tools\opencode.cmd")
    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    evidence = transport.verify_opencode_launcher()
    assert calls == [["opencode.cmd", "--version"]]
    assert evidence["launcher"] == "opencode.cmd"
    assert evidence["version"] == "1.18.10"


def test_isolation_denies_tools_mcp_and_instructions_and_cleans_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    directive = {"kind": "stop", "reason": "done"}
    result = _completed(["opencode.cmd"], stdout=json.dumps({"type": "text", "part": {"text": json.dumps(directive)}}))
    rc, _, _, _, captured = _run_main(monkeypatch, tmp_path, result)
    assert rc == 0
    config = captured["config"]
    assert isinstance(config, dict)
    assert config["permission"]["*"] == "deny"
    assert config["permission"]["bash"] == "deny"
    assert config["permission"]["read"] == "deny"
    assert config["permission"]["edit"] == "deny"
    assert config["mcp"] == {"*": {"enabled": False}}
    assert config["plugin"] == []
    assert config["instructions"] == []
    assert captured["agents_content"].startswith("This task-owned workspace")
    assert "do not use tools" in captured["agents_content"]
    environment = captured["environment"]
    assert environment["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    assert environment["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert environment["OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"] == "1"
    assert environment["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] == "1"
    assert environment["OPENCODE_DISABLE_AUTOUPDATE"] == "true"
    assert environment["HOMEDRIVE"]
    assert environment["HOMEPATH"]
    assert environment["OPENCODE_CONFIG"] != str(Path.home() / ".config" / "opencode" / "opencode.json")
    assert environment["HOME"] != str(Path.home())
    assert not Path(str(environment["OPENCODE_CONFIG"])).exists()
    assert not Path(str(environment["XDG_DATA_HOME"])).exists()


def test_missing_isolation_auth_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    evidence = tmp_path / "transport.jsonl"
    monkeypatch.setattr(transport, "_auth_state_path", lambda: tmp_path / "missing-auth.json")
    monkeypatch.setattr(transport.sys, "stdin", io.StringIO('{"task":"public"}\n'))
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return _completed(command, stdout="1.18.10\n")

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    assert transport.main(["--model", MODEL, "--variant", "max", "--evidence-file", str(evidence)]) == 1
    assert calls == []
    assert "required OpenCode authentication state is unavailable" in evidence.read_text(encoding="utf-8")


def test_launcher_missing_fails_closed_without_real_launcher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    evidence = tmp_path / "transport.jsonl"
    auth = tmp_path / "auth.json"
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    monkeypatch.setattr(transport, "_auth_state_path", lambda: auth)
    monkeypatch.setattr(transport.shutil, "which", lambda name: None)
    monkeypatch.setattr(transport.sys, "stdin", io.StringIO('{"task":"public"}' + chr(10)))
    assert transport.main(["--model", MODEL, "--variant", "max", "--evidence-file", str(evidence)]) == 1
    assert "opencode.cmd was not found on PATH" in evidence.read_text(encoding="utf-8")


def test_effective_config_rejects_permission_mcp_plugin_instruction_and_provider_failures() -> None:
    invalid_cases = [
        {**transport._isolation_config(), "permission": {**transport._ISOLATION_PERMISSION_DENIALS, "bash": "allow"}},
        {**transport._isolation_config(), "mcp": {"server": {"enabled": True}}},
        {**transport._isolation_config(), "plugin": ["unrelated-plugin"]},
        {**transport._isolation_config(), "instructions": ["unrelated.md"]},
        {**transport._isolation_config(), "enabled_providers": ["opencode", "anthropic"]},
    ]
    for config in invalid_cases:
        with pytest.raises(RuntimeError):
            transport._validate_effective_config(config)


def test_effective_config_acceptance_is_sanitized() -> None:
    assertions = transport._validate_effective_config(json.loads(EFFECTIVE_CONFIG))
    assert assertions == {
        "permission_default_denied": True,
        "required_permissions_denied": ["read", "write", "edit", "bash", "task", "webfetch", "websearch", "external_directory"],
        "mcp_servers_disabled": True,
        "plugins_empty": True,
        "instructions_empty": True,
        "sharing_disabled": True,
        "enabled_providers": ["opencode"],
        "autoupdate_disabled": True,
    }


def test_isolation_config_allowlist_is_route_aware() -> None:
    go = transport._isolation_config(route_mode="opencode-go")
    legacy = transport._isolation_config(route_mode="legacy")
    assert go["enabled_providers"] == ["opencode-go"]
    assert legacy["enabled_providers"] == ["opencode"]
    for config in (go, legacy):
        assert config["permission"]["*"] == "deny"
        assert config["mcp"] == {"*": {"enabled": False}}
        assert config["plugin"] == []
        assert config["instructions"] == []
        assert config["share"] == "disabled"
        assert config["autoupdate"] is False


def test_go_effective_config_accepts_only_exact_opencode_go() -> None:
    go_config = json.dumps({
        **transport._isolation_config(route_mode="opencode-go"),
        "agent": {}, "mode": {}, "command": {},
    })
    assertions = transport._validate_effective_config(json.loads(go_config), route_mode="opencode-go")
    assert assertions["enabled_providers"] == ["opencode-go"]
    with pytest.raises(RuntimeError, match="enabled provider allowlist"):
        transport._validate_effective_config(json.loads(EFFECTIVE_CONFIG), route_mode="opencode-go")


def test_effective_config_cross_route_and_mixed_provider_lists_are_rejected() -> None:
    go_base = {**transport._isolation_config(route_mode="opencode-go")}
    legacy_base = {**transport._isolation_config(route_mode="legacy")}
    for providers, route_mode in (
        (["opencode-go"], "legacy"),
        (["opencode"], "opencode-go"),
        (["opencode", "opencode-go"], "opencode-go"),
        (["opencode-go", "opencode"], "legacy"),
        ([], "opencode-go"),
        (["anthropic"], "opencode-go"),
    ):
        with pytest.raises(RuntimeError, match="enabled provider allowlist"):
            transport._validate_effective_config(
                {**(go_base if route_mode == "opencode-go" else legacy_base), "enabled_providers": providers},
                route_mode=route_mode,
            )


def test_catalog_command_failure_evidence_is_typed_bounded_and_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A nonzero local catalog inspection is a typed catalog-failure carrying
    bounded, sanitized stream detail; credentials never enter evidence and
    no ``opencode run`` is attempted."""
    evidence = tmp_path / "catalog-failure.jsonl"
    auth = tmp_path / "auth.json"
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    monkeypatch.setattr(transport, "_auth_state_path", lambda: auth)
    calls: list[list[str]] = []
    secret = "catalog password=hidden-catalog-secret exploded"
    oversized = "boom " + ("y" * (transport._CATALOG_FAILURE_DIAGNOSTIC_LIMIT * 2))

    def fake_run(command: list[str], **kwargs):
        calls.append(command)
        if command == ["opencode.cmd", "--version"]:
            return _completed(command, stdout="1.18.10\n")
        if command[1:3] == ["models", "opencode"]:
            return _completed(command, stdout=oversized, stderr=secret, returncode=1)
        raise AssertionError(f"unexpected OpenCode command: {command}")

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    monkeypatch.setattr(transport.shutil, "which", lambda name: r"C:\fake\opencode.cmd")
    monkeypatch.setattr(transport.sys, "stdin", io.StringIO('{"task": "public-only"}\n'))
    rc = transport.main(["--model", MODEL, "--variant", "max", "--evidence-file", str(evidence)])
    assert rc == 1
    failure = json.loads(evidence.read_text(encoding="utf-8"))
    assert failure["error"].startswith("CatalogFailureError:")
    assert failure["failure_classification"] == "catalog_command_failed"
    detail = failure["failure_detail"]
    assert detail["catalog_command"] == "opencode.cmd models opencode --verbose --pure"
    assert detail["catalog_exit_code"] == 1
    assert "hidden-catalog-secret" not in evidence.read_text(encoding="utf-8")
    assert "<redacted>" in evidence.read_text(encoding="utf-8")
    assert "truncated" in detail["catalog_stdout"]
    assert len(detail["catalog_stdout"]) <= transport._CATALOG_FAILURE_DIAGNOSTIC_LIMIT + 64
    assert not any(len(command) > 2 and command[1] == "run" for command in calls)
    assert not any(command[1:3] == ["debug", "config"] for command in calls)


def test_profile_api_fallback_is_used_when_all_profile_environment_is_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    auth = profile / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("synthetic auth", encoding="utf-8")
    for name in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(transport.Path, "home", staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no home"))))
    monkeypatch.setattr(transport, "_windows_profile_path", lambda: profile)
    assert transport._auth_state_path() == auth


def test_profile_resolution_fails_closed_without_trustworthy_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport.Path, "home", staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no home"))))
    monkeypatch.setattr(transport, "_windows_profile_path", lambda: (_ for _ in ()).throw(RuntimeError("profile API unavailable")))
    with pytest.raises(RuntimeError, match="profile API unavailable"):
        transport._auth_state_path()


def test_jsonl_transport_stripped_environment_is_explicit_and_has_no_profile_inference_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
        monkeypatch.delenv(name, raising=False)
    environment = JsonlCommandTransport.subprocess_environment()
    assert set(environment) <= {"PATH", "PYTHONIOENCODING", "SystemRoot"}
    assert not any(name in environment for name in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"))


def test_no_inference_preflight_succeeds_through_stripped_transport_environment(tmp_path: Path) -> None:
    auth = transport._auth_state_path()
    if not auth.is_file():
        pytest.skip("configured OpenCode auth state is unavailable")
    fake_dir = tmp_path / "fake-opencode"
    fake_dir.mkdir()
    fake_launcher = fake_dir / "opencode.cmd"
    fake_impl = fake_dir / "fake_opencode.py"
    fake_impl.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('1.18.10')\n"
        "elif args[:2] == ['models', 'opencode']:\n"
        f"    print({CATALOG!r})\n"
        "elif args[:3] == ['debug', 'config', '--pure']:\n"
        f"    print({EFFECTIVE_CONFIG!r})\n"
        "elif args and args[0] == 'run':\n"
        "    pathlib.Path(__file__).with_name('run-called').write_text('unexpected')\n"
        "    raise SystemExit(91)\n"
        "else:\n"
        "    raise SystemExit(92)\n",
        encoding="utf-8",
    )
    fake_launcher.write_text(f'@"{sys.executable}" "%~dp0fake_opencode.py" %*\n', encoding="utf-8")
    environment = JsonlCommandTransport.subprocess_environment()
    environment["PATH"] = str(fake_dir)
    evidence = tmp_path / "preflight.jsonl"
    child = subprocess.run(
        [sys.executable, str(Path(transport.__file__)), "--preflight", "--model", MODEL, "--variant", "max", "--evidence-file", str(evidence)],
        cwd=str(tmp_path), env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", check=False,
    )
    assert child.returncode == 0, child.stderr + child.stdout
    result = json.loads(child.stdout)
    assert result["preflight"] == "passed"
    assert result["provider_inference_started"] is False
    assert result["message_nonempty"] is True
    assert result["message_before_file"] is True
    assert result["agents_present_during_preflight"] is True
    assert result["auth_copy_present_during_preflight"] is True
    assert not (fake_dir / "run-called").exists()
    assert not Path(result["command"][result["command"].index("--dir") + 1]).exists()
    assert "synthetic auth" not in child.stdout + child.stderr + evidence.read_text(encoding="utf-8")


def test_go_preflight_reaches_catalog_parsing_under_synthetic_successful_models_opencode_go_response(tmp_path: Path) -> None:
    """The wrapper Go preflight reaches catalog parsing and effective-config
    validation under a synthetic successful ``models opencode-go`` response,
    and never executes ``opencode run``."""
    auth = transport._auth_state_path()
    if not auth.is_file():
        pytest.skip("configured OpenCode auth state is unavailable")
    go_model = "opencode-go/test-deepseek-v4-flash"
    go_catalog = json.dumps({
        "id": "test-deepseek-v4-flash",
        "providerID": "opencode-go",
        "status": "active",
        "cost": {"input": 0.5, "output": 1.5, "cache": {"read": 0.25, "write": 0.25}},
        "variants": {"max": {"reasoningEffort": "max"}},
    })
    go_fingerprint = transport.catalog_entry_fingerprint(json.loads(go_catalog))
    go_config = json.dumps({
        **transport._isolation_config(route_mode="opencode-go"),
        "agent": {}, "mode": {}, "command": {},
    })
    fake_dir = tmp_path / "fake-opencode-go"
    fake_dir.mkdir()
    fake_launcher = fake_dir / "opencode.cmd"
    fake_impl = fake_dir / "fake_opencode_go.py"
    fake_impl.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('1.0.0')\n"
        "elif args[:2] == ['models', 'opencode-go']:\n"
        f"    print({go_catalog!r})\n"
        "elif args[:3] == ['debug', 'config', '--pure']:\n"
        f"    print({go_config!r})\n"
        "elif args and args[0] == 'run':\n"
        "    pathlib.Path(__file__).with_name('run-called').write_text('unexpected')\n"
        "    raise SystemExit(91)\n"
        "else:\n"
        "    raise SystemExit(92)\n",
        encoding="utf-8",
    )
    fake_launcher.write_text(f'@"{sys.executable}" "%~dp0fake_opencode_go.py" %*\n', encoding="utf-8")
    environment = JsonlCommandTransport.subprocess_environment()
    environment["PATH"] = str(fake_dir)
    evidence = tmp_path / "go-preflight.jsonl"
    child = subprocess.run(
        [
            sys.executable, str(Path(transport.__file__)), "--preflight",
            "--model", go_model, "--variant", "max",
            "--route-mode", "opencode-go",
            "--expected-opencode-version", "1.0.0",
            "--expected-catalog-fingerprint", go_fingerprint,
            "--expected-runtime-model-id", go_model,
            "--expected-account-status", "ACTIVE",
            "--expected-billing-route", "SUBSCRIPTION",
            "--evidence-file", str(evidence),
        ],
        cwd=str(tmp_path), env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", check=False,
    )
    assert child.returncode == 0, child.stderr + child.stdout
    result = json.loads(child.stdout)
    assert result["preflight"] == "passed"
    assert result["provider_inference_started"] is False
    assert result["route_mode"] == "opencode-go"
    assert result["catalog"]["catalog_provider"] == "opencode-go"
    assert result["catalog"]["catalog_fingerprint"] == go_fingerprint
    assert result["effective_config"]["enabled_providers"] == ["opencode-go"]
    assert not (fake_dir / "run-called").exists()
    assert "synthetic auth" not in child.stdout + child.stderr + evidence.read_text(encoding="utf-8")


def test_preflight_auth_failure_happens_before_any_opencode_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    evidence = tmp_path / "preflight.jsonl"
    monkeypatch.setattr(transport, "_auth_state_path", lambda: tmp_path / "missing-auth.json")
    calls: list[list[str]] = []

    def fail_if_called(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        raise AssertionError("OpenCode must not be contacted when auth preflight fails")

    monkeypatch.setattr(transport.subprocess, "run", fail_if_called)
    result = transport.main(["--preflight", "--model", MODEL, "--variant", "max", "--evidence-file", str(evidence)])
    assert result == 1
    assert calls == []
    assert json.loads(evidence.read_text(encoding="utf-8"))["preflight"] == "blocked"


def _diagnostic_records(evidence: str) -> tuple[dict, dict]:
    records = _records(evidence)
    diagnostic = next(item for item in records if item.get("event") == "provider_result_diagnostics")
    failure = next(item for item in records if item.get("event") == "directive_extraction_failure")
    return diagnostic, failure


def test_ordinary_prose_preserves_bounded_text_and_classifies_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    raw = "Ordinary assistant prose without a protocol directive.\n"
    rc, evidence, root, _, _ = _run_main(monkeypatch, tmp_path, _completed(["opencode.cmd"], stdout=raw, stderr="provider stderr"))
    assert rc == 1
    assert capsys.readouterr().out == ""
    diagnostic, failure = _diagnostic_records(evidence)
    assert diagnostic["provider_exit_code"] == 0
    assert diagnostic["provider_stdout"] == raw
    assert diagnostic["provider_stderr"] == "provider stderr"
    assert diagnostic["parsed_event_count"] == 0
    assert diagnostic["non_json_line_count"] == 1
    assert diagnostic["non_json_samples"] == [raw.rstrip("\n")]
    assert failure["failure_classification"] == "text_without_protocol_directive"
    assert not Path(root).exists()


def test_markdown_fenced_directive_is_extracted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directive = {"kind": "stop", "reason": "fenced public directive"}
    text = "```json\n" + json.dumps(directive) + "\n```"
    result = _completed(["opencode.cmd"], stdout=json.dumps({"type": "text", "part": {"text": text}}))
    rc, _, _, _, _ = _run_main(monkeypatch, tmp_path, result)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["directive"] == directive


def test_parseable_object_without_kind_is_returned_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    provider_object = {"action": {"name": "run_reproduction", "arguments": {"phase": "baseline"}}}
    result = _completed(["opencode.cmd"], stdout=json.dumps({"type": "text", "part": {"text": json.dumps(provider_object)}}))
    rc, _, _, _, _ = _run_main(monkeypatch, tmp_path, result)
    assert rc == 0
    response = json.loads(capsys.readouterr().out)
    assert response["directive"] == provider_object


def test_multiple_json_objects_fail_closed_as_ambiguous(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = '{"first": 1} {"second": 2}'
    result = _completed(["opencode.cmd"], stdout=json.dumps({"type": "text", "part": {"text": text}}))
    rc, evidence, _, _, _ = _run_main(monkeypatch, tmp_path, result)
    assert rc == 1
    assert capsys.readouterr().out == ""
    _, failure = _diagnostic_records(evidence)
    assert failure["failure_classification"] == "ambiguous_json_output"


def test_empty_stdout_is_classified_without_a_directive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, evidence, _, _, _ = _run_main(monkeypatch, tmp_path, _completed(["opencode.cmd"], stdout="", stderr=""))
    assert rc == 1
    assert capsys.readouterr().out == ""
    diagnostic, failure = _diagnostic_records(evidence)
    assert diagnostic["provider_stdout_character_count"] == 0
    assert diagnostic["provider_stderr_character_count"] == 0
    assert failure["failure_classification"] == "empty_output"


def test_structured_provider_error_is_preserved_and_redacted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    event = {"type": "error", "error": {"message": "authorization=hidden-provider-secret", "code": "PROVIDER_ERROR"}}
    rc, evidence, _, _, _ = _run_main(monkeypatch, tmp_path, _completed(["opencode.cmd"], stdout=json.dumps(event)))
    assert rc == 1
    diagnostic, failure = _diagnostic_records(evidence)
    assert diagnostic["event_type_counts"] == {"error": 1}
    assert diagnostic["structured_error_events"][0]["error"]["message"] == "<redacted>"
    assert failure["failure_classification"] == "structured_provider_error"
    assert "hidden-provider-secret" not in evidence


def test_unknown_json_event_shape_is_preserved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    event = {"type": "future_event", "payload": {"value": "bounded public detail"}}
    rc, evidence, _, _, _ = _run_main(monkeypatch, tmp_path, _completed(["opencode.cmd"], stdout=json.dumps(event)))
    assert rc == 1
    diagnostic, failure = _diagnostic_records(evidence)
    assert diagnostic["parsed_events"] == [event]
    assert diagnostic["event_type_counts"] == {"future_event": 1}
    assert failure["failure_classification"] == "unsupported_event_shape"


def test_mixed_json_and_non_json_lines_retain_both_diagnostics(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    event = {"type": "step_finish", "part": {"status": "done"}}
    raw = "provider preface\n" + json.dumps(event) + "\ntrailing diagnostic\n"
    rc, evidence, _, _, _ = _run_main(monkeypatch, tmp_path, _completed(["opencode.cmd"], stdout=raw))
    assert rc == 1
    diagnostic, failure = _diagnostic_records(evidence)
    assert diagnostic["parsed_event_count"] == 1
    assert diagnostic["event_type_counts"] == {"step_finish": 1}
    assert diagnostic["non_json_line_count"] == 2
    assert diagnostic["non_json_samples"] == ["provider preface", "trailing diagnostic"]
    assert diagnostic["extracted_text_part_count"] == 0
    assert failure["failure_classification"] == "no_text_event"


def test_parse_failure_diagnostics_include_streams_counts_and_no_directive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    event = {"type": "message", "part": {"text": "not a protocol directive"}}
    rc, evidence, _, _, _ = _run_main(
        monkeypatch, tmp_path,
        _completed(["opencode.cmd"], stdout=json.dumps(event), stderr="provider diagnostic"),
    )
    assert rc == 1
    diagnostic, failure = _diagnostic_records(evidence)
    assert diagnostic["provider_exit_code"] == 0
    assert diagnostic["provider_stdout"] == json.dumps(event)
    assert diagnostic["provider_stderr"] == "provider diagnostic"
    assert diagnostic["event_type_counts"] == {"message": 1}
    assert diagnostic["extracted_text_values"] == ["not a protocol directive"]
    assert failure["failure_classification"] == "text_without_protocol_directive"


def test_large_malformed_output_remains_valid_bounded_jsonl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    raw = "ordinary output " + ("x" * (transport._MAX_EVIDENCE_FIELD_CHARS * 2))
    rc, evidence, _, _, _ = _run_main(monkeypatch, tmp_path, _completed(["opencode.cmd"], stdout=raw, stderr="stderr"))
    assert rc == 1
    records = _records(evidence)
    diagnostic = next(item for item in records if item.get("event") == "provider_result_diagnostics")
    assert diagnostic["provider_stdout_truncated"] is True
    assert diagnostic["provider_stdout_character_count"] == len(raw)
    for line in evidence.splitlines():
        assert len(line) <= transport._MAX_EVIDENCE_CHARS
        json.loads(line)


def test_parse_failure_secrets_in_both_streams_and_error_event_are_redacted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    event = {"type": "error", "message": "token=event-secret"}
    rc, evidence, _, _, _ = _run_main(
        monkeypatch, tmp_path,
        _completed(["opencode.cmd"], stdout=json.dumps(event) + "\npassword=stdout-secret", stderr="Bearer stderr-secret"),
    )
    assert rc == 1
    assert "event-secret" not in evidence
    assert "stdout-secret" not in evidence
    assert "stderr-secret" not in evidence
    assert "<redacted>" in evidence


# ---- shared isolated catalog-observation path (observe_isolated_catalog) -------


def _pin_isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hex_value: str = "b" * 32) -> Path:
    """Pin the helper-owned temporary isolation root under ``tmp_path``."""
    monkeypatch.setattr(transport.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(transport.uuid, "uuid4", lambda: types.SimpleNamespace(hex=hex_value))
    return tmp_path / f"agentic-opencode-isolated-catalog-{hex_value}"


def _go_observation_fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, catalog: str = GO_CATALOG, version: str = "1.0.0", effective_config: str = GO_CONFIG) -> list:
    auth = tmp_path / "auth.json"
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    monkeypatch.setattr(transport, "_auth_state_path", lambda: auth)
    calls: list = []

    def fake_run(command: list[str], **kwargs):
        calls.append((command, dict(kwargs.get("env") or {})))
        if command == ["opencode.cmd", "--version"]:
            return _completed(command, stdout=version + "\n")
        if command[1:3] == ["models", "opencode-go"]:
            return _completed(command, stdout=catalog + "\n")
        if command[1:3] == ["debug", "config"]:
            return _completed(command, stdout=effective_config)
        raise AssertionError(f"unexpected OpenCode command: {command}")

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    monkeypatch.setattr(transport.shutil, "which", lambda name: r"C:\fake\opencode.cmd")
    return calls


def test_observe_isolated_catalog_uses_isolated_environment_and_cleans_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _pin_isolated_root(tmp_path, monkeypatch)
    calls = _go_observation_fake(monkeypatch, tmp_path)
    result = transport.observe_isolated_catalog(GO_MODEL, "max", route_mode="opencode-go")
    assert result["fingerprint"] == GO_FINGERPRINT
    assert result["catalog"]["catalog_fingerprint"] == GO_FINGERPRINT
    assert result["effective_config"]["enabled_providers"] == ["opencode-go"]
    assert result["temporary_isolation_cleaned"] is True
    assert not root.exists()
    # Every inspection ran under the isolated environment.
    models_envs = [env for command, env in calls if command[1:3] == ["models", "opencode-go"]]
    assert models_envs
    assert models_envs[0]["OPENCODE_CONFIG"]
    assert models_envs[0]["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    assert models_envs[0]["HOME"] == str(root / "opencode-isolation")
    # Exactly the two inspection commands plus the effective-config check;
    # ``opencode run`` is never constructed or executed.
    assert [command for command, _ in calls] == [
        ["opencode.cmd", "--version"],
        ["opencode.cmd", "models", "opencode-go", "--verbose", "--pure"],
        ["opencode.cmd", "debug", "config", "--pure"],
    ]
    assert not any(len(command) > 2 and command[1] == "run" for command, _ in calls)


def test_observe_isolated_catalog_cleans_root_and_stays_typed_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _pin_isolated_root(tmp_path, monkeypatch)
    auth = tmp_path / "auth.json"
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    monkeypatch.setattr(transport, "_auth_state_path", lambda: auth)
    secret = "catalog token=hidden-isolation-secret exploded"
    calls: list[list[str]] = []

    def failing_run(command: list[str], **kwargs):
        calls.append(command)
        if command == ["opencode.cmd", "--version"]:
            return _completed(command, stdout="1.0.0\n")
        if command[1:3] == ["models", "opencode-go"]:
            return _completed(command, stderr=secret, returncode=4)
        raise AssertionError(f"unexpected OpenCode command: {command}")

    monkeypatch.setattr(transport.subprocess, "run", failing_run)
    monkeypatch.setattr(transport.shutil, "which", lambda name: r"C:\fake\opencode.cmd")
    with pytest.raises(transport.CatalogFailureError) as exc:
        transport.observe_isolated_catalog(GO_MODEL, "max", route_mode="opencode-go")
    assert exc.value.classification == "catalog_command_failed"
    assert "hidden-isolation-secret" not in str(exc.value.detail)
    assert "hidden-isolation-secret" not in str(exc.value)
    assert not root.exists()
    assert not any(len(command) > 2 and command[1] == "run" for command in calls)


def test_observe_isolated_catalog_independently_compares_expected_fingerprint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _pin_isolated_root(tmp_path, monkeypatch)
    _go_observation_fake(monkeypatch, tmp_path)
    result = transport.observe_isolated_catalog(
        GO_MODEL, "max", route_mode="opencode-go",
        expected_opencode_version="1.0.0",
        expected_runtime_model_id=GO_MODEL,
        expected_catalog_fingerprint=GO_FINGERPRINT,
    )
    assert result["fingerprint"] == GO_FINGERPRINT
    with pytest.raises(RuntimeError, match="catalog fingerprint drift"):
        transport.observe_isolated_catalog(
            GO_MODEL, "max", route_mode="opencode-go",
            expected_opencode_version="1.0.0",
            expected_runtime_model_id=GO_MODEL,
            expected_catalog_fingerprint="e" * 64,
        )


def test_observe_isolated_catalog_legacy_preserves_zero_cost_requirement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The shared isolated observation keeps the historical legacy zero-cost
    gate: a nonzero-priced catalog entry is rejected in legacy mode."""
    root = _pin_isolated_root(tmp_path, monkeypatch)
    legacy_config = json.dumps({
        **transport._isolation_config(route_mode="legacy"),
        "agent": {}, "mode": {}, "command": {},
    })
    auth = tmp_path / "auth.json"
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    monkeypatch.setattr(transport, "_auth_state_path", lambda: auth)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs):
        calls.append(command)
        if command == ["opencode.cmd", "--version"]:
            return _completed(command, stdout="1.18.10\n")
        if command[1:3] == ["models", "opencode"]:
            return _completed(command, stdout=GO_CATALOG + "\n")
        if command[1:3] == ["debug", "config"]:
            return _completed(command, stdout=legacy_config)
        raise AssertionError(f"unexpected OpenCode command: {command}")

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    monkeypatch.setattr(transport.shutil, "which", lambda name: r"C:\fake\opencode.cmd")
    with pytest.raises(RuntimeError, match="not zero-cost"):
        transport.observe_isolated_catalog(GO_MODEL, "max", route_mode="legacy")
    assert not root.exists()
    assert not any(len(command) > 2 and command[1] == "run" for command in calls)


def test_observe_isolated_catalog_legacy_accepts_zero_prices_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _pin_isolated_root(tmp_path, monkeypatch)
    legacy_config = json.dumps({
        **transport._isolation_config(route_mode="legacy"),
        "agent": {}, "mode": {}, "command": {},
    })
    auth = tmp_path / "auth.json"
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    monkeypatch.setattr(transport, "_auth_state_path", lambda: auth)
    calls: list = []

    def fake_run(command: list[str], **kwargs):
        calls.append(command)
        if command == ["opencode.cmd", "--version"]:
            return _completed(command, stdout="1.18.10\n")
        if command[1:3] == ["models", "opencode"]:
            return _completed(command, stdout=GO_ZERO_CATALOG + "\n")
        if command[1:3] == ["debug", "config"]:
            return _completed(command, stdout=legacy_config)
        raise AssertionError(f"unexpected OpenCode command: {command}")

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    monkeypatch.setattr(transport.shutil, "which", lambda name: r"C:\fake\opencode.cmd")
    result = transport.observe_isolated_catalog(GO_MODEL, "max", route_mode="legacy")
    assert result["catalog"]["zero_cost"] is True
    assert result["effective_config"]["enabled_providers"] == ["opencode"]
    assert not any(len(command) > 2 and command[1] == "run" for command in calls)


def test_observe_isolated_catalog_requires_exact_effective_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _pin_isolated_root(tmp_path, monkeypatch)
    wrong_config = json.dumps({
        **transport._isolation_config(route_mode="opencode-go"),
        "enabled_providers": ["opencode"],
        "agent": {}, "mode": {}, "command": {},
    })
    _go_observation_fake(monkeypatch, tmp_path, effective_config=wrong_config)
    with pytest.raises(RuntimeError, match="enabled provider allowlist"):
        transport.observe_isolated_catalog(GO_MODEL, "max", route_mode="opencode-go")


def test_observe_isolated_catalog_with_supplied_root_leaves_cleanup_to_caller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When the wrapper supplies its isolation root the helper does not own
    cleanup and the isolation stays alive for the run phase."""
    _pin_isolated_root(tmp_path, monkeypatch)
    _go_observation_fake(monkeypatch, tmp_path)
    root = tmp_path / "wrapper-root"
    root.mkdir()
    result = transport.observe_isolated_catalog(
        GO_MODEL, "max", route_mode="opencode-go",
        expected_opencode_version="1.0.0",
        expected_runtime_model_id=GO_MODEL,
        expected_catalog_fingerprint=GO_FINGERPRINT,
        isolation_root=root,
    )
    assert result["temporary_isolation_cleaned"] is False
    assert result["isolation"]["environment"]["OPENCODE_CONFIG"]
    assert root.is_dir()
    assert result["isolation"]["config_path"].is_file()
