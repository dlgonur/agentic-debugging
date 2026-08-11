#!/usr/bin/env python3
"""R4 — real-model generated regression test probe (buggy FAIL -> same-test fixed PASS).

Orchestrates the professor-requested capability experiment:

    agent-visible task statement (title + description, faithfully rendered)
    + buggy source
    -> frozen RAW Qwen2.5-Coder-7B-Instruct  (EXACTLY ONE generation call)
    -> T_raw -> T_parsed -> T_written (SHA-256 for all three)
    -> BUGGY workspace: buggy implementation + T_written   (structured FAIL)
    -> FIXED workspace: same fixture + accepted R_fix_C + exact same T_written
       (PASS)                                            (strictly separate)
    -> independent EvaluationVerifier(R_fix_C)            (authority)

The model-generated test is AUXILIARY evidence, never the correctness
authority. The independent ``EvaluationVerifier`` remains the final
correctness authority over the frozen F2P/P2P contract.

R4 identities (kept distinct from generated-test identities):
    T_raw     exact transport response bytes/text
    T_parsed  parsed model-authored Python test body
    T_written exact bytes written and executed
    R_fix_B   tracked frozen R3.1 model semantic repair
              (tests/fixtures/r31_model_patch_raw.patch)
    R_fix_C   deterministic metadata-only normalization of R_fix_B

Modes:
    --validate-only   Validate contract/identity without loading the model.
    --run-offline     Full pipeline driven by a deterministic FakeTransport
                      (no model, no GPU) for offline validation/tests.
    --run             Live run with LocalRawQwenTransport (GPU + authorization
                      required).

This runner is experiment-local and self-contained. It reuses the tracked R3
transport/adapter/serialization and production ``agentic_debugger`` modules
only; it never imports from untracked historical S1/S1-P directories
(``_check_import_boundaries`` enforces this).
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier

from experiments.debugger_interaction_v2_r3.adapter import (
    NOT_AVAILABLE,
    TransportError,
    TransportResponse,
)
from experiments.debugger_interaction_v2_r3.serialization import (
    normalize_hunk_counts,
)
from experiments.debugger_interaction_v2_r3.transport import (
    BASE_REPOSITORY,
    BASE_REVISION,
    GENERATION_CONFIG,
    FakeTransport,
    LocalRawQwenTransport,
)

from experiments.model_generated_test_probe_r4 import test_generation as tg
from experiments.model_generated_test_probe_r4.generated_test_runner import (
    run_buggy,
    run_fixed,
)

CONTRACT_PATH = THIS_FILE.with_name("r4_contract.json")
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"
TASK_ID = "curated-off-by-one-002"
FIXTURE_DIR = CURATED_ROOT / TASK_ID
TARGET_MODULE = "recent_window.py"

# Accepted frozen R3.1/R3.2 repair identities (amendment 4).
R_FIX_B_PATH = REPO_ROOT / "tests" / "fixtures" / "r31_model_patch_raw.patch"
R_FIX_B_SHA256 = "831b1c2bc347c9812296de5ddb7ebac5f6f414bbd6512561b4cb29066e6e2c76"
R_FIX_C_SHA256 = "8c051faa605d9cf736540301e204639870408b288ab1ceb8348845afc674b990"
R_FIX_FINGERPRINT_SHA256 = (
    "002fc5ca376c48ffc035b3b0b73ef0bb6735713ef9cff78603fe879e5703fb34"
)

# Untracked historical S1/S1-P directory prefixes that the R4 candidate must
# never import through (amendment 10).
_FORBIDDEN_IMPORT_PREFIXES = (
    "experiments.debugger_interaction_v2.",
    "experiments.model_generated_test_probe.",
)
_FORBIDDEN_IMPORT_RE = re.compile(
    r"(?:from|import)\s+experiments\.("
    r"debugger_interaction_v2(?!_)|model_generated_test_probe(?!_))"
)


# ---------------------------------------------------------------------------
# Contract / identity
# ---------------------------------------------------------------------------


def _load_contract() -> dict[str, Any]:
    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if value.get("schema_version") != "model-generated-test-probe-r4-v1":
        raise RuntimeError("unsupported R4 experiment contract")
    return value


_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _git_head(repository_root: Path) -> Optional[str]:
    """Return the current ``git rev-parse HEAD`` SHA, or None (read-only)."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repository_root),
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    sha = completed.stdout.strip()
    return sha if _COMMIT_SHA_RE.match(sha) else None


