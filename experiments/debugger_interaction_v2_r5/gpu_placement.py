"""Fail-closed CUDA placement policy for the local R5/R6 Qwen transports.

The evaluator deliberately requests one complete model placement on one CUDA
device.  It never asks Accelerate to infer a placement and never permits CPU,
disk, or meta tensors after load.  The returned audit is JSON-compatible so a
run can preserve both the requested module map and the observed tensor devices.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any


PLACEMENT_POLICY_VERSION = "explicit-single-cuda-v1"


def explicit_cuda_device_map(device_index: int = 0) -> dict[str, int]:
    if isinstance(device_index, bool) or not isinstance(device_index, int):
        raise RuntimeError("CUDA device index must be an integer")
    if device_index < 0:
        raise RuntimeError("CUDA device index must be non-negative")
    return {"": device_index}


def _canonical_device(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return f"cuda:{value}"
    rendered = str(value).strip().lower()
    if rendered == "cuda":
        return "cuda:0"
    if rendered.isdecimal():
        return f"cuda:{rendered}"
    return rendered


def _tensor_device_counts(items: Any) -> tuple[dict[str, int], int]:
    counts: Counter[str] = Counter()
    total = 0
    for _name, tensor in items:
        counts[_canonical_device(getattr(tensor, "device", "unavailable"))] += 1
        total += 1
    return dict(sorted(counts.items())), total


def audit_single_cuda_placement(
    model: Any,
    *,
    expected_device_index: int = 0,
) -> dict[str, Any]:
    """Return a placement audit or fail if any model state is not on CUDA.

    ``hf_device_map`` is required even though the loader receives an explicit
    map: without the resolved map the evaluator cannot prove that Transformers
    honored the requested no-offload policy.  Parameter and buffer devices are
    checked independently so a superficially valid module map cannot conceal a
    CPU/meta tensor.
    """

    requested = explicit_cuda_device_map(expected_device_index)
    expected = f"cuda:{expected_device_index}"
    raw_map = getattr(model, "hf_device_map", None)
    if not isinstance(raw_map, Mapping) or not raw_map:
        raise RuntimeError(
            "explicit CUDA placement cannot be proven: hf_device_map is missing"
        )

    resolved = {
        str(module): _canonical_device(device)
        for module, device in sorted(raw_map.items(), key=lambda item: str(item[0]))
    }
    wrong_modules = {
        module: device for module, device in resolved.items() if device != expected
    }
    if wrong_modules:
        raise RuntimeError(
            "explicit CUDA placement rejected non-target modules: "
            f"{wrong_modules}"
        )

    try:
        parameter_counts, parameter_total = _tensor_device_counts(
            model.named_parameters()
        )
        buffer_counts, buffer_total = _tensor_device_counts(model.named_buffers())
    except Exception as exc:
        raise RuntimeError(
            f"explicit CUDA placement tensor audit failed: {type(exc).__name__}: {exc}"
        ) from exc

    if parameter_total <= 0:
        raise RuntimeError("explicit CUDA placement found no model parameters")
    wrong_parameter_devices = {
        device: count
        for device, count in parameter_counts.items()
        if device != expected
    }
    wrong_buffer_devices = {
        device: count for device, count in buffer_counts.items() if device != expected
    }
    if wrong_parameter_devices or wrong_buffer_devices:
        raise RuntimeError(
            "explicit CUDA placement rejected non-target tensors: "
            f"parameters={wrong_parameter_devices}, buffers={wrong_buffer_devices}"
        )

    return {
        "policy_version": PLACEMENT_POLICY_VERSION,
        "expected_device": expected,
        "requested_device_map": requested,
        "resolved_module_device_map": resolved,
        "parameter_tensor_devices": parameter_counts,
        "parameter_tensor_count": parameter_total,
        "buffer_tensor_devices": buffer_counts,
        "buffer_tensor_count": buffer_total,
        "cpu_modules": [],
        "disk_modules": [],
        "meta_tensors": 0,
        "passed": True,
    }


__all__ = [
    "PLACEMENT_POLICY_VERSION",
    "audit_single_cuda_placement",
    "explicit_cuda_device_map",
]
