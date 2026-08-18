"""Deterministic repo-diverse ordering over the clean validation population."""

from __future__ import annotations

from dataclasses import dataclass

from agentic_debugger.swerebench.authority import (
    EXPERIMENT_SEED,
    SELECTION_ALGORITHM_ID,
)
from agentic_debugger.swerebench.hashing import sha256_text
from agentic_debugger.swerebench.population import (
    CleanValidationPopulation,
    ValidationTaskMeta,
)


def assignment_key(instance_id: str, repo_canonical: str, *, seed: str) -> str:
    """Stable SHA-256 over the frozen seed plus immutable identity metadata."""

    payload = f"{seed}\n{instance_id}\n{repo_canonical}\n"
    return sha256_text(payload)


@dataclass(frozen=True)
class OrderedTask:
    order_index: int
    instance_id: str
    repo: str
    repo_canonical: str
    base_commit: str
    assignment_key: str
    first_repo_occurrence: bool
    license: str
    difficulty: str
    age_bin: str
    patch_bin: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "order_index": self.order_index,
            "instance_id": self.instance_id,
            "repo": self.repo,
            "repo_canonical": self.repo_canonical,
            "base_commit": self.base_commit,
            "assignment_key": self.assignment_key,
            "first_repo_occurrence": self.first_repo_occurrence,
            "license": self.license,
            "difficulty": self.difficulty,
            "age_bin": self.age_bin,
            "patch_bin": self.patch_bin,
        }


@dataclass(frozen=True)
class DeterministicOrdering:
    seed: str
    algorithm: str
    population_sha256: str
    entries: tuple[OrderedTask, ...]

    @property
    def pilot10(self) -> tuple[OrderedTask, ...]:
        return self.entries[:10]

    def to_mapping(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "algorithm": self.algorithm,
            "population_sha256": self.population_sha256,
            "n": len(self.entries),
            "entries": [entry.to_mapping() for entry in self.entries],
        }


def select_repo_diverse_ordering(
    population: CleanValidationPopulation,
    *,
    seed: str = EXPERIMENT_SEED,
    algorithm: str = SELECTION_ALGORITHM_ID,
) -> DeterministicOrdering:
    """Order the entire eligible population; Pilot-10 is the first 10 rows.

    1. Score every eligible task with SHA-256(seed, instance_id, repo).
    2. Sort by that key, then instance_id (tie-break only).
    3. Walk that sorted list and emit the first occurrence of each
       repository, preserving score order.
    4. Append the remaining same-repo tasks in the same score order.

    Selection uses only admissible metadata. It does not consult gold
    patches, outcomes, difficulty preference, or PDB friendliness.
    """

    if not population.tasks:
        raise ValueError("population is empty")
    scored: list[tuple[str, ValidationTaskMeta]] = []
    for task in population.tasks:
        key = assignment_key(task.instance_id, task.repo_canonical, seed=seed)
        scored.append((key, task))
    scored.sort(key=lambda item: (item[0], item[1].instance_id))

    seen_repos: set[str] = set()
    heads: list[tuple[str, ValidationTaskMeta]] = []
    rest: list[tuple[str, ValidationTaskMeta]] = []
    for key, task in scored:
        if task.repo_canonical not in seen_repos:
            seen_repos.add(task.repo_canonical)
            heads.append((key, task))
        else:
            rest.append((key, task))

    ordered = heads + rest
    entries: list[OrderedTask] = []
    head_ids = {task.instance_id for _, task in heads}
    for index, (key, task) in enumerate(ordered, start=1):
        entries.append(
            OrderedTask(
                order_index=index,
                instance_id=task.instance_id,
                repo=task.repo,
                repo_canonical=task.repo_canonical,
                base_commit=task.base_commit,
                assignment_key=key,
                first_repo_occurrence=task.instance_id in head_ids,
                license=task.license,
                difficulty=task.difficulty,
                age_bin=task.age_bin,
                patch_bin=task.patch_bin,
            )
        )
    if len({entry.instance_id for entry in entries[:10]}) != 10:
        raise ValueError("Pilot-10 is not 10 distinct instance ids")
    if len({entry.repo_canonical for entry in entries[:10]}) != 10:
        raise ValueError("Pilot-10 is not 10 distinct repositories")
    return DeterministicOrdering(
        seed=seed,
        algorithm=algorithm,
        population_sha256=population.population_sha256,
        entries=tuple(entries),
    )
