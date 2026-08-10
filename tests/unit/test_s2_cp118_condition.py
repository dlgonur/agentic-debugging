"""Focused offline tests for the S2 cp118-on-D1 diagnostic.

These tests prove, WITHOUT any model/GPU load:

1. **Model-condition-only change** — the S2 path changes ONLY the model
   condition vs frozen D1: the cp118 transport inherits the byte-identical
   ``request()`` of the frozen S1 transport; the S2 contract keeps the
   identical task, budgets, generation, interface (system prompt), verifier
   contract and treatment automation; the only differences are the model
   block (adapter applied) and the recorded treatment-difference/patch
   policy declarations.

2. **Adapter identity fail-closed** — ``verify_adapter_identity`` accepts
   only a byte-exact checkpoint directory (per-file SHA-256 + size, no
   extra/missing files, tree identity SHA-256, declared base) and raises
   RuntimeError on any drift — the deterministic enforcement of "if the
   definitive cp118 checkpoint cannot be located or verified exactly,
   STOP and report rather than substituting another checkpoint".

3. **Gate B strict semantics** — the six-condition real iterative loop:
   PASS only with two accepted PDB commands each producing a status-ok
   observation bound into the next model request; tool-error observations
   never satisfy strict; administrative D1 transitions never count;
   fail-closed on unverifiable provenance.

4. **Gate B legacy unchanged** — the frozen ``_compute_gate_b`` path is
   reused as-is.

5. **S1-P isolation** — the S2 contract records that no patch normalizer is
   applied (the S1-P serialization-normalization diagnostic is NOT part of
   S2).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.debugger_interaction_v2.transport import (
    BASE_REPOSITORY,
    BASE_REVISION,
    GENERATION_CONFIG,
    LocalRawQwenTransport,
)
from experiments.debugger_interaction_v2.bridge import SYSTEM_PROMPT
from experiments.debugger_interaction_v2.runner import (
    _compute_gate_b,
    _contract_sha256,
)
from experiments.debugger_interaction_v2_s2_cp118.s2_gates import (
    compute_gate_b_legacy,
    compute_gate_b_strict,
    observation_status_map,
)
from experiments.debugger_interaction_v2_s2_cp118.s2_runner import (
    _load_contract,
    _validate_contract,
)
from experiments.debugger_interaction_v2_s2_cp118.s2_transport import (
    LocalCp118QwenTransport,
    compute_adapter_identity,
    verify_adapter_identity,
)

S2_DIR = REPO_ROOT / "experiments" / "debugger_interaction_v2_s2_cp118"
D1_DIR = REPO_ROOT / "experiments" / "debugger_interaction_v2_d1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def s2_contract() -> dict:
    return _load_contract()


@pytest.fixture(scope="module")
def d1_contract() -> dict:
    value = json.loads((D1_DIR / "d1_contract.json").read_text(encoding="utf-8"))
    assert value["schema_version"] == "debugger-interaction-v2-d1"
    return value


@pytest.fixture()
def synthetic_adapter(tmp_path: Path):
    """A tiny synthetic adapter dir + its frozen identity block."""

    files = {
        "adapter_config.json": json.dumps({
            "base_model_name_or_path": BASE_REPOSITORY,
            "r": 8,
        }).encode("utf-8"),
        "adapter_model.safetensors": b"fake-weights-bytes",
    }
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)

    file_records = []
    combined = hashlib.sha256()
    for name in sorted(files):
        data = files[name]
        digest = hashlib.sha256(data).hexdigest()
        file_records.append({
            "path": name,
            "sha256": digest,
            "size_bytes": len(data),
        })
        combined.update(name.encode("utf-8"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        combined.update(b"\0")

    identity = {
        "tree_identity_sha256": combined.hexdigest(),
        "files": file_records,
    }
    return tmp_path, identity


# ---------------------------------------------------------------------------
# 1. Model-condition-only proof
# ---------------------------------------------------------------------------


def test_transport_request_inherited_byte_identical():
    """The ONLY transport change is the model condition: the request()
    implementation of the cp118 transport is byte-identical to the frozen
    S1 RAW transport (inherited)."""

    assert (
        inspect.getsource(LocalCp118QwenTransport.request)
        == inspect.getsource(LocalRawQwenTransport.request)
    )


def test_transport_inherits_raw_text_retention_envelope():
    """The cp118 transport inherits the S1 raw-text retention contract: it
    must return TransportResponse (raw_text + usage), not a directive
    envelope."""

    assert LocalCp118QwenTransport.request is LocalRawQwenTransport.request
    assert LocalRawQwenTransport.request.__qualname__.startswith(
        "LocalRawQwenTransport"
    )


def test_contract_model_identity_matches_frozen_base(s2_contract):
    """S2 keeps the identical frozen base identity as D1/S1."""

    model = s2_contract["model"]
    assert model["base_repository"] == BASE_REPOSITORY
    assert model["base_revision"] == BASE_REVISION
    assert model["rag_enabled"] is False
    assert model["generation"] == GENERATION_CONFIG
    assert model["adapter_applied"] is True


def test_contract_task_budgets_verifier_identical_to_d1(s2_contract, d1_contract):
    """Task, budgets and verifier contract are byte-identical to D1."""

    assert s2_contract["tasks"] == d1_contract["tasks"]
    assert s2_contract["budgets"] == d1_contract["budgets"]
    assert s2_contract["verifier_contract"] == d1_contract["verifier_contract"]
    assert s2_contract["conditions"][0]["policy"] == "always_on"
    assert (
        s2_contract["conditions"][0]["model_facing_interface"]
        == "state-specific-bridge-grammar"
    )


def test_contract_treatment_automation_identical_to_d1(s2_contract, d1_contract):
    """S2 keeps the D1 harness automation: exactly the two administrative
    transitions, never debugger commands."""

    assert (
        s2_contract["s2_treatment"]["automated_only"]
        == d1_contract["d1_treatment"]["automated_only"]
    )
    assert (
        s2_contract["s2_treatment"]["never_automated"]
        == d1_contract["d1_treatment"]["never_automated"]
    )
    assert (
        s2_contract["s2_treatment"][
            "administrative_transitions_do_not_count_as_debugger_commands"
        ]
        is True
    )
    assert (
        s2_contract["treatment_differences_from_d1"]["everything_else_unchanged"]
        is True
    )


def test_system_prompt_identity_unchanged_from_d1(s2_contract, d1_contract):
    """The model-facing interface (system prompt) is unchanged: the S2 run
    identity records the same system_prompt_sha256 as the frozen D1 identity
    (which itself equals the frozen SYSTEM_PROMPT)."""

    from experiments.debugger_interaction_v2.runner import _run_identity
    from experiments.debugger_interaction_v2_s2_cp118.s2_runner import (
        _s2_run_identity,
    )

    d1_identity = _run_identity(d1_contract)
    s2_identity = _s2_run_identity(s2_contract, {"tree_identity_sha256": "0" * 64, "files": []})
    assert s2_identity["system_prompt_sha256"] == d1_identity["system_prompt_sha256"]
    assert (
        s2_identity["system_prompt_sha256"]
        == hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    )


def test_contract_diff_vs_d1_confined_to_model_block(s2_contract, d1_contract):
    """Aside from the model block and the declared S2 treatment/patch-policy
    records, the S2 contract structure is identical to D1's."""

    s2_keys = set(s2_contract.keys())
    d1_keys = set(d1_contract.keys())
    # S2 adds: s2_treatment, treatment_differences_from_d1, patch_policy,
    # gate_b_criteria (strict), claims_boundary wording.  D1-only: d1_treatment.
    assert {"s2_treatment", "treatment_differences_from_d1", "patch_policy"} <= s2_keys
    assert "d1_treatment" in d1_keys
    # Shared blocks identical except conditions (whose identity fields name
    # the S2 condition itself; the treatment fields must match).
    for shared in ("tasks", "budgets", "verifier_contract"):
        assert s2_contract[shared] == d1_contract[shared]
    s2_cond = dict(s2_contract["conditions"][0])
    d1_cond = dict(d1_contract["conditions"][0])
    s2_cond.pop("condition_id")
    s2_cond.pop("base_experiment")
    d1_cond.pop("condition_id")
    d1_cond.pop("base_experiment")
    assert s2_cond == d1_cond
    # Model block: everything identical except adapter_applied (false->true)
    # and the frozen adapter identity.
    s2_model = {
        k: v for k, v in s2_contract["model"].items()
        if k not in ("adapter_applied", "adapter_identity")
    }
    d1_model = {
        k: v for k, v in d1_contract["model"].items()
        if k != "adapter_applied"
    }
    assert s2_model == d1_model


