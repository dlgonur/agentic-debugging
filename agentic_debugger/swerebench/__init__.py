"""SWE-rebench V2 evaluation adapter for the accepted product path.

This package does not introduce a second controller, verifier, or agent.
It maps official SWE-rebench V2 instance metadata onto the existing
``DebugTask`` / ``EvaluationVerifier`` / configured-command contracts
while keeping gold patches, hidden tests, and oracle localization out of
model-facing context.
"""

from agentic_debugger.swerebench.authority import (
    B15_CONTRACT_DIR,
    CANONICAL_DATASET_ID,
    CANONICAL_DATASET_REVISION,
    CANONICAL_PARQUET_SHA256,
    CLEAN_LE32K_MASK_NAME,
    EXPERIMENT_ID,
    EXPERIMENT_SEED,
    SELECTION_ALGORITHM_ID,
)
from agentic_debugger.swerebench.isolation import (
    FORBIDDEN_MODEL_FIELD_NAMES,
    assert_model_facing_isolated,
    scan_mapping_for_leakage,
)
from agentic_debugger.swerebench.population import (
    CleanValidationPopulation,
    load_clean_validation_population,
)
from agentic_debugger.swerebench.schema import (
    PILOT_RESULT_SCHEMA_VERSION,
    validate_pilot_result,
)
from agentic_debugger.swerebench.selection import (
    DeterministicOrdering,
    select_repo_diverse_ordering,
)

__all__ = [
    "B15_CONTRACT_DIR",
    "CANONICAL_DATASET_ID",
    "CANONICAL_DATASET_REVISION",
    "CANONICAL_PARQUET_SHA256",
    "CLEAN_LE32K_MASK_NAME",
    "CleanValidationPopulation",
    "DeterministicOrdering",
    "EXPERIMENT_ID",
    "EXPERIMENT_SEED",
    "FORBIDDEN_MODEL_FIELD_NAMES",
    "PILOT_RESULT_SCHEMA_VERSION",
    "SELECTION_ALGORITHM_ID",
    "assert_model_facing_isolated",
    "load_clean_validation_population",
    "scan_mapping_for_leakage",
    "select_repo_diverse_ordering",
    "validate_pilot_result",
]
