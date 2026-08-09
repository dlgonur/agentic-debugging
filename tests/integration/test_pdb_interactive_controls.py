from pathlib import Path

from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.workspace import TaskWorkspace


def test_step_next_continue_use_real_persistent_pdb_worker(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "simple.py").write_text(
        "def f():\n"
        "    x = 1\n"
        "    y = x + 1\n"
        "    z = y + 1\n"
        "    return z\n"
        "\n"
        "f()\n",
        encoding="utf-8",
    )
    parent = tmp_path / "workspaces"
    parent.mkdir()

    with TaskWorkspace(str(source), parent_dir=str(parent)) as workspace:
        with PdbSession(workspace) as session:
            assert session.start_paused_target("simple.py", [2])["line"] == 2
            assert session.step_paused_target()["line"] == 3
            stack = session.get_stack_summary()
            assert stack["pause_generation"] == 2
            assert session.next_paused_target()["line"] == 4
            assert session.continue_paused_target()["state"] == "exited"
