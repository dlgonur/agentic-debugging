import pytest

from agentic_debugger.evaluation.task_schema import DebugTask, HIDDEN_TEST_PLACEHOLDER
from agentic_debugger.swerebench.isolation import (
    assert_model_facing_isolated,
    scan_mapping_for_leakage,
)
from agentic_debugger.swerebench.mapping import (
    MODEL_F2P_PLACEHOLDER,
    build_model_task,
    build_verifier_task,
    product_task_id,
)
from agentic_debugger.swerebench.pdb_readiness import classify_pdb_readiness
from agentic_debugger.swerebench.records import (
    OfficialInstanceBundle,
    PublicInstanceRecord,
    VerifierPrivateRecord,
)
from agentic_debugger.swerebench.schema import validate_pilot_result
from agentic_debugger.swerebench.selection import OrderedTask


def _ordered() -> OrderedTask:
    return OrderedTask(
        order_index=1,
        instance_id="example__repo-12",
        repo="example/repo",
        repo_canonical="example/repo",
        base_commit="a" * 40,
        assignment_key="b" * 64,
        first_repo_occurrence=True,
        license="MIT",
        difficulty="medium",
        age_bin="middle",
        patch_bin="small",
    )


def _bundle() -> OfficialInstanceBundle:
    gold = "diff --git a/pkg/mod.py b/pkg/mod.py\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-old_unique_gold_line_xyz\n+new_unique_gold_line_xyz\n"
    tests = "diff --git a/tests/test_hidden.py b/tests/test_hidden.py\n+def test_hidden_oracle():\n+    assert False\n"
    public = PublicInstanceRecord(
        instance_id="example__repo-12",
        repo="example/repo",
        base_commit="a" * 40,
        problem_statement="The formatter mishandles empty input.",
        language="python",
        license="MIT",
        created_at="2024-01-01",
        problem_statement_sha256="c" * 64,
    )
    private = VerifierPrivateRecord(
        instance_id="example__repo-12",
        fail_to_pass=("tests/test_hidden.py::test_hidden_oracle",),
        pass_to_pass=("tests/test_hidden.py::test_ok",),
        test_cmd="pytest",
        image_name="swe-rebench/example",
        python_version="3.11",
        has_gold_patch=True,
        has_test_patch=True,
        gold_patch_sha256="d" * 64,
        test_patch_sha256="e" * 64,
    )
    return OfficialInstanceBundle(
        public=public,
        private=private,
        _gold_patch=gold,
        _test_patch=tests,
        _fail_to_pass=private.fail_to_pass,
        _pass_to_pass=private.pass_to_pass,
        _test_cmd="pytest",
        _install_config={"test_cmd": "pytest", "log_parser": "parse_log_pytest"},
        _image_name="docker.io/swerebenchv2/example:1",
    )


def test_product_task_id_is_schema_safe():
    assert product_task_id("aristanetworks__anta-1000") == "swr-aristanetworks-anta-1000"


def test_model_and_verifier_tasks_isolate_hidden_assets():
    ordered = _ordered()
    bundle = _bundle()
    model = build_model_task(
        ordered, bundle, fixture_path="sources/example", allowed_write_paths=["pkg"]
    )
    verifier = build_verifier_task(
        ordered, bundle, fixture_path="sources/example", allowed_write_paths=["pkg"]
    )
    visible = model.agent_visible_mapping()
    assert "oracle" not in visible
    assert visible["tests"]["fail_to_pass"] == [HIDDEN_TEST_PLACEHOLDER]
    assert "test_hidden_oracle" not in repr(visible)
    assert "new_unique_gold_line_xyz" not in repr(visible)
    assert_model_facing_isolated(
        visible,
        hidden_needles=[
            "new_unique_gold_line_xyz",
            "tests/test_hidden.py::test_hidden_oracle",
        ],
    )
    verifier_visible = verifier.agent_visible_mapping()
    assert verifier_visible["tests"]["fail_to_pass"] == [HIDDEN_TEST_PLACEHOLDER]
    assert verifier.tests.fail_to_pass == ["tests/test_hidden.py::test_hidden_oracle"]
    assert MODEL_F2P_PLACEHOLDER not in verifier.tests.fail_to_pass


def test_leakage_scanner_flags_gold_fields():
    hits = scan_mapping_for_leakage({"oracle": {"target_files": ["pkg.py"]}})
    assert hits
    with pytest.raises(ValueError, match="not isolated"):
        assert_model_facing_isolated({"patch": "secret"})


def test_result_schema_fails_closed_on_debugger_assisted_without_pdb():
    payload = {
        "schema_version": "gpt-oss-swerebench-v2-pilot-result-v1",
        "identity": {
            "task_id": "swr-example",
            "instance_id": "example__repo-12",
            "repository": "example/repo",
            "base_commit": "a" * 40,
            "manifest_order_index": 1,
            "harness_commit": "9a47001",
            "model_profile_id": "ollama-cloud-gpt-oss-20b",
            "model_alias": "gpt-oss:20b-cloud",
            "upstream_model": "gpt-oss:20b",
            "policy": "pdb-on-uncertainty",
            "protocol": "1.3",
        },
        "runtime": {
            "session_id": None,
            "wall_clock_seconds": None,
            "logical_model_calls": None,
            "transport_attempts": None,
            "adapter_retry_count": 0,
            "fallback_count": 0,
            "token_usage": None,
            "provider_failures": None,
        },
        "trajectory": {
            "baseline_reproduced": None,
            "understand_reached": None,
            "hypotheses": None,
            "source_operations": None,
            "test_operations": None,
            "patch_attempts": None,
            "patch_rejections": None,
            "candidate_applied": None,
            "validate_sequence": None,
            "terminal_reason": None,
        },
        "pdb": {
            "pdb_eligible": True,
            "pdb_gate_opened": False,
            "pdb_entered": False,
            "debugger_actions": 0,
            "debugger_observations": 0,
            "runtime_evidence_preceded_patch": False,
            "pdb_not_exercised": True,
            "classification": "pdb_unavailable_by_treatment_contract",
        },
        "verification": {
            "verifier_ran": True,
            "verifier_infrastructure_valid": True,
            "baseline_valid": True,
            "fail_to_pass": "1/1",
            "pass_to_pass": "1/1",
            "full_suite": None,
            "verifier_outcome": "RESOLVED",
            "cleanup": True,
        },
        "science": {
            "admissible_model_result": True,
            "infrastructure_invalid": False,
            "contaminated": False,
            "provider_invalid": False,
            "resolved": True,
            "unresolved": False,
            "debugger_assisted_resolved": True,
            "execution_classification": "independent_verifier_resolved",
            "classification": "debugger_assisted_resolved",
        },
    }
    with pytest.raises(ValueError, match="actual PDB"):
        validate_pilot_result(payload)
    payload["science"]["debugger_assisted_resolved"] = False
    payload["science"]["classification"] = "admissible_resolved"
    validate_pilot_result(payload)


def test_pdb_readiness_is_honest_and_not_always_on():
    ready = classify_pdb_readiness("example__repo-12", has_official_fail_to_pass=True)
    assert ready.classification == "PDB_DEFERRED_TO_SEPARATE_TREATMENT"
    assert ready.model_selected_entry_available is False
    assert ready.oracle_probe_used is False
    assert ready.policy == "pdb-on-uncertainty"
    assert "not ALWAYS_ON" in ready.reason
    assert ready.future_pdb_required_treatment.startswith("pdb-required")
