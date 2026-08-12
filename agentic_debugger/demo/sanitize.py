"""Common deterministic diagnostic sanitizer (R5.9 clean-holdout treatment).

Distinguishes REAL DIAGNOSTIC SIGNAL from HIDDEN TEST ANSWER CONTENT in
runtime test output before anything reaches a model prompt.

REAL DIAGNOSTIC SIGNAL (may be forwarded):
- production exception class + message originating from production code;
- production traceback summary lines (``<script>.py:<line>: <Class>``)
  inside the original production source region;
- the generic behavioral-failure statement.

NEVER FORWARDED:
- hidden test source code, ``def test_...`` bodies, assertion expressions,
  expected hidden literals, hidden F2P/P2P node ids, test filenames or
  function names, pytest source excerpts, exact expected-vs-actual diffs,
  oracle fields, reference repairs, RuntimeProbe semantic hints.

All extraction is mechanical and deterministic (regular expressions over
the normalized output; no inference).  When no safe production diagnostic
can be derived the result fails closed to the generic behavioral-failure
statement — a diagnostic that cannot be separated safely from hidden-test
content is never forwarded.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

from agentic_debugger.evaluation.runner import normalize_output

#: Maximum characters of the sanitized structured diagnostic.
MAX_SANITIZED_DIAGNOSTIC_CHARS = 1200

#: Maximum characters of the RAW reproduction output retained in the
#: evidence payload (audit-only; never rendered into a model prompt).
MAX_RAW_FAILURE_OUTPUT_CHARS = 4000

# Generic fail-closed statement for a failure that cannot be separated
# safely from hidden-test content (goal-sanctioned shape).
GENERIC_BEHAVIORAL_FAILURE = (
    "baseline behavioral check failed after executing the target behavior"
)

#: pytest longrepr exception line: ``E <body>``.
_E_LINE_RE = re.compile(r"^\s*E\s+(?P<body>.+)$")

#: Exception class name token (``AttributeError``, ``NameError``, ...).
_CLASS_RE = r"[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)"

#: ``<class>: <message>`` on an ``E`` line.
_E_EXCEPTION_RE = re.compile(
    rf"^(?P<cls>{_CLASS_RE})(?P<sep>:\s*)(?P<msg>.+)$"
)

#: pytest failure summary line: ``<path>.py:<line>: <Class>``.
_SUMMARY_LINE_RE = re.compile(
    rf"^(?P<path>.+?\.py):(?P<line>\d+):\s*(?P<cls>{_CLASS_RE})\s*$"
)

#: Assertion rewriting reports the failed assert as ``E assert ...``; a
#: production ``assert`` surfaces as ``E AssertionError: ...``.  Both are
#: hidden-test-adjacent (or candidate-authored) and fail closed.
_FORBIDDEN_EXCEPTION_CLASSES = frozenset({"AssertionError"})


@dataclass(frozen=True)
class ProductionException:
    """One mechanically extracted production exception report."""

    path: str
    line: int
    cls: str
    message: Optional[str] = None

    def summary(self) -> str:
        """``<path>:<line>: <Class>`` — the production traceback frame."""
        return f"{self.path}:{self.line}: {self.cls}"

    def full(self) -> str:
        """Summary plus the production-originated message when available."""
        if self.message:
            return f"{self.summary()}: {self.message}"
        return self.summary()


@dataclass(frozen=True)
class SanitizedDiagnostic:
    """The bounded structured diagnostic produced by the sanitizer."""

    text: str
    production_exception: Optional[ProductionException]
    #: SHA-256 of the normalized raw input the sanitizer consumed
    #: (auditability of the mechanical derivation).
    raw_normalized_sha256: str


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/")


def _is_production_path(path: str, script_path: str) -> bool:
    path = _normalize_path(path)
    script = _normalize_path(script_path)
    return path.endswith(script) or script.endswith(path)


def _safe_exception_message(cls: str, message: str) -> Optional[str]:
    """Fail closed when the exception message is assertion-derived or empty.

    A real production exception message is the runtime string produced by
    the exception itself; assertion-rewrite content (``assert ...``,
    expected-vs-actual diffs) is hidden-test answer content and is never
    forwarded.
    """
    if cls in _FORBIDDEN_EXCEPTION_CLASSES:
        return None
    stripped = message.strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    if lowered.startswith("assert") or " assert " in lowered:
        return None
    if lowered.startswith("where "):
        # pytest assertion-diff continuation lines.
        return None
    if lowered.startswith("+"):
        # pytest assertion-diff ``+ where`` continuation lines.
        return None
    return stripped


def extract_production_exception(
    output_text: str,
    script_path: str,
    original_line_count: Optional[int] = None,
) -> Optional[ProductionException]:
    """Mechanically extract the LAST safe production exception report.

    Scans pytest-style output for ``E <Class>: <message>`` lines (the
    exception as reported by the runtime) and the matching
    ``<script>.py:<line>: <Class>`` summary frame.  The last matching
    exception wins (pytest places the deepest failure last).  Returns
    ``None`` (fail closed) when no safe production exception exists.
    """
    if type(output_text) is not str or not output_text:
        return None
    if type(script_path) is not str or not script_path:
        return None

    exception: Optional[tuple[str, Optional[str]]] = None  # (cls, message)
    for line in output_text.splitlines():
        match = _E_LINE_RE.match(line)
        if match is None:
            continue
        body = match.group("body").strip()
        ematch = _E_EXCEPTION_RE.match(body)
        if ematch is None:
            continue
        message = _safe_exception_message(
            ematch.group("cls"), ematch.group("msg")
        )
        if message is None:
            continue
        exception = (ematch.group("cls"), message)

    if exception is None:
        return None

    exc_cls, message = exception
    # The production summary frame: the last in-region summary line whose
    # class matches the extracted exception class.
    summary: Optional[tuple[str, int, str]] = None
    for line in output_text.splitlines():
        smatch = _SUMMARY_LINE_RE.match(line.strip())
        if smatch is None:
            continue
        if smatch.group("cls") != exc_cls:
            continue
        if not _is_production_path(smatch.group("path"), script_path):
            continue
        line_no = int(smatch.group("line"))
        if original_line_count is not None and not (
            1 <= line_no <= original_line_count
        ):
            continue
        summary = (smatch.group("path"), line_no, exc_cls)

    if summary is None:
        # The production frame is required for the structured diagnostic;
        # a bare class without its production frame fails closed.
        return None
    path, line_no, cls = summary
    return ProductionException(
        path=path, line=line_no, cls=cls, message=message
    )


def sanitize_failure_output(
    raw: str,
    workspace_root: Optional[str],
    script_path: str,
    original_line_count: Optional[int] = None,
) -> SanitizedDiagnostic:
    """Derive the bounded structured diagnostic from raw test output.

    ``raw`` is the stdout/stderr of the executed reproduction command.
    The returned text is what a model MAY receive: either the structured
    production-exception diagnostic or the generic behavioral-failure
    statement.  Never test source, assertions, node ids, or literals.
    """
    if type(raw) is not str:
        raw = ""
    normalized = normalize_output(raw, workspace_root)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    exception = extract_production_exception(
        normalized, script_path, original_line_count
    )
    if exception is None:
        text = GENERIC_BEHAVIORAL_FAILURE
    else:
        text = "baseline failure reproduced\nproduction exception:\n"
        text += f"  {exception.summary()}\n"
        if exception.message:
            text += f"  {exception.cls}: {exception.message}\n"
    if len(text) > MAX_SANITIZED_DIAGNOSTIC_CHARS:
        text = text[: MAX_SANITIZED_DIAGNOSTIC_CHARS - 3] + "..."
    return SanitizedDiagnostic(
        text=text,
        production_exception=exception,
        raw_normalized_sha256=digest,
    )


def sanitize_verifier_failure_output(
    raw: str,
    script_path: str,
    original_line_count: Optional[int] = None,
) -> Optional[str]:
    """Sanitized production exception of one failing verifier record.

    Returns the full ``<path>:<line>: <Class>: <message>`` production
    exception (``None`` when only hidden-test content explains the
    failure).  ``raw`` is the record's bounded stdout/stderr; the same
    deterministic extractor as the reproduction path is used — one common
    sanitizer for both dynamic paths.
    """
    if type(raw) is not str or not raw:
        return None
    normalized = normalize_output(raw, None)
    exception = extract_production_exception(
        normalized, script_path, original_line_count
    )
    if exception is None:
        return None
    return exception.full()


__all__ = [
    "GENERIC_BEHAVIORAL_FAILURE",
    "MAX_RAW_FAILURE_OUTPUT_CHARS",
    "MAX_SANITIZED_DIAGNOSTIC_CHARS",
    "ProductionException",
    "SanitizedDiagnostic",
    "extract_production_exception",
    "sanitize_failure_output",
    "sanitize_verifier_failure_output",
]
