"""Unit gates for the OpenCode provider adapter.

Covers model-id policy (opencode-go subscription models only, free-tier
excluded), PATH launcher defaulting, the full stdin->stdout contract with
monkeypatched machinery, the JSON-presence sanity boundary, the typed
error envelope, and canonical LiveModelConfig construction.  No real
OpenCode process is launched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import opencode_provider_adapter as adapter  # noqa: E402


def _request() -> dict:
    return {
        "protocol": {
            "name": "agentic-debugger-live-jsonl",
            "version": "1.3",
            "request_id": "t-1",
            "logical_model_call_index": 1,
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


class TestModelPolicy:
    def test_subscription_models_accepted(self) -> None:
        assert adapter.validate_model_id("opencode-go/glm-5.3") == "opencode-go/glm-5.3"
        assert adapter.validate_model_id("opencode-go/deepseek-v4-pro")

    def test_free_tier_and_foreign_models_rejected(self) -> None:
        for bad in ("opencode/hy3-free", "deepseek-v4-pro", "ollama-cloud/glm-5.2", "../escape", ""):
            with pytest.raises(adapter.OpenCodeProviderAdapterError):
                adapter.validate_model_id(bad)


class TestLauncherDefault:
    def test_windows_launcher_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr("shutil.which", lambda name: r"C:\npm\opencode.cmd" if name == "opencode.cmd" else None)
        assert adapter.resolve_default_opencode_executable() == r"C:\npm\opencode.cmd"

    def test_posix_launcher_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/opencode" if name == "opencode" else None)
        assert adapter.resolve_default_opencode_executable() == "/usr/bin/opencode"

    def test_missing_launcher_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert adapter.resolve_default_opencode_executable() is None


class TestAdapterContract:
    def _patch_machinery(self, monkeypatch: pytest.MonkeyPatch, text: str, usage: dict | None = None) -> None:
        monkeypatch.setattr(
            adapter.executable_identity,
            "resolve_verified_opencode_executable",
            lambda exe: {"native_executable": "fake-opencode"},
        )
        monkeypatch.setattr(adapter.frozen, "resolve_auth_store", lambda auth_file: "{}")
        monkeypatch.setattr(
            adapter.frozen,
            "execute_inference",
            lambda identity, model, message, timeout, auth_content=None: (text, usage, None),
        )

    def test_full_request_response_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_machinery(
            monkeypatch,
            '{"action":{"name":"find_function","arguments":{"symbol":"run"}}}',
            {"prompt_tokens": 5, "completion_tokens": 9},
        )
        stdout = _FakeStdout()
        code = adapter.run_adapter(
            _FakeStdin(json.dumps(_request())),
            stdout,
            model="opencode-go/glm-5.3",
            timeout_seconds=30.0,
        )
        assert code == 0
        payload = json.loads(stdout.text)
        assert payload["provider_completion_schema_version"] == adapter.PROVIDER_COMPLETION_SCHEMA_VERSION
        assert '"find_function"' in payload["directive_content"]
        assert payload["usage"]["completion_tokens"] == 9

    def test_kind_style_directive_also_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_machinery(monkeypatch, '{"kind":"transition","target_state":"DONE","reason":"ok"}')
        stdout = _FakeStdout()
        assert adapter.run_adapter(_FakeStdin(json.dumps(_request())), stdout, model="opencode-go/glm-5.3", timeout_seconds=5.0) == 0
        assert '"transition"' in json.loads(stdout.text)["directive_content"]

    def test_no_json_completion_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_machinery(monkeypatch, "I cannot answer that in JSON, sorry!")
        with pytest.raises(adapter.OpenCodeProviderAdapterError) as excinfo:
            adapter.run_adapter(
                _FakeStdin(json.dumps(_request())),
                _FakeStdout(),
                model="opencode-go/glm-5.3",
                timeout_seconds=5.0,
            )
        assert excinfo.value.kind == "invalid_directive"

    def test_missing_launcher_fails_configuration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(adapter, "resolve_default_opencode_executable", lambda: None)
        with pytest.raises(adapter.OpenCodeProviderAdapterError) as excinfo:
            adapter.run_adapter(
                _FakeStdin(json.dumps(_request())),
                _FakeStdout(),
                model="opencode-go/glm-5.3",
                timeout_seconds=5.0,
            )
        assert excinfo.value.kind == "configuration"

    def test_logical_call_envelope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_machinery(monkeypatch, "{}")
        request = _request()
        request["protocol"]["logical_model_call_index"] = 10
        with pytest.raises(adapter.OpenCodeProviderAdapterError) as excinfo:
            adapter.run_adapter(
                _FakeStdin(json.dumps(request)),
                _FakeStdout(),
                model="opencode-go/glm-5.3",
                timeout_seconds=5.0,
                max_logical_calls=4,
            )
        assert excinfo.value.kind == "logical_call_limit"


class TestLiveConfig:
    def test_build_opencode_live_config(self) -> None:
        from agentic_debugger.evaluation.live import LiveModelConfig

        config = adapter.build_opencode_live_config("opencode-go/glm-5.3", logical_call_ceiling=16)
        assert isinstance(config, LiveModelConfig)
        assert config.model_name == "opencode-go/glm-5.3"
        assert config.command[1].endswith("opencode_provider_adapter.py")
        assert "opencode-go/glm-5.3" in config.command
        assert config.tool_version == adapter.TOOL_VERSION

    def test_invalid_model_fails_closed(self) -> None:
        with pytest.raises(adapter.OpenCodeProviderAdapterError):
            adapter.build_opencode_live_config("opencode/hy3-free")
