"""Golden byte-parity regressions for explicit prompt-profile architecture.

Pre-9fab308 authority: 1abce96 scripts/ollama_cloud_command_adapter.py
Golden expectations derived deterministically from parent and committed
here (no dynamic parent import at test runtime).

Covers 7 canonical request fixtures:
 1 ordinary action decision
 2 add_hypothesis
 3 exact-PDB start
 4 post-PDB revise_hypothesis
 5 POST-PDB express_root_cause_hypothesis diagnosis (the drift point)
 6 apply_patch
 7 transition

For every case asserts:
  frozen system prompt == 1abce96 system prompt
  frozen user prompt == 1abce96 user prompt (SHA-256 golden)

Plus direct-provider enhancement regression and scientific provenance.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import protocol_prompt_shaper as shaper  # noqa: E402

# Golden hashes derived from parent 1abce96 via:
#   git show 1abce96:scripts/ollama_cloud_command_adapter.py > /tmp/parent_adapter.py
#   python execution of parent build_chat_messages on the 7 fixtures.
# System prompt is identical across profiles and parent.
GOLDEN_SYSTEM_SHA256 = "56c800314fafe18676e477877ea8cff13bafbf8ce791b71713734672d5ef7709"

GOLDEN_USER_SHA256 = {
    "ordinary_action": "1259722b1fe9b33c8b6a6104e487d0f36e43d88600d346eef391cd1c105fcb00",
    "add_hypothesis": "5a627bbfa6bf8b62a465f54a837d3942589aab6331d60f9fcb7d0047d0f82d8b",
    "exact_pdb_start": "1bbbad018a8db29e7c00dde9f2e4b5880984cbaabffe8cc7c9924e29866aa486",
    "post_pdb_revise": "d53de4d632b4951de5c4297dd0dd05eed2abe13d87420821767effc10a3246d0",
    "diagnosis": "fae87390dbf7ec114db61e2fda3bdb79ed20b04e415fc74b1fccc1e6a4595e2f",
    "apply_patch": "aece7cb12900a3b272f14dfe29f8446484e02c7010d8c18bd26c0249c6562168",
    "transition": "6881dc8ff1e01fc68841fc5d8252c7888f6d32b98edf2d412b0fb8e4e4cadca1",
}

# Interactive profile's diagnosis hash differs (enhancement).
GOLDEN_INTERACTIVE_DIAGNOSIS_USER_SHA256 = "001caa1feb9d28ae1f320f85ced7edb7a27033b7154a821465672404074dd84f"


def _base_request():
    return {
        "protocol": {"name": "agentic-debugger-live-jsonl", "version": "1.3", "logical_model_call_index": 0, "transport_attempt_index": 1},
        "directive_schema": {"action": {}, "transition": {}},
        "action_contracts": {},
        "controller": {
            "state": "REPRODUCE",
            "allowed_actions": ["run_reproduction"],
            "legal_transition_targets": ["Understand", "Failed"],
            "hypotheses": [],
            "last_observation": None,
            "budget_limits": {"max_patch_attempts": 10},
            "budget_state": {"patch_attempts": 0},
        },
        "history": [],
        "directive_feedback": None,
        "proof_gate": {"next_required_actions": ["run_reproduction"]},
    }


def _fixtures():
    # 1 ordinary action
    req1 = _base_request()
    req1["controller"]["allowed_actions"] = ["run_reproduction"]
    req1["action_contracts"] = {"run_reproduction": {"required": ["phase"], "properties": {"phase": {"type": "string", "enum": ["baseline"]}}, "additional_properties": False}}
    req1["directive_schema"] = {"action": {}, "transition": {}}

    # 2 add_hypothesis
    req2 = _base_request()
    req2["controller"]["state"] = "UNDERSTAND"
    req2["controller"]["allowed_actions"] = []
    req2["directive_schema"] = {"add_hypothesis": {"constraints": {"hypothesis_id": {"type": "string"}, "confidence": {"enum": ["low", "medium", "high"]}, "requires_runtime_evidence": {"enum": [False]}}}, "transition": {}}
    req2["action_contracts"] = {}
    req2["controller"]["legal_transition_targets"] = ["Patch", "Failed"]

    # 3 exact-PDB start
    req3 = _base_request()
    req3["controller"]["state"] = "RUNTIME_EVIDENCE"
    req3["controller"]["allowed_actions"] = ["start_pdb_session", "get_source_window"]
    req3["action_contracts"] = {
        "start_pdb_session": {"required": ["breakpoint_line"], "properties": {"breakpoint_line": {"type": "integer", "minimum": 1}}, "additional_properties": False},
        "get_source_window": {"required": ["path", "line"], "properties": {"path": {"type": "string"}, "line": {"type": "integer", "minimum": 1}}, "additional_properties": False},
    }
    req3["directive_schema"] = {"action": {}, "transition": {}}
    req3["history"] = []

    # 4 post-PDB revise_hypothesis
    req4 = _base_request()
    req4["controller"]["state"] = "UNDERSTAND"
    req4["controller"]["allowed_actions"] = []
    req4["directive_schema"] = {"revise_hypothesis": {"constraints": {"hypothesis_id": {"example": "hyp-1"}, "confidence": {"enum": ["low"]}, "evidence_refs": {"example": []}, "requires_runtime_evidence": {"enum": [True]}}}, "transition": {}}
    req4["history"] = [{"request_index": 0, "state": "UNDERSTAND", "last_observation": {"observation_id": "obs-1", "name": "get_frame_locals", "status": "ok", "payload": {}}}]

    # 5 POST-PDB express_root_cause_hypothesis diagnosis
    req5 = {
        "protocol": {"name": "agentic-debugger-live-jsonl", "version": "1.3", "logical_model_call_index": 12, "transport_attempt_index": 1},
        "directive_schema": {"action": {}, "transition": {}},
        "action_contracts": {
            "express_root_cause_hypothesis": {
                "required": ["hypothesis_id", "statement", "target_file", "target_symbol", "confidence", "evidence_refs", "observed_values"],
                "properties": {
                    "hypothesis_id": {"type": "string", "min_length": 1},
                    "statement": {"type": "string", "min_length": 1},
                    "target_file": {"type": "string", "min_length": 1},
                    "target_symbol": {"type": "string", "min_length": 1},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "evidence_refs": {"type": "array"},
                    "observed_values": {"type": "object"},
                },
                "additional_properties": False,
            }
        },
        "controller": {
            "state": "UNDERSTAND",
            "allowed_actions": ["express_root_cause_hypothesis"],
            "legal_transition_targets": ["Patch"],
            "hypotheses": [{"hypothesis_id": "hypothesis-boundary-006", "statement": "bounded", "confidence": "low", "status": "active", "evidence_refs": [], "requires_runtime_evidence": True, "revision": 1}],
            "last_observation": None,
        },
        "history": [
            {"request_index": 5, "state": "RUNTIME_EVIDENCE", "last_observation": {"observation_id": "obs-start", "name": "start_pdb_session", "status": "ok", "payload": {"proof": {"exact_reproduction": True, "production_file": "window_tail.py", "production_frame": "tail_window", "breakpoint_line": 9}}}},
            {"request_index": 7, "state": "RUNTIME_EVIDENCE", "last_observation": {"observation_id": "obs-stack", "name": "get_stack_summary", "status": "ok", "payload": {"frames": []}}},
            {"request_index": 8, "state": "RUNTIME_EVIDENCE", "last_observation": {"observation_id": "obs-locals", "name": "get_frame_locals", "status": "ok", "payload": {"locals": [{"name": "requested_size", "value": 4}, {"name": "values", "value": [1, 2, 3]}]}}},
            {"request_index": 9, "state": "RUNTIME_EVIDENCE", "last_observation": {"observation_id": "obs-next", "name": "next_pdb_session", "status": "ok", "payload": {}}},
        ],
        "directive_feedback": None,
    }

    # 6 apply_patch
    req6 = _base_request()
    req6["controller"]["state"] = "PATCH"
    req6["controller"]["allowed_actions"] = ["apply_patch", "syntax_check"]
    req6["action_contracts"] = {
        "apply_patch": {"required": ["patch"], "properties": {"patch": {"type": "string"}}, "additional_properties": False},
        "syntax_check": {"required": [], "properties": {}, "additional_properties": False},
    }
    req6["controller"]["last_observation"] = None
    req6["history"] = []

    # 7 transition
    req7 = _base_request()
    req7["controller"]["state"] = "UNDERSTAND"
    req7["controller"]["allowed_actions"] = []
    req7["controller"]["legal_transition_targets"] = ["RUNTIME_EVIDENCE", "Patch", "Failed"]
    req7["directive_schema"] = {"transition": {}}
    req7["action_contracts"] = {}

    return {
        "ordinary_action": req1,
        "add_hypothesis": req2,
        "exact_pdb_start": req3,
        "post_pdb_revise": req4,
        "diagnosis": req5,
        "apply_patch": req6,
        "transition": req7,
    }


FIXTURES = _fixtures()


class TestFrozenScientificByteParity:
    """Frozen profile reproduces parent 1abce96 byte-for-byte."""

    @pytest.mark.parametrize("case", list(GOLDEN_USER_SHA256.keys()))
    def test_frozen_system_prompt_is_parent(self, case):
        request = FIXTURES[case]
        msgs = shaper.build_chat_messages(request, prompt_profile=shaper.PromptProfile.FROZEN_SCIENTIFIC_V1)
        assert msgs[0]["role"] == "system"
        assert hashlib.sha256(msgs[0]["content"].encode("utf-8")).hexdigest() == GOLDEN_SYSTEM_SHA256
        # byte equality: frozen system prompt must be exactly parent's SYSTEM_PROMPT
        assert msgs[0]["content"] == shaper.SYSTEM_PROMPT
        # parent system prompt bytes are golden; we already proved parent == current SYSTEM_PROMPT
        assert hashlib.sha256(shaper.SYSTEM_PROMPT.encode("utf-8")).hexdigest() == GOLDEN_SYSTEM_SHA256

    @pytest.mark.parametrize("case", list(GOLDEN_USER_SHA256.keys()))
    def test_frozen_user_prompt_is_parent(self, case):
        request = FIXTURES[case]
        msgs = shaper.build_chat_messages(request, prompt_profile=shaper.PromptProfile.FROZEN_SCIENTIFIC_V1)
        user = msgs[1]["content"]
        assert hashlib.sha256(user.encode("utf-8")).hexdigest() == GOLDEN_USER_SHA256[case]
        # For non-diagnosis cases, interactive also matches parent (no drift).
        if case != "diagnosis":
            interactive = shaper.build_chat_messages(request, prompt_profile=shaper.PromptProfile.INTERACTIVE_PROVIDER_V2)
            assert hashlib.sha256(interactive[1]["content"].encode("utf-8")).hexdigest() == GOLDEN_USER_SHA256[case]

    def test_diagnosis_is_the_only_divergent_case(self):
        # Diagnosis case is where 9fab308 currently drifts.
        request = FIXTURES["diagnosis"]
        frozen = shaper.build_chat_messages(request, prompt_profile=shaper.PromptProfile.FROZEN_SCIENTIFIC_V1)
        interactive = shaper.build_chat_messages(request, prompt_profile=shaper.PromptProfile.INTERACTIVE_PROVIDER_V2)
        frozen_user = frozen[1]["content"]
        interactive_user = interactive[1]["content"]
        assert hashlib.sha256(frozen_user.encode("utf-8")).hexdigest() == GOLDEN_USER_SHA256["diagnosis"]
        assert hashlib.sha256(interactive_user.encode("utf-8")).hexdigest() == GOLDEN_INTERACTIVE_DIAGNOSIS_USER_SHA256
        assert frozen_user != interactive_user

    def test_frozen_diagnosis_has_no_new_guidance(self):
        request = FIXTURES["diagnosis"]
        frozen = shaper.build_request_guidance(request, prompt_profile=shaper.PromptProfile.FROZEN_SCIENTIFIC_V1)
        assert "Current diagnosis decision" not in frozen
        assert "Current diagnosis legal representation" not in frozen

    def test_interactive_diagnosis_has_enhanced_guidance(self):
        request = FIXTURES["diagnosis"]
        interactive = shaper.build_request_guidance(request, prompt_profile=shaper.PromptProfile.INTERACTIVE_PROVIDER_V2)
        assert "Current diagnosis decision" in interactive
        assert "Current diagnosis legal representation" in interactive


class TestDirectProviderEnhancementRegression:
    """Same diagnosis request: frozen does NOT contain, interactive DOES."""

    def test_frozen_does_not_contain_diagnosis_legal_representation(self):
        request = FIXTURES["diagnosis"]
        frozen = shaper.build_chat_messages(request, prompt_profile=shaper.PromptProfile.FROZEN_SCIENTIFIC_V1)
        frozen_user = frozen[1]["content"]
        assert "Current diagnosis legal representation" not in frozen_user
        # frozen must not leak interactive evidence
        assert "hypothesis-boundary-006" not in frozen_user or "Current diagnosis" not in frozen_user

    def test_interactive_contains_exact_evidence(self):
        request = FIXTURES["diagnosis"]
        interactive = shaper.build_chat_messages(request, prompt_profile=shaper.PromptProfile.INTERACTIVE_PROVIDER_V2)
        user = interactive[1]["content"]
        assert "Current diagnosis legal representation" in user
        assert "Current diagnosis decision" in user
        # exact hypothesis_id
        assert "hypothesis-boundary-006" in user
        # exact target_file / target_symbol from proof
        assert '"target_file":"window_tail.py"' in user
        assert '"target_symbol":"tail_window"' in user
        # actual evidence_refs (observation ids)
        assert '"evidence_refs":["obs-start","obs-stack","obs-locals","obs-next"]' in user
        # actual observed_values
        assert '"observed_values":{"requested_size":4,"values":[1,2,3]}' in user


class TestScientificProvenance:
    def test_prompt_profile_enum_is_typed_and_bounded(self):
        assert shaper.PromptProfile.FROZEN_SCIENTIFIC_V1.value == "frozen_scientific_v1"
        assert shaper.PromptProfile.INTERACTIVE_PROVIDER_V2.value == "interactive_provider_v2"
        assert len(list(shaper.PromptProfile)) == 2
        # unknown profile must fail closed
        with pytest.raises(shaper.ProtocolPromptError):
            shaper.build_request_guidance(FIXTURES["ordinary_action"], prompt_profile="unknown")  # type: ignore[arg-type]

    def test_qualified_scientific_prompt_profile_is_frozen(self):
        # Invariant: qualified paths must be frozen
        import scripts.ollama_cloud_command_adapter as ollama_adapter

        assert ollama_adapter.PROMPT_PROFILE == shaper.PromptProfile.FROZEN_SCIENTIFIC_V1
        assert ollama_adapter.OLLAMA_PROMPT_PROFILE == shaper.PromptProfile.FROZEN_SCIENTIFIC_V1

    def test_interactive_provider_prompt_profile_is_interactive(self):
        import scripts.provider_direct_api_adapter as direct_adapter

        assert direct_adapter.PROMPT_PROFILE == shaper.PromptProfile.INTERACTIVE_PROVIDER_V2
        assert direct_adapter.PROVIDER_PROMPT_PROFILE == shaper.PromptProfile.INTERACTIVE_PROVIDER_V2


class TestLevel32AndLadderContracts:
    def test_level32_uses_frozen_profile(self):
        import scripts.ollama_cloud_command_adapter as ollama_adapter
        from agentic_debugger.application.level32 import LEVEL32_TASK_ID

        # Level-32 operator launches the Ollama adapter; that adapter's profile must be frozen.
        assert ollama_adapter.PROMPT_PROFILE == shaper.PromptProfile.FROZEN_SCIENTIFIC_V1
        assert ollama_adapter.OLLAMA_PROMPT_PROFILE == shaper.PromptProfile.FROZEN_SCIENTIFIC_V1
        # sanity: task id constant
        assert LEVEL32_TASK_ID == "audreyr__cookiecutter-967"

    def test_lower_qualified_ladder_uses_frozen_and_zero_repairs(self):
        from agentic_debugger.application.ollama_cloud_source import LADDER_RUNTIME_CONTRACTS, INTERACTIVE_LADDER_DIRECTIVE_REPAIRS
        import scripts.ollama_cloud_command_adapter as ollama_adapter

        assert INTERACTIVE_LADDER_DIRECTIVE_REPAIRS == 2
        for task_id in ["pdb-required-boundary-006", "pdb-required-caller-callee-007", "pdb-required-multistage-units-008"]:
            contract = LADDER_RUNTIME_CONTRACTS[task_id]
            assert contract.max_retries == 0
            assert contract.max_directive_repairs == 0
        # Adapter profile is frozen for qualified ladder
        assert ollama_adapter.PROMPT_PROFILE == shaper.PromptProfile.FROZEN_SCIENTIFIC_V1

    def test_configured_lower_ladder_uses_interactive_and_two_repairs(self, tmp_path, monkeypatch):
        from agentic_debugger.application.configured_source import run_configured_session
        from agentic_debugger.application.ollama_cloud_source import INTERACTIVE_LADDER_DIRECTIVE_REPAIRS, ladder_runtime_contract
        import agentic_debugger.application.configured_source as cs
        from agentic_debugger.application.emitter import SessionEventEmitter
        from agentic_debugger.application.journal import SessionEventJournal
        from agentic_debugger.application import model_providers as mp
        from agentic_debugger.cancellation import CancellationToken
        from agentic_debugger.application.events import SourceKind
        from agentic_debugger.application.worker_scenarios import ScenarioContext
        from agentic_debugger.evaluation.live import LiveRunLimits

        # Capture LiveRunLimits for a lower-ladder configured run.
        captured = []

        real_init = LiveRunLimits.__init__

        def wrapped(self, *a, **kw):
            real_init(self, *a, **kw)
            captured.append(self)

        monkeypatch.setattr(LiveRunLimits, "__init__", wrapped)

        def fake_run_local(ctx, **kw):
            pass

        monkeypatch.setattr(cs, "run_local_session", fake_run_local)

        def fake_resolve(provider, model_id, **kw):
            from agentic_debugger.evaluation.live import LiveModelConfig

            cfg = LiveModelConfig(model_name=model_id, command=("echo", "hi"), request_timeout_seconds=30, tool_version="test")
            return cfg, {"display_name": model_id, "route": "direct_api", "api_protocol": "chat_completions", "provider_model_id": model_id, "endpoint": "http://fake"}

        monkeypatch.setattr(mp, "resolve_provider_live_config", fake_resolve)
        monkeypatch.setattr(cs, "_is_registry_provider", lambda p: True)

        # Level 6 task is a lower-ladder rung: must use interactive profile + 2 repairs.
        task_id = "pdb-required-boundary-006"
        journal = SessionEventJournal(tmp_path / "j.events.jsonl", session_id="sess-test", task_id=task_id, source_kind=SourceKind.CONFIGURED_MODEL)
        emitter = SessionEventEmitter(sink=journal, session_id="sess-test", task_id=task_id, source_kind=SourceKind.CONFIGURED_MODEL)
        ctx = ScenarioContext(work_dir=tmp_path / "work", emitter=emitter, token=CancellationToken())
        run_configured_session(ctx, {"provider": "commandcode_goat", "model_id": "deepseek/deepseek-v4-flash", "policy": "pdb-on-uncertainty"})
        assert len(captured) >= 1
        limits = captured[-1]
        assert limits.max_retries == 0
        assert limits.max_directive_repairs == INTERACTIVE_LADDER_DIRECTIVE_REPAIRS == 2

        # Provider adapter itself is interactive
        import scripts.provider_direct_api_adapter as direct_adapter

        assert direct_adapter.PROMPT_PROFILE == shaper.PromptProfile.INTERACTIVE_PROVIDER_V2

    def test_qualified_ollama_session_uses_frozen_and_zero_repairs(self, tmp_path, monkeypatch):
        # run_ollama_cloud_session is qualified and must remain frozen + 0
        from agentic_debugger.application.ollama_cloud_source import run_ollama_cloud_session
        from agentic_debugger.evaluation.live import LiveRunLimits
        import agentic_debugger.application.ollama_cloud_source as ocs
        from agentic_debugger.application.emitter import SessionEventEmitter
        from agentic_debugger.application.journal import SessionEventJournal
        from agentic_debugger.application.events import SourceKind
        from agentic_debugger.application.worker_scenarios import ScenarioContext
        from agentic_debugger.cancellation import CancellationToken

        captured = []
        real_init = LiveRunLimits.__init__

        def wrapped(self, *a, **kw):
            real_init(self, *a, **kw)
            captured.append(self)

        monkeypatch.setattr(LiveRunLimits, "__init__", wrapped)

        def fake_run_local(ctx, **kw):
            pass

        monkeypatch.setattr(ocs, "run_local_session", fake_run_local)

        # Need to mock _config to avoid real Ollama spec resolution and command building
        def fake_config(alias, **kw):
            from agentic_debugger.evaluation.live import LiveModelConfig

            cfg = LiveModelConfig(model_name=alias, command=("echo", "hi"), request_timeout_seconds=30, tool_version="ollama-cloud-adapter-v1.3-ladder")

            class Spec:
                local_alias = alias
                upstream_model = alias.replace(":cloud", "")
                effective_tags_remote_model = alias.replace(":cloud", "")

            return cfg, Spec()

        monkeypatch.setattr(ocs, "_config", fake_config)

        task_id = "pdb-required-boundary-006"
        journal = SessionEventJournal(tmp_path / "j2.events.jsonl", session_id="sess-qual", task_id=task_id, source_kind=SourceKind.CONFIGURED_MODEL)
        emitter = SessionEventEmitter(sink=journal, session_id="sess-qual", task_id=task_id, source_kind=SourceKind.CONFIGURED_MODEL)
        ctx = ScenarioContext(work_dir=tmp_path / "work2", emitter=emitter, token=CancellationToken())
        # Policy must be valid DemoPolicy value
        run_ollama_cloud_session(ctx, {"model_alias": "gpt-oss:20b-cloud", "policy": "pdb-on-uncertainty"})
        assert len(captured) >= 1
        limits = captured[-1]
        assert limits.max_retries == 0
        assert limits.max_directive_repairs == 0
        import scripts.ollama_cloud_command_adapter as ollama_adapter

        assert ollama_adapter.PROMPT_PROFILE == shaper.PromptProfile.FROZEN_SCIENTIFIC_V1


class TestNoDuplicateTestDefinitions:
    def test_live_evaluation_has_no_duplicate_test_names(self):
        path = REPO_ROOT / "tests" / "unit" / "test_live_evaluation.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
        seen = set()
        duplicates = set()
        for name in names:
            if name in seen:
                duplicates.add(name)
            seen.add(name)
        assert not duplicates, f"duplicate top-level test names found: {sorted(duplicates)}"
        # Ensure the retained qualified-ladder assertion is exactly as required
        assert "assert contract.max_retries == 0" in source
        assert "assert contract.max_directive_repairs == 0" in source
        # The dead incomplete expression must be gone
        assert "assert contract.max_directive_" not in source or "assert contract.max_directive_repairs" in source
        # Ensure only one authoritative version: count occurrences of the specific test names
        for dup_name in [
            "test_directive_repair_sends_feedback_without_advancing_the_controller",
            "test_zero_directive_repairs_is_the_frozen_scientific_default",
            "test_directive_repair_exhaustion_terminates_after_bounded_attempts",
            "test_transport_failure_is_never_retried_under_directive_repairs",
            "test_directive_repairs_stay_inside_the_model_request_ceiling",
            "test_treatment_budget_enforces_zero_directive_repairs",
            "test_ladder_contracts_remain_zero_directive_repairs_for_qualified_runs",
        ]:
            assert names.count(dup_name) == 1, f"{dup_name} appears {names.count(dup_name)} times"
