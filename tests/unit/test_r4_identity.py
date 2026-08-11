"""R4 unit tests — accepted R3 fix identity (amendment 4).

R_fix_B / R_fix_C / fingerprint must equal the frozen accepted R3.2
identities; any drift fails closed before the fixed-side comparison.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.model_generated_test_probe_r4 import probe as r4_probe
from experiments.model_generated_test_probe_r4.generated_test_runner import (
    ALLOWED_PATCH_PATHS,
    DENIED_PATCH_PATHS,
)

R_FIX_B_SHA256 = "831b1c2bc347c9812296de5ddb7ebac5f6f414bbd6512561b4cb29066e6e2c76"
R_FIX_C_SHA256 = "8c051faa605d9cf736540301e204639870408b288ab1ceb8348845afc674b990"
FINGERPRINT = "002fc5ca376c48ffc035b3b0b73ef0bb6735713ef9cff78603fe879e5703fb34"


class TestRFixIdentity:
    def test_frozen_b_c_fingerprint(self):
        identity = r4_probe._r3_fix_identity()
        assert identity["r_fix_b_sha256"] == R_FIX_B_SHA256
        assert identity["r_fix_c_sha256"] == R_FIX_C_SHA256
        assert identity["semantic_body_fingerprint"] == FINGERPRINT
        assert identity["fingerprint_equal"] is True

    def test_drift_fails_closed(self, tmp_path, monkeypatch):
        tampered = tmp_path / "tampered.patch"
        tampered.write_text(
            "--- a/recent_window.py\n+++ b/recent_window.py\n"
            "@@ -1,2 +1,2 @@\n x\n-y\n+z\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(r4_probe, "R_FIX_B_PATH", tampered)
        with pytest.raises(RuntimeError, match="identity drift"):
            r4_probe._r3_fix_identity()

    def test_patch_path_gates_unchanged(self):
        # Same gates as the production verifier / task constraints; not weakened.
        assert ALLOWED_PATCH_PATHS == ["recent_window.py"]
        assert DENIED_PATCH_PATHS == ["tests", "task.json"]

    def test_r_fix_c_applies_through_real_patcher(self, tmp_path):
        from agentic_debugger.evaluation.runner import load_task
        from agentic_debugger.runtime.patcher import PatchManager
        from agentic_debugger.runtime.workspace import TaskWorkspace

        task = load_task(
            str(
                REPO_ROOT
                / "agentic_debugger" / "datasets" / "curated"
                / "curated-off-by-one-002" / "task.json"
            )
        )
        c = r4_probe._derive_r_fix_c()
        workspace = TaskWorkspace(
            str(
                REPO_ROOT
                / "agentic_debugger" / "datasets" / "curated"
                / "curated-off-by-one-002"
            ),
            parent_dir=str(tmp_path),
        )
        try:
            manager = PatchManager(
                workspace,
                allowed_paths=list(ALLOWED_PATCH_PATHS),
                denied_paths=list(DENIED_PATCH_PATHS),
            )
            manager.apply_patch(c)
            fixed_source = (
                pathlib.Path(workspace.root) / "recent_window.py"
            ).read_text(encoding="utf-8")
            assert "range(start_index, end_index)" in fixed_source
            assert "end_index - (1 if requested_size" not in fixed_source
        finally:
            workspace.cleanup()
