"""Render the human-readable technical summary from the machine-readable data.

The Markdown summary is generated *from* ``results.json`` rather than written
alongside it, so the two documents cannot disagree.  Every number below is read
out of the result mapping; this module contains no measurement of its own.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from agentic_debugger.demo.catalog import scenario_for, scenario_ids
from agentic_debugger.demo.runner import (
    NONDETERMINISTIC_RESULT_KEYS,
    RUNTIME_ATTRIBUTION_NOTE,
)

#: Limitations that hold for every Task 9 demonstration run.
STANDING_LIMITATIONS: tuple[str, ...] = (
    "There is no model in the loop. The localization claim, root-cause "
    "statement, candidate diff and debugger probe are fixed catalog data, so "
    "the demonstration measures the platform, not repair capability.",
    "Both policy variants receive the identical candidate diff for a task. "
    "Repair outcomes are therefore expected to match by construction and "
    "cannot be read as evidence about debugger usefulness.",
    "The curated set is five small single-file Python defects. Nothing here "
    "generalizes to repository-scale tasks, multi-file repairs or SWE-bench.",
    "Execution uses the trusted-local workspace boundary. It is not an "
    "OS-level hostile-code sandbox.",
    "Only the static baseline and the on-uncertainty PDB gate are exercised. "
    "The always-on and after-failed-patch gate variants remain unexercised.",
    "Localization is scored from the model's declared claim plus the files the "
    "patch actually changed. It does not verify statement-level fault "
    "localization inside the symbol.",
    "Token and cost metrics are absent because no metered service is used.",
    "The debugger never pauses inside the failing test. It attaches to a "
    "second disposable copy of the fixture carrying an appended driver that "
    "calls the focus function with catalog-supplied arguments, so runtime "
    "evidence describes a curated stand-in reproduction, not the F2P run.",
    "The breakpoint anchor and the probe expressions were authored with "
    "knowledge of each defect, so the debugger is guaranteed to pause on the "
    "relevant line. That is scaffolding, not a capability result.",
    "The localization claim is pinned to the task oracle by an automated "
    "test, so a clean run can only ever score CORRECT_TARGET_SYMBOL.",
    "The curated fixtures are synthetic and partly instrumented for "
    "observability. Two reference repairs also remove planted assertion "
    "scaffolding, so they are not minimal real-world bug fixes.",
    "Controller-side validation aggregates all pass-to-pass tests into one "
    "batched pytest exit code. Per-node evidence comes only from the "
    "verifier, which is therefore the authoritative source.",
    "Cost is reported as call and command counts. No per-action latency is "
    "measured at any granularity below per-case wall clock.",
)

#: Comparisons the produced data cannot support, stated explicitly.
UNSUPPORTED_CLAIMS: tuple[str, ...] = (
    "PDB-enabled runs repair more curated bugs than the static baseline.",
    "Runtime evidence improved localization, diagnosis or patch correctness.",
    "The observed cost difference between the policies predicts cost on any "
    "other task distribution.",
    "The demonstration establishes any causal effect of debugger access.",
)


def _row(values: Sequence[str]) -> str:
    return "| " + " | ".join(values) + " |"


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    lines = [_row(headers), _row(["---"] * len(headers))]
    lines.extend(_row(row) for row in rows)
    return lines


def _flag(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "n/a"


def _text(value: Any) -> str:
    return "n/a" if value is None else str(value)


def _task_aware_limitations(cases: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Keep historical limitations truthful for mixed or proof-task reports."""

    exact_task_ids = {
        task_id
        for task_id in scenario_ids()
        if scenario_for(task_id).runtime_probe.exact_public_reproduction
    }
    exact = tuple(
        case.get("task_id")
        for case in cases
        if case.get("task_id") in exact_task_ids
    )
    if not exact:
        return STANDING_LIMITATIONS
    values = list(STANDING_LIMITATIONS)
    values[2] = (
        f"This report covers {len(cases)} executed case(s), including the opt-in "
        "single-task exact-runtime proof fixture. It does not generalize to "
        "repository-scale tasks, multi-file repairs or SWE-bench."
    )
    values[7] = (
        "For the exact-runtime proof fixture, the bounded PDB worker executes "
        "the declared public failing pytest node in the same disposable task "
        "workspace. Other legacy catalog tasks, if present, retain the older "
        "appended-driver probe and must not be interpreted as exact-test PDB "
        "evidence."
    )
    return tuple(values)


