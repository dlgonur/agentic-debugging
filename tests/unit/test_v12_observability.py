import json

from agentic_debugger.swerebench.result_rows import durable_session_evidence


def test_response_bound_is_provider_invalid_not_infrastructure(tmp_path) -> None:
    (tmp_path / "provider.metrics.json").write_text(
        json.dumps(
            {
                "setup_error_kinds": ["response_too_large"],
                "provider_error_kinds": ["provider_response_bound_exceeded"],
                "termination_reason": "provider_response_bound_exceeded",
            }
        ),
        encoding="utf-8",
    )
    evidence = durable_session_evidence(tmp_path, {})
    assert evidence["provider_invalid"] is True
    assert evidence["infrastructure_invalid"] is False
    assert evidence["runtime"]["provider_response_bound_exceeded"] is True


def _write_bound_metrics(path, *, execution=None, setup=None):
    (path / "provider.metrics.json").write_text(
        json.dumps({
            "setup_error_kinds": list(setup or ["response_too_large"]),
            "provider_error_kinds": ["provider_response_bound_exceeded"],
            "termination_reason": "provider_response_bound_exceeded",
        }), encoding="utf-8"
    )
    if execution is not None:
        (path / "execution.evidence.json").write_text(json.dumps(execution), encoding="utf-8")


def test_response_bound_plus_runtime_infrastructure_preserves_infrastructure(tmp_path) -> None:
    _write_bound_metrics(tmp_path, execution={"runtime_infrastructure_failure": True})
    evidence = durable_session_evidence(tmp_path, {})
    assert evidence["provider_invalid"] is True
    assert evidence["infrastructure_invalid"] is True


def test_response_bound_plus_setup_failure_preserves_infrastructure(tmp_path) -> None:
    _write_bound_metrics(tmp_path, setup=["response_too_large", "configuration"])
    evidence = durable_session_evidence(tmp_path, {})
    assert evidence["provider_invalid"] is True
    assert evidence["infrastructure_invalid"] is True
