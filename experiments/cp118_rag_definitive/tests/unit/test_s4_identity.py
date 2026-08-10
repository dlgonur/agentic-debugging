"""S4 — cp118 adapter identity tests (accepted S2 convention).

Prove, WITHOUT any model/GPU load:

1. the accepted cp118 artifact (when present) verifies byte-exact against
   the frozen contract identity block (tree `65b5ed9a...`);
2. fail-closed negative behavior: missing file, extra file, wrong hash,
   wrong size, wrong tree identity, foreign declared base;
3. the tree-identity convention is deterministic and self-consistent.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from experiments.cp118_rag_definitive.s4_identity import (
    BASE_REPOSITORY,
    compute_adapter_identity,
    verify_adapter_identity,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACT = json.loads(
    (REPO_ROOT / "experiments/cp118_rag_definitive/s4_contract.json")
    .read_text(encoding="utf-8")
)
EXPECTED_IDENTITY = CONTRACT["model"]["adapter_identity"]

ACCEPTED_CP118_PATH = (
    Path(r"C:\Users\benya\Downloads\selected-adapter-corrected-cp118-"
         r"20260809T193500Z-1-001\selected-adapter-corrected-cp118")
)
FROZEN_TREE = "65b5ed9a354d4b2c03ba86e2b8065118e11abab9c439cb481b5739f1b86e7c00"


def _write_file(root: Path, rel: str, content: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _synthetic_adapter(tmp_path: Path) -> Path:
    """A synthetic adapter dir whose files are encoded in the identity
    records (path -> sha256+size) so the expected block is exact."""

    root = tmp_path / "adapter"
    files = [
        ("adapter_config.json",
         json.dumps({"base_model_name_or_path": BASE_REPOSITORY}).encode("utf-8")),
        ("adapter_model.safetensors", b"fake-weights" * 1000),
        ("tokenizer.json", b"{}"),
        ("tokenizer_config.json", b"{}"),
        ("chat_template.jinja", b"{{}}"),
        ("README.md", b"readme"),
        ("ADAPTER_MANIFEST.json", b"{}"),
    ]
    records = []
    for rel, content in files:
        _write_file(root, rel, content)
        records.append({
            "path": rel,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        })
    identity = compute_adapter_identity(root)
    return root, {"tree_identity_sha256": identity["tree_identity_sha256"],
                  "files": records}


@pytest.mark.skipif(
    not ACCEPTED_CP118_PATH.is_dir(),
    reason="accepted cp118 artifact not present at the recorded path",
)
def test_accepted_cp118_verifies_against_frozen_contract():
    """The accepted cp118 artifact must reproduce the frozen tree identity
    (65b5ed9a...) — the definitive checkpoint is not substituted."""

    on_disk = verify_adapter_identity(ACCEPTED_CP118_PATH, EXPECTED_IDENTITY)
    assert on_disk["tree_identity_sha256"] == FROZEN_TREE


def test_synthetic_adapter_round_trip_verifies(tmp_path):
    root, expected = _synthetic_adapter(tmp_path)
    on_disk = verify_adapter_identity(root, expected)
    assert on_disk["tree_identity_sha256"] == expected["tree_identity_sha256"]


def test_compute_identity_deterministic(tmp_path):
    root, _ = _synthetic_adapter(tmp_path)
    assert compute_adapter_identity(root) == compute_adapter_identity(root)


def test_missing_file_fails_closed(tmp_path):
    root, expected = _synthetic_adapter(tmp_path)
    (root / "README.md").unlink()
    with pytest.raises(RuntimeError, match="missing files"):
        verify_adapter_identity(root, expected)


def test_extra_file_fails_closed(tmp_path):
    root, expected = _synthetic_adapter(tmp_path)
    _write_file(root, "EXTRA.bin", b"surprise")
    with pytest.raises(RuntimeError, match="unexpected files"):
        verify_adapter_identity(root, expected)


def test_wrong_hash_fails_closed(tmp_path):
    root, expected = _synthetic_adapter(tmp_path)
    _write_file(root, "README.md", b"tampered")
    with pytest.raises(RuntimeError, match="sha256"):
        verify_adapter_identity(root, expected)


def test_wrong_hash_or_size_fails_closed(tmp_path):
    root, expected = _synthetic_adapter(tmp_path)
    _write_file(root, "tokenizer.json", b"{}x")
    # Any content change alters both SHA-256 and size; the verifier must
    # fail closed on whichever check fires first.
    with pytest.raises(RuntimeError, match="(sha256|size)"):
        verify_adapter_identity(root, expected)


def test_tree_identity_drift_fails_closed(tmp_path):
    root, expected = _synthetic_adapter(tmp_path)
    expected = dict(expected)
    expected["tree_identity_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="tree_identity_sha256"):
        verify_adapter_identity(root, expected)


def test_foreign_base_fails_closed(tmp_path):
    # A foreign base declared by a byte-identical adapter directory must be
    # rejected by the semantic base check (defense in depth).
    root = tmp_path / "foreign"
    _write_file(root, "adapter_config.json",
                json.dumps({"base_model_name_or_path": "Other/Model"})
                .encode("utf-8"))
    _write_file(root, "adapter_model.safetensors", b"fake-weights" * 1000)
    _write_file(root, "tokenizer.json", b"{}")
    identity = compute_adapter_identity(root)
    with pytest.raises(RuntimeError, match="different base model"):
        verify_adapter_identity(root, identity)


def test_contract_identity_block_is_complete():
    assert EXPECTED_IDENTITY["tree_identity_sha256"] == FROZEN_TREE
    assert len(EXPECTED_IDENTITY["files"]) == 7
    weights = next(f for f in EXPECTED_IDENTITY["files"]
                   if f["path"] == "adapter_model.safetensors")
    assert weights["sha256"].startswith("59398e32")
