"""Unit gates for the provider-neutral protocol-1.3 prompt-shaping authority.

Locks the model-facing contract every transport consumes: the real
system-role instruction, the request-specific legal representations, the
diagnosis-decision shape derived ONLY from current public controller/
contract/PDB evidence (never fixture oracles), and the byte ceiling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import protocol_prompt_shaper as shaper  # noqa: E402


_DIAGNOSIS_CONTRACT = {
    "required": [
        "hypothesis_id",
        "statement",
        "target_file",
        "target_symbol",
        "confidence",
        "evidence_refs",
        "observed_values",
    ],
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


def _diagnosis_request() -> dict:
    """A synthetic post-PDB diagnosis request (public fields only)."""

    return {
        "protocol": {
            "name": "agentic-debugger-live-jsonl",
            "version": "1.3",
            "logical_model_call_index": 12,
            "transport_attempt_index": 1,
        },
        "directive_schema": {"action": {}, "transition": {}},
        "action_contracts": {
            "express_root_cause_hypothesis": json.loads(
                json.dumps(_DIAGNOSIS_CONTRACT)
            )
        },
        "controller": {
            "state": "UNDERSTAND",
            "allowed_actions": ["express_root_cause_hypothesis"],
            "legal_transition_targets": ["Patch"],
            "hypotheses": [
                {
                    "hypothesis_id": "hypothesis-boundary-006",
                    "statement": "bounded statement",
                    "confidence": "low",
                    "status": "active",
                    "evidence_refs": [],
                    "requires_runtime_evidence": True,
                    "revision": 1,
                },
                {
                    "hypothesis_id": "hypothesis-discarded",
                    "statement": "old",
                    "confidence": "low",
                    "status": "discarded",
                    "evidence_refs": [],
                    "requires_runtime_evidence": False,
                    "revision": 0,
                },
            ],
            "last_observation": None,
        },
        "history": [
            {
                "request_index": 5,
                "state": "RUNTIME_EVIDENCE",
                "last_observation": {
                    "observation_id": "obs-start",
                    "name": "start_pdb_session",
                    "status": "ok",
                    "payload": {
                        "proof": {
                            "exact_reproduction": True,
                            "production_file": "window_tail.py",
                            "production_frame": "tail_window",
                            "breakpoint_line": 9,
                        }
                    },
                },
            },
            {
                "request_index": 7,
                "state": "RUNTIME_EVIDENCE",
                "last_observation": {
                    "observation_id": "obs-stack",
                    "name": "get_stack_summary",
                    "status": "ok",
                    "payload": {"frames": []},
                },
            },
            {
                "request_index": 8,
                "state": "RUNTIME_EVIDENCE",
                "last_observation": {
                    "observation_id": "obs-locals",
                    "name": "get_frame_locals",
                    "status": "ok",
                    "payload": {
                        "locals": [
                            {"name": "requested_size", "value": 4},
                            {"name": "values", "value": [1, 2, 3]},
                        ]
                    },
                },
            },
            {
                "request_index": 9,
                "state": "RUNTIME_EVIDENCE",
                "last_observation": {
                    "observation_id": "obs-next",
                    "name": "next_pdb_session",
                    "status": "ok",
                    "payload": {},
                },
            },
        ],
        "directive_feedback": None,
    }


class TestProviderNeutralAuthority:
    def test_system_prompt_is_provider_neutral(self) -> None:
        lowered = shaper.SYSTEM_PROMPT.lower()
        assert "ollama" not in lowered
        assert "opencode" not in lowered
        assert "commandcode" not in lowered

    def test_system_prompt_teaches_exact_top_level_forms(self) -> None:
        assert (
            '{"kind":"action","name":"<allowed action>","arguments":{...}}'
            in shaper.SYSTEM_PROMPT
        )
        assert (
            '{"kind":"transition","target_state":"<legal target>","reason":"<bounded reason>"}'
            in shaper.SYSTEM_PROMPT
        )
        assert "Do not use top-level keys named action, payload, or transition." in shaper.SYSTEM_PROMPT

    def test_build_chat_messages_roles_and_body(self) -> None:
        messages = shaper.build_chat_messages(_diagnosis_request())
        assert [message["role"] for message in messages] == ["system", "user"]
        assert messages[0]["content"] == shaper.SYSTEM_PROMPT
        assert messages[1]["content"].startswith(
            "Current request legal decision surface:"
        )
        assert shaper.PUBLIC_REQUEST_START in messages[1]["content"]
        assert shaper.PUBLIC_REQUEST_END in messages[1]["content"]

    def test_user_message_carries_the_canonical_request_verbatim(self) -> None:
        request = _diagnosis_request()
        user = shaper.build_user_protocol_message(request)
        canonical = json.dumps(
            request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        assert f"{shaper.PUBLIC_REQUEST_START}\n{canonical}\n{shaper.PUBLIC_REQUEST_END}" in user

    def test_directive_field_order_matches_validator_contract(self) -> None:
        top_level = {
            "action": frozenset({"kind", "name", "arguments"}),
            "transition": frozenset({"kind", "target_state", "reason"}),
            "add_hypothesis": frozenset(
                {"kind", "hypothesis_id", "statement", "confidence", "evidence_refs", "requires_runtime_evidence"}
            ),
            "revise_hypothesis": frozenset(
                {"kind", "hypothesis_id", "statement", "confidence", "evidence_refs", "requires_runtime_evidence"}
            ),
            "set_hypothesis_status": frozenset({"kind", "hypothesis_id", "status"}),
        }
        assert shaper.directive_fields_match_validator(top_level) is True
        drifted = dict(top_level)
        drifted["action"] = frozenset({"kind", "name", "arguments", "extra"})
        assert shaper.directive_fields_match_validator(drifted) is False

    def test_canonical_request_byte_ceiling_is_enforced(self) -> None:
        request = {"protocol": {"logical_model_call_index": 0}, "pad": "z" * 100}
        with pytest.raises(shaper.ProtocolPromptError) as excinfo:
            shaper.canonical_public_request(request, max_request_bytes=64)
        assert excinfo.value.kind == "request_too_large"

    def test_per_transport_ceiling_is_honored(self) -> None:
        request = {"protocol": {"logical_model_call_index": 0}}
        # The default (mature ladder) ceiling accepts what a tighter
        # transport-specific ceiling must still reject.
        shaper.build_user_protocol_message(request)
        with pytest.raises(shaper.ProtocolPromptError):
            shaper.build_user_protocol_message(request, max_request_bytes=8)


class TestDiagnosisDecisionShape:
    def test_diagnosis_shape_derives_exact_public_values(self) -> None:
        guidance = shaper.build_request_guidance(
            _diagnosis_request(), prompt_profile=shaper.PromptProfile.INTERACTIVE_PROVIDER_V2
        )
        assert "Legal action representation" in guidance
        assert "express_root_cause_hypothesis" in guidance
        assert "Current diagnosis decision (express_root_cause_hypothesis)" in guidance
        # Every concrete value derives from the request's public evidence.
        assert "hypothesis-boundary-006" in guidance
        assert '"target_file":"window_tail.py"' in guidance
        assert '"target_symbol":"tail_window"' in guidance
        assert '"confidence":"low"' in guidance
        assert (
            '"evidence_refs":["obs-start","obs-stack","obs-locals","obs-next"]'
            in guidance
        )
        assert '"observed_values":{"requested_size":4,"values":[1,2,3]}' in guidance
        assert "hypothesis-discarded" not in guidance

    def test_diagnosis_shape_never_fabricates_evidence(self) -> None:
        request = _diagnosis_request()
        request["history"] = []
        request["controller"]["hypotheses"] = []
        guidance = shaper.build_request_guidance(
            request, prompt_profile=shaper.PromptProfile.INTERACTIVE_PROVIDER_V2
        )
        assert "<current active hypothesis_id>" in guidance
        assert '"target_file":"<diagnosed file>"' in guidance
        assert '"target_symbol":"<diagnosed function>"' in guidance
        assert "window_tail.py" not in guidance
        assert "tail_window" not in guidance
        assert "obs-start" not in guidance

    def test_diagnosis_shape_follows_the_current_contract_required_fields(self) -> None:
        # A contract without evidence_refs/observed_values (non-exact-probe
        # scenarios) must not advertise fields the validator would reject.
        contract = json.loads(json.dumps(_DIAGNOSIS_CONTRACT))
        contract["required"] = [
            "hypothesis_id",
            "statement",
            "target_file",
            "target_symbol",
            "confidence",
        ]
        del contract["properties"]["evidence_refs"]
        del contract["properties"]["observed_values"]
        request = _diagnosis_request()
        request["action_contracts"]["express_root_cause_hypothesis"] = contract
        example = shaper._diagnosis_example(request)
        assert example is not None
        assert set(example["arguments"]) == set(contract["required"])

    def test_no_diagnosis_block_when_action_is_not_advertised(self) -> None:
        request = _diagnosis_request()
        request["controller"]["allowed_actions"] = ["run_reproduction"]
        guidance = shaper.build_request_guidance(request)
        assert "Current diagnosis decision" not in guidance
