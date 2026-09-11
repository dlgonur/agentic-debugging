"""Structural ownership checks for the decomposed PDB worker.

The paused-target lifecycle has exactly one implementation authority:
:class:`PdbLifecycleMixin`. ``PdbWorker`` inherits that behavior and must
never shadow it with an independently defined copy.
"""
from agentic_debugger.runtime.pdb_worker import PdbWorker
from agentic_debugger.runtime.pdb_worker_lifecycle import PdbLifecycleMixin


def test_terminate_handler_authority_is_inherited_from_lifecycle_mixin():
    assert "_handle_terminate_paused_target" not in PdbWorker.__dict__
    assert (
        PdbWorker._handle_terminate_paused_target
        is PdbLifecycleMixin._handle_terminate_paused_target
    )


def test_no_lifecycle_handler_is_defined_by_both_worker_and_mixin():
    overlap = {
        name
        for name in set(PdbWorker.__dict__) & set(PdbLifecycleMixin.__dict__)
        if not (name.startswith("__") and name.endswith("__"))
    }
    assert overlap == set()
