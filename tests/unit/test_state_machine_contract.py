import pytest

from agentic_debugger.agent.state_machine import (
    TRANSITION_GRAPH,
    ControllerState,
    is_transition_allowed,
)


class TestStateMachineContract:
    def test_allowed_transitions_reproduce(self):
        assert is_transition_allowed(ControllerState.REPRODUCE, ControllerState.UNDERSTAND)
        assert is_transition_allowed(ControllerState.REPRODUCE, ControllerState.FAILED)

    def test_allowed_transitions_understand(self):
        assert is_transition_allowed(ControllerState.UNDERSTAND, ControllerState.UNDERSTAND)
        assert is_transition_allowed(ControllerState.UNDERSTAND, ControllerState.RUNTIME_EVIDENCE)
        assert is_transition_allowed(ControllerState.UNDERSTAND, ControllerState.PATCH)
        assert is_transition_allowed(ControllerState.UNDERSTAND, ControllerState.FAILED)

    def test_allowed_transitions_runtime_evidence(self):
        assert is_transition_allowed(ControllerState.RUNTIME_EVIDENCE, ControllerState.UNDERSTAND)
        assert is_transition_allowed(ControllerState.RUNTIME_EVIDENCE, ControllerState.RUNTIME_EVIDENCE)
        assert is_transition_allowed(ControllerState.RUNTIME_EVIDENCE, ControllerState.PATCH)
        assert is_transition_allowed(ControllerState.RUNTIME_EVIDENCE, ControllerState.FAILED)

    def test_allowed_transitions_patch(self):
        assert is_transition_allowed(ControllerState.PATCH, ControllerState.PATCH)
        assert is_transition_allowed(ControllerState.PATCH, ControllerState.UNDERSTAND)
        assert is_transition_allowed(ControllerState.PATCH, ControllerState.VALIDATE)
        assert is_transition_allowed(ControllerState.PATCH, ControllerState.FAILED)

    def test_allowed_transitions_validate(self):
        assert is_transition_allowed(ControllerState.VALIDATE, ControllerState.DONE)
        assert is_transition_allowed(ControllerState.VALIDATE, ControllerState.UNDERSTAND)
        assert is_transition_allowed(ControllerState.VALIDATE, ControllerState.RUNTIME_EVIDENCE)
        assert is_transition_allowed(ControllerState.VALIDATE, ControllerState.PATCH)
        assert is_transition_allowed(ControllerState.VALIDATE, ControllerState.FAILED)

    def test_reproduce_cannot_skip_to_done(self):
        assert not is_transition_allowed(ControllerState.REPRODUCE, ControllerState.DONE)

    def test_reproduce_cannot_skip_to_patch(self):
        assert not is_transition_allowed(ControllerState.REPRODUCE, ControllerState.PATCH)

    def test_reproduce_cannot_skip_to_runtime_evidence(self):
        assert not is_transition_allowed(ControllerState.REPRODUCE, ControllerState.RUNTIME_EVIDENCE)

    def test_reproduce_cannot_skip_to_validate(self):
        assert not is_transition_allowed(ControllerState.REPRODUCE, ControllerState.VALIDATE)

    def test_understand_cannot_skip_to_done(self):
        assert not is_transition_allowed(ControllerState.UNDERSTAND, ControllerState.DONE)

    def test_runtime_evidence_cannot_skip_to_done(self):
        assert not is_transition_allowed(ControllerState.RUNTIME_EVIDENCE, ControllerState.DONE)

    def test_runtime_evidence_cannot_skip_to_validate(self):
        assert not is_transition_allowed(ControllerState.RUNTIME_EVIDENCE, ControllerState.VALIDATE)

    def test_patch_cannot_skip_to_done(self):
        assert not is_transition_allowed(ControllerState.PATCH, ControllerState.DONE)

    def test_patch_cannot_skip_to_runtime_evidence(self):
        assert not is_transition_allowed(ControllerState.PATCH, ControllerState.RUNTIME_EVIDENCE)

    def test_validate_cannot_skip_to_reproduce(self):
        assert not is_transition_allowed(ControllerState.VALIDATE, ControllerState.REPRODUCE)

    def test_done_is_terminal(self):
        assert not is_transition_allowed(ControllerState.DONE, ControllerState.UNDERSTAND)
        assert not is_transition_allowed(ControllerState.DONE, ControllerState.REPRODUCE)
        assert not is_transition_allowed(ControllerState.DONE, ControllerState.RUNTIME_EVIDENCE)
        assert not is_transition_allowed(ControllerState.DONE, ControllerState.PATCH)
        assert not is_transition_allowed(ControllerState.DONE, ControllerState.VALIDATE)
        assert not is_transition_allowed(ControllerState.DONE, ControllerState.FAILED)
        assert not is_transition_allowed(ControllerState.DONE, ControllerState.DONE)

    def test_failed_is_terminal(self):
        assert not is_transition_allowed(ControllerState.FAILED, ControllerState.UNDERSTAND)
        assert not is_transition_allowed(ControllerState.FAILED, ControllerState.REPRODUCE)
        assert not is_transition_allowed(ControllerState.FAILED, ControllerState.RUNTIME_EVIDENCE)
        assert not is_transition_allowed(ControllerState.FAILED, ControllerState.PATCH)
        assert not is_transition_allowed(ControllerState.FAILED, ControllerState.VALIDATE)
        assert not is_transition_allowed(ControllerState.FAILED, ControllerState.DONE)
        assert not is_transition_allowed(ControllerState.FAILED, ControllerState.FAILED)

    def test_validation_is_pure(self):
        state = ControllerState.REPRODUCE
        result = is_transition_allowed(state, ControllerState.UNDERSTAND)
        assert result is True
        assert state == ControllerState.REPRODUCE

    def test_graph_is_complete(self):
        expected_states = {
            ControllerState.REPRODUCE,
            ControllerState.UNDERSTAND,
            ControllerState.RUNTIME_EVIDENCE,
            ControllerState.PATCH,
            ControllerState.VALIDATE,
            ControllerState.DONE,
            ControllerState.FAILED,
        }
        assert set(TRANSITION_GRAPH.keys()) == expected_states

    def test_understand_cannot_validate(self):
        assert not is_transition_allowed(ControllerState.UNDERSTAND, ControllerState.VALIDATE)

    def test_understand_cannot_done(self):
        assert not is_transition_allowed(ControllerState.UNDERSTAND, ControllerState.DONE)