def test_contract_adapter_identity_frozen_and_consistent(s2_contract):
    """The contract's frozen cp118 adapter identity is structurally
    consistent (unique paths, well-formed digests/sizes) and matches the
    established frozen tree identity recorded by the accepted prior cp118
    pilot."""

    identity = s2_contract["model"]["adapter_identity"]
    files = identity["files"]
    assert len(files) == 7
    paths = [f["path"] for f in files]
    assert len(set(paths)) == len(paths)
    assert {
        "adapter_config.json",
        "adapter_model.safetensors",
        "ADAPTER_MANIFEST.json",
    } <= set(paths)
    for item in files:
        assert len(item["sha256"]) == 64
        assert all(ch in "0123456789abcdef" for ch in item["sha256"])
        assert item["size_bytes"] > 0
    # The frozen tree identity is the established one recorded by the
    # accepted prior cp118 pilot run (same Path-based convention).
    assert identity["tree_identity_sha256"] == (
        "65b5ed9a354d4b2c03ba86e2b8065118e11abab9c439cb481b5739f1b86e7c00"
    )
    assert identity["selected_checkpoint_step"] == 118
    assert "eval_loss" in identity["selection_rule"]


def test_contract_patch_policy_no_normalizer(s2_contract):
    """S1-P serialization normalization is NOT part of S2: the contract
    records that no patch normalizer is applied (FirstMate amendment 1)."""

    assert s2_contract["patch_policy"]["patch_normalizer_applied"] is False


