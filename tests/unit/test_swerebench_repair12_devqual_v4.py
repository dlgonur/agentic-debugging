"""Repair-12 zero-provider request-envelope and V4 CLI regressions."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agentic_debugger.evaluation.live import LiveModelMetrics
from scripts import gpt_oss_swerebench_v2_devqual10_v4 as v4
from scripts import ollama_cloud_command_adapter as adapter


def _payload_with_json_line_length(target: int) -> dict[str, str]:
    payload = {"padding": ""}
    base = len((json.dumps(payload, separators=(",", ":")) + "\n").encode())
    payload["padding"] = "x" * (target - base)
    assert len((json.dumps(payload, separators=(",", ":")) + "\n").encode()) == target
    return payload


def _request_with_canonical_size(target: int) -> dict[str, str]:
    request = {"padding": ""}
    base = len(json.dumps(request, sort_keys=True, separators=(",", ":")).encode())
    request["padding"] = "x" * (target - base)
    assert len(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()) == target
    return request


def test_v4_request_envelope_constants_are_explicit_and_response_bound_is_separate():
    assert adapter.MAX_PUBLIC_REQUEST_BYTES == 128 * 1024
    assert adapter.MAX_STDIN_REQUEST_BYTES == 192 * 1024
    assert adapter.MAX_HTTP_REQUEST_BODY_BYTES == 256 * 1024
    assert adapter.MAX_RAW_RESPONSE_BYTES == 64 * 1024
    assert adapter.MAX_HTTP_REQUEST_BODY_BYTES != adapter.MAX_RAW_RESPONSE_BYTES


def test_canonical_public_request_accepts_just_below_v4_limit():
    request = _request_with_canonical_size(adapter.MAX_PUBLIC_REQUEST_BYTES - 1)
    assert len(adapter.canonical_public_request(request).encode()) == adapter.MAX_PUBLIC_REQUEST_BYTES - 1


def test_canonical_public_request_rejects_above_v4_limit_with_exact_numeric_detail():
    request = _request_with_canonical_size(adapter.MAX_PUBLIC_REQUEST_BYTES + 1)
    with pytest.raises(adapter.OllamaAdapterError) as raised:
        adapter.canonical_public_request(request)
    assert raised.value.kind == "request_too_large"
    assert str(adapter.MAX_PUBLIC_REQUEST_BYTES + 1) in str(raised.value)


def test_request_larger_than_historical_ceiling_is_valid_under_v4():
    request = _request_with_canonical_size(25_001)
    assert len(adapter.canonical_public_request(request).encode()) == 25_001


def test_stdin_bound_is_exactly_192_kib_and_independent_of_public_bound():
    accepted = _payload_with_json_line_length(adapter.MAX_STDIN_REQUEST_BYTES)
    assert adapter._read_request(io.StringIO(json.dumps(accepted, separators=(",", ":")) + "\n")) == accepted

    oversized = _payload_with_json_line_length(adapter.MAX_STDIN_REQUEST_BYTES + 1)
    with pytest.raises(adapter.OllamaAdapterError) as raised:
        adapter._read_request(io.StringIO(json.dumps(oversized, separators=(",", ":")) + "\n"))
    assert raised.value.kind == "request_too_large"


def test_http_request_body_uses_dedicated_bound_not_response_bound(monkeypatch):
    requests = []

    class Response:
        status = 200

        def getheader(self, name):
            return "2" if name == "Content-Length" else None

        def read(self, _size):
            return b"{}"

    class Connection:
        sock = None

        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, _method, _path, *, body, headers):
            requests.append(json.loads(body.decode("utf-8")))

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(adapter.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(adapter, "MAX_RAW_RESPONSE_BYTES", 1)
    request = _request_with_canonical_size(25_001)
    # The request reaches the fixture despite the deliberately tiny response
    # bound; the response then fails independently with response_too_large.
    with pytest.raises(adapter.OllamaAdapterError) as raised:
        adapter._http_json_request("http://127.0.0.1:11434/api", "POST", "/show", body=request, timeout_seconds=1)
    assert raised.value.kind == "response_too_large"
    assert requests == [request]


def test_live_metrics_persist_bounded_numeric_request_provenance():
    metrics = LiveModelMetrics()
    metrics.observe_request_size(adapter.MAX_PUBLIC_REQUEST_BYTES + 7, stdin_bytes=adapter.MAX_STDIN_REQUEST_BYTES + 3)
    value = metrics.to_mapping()["request_size"]
    assert value == {
        "max_canonical_public_request_bytes": adapter.MAX_PUBLIC_REQUEST_BYTES + 7,
        "canonical_public_request_bytes_limit": adapter.MAX_PUBLIC_REQUEST_BYTES,
        "rejected_canonical_public_request_bytes": adapter.MAX_PUBLIC_REQUEST_BYTES + 7,
        "stdin_request_bytes_limit": adapter.MAX_STDIN_REQUEST_BYTES,
        "rejected_stdin_request_bytes": adapter.MAX_STDIN_REQUEST_BYTES + 3,
        "http_request_body_bytes_limit": adapter.MAX_HTTP_REQUEST_BODY_BYTES,
    }
    assert "padding" not in json.dumps(value)


def test_v4_preflight_uses_output_summary_without_namespace_workaround(tmp_path: Path, monkeypatch):
    output = tmp_path / "preflight" / "summary.json"
    external = tmp_path / "campaign"
    monkeypatch.setattr(v4, "validate_devqual_identity", lambda project=None: {"experiment_id": v4.DEVQUAL_EXPERIMENT_ID})
    tasks = [type("Task", (), {"instance_id": f"instance-{index}"})() for index in range(10)]
    monkeypatch.setattr(v4, "_load_tasks", lambda: tasks)
    monkeypatch.setattr(v4, "load_official_bundles", lambda ids: {item: object() for item in ids})
    monkeypatch.setattr(v4, "run_task_preflight", lambda task, _bundle, external_root: {"instance_id": task.instance_id, "authorization_status": "ready-for-authorized-execution", "model_facing_isolated": True, "model_side_runtime_ready": True, "verifier_environment_ready": True, "verifier_baseline_valid": True, "pdb": {"classification": "none"}})
    rc = v4.main(["preflight", "--external-root", str(external), "--output-summary", str(output)])
    assert rc == 0
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["experiment_id"] == v4.DEVQUAL_EXPERIMENT_ID
    assert persisted["provider_generation_calls"] == 0


def test_v4_preflight_missing_output_path_fails_cleanly():
    with pytest.raises(SystemExit, match="preflight output path is required"):
        v4.main(["preflight", "--output-summary", ""])


def test_v4_authorize_consumes_distinct_preflight_argument(monkeypatch, tmp_path: Path):
    summary = tmp_path / "preflight" / "summary.json"
    summary.parent.mkdir()
    summary.write_text("{}", encoding="utf-8")
    config = tmp_path / "config"

    class FakeStore:
        def __init__(self, _root):
            pass

        def get(self, _profile):
            return type("Profile", (), {"live_command": lambda self: ("python", "--reasoning-effort", "high")})()

    monkeypatch.setattr(v4, "validate_devqual_identity", lambda project=None: {"experiment_id": v4.DEVQUAL_EXPERIMENT_ID})
    monkeypatch.setattr(v4, "_load_tasks", lambda: [])
    monkeypatch.setattr(v4, "_selection_hashes", lambda: {})
    monkeypatch.setattr(v4, "_selection_files", lambda: {})
    monkeypatch.setattr(v4, "CommandModelConfigStore", FakeStore)
    monkeypatch.setattr(v4, "run_zero_provider_authorization_preflight", lambda **kwargs: {"ready": True, "preflight_evidence_fingerprint": "f" * 64, "profile_metadata": {"configuration_fingerprint": "c" * 64}, "reasons": []})
    args = v4.build_parser().parse_args(["authorize", "--config-root", str(config), "--external-root", str(tmp_path / "campaign"), "--preflight-summary", str(summary)])
    result = v4._authorize(args)
    assert result["ready"] is True
    assert result["preflight_evidence_fingerprint"] == "f" * 64
    assert result["provider_generation_calls"] == 0
