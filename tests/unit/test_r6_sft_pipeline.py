from __future__ import annotations

from experiments.r6_debugger_training.generate_trajectories import (
    breakpoint_candidates,
)
from experiments.r6_debugger_training.scripted_transport import (
    ScriptedTrajectoryTransport,
)
from experiments.r6_debugger_training.run_bounded_training import (
    SFT_FILES,
    _sft_identity,
)


def _patch_prompt() -> str:
    return (
        "Current phase: Patch\n"
        "Available commands:\n"
        "  - file\n"
        "  - patch\n"
    )


def test_feedback_recovery_transport_emits_rejected_then_corrected_source() -> None:
    transport = ScriptedTrajectoryTransport(
        module_path="sample.py",
        breakpoint_line=3,
        diagnosis_text="the branch is inverted",
        corrected_source="def f():\n    return 2\n",
        rejected_source="def f():\n    return 1\n\n",
    )

    first = transport.request("system", _patch_prompt(), 60).raw_text
    second = transport.request("system", _patch_prompt(), 60).raw_text

    assert first == "file sample.py\ndef f():\n    return 1\n\n"
    assert second == "file sample.py\ndef f():\n    return 2\n"
    assert transport.patch_requests == 2


def test_default_scripted_transport_remains_single_correct_patch() -> None:
    transport = ScriptedTrajectoryTransport(
        module_path="sample.py",
        breakpoint_line=3,
        diagnosis_text="the branch is inverted",
        corrected_source="def f():\n    return 2\n",
    )

    assert transport.request("system", _patch_prompt(), 60).raw_text.endswith(
        "def f():\n    return 2\n"
    )


def test_breakpoint_candidates_prioritize_changed_region_then_neighbors() -> None:
    assert breakpoint_candidates((2, 4, 7, 9), [6]) == [7, 9, 4, 2]


def test_recovery_candidate_is_a_real_syntax_valid_source_change() -> None:
    from experiments.r6_debugger_training.generate_trajectories import build_transport

    _task, transport, _metadata = build_transport(
        "quixbugs-gcd",
        "gcd",
        "gcd.py",
        feedback_recovery=True,
    )

    assert transport.rejected_source is not None
    assert transport.rejected_source != transport.corrected_source
    assert "behavior intentionally unchanged" in transport.rejected_source
    compile(transport.rejected_source, "gcd.py", "exec")


def test_sft_input_identity_hashes_every_consumed_file(tmp_path) -> None:
    for name in SFT_FILES:
        (tmp_path / name).write_text(f"{name}\n", encoding="utf-8")

    before = _sft_identity(tmp_path)
    (tmp_path / "sft_train.jsonl").write_text("changed\n", encoding="utf-8")
    after = _sft_identity(tmp_path)

    assert {entry["name"] for entry in before["files"]} == set(SFT_FILES)
    assert before["manifest_sha256"] != after["manifest_sha256"]
