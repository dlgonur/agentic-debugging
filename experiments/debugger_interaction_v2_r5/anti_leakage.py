"""R5.9 — fail-closed ACTUAL-PROMPT anti-leakage scanner (AUDIT ONLY).

Strict separation: this module is an AUDITOR.  It reads the exact fixture
evaluation assets — hidden tests, oracle fields, reference repair,
RuntimeProbe semantic hints — and scans every exact live
``telemetry[*].request.user_prompt_full`` AFTER a run.  It is never
imported by any model prompt-construction path: prompt construction
(``sanitize.py``, ``bridge.py``, ``adapter.py``, ``demo/tools.py``) never
imports this module, and this module never constructs a prompt.

The final matrix PASS gate requires ``leakage_findings == []`` for every
actual model prompt.  The old r5.7 evidence (which forwarded raw pytest
failure output and failing-verifier-record tails) MUST fail this audit —
that failure is a regression test.

Matching is mechanical: exact substring / per-line checks against the
derived forbidden content.  Occurrences that are themselves part of the
legitimately shown ORIGINAL production source are not findings (the model
must see production source); everything else is.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Forbidden content derived from the fixture evaluation assets (AUDIT ONLY)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForbiddenContent:
    task_id: str
    f2p_node_ids: tuple[str, ...] = ()
    p2p_node_ids: tuple[str, ...] = ()
    hidden_test_filenames: tuple[str, ...] = ()
    hidden_test_function_names: tuple[str, ...] = ()
    hidden_test_source_lines: tuple[str, ...] = ()
    assertion_source_lines: tuple[str, ...] = ()
    expected_literals: tuple[str, ...] = ()
    oracle_root_cause_summary: Optional[str] = None
    oracle_runtime_evidence_hint: Optional[str] = None
    oracle_bug_category: Optional[str] = None
    oracle_target_symbols: tuple[str, ...] = ()
    reference_repair_snippets: tuple[str, ...] = ()
    runtime_probe_call_sources: tuple[str, ...] = ()
    runtime_probe_anchors: tuple[str, ...] = ()
    runtime_probe_focus_functions: tuple[str, ...] = ()
    #: Stripped non-empty lines of the ORIGINAL production source.  The
    #: model legitimately sees these; needle occurrences that lie entirely
    #: inside a production source line are not findings.
    production_source_lines: tuple[str, ...] = ()
    #: The production module path (``recent_window.py``) — legitimately
    #: present in source headers and the patch affordance.
    production_module_path: Optional[str] = None

    def needles(self) -> list[tuple[str, str]]:
        """All ``(kind, needle)`` pairs.  Source-derived needles are checked
        against the prompt minus the production source lines."""
        out: list[tuple[str, str]] = []
        for nid in self.f2p_node_ids:
            out.append(("hidden_f2p_node_id", nid))
        for nid in self.p2p_node_ids:
            out.append(("hidden_p2p_node_id", nid))
        for name in self.hidden_test_filenames:
            out.append(("hidden_test_filename", name))
        for name in self.hidden_test_function_names:
            out.append(("hidden_test_function_name", name))
        for line in self.hidden_test_source_lines:
            out.append(("hidden_test_source_line", line))
        for line in self.assertion_source_lines:
            out.append(("assertion_source_expression", line))
        for lit in self.expected_literals:
            out.append(("expected_literal", lit))
        if self.oracle_root_cause_summary:
            out.append(("oracle_root_cause_summary", self.oracle_root_cause_summary))
        if self.oracle_runtime_evidence_hint:
            out.append(("oracle_runtime_evidence_hint", self.oracle_runtime_evidence_hint))
        if self.oracle_bug_category:
            out.append(("oracle_bug_category", self.oracle_bug_category))
        for sym in self.oracle_target_symbols:
            out.append(("oracle_target_symbol", sym))
        for snip in self.reference_repair_snippets:
            out.append(("reference_repair_snippet", snip))
        for src in self.runtime_probe_call_sources:
            out.append(("runtime_probe_call_source", src))
        for anc in self.runtime_probe_anchors:
            out.append(("runtime_probe_anchor", anc))
        for fn in self.runtime_probe_focus_functions:
            out.append(("runtime_probe_focus_function", fn))
        return out


def _strip_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            out.append(stripped)
    return out


def _literal_reprs(node: ast.AST) -> list[str]:
    """Representation forms of a literal comparison operand (expected
    hidden value).  ``None``/``True``/``False`` and bare numbers are too
    common to be evidence and are excluded."""
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return []
    if value is None or value is True or value is False:
        return []
    if type(value) in (int, float):
        return []
    out: list[str] = []
    if type(value) is str and len(value) >= 2:
        out.append(value)
    out.append(repr(value))
    return out


def _assertion_literals(source: str) -> list[str]:
    """Expected literals mechanically extracted from hidden-test asserts."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    literals: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare):
            continue
        # Literal RHS of any comparison operator (==, !=, in, not in, ...).
        for comparator in test.comparators:
            for form in _literal_reprs(comparator):
                if form not in literals:
                    literals.append(form)
    return literals