def render_summary(results: Mapping[str, Any], *, entry_point: str) -> str:
    """Render the complete technical evaluation summary."""

    environment = results.get("environment", {})
    aggregates = results.get("aggregates", {})
    cases: Sequence[Mapping[str, Any]] = results.get("cases", ())
    lines: list[str] = []

    lines += [
        "# Task 9 — First End-to-End Demonstration: Technical Evaluation Summary",
        "",
        "This document is generated from `results.json` by "
        "`agentic_debugger.demo.report`. Every figure below is read out of that "
        "file, which is itself produced by executing the integrated MVP. No "
        "outcome in this report is hand-authored.",
        "",
        "## 1. Tested state",
        "",
    ]
    lines += _table(
        ("Field", "Value"),
        [
            ("Git branch", _text(environment.get("git_branch"))),
            ("Git HEAD", _text(environment.get("git_head"))),
            ("Working tree dirty", _flag(environment.get("git_working_tree_dirty"))),
            ("Working-tree status digest", _text(environment.get("git_status_digest"))),
            ("`agentic_debugger/` source digest", _text(environment.get("source_tree_sha256"))),
            ("Python", _text(environment.get("python_version"))),
            ("Platform", _text(environment.get("platform"))),
            ("Model backend", _text(environment.get("model_backend"))),
            ("Model-provider policy", _text(environment.get("model_provider_policy"))),
            ("Network-access policy", _text(environment.get("network_access_policy"))),
            ("Generated (UTC)", _text(environment.get("generated_utc"))),
        ],
    )
    lines += [
        "",
        "The Git HEAD identifies the committed baseline; the source digest "
        "identifies the exact `agentic_debugger/` bytes that were executed, "
        "including uncommitted changes. The two policy rows are stated policy, "
        "not measurements; the measured offline counters are in section 4.",
        "",
        "## 2. Reproducing this run",
        "",
        "From a clean checkout of this repository, with Python >= 3.11 and "
        "`pytest` installed:",
        "",
        "```powershell",
        entry_point,
        "```",
        "",
        "The demonstration is offline by default: it constructs no provider "
        "client and opens no socket. Measured provider and network attempt "
        "counts are reported per case in `results.json` and aggregated in "
        "section 4.",
        "",
        "## 3. Complete curated-set results",
        "",
        f"{aggregates.get('case_count', 0)} case(s) over "
        f"{aggregates.get('task_count', 0)} curated task(s).",
        "",
    ]

    rows = []
    for case in cases:
        verifier = case.get("verifier", {})
        controller = case.get("controller", {})
        runtime = case.get("runtime_evidence", {})
        rows.append(
            (
                case.get("task_id", "?"),
                case.get("policy", "?"),
                _text(controller.get("final_state")),
                _text(verifier.get("status")),
                _text(verifier.get("outcome")),
                f"{_text(verifier.get('f2p_passed'))}/{_text(verifier.get('f2p_total'))}",
                f"{_text(verifier.get('p2p_passed'))}/{_text(verifier.get('p2p_total'))}",
                _text(verifier.get("full_suite_status")),
                _text(case.get("localization", {}).get("outcome")),
                _text(runtime.get("outcome")),
            )
        )
    lines += _table(
        (
            "Task",
            "Policy",
            "Controller",
            "Verifier status",
            "Outcome",
            "F2P",
            "P2P",
            "Full suite",
            "Localization",
            "Runtime evidence",
        ),
        rows,
    )

    lines += [
        "",
        "`Verifier status` and `Outcome` come from the Task 7 verifier, which "
        "re-evaluates the candidate diff from an independent clean baseline "
        "workspace. The controller's own validation classification is recorded "
        "separately per case in `results.json` under `controller_validation`.",
        "",
        "### 3.1 Cost and debugger-use measurements",
        "",
    ]
    cost_rows = []
    for case in cases:
        controller = case.get("controller", {})
        runtime = case.get("runtime_evidence", {})
        budget = controller.get("budget_state") or {}
        cost_rows.append(
            (
                case.get("task_id", "?"),
                case.get("policy", "?"),
                _text(controller.get("model_calls")),
                _text(controller.get("tool_call_count")),
                _text(budget.get("test_runs")),
                _text(budget.get("source_observations")),
                _text(budget.get("pdb_observations")),
                _text(runtime.get("pdb_observations_succeeded")),
                _text(runtime.get("pdb_action_count")),
                _text(_verification_command_count(case)),
            )
        )
    lines += _table(
        (
            "Task",
            "Policy",
            "Model calls",
            "Tool calls",
            "Controller test runs",
            "Source obs.",
            "PDB obs. (charged)",
            "PDB obs. (succeeded)",
            "PDB actions",
            "Verifier commands",
        ),
        cost_rows,
    )
    lines += [
        "",
        "Model calls are calls into the deterministic offline adapter, not "
        "provider requests. No token or cost figures exist because no metered "
        "service is used.",
        "",
        "Wall-clock durations are recorded in `results.json` under `timing` and "
        "are deliberately excluded from the deterministic comparison.",
        "",
        "## 4. Static baseline versus PDB-enabled policy",
        "",
    ]

    policy_rows = []
    for bucket in aggregates.get("by_policy", ()):
        policy_rows.append(
            (
                _text(bucket.get("policy")),
                _text(bucket.get("pdb_policy")),
                _text(bucket.get("cases")),
                _text(bucket.get("controller_done")),
                _text(bucket.get("verifier_completed")),
                _format_counts(bucket.get("verifier_outcomes", {})),
                f"{_text(bucket.get('f2p_passed'))}/{_text(bucket.get('f2p_total'))}",
                f"{_text(bucket.get('p2p_passed'))}/{_text(bucket.get('p2p_total'))}",
                _text(bucket.get("model_calls")),
                _text(bucket.get("tool_calls")),
                _text(bucket.get("pdb_observation_attempts")),
            )
        )
    lines += _table(
        (
            "Policy",
            "PDB gate",
            "Cases",
            "Controller Done",
            "Verifier COMPLETED",
            "Verifier outcomes",
            "F2P",
            "P2P",
            "Model calls",
            "Tool calls",
            "PDB obs. (attempted)",
        ),
        policy_rows,
    )
    parity = aggregates.get("patch_parity", {})
    parity_rows = [
        (
            _text(item.get("task_id")),
            ", ".join(item.get("policies_compared", ())) or "n/a",
            _text(item.get("distinct_patch_digests")),
            _flag(item.get("identical_across_policies")),
        )
        for item in parity.get("tasks", ())
    ]
    lines += [
        "",
        "#### Candidate-patch parity (derived from the recorded SHA-256 values)",
        "",
    ]
    lines += _table(
        ("Task", "Policies compared", "Distinct patch digests", "Identical"),
        parity_rows,
    )
    lines += [
        "",
        "Every comparable task identical: "
        f"{_flag(parity.get('all_comparable_tasks_identical'))} "
        f"({_text(parity.get('comparable_task_count'))} comparable task(s)).",
    ]

    # Read the stored totals rather than recomputing them, so every printed
    # figure has a counterpart in results.json that can be cross-checked.
    offline = aggregates.get("offline", {})
    provider_total = _text(offline.get("provider_attempts"))
    network_total = _text(offline.get("network_attempts"))
    lines += [
        "",
        "#### Offline enforcement (measured)",
        "",
        "An in-process guard intercepts, counts and refuses outbound socket use "
        "and imports of recognised model-provider SDKs for the duration of every "
        "case. Across all cases it recorded "
        f"**{provider_total}** refused provider-SDK import(s) and "
        f"**{network_total}** refused network attempt(s).",
        "",
        f"Guard scope: {_text(offline.get('guard_scope'))}",
        "",
        "### 4.1 What this comparison supports",
        "",
        "* Both policy variants execute end to end through the same controller, "
        "tool registry, runtime, patch lifecycle and verifier.",
        "* The PDB gate decision is taken by the accepted "
        "`decide_pdb_access` policy over live budget, reproduction and "
        "hypothesis state, and each decision is recorded per case. Note that "
        "the offline model always declares a low-confidence hypothesis that "
        "requires runtime state, so the on-uncertainty arm is never observed "
        "denying access; only the disabled arm is observed denying it.",
        "* The measured cost difference between the variants (model calls, tool "
        "calls, PDB observations) is real for this curated set and this fixed "
        "plan.",
        "",
        "### 4.2 What this comparison does not support",
        "",
        f"* {RUNTIME_ATTRIBUTION_NOTE}",
    ]
    lines += [f"* {claim}" for claim in UNSUPPORTED_CLAIMS]

    lines += ["", "## 5. Failures, rejections and incomplete runs", ""]
    lines += _render_problems(cases)

    lines += ["", "## 6. Limitations, assumptions and deferred work", ""]
    lines += [f"* {item}" for item in _task_aware_limitations(cases)]
    lines += [
        "",
        "Deliberately deferred: real-model execution, the always-on and "
        "after-failed-patch PDB gates, BugsInPy or SWE-bench ingestion, "
        "multi-attempt repair loops, hostile-code containment, and any "
        "fine-tuning, RAG or preference-optimization work.",
        "",
        "## 7. Determinism",
        "",
        "Repeated runs of the same working tree produce byte-identical "
        f"`results.json` outside the top-level keys "
        f"{list(NONDETERMINISTIC_RESULT_KEYS)}:",
        "",
        "* `environment` identifies the tested tree, interpreter and generation "
        "time, and changes by design.",
        "* `timing` holds wall-clock measurements, which vary with machine load.",
        "",
        "One caveat: several deterministic fields are outcomes of real pytest "
        "subprocesses (`verifier.f2p_passed`, `full_suite_status`, `timeout`). "
        "On a machine loaded enough to hit a task timeout those values change. "
        "Byte stability is a healthy-machine property, not a guarantee.",
        "",
        "The raw event trajectories under `trajectories/*.events.jsonl` carry "
        "real UTC timestamps and are therefore not byte-stable. The semantic "
        "projections under `trajectories/*.semantic.json` are stable: replay "
        "strips timestamps and duration metadata and aliases run-scoped "
        "identifiers.",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _verification_command_count(case: Mapping[str, Any]) -> Any:
    return case.get("verifier", {}).get("verification_command_count")


def _format_counts(counts: Mapping[str, Any]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{name}={counts[name]}" for name in sorted(counts))


def _render_problems(cases: Sequence[Mapping[str, Any]]) -> list[str]:
    problems: list[str] = []
    for case in cases:
        case_id = case.get("case_id", "?")
        if case.get("status") != "COMPLETED":
            problems.append(f"* `{case_id}`: case status {case.get('status')}.")
        controller = case.get("controller", {})
        if controller.get("final_state") is None:
            problems.append(f"* `{case_id}`: the controller never ran.")
        elif controller.get("final_state") != "Done":
            problems.append(
                f"* `{case_id}`: controller stopped in "
                f"{_text(controller.get('final_state'))} "
                f"({_text(controller.get('stop_reason'))})."
            )
        verifier = case.get("verifier", {})
        if not verifier.get("executed"):
            problems.append(f"* `{case_id}`: verifier not executed — {_text(verifier.get('note'))}.")
        elif verifier.get("status") != "COMPLETED":
            problems.append(
                f"* `{case_id}`: verifier status {_text(verifier.get('status'))} "
                f"({_text(verifier.get('stop_reason'))})."
            )
        elif verifier.get("outcome") != "RESOLVED":
            problems.append(
                f"* `{case_id}`: verifier outcome {_text(verifier.get('outcome'))}."
            )
        for error in case.get("tool_errors", ()):
            problems.append(
                f"* `{case_id}`: tool `{error.get('action')}` reported "
                f"{error.get('diagnostic')}"
            )
        for diagnostic in case.get("diagnostics", ()):
            problems.append(f"* `{case_id}`: {diagnostic}")
        offline = case.get("offline", {})
        if offline.get("network_attempts") or offline.get("provider_attempts"):
            problems.append(
                f"* `{case_id}`: offline guard refused "
                f"{_text(offline.get('network_attempts'))} network and "
                f"{_text(offline.get('provider_attempts'))} provider attempt(s)."
            )
        if offline.get("canonical_fixture_unchanged_by_controller") is False:
            problems.append(
                f"* `{case_id}`: the canonical fixture changed during the "
                "controller phase."
            )
        controller_outcome = case.get("controller_validation", {}).get("outcome")
        verifier_outcome = case.get("verifier", {}).get("outcome")
        if (
            controller_outcome is not None
            and verifier_outcome is not None
            and controller_outcome != verifier_outcome
        ):
            problems.append(
                f"* `{case_id}`: controller classified {controller_outcome} but the "
                f"verifier classified {verifier_outcome}."
            )
        trajectory = case.get("trajectory", {})
        if not trajectory.get("replay_valid"):
            problems.append(
                f"* `{case_id}`: trajectory replay not validated — "
                f"{_text(trajectory.get('replay_error')) if trajectory.get('replay_error') else 'no events were produced'}."
            )
        rejected = (trajectory.get("observation_status_counts") or {}).get("rejected", 0)
        errored = (trajectory.get("observation_status_counts") or {}).get("error", 0)
        timed_out = (trajectory.get("observation_status_counts") or {}).get("timeout", 0)
        if rejected or errored or timed_out:
            problems.append(
                f"* `{case_id}`: observations rejected={rejected} error={errored} "
                f"timeout={timed_out}."
            )
    if not problems:
        return [
            "No case reported a failure, rejection, incomplete run or tool "
            "error. Every case completed, its trajectory replayed cleanly, and "
            "the verifier produced a full result."
        ]
    return problems


__all__ = [
    "STANDING_LIMITATIONS",
    "UNSUPPORTED_CLAIMS",
    "render_summary",
]
