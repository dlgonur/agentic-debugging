"""Fail-closed leakage checks for model-facing SWE-rebench context."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from agentic_debugger.evaluation.task_schema import (
    HIDDEN_TEST_PLACEHOLDER,
    DebugTask,
)

FORBIDDEN_MODEL_FIELD_NAMES = frozenset(
    {
        "patch",
        "test_patch",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "fail_to_pass",
        "pass_to_pass",
        "interface",
        "pr_description",
        "gold",
        "gold_patch",
        "reference_patch",
        "oracle",
        "oracle_localization",
        "target_files",
        "target_symbols",
        "root_cause_summary",
        "runtime_evidence_hint",
        "fixed_revision",
        "meta",
        "llm_metadata",
        "reasoning",
        "install_config",
        "image_name",
        "test_cmd",
        "professor",
        "r5_trajectory",
        "r6_trajectory",
    }
)

FORBIDDEN_SUBSTRINGS = (
    "### Target",
    "PATCH\n",
    "oracle-file-localized",
    "gold repair",
    "correct_python_programs",
)


def _walk(value: Any, path: str, hits: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child = f"{path}.{key_text}" if path else key_text
            if key_text in FORBIDDEN_MODEL_FIELD_NAMES:
                if key_text in {"fail_to_pass", "pass_to_pass"}:
                    if (
                        isinstance(item, list)
                        and item == [HIDDEN_TEST_PLACEHOLDER]
                    ):
                        continue
                hits.append(child)
                continue
            _walk(item, child, hits)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]", hits)
        return
    if isinstance(value, str):
        for needle in FORBIDDEN_SUBSTRINGS:
            if needle in value:
                hits.append(f"{path} contains {needle!r}")


def scan_mapping_for_leakage(mapping: Mapping[str, Any]) -> tuple[str, ...]:
    hits: list[str] = []
    _walk(mapping, "", hits)
    return tuple(hits)


def _without_public_issue_text(mapping: Mapping[str, Any]) -> dict[str, Any]:
    redacted = dict(mapping)
    redacted.pop("description", None)
    redacted.pop("title", None)
    redacted.pop("problem_statement", None)
    return redacted


def assert_model_facing_isolated(
    mapping: Mapping[str, Any],
    *,
    hidden_needles: Iterable[str] = (),
) -> None:
    hits = list(scan_mapping_for_leakage(mapping))
    serialized = repr(_without_public_issue_text(mapping))
    for needle in hidden_needles:
        if needle and needle in serialized:
            hits.append(f"hidden needle present: {needle!r}")
    if hits:
        raise ValueError(
            "model-facing context is not isolated: " + "; ".join(hits[:12])
        )


def hidden_needles_from_private(private: Mapping[str, Any]) -> tuple[str, ...]:
    needles: list[str] = []
    gold = private.get("patch")
    if isinstance(gold, str) and gold.strip():
        for line in gold.splitlines():
            stripped = line.strip()
            if stripped.startswith("+") and not stripped.startswith("+++"):
                body = stripped[1:].strip()
                if len(body) >= 16:
                    needles.append(body)
                    if len(needles) >= 8:
                        break
    test_patch = private.get("test_patch")
    if isinstance(test_patch, str) and test_patch.strip():
        needles.append(test_patch.strip()[:80])
    for key in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        values = private.get(key) or []
        if isinstance(values, list):
            needles.extend(str(item) for item in values if item)
    return tuple(item for item in needles if item)


def assert_task_model_view_isolated(
    task: DebugTask, *, hidden_needles: Iterable[str] = ()
) -> None:
    assert_model_facing_isolated(
        task.agent_visible_mapping(), hidden_needles=hidden_needles
    )
    if "oracle" in task.agent_visible_mapping():
        raise ValueError("oracle leaked into agent-visible mapping")