def test_validate_contract_passes_on_frozen_contract(s2_contract):
    """The frozen contract passes its own validation (no external state)."""

    result = _validate_contract(s2_contract)
    assert result["validated"] is True
    assert result["contract_sha256"] == _contract_sha256(s2_contract)


def test_validate_contract_rejects_model_condition_drift(s2_contract):
    """If the model block drifted back to RAW, validation must fail closed."""

    drifted = json.loads(json.dumps(s2_contract))
    drifted["model"]["adapter_applied"] = False
    with pytest.raises(RuntimeError, match="adapter_applied"):
        _validate_contract(drifted)


def test_validate_contract_rejects_adapter_identity_drift(s2_contract):
    """If the frozen adapter identity is tampered, validation fails closed."""

    drifted = json.loads(json.dumps(s2_contract))
    drifted["model"]["adapter_identity"]["files"][0]["sha256"] = "g" * 64
    with pytest.raises(RuntimeError, match="malformed sha256"):
        _validate_contract(drifted)


def test_validate_contract_rejects_normalizer_reenabled(s2_contract):
    drifted = json.loads(json.dumps(s2_contract))
    drifted["patch_policy"]["patch_normalizer_applied"] = True
    with pytest.raises(RuntimeError, match="normalizer"):
        _validate_contract(drifted)


# ---------------------------------------------------------------------------
# 2. Adapter identity fail-closed
# ---------------------------------------------------------------------------


def test_adapter_identity_accepts_exact_dir(synthetic_adapter):
    adapter_dir, identity = synthetic_adapter
    verified = verify_adapter_identity(adapter_dir, identity)
    assert verified["tree_identity_sha256"] == identity["tree_identity_sha256"]
    assert {f["path"] for f in verified["files"]} == {
        "adapter_config.json",
        "adapter_model.safetensors",
    }


def test_adapter_identity_rejects_modified_file(synthetic_adapter):
    adapter_dir, identity = synthetic_adapter
    (adapter_dir / "adapter_model.safetensors").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="sha256"):
        verify_adapter_identity(adapter_dir, identity)


def test_adapter_identity_rejects_missing_file(synthetic_adapter):
    adapter_dir, identity = synthetic_adapter
    (adapter_dir / "adapter_model.safetensors").unlink()
    with pytest.raises(RuntimeError, match="missing files"):
        verify_adapter_identity(adapter_dir, identity)


def test_adapter_identity_rejects_extra_file(synthetic_adapter):
    adapter_dir, identity = synthetic_adapter
    (adapter_dir / "unexpected.bin").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="unexpected files"):
        verify_adapter_identity(adapter_dir, identity)


def test_adapter_identity_rejects_foreign_base(synthetic_adapter):
    adapter_dir, _ = synthetic_adapter
    # Rewrite the config with a foreign base, then recompute the on-disk
    # identity so the per-file hash/size checks pass and the declared-base
    # check is the one that must fire.
    (adapter_dir / "adapter_config.json").write_text(json.dumps({
        "base_model_name_or_path": "someone/else-7B",
    }), encoding="utf-8")
    identity = compute_adapter_identity(adapter_dir)
    with pytest.raises(RuntimeError, match="different base"):
        verify_adapter_identity(adapter_dir, identity)


