"""Structural checks for the professor-facing navigation layer.

These tests read the shipped README, results index, family notes, and
``.gitignore``. They do not invent scientific numbers: they assert that the
accepted wording and evidence paths already present in those documents
remain discoverable after repository curation.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
RESULTS_INDEX = (REPO_ROOT / "docs" / "results-index.md").read_text(encoding="utf-8")
GITIGNORE = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")


FAMILY_ENTRY_POINTS = {
    "r1": "experiments/debugger_interaction_v2_r1/README.md",
    "r2": "experiments/debugger_interaction_v2_r2/README.md",
    "r3": "experiments/debugger_interaction_v2_r3/README.md",
    "r4": "experiments/model_generated_test_probe_r4/README.md",
    "r5": "experiments/debugger_interaction_v2_r5/README.md",
    "r6": "experiments/r6_debugger_training/README.md",
    "s4_rag": "experiments/cp118_rag_definitive/RESULT.md",
    "s4_readme": "experiments/cp118_rag_definitive/README.md",
    "tuned_pilot": "experiments/tuned_debugger_pilot_v1/README.md",
    "local_inference": "experiments/local_inference_perf/README.md",
    "s5": "analysis/s5_final_controlled_comparison/README.md",
    "s5_report": "analysis/s5_final_controlled_comparison/s5_controlled_comparison_report.md",
    "s6": "presentation/s6-real-debugging-evidence/README.md",
    "quixbugs": "research/quixbugs/README.md",
    "bugsinpy": "research/bugsinpy/README.md",
    "professor_traces": "docs/professor_traces/README.md",
    "experiments_index": "experiments/README.md",
    "research_index": "research/README.md",
    "results_index": "docs/results-index.md",
    "closeout": "docs/project-closeout.md",
    "final_report": "docs/final-report.md",
    "local_app": "docs/architecture/local-application-v1.md",
    "ollama_adapter": "docs/architecture/ollama-cloud-command-adapter-v1.md",
    "archived_master_plan": (
        "docs/archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md"
    ),
    "archived_readme_log": (
        "docs/archive/status/README-historical-status-log-through-2026-08-07.md"
    ),
}


REQUIRED_README_POINTERS = (
    "docs/architecture/local-application-v1.md",
    "docs/results-index.md",
    "docs/final-report.md",
    "docs/project-closeout.md",
    "docs/archive/",
    "docs/archive/status/README-historical-status-log-through-2026-08-07.md",
    "experiments/README.md",
)


REQUIRED_INDEX_FACTS = (
    "sess-20260817-103258-3d1193",
    "RESOLVED",
    "1/1",
    "2/2",
    "NOT EXERCISED",
    "INCOMPLETE_HARDWARE_STOP",
    "CLOSED — PARTIAL / COMPUTE-CONSTRAINED / NOT_EVALUATED",
    "CLOSED / NOT JUSTIFIED",
    "BLOCKED / license-gated",
    "RETAIN_OPTIONAL / OWNER-AUTHORIZED",
)


REQUIRED_FAMILY_PHRASES = {
    "r1": ("Identity", "What was run", "Main result", "Accepted interpretation"),
    "r2": ("Identity", "What was run", "Main result", "Accepted interpretation"),
    "r3": ("Identity", "What was run", "Main result", "COUNT-ONLY"),
    "r4": ("Accepted result", "curated-off-by-one-002"),
    "r5": ("5/5", "adapter_applied=false", "does not claim"),
    "r6": ("8/8 RESOLVED", "INCOMPLETE_HARDWARE_STOP", "checkpoint-30"),
    "s4_rag": (
        "CLOSED — PARTIAL / COMPUTE-CONSTRAINED / NOT_EVALUATED",
        "NOT_EVALUATED",
    ),
    "s5": ("s5-controlled-comparison-v1", "NOT_EVALUATED"),
    "quixbugs": ("RETAIN_OPTIONAL / OWNER-AUTHORIZED", "PAIRED_PILOT_V4.json"),
    "professor_traces": ("8/8 RESOLVED", "professor_debug_trace_v1"),
}


def test_family_entry_points_exist() -> None:
    missing = [
        relative
        for relative in FAMILY_ENTRY_POINTS.values()
        if not (REPO_ROOT / relative).is_file()
    ]
    assert missing == [], f"missing professor-facing entry points: {missing}"


def test_obsolete_master_plan_is_archived_not_at_root() -> None:
    assert not (REPO_ROOT / "Agentic_Debugging_Master_Execution_Plan_2026-08-10.md").exists()
    archived = REPO_ROOT / FAMILY_ENTRY_POINTS["archived_master_plan"]
    assert archived.is_file()
    text = archived.read_text(encoding="utf-8")
    # Historical header still records ACTIVE at S1; location is the archive.
    assert "**Status:** ACTIVE" in text
    assert "S1 — Debugger Interaction v2" in text


def test_readme_navigation_pointers() -> None:
    missing = [pointer for pointer in REQUIRED_README_POINTERS if pointer not in README]
    assert missing == [], f"README missing navigation pointers: {missing}"
    assert "Current status" in README
    assert "docs/project-closeout.md" in README
    assert "docs/final-report.md" in README
    assert "docs/results-index.md" in README
    assert (
        "docs/archive/status/README-historical-status-log-through-2026-08-07.md"
        in README
    )


def test_results_index_maps_accepted_boundaries() -> None:
    missing = [fact for fact in REQUIRED_INDEX_FACTS if fact not in RESULTS_INDEX]
    assert missing == [], f"results-index missing accepted facts: {missing}"
    for relative in (
        "docs/architecture/local-application-v1.md",
        "docs/architecture/ollama-cloud-command-adapter-v1.md",
        "experiments/debugger_interaction_v2_r1/README.md",
        "experiments/r6_debugger_training/README.md",
        "experiments/cp118_rag_definitive/RESULT.md",
        "research/quixbugs/PAIRED_PILOT_V4.json",
        "docs/datasets/bugsinpy/license-gate.md",
        "docs/professor_traces/",
        "docs/archive/status/Agentic_Debugging_Master_Execution_Plan_2026-08-10.md",
    ):
        # Index uses repo-relative or docs-relative links; require the basename
        # plus a parent folder so the pointer is a real path, not a slogan.
        name = Path(relative).name
        parent = Path(relative).parts[-2] if len(Path(relative).parts) > 1 else name
        assert name in RESULTS_INDEX or relative in RESULTS_INDEX, relative
        assert parent in RESULTS_INDEX, relative


def test_results_index_evidence_paths_exist() -> None:
    """Every markdown link target in the index that looks like a repo path exists."""
    missing: list[str] = []
    for raw in _markdown_link_targets(RESULTS_INDEX):
        if raw.startswith("http") or raw.startswith("#"):
            continue
        target = (REPO_ROOT / "docs" / raw).resolve()
        if not target.exists():
            missing.append(raw)
    assert missing == [], f"results-index links that do not exist: {missing}"


def test_family_notes_state_what_was_learned() -> None:
    for key, phrases in REQUIRED_FAMILY_PHRASES.items():
        text = (REPO_ROOT / FAMILY_ENTRY_POINTS[key]).read_text(encoding="utf-8")
        absent = [phrase for phrase in phrases if phrase not in text]
        assert absent == [], f"{key} missing phrases: {absent}"


def test_gitignore_excludes_generated_and_local_trees() -> None:
    required = (
        "*.egg-info/",
        ".pytest_cache/",
        "operator/",
        "artifacts/",
        "runs/",
        ".opencode/",
        ".claude/",
        ".codex/",
        "_ai-review/",
    )
    missing = [pattern for pattern in required if pattern not in GITIGNORE]
    assert missing == [], f".gitignore missing patterns: {missing}"


def test_goal_prompt_is_not_tracked_documentation() -> None:
    assert "Goal_Prompt.md" not in README
    assert "Goal_Prompt.md" not in RESULTS_INDEX


def test_readme_is_landing_page_not_historical_log() -> None:
    """Root README must stay a landing page; the old diary lives in the archive."""
    readme_path = REPO_ROOT / "README.md"
    assert readme_path.stat().st_size < 12_000
    assert README.count("\n") < 120
    assert "## Historical status log" not in README
    assert "Current status (2026-08-03)" not in README
    archived = (
        REPO_ROOT / FAMILY_ENTRY_POINTS["archived_readme_log"]
    ).read_text(encoding="utf-8")
    assert "## Historical status log" in archived
    assert "Current status (2026-08-03)" in archived
    docs_map = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "README-historical-status-log-through-2026-08-07.md" in docs_map


def test_empty_prompts_placeholder_removed() -> None:
    assert not (REPO_ROOT / "prompts" / ".gitkeep").exists()
    assert "prompts/" not in README
    # Unrelated English uses of the word "prompts" may remain in other docs.


def _markdown_link_targets(text: str) -> list[str]:
    targets: list[str] = []
    remainder = text
    while "](" in remainder:
        _, remainder = remainder.split("](", 1)
        target, remainder = remainder.split(")", 1)
        targets.append(target.split()[0])
    return targets
