"""No-model compatibility tests for the efficient-SDPA package.

These tests run under any Python (including a torch-less interpreter).  They
verify:

* importing the package does not import torch/transformers (import discipline);
* the fail-closed error type and message when required APIs are unavailable;
* the registry conflict-guard logic (absent -> register, same -> no-op,
  different -> error) using a stand-in GeneralInterface so no torch is needed;
* the env_report schema shape with stubs;
* the benchmark metric-naming invariant (end_to_end, not "decode throughput").
"""

from __future__ import annotations

import importlib
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

# --- import discipline ------------------------------------------------------


def test_package_import_is_torch_free():
    """Importing efficient_sdpa must not pull in torch or transformers."""
    for mod in list(sys.modules):
        if mod.split(".")[0] in {"torch", "transformers", "peft", "bitsandbytes"}:
            del sys.modules[mod]
    # fresh import
    for m in ("experiments.local_inference_perf.efficient_sdpa",):
        if m in sys.modules:
            del sys.modules[m]
    importlib.import_module(m)
    # importing the helper must not have loaded torch/transformers.
    assert "torch" not in sys.modules, "efficient_sdpa imported torch at import time"
    assert "transformers" not in sys.modules, (
        "efficient_sdpa imported transformers at import time"
    )


def test_register_raises_without_torch(monkeypatch):
    """When torch is unavailable, register_efficient_sdpa fails closed."""
    from experiments.local_inference_perf import efficient_sdpa as es

    # Simulate a torch-less environment by breaking the import.
    real_import = __builtins__.__import__ if isinstance(__builtins__, dict) is False else __import__

    import builtins

    orig_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.split(".")[0] == "torch":
            raise ImportError("simulated torch absence")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(es.EfficientSdpaError, match="torch"):
        es.register_efficient_sdpa()


# --- conflict guard ---------------------------------------------------------


class _FakeRegistry:
    """Minimal stand-in for transformers GeneralInterface used to test the
    conflict guard without importing transformers."""

    def __init__(self):
        self._mapping = {}

    def __getitem__(self, key):
        if key not in self._mapping:
            raise KeyError(key)
        return self._mapping[key]

    def register(self, key, value):
        self._mapping[key] = value

    def valid_keys(self):
        return list(self._mapping)


def test_conflict_guard_logic():
    """Conflict guard: absent -> ok, same -> no-op, different -> error."""
    from experiments.local_inference_perf.efficient_sdpa import (
        EfficientSdpaError,
        _conflict_guard,
    )

    reg = _FakeRegistry()
    fn_a = object()
    fn_b = object()
    # absent -> ok (no raise)
    _conflict_guard(reg, "k", fn_a, "FakeRegistry")
    # same -> no-op
    _conflict_guard(reg, "k", fn_a, "FakeRegistry")
    # different -> error
    reg._mapping["k"] = fn_b
    with pytest.raises(EfficientSdpaError, match="already bound to a different"):
        _conflict_guard(reg, "k", fn_a, "FakeRegistry")


# --- env_report schema ------------------------------------------------------


def test_env_report_shape_without_model(monkeypatch):
    """env_report.capture_environment returns a dict with all required keys
    even when torch/transformers are absent (values NOT_AVAILABLE)."""
    from experiments.local_inference_perf import env_report

    # Force torch/transformers absent for this test.
    import builtins

    orig_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.split(".")[0] in {"torch", "transformers", "peft", "bitsandbytes"}:
            raise ImportError(f"simulated absence of {name}")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    report = env_report.capture_environment(model=None)
    required = {
        "torch_version",
        "cuda_runtime_version",
        "gpu_name",
        "transformers_version",
        "peft_version",
        "bitsandbytes_version",
        "torch_flash_sdp_available",
        "hf_flash_attn2_package_available",
        "sdp_backends",
        "active_attention_implementation",
        "model_device_map",
        "use_cache",
    }
    for key in required:
        assert key in report, f"missing {key}"
        assert report[key] == "NOT_AVAILABLE", (
            f"{key} should be NOT_AVAILABLE without torch; got {report[key]!r}"
        )


def test_env_report_dual_flash_flags_present():
    """The two flash flags must be distinct keys (amendment 4)."""
    from experiments.local_inference_perf import env_report

    report = env_report.capture_environment(model=None)
    assert "torch_flash_sdp_available" in report
    assert "hf_flash_attn2_package_available" in report
    assert "torch_flash_sdp_available" != "hf_flash_attn2_package_available"


# --- benchmark metric naming ------------------------------------------------


