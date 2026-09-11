"""R6 forbidden-content derivation and exported-text audit.

This module owns the anti-leakage audit layer of the R6 professor trace
export: scoped forbidden-content derivation from the hidden test assets
(reusing the accepted ``anti_leakage`` authority), the actual-output
exported-text audit, and the observed production-function-name audit.
Fail-closed: any hidden test source, node id, assertion, expected
literal, oracle root cause, reference repair snippet, or
chain-of-thought reconstruction is an error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

try:  # accepted anti-leakage authority (AUDIT ONLY; never prompt-facing)
    from experiments.debugger_interaction_v2_r5.anti_leakage import (
        ForbiddenContent,
        _evidence_diagnosis_texts,
        _hidden_test_assets,
        _strip_lines,
        derive_forbidden_content,
        scan_prompt,
    )
except ImportError:  # pragma: no cover - repository layout guard
    ForbiddenContent = None  # type: ignore[assignment]
    _evidence_diagnosis_texts = None  # type: ignore[assignment]
    _hidden_test_assets = None  # type: ignore[assignment]
    _strip_lines = None  # type: ignore[assignment]
    derive_forbidden_content = None  # type: ignore[assignment]
    scan_prompt = None  # type: ignore[assignment]

from agentic_debugger.evaluation.r6_evidence import (
    CURATED_ROOT,
    GOLD_DIFF_DIR,
    EvidenceResolver,
)
from agentic_debugger.evaluation.r6_trace import _stable_json

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Professor-safe leakage audit over exported output (fail closed)
# ---------------------------------------------------------------------------


def _fixture_dir_for(
    task_id: str, curated_root: Optional[Path] = None
) -> Path:
    root = curated_root or CURATED_ROOT
    fixture = root / task_id
    if fixture.is_dir() and (fixture / "task.json").is_file():
        return fixture
    raise FileNotFoundError(
        f"curated fixture for {task_id!r} missing; cannot derive audit "
        f"forbidden content (fail closed)"
    )


def _fixture_available(
    task_id: str, curated_root: Optional[Path] = None
) -> bool:
    root = curated_root or CURATED_ROOT
    fixture = root / task_id
    return fixture.is_dir() and (fixture / "task.json").is_file()


def _frozen_forbidden_content(
    task_id: str, resolver: Optional[EvidenceResolver]
) -> Optional[ForbiddenContent]:
    """Reconstruct AUDIT-ONLY forbidden content from the tracked frozen
    needle capsule (used when the local ignored quixbugs fixture is absent,
    e.g. pristine tracked-only checkouts)."""
    if resolver is None:
        return None
    path = resolver.audit_needles_path(task_id)
    if not path.is_file():
        return None
    frozen = json.loads(path.read_text(encoding="utf-8"))
    if frozen.get("schema_version") != "r6-quixbugs-audit-needles-v2":
        raise RuntimeError(f"frozen audit needles schema mismatch for {task_id}")
    fields = frozen.get("forbidden_content") or {}
    tuple_fields = {
        "f2p_node_ids", "p2p_node_ids", "hidden_test_filenames",
        "hidden_test_function_names", "hidden_test_source_lines",
        "assertion_source_lines", "expected_literals",
        "oracle_target_symbols", "reference_repair_snippets",
        "runtime_probe_call_sources", "runtime_probe_anchors",
        "runtime_probe_focus_functions", "production_source_lines",
    }
    kwargs = {
        key: (tuple(value) if key in tuple_fields and value is not None else value)
        for key, value in fields.items()
    }
    return ForbiddenContent(**kwargs)


def _gold_diff_added_lines(
    task_id: str, gold_diff_dir: Optional[Path] = None
) -> tuple[str, ...]:
    """AUDIT-ONLY: unique non-empty added lines of the tracked gold diff.

    The gold diff is the reference repair; its ``+`` lines are forbidden
    needles exactly like the accepted catalog reference-repair snippets.
    This function never feeds any model-facing path.
    """
    diff_path = (gold_diff_dir or GOLD_DIFF_DIR) / f"{task_id}.patch"
    if not diff_path.is_file():
        return ()
    lines: list[str] = []
    for line in diff_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            stripped = line[1:].strip()
            if stripped:
                lines.append(stripped)
    # De-duplicate preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for item in lines:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def derive_forbidden_content_scoped(
    task_id: str,
    fixture_dir: Path,
    *,
    gold_diff_dir: Optional[Path] = None,
) -> ForbiddenContent:
    """Derive AUDIT-ONLY forbidden content for one task, fail closed.

    - Catalog-curated tasks (the five curated holdout fixtures) reuse the
      accepted authority unchanged (hidden tests + catalog RuntimeProbe
      semantics + reference repair snippets).
    - QuixBugs validation tasks have no catalog scenario (interactive mode
      uses no RuntimeProbe), so the forbidden content is derived
      mechanically from the tracked fixture: hidden test assets, task.json
      oracle fields, and gold-diff added lines as reference-repair needles.
    """
    from agentic_debugger.demo.catalog import DemoCatalogError, scenario_for

    try:
        scenario_for(task_id)
    except DemoCatalogError:
        pass
    else:
        return derive_forbidden_content(task_id, fixture_dir)

    task_meta = json.loads(
        (fixture_dir / "task.json").read_text(encoding="utf-8")
    )
    tests = task_meta.get("tests", {})
    f2p = tuple(tests.get("fail_to_pass", []) or [])
    p2p = tuple(tests.get("pass_to_pass", []) or [])
    oracle = task_meta.get("oracle", {}) or {}
    hidden = _hidden_test_assets(fixture_dir / "tests")

    production_source_lines: list[str] = []
    module_path: Optional[str] = None
    for name in (
        (task_meta.get("constraints", {}) or {}).get("allowed_write_paths", [])
        or []
    ):
        path = fixture_dir / name
        if path.is_file() and name.endswith(".py") and not name.startswith("tests/"):
            production_source_lines.extend(_strip_lines(path.read_text(encoding="utf-8")))
            if module_path is None:
                module_path = name
    original_source_text = "\n".join(production_source_lines)

    # Gold-diff added lines are reference-repair needles only when they add
    # text absent from the original program (accepted derivation rule).
    reference_snippets = tuple(
        line for line in _gold_diff_added_lines(task_id, gold_diff_dir)
        if line not in original_source_text
    )

    public_context = (task_meta.get("title", "") or "") + " " + (
        task_meta.get("description", "") or ""
    )
    expected_literals = tuple(
        literal
        for literal in hidden["literals"]
        if len(literal) >= 3 and literal not in public_context
    )
    # Stripped source lines shorter than 3 characters (e.g. a lone "}")
    # are shared punctuation, not hidden-test evidence — the accepted
    # authority applies the same "too weak to be evidence" threshold to
    # expected literals.  The exported document is JSON, so a one- or
    # two-character punctuation needle could only fire spuriously.
    source_lines = tuple(
        line for line in hidden["source_lines"] if len(line) >= 3
    )

    return ForbiddenContent(
        task_id=task_id,
        f2p_node_ids=f2p,
        p2p_node_ids=p2p,
        hidden_test_filenames=tuple(hidden["filenames"]),
        hidden_test_function_names=tuple(hidden["function_names"]),
        hidden_test_source_lines=source_lines,
        assertion_source_lines=tuple(hidden["assertion_lines"]),
        expected_literals=expected_literals,
        oracle_root_cause_summary=oracle.get("root_cause_summary"),
        oracle_runtime_evidence_hint=oracle.get("runtime_evidence_hint"),
        oracle_bug_category=oracle.get("bug_category"),
        oracle_target_symbols=tuple(oracle.get("target_symbols", []) or []),
        reference_repair_snippets=reference_snippets,
        runtime_probe_call_sources=(),
        runtime_probe_anchors=(),
        runtime_probe_focus_functions=(),
        production_source_lines=tuple(production_source_lines),
        production_module_path=module_path,
    )


def audit_exported_text(
    text: str,
    task_id: str,
    legitimate_texts: tuple[str, ...] = (),
    *,
    curated_root: Optional[Path] = None,
    gold_diff_dir: Optional[Path] = None,
    resolver: Optional[EvidenceResolver] = None,
) -> dict[str, Any]:
    """Run the accepted anti-leakage scanner over exported trace text.

    The exported trace JSON is scanned exactly like a model prompt against
    the mechanically derived forbidden content of the fixture's hidden
    tests (hidden test source, node ids, assertion expressions, expected
    literals, oracle root cause, reference repair snippets).  Any finding
    is a fail-closed error — professor JSON must not reintroduce anything
    the clean-holdout policy protected.

    Forbidden content comes from the accepted derivation when the tracked
    fixture is present, or from the tracked frozen needle capsule when it
    is not (pristine tracked-only checkouts).

    The model-authored diagnosis text is legitimately retained in the
    export (the professor asks for the diagnosis), so it is subtracted
    before scanning, exactly as the accepted prompt audit subtracts it
    when it is rendered back into later prompts.  Everything else is
    scanned conservatively — no other subtraction.
    """
    if derive_forbidden_content is None or scan_prompt is None:  # pragma: no cover
        raise RuntimeError("accepted anti-leakage authority is not importable")
    if _fixture_available(task_id, curated_root):
        forbidden = derive_forbidden_content_scoped(
            task_id,
            _fixture_dir_for(task_id, curated_root),
            gold_diff_dir=gold_diff_dir,
        )
    else:
        forbidden = _frozen_forbidden_content(task_id, resolver)
        if forbidden is None:
            raise RuntimeError(
                f"audit forbidden content unavailable for {task_id!r}: "
                f"neither the tracked fixture nor the frozen needle capsule "
                f"is present (fail closed)"
            )
    # The task id is PUBLIC fixture identity (tracked fixture directory,
    # split manifest, task.json).  The accepted authority already excludes
    # needles present in the public title/description ("public_context"
    # rule); the task id is the same class of public context and is
    # REQUIRED by the professor-facing trace identity.  For some quixbugs
    # tasks the oracle bug category is mechanically the task id itself, so
    # the id is removed before scanning.  This cannot hide a hidden-test
    # needle: node ids / test filenames / assertion lines never contain
    # the "quixbugs-<task>" id (covered by a dedicated regression test).
    reduced = text.replace(task_id, "")
    findings = scan_prompt(
        reduced,
        forbidden,
        prompt_index=0,
        controller_state="professor_trace_export",
        legitimate_texts=legitimate_texts,
    )
    return {
        "task_id": task_id,
        "scanned_chars": len(text),
        "leakage_findings": [f.to_mapping() for f in findings],
        "passed": len(findings) == 0,
    }


def _observed_production_function_names(
    evidence: dict[str, Any],
) -> tuple[str, ...]:
    """Production function names actually observed by the debugger.

    These are real production-region frame/pause observations (the model
    legitimately saw them as stack evidence).  The accepted prompt audit
    subtracts their frame-line renderings before source-derived needle
    checks; the structured trace export subtracts the same identifiers.
    """
    task = evidence.get("task") or {}
    module_path = task.get("module_path")
    names: list[str] = []
    for line in (evidence.get("trajectory_jsonl") or "").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event_type") != "observation":
            continue
        obs = (event.get("payload") or {}).get("observation") or {}
        if obs.get("status") != "ok":
            continue
        payload = obs.get("payload") or {}
        if payload.get("script") != module_path:
            continue
        for fn in (payload.get("function"),):
            if isinstance(fn, str) and fn and fn != "<module>" and fn not in names:
                names.append(fn)
        # Real production-region stack frames observed by the debugger
        # (the accepted prompt audit subtracts exactly these frame-line
        # renderings).
        for frame in payload.get("frames") or []:
            if not isinstance(frame, dict):
                continue
            if frame.get("script") != module_path:
                continue
            fn = frame.get("function")
            if isinstance(fn, str) and fn and fn != "<module>" and fn not in names:
                names.append(fn)
    return tuple(names)


def _audit_trace(
    trace: dict[str, Any],
    task_id: str,
    evidence: dict[str, Any],
    *,
    curated_root: Optional[Path] = None,
    gold_diff_dir: Optional[Path] = None,
    resolver: Optional[EvidenceResolver] = None,
) -> dict[str, Any]:
    """Audit one exported trace with the accepted legitimate subtractions.

    Subtracted as legitimate (never as leak evidence): the model-authored
    diagnosis (the model's own audited output rendered back at PATCH time —
    the accepted audit subtracts it) and the production function names the
    debugger really observed (the accepted audit subtracts their frame-line
    renderings; the export carries the same identifiers structurally).
    Hidden test source, node ids, assertion expressions, expected literals,
    oracle root cause and reference repair are matched against the raw
    exported text and remain fail-closed.
    """
    diagnosis_texts = _evidence_diagnosis_texts(evidence)
    observed = _observed_production_function_names(evidence)
    legitimate = tuple(sorted(set(diagnosis_texts) | set(observed), key=len, reverse=True))
    return audit_exported_text(
        _stable_json(trace),
        task_id,
        legitimate_texts=legitimate,
        curated_root=curated_root,
        gold_diff_dir=gold_diff_dir,
        resolver=resolver,
    )
