"""BugsInPy manifest, preflight, and external-workspace adapter."""

from agentic_debugger.bugsinpy.adapter import (
    BugsInPyAdapter,
    BugsInPyManifest,
    AcquiredSourceReceipt,
    ExternalWorkspace,
    GateName,
    GateStatus,
    GitSourceAcquirer,
    ManifestValidationError,
    NoModelSmokeRunner,
    PreflightFacts,
    PreflightReport,
    TaskMappingError,
    normalize_pytest_commands,
)
from agentic_debugger.bugsinpy.metadata_preflight import (
    BugsInPyMetadataPreflight,
    BugsInPyOperation,
    BugsInPyOperationPermit,
    MetadataPreflightDecision,
    PreflightAuthorizationError,
    PreflightReasonCode,
)
from agentic_debugger.evaluation.task_schema import TaskSource
from agentic_debugger.runtime.execution import (
    ContainmentGuarantee,
    DependencyPreparation,
    PdbLaunchPlan,
    PreparedEnvironment,
    VerifiedExecutionContext,
)

__all__ = [
    "BugsInPyAdapter",
    "BugsInPyManifest",
    "AcquiredSourceReceipt",
    "ExternalWorkspace",
    "GateName",
    "GateStatus",
    "GitSourceAcquirer",
    "ManifestValidationError",
    "NoModelSmokeRunner",
    "PreflightFacts",
    "PreflightReport",
    "TaskMappingError",
   "normalize_pytest_commands",
    "ContainmentGuarantee",
    "DependencyPreparation",
    "PdbLaunchPlan",
    "PreparedEnvironment",
    "TaskSource",
    "VerifiedExecutionContext",
    "BugsInPyMetadataPreflight",
    "BugsInPyOperation",
    "BugsInPyOperationPermit",
    "MetadataPreflightDecision",
    "PreflightAuthorizationError",
    "PreflightReasonCode",
]