def _hidden_test_assets(test_dir: Path) -> dict[str, Any]:
    """Collect every test filename, function name, source line, assertion
    line, and expected literal from the fixture's hidden tests."""
    filenames: list[str] = []
    function_names: list[str] = []
    source_lines: list[str] = []
    assertion_lines: list[str] = []
    literals: list[str] = []
    if not test_dir.is_dir():
        return {
            "filenames": filenames, "function_names": function_names,
            "source_lines": source_lines, "assertion_lines": assertion_lines,
            "literals": literals,
        }
    for path in sorted(test_dir.rglob("*.py")):
        relative = path.relative_to(test_dir.parent).as_posix()
        filenames.append(relative)
        text = path.read_text(encoding="utf-8")
        source_lines.extend(_strip_lines(text))
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    function_names.append(node.name)
            if isinstance(node, ast.Assert):
                assertion_lines.append(ast.get_source_segment(text, node) or "")
        literals.extend(_assertion_literals(text))
    # De-duplicate preserving order.
    def _uniq(items: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out

    return {
        "filenames": _uniq(filenames),
        "function_names": _uniq(function_names),
        "source_lines": _uniq(source_lines),
        "assertion_lines": _uniq(assertion_lines),
        "literals": _uniq(literals),
    }


def derive_forbidden_content(task_id: str, fixture_dir: Path) -> ForbiddenContent:
    """Derive the AUDIT-ONLY forbidden content from the actual fixture.

    Reads: ``task.json`` (F2P/P2P node ids, oracle fields), the hidden test
    files, the original production source, the demo catalog's RuntimeProbe
    semantic hints, and the reference repair snippets.  This function is
    the only reader of hidden-test assets besides the verifier itself.
    """
    from agentic_debugger.demo.catalog import scenario_for

    task_path = fixture_dir / "task.json"
    task_meta: dict[str, Any] = json.loads(task_path.read_text(encoding="utf-8"))
    tests = task_meta.get("tests", {})
    f2p = tuple(tests.get("fail_to_pass", []) or [])
    p2p = tuple(tests.get("pass_to_pass", []) or [])
    oracle = task_meta.get("oracle", {}) or {}
    hidden = _hidden_test_assets(fixture_dir / "tests")

    production_source_lines: list[str] = []
    for name in (task_meta.get("constraints", {}) or {}).get("allowed_write_paths", []) or []:
        path = fixture_dir / name
        if path.is_file() and name.endswith(".py") and not name.startswith("tests/"):
            production_source_lines.extend(_strip_lines(path.read_text(encoding="utf-8")))

    probe = scenario_for(task_id).runtime_probe
    repair = scenario_for(task_id).reference_repair

    module_path: Optional[str] = None
    for name in (task_meta.get("constraints", {}) or {}).get("allowed_write_paths", []) or []:
        path = fixture_dir / name
        if path.is_file() and name.endswith(".py") and not name.startswith("tests/"):
            module_path = name
            break

    # A repair snippet is only an oracle needle when it adds text absent from
    # the original program.  Some repairs reduce an expression to an
    # identifier already present in the original source and real PDB locals;
    # that identifier carries no reference-repair information.
    original_source_text = "\n".join(production_source_lines)
    reference_snippets: list[str] = (
        [repair.new_snippet]
        if repair.new_snippet not in original_source_text
        else []
    )

    # Expected literals: drop needles shorter than 3 characters (too weak to
    # be evidence) and needles that appear in the PUBLIC task title or
    # description (legitimate model context; the hidden assertion line that
    # introduced them is still caught via assertion_source_expression).
    public_context = (task_meta.get("title", "") or "") + " " + (
        task_meta.get("description", "") or ""
    )
    expected_literals = [
        literal for literal in hidden["literals"]
        if len(literal) >= 3 and literal not in public_context
    ]

    # The RuntimeProbe focus function is the production function name — it
    # legitimately appears as real production frame/stack evidence (allowed
    # debugger observations) and adds no probe-specific information; the
    # probe's semantic value is the hidden call source and the anchor line,
    # which are scanned.
    return ForbiddenContent(
        task_id=task_id,
        f2p_node_ids=f2p,
        p2p_node_ids=p2p,
        hidden_test_filenames=tuple(hidden["filenames"]),
        hidden_test_function_names=tuple(hidden["function_names"]),
        hidden_test_source_lines=tuple(hidden["source_lines"]),
        assertion_source_lines=tuple(hidden["assertion_lines"]),
        expected_literals=tuple(expected_literals),
        oracle_root_cause_summary=oracle.get("root_cause_summary"),
        oracle_runtime_evidence_hint=oracle.get("runtime_evidence_hint"),
        oracle_bug_category=oracle.get("bug_category"),
        oracle_target_symbols=tuple(oracle.get("target_symbols", []) or []),
        reference_repair_snippets=tuple(reference_snippets),
        runtime_probe_call_sources=(probe.call_source,),
        runtime_probe_anchors=(probe.anchor,),
        runtime_probe_focus_functions=(),
        production_source_lines=tuple(production_source_lines),
        production_module_path=module_path,
    )


# ---------------------------------------------------------------------------
# Prompt scanning
# ---------------------------------------------------------------------------

#: Kinds whose needles may legitimately appear inside the ORIGINAL
#: production source or as REAL production debugger evidence (source
#: definitions, stack frames, pause lines — the model must see both).  They
#: are checked against the prompt with that legitimate evidence subtracted.
_SOURCE_DERIVED_KINDS = frozenset({
    "oracle_target_symbol",
    "reference_repair_snippet",
    "runtime_probe_anchor",
    "expected_literal",
})

#: Model-facing rendering of one real production stack frame.  The frame
#: names the production function — legitimate debugger evidence.
_FRAME_LINE_RE = re.compile(r"frame_id=\d+\s+\S+\s+line=\d+\s+script=\S+")
#: Model-facing pause lines (``Paused at line 2 in function 'f'``,
#: ``Paused at line 3 in 'f'`` for step observations, ``was paused ...``
#: in the terminal stages).
_PAUSE_LINE_RE = re.compile(
    r"(?i)(?:was )?paused at line \d+ in (?:function )?'[^']+'"
)

#: Needles shorter than this that consist only of identifier characters are
#: matched as WHOLE WORDS so a hidden expected literal like ``employee`` can
#: never fire on a legitimate substring such as ``employee_flag`` in the
#: production source or debugger locals.
_MAX_WORD_BOUNDARY_NEEDLE_CHARS = 24

_WORD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _needle_finder(needle: str) -> "re.Pattern[str]":
    """Return the mechanical matcher for one needle.

    Short pure-identifier needles match as whole words (word boundaries);
    everything else (test filenames, node ids, assertion source lines,
    multi-word phrases) matches as an exact substring.
    """
    if (
        len(needle) <= _MAX_WORD_BOUNDARY_NEEDLE_CHARS
        and _WORD_RE.match(needle) is not None
    ):
        return re.compile(rf"\b{re.escape(needle)}\b")
    return re.compile(re.escape(needle))


def _subtract_legitimate_evidence(
    prompt: str,
    source_lines: tuple[str, ...],
    module_path: Optional[str] = None,
    legitimate_texts: tuple[str, ...] = (),
) -> str:
    """Remove the model's LEGITIMATE evidence before checking source-derived
    needles: the rendered original source lines, real stack frame lines,
    real pause lines, the production module path (source headers, patch
    affordance), and MODEL-AUTHORED text rendered back into later prompts
    (the retained diagnosis).  Everything remaining in the prompt is
    material the model should never have received."""
    reduced = prompt
    for line in source_lines:
        if line in reduced:
            reduced = reduced.replace(line, "", 1)
    reduced = _FRAME_LINE_RE.sub("", reduced)
    reduced = _PAUSE_LINE_RE.sub("", reduced)
    if module_path:
        reduced = reduced.replace(module_path, "")
    for text in sorted(legitimate_texts, key=len, reverse=True):
        if text and text in reduced:
            reduced = reduced.replace(text, "")
    return reduced


@dataclass(frozen=True)
class LeakageFinding:
    prompt_index: int
    controller_state: str
    kind: str
    needle: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "prompt_index": str(self.prompt_index),
            "controller_state": self.controller_state,
            "kind": self.kind,
            "needle": self.needle,
        }


