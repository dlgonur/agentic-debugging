"""R4 — model-generated regression test production (attempt-0 only).

This module is the test-generation half of the R4 professor-requested probe:

    agent-visible task statement (title + description, faithfully rendered)
    + buggy source
    -> frozen RAW Qwen2.5-Coder-7B-Instruct
    -> ONE pytest regression test
    -> FREEZE exact source + SHA-256 + raw-response provenance

It is a bounded reimplementation of the historical S1-P probe
(``experiments/model_generated_test_probe/``, provenance commit
``c47be60e6919626b6f431cd337d1d847a97f0722``, branch
``experiment/model-generated-test-probe``) with the R4 amendments:

* behavioral requirements are rendered ONLY from
  ``DebugTask.agent_visible_mapping()`` (title + description); no
  harness-authored behavioral spec is added;
* EXACTLY ONE generation call (attempt 0); no retries — any failure leaves
  R4 OPEN with the first causal boundary recorded;
* transport protocol is the tracked R3 adapter/transport
  (``experiments.debugger_interaction_v2_r3``), never the untracked S1/S1-P
  harness (see ``probe._check_import_boundaries``).

Anti-leakage (asserted on the FINAL rendered prompt in probe.py):
    * the oracle (root-cause / runtime-evidence hint / target symbols) is
      never rendered — only ``agent_visible_mapping()`` fields are used;
    * the existing fixture test source is never shown;
    * the existing failing/passing test NODE NAMES are never shown;
    * the fixed/gold source, R3 repair B/C, and gold patch are never shown.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.runtime.test_runner import TestRunner
from agentic_debugger.runtime.workspace import TaskWorkspace

from experiments.debugger_interaction_v2_r3.adapter import (
    NOT_AVAILABLE,
    NOT_RECORDED,
    TransportError,
    TransportResponse,
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Frozen text assets (single source of truth; hashes recorded in the contract)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_GENERATION = (
    "You are a test author. Given a Python function's task statement and its "
    "current source, write ONE self-contained pytest regression test that "
    "encodes the behavior described by the task statement. Output only a "
    "single fenced python code block. Do not output explanations. The test "
    "must import recent_window from the module under test (recent_window) "
    "and assert expected results. Do not include the function implementation."
)

SYSTEM_PROMPT_GENERATION_SHA256 = _sha256(SYSTEM_PROMPT_GENERATION)

GENERATED_TEST_MODULE = "tests/test_generated_regression.py"
GENERATED_TEST_NODE = "tests/test_generated_regression.py"


def render_task_spec_section(task: DebugTask) -> str:
    """Render the model-facing task/spec section from agent-visible fields only.

    R4 amendment 1: the behavioral requirements must be derived ONLY from
    information legitimately exposed by ``task.agent_visible_mapping()``.
    Only the ``title`` and ``description`` fields are rendered (the mapping's
    reproduction/tests/constraints/tags sections are never rendered, so test
    node names stay hidden). No behavior beyond those fields is added.
    """

    mapping = task.agent_visible_mapping()
    lines: list[str] = []
    title = mapping.get("title", "")
    description = mapping.get("description", "")
    if title:
        lines.append(f"Title: {title}")
    if description:
        lines.append(f"Description: {description}")
    return "\n".join(lines)


def build_generation_user_prompt(
    task: DebugTask,
    buggy_source: str,
) -> str:
    """Build the model-facing test-generation prompt.

    Contents (anti-leakage-compliant):
        * the task/spec section rendered from ``agent_visible_mapping``
          (title + description only — the treatment input);
        * the buggy source of the target module (no fixed/gold code);
        * the import contract and the output contract.

    Strictly excluded:
        * the oracle (root_cause_summary, runtime_evidence_hint,
          target_symbols) — never touched;
        * existing fixture test source;
        * existing failing/passing test node names;
        * fixed/gold source and gold patch;
        * the R3 repair B or normalized C.
    """

    spec_section = render_task_spec_section(task)

    # Explicit line-by-line assembly (no dedent): fully deterministic and
    # immune to multi-line spec-section indentation artifacts.
    return "\n".join([
        spec_section,
        "",
        "CURRENT SOURCE of recent_window.py (this version is BUGGY):",
        "```python",
        buggy_source.rstrip(),
        "```",
        "",
        "Write ONE self-contained pytest regression test that encodes the behavior described in the task statement above.",
        "The test must import recent_window from recent_window. Output only a single fenced python code block.",
    ])


# ---------------------------------------------------------------------------
# Deterministic extraction (framing)
# ---------------------------------------------------------------------------


class ExtractionError(Exception):
    """Raised when a code block cannot be deterministically extracted."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail


_FENCE_RE = re.compile(
    r"```(?:python|py)?\s*\n(?P<body>.*?)```",
    re.DOTALL,
)


