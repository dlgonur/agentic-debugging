"""Contract tests for the Task 9 demonstration catalog and policy set."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_debugger.agent.controller_policy import PdbPolicy
from agentic_debugger.demo.catalog import (
    MAX_PROBE_EXPRESSIONS,
    PROBE_DRIVER_FUNCTION,
    DemoCatalogError,
    LocalizationClaim,
    ReferenceRepair,
    RuntimeProbe,
    build_reference_patch,
    probe_driver_source,
    reference_repair_snippets,
    resolve_probe_breakpoint,
    scenario_for,
    scenario_ids,
)
from agentic_debugger.demo.policies import (
    DEMO_POLICIES,
    DemoPolicy,
    pdb_policy_for,
    policy_from_value,
)
from agentic_debugger.demo.runner import CURATED_RELATIVE_ROOT, curated_task_ids
from agentic_debugger.evaluation.runner import load_task

REPO_ROOT = Path(__file__).resolve().parents[2]
CURATED = REPO_ROOT / CURATED_RELATIVE_ROOT


def _fixture(task_id: str) -> Path:
    return CURATED / task_id


class TestCatalogCoversCuratedSet:
    def test_catalog_matches_the_live_curated_set_exactly(self) -> None:
        assert scenario_ids() == curated_task_ids(REPO_ROOT)

    def test_every_scenario_names_a_real_manifest(self) -> None:
        for task_id in scenario_ids():
            assert (_fixture(task_id) / "task.json").is_file()

    def test_unknown_task_is_rejected(self) -> None:
        with pytest.raises(DemoCatalogError):
            scenario_for("curated-does-not-exist-999")
        with pytest.raises(DemoCatalogError):
            scenario_for(None)  # type: ignore[arg-type]


class TestScenarioConsistency:
    @pytest.mark.parametrize("task_id", scenario_ids())
    def test_localization_claim_matches_the_task_oracle(self, task_id: str) -> None:
        scenario = scenario_for(task_id)
        task = load_task(str(_fixture(task_id) / "task.json"))
        assert scenario.localization.file_path in task.oracle.target_files
        assert scenario.localization.symbol in task.oracle.target_symbols

    @pytest.mark.parametrize("task_id", scenario_ids())
    def test_reference_repair_targets_an_allowed_write_path(self, task_id: str) -> None:
        scenario = scenario_for(task_id)
        task = load_task(str(_fixture(task_id) / "task.json"))
        assert scenario.reference_repair.target_path in task.constraints.allowed_write_paths

    @pytest.mark.parametrize("task_id", scenario_ids())
    def test_reference_repair_renders_exactly_one_applicable_diff(self, task_id: str) -> None:
        scenario = scenario_for(task_id)
        source = (_fixture(task_id) / scenario.reference_repair.target_path).read_text(
            encoding="utf-8"
        )
        patch = build_reference_patch(source, scenario.reference_repair)
        assert patch.startswith(f"--- a/{scenario.reference_repair.target_path}\n")
        assert f"\n+++ b/{scenario.reference_repair.target_path}\n" in patch
        assert scenario.reference_repair.new_snippet.splitlines()[0].strip() in patch

    @pytest.mark.parametrize("task_id", scenario_ids())
    def test_probe_breakpoint_resolves_inside_the_focus_function(self, task_id: str) -> None:
        scenario = scenario_for(task_id)
        probe = scenario.runtime_probe
        source = (_fixture(task_id) / probe.module_path).read_text(encoding="utf-8")
        line = resolve_probe_breakpoint(source, probe)
        assert probe.anchor in source.splitlines()[line - 1]

    @pytest.mark.parametrize("task_id", scenario_ids())
    def test_probe_driver_is_valid_python_and_calls_the_focus_function(
        self, task_id: str
    ) -> None:
        scenario = scenario_for(task_id)
        probe = scenario.runtime_probe
        source = (_fixture(task_id) / probe.module_path).read_text(encoding="utf-8")
        driver = probe_driver_source(probe)
        assert PROBE_DRIVER_FUNCTION in driver
        assert probe.call_source in driver
        compile(source + driver, probe.module_path, "exec")

    @pytest.mark.parametrize("task_id", scenario_ids())
    def test_probe_stays_inside_the_curated_pdb_observation_budget(self, task_id: str) -> None:
        scenario = scenario_for(task_id)
        task = load_task(str(_fixture(task_id) / "task.json"))
        # one stack summary + one locals read + one evaluation per expression
        required = 2 + len(scenario.runtime_probe.inspect_expressions)
        assert required <= task.constraints.max_pdb_observations

    @pytest.mark.parametrize("task_id", scenario_ids())
    def test_scenario_mapping_is_json_compatible(self, task_id: str) -> None:
        json.dumps(scenario_for(task_id).to_mapping(), allow_nan=False)

    def test_reference_repair_snippets_round_trip(self) -> None:
        for task_id in scenario_ids():
            path, old, new = reference_repair_snippets(task_id)
            repair = scenario_for(task_id).reference_repair
            assert (path, old, new) == (
                repair.target_path,
                repair.old_snippet,
                repair.new_snippet,
            )


class TestCatalogRejections:
    def test_repair_requires_a_relative_posix_target(self) -> None:
        with pytest.raises(DemoCatalogError):
            ReferenceRepair("/abs/path.py", "a", "b")
        with pytest.raises(DemoCatalogError):
            ReferenceRepair("dir\\file.py", "a", "b")

    def test_repair_must_change_the_source(self) -> None:
        with pytest.raises(DemoCatalogError):
            ReferenceRepair("a.py", "same", "same")

    def test_localization_claim_must_be_complete(self) -> None:
        with pytest.raises(DemoCatalogError):
            LocalizationClaim("", "symbol")

    def test_probe_rejects_too_many_or_duplicate_expressions(self) -> None:
        base = dict(
            module_path="a.py",
            focus_function="f",
            call_source="f()",
            anchor="x = 1",
        )
        with pytest.raises(DemoCatalogError):
            RuntimeProbe(**base, inspect_expressions=tuple(
                f"e{index}" for index in range(MAX_PROBE_EXPRESSIONS + 1)
            ))
        with pytest.raises(DemoCatalogError):
            RuntimeProbe(**base, inspect_expressions=("x", "x"))
        with pytest.raises(DemoCatalogError):
            RuntimeProbe(**base, inspect_expressions=())

    def test_drifted_reference_repair_is_a_hard_error(self) -> None:
        repair = ReferenceRepair("a.py", "missing snippet", "replacement")
        with pytest.raises(DemoCatalogError, match="matched 0 times"):
            build_reference_patch("def f():\n    return 1\n", repair)

    def test_ambiguous_reference_repair_is_a_hard_error(self) -> None:
        repair = ReferenceRepair("a.py", "value = 1", "value = 2")
        with pytest.raises(DemoCatalogError, match="matched 2 times"):
            build_reference_patch("value = 1\nvalue = 1\n", repair)

    def test_probe_anchor_outside_the_focus_function_is_rejected(self) -> None:
        probe = RuntimeProbe(
            module_path="a.py",
            focus_function="inside",
            call_source="inside()",
            anchor="outside_marker",
            inspect_expressions=("x",),
        )
        source = "outside_marker = 1\n\n\ndef inside():\n    x = 1\n    return x\n"
        with pytest.raises(DemoCatalogError, match="matched 0 own statement lines"):
            resolve_probe_breakpoint(source, probe)

    def test_probe_anchor_inside_a_nested_function_is_rejected(self) -> None:
        probe = RuntimeProbe(
            module_path="a.py",
            focus_function="outer",
            call_source="outer()",
            anchor="nested_marker = 1",
            inspect_expressions=("x",),
        )
        source = (
            "def outer():\n"
            "    def inner():\n"
            "        nested_marker = 1\n"
            "        return nested_marker\n"
            "    return inner()\n"
        )
        with pytest.raises(DemoCatalogError, match="matched 0 own statement lines"):
            resolve_probe_breakpoint(source, probe)

    def test_probe_anchor_on_a_continuation_line_is_rejected(self) -> None:
        probe = RuntimeProbe(
            module_path="a.py",
            focus_function="outer",
            call_source="outer()",
            anchor="second_argument",
            inspect_expressions=("x",),
        )
        source = (
            "def outer():\n"
            "    total = sum(\n"
            "        [1, 2],\n"
            "        second_argument,\n"
            "    )\n"
            "    return total\n"
        )
        with pytest.raises(DemoCatalogError, match="matched 0 own statement lines"):
            resolve_probe_breakpoint(source, probe)

    def test_probe_anchor_must_start_the_stripped_line(self) -> None:
        probe = RuntimeProbe(
            module_path="a.py",
            focus_function="outer",
            call_source="outer()",
            anchor="value = 1",
            inspect_expressions=("x",),
        )
        # "value = 1" occurs inside the line but does not start it.
        source = "def outer():\n    other_value = 1\n    return other_value\n"
        with pytest.raises(DemoCatalogError, match="matched 0 own statement lines"):
            resolve_probe_breakpoint(source, probe)

    def test_probe_anchor_resolves_a_nested_statement_in_the_same_scope(self) -> None:
        probe = RuntimeProbe(
            module_path="a.py",
            focus_function="outer",
            call_source="outer()",
            anchor="chosen = 2",
            inspect_expressions=("chosen",),
        )
        source = "def outer():\n    if True:\n        chosen = 2\n    return chosen\n"
        assert resolve_probe_breakpoint(source, probe) == 3

    def test_missing_probe_function_is_rejected(self) -> None:
        probe = RuntimeProbe(
            module_path="a.py",
            focus_function="absent",
            call_source="absent()",
            anchor="x = 1",
            inspect_expressions=("x",),
        )
        with pytest.raises(DemoCatalogError, match="not found"):
            resolve_probe_breakpoint("def other():\n    x = 1\n", probe)

    def test_unparsable_probe_module_is_rejected(self) -> None:
        probe = scenario_for(scenario_ids()[0]).runtime_probe
        with pytest.raises(DemoCatalogError, match="does not parse"):
            resolve_probe_breakpoint("def broken(:\n", probe)


class TestPolicySet:
    def test_demo_policies_map_onto_accepted_pdb_policies(self) -> None:
        assert DEMO_POLICIES == (
            DemoPolicy.STATIC_BASELINE,
            DemoPolicy.PDB_ON_UNCERTAINTY,
        )
        assert pdb_policy_for(DemoPolicy.STATIC_BASELINE) is PdbPolicy.DISABLED
        assert pdb_policy_for(DemoPolicy.PDB_ON_UNCERTAINTY) is PdbPolicy.ON_UNCERTAINTY

    def test_policy_lookup_rejects_unknown_and_partial_values(self) -> None:
        assert policy_from_value("static-baseline") is DemoPolicy.STATIC_BASELINE
        for value in ("static", "STATIC-BASELINE", "", "pdb"):
            with pytest.raises(ValueError):
                policy_from_value(value)

    def test_pdb_policy_lookup_requires_a_demo_policy(self) -> None:
        with pytest.raises(ValueError):
            pdb_policy_for("static-baseline")  # type: ignore[arg-type]