def _subtract_production_source(prompt: str, source_lines: tuple[str, ...]) -> str:
    """Remove occurrences of production source line contents (the model
    legitimately sees the original source) before checking source-derived
    needles."""
    reduced = prompt
    for line in source_lines:
        if line in reduced:
            reduced = reduced.replace(line, "", 1)
    return reduced


def scan_prompt(
    prompt: str,
    forbidden: ForbiddenContent,
    *,
    prompt_index: int = 0,
    controller_state: str = "?",
    legitimate_texts: tuple[str, ...] = (),
) -> list[LeakageFinding]:
    """Scan ONE exact actual user prompt against the forbidden content.

    Fail-closed mechanical matching: any forbidden needle present in the
    actual prompt (outside the legitimately shown production source,
    debugger evidence, and model-authored text) is a finding.
    """
    findings: list[LeakageFinding] = []
    if type(prompt) is not str or not prompt:
        return findings
    for kind, needle in forbidden.needles():
        if not needle:
            continue
        haystack = prompt
        if kind in _SOURCE_DERIVED_KINDS:
            haystack = _subtract_legitimate_evidence(
                prompt,
                forbidden.production_source_lines,
                forbidden.production_module_path,
                legitimate_texts,
            )
        finder = _needle_finder(needle)
        if finder.search(haystack) is not None:
            findings.append(LeakageFinding(prompt_index, controller_state, kind, needle[:200]))
    return findings


