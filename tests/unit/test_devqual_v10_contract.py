from __future__ import annotations

import pytest

from agentic_debugger.swerebench import devqual_v10
from scripts import gpt_oss_swerebench_v2_devqual10_v10 as v10


def test_v10_identity_and_fixed_timeout_envelope() -> None:
    identity = devqual_v10.validate_devqual_identity()
    contract = devqual_v10.load_devqual_contract()
    assert identity["experiment_id"] == "gpt_oss_swerebench_v2_devqual10_v10"
    assert identity["parent_experiment_id"] == "gpt_oss_swerebench_v2_devqual10_v9"
    assert contract["provider"]["generation_timeout_seconds"] == 1080
    assert contract["provider"]["outer_request_timeout_seconds"] == 1200
    assert contract["provider"]["model_phase_seconds"] == 1200
    assert contract["provider"]["overall_task_timeout_seconds"] == 2400
    assert contract["timeout_semantics"]["official_evaluator_watchdog_seconds"] == 360
    assert contract["direct_execution_contract"]["preflight_command"] is False


def test_v10_has_no_preflight_subcommand() -> None:
    with pytest.raises(SystemExit):
        v10.build_parser().parse_args(["preflight"])