def test_benchmark_metric_naming():
    """The result schema must use end_to_end_output_tokens_per_second, not a
    bare 'decode throughput' label (amendment 3)."""
    from experiments.local_inference_perf.benchmark import CellResult

    cr = CellResult(
        prompt_class="short",
        max_new_tokens=1,
        actual_input_tokens=10,
        output_tokens=1,
        total_elapsed_s=0.5,
        peak_allocated_mib=100.0,
        reserved_mib=120.0,
        free_cuda_mib_before=8000.0,
        free_cuda_mib_after=7900.0,
        model_load_time_s=20.0,
        backend="x",
        attn_implementation="efficient_sdpa",
        use_cache=True,
        device_map={},
        end_to_end_output_tokens_per_second=2.0,
    )
    j = cr.to_json()
    assert "end_to_end_output_tokens_per_second" in j
    assert "output_tokens_per_second" not in j  # no ambiguous bare label
    assert "decode_throughput" not in j
    # approx field exists and defaults to None
    assert "approx_decode_output_tokens_per_second" in j
    assert j["approx_decode_output_tokens_per_second"] is None


def test_benchmark_backends_resolve():
    from experiments.local_inference_perf.benchmark import resolve_backend

    eff = resolve_backend("efficient")
    assert eff.attn_implementation == "efficient_sdpa"
    assert eff.register_first is True
    assert eff.require_gpu_placement is True
    stock = resolve_backend("stock")
    assert stock.attn_implementation == "sdpa"
    assert stock.register_first is False
    with pytest.raises(ValueError):
        resolve_backend("bogus")


def test_build_prompt_deterministic():
    from experiments.local_inference_perf.benchmark import build_prompt

    a = build_prompt(1000)
    b = build_prompt(1000)
    assert a == b
    assert len(a) == 1000


def test_default_matrix_and_long_gen():
    from experiments.local_inference_perf.benchmark import (
        DEFAULT_MATRIX,
        LONG_GEN_CELL,
    )

    assert LONG_GEN_CELL not in DEFAULT_MATRIX
    assert ("long", 256) in DEFAULT_MATRIX
    assert ("long", 1024) not in DEFAULT_MATRIX


# --- BLOCKER 1: transformers version gate -----------------------------------


def test_version_gate_accepts_supported():
    """The pure version helper accepts exactly the supported version."""
    from experiments.local_inference_perf.efficient_sdpa import (
        SUPPORTED_TRANSFORMERS_VERSION,
        validate_transformers_version,
    )

    # Must not raise.
    validate_transformers_version(SUPPORTED_TRANSFORMERS_VERSION)


def test_version_gate_rejects_mismatch():
    """A mismatched version fails closed with a clear message reporting both
    versions and the internal-API reason."""
    from experiments.local_inference_perf.efficient_sdpa import (
        SUPPORTED_TRANSFORMERS_VERSION,
        EfficientSdpaError,
        validate_transformers_version,
    )

    with pytest.raises(EfficientSdpaError) as exc:
        validate_transformers_version("4.58.0")
    msg = str(exc.value)
    assert "4.58.0" in msg
    assert SUPPORTED_TRANSFORMERS_VERSION in msg
    assert "version-bound" in msg or "internal" in msg.lower()


def test_version_gate_rejects_major_bump():
    from experiments.local_inference_perf.efficient_sdpa import (
        EfficientSdpaError,
        validate_transformers_version,
    )

    with pytest.raises(EfficientSdpaError):
        validate_transformers_version("5.0.0")


def test_version_gate_rejects_unknown():
    from experiments.local_inference_perf.efficient_sdpa import (
        EfficientSdpaError,
        validate_transformers_version,
    )

    with pytest.raises(EfficientSdpaError):
        validate_transformers_version("UNKNOWN")


# --- BLOCKER 2: parity policy fail-closed ----------------------------------


def _accept_metrics():
    from experiments.local_inference_perf.parity import (
        COSINE_MIN,
        MAX_ABS_DIFF_TOL,
        MEAN_ABS_DIFF_TOL,
    )

    return {
        "same_top_token": True,
        "max_abs_diff": 0.125,  # <= 0.25
        "mean_abs_diff": 0.014,  # <= 0.02
        "cosine_similarity": 0.99995,  # >= 0.9999
    }


def test_parity_policy_accepts_proven_evidence():
    """The accepted real evidence (same_top=True, 0.125, 0.014, 0.99995) must
    pass the shared policy."""
    from experiments.local_inference_perf.parity import enforce_parity_policy

    enforce_parity_policy(_accept_metrics())  # must not raise


