"""R5 bridge unit tests: R5-only adapter coupling, original-region stack
filtering with frame-id preservation, and final-prompt anti-leakage across
all five tasks and all model-facing phases."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.events.schema import Observation

from experiments.debugger_interaction_v2_r5 import bridge as r5_bridge
from experiments.debugger_interaction_v2_r5.adapter import (
    R5DebuggerBridgeAdapter,
    R5StageTracker,
    make_r5_session_state_provider,
)
from experiments.debugger_interaction_v2_r5.launcher import (
    build_r5_launcher_source,
    task_target_module_path,
)

CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"

R5_TASKS = (
    "curated-none-handling-001",
    "curated-off-by-one-002",
    "curated-wrong-branch-003",
    "curated-mutation-alias-004",
    "curated-caller-callee-005",
)


def _task(task_id: str):
    return load_task(str(CURATED_ROOT / task_id / "task.json"))


def _observation(name: str, payload: dict, status: str = "ok") -> Observation:
    return Observation.from_mapping({
        "observation_id": f"obs-{name}",
        "action_id": f"act-{name}",
        "run_id": "r5-test",
        "task_id": "r5-test",
        "name": name,
        "status": status,
        "payload": payload,
        "summary": "",
        "truncated": False,
    })


def _stack_observation(gen: int, frames: list[dict]) -> Observation:
    return _observation(
        "get_stack_summary",
        {"state": "paused", "script": "display_name.py", "pause_generation": gen, "frames": frames, "total_frames": len(frames), "truncated": False},
    )


def _snapshot(state: ControllerState, last_obs=None) -> ControllerSnapshot:
    return ControllerSnapshot(
        "r5-test", "r5-test", state, 0,
        ControllerBudgetLimits(max_patch_attempts=2, max_test_runs=5, max_pdb_observations=8),
        ControllerBudgetState(), HypothesisLedger(),
        last_observation=last_obs,
    )


class _RecordingTransport:
    def __init__(self, responses: tuple[str, ...] = ("reproduce",)):
        self.responses = list(responses)
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []

    def request(self, system_prompt, user_prompt, timeout_seconds):
        self.system_prompts.append(system_prompt)
        self.user_prompts.append(user_prompt)
        text = self.responses.pop(0)
        return type("Resp", (), {"raw_text": text, "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})()


class TestR5AdapterCoupling:
    """Prove the R5 adapter binds exclusively to the R5 bridge: an R5-specific
    system-prompt/path change is visible in BOTH transport.request and
    telemetry.system_prompt_sha256."""

    def test_r5_system_prompt_reaches_transport_and_telemetry(self):
        script_path = "display_name.py"
        source_text = (CURATED_ROOT / "curated-none-handling-001" / script_path).read_text(encoding="utf-8")
        transport = _RecordingTransport()
        tracker = R5StageTracker()
        provider = make_r5_session_state_provider(type("C", (), {"pdb_session": None, "pdb_session_started": False})(), lambda: tracker.stage)
        adapter = R5DebuggerBridgeAdapter(
            transport=transport,
            model_name="r5-test",
            task_description="Title: t\nDescription: d",
            script_path=script_path,
            source_text=source_text,
            eligible_lines=(2, 3, 4, 5),
            original_line_count=len(source_text.splitlines()),
            session_state_provider=provider,
            stage_tracker=tracker,
        )
        # R5-specific path must be in the R5 system prompt...
        assert f"--- a/{script_path}" in adapter.system_prompt
        assert "recent_window.py" not in adapter.system_prompt
        # ...and the accepted R3 bridge still carries its own hardcoded path,
        # proving the R5 adapter is NOT bound to the R3 bridge.
        from experiments.debugger_interaction_v2_r3 import bridge as r3_bridge
        assert "recent_window.py" in r3_bridge.SYSTEM_PROMPT
        assert r3_bridge.SYSTEM_PROMPT != adapter.system_prompt

        directive = adapter.next_directive(_snapshot(ControllerState.REPRODUCE))
        assert directive is not None
        # Transport received the R5 system prompt with the R5-specific path.
        assert len(transport.system_prompts) == 1
        assert transport.system_prompts[0] == adapter.system_prompt
        assert "display_name.py" in transport.system_prompts[0]
        # Telemetry records the same R5 system prompt hash.
        records = adapter.telemetry
        assert len(records) == 1
        expected = hashlib.sha256(adapter.system_prompt.encode("utf-8")).hexdigest()
        assert records[0]["request"]["system_prompt_sha256"] == expected
        # The R3 adapter would have hashed a different prompt for this path.
        assert records[0]["request"]["system_prompt_sha256"] != hashlib.sha256(
            r3_bridge.SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest()


class TestStackRegionFiltering:
    FRAMES = [
        {"frame_id": 0, "script": "display_name.py", "line": 2, "function": "format_display_name", "is_current": True},
        {"frame_id": 1, "script": "display_name.py", "line": 4, "function": "format_display_name", "is_current": False},
        # Appended launcher frame: SAME script, line beyond the original count.
        {"frame_id": 2, "script": "display_name.py", "line": 10, "function": "_r5_failing_execution", "is_current": False},
        # Pytest internal frame.
        {"frame_id": 3, "script": "lib/python3.10/site-packages/_pytest/runner.py", "line": 341, "function": "pytest_runtest_call", "is_current": False},
    ]

    def test_render_hides_driver_and_pytest_frames_keeps_raw_ids(self):
        obs = _stack_observation(7, self.FRAMES)
        rendered = r5_bridge._render_observation(
            obs, filter_scripts=frozenset({"display_name.py"}), original_line_count=5
        )
        assert "frame_id=0" in rendered
        assert "frame_id=1" in rendered
        assert "frame_id=2" not in rendered
        assert "frame_id=3" not in rendered
        assert "_r5_failing_execution" not in rendered
        assert "stack rendering filtered" in rendered
        # Raw ids are never renumbered: rendered ids equal raw payload ids.
        assert "pause_generation=7" in rendered
        # Unfiltered render still shows everything (evidence completeness).
        raw = r5_bridge._render_observation(obs)
        assert "frame_id=2" in raw and "frame_id=3" in raw

    def test_parse_uses_authoritative_raw_payload_after_filtering(self):
        obs = _stack_observation(7, self.FRAMES)
        # locals after a filtered stack must dispatch the RAW current frame id
        # and RAW generation from the payload, not from the display.
        result = r5_bridge.parse(
            "locals", ControllerState.RUNTIME_EVIDENCE, obs,
            r2_stage=r5_bridge.R2Stage.PAUSED_NEEDS_INSPECTION,
        )
        assert result.directive.arguments["frame_id"] == 0
        assert result.directive.arguments["pause_generation"] == 7
        result2 = r5_bridge.parse(
            "print name", ControllerState.RUNTIME_EVIDENCE, obs,
            r2_stage=r5_bridge.R2Stage.PAUSED_NEEDS_INSPECTION,
        )
        assert result2.directive.arguments["frame_id"] == 0
        assert result2.directive.arguments["expression"] == "name"


class TestFenceUnwrap:
    """R5.2 deterministic single-fence unwrap — both shapes and fail-closed
    behavior.  The unwrap only removes markdown framing; semantics are never
    invented or altered."""

    DIFF = (
        "--- a/display_name.py\n"
        "+++ b/display_name.py\n"
        "@@ -2,1 +2,1 @@\n"
        "-    normalized_name = name.strip()\n"
        "+    normalized_name = name.strip() if name is not None else \"\"\n"
    )

    def test_bare_fence_synthesizes_patch_command(self):
        text = "```diff\n" + self.DIFF + "```\n\nThis patch fixes the crash."
        unwrapped, record = r5_bridge.unwrap_single_fence(text)
        assert record is not None and record.unwrapped is True
        assert record.shape == "bare_fence"
        assert record.synthesized_patch_command is True
        assert record.fence_language == "diff"
        assert record.trailing_prose_bytes > 0
        assert unwrapped.startswith("patch\n--- a/display_name.py")

    def test_patch_plus_fence_shape(self):
        text = "patch\n```python\n" + self.DIFF + "\n```"
        unwrapped, record = r5_bridge.unwrap_single_fence(text)
        assert record is not None and record.unwrapped is True
        assert record.shape == "patch_plus_fence"
        assert record.synthesized_patch_command is False
        assert record.fence_language == "python"
        assert unwrapped == "patch\n" + self.DIFF

    def test_non_fenced_text_passes_through(self):
        text = "stack"
        unwrapped, record = r5_bridge.unwrap_single_fence(text)
        assert record is None
        assert unwrapped == "stack"

    def test_unterminated_fence_fails_closed(self):
        text = "```diff\n" + self.DIFF
        unwrapped, record = r5_bridge.unwrap_single_fence(text)
        assert record is None
        assert unwrapped == text

    def test_empty_fenced_content_fails_closed(self):
        text = "```diff\n```"
        unwrapped, record = r5_bridge.unwrap_single_fence(text)
        assert record is None
        assert unwrapped == text

    def test_second_fence_in_trailing_prose_fails_closed(self):
        text = "```diff\n" + self.DIFF + "```\n```python\nx\n```"
        unwrapped, record = r5_bridge.unwrap_single_fence(text)
        assert record is None
        assert unwrapped == text

    def test_unwrapped_fence_parses_as_patch(self):
        text = "```diff\n" + self.DIFF + "```"
        unwrapped, _ = r5_bridge.unwrap_single_fence(text)
        result = r5_bridge.parse(
            unwrapped, ControllerState.PATCH,
            patch_stage=r5_bridge.R3PatchStage.NEEDS_FIRST_REPAIR,
        )
        assert result.command_token == "patch"
        assert result.directive.arguments["patch"].startswith("--- a/display_name.py")


class TestTerminalStage:
    """R5.2 terminal runtime progression: crash-on-step evidence allows
    diagnosis without a second PAUSED pause."""

    def _terminal_observation(self) -> Observation:
        return _observation(
            "next_pdb_session",
            {"state": "exited", "script": "display_name.py", "exit_code": 1},
        )

    def test_terminal_stage_allows_diagnosis(self):
        obs = self._terminal_observation()
        result = r5_bridge.parse(
            "diagnosis the target crashed on None.strip()",
            ControllerState.RUNTIME_EVIDENCE, obs,
            r2_stage=r5_bridge.R2Stage.PAUSED_AFTER_TERMINAL_STEP,
        )
        assert result.is_diagnosis is True
        assert result.directive.target_state.value == "Patch"

    def test_terminal_stage_forbids_stack(self):
        obs = self._terminal_observation()
        with pytest.raises(r5_bridge.BridgeParseError) as exc:
            r5_bridge.parse(
                "stack", ControllerState.RUNTIME_EVIDENCE, obs,
                r2_stage=r5_bridge.R2Stage.PAUSED_AFTER_TERMINAL_STEP,
            )
        assert exc.value.category is r5_bridge.BridgeRejection.COMMAND_NOT_IN_LIFECYCLE

    def test_terminal_stage_commands_visible(self):
        commands = r5_bridge.visible_commands_r2(
            ControllerState.RUNTIME_EVIDENCE,
            r5_bridge.R2Stage.PAUSED_AFTER_TERMINAL_STEP,
        )
        assert commands == ("diagnosis", "failed")

    def test_terminal_observation_rendering_names_the_exit(self):
        obs = self._terminal_observation()
        rendered = r5_bridge._render_observation(obs)
        assert "Execution exited (exit_code=1)" in rendered
        assert "Terminal: no further pause is available" in rendered

    def test_terminal_prompt_mentions_real_failure_evidence(self):
        prompt = r5_bridge.render_prompt(
            ControllerState.RUNTIME_EVIDENCE, None, "Task: t",
            debugger=r5_bridge.DebuggerContext(
                script_path="display_name.py",
                source_text="def f():\n    pass\n",
                eligible_lines=(2,),
                lifecycle=r5_bridge.DebuggerLifecycle.CONSUMED_OR_ENDED,
                r2_stage=r5_bridge.R2Stage.PAUSED_AFTER_TERMINAL_STEP,
                paused_line=2,
                paused_function="format_display_name",
            ),
        )
        assert "exited or crashed during the last step/next" in prompt
        assert "real failure evidence below" in prompt

    def test_terminal_prompt_renders_mechanical_crash_summary(self):
        prompt = r5_bridge.render_prompt(
            ControllerState.RUNTIME_EVIDENCE, None, "Task: t",
            debugger=r5_bridge.DebuggerContext(
                script_path="display_name.py",
                source_text="def f():\n    pass\n",
                eligible_lines=(2,),
                lifecycle=r5_bridge.DebuggerLifecycle.CONSUMED_OR_ENDED,
                r2_stage=r5_bridge.R2Stage.PAUSED_AFTER_TERMINAL_STEP,
                paused_line=2,
                paused_function="format_display_name",
                runtime_slice={
                    "crash_summary": "display_name.py:2: AttributeError",
                    "reproduction": "[run_reproduction] status=ok\n  failure output",
                    "step": "[next_pdb_session] status=ok\n  Execution exited (exit_code=1)",
                },
            ),
        )
        assert "Terminal failure evidence: display_name.py:2: AttributeError" in prompt
        assert "[reproduction]" in prompt
        assert "Execution exited (exit_code=1)" in prompt


class TestRealFailureEvidenceRendering:
    """R5.2: the bounded real reproduction failure output (a real test
    diagnostic from the task's own reproduction command — explicitly
    permitted by the goal's non-cheating rules) is rendered to the model."""

    def test_reproduction_failure_output_rendered(self):
        obs = _observation(
            "run_reproduction",
            {
                "phase": "baseline",
                "exit_code": 1,
                "passed": False,
                "failure_reproduced": True,
                "failure_output": "E AttributeError: 'NoneType' object has no attribute 'strip'",
            },
        )
        rendered = r5_bridge._render_observation(obs)
        assert "failure_reproduced=True" in rendered
        assert "Real failure output" in rendered
        assert "AttributeError" in rendered

    def test_reproduction_without_failure_output_renders_plain(self):
        obs = _observation(
            "run_reproduction",
            {
                "phase": "baseline",
                "exit_code": 0,
                "passed": True,
                "failure_reproduced": False,
                "failure_output": "",
            },
        )
        rendered = r5_bridge._render_observation(obs)
        assert "Real failure output" not in rendered

    def test_apply_patch_verifier_feedback_rendered(self):
        obs = _observation(
            "apply_patch",
            {
                "applied": True,
                "changed_files": ["price.py"],
                "hunk_count": 1,
                "verifier_feedback": {
                    "status": "COMPLETED",
                    "outcome": "REGRESSION",
                    "f2p_total": 1,
                    "f2p_passed": 0,
                    "p2p_total": 2,
                    "p2p_passed": 0,
                    "full_suite": "FAIL",
                    "syntax": True,
                    "failures": [
                        {
                            "kind": "f2p",
                            "node_id": "tests/test_price.py::test_x",
                            "status": "FAIL",
                            "detail": "E TypeError: _format_price() takes 1 positional argument but 2 were given",
                        }
                    ],
                },
            },
        )
        rendered = r5_bridge._render_observation(obs)
        assert "Real verifier (independent EvaluationVerifier)" in rendered
        assert "outcome=REGRESSION" in rendered
        assert "TypeError: _format_price() takes 1 positional argument" in rendered


class TestFinalPromptAntiLeakage:
    """Final rendered prompts for every model-facing phase on all five tasks
    must contain no test metadata, no oracle, no reference repair, no
    RuntimeProbe semantic fields, and no launcher harness text.

    R5.2 note: the REAL bounded reproduction failure output (a real test
    diagnostic from the task's own public reproduction command) is permitted
    evidence under the goal's non-cheating rules and is rendered through the
    observation renderer (tested separately above); it is not injected into
    these static prompt constructions."""

    @pytest.mark.parametrize("task_id", R5_TASKS)
    def test_no_leakage_in_final_rendered_prompts(self, task_id):
        from agentic_debugger.demo.catalog import scenario_for

        task = _task(task_id)
        module_path = task_target_module_path(task)
        original = (CURATED_ROOT / task_id / module_path).read_text(encoding="utf-8")
        eligible = r5_bridge.breakpoint_eligible_lines(original)
        line_count = len(original.splitlines())
        task_desc = f"Title: {task.title}\nDescription: {task.description}"
        scenario = scenario_for(task_id)
        probe = scenario.runtime_probe

        forbidden = []
        forbidden.extend(task.tests.fail_to_pass)
        forbidden.extend(task.tests.pass_to_pass)
        forbidden.extend([
            task.oracle.root_cause_summary,
            task.oracle.runtime_evidence_hint,
            task.oracle.bug_category,
            # RuntimeProbe semantic field NAMES (their values are ordinary
            # source text and may legitimately appear in the source).
            "focus_function",
            "call_source",
            "anchor",
            "inspect_expressions",
            "runtime_probe",
            probe.call_source,
            "_r5_failing_execution",
            "_ARGS",
            "pytest.main",
            "reproduction.argv",
            "no:cacheprovider",
        ])
        forbidden = [s for s in forbidden if s]

        runtime_slice = {"stack_G1": "x", "inspection": "y", "step": "z", "stack_G2": "w"}
        debugger = r5_bridge.DebuggerContext(
            script_path=module_path, source_text=original,
            eligible_lines=eligible,
            lifecycle=r5_bridge.DebuggerLifecycle.NOT_STARTED,
            r2_stage=r5_bridge.R2Stage.NOT_STARTED,
        )

        prompts = {
            "reproduce": r5_bridge.render_prompt(
                ControllerState.REPRODUCE, None, task_desc, debugger=None
            ),
            "runtime_not_started": r5_bridge.render_prompt(
                ControllerState.RUNTIME_EVIDENCE, None, task_desc, debugger=debugger
            ),
            "runtime_ready_diagnosis": r5_bridge.render_prompt(
                ControllerState.RUNTIME_EVIDENCE, None, task_desc,
                debugger=r5_bridge.DebuggerContext(
                    script_path=module_path, source_text=original,
                    eligible_lines=eligible,
                    lifecycle=r5_bridge.DebuggerLifecycle.PAUSED,
                    r2_stage=r5_bridge.R2Stage.READY_FOR_DIAGNOSIS,
                    paused_line=2, paused_function="f",
                ),
            ),
            "patch": r5_bridge.render_prompt(
                ControllerState.PATCH, None, task_desc,
                debugger=r5_bridge.DebuggerContext(
                    script_path=module_path, source_text=original,
                    eligible_lines=eligible,
                    lifecycle=r5_bridge.DebuggerLifecycle.CONSUMED_OR_ENDED,
                    r2_stage=r5_bridge.R2Stage.CONSUMED_OR_ENDED,
                    retained_diagnosis="the bug is in the formatter",
                    runtime_slice=runtime_slice,
                ),
                patch_stage=r5_bridge.R3PatchStage.NEEDS_FIRST_REPAIR,
            ),
        }

        for phase, prompt in prompts.items():
            for needle in forbidden:
                assert needle not in prompt, (
                    f"task {task_id} phase {phase}: forbidden content leaked: {needle!r}"
                )
            assert "tests/test_" not in prompt
            assert "def _r5_failing_execution" not in prompt
            # Model-facing source is exactly the original source region:
            # at most original_line_count numbered source lines are rendered.
            import re as _re
            numbered = [l for l in prompt.splitlines() if _re.match(r"^[ >] *\d+:", l)]
            assert len(numbered) <= line_count, (
                f"task {task_id} phase {phase}: rendered source exceeds the "
                f"original region ({len(numbered)} > {line_count})"
            )
            # The launcher text itself never appears in any prompt.
            launcher = build_r5_launcher_source(task.reproduction.argv)
            assert launcher.strip() not in prompt

    def test_runtime_slice_rendering_is_region_filtered(self):
        obs = _stack_observation(7, TestStackRegionFiltering.FRAMES)
        rendered = r5_bridge._render_observation(
            obs, filter_scripts=frozenset({"display_name.py"}), original_line_count=5
        )
        assert "frame_id=2" not in rendered
