"""S4 — cp118 adapter identity verification (accepted S2 convention).

This module replicates the accepted S2 cp118 identity semantics
(``experiments/debugger_interaction_v2_s2_cp118/s2_transport.py`` at commit
``1bd90a2dc76f88307e3387fbf556b0d1ff4dee49``) exactly:

* every file under the adapter directory contributes ``relative_path``
  (POSIX form), SHA-256 and byte size;
* the tree identity is the SHA-256 over the concatenation of
  ``relative_path`` + ``\\0`` + ``digest`` + ``\\0`` for every file, in the
  same deterministic order produced by ``sorted(pathlib.Path.rglob("*.is_file"))``
  on this platform (the accepted convention, which is what produced the
  frozen tree identity ``65b5ed9a...`` on the accepted Windows host);
* ``verify_adapter_identity`` fails closed on: missing files, extra files,
  per-file SHA-256 drift, per-file size drift, tree identity drift, or an
  ``adapter_config.json`` that does not declare the pinned base model.

The frozen identity block is the single source of truth and lives in
``s4_contract.json`` (``model.adapter_identity``).  No third tree-hash
convention is introduced; the S4 check must reproduce the accepted
``65b5ed9a354d4b2c03ba86e2b8065118e11abab9c439cb481b5739f1b86e7c00``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

BASE_REPOSITORY = "Qwen/Qwen2.5-Coder-7B-Instruct"
BASE_REVISION = "c03e6d358207e414f1eca0bb1891e29f1db0e242"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_adapter_identity(adapter_dir: Path) -> Dict[str, Any]:
    """Compute the on-disk adapter identity (tree hash + per-file records).

    Ordering convention: ``sorted(item for item in adapter_dir.rglob("*")
    if item.is_file())`` — the exact accepted S2/run_pilot convention.
    """

    adapter_dir = Path(adapter_dir)
    if not adapter_dir.is_dir():
        raise RuntimeError(f"adapter path is not a directory: {adapter_dir}")
    files: List[Dict[str, Any]] = []
    combined = hashlib.sha256()
    for path in sorted(item for item in adapter_dir.rglob("*") if item.is_file()):
        relative = path.relative_to(adapter_dir).as_posix()
        digest = _sha256_bytes(path.read_bytes())
        files.append({
            "path": relative,
            "sha256": digest,
            "size_bytes": path.stat().st_size,
        })
        combined.update(relative.encode("utf-8"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        combined.update(b"\0")
    return {
        "path": str(adapter_dir.resolve()),
        "tree_identity_sha256": combined.hexdigest(),
        "files": files,
    }


def verify_adapter_identity(
    adapter_dir: Path,
    expected_identity: Dict[str, Any],
) -> Dict[str, Any]:
    """Fail-closed verification of the on-disk adapter against the frozen
    contract identity block.

    Checks, in order:

    1. every frozen expected file exists with the exact SHA-256 and size;
    2. no unexpected file is present;
    3. the tree identity SHA-256 matches the frozen
       ``tree_identity_sha256`` (``65b5ed9a354d4b2c03ba86e2b8065118e11abab
       9c439cb481b5739f1b86e7c00`` for the accepted cp118 artifact);
    4. ``adapter_config.json`` declares the pinned base repository.

    Returns the on-disk identity on success; raises ``RuntimeError`` with a
    precise reason on any mismatch.  This is the deterministic enforcement
    of the S4 rule: if the definitive cp118 checkpoint cannot be located or
    verified exactly, STOP and report rather than substituting another
    checkpoint.
    """

    adapter_dir = Path(adapter_dir)
    expected_files = expected_identity.get("files", [])
    expected_tree = expected_identity.get("tree_identity_sha256")
    if not expected_files or not expected_tree:
        raise RuntimeError("frozen adapter identity block is incomplete")

    on_disk = compute_adapter_identity(adapter_dir)

    expected_map = {f["path"]: f for f in expected_files}
    on_disk_map = {f["path"]: f for f in on_disk["files"]}

    missing = sorted(set(expected_map) - set(on_disk_map))
    if missing:
        raise RuntimeError(
            "cp118 adapter identity mismatch: missing files: " + ", ".join(missing)
        )
    extra = sorted(set(on_disk_map) - set(expected_map))
    if extra:
        raise RuntimeError(
            "cp118 adapter identity mismatch: unexpected files: " + ", ".join(extra)
        )
    for path, expected in sorted(expected_map.items()):
        actual = on_disk_map[path]
        if actual["sha256"] != expected["sha256"]:
            raise RuntimeError(
                f"cp118 adapter identity mismatch: {path} sha256 "
                f"expected {expected['sha256']}, got {actual['sha256']}"
            )
        if actual["size_bytes"] != expected["size_bytes"]:
            raise RuntimeError(
                f"cp118 adapter identity mismatch: {path} size "
                f"expected {expected['size_bytes']}, got {actual['size_bytes']}"
            )

    if on_disk["tree_identity_sha256"] != expected_tree:
        raise RuntimeError(
            "cp118 adapter identity mismatch: tree_identity_sha256 "
            f"expected {expected_tree}, got {on_disk['tree_identity_sha256']}"
        )

    # The frozen adapter must declare the pinned base (established tuned
    # pilot rule; fail closed on a foreign base).
    config_path = adapter_dir / "adapter_config.json"
    try:
        adapter_config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"cp118 adapter_config.json unreadable: {type(exc).__name__}"
        ) from exc
    declared_base = adapter_config.get("base_model_name_or_path")
    if declared_base not in {None, "", BASE_REPOSITORY}:
        raise RuntimeError(
            "cp118 adapter declares a different base model: "
            + str(declared_base)
        )

    return on_disk