def test_parity_policy_rejects_top_token_mismatch():
    from experiments.local_inference_perf.parity import (
        ParityError,
        enforce_parity_policy,
    )

    m = _accept_metrics()
    m["same_top_token"] = False
    with pytest.raises(ParityError, match="same_top_token") as exc:
        enforce_parity_policy(m)
    assert exc.value.metrics["same_top_token"] is False


def test_parity_policy_rejects_low_cosine():
    from experiments.local_inference_perf.parity import (
        ParityError,
        enforce_parity_policy,
    )

    m = _accept_metrics()
    m["cosine_similarity"] = 0.95  # < COSINE_MIN
    with pytest.raises(ParityError, match="cosine") as exc:
        enforce_parity_policy(m)
    assert "cosine" in str(exc.value)


def test_parity_policy_rejects_high_max_abs():
    from experiments.local_inference_perf.parity import (
        ParityError,
        enforce_parity_policy,
    )

    m = _accept_metrics()
    m["max_abs_diff"] = 0.5  # > MAX_ABS_DIFF_TOL
    with pytest.raises(ParityError, match="max_abs_diff") as exc:
        enforce_parity_policy(m)
    assert "max_abs_diff" in str(exc.value)


def test_parity_policy_rejects_high_mean_abs():
    from experiments.local_inference_perf.parity import (
        ParityError,
        enforce_parity_policy,
    )

    m = _accept_metrics()
    m["mean_abs_diff"] = 0.1  # > MEAN_ABS_DIFF_TOL
    with pytest.raises(ParityError, match="mean_abs_diff") as exc:
        enforce_parity_policy(m)
    assert "mean_abs_diff" in str(exc.value)


def test_parity_policy_rejects_none_metrics():
    """None metrics (e.g. candidate backend was unavailable) must fail closed."""
    from experiments.local_inference_perf.parity import (
        ParityError,
        enforce_parity_policy,
    )

    m = {
        "same_top_token": None,
        "max_abs_diff": None,
        "mean_abs_diff": None,
        "cosine_similarity": None,
    }
    with pytest.raises(ParityError) as exc:
        enforce_parity_policy(m)
    # all four failures reported
    msg = str(exc.value)
    assert "same_top_token" in msg
    assert "cosine" in msg
    assert "max_abs_diff" in msg
    assert "mean_abs_diff" in msg


def test_parity_error_carries_metrics():
    """ParityError must preserve the measured metrics for diagnosis."""
    from experiments.local_inference_perf.parity import (
        ParityError,
        enforce_parity_policy,
    )

    m = _accept_metrics()
    m["cosine_similarity"] = 0.5
    try:
        enforce_parity_policy(m)
    except ParityError as exc:
        assert exc.metrics == m
        assert exc.metrics["cosine_similarity"] == 0.5


# --- BLOCKER 3: placement guard --------------------------------------------


def _loader(require_gpu=True):
    from experiments.local_inference_perf.model_loader import (
        LocalQwenAdapterLoader,
    )

    return LocalQwenAdapterLoader(
        adapter_path="/nonexistent/adapter",
        attn_implementation="efficient_sdpa",
        require_gpu_placement=require_gpu,
    )


def test_placement_guard_rejects_empty_map():
    """In require_gpu mode an empty/unavailable device map must fail closed."""
    loader = _loader(require_gpu=True)
    with pytest.raises(Exception, match="empty|unavailable|resolved GPU"):
        loader._assert_gpu_placement({})


def test_placement_guard_rejects_cpu():
    loader = _loader(require_gpu=True)
    with pytest.raises(Exception, match="non-GPU|CPU"):
        loader._assert_gpu_placement({"": "cpu"})


def test_placement_guard_rejects_disk():
    loader = _loader(require_gpu=True)
    with pytest.raises(Exception, match="non-GPU|disk"):
        loader._assert_gpu_placement({"": "disk"})


def test_placement_guard_rejects_offload():
    loader = _loader(require_gpu=True)
    with pytest.raises(Exception, match="non-GPU|offload"):
        loader._assert_gpu_placement({"lm_head": "cpu", "model.embed_tokens": "disk"})


def test_placement_guard_accepts_resolved_gpu_map():
    """The normal resolved CUDA mapping {\"\": \"0\"} must be accepted."""
    loader = _loader(require_gpu=True)
    # Must not raise.
    loader._assert_gpu_placement({"": "0"})


def test_placement_guard_accepts_multi_gpu_map():
    """A multi-GPU resolved mapping must be accepted (only CPU/disk/offload
    are rejected)."""
    loader = _loader(require_gpu=True)
    loader._assert_gpu_placement({"model.embed_tokens": "0", "lm_head": "1"})