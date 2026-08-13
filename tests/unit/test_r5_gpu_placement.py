from __future__ import annotations

from dataclasses import dataclass

import pytest

from experiments.debugger_interaction_v2_r5.gpu_placement import (
    audit_single_cuda_placement,
    explicit_cuda_device_map,
)


@dataclass
class _Tensor:
    device: object


class _Model:
    def __init__(self, device_map, parameter_devices, buffer_devices=()):
        self.hf_device_map = device_map
        self._parameter_devices = parameter_devices
        self._buffer_devices = buffer_devices

    def named_parameters(self):
        return iter(
            (f"parameter_{index}", _Tensor(device))
            for index, device in enumerate(self._parameter_devices)
        )

    def named_buffers(self):
        return iter(
            (f"buffer_{index}", _Tensor(device))
            for index, device in enumerate(self._buffer_devices)
        )


def test_explicit_single_cuda_placement_is_audited() -> None:
    model = _Model(
        {"": 0, "lm_head": "cuda:0"},
        ["cuda:0", "cuda"],
        ["cuda:0"],
    )

    audit = audit_single_cuda_placement(model, expected_device_index=0)

    assert explicit_cuda_device_map(0) == {"": 0}
    assert audit["passed"] is True
    assert audit["requested_device_map"] == {"": 0}
    assert audit["resolved_module_device_map"] == {
        "": "cuda:0",
        "lm_head": "cuda:0",
    }
    assert audit["parameter_tensor_devices"] == {"cuda:0": 2}
    assert audit["buffer_tensor_devices"] == {"cuda:0": 1}


@pytest.mark.parametrize(
    ("device_map", "parameters", "buffers"),
    [
        ({"": "cpu"}, ["cuda:0"], []),
        ({"": "disk"}, ["cuda:0"], []),
        ({"": 0}, ["cpu"], []),
        ({"": 0}, ["cuda:0"], ["meta"]),
    ],
)
def test_placement_audit_rejects_offload_or_non_cuda_tensors(
    device_map, parameters, buffers
) -> None:
    with pytest.raises(RuntimeError, match="explicit CUDA placement rejected"):
        audit_single_cuda_placement(
            _Model(device_map, parameters, buffers),
            expected_device_index=0,
        )


def test_placement_audit_requires_resolved_map_and_parameters() -> None:
    with pytest.raises(RuntimeError, match="hf_device_map is missing"):
        audit_single_cuda_placement(_Model({}, ["cuda:0"]))
    with pytest.raises(RuntimeError, match="no model parameters"):
        audit_single_cuda_placement(_Model({"": 0}, []))


def test_explicit_device_map_rejects_invalid_indices() -> None:
    with pytest.raises(RuntimeError, match="must be an integer"):
        explicit_cuda_device_map(True)
    with pytest.raises(RuntimeError, match="must be non-negative"):
        explicit_cuda_device_map(-1)