def extract_code_block(raw_text: str) -> str:
    """Extract the first fenced python code block from raw model text.

    Deterministic framing only (R4 amendment 3): remove one surrounding
    markdown python fence and normalize the terminal newline. Falls back to
    the whole stripped response only if it contains no fence at all AND looks
    like a bare module (fail-closed otherwise).
    """

    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ExtractionError("empty_response", "raw text is empty")

    match = _FENCE_RE.search(raw_text)
    if match:
        body = match.group("body").strip("\r\n")
        if body:
            return body
        raise ExtractionError("empty_code_block", "fenced block is empty")

    stripped = raw_text.strip()
    first_line = stripped.splitlines()[0] if stripped.splitlines() else ""
    if first_line.startswith(("from ", "import ")):
        return stripped
    raise ExtractionError(
        "no_code_block",
        "response has no fenced code block and does not look like a bare module",
    )


# ---------------------------------------------------------------------------
# Frozen generated test record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenTest:
    """An executable, frozen model-generated regression test."""

    source: str  # T_parsed — parsed model-authored Python test body
    sha256: str
    attempt_index: int

    # Raw response provenance (always retained): T_raw.
    raw_response_text: str
    raw_response_sha256: str
    system_prompt_sha256: str
    user_prompt_sha256: str
    transport_error_category: Optional[str]
    usage: dict[str, Any]

    # Executability gate result (why this attempt was accepted as executable).
    executability: dict[str, Any]


@dataclass
class GenerationOutcome:
    """The result of the single-attempt test-generation phase."""

    frozen_test: Optional[FrozenTest]
    attempts: list[dict[str, Any]]
    stop_reason: str  # "frozen" | "no_executable_test" | "transport_failure"
    spec_section_sha256: str
    system_prompt_sha256: str
    anti_leakage: dict[str, bool] = field(default_factory=dict)


def _usage_dict(usage: Optional[dict[str, Any]]) -> dict[str, Any]:
    if usage is None or type(usage) is not dict:
        return {
            "prompt_tokens": NOT_RECORDED,
            "completion_tokens": NOT_RECORDED,
            "total_tokens": NOT_RECORDED,
            "provider_reported": False,
        }
    result: dict[str, Any] = {"provider_reported": True}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = usage.get(key)
        if type(val) is int and val >= 0:
            result[key] = val
        else:
            result[key] = NOT_RECORDED
    return result


def _parse_pytest_summary(output: str) -> Optional[dict[str, int]]:
    """Parse a pytest summary line into counts; return None if malformed."""
    text = output.lower()
    if "no tests ran" in text or "internalerror" in text:
        return None
    values = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0,
              "xfailed": 0, "xpassed": 0}
    found_any = False
    for line in output.splitlines():
        candidate = line.strip("= \t")
        m = re.match(r"^\s*(?P<body>.+?)\s+in\s+(?:\d+(?:\.\d+)?|<DURATION>)s\s*$",
                     candidate, re.IGNORECASE)
        if not m:
            continue
        found = 0
        for part in m.group("body").split(","):
            im = re.match(
                r"^\s*(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)\b",
                part, re.IGNORECASE)
            if im is None:
                continue
            number = int(im.group(1))
            label = im.group(2).lower()
            key = "errors" if label in {"error", "errors"} else label
            values[key] += number
            found += number
        if found:
            found_any = True
            break
    if not found_any:
        return None
    return values


