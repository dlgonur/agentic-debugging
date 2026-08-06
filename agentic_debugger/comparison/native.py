"""Native agentic comparison mode over the existing demo runner path.

Native attempts reuse the accepted end-to-end execution path
(:func:`agentic_debugger.demo.runner.run_demo_case`): the real controller,
tool registry, event projection, replay, independent verifier, cleanup and
offline guard.  Nothing is duplicated.

Two native conditions are supported:

* ``agentic`` — the established controller run without RAG context;
* ``rag-assisted`` — the identical run with an explicit bounded
  :class:`RagContext`; the offline model records retrieval evidence on the
  case result while producing identical directives (same-patch parity), so
  RAG can change only retrieval/citation metrics, never the repair outcome.

This module also owns the deterministic demo-side helpers that are *not*
frozen demo assets:

* baseline failure-output capture (disposable workspace);
* the retrieval query (task/issue projection plus failure excerpt);
* synthetic generation artifacts labeled ``offline-deterministic-demo``
  (one verified correct patch and one deterministic non-repair patch, both
  run through the real parser, workspace and verifier — the verdict is
  never hand-authored);
* conversion of :class:`DemoCaseResult` records into normalized attempt
  records.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from agentic_debugger.comparison.import_schema import (
    SYNTHETIC_GENERATOR,
    GenerationArtifact,
    extraction_for_substring,
)
from agentic_debugger.comparison.metrics import attempt_facts, normalize_failure_category
from agentic_debugger.demo.catalog import DemoCatalogError, build_reference_patch, scenario_for
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.demo.runner import DemoCaseResult, run_demo_case
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.rag.corpus import CorpusError, project_failure_output, task_issue_projection
from agentic_debugger.rag.schema import MAX_QUERY_BYTES, RagInputError
from agentic_debugger.runtime.test_runner import TestRunner
from agentic_debugger.runtime.workspace import TaskWorkspace


class NativeError(RagInputError):
    """Raised when a native comparison attempt cannot be executed."""


#: Fixed policy for native comparison conditions: the deterministic
#: static-baseline path (no PDB worker processes) keeps the demo bounded.
NATIVE_POLICY = DemoPolicy.STATIC_BASELINE

#: Marker appended when the query must be bounded to the declared cap.
_QUERY_TRUNCATION_MARKER = "\n[query-truncated]\n"

#: Marker inside a synthetic non-repair patch (self-identifying).
NON_REPAIR_COMMENT = "# deterministic non-repair fixture (offline-deterministic-demo)"


def capture_failure_output(
    task: DebugTask,
    *,
    fixture_dir: str,
    workspace_parent: str,
) -> str:
    """Run the baseline reproduction in a disposable workspace and return the
    projected failure/traceback excerpt (deterministic, bounded).

    The canonical fixture is never modified: the reproduction runs on a
    disposable copy which is always cleaned.
    """

    if not os.path.isdir(fixture_dir):
        raise NativeError(f"fixture_dir is not a directory: {fixture_dir!r}")
    if not os.path.isdir(workspace_parent):
        raise NativeError(f"workspace_parent is not a directory: {workspace_parent!r}")
    workspace: Optional[TaskWorkspace] = None
    try:
        workspace = TaskWorkspace(fixture_dir, parent_dir=workspace_parent)
        runner = TestRunner(workspace)
        result = runner.run_reproduction(task)
        command = result.command_result
        combined = command.stdout or ""
        if command.stderr:
            combined = f"{combined}\n{command.stderr}"
        projected = project_failure_output(combined, workspace_root=workspace.root)
        if not projected and result.launch_error:
            raise NativeError("baseline reproduction could not be launched")
        return projected
    finally:
        if workspace is not None:
            workspace.cleanup()
            if os.path.exists(workspace.root):
                raise NativeError("failure-capture workspace root remains after cleanup")


def build_task_query(task: DebugTask, failure_text: str) -> str:
    """Deterministic retrieval query: issue projection plus failure excerpt.

    The query is bounded to :data:`MAX_QUERY_BYTES`; when the failure excerpt
    must be cut, the cut happens at a newline boundary and an explicit marker
    is appended (never silent truncation).
    """

    if type(failure_text) is not str:
        raise NativeError("failure_text must be a string")
    issue = task_issue_projection(task)
    query = f"{issue}\n-- failing test output --\n{failure_text}"
    encoded = query.encode("utf-8")
    if len(encoded) <= MAX_QUERY_BYTES:
        return query
    budget = MAX_QUERY_BYTES - len(_QUERY_TRUNCATION_MARKER.encode("utf-8"))
    cut = encoded[:budget]
    boundary = cut.rfind(b"\n")
    if boundary > 0:
        cut = cut[:boundary]
    return cut.decode("utf-8", errors="replace") + _QUERY_TRUNCATION_MARKER


def build_comment_append_patch(source_text: str, target_path: str) -> str:
    """A deterministic, self-identifying non-repair patch.

    Appends one comment line at end-of-file, which is always syntactically
    safe in Python.  The patch is a real unified diff; whether it repairs
    anything is decided by the real parser, workspace and verifier — never
    hand-authored here.
    """

    if type(source_text) is not str or not source_text:
        raise NativeError("source_text must be non-empty")
    if "\\" in target_path or target_path.startswith("/"):
        raise NativeError(f"target_path must be a relative POSIX path: {target_path!r}")
    lines = source_text.splitlines(keepends=True)
    patched = list(lines)
    if patched and not patched[-1].endswith("\n"):
        patched[-1] = patched[-1] + "\n"
    patched.append(f"{NON_REPAIR_COMMENT}\n")
    diff = "".join(
        difflib.unified_diff(
            lines,
            patched,
            fromfile=f"a/{target_path}",
            tofile=f"b/{target_path}",
            lineterm="\n",
        )
    )
    if not diff:
        raise NativeError("non-repair patch produced an empty diff")
    return diff


def reference_patch_for(repository_root: str, task_id: str) -> str:
    """Render the verified reference repair from the canonical fixture bytes."""

    fixture_dir = os.path.join(
        repository_root, "agentic_debugger", "datasets", "curated", task_id
    )
    try:
        scenario = scenario_for(task_id)
    except DemoCatalogError as exc:
        raise NativeError(str(exc)) from exc
    source_path = os.path.join(fixture_dir, scenario.reference_repair.target_path)
    try:
        with open(source_path, "r", encoding="utf-8") as handle:
            source_text = handle.read()
    except OSError as exc:
        raise NativeError(f"cannot read fixture source: {exc}") from exc
    return build_reference_patch(source_text, scenario.reference_repair)


def synthetic_demo_artifact(
    *,
    experiment_id: str,
    attempt_id: str,
    condition_id: str,
    task_id: str,
    model_revision: str,
    adapter_identity: Optional[str],
    patch: str,
    generation_note: str,
    cost: Optional[float] = None,
    tokens: Optional[int] = None,
    memory_bytes: Optional[int] = None,
    raw_output: Optional[str] = None,
    raw_output_prefix: str = "Synthetic offline deterministic generation.",
) -> GenerationArtifact:
    """A synthetic, clearly labeled imported-generation artifact.

    The candidate patch is bound to the raw output through the strict
    substring extraction contract: the artifact embeds the patch text inside
    ``raw_output`` and records its exact byte offsets.  Provenance generator
    is always ``offline-deterministic-demo``; such artifacts are
    infrastructure evidence only and never imply real QLoRA evaluation.
    External generation telemetry is declared as verified zero (synthetic
    offline generation never contacts a provider).
    """

    if raw_output is None:
        raw_output = f"{raw_output_prefix}\nCandidate patch:\n{patch}"
    extraction = extraction_for_substring(raw_output, patch)
    return GenerationArtifact(
        schema_version="generation-artifact-v1",
        experiment_id=experiment_id,
        attempt_id=attempt_id,
        condition_id=condition_id,
        task_id=task_id,
        model_repository="offline-deterministic-demo",
        model_revision=model_revision,
        adapter_identity=adapter_identity,
        prompt_contract="generation-artifact-v1:offline-demo",
        generation_config={
            "synthetic": True,
            "temperature": 0.0,
            "deterministic": True,
        },
        raw_output=raw_output,
        patch_extraction=extraction,
        patch=patch,
        runtime_ms=None,
        memory_bytes=memory_bytes,
        cost=cost,
        tokens=tokens,
        external_provider_attempts=0,
        external_network_attempts=0,
        provenance={
            "generator": SYNTHETIC_GENERATOR,
            "note": generation_note,
        },
    )


def run_native_attempt(
    *,
    repository_root: str,
    task_id: str,
    condition_id: str,
    workspace_parent: str,
    response_text: str,
    rag_context: Any = None,
    role: str = "evaluation",
) -> Dict[str, Any]:
    """Run one native condition through the established demo runner path."""

    started = time.monotonic()
    case: DemoCaseResult = run_demo_case(
        repository_root=repository_root,
        task_id=task_id,
        policy=NATIVE_POLICY,
        workspace_parent=workspace_parent,
        rag_context=rag_context,
    )
    runtime_ms = int((time.monotonic() - started) * 1000)
    return native_case_to_attempt(
        case,
        condition_id=condition_id,
        response_text=response_text,
        runtime_ms=runtime_ms,
        role=role,
    )


def native_case_to_attempt(
    case: DemoCaseResult,
    *,
    condition_id: str,
    response_text: str,
    runtime_ms: Optional[int] = None,
    role: str = "evaluation",
) -> Dict[str, Any]:
    """Convert a demo case result into a normalized attempt record."""

    if not isinstance(case, DemoCaseResult):
        raise NativeError("native_case_to_attempt requires a DemoCaseResult")
    if role not in ("evaluation", "preference-fixture"):
        raise NativeError(f"unknown attempt role: {role!r}")
    mapping = case.to_mapping()
    verifier = mapping.get("verifier") or {}
    localization = mapping.get("localization") or {}
    offline = mapping.get("offline") or {}
    patch = mapping.get("patch") or {}
    trajectory = mapping.get("trajectory") or {}
    controller = mapping.get("controller") or {}
    controller_validation = mapping.get("controller_validation") or {}
    retrieval = mapping.get("retrieval")

    f2p_total = verifier.get("f2p_total")
    p2p_total = verifier.get("p2p_total")
    patch_applied = bool(verifier.get("patch_applied"))
    facts = {
        "generation_produced": True,
        "valid_patch": patch_applied,
        "patch_present": patch.get("sha256") is not None,
        "patch_applied": patch_applied,
        "syntax_passed": verifier.get("syntax_passed"),
        "verifier_outcome": verifier.get("outcome"),
        "verifier_status": verifier.get("status"),
        "f2p_passed": verifier.get("f2p_passed"),
        "f2p_total": f2p_total,
        "p2p_passed": verifier.get("p2p_passed"),
        "p2p_total": p2p_total,
    }
    failure_category = normalize_failure_category(facts)
    abort_reason = controller_validation.get("abort_reason")
    if failure_category is None and abort_reason == "failure_not_reproduced":
        failure_category = "NOT_REPRODUCED"

    response_sha256 = (
        hashlib.sha256(response_text.encode("utf-8")).hexdigest()
        if response_text
        else None
    )
    return {
        "attempt_id": f"{condition_id}:{case.task_id}",
        "condition_id": condition_id,
        "task_id": case.task_id,
        "mode": "native",
        "role": role,
        "source_identity": f"demo-case:{case.case_id}",
        "generation_produced": True,
        "valid_patch": patch_applied,
        "patch_sha256": patch.get("sha256"),
        "changed_file_count": len(verifier.get("patch_changed_files") or []),
        "correct_target_file": localization.get("claim_file_matches_oracle"),
        "localization_outcome": localization.get("outcome"),
        "f2p_passed": verifier.get("f2p_passed"),
        "f2p_total": f2p_total,
        "p2p_passed": verifier.get("p2p_passed"),
        "p2p_total": p2p_total,
        "verifier_outcome": verifier.get("outcome"),
        "verifier_status": verifier.get("status"),
        "failure_category": failure_category,
        "runtime_ms": runtime_ms,
        "memory_bytes": None,
        "cost": None,
        "tokens": None,
        "retrieval_count": (
            retrieval.get("chunk_count") if retrieval is not None else None
        ),
        "retrieval_bytes": (
            retrieval.get("selected_bytes") if retrieval is not None else None
        ),
        "retrieval_latency_ms": (
            retrieval.get("retrieval_latency_ms") if retrieval is not None else None
        ),
        "replay_valid": trajectory.get("replay_valid"),
        "cleanup_status": (
            "cleaned"
            if verifier.get("status") == "COMPLETED"
            and verifier.get("workspace_cleaned") is True
            else ("failed" if verifier.get("executed") else None)
        ),
        "canonical_fixture_unchanged": verifier.get("canonical_fixture_unchanged"),
        "provider_attempts": int(offline.get("provider_attempts") or 0),
        "network_attempts": int(offline.get("network_attempts") or 0),
        "external_provider_attempts": None,
        "external_network_attempts": None,
        "response_text": response_text,
        "response_sha256": response_sha256,
        "verifier_evidence": verifier if verifier.get("executed") else None,
        "provenance": {
            "source": "demo-runner",
            "generator": SYNTHETIC_GENERATOR,
            "note": (
                "native agentic condition over the accepted demo runner path; "
                "scripted offline model, infrastructure evidence only"
            ),
            "model_identity": "offline-deterministic-demo",
            "model_revision": None,
            "adapter_identity": None,
            "prompt_contract": "controller-snapshot:demo-task9-v1",
            "generation_config": {"synthetic": True, "policy": NATIVE_POLICY.value},
            "final_state": controller.get("final_state"),
            "stop_reason": controller.get("stop_reason"),
        },
    }


def check_native_parity(attempt_a: Dict[str, Any], attempt_b: Dict[str, Any]) -> None:
    """Fail-closed parity check for the two native conditions.

    The RAG-native condition may change only retrieval metrics; the repair
    (patch digest, verifier outcome) must be identical, and exactly one of
    the two attempts may carry retrieval evidence.
    """

    for field in ("patch_sha256", "verifier_outcome", "valid_patch"):
        if attempt_a.get(field) != attempt_b.get(field):
            raise NativeError(
                f"native parity violated on {field}: "
                f"{attempt_a.get(field)!r} vs {attempt_b.get(field)!r}"
            )
    rag_enabled = (
        attempt_a.get("retrieval_count") is not None,
        attempt_b.get("retrieval_count") is not None,
    )
    if sum(1 for enabled in rag_enabled if enabled) != 1:
        raise NativeError(
            "native parity requires exactly one RAG-enabled condition"
        )


__all__ = [
    "NATIVE_POLICY",
    "NON_REPAIR_COMMENT",
    "NativeError",
    "capture_failure_output",
    "build_task_query",
    "build_comment_append_patch",
    "reference_patch_for",
    "synthetic_demo_artifact",
    "run_native_attempt",
    "native_case_to_attempt",
    "check_native_parity",
]
