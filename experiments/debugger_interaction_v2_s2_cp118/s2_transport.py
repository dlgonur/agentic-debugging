"""S2 — cp118 transport: the ONLY material model-condition change vs D1.

S2 replaces the frozen D1 model condition (RAW Qwen2.5-Coder-7B-Instruct)
with the definitive surviving cp118 tuned checkpoint.  Everything else in
the D1 treatment stays unchanged.

This module provides:

1. ``verify_adapter_identity(adapter_dir, expected_identity)`` — a pure,
   fail-closed verifier that proves the on-disk adapter directory is
   byte-exact against the frozen S2 contract identity (every file's
   SHA-256 + size, no extra files, tree identity SHA-256).  It is used by
   ``--validate-only`` (no model load) and again by the transport ``__init__``
   (defense in depth, before the expensive GPU load).

2. ``LocalCp118QwenTransport`` — a subclass of the frozen S1
   ``LocalRawQwenTransport`` whose ONLY change is the model condition:
   ``__init__`` attaches the verified cp118 PEFT/QLoRA adapter to the
   identical pinned base via the established tuned-pilot loading mechanism
   (``PeftModel.from_pretrained(base, adapter_path, is_trainable=False)``,
   same 4-bit NF4 double-quantization base load).  The inherited ``request``
   method is byte-identical to the frozen S1 transport (same tokenizer
   chat-template application, same generation call, same raw-text
   retention, same ``TransportResponse`` envelope, same error categories) —
   the test suite asserts this by source equality.

The S2 runner passes the frozen contract's ``model.adapter_identity`` block
as ``expected_identity``; the contract is the single source of truth for the
checkpoint identity.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from experiments.debugger_interaction_v2.transport import (
    LocalRawQwenTransport,
)

# ---------------------------------------------------------------------------
# Adapter identity verification (pure, no model/GPU dependency)
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_adapter_identity(adapter_dir: Path) -> dict[str, Any]:
    """Compute the on-disk adapter identity (tree hash + per-file records)."""

    adapter_dir = Path(adapter_dir)
    if not adapter_dir.is_dir():
        raise RuntimeError(f"adapter path is not a directory: {adapter_dir}")
    files = []
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
    expected_identity: dict[str, Any],
) -> dict[str, Any]:
    """Fail-closed verification of the on-disk adapter against the frozen
    contract identity.

    Checks, in order:

    1. every frozen expected file exists with the exact SHA-256 and size;
    2. no unexpected file is present;
    3. the tree identity SHA-256 (over relative path + digest bytes, same
       convention as the accepted tuned pilot ``run_pilot._adapter_identity``)
       matches the frozen ``tree_identity_sha256``.

    Returns the on-disk identity on success; raises ``RuntimeError`` with a
    precise reason on any mismatch.  This is the deterministic enforcement
    of the S2 rule: if the definitive cp118 checkpoint cannot be located or
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
    from experiments.debugger_interaction_v2.transport import BASE_REPOSITORY

    if declared_base not in {None, "", BASE_REPOSITORY}:
        raise RuntimeError(
            "cp118 adapter declares a different base model: "
            + str(declared_base)
        )

    return on_disk


# ---------------------------------------------------------------------------
# The cp118 transport (model-condition-only change vs frozen D1/S1)
# ---------------------------------------------------------------------------


class LocalCp118QwenTransport(LocalRawQwenTransport):
    """Local pinned Qwen2.5-Coder-7B + definitive cp118 PEFT adapter.

    The ONLY material change relative to the frozen S1 ``LocalRawQwenTransport``
    is the model condition: the verified cp118 adapter is attached to the
    identical pinned base through the established tuned-pilot loading
    mechanism.  ``request`` is inherited byte-identical (raw-text retention,
    tokenizer chat template, generation call, ``TransportResponse``
    envelope, error categories, budgets).

    The adapter identity is re-verified fail-closed in ``__init__`` BEFORE
    the expensive base load.
    """

    def __init__(
        self,
        *,
        adapter_path: str,
        expected_adapter_identity: dict[str, Any],
        max_new_tokens: int = 1024,
        max_input_tokens: int = 32768,
        request_timeout_seconds: float = 60.0,
    ) -> None:
        # Fail closed BEFORE any GPU load: the definitive cp118 checkpoint
        # must be located and verified exactly.
        self.adapter_identity = verify_adapter_identity(
            Path(adapter_path), expected_adapter_identity
        )

        # Load the pinned base through the identical frozen path.
        super().__init__(
            max_new_tokens=max_new_tokens,
            max_input_tokens=max_input_tokens,
            request_timeout_seconds=request_timeout_seconds,
        )

        # Attach the verified cp118 adapter (established PEFT/QLoRA loading
        # mechanism from the accepted tuned pilot: PeftModel.from_pretrained
        # on the quantized base, is_trainable=False).
        try:
            from peft import PeftModel
        except ImportError as exc:
            raise RuntimeError(
                "cp118 Qwen transport requires peft for adapter loading"
            ) from exc

        self.model = PeftModel.from_pretrained(
            self.model,
            str(Path(adapter_path).resolve()),
            is_trainable=False,
        )
        self.model.eval()
        self.device = next(self.model.parameters()).device


__all__ = [
    "LocalCp118QwenTransport",
    "verify_adapter_identity",
    "compute_adapter_identity",
]
