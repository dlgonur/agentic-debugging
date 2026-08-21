from pathlib import Path

from agentic_debugger.swerebench.devqual_v12 import validate_devqual_identity
from agentic_debugger.swerebench.provenance import harness_content_sha256


def test_v12_identity_keeps_first_ten_and_observability_hash() -> None:
    identity = validate_devqual_identity()
    assert identity["experiment_id"] == "gpt_oss_swerebench_v2_devqual10_v12"
    assert len(identity["first_ten_instance_ids"]) == 10
    assert identity["harness_content_sha256"] == harness_content_sha256(Path.cwd())
