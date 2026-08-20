from __future__ import annotations

import pytest

from agentic_debugger.swerebench import devqual_v11
from scripts import gpt_oss_swerebench_v2_devqual10_v11 as v11


def test_v11_identity_and_fixed_timeout_envelope() -> None:
    identity = devqual_v11.validate_devqual_identity()
    contract = devqual_v11.load_devqual_contract()
    assert identity["experiment_id"] == "gpt_oss_swerebench_v2_devqual10_v11"
    assert identity["parent_experiment_id"] == "gpt_oss_swerebench_v2_devqual10_v10"
    assert contract["provider"]["generation_timeout_seconds"] == 1080
    assert contract["provider"]["outer_request_timeout_seconds"] == 1200
    assert contract["provider"]["model_phase_seconds"] == 2400
    assert contract["provider"]["overall_task_timeout_seconds"] == 3600
    assert v11.MODEL_PHASE_SECONDS == 2400
    assert v11.TASK_TIMEOUT_SECONDS == 3600
    assert contract["timeout_semantics"]["official_evaluator_watchdog_seconds"] == 360
    assert contract["direct_execution_contract"]["preflight_command"] is False


def test_v11_has_no_preflight_subcommand() -> None:
    with pytest.raises(SystemExit):
        v11.build_parser().parse_args(["preflight"])
