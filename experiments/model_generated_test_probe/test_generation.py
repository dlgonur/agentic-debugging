"""Model-generated regression test production for the S1-P probe.

This module is the test-generation half of the professor-requested probe:

    explicit expected behavior (PUBLIC BEHAVIOR SPEC)
    + buggy source
    -> frozen RAW model
    -> ONE executable pytest regression test
    -> freeze exact source + SHA-256 + raw-response provenance

It reuses the S1 experiment-local transport protocol
(``experiments.debugger_interaction_v2.adapter``) and the production task
loader / workspace / test runner. It does NOT touch the S1 bridge grammar,
the controller, or any production core.

Anti-leakage (enforced in :func:`build_generation_user_prompt`):
    * the oracle (root-cause / runtime-evidence hint) is stripped via
      ``DebugTask.agent_visible_mapping()``;
    * the existing fixture test source is never shown;
    * the existing failing/passing test NODE NAMES are never shown;
    * the fixed/gold source and gold patch are never shown.

The PUBLIC BEHAVIOR SPEC is intentionally supplied to the model. Generating a
test from a specified expected behavior is the treatment being measured; it is
NOT leakage for this auxiliary probe, and the model is NOT claimed to have
discovered the requirement.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.runtime.test_runner import TestRunner
from agentic_debugger.runtime.workspace import TaskWorkspace

from experiments.debugger_interaction_v2.adapter import (
    NOT_AVAILABLE,
    NOT_RECORDED,
    TransportError,
    TransportResponse,
)


# ---------------------------------------------------------------------------
# Frozen text assets (single source of truth; hashes recorded in the contract)
# ---------------------------------------------------------------------------

# The explicit public behavior contract. This is the treatment input.
BEHAVIOR_SPEC = (
    'The function format_display_name(name) must satisfy this public behavior '
    'contract:\n\n'
    '1. When name is None, it must return exactly the string "Anonymous".\n'
    '2. When name is a non-None string, it must trim surrounding whitespace and\n'
    '   title-case the result. If the trimmed value is empty, it must return\n'
    '   "Anonymous"; otherwise it must return the title-cased trimmed value.\n\n'
    'The function is defined in display_name.py and imported as:\n'
    '    from display_name import format_display_name'
)

SYSTEM_PROMPT_GENERATION = (
    "You are a test author. Given a Python function's specified expected "
    "behavior and its current source, write ONE self-contained pytest "
    "regression test that encodes the specified expected behavior. Output "
    "only a single fenced python code block. Do not output explanations. "
    "The test must import the function from the module under test "
    "(display_name) and assert the expected results. Do not include the "
    "function implementation."
)

SYSTEM_PROMPT_FIX = (
    "You are a repair agent. Given a Python function's specified expected "
    "behavior, its current buggy source, and a frozen regression test that "
    "encodes the expected behavior, produce a unified diff against "
    "display_name.py that makes the function satisfy the specified behavior "
    "and the regression test. Output only a single fenced diff code block. "
    "Do not modify the tests. Do not output explanations."
)

GENERATED_TEST_MODULE = "tests/test_generated_regression.py"
GENERATED_TEST_NODE = "tests/test_generated_regression.py"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


BEHAVIOR_SPEC_SHA256 = _sha256(BEHAVIOR_SPEC)
SYSTEM_PROMPT_GENERATION_SHA256 = _sha256(SYSTEM_PROMPT_GENERATION)
SYSTEM_PROMPT_FIX_SHA256 = _sha256(SYSTEM_PROMPT_FIX)


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def build_generation_user_prompt(
    task: DebugTask,
    buggy_source: str,
) -> str:
    """Build the model-facing test-generation prompt.

    Contents (anti-leakage-compliant):
        * PUBLIC BEHAVIOR SPEC (intentionally supplied — the treatment);
        * the buggy source of the target module (no fixed/gold code);
        * an instruction to emit one fenced python code block.

    Strictly excluded:
        * the oracle (root_cause_summary, runtime_evidence_hint, target_symbols
          beyond the public name) — stripped via agent_visible_mapping;
        * existing fixture test source;
        * existing failing/passing test node names;
        * fixed/gold source and gold patch.
    """

    # agent_visible_mapping() deletes the oracle and fixed_revision.
    visible = task.agent_visible_mapping()
    title = visible.get("title", "")
    description = visible.get("description", "")
    header_lines: list[str] = []
    if title:
        header_lines.append(f"Title: {title}")
    if description:
        header_lines.append(f"Description: {description}")
    header = "\n".join(header_lines)

    return textwrap.dedent(
        f"""\
        {header}

        PUBLIC BEHAVIOR SPEC:
        {BEHAVIOR_SPEC}

        CURRENT SOURCE of display_name.py (this version is BUGGY):
        ```python
        {buggy_source.rstrip()}
        ```

        Write ONE self-contained pytest regression test that encodes the
        PUBLIC BEHAVIOR SPEC above. The test must import format_display_name
        from display_name. Output only a single fenced python code block."""
    )


def build_fix_user_prompt(
    task: DebugTask,
    buggy_source: str,
    frozen_test_source: str,
) -> str:
    """Build the model-facing fix-generation prompt (one-shot).

    Contents:
        * PUBLIC BEHAVIOR SPEC (same frozen spec);
        * buggy source;
        * the exact frozen generated regression test;
        * an instruction to emit one fenced diff code block.

    Gold/fixed source remains hidden.
    """

    visible = task.agent_visible_mapping()
    title = visible.get("title", "")
    description = visible.get("description", "")
    header_lines: list[str] = []
    if title:
        header_lines.append(f"Title: {title}")
    if description:
        header_lines.append(f"Description: {description}")
    header = "\n".join(header_lines)

    return textwrap.dedent(
        f"""\
        {header}

        PUBLIC BEHAVIOR SPEC:
        {BEHAVIOR_SPEC}

        CURRENT BUGGY SOURCE of display_name.py:
        ```python
        {buggy_source.rstrip()}
        ```

        FROZEN REGRESSION TEST (tests/test_generated_regression.py) that the
        repair must satisfy:
        ```python
        {frozen_test_source.rstrip()}
        ```

        Produce a unified diff against display_name.py that makes the function
        satisfy the PUBLIC BEHAVIOR SPEC and the frozen regression test. Output
        only a single fenced diff code block. Do not modify tests."""
    )


# ---------------------------------------------------------------------------
# Deterministic extraction
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(
    r"```(?:python|py)?\s*\n(?P<body>.*?)```",
    re.DOTALL,
)


def extract_code_block(raw_text: str, *, fence: str = "```") -> str:
    """Extract the first fenced code block from raw model text.

    Accepts an optional language tag (```python ... ```). Falls back to the
    whole stripped response only if it contains no fence at all AND looks like
    a bare module (deterministic, fail-closed otherwise).

    Raises :class:`ExtractionError` if no usable code can be extracted.
    """

    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ExtractionError("empty_response", "raw text is empty")

    match = _FENCE_RE.search(raw_text)
    if match:
        body = match.group("body").strip("\r\n")
        if body:
            return body
        raise ExtractionError("empty_code_block", "fenced block is empty")

    # No fence: only accept a bare response that begins with a Python import or
    # a def/test def — anything else is ambiguous prose and must be rejected.
    stripped = raw_text.strip()
    first_line = stripped.splitlines()[0] if stripped.splitlines() else ""
    if first_line.startswith(("from ", "import ", "def test_", "import ")):
        return stripped
    raise ExtractionError(
        "no_code_block",
        "response has no fenced code block and does not look like a bare module",
    )


def extract_diff_block(raw_text: str) -> str:
    """Extract a fenced diff block from raw model text.

    Accepts ```diff ... ``` or ``` ... ```. Falls back to the whole stripped
    response only if it begins with a '--- ' diff header.
    """

    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ExtractionError("empty_response", "raw text is empty")

    # Prefer an explicit diff fence, then any fence.
    diff_fence = re.compile(r"```(?:diff|patch)?\s*\n(?P<body>.*?)```", re.DOTALL)
    match = diff_fence.search(raw_text)
    if match:
        body = match.group("body").strip("\r\n")
        if body:
            return body
        raise ExtractionError("empty_diff_block", "fenced diff block is empty")

    stripped = raw_text.strip()
    if stripped.startswith("--- "):
        return stripped
    raise ExtractionError(
        "no_diff_block",
        "response has no fenced diff and does not start with a diff header",
    )


class ExtractionError(Exception):
    """Raised when a code/diff block cannot be deterministically extracted."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail


# ---------------------------------------------------------------------------
# Frozen generated test record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenTest:
    """An executable, frozen model-generated regression test."""

    source: str
    sha256: str
    attempt_index: int

    # Raw response provenance (always retained).
    raw_response_text: str
    raw_response_sha256: str
    system_prompt_sha256: str
    user_prompt_sha256: str
    transport_error_category: Optional[str]
    usage: dict[str, Any]

    # Executability gate result (why this attempt was accepted as executable).
    executability: dict[str, Any]


# ---------------------------------------------------------------------------
# Executability gate
# ---------------------------------------------------------------------------


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


def check_executable(
    frozen_test_source: str,
    fixture_dir: Path,
    case_dir: Path,
    *,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Write the candidate test into a disposable workspace and run it.

    A test is "executable" iff pytest collects exactly one test node and runs
    it as a genuine PASS or FAIL (no collection error, no syntax/import error,
    no skips/xfail/errors, exactly one executed test).

    Returns a dict describing the executability outcome. Cleans up the
    disposable workspace.
    """

    workspace: Optional[TaskWorkspace] = None
    try:
        workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_dir))
        test_dir = Path(workspace.root) / "tests"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "test_generated_regression.py").write_text(
            frozen_test_source, encoding="utf-8"
        )
        runner = TestRunner(workspace)
        raw = runner.run_tests(
            argv=[
                "python", "-m", "pytest", GENERATED_TEST_NODE,
                "-q", "-p", "no:cacheprovider", "--no-header", "-vv",
            ],
            cwd=".",
            timeout_seconds=timeout_seconds,
        )
        cmd = raw.command_result
        output = (cmd.stdout or "") + "\n" + (cmd.stderr or "")
        counts = _parse_pytest_summary(output)
        exit_code = cmd.exit_code

        # Decide executability.
        executable = False
        reason = ""
        status = "unknown"
        if raw.timed_out:
            reason = "test_execution_timed_out"
        elif raw.launch_error or exit_code is None:
            reason = "launch_error"
        elif counts is None:
            reason = "malformed_pytest_summary"
        else:
            executed = counts["passed"] + counts["failed"]
            bad = (counts["errors"] + counts["skipped"]
                   + counts["xfailed"] + counts["xpassed"])
            if bad > 0:
                reason = (
                    f"non_genuine_test errors={counts['errors']} "
                    f"skipped={counts['skipped']} xfail={counts['xfailed']} "
                    f"xpass={counts['xpassed']}"
                )
            elif executed != 1:
                reason = f"expected exactly 1 executed test, got {executed}"
            else:
                executable = True
                status = "PASS" if counts["passed"] == 1 else "FAIL"
                reason = "executable"

        return {
            "executable": executable,
            "status": status,
            "reason": reason,
            "exit_code": exit_code,
            "timed_out": raw.timed_out,
            "counts": counts,
        }
    finally:
        if workspace is not None:
            workspace.cleanup()


# ---------------------------------------------------------------------------
# Retry-bounded generation
# ---------------------------------------------------------------------------


@dataclass
class GenerationOutcome:
    """The result of the retry-bounded test-generation phase."""

    frozen_test: Optional[FrozenTest]
    attempts: list[dict[str, Any]]
    stop_reason: str  # "frozen" | "no_executable_test" | "transport_failure"
    behavior_spec_sha256: str
    system_prompt_sha256: str
    anti_leakage: dict[str, bool] = field(default_factory=dict)


def generate_frozen_test(
    transport: Any,
    task: DebugTask,
    fixture_dir: Path,
    case_dir: Path,
    *,
    model_name: str,
    max_attempts: int = 3,
    request_timeout_seconds: float = 60.0,
    test_timeout_seconds: int = 20,
) -> GenerationOutcome:
    """Run the retry-bounded test-generation phase.

    Retries are triggered ONLY by:
        * non-extractable response (no code block); or
        * generated test is not executable (collection/syntax/import error,
          zero or >1 collected nodes, skip/error rather than PASS-or-FAIL).

    The system+user prompts are FROZEN (hashed). Only a deterministic
    ``feedback`` field changes between attempts. No prompt-tuning until pass.

    If an executable test is obtained but the buggy code PASSES it, that is NOT
    a retry trigger here — the freeze is returned and the caller records
    ``generated_test_did_not_encode_defect`` and STOPs.
    """

    buggy_source = (Path(fixture_dir) / "display_name.py").read_text(
        encoding="utf-8"
    )
    base_user_prompt = build_generation_user_prompt(task, buggy_source)
    sys_hash = SYSTEM_PROMPT_GENERATION_SHA256

    attempts: list[dict[str, Any]] = []
    feedback: Optional[str] = None

    for attempt in range(max_attempts):
        user_prompt = (
            base_user_prompt
            if feedback is None
            else base_user_prompt + f"\n\nPrevious attempt was not usable: {feedback}. Try again."
        )
        user_hash = _sha256(user_prompt)

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
            attempts.append(attempt_record)
            if attempt < max_attempts - 1:
                feedback = f"transport failure ({transport_error_cat})"
                continue
            return GenerationOutcome(
                frozen_test=None,
                attempts=attempts,
                stop_reason="transport_failure",
                behavior_spec_sha256=BEHAVIOR_SPEC_SHA256,
                system_prompt_sha256=sys_hash,
                anti_leakage=_anti_leakage_flags(),
            )

        # Extract a code block.
        try:
            test_source = extract_code_block(raw_text)
        except ExtractionError as exc:
            attempt_record["extraction"] = {
                "category": exc.category,
                "detail": exc.detail,
            }
            attempts.append(attempt_record)
            if attempt < max_attempts - 1:
                feedback = f"{exc.category}: {exc.detail}"
                continue
            return GenerationOutcome(
                frozen_test=None,
                attempts=attempts,
                stop_reason="no_executable_test",
                behavior_spec_sha256=BEHAVIOR_SPEC_SHA256,
                system_prompt_sha256=sys_hash,
                anti_leakage=_anti_leakage_flags(),
            )

        attempt_record["extraction"] = {"category": "ok", "detail": ""}

        # Executability gate (disposable workspace).
        exec_result = check_executable(
            test_source, fixture_dir, case_dir,
            timeout_seconds=test_timeout_seconds,
        )
        attempt_record["executability"] = exec_result

        if not exec_result["executable"]:
            attempts.append(attempt_record)
            if attempt < max_attempts - 1:
                feedback = f"generated test not executable: {exec_result['reason']}"
                continue
            return GenerationOutcome(
                frozen_test=None,
                attempts=attempts,
                stop_reason="no_executable_test",
                behavior_spec_sha256=BEHAVIOR_SPEC_SHA256,
                system_prompt_sha256=sys_hash,
                anti_leakage=_anti_leakage_flags(),
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
        attempts.append(attempt_record)
        return GenerationOutcome(
            frozen_test=frozen,
            attempts=attempts,
            stop_reason="frozen",
            behavior_spec_sha256=BEHAVIOR_SPEC_SHA256,
            system_prompt_sha256=sys_hash,
            anti_leakage=_anti_leakage_flags(),
        )

    # Should not reach here, but fail closed.
    return GenerationOutcome(
        frozen_test=None,
        attempts=attempts,
        stop_reason="no_executable_test",
        behavior_spec_sha256=BEHAVIOR_SPEC_SHA256,
        system_prompt_sha256=sys_hash,
        anti_leakage=_anti_leakage_flags(),
    )


def _anti_leakage_flags() -> dict[str, bool]:
    return {
        "oracle_shown_to_model": False,
        "fixed_or_gold_source_shown": False,
        "gold_patch_shown": False,
        "runtime_evidence_hint_shown": False,
        "existing_fixture_test_source_shown": False,
        "existing_test_node_names_shown": False,
        "behavior_spec_is_intentionally_shown": True,
    }


__all__ = [
    "BEHAVIOR_SPEC",
    "BEHAVIOR_SPEC_SHA256",
    "SYSTEM_PROMPT_GENERATION",
    "SYSTEM_PROMPT_GENERATION_SHA256",
    "SYSTEM_PROMPT_FIX",
    "SYSTEM_PROMPT_FIX_SHA256",
    "GENERATED_TEST_MODULE",
    "GENERATED_TEST_NODE",
    "FrozenTest",
    "GenerationOutcome",
    "ExtractionError",
    "build_generation_user_prompt",
    "build_fix_user_prompt",
    "extract_code_block",
    "extract_diff_block",
    "check_executable",
    "generate_frozen_test",
]