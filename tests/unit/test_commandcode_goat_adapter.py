"""Unit gates for the CommandCode GOAT provider adapter.

Covers model/timeout validation, CLI command resolution (node + package
entry, shim bypass, fail-closed misses), NDJSON result parsing, the full
stdin->stdout protocol-1.3 contract against a fake CLI, the typed stderr
error envelope, and canonical LiveModelConfig construction.  No real
provider contact: the CLI is a deterministic fake script.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import commandcode_goat_adapter as adapter  # noqa: E402


def _request(logical_index: int = 1) -> dict:
    return {
        "protocol": {
            "name": "agentic-debugger-live-jsonl",
            "version": "1.3",
            "request_id": "t-1",
            "logical_model_call_index": logical_index,
            "transport_attempt_index": 1,
        },
        "identity": {"evaluation_id": "t", "case_id": "t:c", "run_id": "t", "trajectory_id": "t"},
        "task": {"task_id": "t", "bug_description": "bug", "file_path": "x.py", "failure_output": "fail"},
        "policy": "pdb-on-uncertainty",
        "directive_schema": ["action", "transition"],
        "action_contracts": {},
        "controller": {"state": "UNDERSTAND", "budgets": {}, "hypotheses": [], "last_observation": None},
        "history": [],
        "directive_feedback": None,
        "instructions": "controller",
    }


class _FakeStdin:
    def __init__(self, payload: str) -> None:
        self.buffer = self
        self._payload = payload.encode("utf-8")

    def readline(self, limit: int = -1) -> bytes:
        return self._payload[:limit] if limit and limit > 0 else self._payload


class _FakeStdout:
    def __init__(self) -> None:
        self.parts: list[str] = []

    def write(self, text: str) -> None:
        self.parts.append(text)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self.parts)


def _write_fake_cli(tmp_path: Path, *, result_lines: list[str] | None = None, exit_code: int = 0, sleep_seconds: float = 0.0) -> Path:
    """A deterministic fake CommandCode CLI emitting canned NDJSON."""
    script = tmp_path / "fake_cmdc.py"
    body = json.dumps(result_lines if result_lines is not None else [])
    script.write_text(
        "\n".join(
            [
                "import json, sys, time",
                f"lines = json.loads({body!r})",
                f"if {sleep_seconds} > 0: time.sleep({sleep_seconds})",
                "for line in lines:",
                "    print(line, flush=True)",
                f"sys.exit({exit_code})",
            ]
        ),
        encoding="utf-8",
    )
    return script


_SUCCESS_LINES = [
    '{"type":"event","event":{"type":"run_start"}}',
    '{"type":"result","subtype":"success","stopReason":"end_turn","usage":{"inputTokens":10,"outputTokens":2},"finalText":"{\\"kind\\":\\"transition\\",\\"target_state\\":\\"DONE\\",\\"reason\\":\\"ok\\"}"}',
]


class TestValidation:
    def test_model_id_requires_vendor_slug_shape(self) -> None:
        assert adapter.validate_model_id("deepseek/deepseek-v4-flash") == "deepseek/deepseek-v4-flash"
        assert adapter.validate_model_id("gpt-5.6-sol") == "gpt-5.6-sol"
        with pytest.raises(adapter.CommandCodeAdapterError):
            adapter.validate_model_id("not a model!")
        with pytest.raises(adapter.CommandCodeAdapterError):
            adapter.validate_model_id("")

    def test_timeout_bounds(self) -> None:
        assert adapter.validate_timeout_seconds(300) == 300.0
        with pytest.raises(adapter.CommandCodeAdapterError):
            adapter.validate_timeout_seconds(0.5)
        with pytest.raises(adapter.CommandCodeAdapterError):
            adapter.validate_timeout_seconds(7200.0)


class TestCliResolution:
    def test_explicit_mjs_entry_uses_node(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        entry = tmp_path / "index.mjs"
        entry.write_text("// fake", encoding="utf-8")
        monkeypatch.setattr("shutil.which", lambda name: r"C:\node\node.exe" if name == "node" else None)
        prefix = adapter.resolve_cmdc_command(str(entry))
        assert prefix == (r"C:\node\node.exe", str(entry))

    def test_shim_resolves_package_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shim = tmp_path / "cmdc.CMD"
        shim.write_text("@echo off", encoding="utf-8")
        entry = tmp_path / "node_modules" / "command-code" / "dist" / "index.mjs"
        entry.parent.mkdir(parents=True)
        entry.write_text("// fake", encoding="utf-8")
        monkeypatch.setattr("shutil.which", lambda name: str(shim) if name == "cmdc" else r"C:\node\node.exe")
        prefix = adapter.resolve_cmdc_command()
        assert prefix == (r"C:\node\node.exe", str(entry))

    def test_missing_node_fails_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: None)
        with pytest.raises(adapter.CommandCodeAdapterError) as excinfo:
            adapter.resolve_cmdc_command()
        assert excinfo.value.kind == "configuration"


class TestResultParsing:
    def test_success_result_line(self) -> None:
        text, usage, stop = adapter.parse_cli_result("\n".join(_SUCCESS_LINES))
        assert '"kind"' in text
        assert usage is not None and usage.get("outputTokens") == 2
        assert stop == "end_turn"

    def test_no_result_record(self) -> None:
        with pytest.raises(adapter.CommandCodeAdapterError) as excinfo:
            adapter.parse_cli_result('{"type":"event","event":{"type":"run_start"}}')
        assert excinfo.value.kind == "invalid_completion"

    def test_error_subtype_maps_to_http_error(self) -> None:
        lines = ['{"type":"result","subtype":"error","error":"Error: 401 bad key","finalText":""}']
        with pytest.raises(adapter.CommandCodeAdapterError) as excinfo:
            adapter.parse_cli_result("\n".join(lines))
        assert excinfo.value.kind == "http_error"

    def test_empty_final_text(self) -> None:
        lines = ['{"type":"result","subtype":"success","finalText":"   "}']
        with pytest.raises(adapter.CommandCodeAdapterError) as excinfo:
            adapter.parse_cli_result("\n".join(lines))
        assert excinfo.value.kind == "invalid_completion"


class TestAdapterContract:
    def test_full_request_response_contract(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _write_fake_cli(tmp_path, result_lines=_SUCCESS_LINES)
        monkeypatch.setattr("shutil.which", lambda name: r"C:\node\node.exe" if name == "node" else None)
        monkeypatch.setattr(adapter, "resolve_cmdc_command", lambda explicit=None: (sys.executable, str(fake)))
        stdout = _FakeStdout()
        code = adapter.run_adapter(
            _FakeStdin(json.dumps(_request())),
            stdout,
            model="deepseek/deepseek-v4-flash",
            timeout_seconds=30.0,
        )
        assert code == 0
        payload = json.loads(stdout.text)
        assert payload["provider_completion_schema_version"] == adapter.PROVIDER_COMPLETION_SCHEMA_VERSION
        assert '"kind"' in payload["directive_content"]
        assert payload["usage"]["inputTokens"] == 10

    def test_logical_call_envelope(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _write_fake_cli(tmp_path, result_lines=_SUCCESS_LINES)
        monkeypatch.setattr(adapter, "resolve_cmdc_command", lambda explicit=None: (sys.executable, str(fake)))
        with pytest.raises(adapter.CommandCodeAdapterError) as excinfo:
            adapter.run_adapter(
                _FakeStdin(json.dumps(_request(logical_index=99))),
                _FakeStdout(),
                model="deepseek/deepseek-v4-flash",
                timeout_seconds=30.0,
                max_logical_calls=4,
            )
        assert excinfo.value.kind == "logical_call_limit"

    def test_cli_failure_emits_typed_error_envelope(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        fake = _write_fake_cli(tmp_path, result_lines=[], exit_code=3)
        monkeypatch.setattr(adapter, "resolve_cmdc_command", lambda explicit=None: (sys.executable, str(fake)))
        monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps(_request())))
        monkeypatch.setattr(
            sys,
            "argv",
            ["adapter", "--model", "deepseek/deepseek-v4-flash", "--cmdc-executable", str(fake)],
        )
        with pytest.raises(SystemExit) as exitinfo:
            adapter.main()
        assert exitinfo.value.code == 1
        stderr = capsys.readouterr().err
        envelope = json.loads(stderr.strip().splitlines()[-1])
        assert envelope["schema_version"] == "command-error-v1"
        assert envelope["kind"] in {"http_error", "adapter_error"}

    def test_prompt_embeds_wrapped_request(self) -> None:
        prompt = adapter.build_prompt(_request())
        assert "PUBLIC_REQUEST" in prompt or "{" in prompt
        assert "agentic-debugger-live-jsonl" in prompt


class TestLiveConfig:
    def test_build_commandcode_live_config(self) -> None:
        from agentic_debugger.evaluation.live import LiveModelConfig

        config = adapter.build_commandcode_live_config("deepseek/deepseek-v4-flash", logical_call_ceiling=16)
        assert isinstance(config, LiveModelConfig)
        assert config.model_name == "deepseek/deepseek-v4-flash"
        assert config.command[1].endswith("commandcode_goat_adapter.py")
        assert "--model" in config.command and "deepseek/deepseek-v4-flash" in config.command
        assert config.tool_version == adapter.TOOL_VERSION
        again = adapter.build_commandcode_live_config("deepseek/deepseek-v4-flash", logical_call_ceiling=16)
        assert again.configuration_fingerprint == config.configuration_fingerprint

    def test_invalid_model_fails_closed(self) -> None:
        with pytest.raises(adapter.CommandCodeAdapterError):
            adapter.build_commandcode_live_config("Not A Model")