def _evidence_diagnosis_texts(evidence: dict[str, Any]) -> tuple[str, ...]:
    """Model-authored diagnosis texts retained into later prompts.

    The diagnosis is the model's OWN output rendered back at PATCH time —
    legitimate context, never hidden-test material (any content in it
    originated from the model's already-audited inputs).  Subtracting it
    cannot hide a leak.
    """
    texts: list[str] = []
    for record in evidence.get("telemetry") or []:
        if type(record) is not dict:
            continue
        directive = record.get("translated_directive") or {}
        if directive.get("is_diagnosis") is not True:
            continue
        text = directive.get("diagnosis_text")
        if type(text) is str and text.strip():
            texts.append(text)
    return tuple(texts)


def scan_evidence(
    evidence: dict[str, Any],
    forbidden: ForbiddenContent,
) -> list[LeakageFinding]:
    """Scan every exact live ``telemetry[*].request.user_prompt_full`` in
    one evidence document."""
    findings: list[LeakageFinding] = []
    legitimate_texts = _evidence_diagnosis_texts(evidence)
    telemetry = evidence.get("telemetry") or []
    for index, record in enumerate(telemetry):
        if type(record) is not dict:
            continue
        request = record.get("request") or {}
        prompt = request.get("user_prompt_full")
        if type(prompt) is not str:
            continue
        findings.extend(
            scan_prompt(
                prompt,
                forbidden,
                prompt_index=index,
                controller_state=str(record.get("controller_state", "?")),
                legitimate_texts=legitimate_texts,
            )
        )
    return findings


def audit_evidence_dict(
    evidence: dict[str, Any],
    task_id: str,
    fixture_dir: Path,
) -> dict[str, Any]:
    """One-task audit: derived forbidden content + findings per prompt."""
    forbidden = derive_forbidden_content(task_id, fixture_dir)
    findings = scan_evidence(evidence, forbidden)
    telemetry = evidence.get("telemetry") or []
    prompt_count = sum(
        1 for rec in telemetry
        if type(rec) is dict
        and type((rec.get("request") or {}).get("user_prompt_full")) is str
    )
    return {
        "task_id": task_id,
        "scanned_prompt_count": prompt_count,
        "leakage_findings": [f.to_mapping() for f in findings],
        "passed": len(findings) == 0,
    }


def audit_evidence_file(
    evidence_path: Path,
    task_id: str,
    fixture_dir: Path,
) -> dict[str, Any]:
    with open(evidence_path, encoding="utf-8") as fh:
        evidence = json.load(fh)
    return audit_evidence_dict(evidence, task_id, fixture_dir)


def audit_matrix_dir(output_dir: Path, curated_root: Path) -> dict[str, Any]:
    """Audit every ``<output_dir>/<task_id>/evidence.json`` (matrix shape).

    Writes nothing; returns the aggregate audit for embedding in the
    matrix/review package.
    """
    per_task: dict[str, Any] = {}
    total_prompts = 0
    total_findings = 0
    for task_dir in sorted(output_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        evidence_path = task_dir / "evidence.json"
        if not evidence_path.is_file():
            continue
        task_id = task_dir.name
        audit = audit_evidence_file(
            evidence_path, task_id, curated_root / task_id
        )
        per_task[task_id] = audit
        total_prompts += audit["scanned_prompt_count"]
        total_findings += len(audit["leakage_findings"])
    return {
        "scanned_prompt_count": total_prompts,
        "leakage_findings_total": total_findings,
        "passed": total_findings == 0,
        "per_task": per_task,
    }


__all__ = [
    "ForbiddenContent",
    "LeakageFinding",
    "audit_evidence_dict",
    "audit_evidence_file",
    "audit_matrix_dir",
    "derive_forbidden_content",
    "scan_evidence",
    "scan_prompt",
]