def test_adapter_identity_rejects_tree_drift(synthetic_adapter):
    adapter_dir, identity = synthetic_adapter
    identity = json.loads(json.dumps(identity))
    identity["tree_identity_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="tree_identity_sha256"):
        verify_adapter_identity(adapter_dir, identity)


def test_adapter_identity_rejects_missing_directory(tmp_path):
    with pytest.raises(RuntimeError, match="not a directory"):
        compute_adapter_identity(tmp_path / "does-not-exist")


# ---------------------------------------------------------------------------
# 3. Gate B strict semantics
# ---------------------------------------------------------------------------


def _telemetry_record(
    index: int,
    *,
    status: str = "accepted",
    action: str | None = None,
    prior_obs: str | None = None,
    rendered_sha: str | None = "abc",
    raw_status: str = "decoded",
) -> dict:
    return {
        "model_call_index": index,
        "raw_response_status": raw_status,
        "parse_result": {"status": status},
        "translated_directive": {"action_name": action},
        "provenance": {
            "prior_observation_id": prior_obs,
            "prior_observation_sha256": "x" if prior_obs else None,
            "rendered_observation_sha256": rendered_sha if prior_obs else None,
        },
    }


def _admin_record(index: int, prior_obs: str | None = None) -> dict:
    record = _telemetry_record(index, status="administrative", raw_status="administrative_navigation")
    record["provenance"]["prior_observation_id"] = prior_obs
    return record


def test_gate_b_strict_passes_with_two_ok_observations():
    """Ideal loop: command1 -> ok obs1 -> bound -> command2 -> ok obs2 bound
    into the next model request."""

    telemetry = [
        _telemetry_record(0, action="run_reproduction", prior_obs=None, rendered_sha=None),
        _telemetry_record(1, action="start_pdb_session", prior_obs="obs-repro"),
        _telemetry_record(2, action="get_stack_summary", prior_obs="obs-ok-1"),
        _telemetry_record(3, action="run_reproduction", status="rejected", prior_obs="obs-ok-2"),
    ]
    status_by_id = {"obs-repro": "ok", "obs-ok-1": "ok", "obs-ok-2": "ok"}
    result = compute_gate_b_strict(telemetry, status_by_id)
    assert result["passed"] is True
    assert result["accepted_pdb_count"] == 2
    assert result["successful_pdb_observations"] == ["obs-ok-1", "obs-ok-2"]
    assert all(c["met"] for c in result["conditions"])
    assert len(result["conditions"]) == 6


def test_gate_b_strict_fails_on_tool_error_observation():
    """A tool-error observation (e.g. D1's break 20 on a 19-line probe) is
    real provenance but MUST NOT satisfy Gate B strict."""

    telemetry = [
        _telemetry_record(0, action="run_reproduction", prior_obs=None, rendered_sha=None),
        _telemetry_record(1, action="start_pdb_session", prior_obs="obs-repro"),
        _telemetry_record(2, action="get_source_window", prior_obs="obs-error"),
    ]
    status_by_id = {"obs-repro": "ok", "obs-error": "error"}
    result = compute_gate_b_strict(telemetry, status_by_id)
    assert result["passed"] is False
    assert result["successful_pdb_observations"] == []
    cond3 = [c for c in result["conditions"] if c["condition"] == 3][0]
    assert cond3["met"] is False
    assert "MUST NOT satisfy" in cond3["detail"]
    cond5 = [c for c in result["conditions"] if c["condition"] == 5][0]
    assert cond5["met"] is False


def test_gate_b_strict_fails_with_single_command():
    """One accepted PDB command (D1 RAW shape) can never pass strict."""

    telemetry = [
        _telemetry_record(0, action="run_reproduction", prior_obs=None, rendered_sha=None),
        _telemetry_record(1, action="start_pdb_session", prior_obs="obs-repro"),
        _telemetry_record(2, action="get_source_window", prior_obs="obs-ok-1"),
    ]
    status_by_id = {"obs-repro": "ok", "obs-ok-1": "ok"}
    result = compute_gate_b_strict(telemetry, status_by_id)
    assert result["passed"] is False
    assert result["accepted_pdb_count"] == 1
    assert result["successful_pdb_observations"] == ["obs-ok-1"]
    assert [c["condition"] for c in result["conditions"] if not c["met"]] == [5, 6]


def test_gate_b_strict_fails_with_no_commands():
    telemetry = [_telemetry_record(0, action="run_reproduction", prior_obs=None, rendered_sha=None)]
    result = compute_gate_b_strict(telemetry, {"obs-repro": "ok"})
    assert result["passed"] is False
    assert result["accepted_pdb_count"] == 0
    assert all(c["met"] is False for c in result["conditions"])


def test_gate_b_strict_fails_without_provenance_binding():
    """Second command whose observation is not bound into a next request:
    fail closed."""

    telemetry = [
        _telemetry_record(0, action="start_pdb_session", prior_obs=None, rendered_sha=None),
        _telemetry_record(1, action="continue_pdb_session", prior_obs="obs-ok-1"),
    ]
    status_by_id = {"obs-ok-1": "ok"}
    result = compute_gate_b_strict(telemetry, status_by_id)
    assert result["passed"] is False
    assert result["accepted_pdb_count"] == 2
    # The second command's produced observation cannot be verified (no next
    # model request): condition 6 fails.
    cond6 = [c for c in result["conditions"] if c["condition"] == 6][0]
    assert cond6["met"] is False


def test_gate_b_strict_ignores_administrative_records():
    """Administrative D1 transitions never count toward Gate B strict and
    are skipped when locating the next actual model request."""

    telemetry = [
        _admin_record(0, prior_obs="obs-repro"),
        _admin_record(1, prior_obs="obs-repro"),
        _telemetry_record(2, action="start_pdb_session", prior_obs="obs-repro"),
        _telemetry_record(3, action="get_stack_summary", prior_obs="obs-ok-1"),
        _telemetry_record(4, action="run_reproduction", status="rejected", prior_obs="obs-ok-2"),
    ]
    status_by_id = {"obs-repro": "ok", "obs-ok-1": "ok", "obs-ok-2": "ok"}
    result = compute_gate_b_strict(telemetry, status_by_id)
    assert result["passed"] is True
    assert result["accepted_pdb_count"] == 2


def test_gate_b_strict_skips_rejected_records_between_commands():
    """Rejected model responses between PDB commands do not break the
    observation-binding chain."""

    telemetry = [
        _telemetry_record(0, action="start_pdb_session", prior_obs=None, rendered_sha=None),
        _telemetry_record(1, action="run_reproduction", status="rejected", prior_obs="obs-ok-1"),
        _telemetry_record(2, action="get_stack_summary", prior_obs="obs-ok-1"),
        _telemetry_record(3, action="run_reproduction", status="rejected", prior_obs="obs-ok-2"),
    ]
    status_by_id = {"obs-ok-1": "ok", "obs-ok-2": "ok"}
    result = compute_gate_b_strict(telemetry, status_by_id)
    assert result["passed"] is True
    assert result["accepted_pdb_count"] == 2


def test_observation_status_map_parses_trajectory():
    events = "\n".join([
        json.dumps({
            "event_type": "observation",
            "payload": {
                "observation": {
                    "observation_id": "obs-a",
                    "status": "ok",
                    "name": "run_reproduction",
                }
            },
        }),
        json.dumps({
            "event_type": "observation",
            "payload": {
                "observation": {
                    "observation_id": "obs-b",
                    "status": "error",
                    "name": "start_pdb_session",
                }
            },
        }),
        json.dumps({"event_type": "action", "payload": {"action": {"name": "x"}}}),
    ])
    mapping = observation_status_map(events)
    assert mapping == {"obs-a": "ok", "obs-b": "error"}


def test_observation_status_map_fails_closed_on_malformed():
    with pytest.raises(RuntimeError):
        observation_status_map("not json")


def test_observation_status_map_fails_closed_on_missing_status():
    events = json.dumps({
        "event_type": "observation",
        "payload": {"observation": {"observation_id": "obs-a"}},
    })
    with pytest.raises(RuntimeError, match="status"):
        observation_status_map(events)


# ---------------------------------------------------------------------------
# 4. Gate B legacy unchanged
# ---------------------------------------------------------------------------


def test_gate_b_legacy_is_frozen_computation():
    """The legacy gate is the repository's existing computation, unchanged."""

    telemetry = [
        _telemetry_record(0, action="run_reproduction", prior_obs=None, rendered_sha=None),
        _telemetry_record(1, action="start_pdb_session", prior_obs="obs-repro"),
    ]
    legacy = compute_gate_b_legacy(telemetry)
    frozen = _compute_gate_b(telemetry)
    assert legacy == frozen
    assert legacy["passed"] is False
    assert legacy["accepted_pdb_count"] == 1


def test_gate_b_legacy_excludes_administrative_records():
    telemetry = [
        _admin_record(0),
        _admin_record(1),
        _telemetry_record(2, action="start_pdb_session", prior_obs=None, rendered_sha=None),
        _telemetry_record(3, action="continue_pdb_session", prior_obs="obs-1"),
    ]
    legacy = compute_gate_b_legacy(telemetry)
    assert legacy["passed"] is True
    assert legacy["accepted_pdb_count"] == 2
