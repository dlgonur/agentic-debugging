"""R5 contract unit tests: frozen five-task order, fixture tree hashes,
budgets, and the pre-registered common timeout derivation rule."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.debugger_interaction_v2_r5.launcher import fixture_tree_sha256
from experiments.debugger_interaction_v2_r5.r5_runner import (
    R5_BUDGETS,
    R5_TASKS,
    _contract_sha256,
    _load_contract,
    _validate_contract,
    derive_common_pdb_timeout,
)

CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"


class TestContract:
    def test_frozen_task_order(self):
        contract = _load_contract()
        assert [t["task_id"] for t in contract["tasks"]] == list(R5_TASKS)

    def test_fixture_tree_hashes_match_live_fixtures(self):
        contract = _load_contract()
        for entry in contract["tasks"]:
            task_id = entry["task_id"]
            assert entry["fixture_tree_sha256"] == fixture_tree_sha256(
                CURATED_ROOT / task_id
            ), f"tree hash drift for {task_id}"

    def test_common_budget_frozen(self):
        contract = _load_contract()
        budgets = contract["budgets"]
        for key, value in R5_BUDGETS.items():
            assert key in budgets
        assert budgets["controller_steps_max"] == R5_BUDGETS["controller_steps_max"]
        assert budgets["model_requests_max"] == R5_BUDGETS["model_requests_max"]

    def test_contract_validates(self):
        contract = _load_contract()
        from experiments.debugger_interaction_v2_r5.transport import (
            BASE_REPOSITORY,
            BASE_REVISION,
            GENERATION_CONFIG,
        )
        validation = _validate_contract(
            contract, repo=BASE_REPOSITORY, revision=BASE_REVISION,
            gen=GENERATION_CONFIG,
        )
        assert validation["validated"] is True
        assert len(validation["contract_sha256"]) == 64

    def test_timeout_derivation_rule(self):
        assert derive_common_pdb_timeout(1000) == 15  # floor
        assert derive_common_pdb_timeout(5000) == 15  # 15s = 3x margin
        assert derive_common_pdb_timeout(6000) == 20
        assert derive_common_pdb_timeout(8333) == 25
        assert derive_common_pdb_timeout(10000) == 30
        assert derive_common_pdb_timeout(40000) == 120
        with pytest.raises(ValueError):
            derive_common_pdb_timeout(0)

    def test_contract_sha_is_stable_for_identical_content(self):
        c1 = _load_contract()
        c2 = _load_contract()
        assert _contract_sha256(c1) == _contract_sha256(c2)


class TestCleanHoldoutAuthority:
    """R5.9 closeout: the explicit fail-closed clean-holdout aggregate.

    CLEAN 5/5 holds ONLY when every row passes its strict per-task gate AND
    the fail-closed actual-prompt anti-leakage audit is empty."""

    def test_clean_holdout_5_of_5_true_only_when_all_conditions_hold(self):
        from experiments.debugger_interaction_v2_r5.r5_runner import (
            _clean_holdout_5_of_5,
        )

        assert _clean_holdout_5_of_5(True, True, 0) is True
        # Any single violation fails the authority.
        assert _clean_holdout_5_of_5(False, True, 0) is False
        assert _clean_holdout_5_of_5(True, False, 0) is False
        assert _clean_holdout_5_of_5(True, True, 1) is False
        assert _clean_holdout_5_of_5(True, True, None) is False
        assert _clean_holdout_5_of_5(None, True, 0) is False