def generate_frozen_test(
    transport: Any,
    task: DebugTask,
    fixture_dir: Path,
    case_dir: Path,
    *,
    model_name: str,
    request_timeout_seconds: float = 60.0,
    test_timeout_seconds: int = 20,
) -> GenerationOutcome:
    """Run the SINGLE-ATTEMPT test-generation phase (R4 amendment 2).

    Exactly one ``transport.request`` call (attempt 0). No retries, no
    prompt-tuning, no second model request. If attempt 0 cannot be parsed or
    is not executable, R4 stays OPEN and the first causal boundary is
    recorded via ``stop_reason`` + the attempt record.
    """

    buggy_source = (Path(fixture_dir) / "recent_window.py").read_text(
        encoding="utf-8"
    )
    user_prompt = build_generation_user_prompt(task, buggy_source)
    user_hash = _sha256(user_prompt)
    spec_section_sha = _sha256(render_task_spec_section(task))
    sys_hash = SYSTEM_PROMPT_GENERATION_SHA256

    attempt = 0
    raw_text: str
    raw_status: str
    transport_error_cat: Optional[str] = None
    usage: dict[str, Any]
    try:
        response: TransportResponse = transport.request(
            system_prompt=SYSTEM_PROMPT_GENERATION,
            user_prompt=user_prompt,
            timeout_seconds=request_timeout_seconds,
        )
        raw_text = response.raw_text
        raw_status = "decoded"
        usage = _usage_dict(response.usage)
    except TransportError as exc:
        raw_text = NOT_AVAILABLE
        raw_status = "transport_failure"
        transport_error_cat = exc.category
        usage = _usage_dict(None)
    except Exception as exc:  # noqa: BLE001 — fail-closed retention
        raw_text = NOT_AVAILABLE
        raw_status = "transport_failure"
        transport_error_cat = type(exc).__name__
        usage = _usage_dict(None)

    attempt_record: dict[str, Any] = {
        "attempt_index": attempt,
        "system_prompt_sha256": sys_hash,
        "user_prompt_sha256": user_hash,
        "raw_response_text": raw_text if raw_status == "decoded" else NOT_AVAILABLE,
        "raw_response_sha256": (
            _sha256(raw_text) if raw_status == "decoded" else NOT_AVAILABLE
        ),
        "raw_response_status": raw_status,
        "transport_error_category": transport_error_cat,
        "usage": usage,
        "extraction": None,
        "executability": None,
    }

    if raw_status == "transport_failure":
        attempt_record["extraction"] = {
            "category": "transport_failure",
            "detail": transport_error_cat,
        }
        return GenerationOutcome(
            frozen_test=None,
            attempts=[attempt_record],
            stop_reason="transport_failure",
            spec_section_sha256=spec_section_sha,
            system_prompt_sha256=sys_hash,
        )

    try:
        test_source = extract_code_block(raw_text)
    except ExtractionError as exc:
        attempt_record["extraction"] = {
            "category": exc.category,
            "detail": exc.detail,
        }
        return GenerationOutcome(
            frozen_test=None,
            attempts=[attempt_record],
            stop_reason="no_executable_test",
            spec_section_sha256=spec_section_sha,
            system_prompt_sha256=sys_hash,
        )

    attempt_record["extraction"] = {"category": "ok", "detail": ""}

    # Executability gate (disposable workspace; no patch).
    exec_result = check_executable(
        test_source, fixture_dir, case_dir,
        timeout_seconds=test_timeout_seconds,
    )
    attempt_record["executability"] = exec_result

    if not exec_result["executable"]:
        return GenerationOutcome(
            frozen_test=None,
            attempts=[attempt_record],
            stop_reason="no_executable_test",
            spec_section_sha256=spec_section_sha,
            system_prompt_sha256=sys_hash,
        )

    # Executable: FREEZE. Do not regenerate based on pass/fail.
    frozen = FrozenTest(
        source=test_source,
        sha256=_sha256(test_source),
        attempt_index=attempt,
        raw_response_text=raw_text,
        raw_response_sha256=_sha256(raw_text),
        system_prompt_sha256=sys_hash,
        user_prompt_sha256=user_hash,
        transport_error_category=None,
        usage=usage,
        executability=exec_result,
    )
    return GenerationOutcome(
        frozen_test=frozen,
        attempts=[attempt_record],
        stop_reason="frozen",
        spec_section_sha256=spec_section_sha,
        system_prompt_sha256=sys_hash,
    )


# ---------------------------------------------------------------------------
# Executability gate
# ---------------------------------------------------------------------------


def check_executable(
    frozen_test_source: str,
    fixture_dir: Path,
    case_dir: Path,
    *,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Write the candidate test into a disposable workspace and run it.

    A test is "executable" iff it compiles, pytest collects exactly one test
    node, and it runs as a genuine PASS or FAIL (no collection/syntax/import
    error, no skips/xfail/errors, exactly one executed test).

    Returns a dict describing the executability outcome. Cleans up the
    disposable workspace.
    """

    from experiments.model_generated_test_probe_r4.generated_test_runner import (
        run_structured_generated_test,
    )

    result = run_structured_generated_test(
        frozen_test_source,
        fixture_dir,
        case_dir,
        label="executability",
        candidate_patch=None,
        timeout_seconds=timeout_seconds,
    )
    executable = bool(
        result.compiled
        and result.collected == 1
        and not result.collect_error
        and not result.timed_out
        and not result.launch_error
        and result.exit_code is not None
        and result.counts is not None
        and result.counts["errors"] == 0
        and result.counts["skipped"] == 0
        and result.counts["xfailed"] == 0
        and result.counts["xpassed"] == 0
        and (result.counts["passed"] + result.counts["failed"]) == 1
        and not result.infrastructure_markers
    )
    status = "unknown"
    if executable:
        status = "PASS" if result.counts["passed"] == 1 else "FAIL"
    return {
        "executable": executable,
        "status": status,
        "reason": result.reason,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "counts": result.counts,
        "compiled": result.compiled,
        "collected": result.collected,
        "collect_error": result.collect_error,
    }


__all__ = [
    "SYSTEM_PROMPT_GENERATION",
    "SYSTEM_PROMPT_GENERATION_SHA256",
    "GENERATED_TEST_MODULE",
    "GENERATED_TEST_NODE",
    "FrozenTest",
    "GenerationOutcome",
    "ExtractionError",
    "render_task_spec_section",
    "build_generation_user_prompt",
    "extract_code_block",
    "check_executable",
    "generate_frozen_test",
    "_parse_pytest_summary",
    "_sha256",
]
