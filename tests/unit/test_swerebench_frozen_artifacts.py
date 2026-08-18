import json
from pathlib import Path

from agentic_debugger.swerebench.authority import frozen_dir
from agentic_debugger.swerebench.population import load_clean_validation_population
from agentic_debugger.swerebench.selection import select_repo_diverse_ordering


def test_frozen_pilot10_matches_live_deterministic_selection():
    output = frozen_dir()
    population = load_clean_validation_population()
    ordering = select_repo_diverse_ordering(population)
    frozen = json.loads((output / "pilot10_manifest.json").read_text(encoding="utf-8"))
    assert frozen["selected_instance_ids"] == [
        item.instance_id for item in ordering.pilot10
    ]
    full = json.loads((output / "full_ordering.json").read_text(encoding="utf-8"))
    assert [row["instance_id"] for row in full["entries"][:10]] == frozen[
        "selected_instance_ids"
    ]
    assert frozen["n"] == 10
    assert frozen["distinct_repos"] == 10
    hashes = json.loads((output / "artifact_hashes.json").read_text(encoding="utf-8"))
    assert hashes["pilot10_manifest.json"] == (
        "4b9b17f8f897e56263f0394e35c06261bc613097f38a1b2e157d4d9a215a963f"
    )
    assert hashes["population.json"] == (
        "36bd31d1470b86db982235153372793455a850ae1fe9c1669bdf8c0e7e68ab8f"
    )
    assert hashes["full_ordering.json"] == (
        "599a07b6a527b4f8dffda4120be8e3c524ad608929bb048ea98286f80e0f5061"
    )


def test_frozen_and_preflight_files_do_not_embed_gold_or_hidden_tests():
    root = frozen_dir()
    for path in root.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "diff --git" not in text
        assert "### Target" not in text
        assert '"FAIL_TO_PASS"' not in text
        assert '"PASS_TO_PASS"' not in text
        assert '"test_patch":' not in text
        assert "sess-20260817-103258-3d1193" not in text or path.name == (
            "execution_contract.json"
        )
