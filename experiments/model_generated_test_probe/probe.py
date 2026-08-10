#!/usr/bin/env python3
"""S1-P — Professor-requested model-generated regression test probe.

Orchestrates the probe:

    explicit expected behavior (PUBLIC BEHAVIOR SPEC)
    + buggy source
    -> frozen RAW Qwen2.5-Coder-7B
    -> ONE executable pytest regression test
    -> FREEZE exact source + SHA-256 + raw-response provenance
    -> buggy code FAILS the frozen test   (recorded)
    -> one-shot model-produced fix         (same frozen model; gold hidden)
    -> fixed code evaluated against the SAME frozen test  (PASS/FAIL recorded)
    -> independent EvaluationVerifier executed separately   (authority)

The generated test is AUXILIARY evidence, never the correctness authority.
The independent ``EvaluationVerifier`` remains the final correctness authority.

Modes:
    --validate-only   Validate contract/identity without loading the model.
    --run-offline     Full pipeline driven by a deterministic FakeTransport
                      (no model, no GPU) for offline validation/tests.
    --run             Live run with LocalRawQwenTransport (GPU + authorization
                      required; NOT executed in BUILD).

This runner is experiment-local. It imports from the production
``agentic_debugger`` package and from the accepted S1 experiment
``experiments.debugger_interaction_v2`` but modifies neither.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier

from experiments.debugger_interaction_v2.adapter import (
    NOT_AVAILABLE,
    NOT_RECORDED,
    TransportError,
    TransportResponse,
)
from experiments.debugger_interaction_v2.transport import (
    BASE_REPOSITORY,
    BASE_REVISION,
    GENERATION_CONFIG,
    FakeTransport,
    LocalRawQwenTransport,
)

from experiments.model_generated_test_probe import test_generation as tg
from experiments.model_generated_test_probe.generated_test_runner import (
    run_buggy,
    run_fixed,
)


CONTRACT_PATH = THIS_FILE.with_name("experiment_contract.json")
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"
TASK_ID = "curated-none-handling-001"
FIXTURE_DIR = CURATED_ROOT / TASK_ID


# ---------------------------------------------------------------------------
# Contract / identity
# ---------------------------------------------------------------------------


def _load_contract() -> dict[str, Any]:
    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if value.get("schema_version") != "model-generated-test-probe-v1":
        raise RuntimeError("unsupported experiment contract")
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

    # Cross-check frozen text-asset hashes against the live module.
    if contract.get("behavior_spec", {}).get("sha256") != tg.BEHAVIOR_SPEC_SHA256:
        raise RuntimeError("behavior_spec sha256 drift between contract and module")
    prompts = contract.get("prompts", {})
    if prompts.get("system_prompt_generation_sha256") != tg.SYSTEM_PROMPT_GENERATION_SHA256:
        raise RuntimeError("system_prompt_generation sha256 drift")
    if prompts.get("system_prompt_fix_sha256") != tg.SYSTEM_PROMPT_FIX_SHA256:
        raise RuntimeError("system_prompt_fix sha256 drift")

    # Fixture tree hash stability.
    fixture_hash = _fixture_tree_sha256(FIXTURE_DIR)
    frozen_hash = task.get("fixture_tree_sha256")
    if frozen_hash and fixture_hash != frozen_hash:
        raise RuntimeError(
            f"fixture tree hash drift: expected {frozen_hash}, got {fixture_hash}"
        )

    return {
        "contract_sha256": _contract_sha256(contract),
        "fixture_tree_sha256": fixture_hash,
        "behavior_spec_sha256": tg.BEHAVIOR_SPEC_SHA256,
        "system_prompt_generation_sha256": tg.SYSTEM_PROMPT_GENERATION_SHA256,
        "system_prompt_fix_sha256": tg.SYSTEM_PROMPT_FIX_SHA256,
        "validated": True,
    }


def _run_identity(contract: dict[str, Any]) -> dict[str, Any]:
    model = contract["model"]
    return {
        "schema_version": "model-generated-test-probe-v1-identity",
        "experiment_id": "model-generated-test-probe",
        "source_commit_sha": _git_head(REPO_ROOT),
        "experiment_contract_sha256": _contract_sha256(contract),
        "task_id": TASK_ID,
        "model_condition": "RAW_BASE",
        "adapter_applied": False,
        "base_repository": model["base_repository"],
        "base_revision": model["base_revision"],
        "rag_enabled": False,
        "generation": model["generation"],
        "behavior_spec_sha256": tg.BEHAVIOR_SPEC_SHA256,
        "system_prompt_generation_sha256": tg.SYSTEM_PROMPT_GENERATION_SHA256,
        "system_prompt_fix_sha256": tg.SYSTEM_PROMPT_FIX_SHA256,
    }


# ---------------------------------------------------------------------------
# One-shot fix generation
# ---------------------------------------------------------------------------


def _usage_dict(usage: Optional[dict[str, Any]]) -> dict[str, Any]:
    return tg._usage_dict(usage)


def _produce_fix(
    transport: Any,
    task: Any,
    frozen_test: tg.FrozenTest,
    *,
    request_timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """One-shot model-produced fix. No retries for malformed/failing diffs.

    Returns a provenance record with the candidate patch (or null + error).
    Gold/fixed source remains hidden.
    """

    buggy_source = (FIXTURE_DIR / "display_name.py").read_text(encoding="utf-8")
    user_prompt = tg.build_fix_user_prompt(task, buggy_source, frozen_test.source)
    user_hash = tg._sha256(user_prompt)

    raw_text: str
    transport_error_cat: Optional[str] = None
    usage: dict[str, Any]
    try:
        response: TransportResponse = transport.request(
            system_prompt=tg.SYSTEM_PROMPT_FIX,
            user_prompt=user_prompt,
            timeout_seconds=request_timeout_seconds,
        )
        raw_text = response.raw_text
        usage = _usage_dict(response.usage)
    except TransportError as exc:
        raw_text = NOT_AVAILABLE
        transport_error_cat = exc.category
        usage = _usage_dict(None)
    except Exception as exc:  # noqa: BLE001 — fail-closed retention
        raw_text = NOT_AVAILABLE
        transport_error_cat = type(exc).__name__
        usage = _usage_dict(None)

    record: dict[str, Any] = {
        "sourcing": "second_model_request",
        "system_prompt_sha256": tg.SYSTEM_PROMPT_FIX_SHA256,
        "user_prompt_sha256": user_hash,
        "raw_model_response": raw_text if raw_text != NOT_AVAILABLE else NOT_AVAILABLE,
        "raw_response_sha256": (
            tg._sha256(raw_text) if raw_text != NOT_AVAILABLE else NOT_AVAILABLE
        ),
        "transport_error_category": transport_error_cat,
        "usage": usage,
        "anti_leakage": {
            "gold_code_shown": False,
            "oracle_shown": False,
            "fixed_source_shown": False,
        },
        "candidate_patch": None,
        "candidate_patch_sha256": None,
        "extraction_error": None,
    }

    if transport_error_cat is not None:
        record["extraction_error"] = f"transport_failure: {transport_error_cat}"
        return record

    try:
        diff = tg.extract_diff_block(raw_text)
    except tg.ExtractionError as exc:
        record["extraction_error"] = f"{exc.category}: {exc.detail}"
        return record

    record["candidate_patch"] = diff
    record["candidate_patch_sha256"] = tg._sha256(diff)
    return record


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
    """Run the full probe pipeline with a given transport.

    Returns the evidence dict (also written to ``evidence.json``).
    """

    task = load_task(str(FIXTURE_DIR / "task.json"))
    budgets = contract.get("budgets", {})
    gen_max = int(budgets.get("generation_attempts_max", 3))
    test_timeout = int(budgets.get("test_timeout_seconds", 20))
    req_timeout = float(budgets.get("model_request_timeout_seconds", 60.0))

    # All disposable workspaces (generation executability checks, buggy/fixed
    # runs, and the independent verifier's own workspace) nest under a per-run
    # case dir. By default this case dir lives in a SYSTEM TEMP dir, NOT under
    # output_dir: pytest's --collect-only emits node paths relative to its
    # rootdir, and when the workspace is under the repository (which contains
    # pyproject.toml) pytest's rootdir becomes the repo root, leaking the full
    # relative "outputs/.../workspace/tests/...::node" prefix into the collected
    # ids and breaking the verifier's declared-node match. A system-temp parent
    # (the same approach the accepted demo CLI uses via tempfile.mkdtemp) makes
    # pytest resolve rootdir to the workspace itself, yielding the expected
    # relative "tests/...::node" ids. Evidence still goes to output_dir; only
    # disposable workspaces go under temp and are cleaned up.
    if case_parent is None:
        import tempfile
        case_parent = Path(tempfile.mkdtemp(prefix="s1p-probe-"))
    case_dir = case_parent.resolve() / f"case-{TASK_ID}"
    case_dir.mkdir(parents=True, exist_ok=True)
    fixture_dir_abs = FIXTURE_DIR.resolve()

    # -- 1. Retry-bounded test generation --------------------------------
    gen_outcome = tg.generate_frozen_test(
        transport, task, fixture_dir_abs, case_dir,
        model_name=model_name,
        max_attempts=gen_max,
        request_timeout_seconds=req_timeout,
        test_timeout_seconds=test_timeout,
    )

    evidence: dict[str, Any] = {
        "schema_version": "model-generated-test-probe-v1",
        "run_identity": _run_identity(contract),
        "test_generation": {
            "stop_reason": gen_outcome.stop_reason,
            "behavior_spec_sha256": gen_outcome.behavior_spec_sha256,
            "behavior_spec": tg.BEHAVIOR_SPEC,
            "system_prompt_sha256": gen_outcome.system_prompt_sha256,
            "anti_leakage": gen_outcome.anti_leakage,
            "attempts": gen_outcome.attempts,
            "frozen_test": None,
        },
        "buggy_run": None,
        "model_fixed_code": None,
        "generated_test_eval": None,
        "verifier": {"executed": False},
        "summary": None,
        "claims_boundary": contract.get("claims_boundary"),
    }

    frozen = gen_outcome.frozen_test
    if frozen is not None:
        evidence["test_generation"]["frozen_test"] = {
            "source": frozen.source,
            "sha256": frozen.sha256,
            "attempt_index": frozen.attempt_index,
            "raw_response_sha256": frozen.raw_response_sha256,
            "user_prompt_sha256": frozen.user_prompt_sha256,
            "usage": frozen.usage,
            "executability": frozen.executability,
        }

    # -- 2. If no executable test, STOP (record negative result) ---------
    if frozen is None:
        evidence["summary"] = _summary(
            generated_test_froze=False,
            generated_test_did_not_encode_defect=False,
            buggy_failed_frozen_test=False,
            fixed_code_passed_frozen_test=False,
            verifier_executed=False,
            verifier_resolved=False,
            stop_reason=gen_outcome.stop_reason,
        )
        _write_evidence(evidence, output_dir)
        return evidence

    # -- 3. Buggy run against the FROZEN test ----------------------------
    buggy_result = run_buggy(
        frozen, fixture_dir_abs, case_dir, timeout_seconds=test_timeout,
    )
    evidence["buggy_run"] = _run_result_to_dict(buggy_result)

    buggy_failed = (buggy_result.status == "FAIL")
    did_not_encode = (buggy_result.status == "PASS")

    # -- 4. One-shot model-produced fix ----------------------------------
    fix_record = _produce_fix(
        transport, task, frozen, request_timeout_seconds=req_timeout,
    )
    evidence["model_fixed_code"] = fix_record
    candidate_patch = fix_record.get("candidate_patch")

    # -- 5. Fixed-code run against the SAME frozen test ------------------
    fixed_result: Optional[dict[str, Any]] = None
    if candidate_patch:
        fr = run_fixed(
            frozen, candidate_patch, fixture_dir_abs, case_dir,
            timeout_seconds=test_timeout,
        )
        fixed_result = _run_result_to_dict(fr)
        fixed_passed = (fr.status == "PASS")
    else:
        fixed_passed = False
        fixed_result = {
            "label": "fixed",
            "frozen_test_sha256": frozen.sha256,
            "executed": False,
            "status": "NOT_RUN",
            "reason": "no_candidate_patch",
            "patch_applied": False,
            "patch_error": fix_record.get("extraction_error"),
            "workspace_cleaned": True,
        }
    evidence["generated_test_eval"] = fixed_result

    # -- 6. Independent verifier (separate; authority) -------------------
    verifier_result: dict[str, Any] = {"executed": False}
    if candidate_patch:
        try:
            evaluation = EvaluationVerifier(
                str(REPO_ROOT), workspace_parent=str(case_dir)
            ).evaluate(task, candidate_patch)
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

    evidence["summary"] = _summary(
        generated_test_froze=True,
        generated_test_did_not_encode_defect=did_not_encode,
        buggy_failed_frozen_test=buggy_failed,
        fixed_code_passed_frozen_test=fixed_passed,
        verifier_executed=verifier_executed,
        verifier_resolved=verifier_resolved,
        stop_reason=(
            "generated_test_did_not_encode_defect" if did_not_encode else "completed"
        ),
    )

    _write_evidence(evidence, output_dir)
    return evidence


def _run_result_to_dict(r: Any) -> dict[str, Any]:
    return {
        "label": r.label,
        "frozen_test_sha256": r.frozen_test_sha256,
        "executed": r.executed,
        "status": r.status,
        "exit_code": r.exit_code,
        "counts": r.counts,
        "reason": r.reason,
        "patch_applied": r.patch_applied,
        "patch_error": r.patch_error,
        "workspace_cleaned": r.workspace_cleaned,
        "stdout": r.stdout_bounded,
        "stderr": r.stderr_bounded,
    }


def _summary(
    *,
    generated_test_froze: bool,
    generated_test_did_not_encode_defect: bool,
    buggy_failed_frozen_test: bool,
    fixed_code_passed_frozen_test: bool,
    verifier_executed: bool,
    verifier_resolved: bool,
    stop_reason: str,
) -> dict[str, Any]:
    return {
        "stop_reason": stop_reason,
        "generated_test_froze": generated_test_froze,
        "generated_test_did_not_encode_defect": generated_test_did_not_encode_defect,
        "buggy_failed_frozen_test": buggy_failed_frozen_test,
        "fixed_code_passed_frozen_test": fixed_code_passed_frozen_test,
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


# A deterministic generated test that encodes the public behavior spec and
# FAILS the buggy code (which calls .strip() on None) and PASSES the fix.
# Exactly ONE test function: the executability gate requires exactly one
# executed test node.
_OFFLINE_GENERATED_TEST = (
    "from display_name import format_display_name\n"
    "\n"
    "\n"
    "def test_none_returns_anonymous() -> None:\n"
    "    assert format_display_name(None) == \"Anonymous\"\n"
)

# A correct unified diff against the buggy source. Hunk header counts: the
# original file has 5 lines; this hunk shows all 5 (1 context + 1 removed + 3
# context) on the old side and 7 (1 context + 3 added + 3 context) on the new
# side. The patcher detects the dominant EOL and appends it to + lines; hunk
# body lines are LF-only here, which the patcher accepts (context match strips
# \n\r).
_OFFLINE_FIX_DIFF = (
    "--- a/display_name.py\n"
    "+++ b/display_name.py\n"
    "@@ -1,5 +1,7 @@\n"
    " def format_display_name(name: str | None) -> str:\n"
    "-    normalized_name = name.strip()\n"
    "+    if name is None:\n"
    "+        return \"Anonymous\"\n"
    "+    normalized_name = name.strip()\n"
    "     if not normalized_name:\n"
    "         return \"Anonymous\"\n"
    "     return normalized_name.title()\n"
)


def _build_offline_transport() -> FakeTransport:
    """A FakeTransport that yields a generated test then a fix diff.

    Two responses in order: (1) the generated test code block; (2) the fix
    diff code block. This drives the full pipeline offline.
    """

    gen_response = (
        "```python\n" + _OFFLINE_GENERATED_TEST + "```\n"
    )
    fix_response = (
        "```diff\n" + _OFFLINE_FIX_DIFF + "```\n"
    )
    return FakeTransport((gen_response, fix_response))


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
        description="S1-P — model-generated regression test probe"
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
        identity = _run_identity(contract)
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
        # Live RAW Qwen2.5 transport (requires GPU).
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