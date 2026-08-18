from pathlib import Path

from agentic_debugger.swerebench.authority import (
    EXPECTED_CD_CLEAN_LE32K,
    EXPECTED_VALIDATION_REPOS,
    EXPERIMENT_SEED,
    HISTORICAL_GPT_OSS_PRODUCT_TASK,
)
from agentic_debugger.swerebench.hashing import sha256_canonical_json
from agentic_debugger.swerebench.population import load_clean_validation_population
from agentic_debugger.swerebench.selection import (
    assignment_key,
    select_repo_diverse_ordering,
)


def test_canonical_clean_validation_population_matches_b15_mask():
    population = load_clean_validation_population()
    assert population.counts["cd_clean_and_le32k"] == EXPECTED_CD_CLEAN_LE32K
    assert len(population.instance_ids) == EXPECTED_CD_CLEAN_LE32K
    assert population.counts["canonical_validation"] == 150
    assert population.counts["canonical_validation_repos"] == EXPECTED_VALIDATION_REPOS
    assert HISTORICAL_GPT_OSS_PRODUCT_TASK not in population.instance_ids
    assert all("__" in item for item in population.instance_ids)
    rebuilt = {
        "authority": "validation_cd_clean_le32k_mask.json",
        "instance_ids": list(population.instance_ids),
        "source_hashes": dict(population.source_hashes),
    }
    assert sha256_canonical_json(rebuilt) == population.population_sha256


def test_population_is_subset_of_validation_and_repo_disjoint_split():
    population = load_clean_validation_population()
    assert all(task.split == "validation" for task in population.tasks)
    assert len(population.repos()) >= 10
    assert "train" not in {task.split for task in population.tasks}


def test_deterministic_full_order_and_pilot10_are_reproducible():
    population = load_clean_validation_population()
    first = select_repo_diverse_ordering(population)
    second = select_repo_diverse_ordering(population, seed=EXPERIMENT_SEED)
    assert [item.instance_id for item in first.entries] == [
        item.instance_id for item in second.entries
    ]
    assert len(first.pilot10) == 10
    assert len({item.repo_canonical for item in first.pilot10}) == 10
    assert [item.order_index for item in first.pilot10] == list(range(1, 11))
    assert first.entries[0].instance_id == first.pilot10[0].instance_id
    keys = [assignment_key(item.instance_id, item.repo_canonical, seed=EXPERIMENT_SEED) for item in first.pilot10]
    assert keys == sorted(keys)


def test_later_expansion_continues_the_same_order():
    population = load_clean_validation_population()
    ordering = select_repo_diverse_ordering(population)
    assert len(ordering.entries) == EXPECTED_CD_CLEAN_LE32K
    assert [item.instance_id for item in ordering.entries[:10]] == [
        item.instance_id for item in ordering.pilot10
    ]
    assert [item.instance_id for item in ordering.entries[:30]][:10] == [
        item.instance_id for item in ordering.pilot10
    ]