def _contract_sha256(contract: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _fixture_tree_sha256(task_dir: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        p for p in task_dir.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    for p in files:
        rel = p.relative_to(task_dir).as_posix()
        digest.update(rel.encode("utf-8")); digest.update(b"\0")
        digest.update(p.read_bytes()); digest.update(b"\0")
    return digest.hexdigest()


def _r3_fix_identity() -> dict[str, Any]:
    """Derive and verify the accepted R_fix_B -> R_fix_C identities.

    Fail-closed: any drift from the frozen accepted R3 identities raises.
    """

    b = R_FIX_B_PATH.read_text(encoding="utf-8")
    sha_b = tg._sha256(b)
    c, record = normalize_hunk_counts(b)
    sha_c = tg._sha256(c)
    fingerprint = record.semantic_body_fingerprint_normalized

    if sha_b != R_FIX_B_SHA256:
        raise RuntimeError(
            f"R_fix_B identity drift: expected {R_FIX_B_SHA256}, got {sha_b}"
        )
    if sha_c != R_FIX_C_SHA256:
        raise RuntimeError(
            f"R_fix_C identity drift: expected {R_FIX_C_SHA256}, got {sha_c}"
        )
    if not record.fingerprint_equal:
        raise RuntimeError("R_fix normalization fingerprint mismatch")
    if fingerprint != R_FIX_FINGERPRINT_SHA256:
        raise RuntimeError(
            f"R_fix fingerprint drift: expected {R_FIX_FINGERPRINT_SHA256}, "
            f"got {fingerprint}"
        )
    return {
        "r_fix_b_sha256": sha_b,
        "r_fix_b_provenance": (
            "tracked tests/fixtures/r31_model_patch_raw.patch — frozen R3.1 "
            "live model semantic repair (debugger-informed), SHA 5148e97c..."
            " raw response"
        ),
        "r_fix_c_sha256": sha_c,
        "r_fix_c_provenance": (
            "deterministic metadata-only hunk-count normalization of R_fix_B "
            "via experiments/debugger_interaction_v2_r3/serialization.py; "
            "accepted R3.2 verifier input"
        ),
        "semantic_body_fingerprint": fingerprint,
        "fingerprint_equal": record.fingerprint_equal,
        "header_fields_changed": record.header_fields_changed,
    }


def _check_import_boundaries() -> dict[str, Any]:
    """Prove the R4 candidate does not import through untracked S1/S1-P dirs.

    Static scan: no R4 package source line imports
    ``experiments.debugger_interaction_v2.`` (bare S1) or
    ``experiments.model_generated_test_probe.`` (bare S1-P).
    Runtime check: the reused R3 transport/adapter/serialization modules
    resolve to the tracked ``experiments/debugger_interaction_v2_r3/`` path.
    """

    package_dir = THIS_FILE.parent
    offenders: list[str] = []
    for path in sorted(package_dir.rglob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if _FORBIDDEN_IMPORT_RE.search(line):
                offenders.append(f"{path.name}: {line.strip()}")
    if offenders:
        return {
            "passed": False,
            "reason": "forbidden historical imports found",
            "offenders": offenders,
        }

    runtime = {}
    for module in (
        "experiments.debugger_interaction_v2_r3.transport",
        "experiments.debugger_interaction_v2_r3.adapter",
        "experiments.debugger_interaction_v2_r3.serialization",
    ):
        imported = __import__(module, fromlist=["*"])
        file_path = Path(inspect.getfile(imported)).resolve()
        runtime[module] = str(file_path)
        if "debugger_interaction_v2_r3" not in file_path.parts:
            return {
                "passed": False,
                "reason": f"runtime import resolved outside tracked r3 path: {module}",
                "runtime": runtime,
            }
    return {
        "passed": True,
        "reason": "no imports resolve through untracked S1/S1-P directories",
        "static_offenders": offenders,
        "runtime_modules": runtime,
    }


def _check_anti_leakage(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Check the FINAL rendered live prompt (amendment 9), not helper inputs."""

    forbidden = {
        "test_full_length_window_includes_every_value": "fixture f2p node name",
        "test_smaller_window_returns_recent_values": "fixture p2p node name",
        "test_zero_window_is_empty": "fixture p2p node name",
        "test_recent_window.py": "fixture test source name",
        "root_cause_summary": "oracle field",
        "runtime_evidence_hint": "oracle field",
        "target_symbols": "oracle field",
        "inspect_expressions": "runtime probe expressions",
        "The calculated loop indexes omit": "oracle root-cause text",
        "831b1c2b": "R_fix_B hash fragment",
        "8c051faa": "R_fix_C hash fragment",
        "002fc5ca": "R_fix fingerprint fragment",
        "R_fix": "repair identity label",
        "normalize_hunk_counts": "normalizer identity",
        "model_patch_serialization_normalized": "normalizer artifact",
        "--- a/recent_window.py": "patch serialization",
        "+++ b/recent_window.py": "patch serialization",
        "diff --git": "git metadata patch header",
        "reference_repair": "reference repair field",
        "fixed_revision": "fixed revision field",
        "gold": "gold reference label",
        "oracle": "oracle label",
    }
    combined = system_prompt + "\n" + user_prompt
    found: list[dict[str, str]] = []
    for fragment, label in forbidden.items():
        if fragment.lower() in combined.lower():
            found.append({"fragment": fragment, "label": label})
    return {
        "checked": True,
        "passed": not found,
        "forbidden_found": found,
        "checked_prompt": "final rendered system+user prompt",
    }


def _validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate frozen contract: model identity, task, hashes, budgets."""

    model = contract.get("model", {})
    if model.get("base_repository") != BASE_REPOSITORY:
        raise RuntimeError("model base_repository drift")
    if model.get("base_revision") != BASE_REVISION:
        raise RuntimeError("model base_revision drift")
    if model.get("adapter_applied") is not False:
        raise RuntimeError("probe must be RAW base only (adapter_applied=false)")
    if model.get("rag_enabled") is not False:
        raise RuntimeError("probe must have RAG OFF (rag_enabled=false)")

    gen = model.get("generation", {})
    if gen.get("do_sample") is not False:
        raise RuntimeError("generation.do_sample must be False")
    if gen.get("max_new_tokens") != 1024:
        raise RuntimeError("generation.max_new_tokens must be 1024 (frozen)")
    if gen.get("max_input_tokens") != 32768:
        raise RuntimeError("generation.max_input_tokens must be 32768 (frozen)")

    task = contract.get("task", {})
    if task.get("task_id") != TASK_ID:
        raise RuntimeError(f"task must be {TASK_ID!r}")

    # Frozen text-asset hashes vs the live module.
    spec_sha = tg._sha256(tg.render_task_spec_section(load_task(str(FIXTURE_DIR / "task.json"))))
    if contract.get("spec_section", {}).get("sha256") != spec_sha:
        raise RuntimeError("spec_section sha256 drift between contract and module")
    prompts = contract.get("prompts", {})
    if prompts.get("system_prompt_generation_sha256") != tg.SYSTEM_PROMPT_GENERATION_SHA256:
        raise RuntimeError("system_prompt_generation sha256 drift")

    # Fixture tree hash stability.
    fixture_hash = _fixture_tree_sha256(FIXTURE_DIR)
    frozen_hash = task.get("fixture_tree_sha256")
    if frozen_hash and fixture_hash != frozen_hash:
        raise RuntimeError(
            f"fixture tree hash drift: expected {frozen_hash}, got {fixture_hash}"
        )

    # R_fix_B -> R_fix_C identity (fail-closed before any fixed-side use).
    r3_fix = _r3_fix_identity()

    # Import boundaries.
    boundaries = _check_import_boundaries()
    if not boundaries.get("passed"):
        raise RuntimeError(f"import boundary violation: {boundaries}")

    return {
        "contract_sha256": _contract_sha256(contract),
        "fixture_tree_sha256": fixture_hash,
        "spec_section_sha256": spec_sha,
        "system_prompt_generation_sha256": tg.SYSTEM_PROMPT_GENERATION_SHA256,
        "r3_fix_identity": r3_fix,
        "import_boundaries": boundaries,
        "validated": True,
    }


def _run_identity(contract: dict[str, Any], runtime_python: str) -> dict[str, Any]:
    model = contract["model"]
    return {
        "schema_version": "model-generated-test-probe-r4-v1-identity",
        "experiment_id": "model-generated-test-probe-r4",
        "source_commit_sha": _git_head(REPO_ROOT),
        "experiment_contract_sha256": _contract_sha256(contract),
        "task_id": TASK_ID,
        "model_condition": "RAW_BASE",
        "adapter_applied": False,
        "base_repository": model["base_repository"],
        "base_revision": model["base_revision"],
        "rag_enabled": False,
        "generation": model["generation"],
        "spec_section_sha256": tg._sha256(
            tg.render_task_spec_section(load_task(str(FIXTURE_DIR / "task.json")))
        ),
        "system_prompt_generation_sha256": tg.SYSTEM_PROMPT_GENERATION_SHA256,
        "runtime_python": runtime_python,
        "candidate_source_manifest": _candidate_source_manifest(),
    }


def _candidate_source_manifest() -> dict[str, str]:
    relative_paths = (
        "experiments/model_generated_test_probe_r4/probe.py",
        "experiments/model_generated_test_probe_r4/test_generation.py",
        "experiments/model_generated_test_probe_r4/generated_test_runner.py",
        "experiments/model_generated_test_probe_r4/r4_contract.json",
        "experiments/debugger_interaction_v2_r3/transport.py",
        "experiments/debugger_interaction_v2_r3/adapter.py",
        "experiments/debugger_interaction_v2_r3/serialization.py",
        "tests/fixtures/r31_model_patch_raw.patch",
        "agentic_debugger/evaluation/runner.py",
        "agentic_debugger/evaluation/verifier.py",
        "agentic_debugger/evaluation/task_schema.py",
        "agentic_debugger/evaluation/outcome_taxonomy.py",
        "agentic_debugger/runtime/command_runner.py",
        "agentic_debugger/runtime/execution.py",
        "agentic_debugger/runtime/exceptions.py",
        "agentic_debugger/runtime/patcher.py",
        "agentic_debugger/runtime/test_runner.py",
        "agentic_debugger/runtime/workspace.py",
    )
    manifest: dict[str, str] = {}
    for relative in relative_paths:
        path = REPO_ROOT / relative
        if not path.is_file():
            raise RuntimeError(f"candidate source manifest file missing: {relative}")
        manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(manifest.items()))


# ---------------------------------------------------------------------------
# Verifier serialization
# ---------------------------------------------------------------------------


def _status_value(v: Any) -> Any:
    if hasattr(v, "value"):
        return v.value
    return str(v) if v is not None else None


def _serialize_verifier(evaluation: Any) -> dict[str, Any]:
    """Serialize the EvaluationVerifier result to a JSON-safe dict."""

    workspace = getattr(evaluation, "workspace", None)
    return {
        "executed": True,
        "status": _status_value(getattr(evaluation, "status", None)),
        "stop_reason": getattr(evaluation, "stop_reason", None),
        "outcome": _status_value(getattr(evaluation, "outcome", None)),
        "f2p_total": getattr(evaluation, "f2p_total", None),
        "f2p_passed": getattr(evaluation, "f2p_passed", None),
        "p2p_total": getattr(evaluation, "p2p_total", None),
        "p2p_passed": getattr(evaluation, "p2p_passed", None),
        "timeout": getattr(evaluation, "timeout", None),
        "diagnostic": getattr(evaluation, "diagnostic", None),
        "f2p_records": [
            _status_value(getattr(t, "status", None))
            for t in getattr(evaluation, "post_patch_f2p", []) or []
        ],
        "p2p_records": [
            _status_value(getattr(t, "status", None))
            for t in getattr(evaluation, "post_patch_p2p", []) or []
        ],
        "workspace_lifecycle": _status_value(getattr(workspace, "lifecycle", None)),
        "canonical_fixture_unchanged": bool(
            getattr(workspace, "canonical_fixture_unchanged", False)
        ),
        "workspace_cleaned": bool(getattr(workspace, "cleaned", False)),
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run_probe(
    contract: dict[str, Any],
    transport: Any,
    *,
    model_name: str,
    output_dir: Path,
    case_parent: Optional[Path] = None,
) -> dict[str, Any]:
    """Run the full R4 pipeline with a given transport.

    Returns the evidence dict (also written to ``evidence.json``).
    """

    task = load_task(str(FIXTURE_DIR / "task.json"))
    budgets = contract.get("budgets", {})
    test_timeout = int(budgets.get("test_timeout_seconds", 20))
    req_timeout = float(budgets.get("model_request_timeout_seconds", 60.0))

    # Disposable workspaces nest under a per-run case dir in SYSTEM TEMP
    # (S1-P precedent): keeps pytest rootdir at the workspace so collected
    # node ids are the expected relative "tests/..." ids.
    if case_parent is None:
        case_parent = Path(tempfile.mkdtemp(prefix="r4-probe-"))
    case_dir = case_parent.resolve() / f"case-{TASK_ID}"
    case_dir.mkdir(parents=True, exist_ok=True)
    fixture_dir_abs = FIXTURE_DIR.resolve()

    # Fail-closed R_fix identity BEFORE any live comparison.
    r3_fix_identity = _r3_fix_identity()
    r_fix_c = _derive_r_fix_c()

    evidence: dict[str, Any] = {
        "schema_version": "model-generated-test-probe-r4-v1",
        "run_identity": _run_identity(contract, runtime_python=_runtime_python()),
        "model_facing_prompt": None,
        "anti_leakage": None,
        "test_generation": None,
        "framing": None,
        "r3_fix_identity": r3_fix_identity,
        "buggy_run": None,
        "fixed_run": None,
        "verifier": {"executed": False},
        "summary": None,
        "first_causal_boundary": None,
        "claims_boundary": contract.get("claims_boundary"),
    }

    # -- 1. Single-attempt test generation (attempt 0 only) ---------------
    gen_outcome = tg.generate_frozen_test(
        transport, task, fixture_dir_abs, case_dir,
        model_name=model_name,
        request_timeout_seconds=req_timeout,
        test_timeout_seconds=test_timeout,
    )

    spec_section = tg.render_task_spec_section(task)
    user_prompt = tg.build_generation_user_prompt(
        task, (fixture_dir_abs / TARGET_MODULE).read_text(encoding="utf-8")
    )
    anti_leakage = _check_anti_leakage(tg.SYSTEM_PROMPT_GENERATION, user_prompt)
    evidence["model_facing_prompt"] = {
        "system_prompt": tg.SYSTEM_PROMPT_GENERATION,
        "system_prompt_sha256": tg.SYSTEM_PROMPT_GENERATION_SHA256,
        "user_prompt": user_prompt,
        "user_prompt_sha256": tg._sha256(user_prompt),
        "spec_section": spec_section,
        "spec_section_sha256": tg._sha256(spec_section),
        "agent_visible_mapping_used": {
            "title": task.agent_visible_mapping().get("title"),
            "description": task.agent_visible_mapping().get("description"),
        },
    }
    evidence["anti_leakage"] = anti_leakage

    evidence["test_generation"] = {
        "stop_reason": gen_outcome.stop_reason,
        "attempts": gen_outcome.attempts,
        "frozen_test": None,
    }
    frozen = gen_outcome.frozen_test

    if not anti_leakage.get("passed"):
        evidence["first_causal_boundary"] = {
            "stage": "anti_leakage_failed",
            "detail": anti_leakage.get("forbidden_found"),
        }
        evidence["summary"] = _summary(
            generated_test_froze=False,
            stop_reason="open_anti_leakage_failed",
        )
        _write_evidence(evidence, output_dir)
        return evidence

    if frozen is None:
        attempt = gen_outcome.attempts[0] if gen_outcome.attempts else {}
        evidence["first_causal_boundary"] = {
            "stage": gen_outcome.stop_reason,
            "detail": {
                "extraction": attempt.get("extraction"),
                "executability": attempt.get("executability"),
                "transport_error_category": attempt.get("transport_error_category"),
            },
        }
        evidence["summary"] = _summary(
            generated_test_froze=False,
            stop_reason=f"open_{gen_outcome.stop_reason}",
        )
        _write_evidence(evidence, output_dir)
        return evidence

    evidence["test_generation"]["frozen_test"] = {
        "source": frozen.source,
        "sha256": frozen.sha256,
        "attempt_index": frozen.attempt_index,
        "raw_response_sha256": frozen.raw_response_sha256,
        "user_prompt_sha256": frozen.user_prompt_sha256,
        "usage": frozen.usage,
        "executability": frozen.executability,
    }

    # -- 2. Framing identities: T_raw / T_parsed / T_written ---------------
    # T_written == T_parsed byte-for-byte (binary write, no newline
    # translation); T_parsed == deterministic fence-stripped T_raw body.
    evidence["framing"] = {
        "t_raw_sha256": frozen.raw_response_sha256,
        "t_parsed_sha256": frozen.sha256,
        "t_written_sha256": tg._sha256(frozen.source),
        "relation": (
            "T_parsed = deterministic fence-strip + terminal-newline "
            "normalization of T_raw; T_written bytes == T_parsed bytes "
            "(binary write, no newline translation)."
        ),
    }

    # -- 3. BUGGY workspace (structured FAIL gate) -------------------------
    buggy_result = run_buggy(
        frozen, fixture_dir_abs, case_dir, timeout_seconds=test_timeout,
    )
    evidence["buggy_run"] = _run_result_to_dict(buggy_result)

    if not buggy_result.valid_buggy_failure:
        evidence["first_causal_boundary"] = {
            "stage": "buggy_not_valid_failure",
            "detail": {
                "status": buggy_result.status,
                "reason": buggy_result.reason,
                "compiled": buggy_result.compiled,
                "collected": buggy_result.collected,
                "collect_error": buggy_result.collect_error,
                "counts": buggy_result.counts,
                "infrastructure_markers": buggy_result.infrastructure_markers,
                "assertion_attributed": buggy_result.assertion_attributed,
                "timed_out": buggy_result.timed_out,
                "launch_error": buggy_result.launch_error,
            },
        }
        evidence["summary"] = _summary(
            generated_test_froze=True,
            generated_test_did_not_encode_defect=(
                buggy_result.status == "PASS"
            ),
            stop_reason="open_buggy_not_valid_failure",
        )
        _write_evidence(evidence, output_dir)
        return evidence

    # -- 4. FIXED workspace: same fixture + R_fix_C + exact same T_written --
    fixed_result = run_fixed(
        frozen, r_fix_c, fixture_dir_abs, case_dir,
        timeout_seconds=test_timeout,
    )
    evidence["fixed_run"] = _run_result_to_dict(fixed_result)

    if fixed_result.status != "PASS" or not fixed_result.executed:
        evidence["first_causal_boundary"] = {
            "stage": "fixed_not_pass",
            "detail": {
                "status": fixed_result.status,
                "reason": fixed_result.reason,
                "patch_applied": fixed_result.patch_applied,
                "patch_error": fixed_result.patch_error,
                "counts": fixed_result.counts,
            },
        }
        evidence["summary"] = _summary(
            generated_test_froze=True,
            stop_reason="open_fixed_not_pass",
        )
        _write_evidence(evidence, output_dir)
        return evidence

    # -- 5. Independent verifier (separate; authority) on exact R_fix_C ----
    verifier_result: dict[str, Any] = {"executed": False}
    try:
        evaluation = EvaluationVerifier(
            str(REPO_ROOT), workspace_parent=str(case_dir)
        ).evaluate(task, r_fix_c)
        verifier_result = _serialize_verifier(evaluation)
    except Exception as exc:  # noqa: BLE001 — record, do not crash
        verifier_result = {
            "executed": True, "error": f"{type(exc).__name__}: {exc}",
        }
    evidence["verifier"] = verifier_result

    verifier_executed = bool(verifier_result.get("executed"))
    verifier_resolved = (
        verifier_executed and verifier_result.get("outcome") == "RESOLVED"
    )
    if not verifier_resolved:
        evidence["first_causal_boundary"] = {
            "stage": "verifier_not_resolved",
            "detail": verifier_result,
        }

    # -- 6. Fail-closed R4 PASS gate ---------------------------------------
    same_test_identity = bool(
        evidence["framing"]["t_written_sha256"]
        == buggy_result.written_test_sha256
        == fixed_result.written_test_sha256
    )
    r4_pass = bool(
        same_test_identity
        and buggy_result.valid_buggy_failure
        and fixed_result.status == "PASS"
        and fixed_result.executed
        and fixed_result.patch_applied
        and fixed_result.patch_error is None
        and verifier_resolved
        and verifier_result.get("status") == "COMPLETED"
        and verifier_result.get("f2p_passed") == 1
        and verifier_result.get("f2p_total") == 1
        and verifier_result.get("p2p_passed") == 2
        and verifier_result.get("p2p_total") == 2
        and verifier_result.get("workspace_lifecycle") == "CLEANED"
        and verifier_result.get("canonical_fixture_unchanged") is True
    )

    evidence["summary"] = _summary(
        generated_test_froze=True,
        generated_test_did_not_encode_defect=False,
        buggy_failed_frozen_test=True,
        fixed_code_passed_frozen_test=True,
        verifier_executed=verifier_executed,
        verifier_resolved=verifier_resolved,
        r4_pass=r4_pass,
        same_test_identity=same_test_identity,
        stop_reason="completed" if r4_pass else "open_verifier_not_resolved",
    )

    _write_evidence(evidence, output_dir)
    return evidence


def _derive_r_fix_c() -> str:
    b = R_FIX_B_PATH.read_text(encoding="utf-8")
    c, _record = normalize_hunk_counts(b)
    return c


def _runtime_python() -> str:
    return sys.version.split()[0]


def _run_result_to_dict(r: Any) -> dict[str, Any]:
    return {
        "label": r.label,
        "frozen_test_sha256": r.frozen_test_sha256,
        "written_test_sha256": r.written_test_sha256,
        "executed": r.executed,
        "status": r.status,
        "exit_code": r.exit_code,
        "counts": r.counts,
        "reason": r.reason,
        "timed_out": r.timed_out,
        "launch_error": r.launch_error,
        "patch_applied": r.patch_applied,
        "patch_error": r.patch_error,
        "compiled": r.compiled,
        "compile_error": r.compile_error,
        "collected": r.collected,
        "collect_error": r.collect_error,
        "infrastructure_markers": r.infrastructure_markers,
        "assertion_attributed": r.assertion_attributed,
        "valid_buggy_failure": r.valid_buggy_failure,
        "workspace_cleaned": r.workspace_cleaned,
        "canonical_tree_hash_before": r.canonical_tree_hash_before,
        "canonical_tree_hash_after": r.canonical_tree_hash_after,
        "stdout": r.stdout_bounded,
        "stderr": r.stderr_bounded,
    }


def _summary(
    *,
    generated_test_froze: bool,
    generated_test_did_not_encode_defect: bool = False,
    buggy_failed_frozen_test: bool = False,
    fixed_code_passed_frozen_test: bool = False,
    verifier_executed: bool = False,
    verifier_resolved: bool = False,
    r4_pass: bool = False,
    same_test_identity: bool = False,
    stop_reason: str,
) -> dict[str, Any]:
    return {
        "stop_reason": stop_reason,
        "r4_pass": r4_pass,
        "generated_test_froze": generated_test_froze,
        "generated_test_did_not_encode_defect": generated_test_did_not_encode_defect,
        "buggy_failed_frozen_test": buggy_failed_frozen_test,
        "fixed_code_passed_frozen_test": fixed_code_passed_frozen_test,
        "same_test_identity": same_test_identity,
        "verifier_executed": verifier_executed,
        "verifier_resolved": verifier_resolved,
        "verifier_is_correctness_authority": True,
        "generated_test_is_auxiliary_evidence": True,
    }


def _write_evidence(evidence: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "evidence.json"
    path.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Offline FakeTransport scenario
# ---------------------------------------------------------------------------


# A deterministic generated test that encodes the boundary behavior stated in
# the agent-visible task (full window == all values) and FAILS the buggy code
# (which drops the final value at the boundary) and PASSES R_fix_C. Exactly
# ONE test function: the executability gate requires exactly one executed
# test node.
_OFFLINE_GENERATED_TEST = (
    "from recent_window import recent_window\n"
    "\n"
    "\n"
    "def test_full_window_returns_all_values() -> None:\n"
    "    values = [10, 20, 30, 40]\n"
    "    assert recent_window(values, len(values)) == values\n"
)


def _build_offline_transport() -> FakeTransport:
    """A FakeTransport yielding exactly one generated-test response."""

    gen_response = (
        "```python\n" + _OFFLINE_GENERATED_TEST + "```\n"
    )
    return FakeTransport((gen_response,))


def run_offline(output_dir: Path) -> dict[str, Any]:
    """Run the full probe with a deterministic FakeTransport (no model)."""

    contract = _load_contract()
    _validate_contract(contract)
    transport = _build_offline_transport()
    return run_probe(
        contract, transport,
        model_name="offline-fake-transport",
        output_dir=output_dir.resolve(),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="R4 — model-generated regression test probe"
    )
    parser.add_argument("--validate-only", action="store_true",
                        help="Validate contract/identity without loading the model.")
    parser.add_argument("--run-offline", action="store_true",
                        help="Run the full pipeline with a deterministic FakeTransport.")
    parser.add_argument("--run", action="store_true",
                        help="Run the live RAW Qwen2.5 probe (GPU + authorization).")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Output directory for evidence (required for --run / --run-offline).")
    args = parser.parse_args()

    if not (args.validate_only or args.run_offline or args.run):
        parser.error("select --validate-only, --run-offline, or --run")

    contract = _load_contract()

    if args.validate_only:
        validation = _validate_contract(contract)
        identity = _run_identity(contract, runtime_python=_runtime_python())
        source_commit_sha = identity.get("source_commit_sha")
        validation = dict(validation)
        validation["source_commit_sha"] = source_commit_sha
        validation["experiment_contract_sha256"] = identity.get(
            "experiment_contract_sha256"
        )
        if source_commit_sha is None:
            print(json.dumps({
                "status": "FAIL",
                "reason": "source_commit_sha unresolved: "
                          "git rev-parse HEAD failed or returned a non-SHA value",
                "validation": validation,
                "run_identity": identity,
            }, indent=2, ensure_ascii=False))
            return 1
        print(json.dumps({
            "status": "PASS",
            "validation": validation,
            "run_identity": identity,
        }, indent=2, ensure_ascii=False))
        return 0

    if args.run_offline:
        if not args.output_dir:
            parser.error("--output-dir is required for --run-offline")
        output_dir = Path(args.output_dir).resolve()
        evidence = run_offline(output_dir)
        print(json.dumps({
            "status": "COMPLETE_OFFLINE",
            "summary": evidence.get("summary"),
            "evidence_path": str(output_dir / "evidence.json"),
        }, indent=2, ensure_ascii=False))
        return 0

    if args.run:
        if not args.output_dir:
            parser.error("--output-dir is required for --run")
        _validate_contract(contract)
        output_dir = Path(args.output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        # Live RAW Qwen2.5-Coder-7B-Instruct transport (requires CUDA).
        transport = LocalRawQwenTransport(
            max_new_tokens=GENERATION_CONFIG["max_new_tokens"],
            max_input_tokens=GENERATION_CONFIG["max_input_tokens"],
        )
        evidence = run_probe(
            contract, transport,
            model_name=f"{BASE_REPOSITORY}+RAW-BASE",
            output_dir=output_dir,
        )
        print(json.dumps({
            "status": "COMPLETE",
            "summary": evidence.get("summary"),
            "evidence_path": str(output_dir / "evidence.json"),
        }, indent=2, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
