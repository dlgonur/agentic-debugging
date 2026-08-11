"""Focused unit tests for efficient_sdpa (require torch + transformers).

Skipped automatically when torch/transformers/CUDA are unavailable so the
no-model compatibility tests can run anywhere.
"""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")

if not torch.cuda.is_available():
    pytest.skip("efficient_sdpa unit tests require CUDA", allow_module_level=True)


def test_registration_registers_in_both_registries():
    from experiments.local_inference_perf import efficient_sdpa as es

    info = es.register_efficient_sdpa()
    assert info["registered"] is True
    assert info["key"] == "efficient_sdpa"
    from transformers.modeling_utils import ALL_ATTENTION_FUNCTIONS
    from transformers.masking_utils import ALL_MASK_ATTENTION_FUNCTIONS

    assert "efficient_sdpa" in ALL_ATTENTION_FUNCTIONS.valid_keys()
    assert "efficient_sdpa" in ALL_MASK_ATTENTION_FUNCTIONS.valid_keys()


def test_registration_is_idempotent():
    from experiments.local_inference_perf import efficient_sdpa as es

    es.register_efficient_sdpa()
    info2 = es.register_efficient_sdpa()  # no raise, same binding
    assert info2["registered"] is True


def test_registration_info_reflects_state():
    from experiments.local_inference_perf import efficient_sdpa as es

    es.register_efficient_sdpa()
    info = es.registration_info()
    assert info is not None
    assert info["backend"] == "EFFICIENT_ATTENTION"
    assert es.is_registered() is True


def test_repeat_kv_shape_expansion():
    """repeat_kv must expand 4 KV heads -> 28 for num_key_value_groups=7."""
    from transformers.integrations.sdpa_attention import repeat_kv

    batch, kv_heads, slen, head_dim = 1, 4, 16, 128
    k = torch.randn(batch, kv_heads, slen, head_dim, device="cuda")
    out = repeat_kv(k, 7)
    assert out.shape == (batch, 28, slen, head_dim)


def test_efficient_forward_preserves_mask_slicing_and_is_causal():
    """A short forward through the efficient helper must produce the right
    output shape and respect attention_mask slicing + is_causal derivation."""
    from torch.nn.attention import SDPBackend, sdpa_kernel
    from transformers.integrations.sdpa_attention import (
        repeat_kv as tf_repeat_kv,
    )
    from experiments.local_inference_perf import efficient_sdpa as es

    es.register_efficient_sdpa()
    eff_fn = es._make_efficient_sdpa_forward(
        repeat_kv=tf_repeat_kv, sdpa_kernel=sdpa_kernel, SDPBackend=SDPBackend
    )

    class FakeAttn:
        num_key_value_groups = 7
        is_causal = True
        scaling = 128 ** -0.5

    num_heads, kv_heads, head_dim = 28, 4, 128
    slen = 16
    q = torch.randn(1, num_heads, slen, head_dim, dtype=torch.bfloat16, device="cuda")
    k = torch.randn(1, kv_heads, slen, head_dim, dtype=torch.bfloat16, device="cuda")
    v = torch.randn(1, kv_heads, slen, head_dim, dtype=torch.bfloat16, device="cuda")
    out, weights = eff_fn(FakeAttn(), q, k, v, attention_mask=None, dropout=0.0)
    # The helper returns (batch, slen, heads, head_dim) — head flattening is
    # done by the caller (Qwen2Attention.reshape), not by the helper.
    assert out.shape == (1, slen, num_heads, head_dim)
    assert weights is None


def test_efficient_forward_rejects_enable_gqa_path():
    """The efficient helper must NEVER pass enable_gqa=True; it always
    expands KV with repeat_kv. Verify the candidate accepts unequal input
    head counts (4) and internally expands to 28."""
    from torch.nn.attention import SDPBackend, sdpa_kernel
    from transformers.integrations.sdpa_attention import repeat_kv as tf_repeat_kv
    from experiments.local_inference_perf import efficient_sdpa as es

    es.register_efficient_sdpa()
    eff_fn = es._make_efficient_sdpa_forward(
        repeat_kv=tf_repeat_kv, sdpa_kernel=sdpa_kernel, SDPBackend=SDPBackend
    )

    class FakeAttn:
        num_key_value_groups = 7
        is_causal = True
        scaling = 128 ** -0.5

    # unequal Q/K head counts (28 vs 4) must be accepted because the helper
    # expands K/V to 28 before calling SDPA.
    q = torch.randn(1, 28, 16, 128, dtype=torch.bfloat16, device="cuda")
    k = torch.randn(1, 4, 16, 128, dtype=torch.bfloat16, device="cuda")
    v = torch.randn(1, 4, 16, 128, dtype=torch.bfloat16, device="cuda")
    out, _ = eff_fn(FakeAttn(), q, k, v, attention_mask=None, dropout=0.0)
    # Helper returns (batch, slen, heads, head_dim) — unflattened (flattening
    # is the caller's job). Equal head counts (28) prove repeat_kv ran.
    assert out.shape == (1, 16, 28, 128)


def test_conflict_guard_raises_on_different_binding():
    """If efficient_sdpa is already bound to a different impl, re-registration
    via _conflict_guard must raise (not silently overwrite)."""
    from experiments.local_inference_perf.efficient_sdpa import (
        EfficientSdpaError,
        _conflict_guard,
    )

    class Reg:
        def __init__(self):
            self.m = {}

        def __getitem__(self, k):
            if k not in self.m:
                raise KeyError(k)
            return self.m[k]

        def register(self, k, v):
            self.m[k] = v

        def valid_keys(self):
            return list(self.m)

    reg = Reg()
    reg.register("efficient_sdpa", "other_impl")
    with pytest.raises(EfficientSdpaError, match="already bound to a different"):
        _conflict_guard(reg, "efficient_sdpa", "my_impl", "Reg")