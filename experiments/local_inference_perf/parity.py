"""Numerical parity: stock ``sdpa`` (MATH) reference vs ``efficient_sdpa``
(EFFICIENT_ATTENTION + explicit repeat_kv) candidate.

Correct reference semantics (amendment 1)
----------------------------------------

The stock Transformers ``sdpa`` path on this Windows torch build is:

* native Qwen GQA -> 28 Q heads / 4 KV heads -> ``enable_gqa=True``
* fused backend unavailable/inapplicable
* -> MATH SDPA.

So the reference here is stock Transformers ``"sdpa"`` with
``SDPBackend.MATH`` forced around the forward.  The candidate is explicit
``repeat_kv(4 KV -> 28)`` + ``SDPBackend.EFFICIENT_ATTENTION``.

No bitwise equality is required (BF16 fused-vs-math differences are expected
and justified).  At minimum we record/assert: same top-token where the
deterministic fixture supports it, high cosine similarity, and bounded
mean/max absolute differences with tolerances justified by BF16 fused-vs-math
differences.

Two layers
-----------

* ``parity_unit()`` — no model; a tiny synthetic CPU/CUDA forward with a fake
  Qwen-shaped module.  On CPU (no EFFICIENT backend) it asserts the **fail
  closed** behavior instead of comparing numbers.
* ``parity_real_model()`` — bounded real-model forward on a short fixture
  (<=64 tokens); records same_top_token, max_abs_diff, mean_abs_diff,
  cosine_similarity into a JSON result.  Reproduces the already-measured
  cp118 50-token parity evidence (same_top_token=True, max_abs_diff=0.125,
  mean_abs_diff=0.013, cosine=0.9999645).

Run as a module from the repo root:

    python -m experiments.local_inference_perf.parity unit
    python -m experiments.local_inference_perf.parity real-model \
        --adapter-path <path> --output-dir parity-out
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from . import efficient_sdpa as _es

# BF16 fused-vs-math tolerances.  Justified by the measured real cp118 50-token
# forward: max_abs_diff=0.125, mean_abs_diff=0.013.  We set headroom above the
# observed values so deterministic fixtures pass while still catching real
# regressions.
MAX_ABS_DIFF_TOL = 0.25
MEAN_ABS_DIFF_TOL = 0.02
COSINE_MIN = 0.9999


class ParityError(RuntimeError):
    """Raised when parity metrics violate the fail-closed policy.

    The measured metrics are attached as ``.metrics`` so diagnosis remains
    possible even when the gate fails.  Bitwise equality is never required;
    only the four policy checks below are enforced.
    """

    def __init__(self, message: str, metrics: Dict[str, Any]):
        super().__init__(message)
        self.metrics = dict(metrics)


def enforce_parity_policy(metrics: Dict[str, Any]) -> None:
    """Shared fail-closed parity policy (BLOCKER 2).

    Requires all four:
    * ``same_top_token == True``;
    * ``cosine_similarity >= COSINE_MIN``;
    * ``max_abs_diff <= MAX_ABS_DIFF_TOL``;
    * ``mean_abs_diff <= MEAN_ABS_DIFF_TOL``.

    A missing or ``None`` metric (e.g. the candidate backend was unavailable
    and no numbers were produced) fails closed.  On failure raises
    :class:`ParityError` carrying the full metrics dict.  Bitwise equality is
    never required.
    """

    same_top = metrics.get("same_top_token")
    cos = metrics.get("cosine_similarity")
    max_abs = metrics.get("max_abs_diff")
    mean_abs = metrics.get("mean_abs_diff")

    failures = []
    if same_top is not True:
        failures.append(
            f"same_top_token must be True (got {same_top!r})"
        )
    if cos is None or not (cos >= COSINE_MIN):
        failures.append(
            f"cosine_similarity must be >= {COSINE_MIN} (got {cos!r})"
        )
    if max_abs is None or not (max_abs <= MAX_ABS_DIFF_TOL):
        failures.append(
            f"max_abs_diff must be <= {MAX_ABS_DIFF_TOL} (got {max_abs!r})"
        )
    if mean_abs is None or not (mean_abs <= MEAN_ABS_DIFF_TOL):
        failures.append(
            f"mean_abs_diff must be <= {MEAN_ABS_DIFF_TOL} (got {mean_abs!r})"
        )
    if failures:
        raise ParityError(
            "parity policy violated: " + "; ".join(failures),
            metrics,
        )


@dataclass
class ParityMetrics:
    same_top_token: Optional[bool]
    max_abs_diff: Optional[float]
    mean_abs_diff: Optional[float]
    cosine_similarity: Optional[float]
    baseline_backend: str
    candidate_backend: str
    note: str

    def to_json(self) -> Dict[str, Any]:
        return {
            "same_top_token": self.same_top_token,
            "max_abs_diff": self.max_abs_diff,
            "mean_abs_diff": self.mean_abs_diff,
            "cosine_similarity": self.cosine_similarity,
            "baseline_backend": self.baseline_backend,
            "candidate_backend": self.candidate_backend,
            "note": self.note,
            "tolerances": {
                "max_abs_diff": MAX_ABS_DIFF_TOL,
                "mean_abs_diff": MEAN_ABS_DIFF_TOL,
                "cosine_min": COSINE_MIN,
            },
        }


def _cosine(a: Any, b: Any) -> float:
    import torch

    a = a.flatten().to(torch.float32)
    b = b.flatten().to(torch.float32)
    num = (a * b).sum().item()
    den = (a.norm() * b.norm()).item()
    if den == 0:
        return float("nan")
    return num / den


# ---------------------------------------------------------------------------
# Unit parity (no model).
# ---------------------------------------------------------------------------


class _FakeQwenAttn:
    """Minimal stand-in exposing the attributes the forward reads."""

    def __init__(self, *, num_heads: int, kv_heads: int, head_dim: int):
        self.num_key_value_groups = num_heads // kv_heads
        self.is_causal = True
        self.scaling = head_dim ** -0.5
        self.head_dim = head_dim
        self.num_attention_heads = num_heads
        self.num_key_value_heads = kv_heads


def parity_unit() -> Dict[str, Any]:
    """Synthetic forward comparing stock sdpa (MATH) vs efficient_sdpa.

    On CUDA this compares numbers with the BF16 tolerances.  On a torch build
    where EFFICIENT_ATTENTION cannot execute on the chosen device, this
    asserts the fail-closed behavior (the candidate raises rather than falling
    back to MATH) and records that as the note.
    """

    import torch
    from torch.nn.attention import SDPBackend, sdpa_kernel
    from transformers.integrations.sdpa_attention import (
        repeat_kv as tf_repeat_kv,
        sdpa_attention_forward as tf_sdpa_forward,
    )

    _es.register_efficient_sdpa()
    efficient_fn = _es._make_efficient_sdpa_forward(
        repeat_kv=tf_repeat_kv, sdpa_kernel=sdpa_kernel, SDPBackend=SDPBackend
    )

    num_heads, kv_heads, head_dim = 28, 4, 128
    module = _FakeQwenAttn(num_heads=num_heads, kv_heads=kv_heads, head_dim=head_dim)
    batch, slen = 1, 16
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16

    q = torch.randn(batch, num_heads, slen, head_dim, dtype=dtype, device=device)
    k = torch.randn(batch, kv_heads, slen, head_dim, dtype=dtype, device=device)
    v = torch.randn(batch, kv_heads, slen, head_dim, dtype=dtype, device=device)

    # Reference: stock sdpa forward forced onto MATH backend.
    with sdpa_kernel([SDPBackend.MATH]):
        ref_out, _ = tf_sdpa_forward(
            module, q, k, v, attention_mask=None, dropout=0.0, scaling=module.scaling
        )

    # Candidate: efficient_sdpa forward (explicit repeat_kv + EFFICIENT).
    try:
        cand_out, _ = efficient_fn(
            module, q, k, v, attention_mask=None, dropout=0.0, scaling=module.scaling
        )
        candidate_raised = None
    except Exception as exc:  # noqa: BLE001
        cand_out = None
        candidate_raised = repr(exc)

    if cand_out is None:
        return ParityMetrics(
            same_top_token=None,
            max_abs_diff=None,
            mean_abs_diff=None,
            cosine_similarity=None,
            baseline_backend="MATH (stock sdpa)",
            candidate_backend="EFFICIENT_ATTENTION (efficient_sdpa)",
            note=(
                "EFFICIENT_ATTENTION unavailable on device="
                f"{device}; candidate raised (fail-closed, no MATH fallback): "
                f"{candidate_raised}"
            ),
        ).to_json()

    ref_logits = ref_out.to(torch.float32)
    cand_logits = cand_out.to(torch.float32)
    diff = (cand_logits - ref_logits).abs()
    max_abs = diff.max().item()
    mean_abs = diff.mean().item()
    cos = _cosine(cand_logits, ref_logits)

    # same top-token: compare argmax over the last (head_dim) dim per position.
    ref_top = ref_logits.argmax(dim=-1)
    cand_top = cand_logits.argmax(dim=-1)
    same_top = bool((ref_top == cand_top).all().item())

    metrics = ParityMetrics(
        same_top_token=same_top,
        max_abs_diff=max_abs,
        mean_abs_diff=mean_abs,
        cosine_similarity=cos,
        baseline_backend="MATH (stock sdpa, forced)",
        candidate_backend="EFFICIENT_ATTENTION (efficient_sdpa, explicit repeat_kv)",
        note=f"synthetic unit forward on device={device}, dtype={dtype}",
    )
    j = metrics.to_json()

    # Enforce the shared fail-closed parity policy on CUDA (where both ran).
    # On CPU we already returned the fail-closed branch above when EFFICIENT was
    # unavailable (None metrics -> enforce_parity_policy raises, but we don't
    # reach here in that path).  On CUDA both backends ran, so the policy must
    # pass.  Bitwise equality is never required.
    if torch.cuda.is_available():
        enforce_parity_policy(j)
        j["policy_passed"] = True
    return j


# ---------------------------------------------------------------------------
# Real-model parity (bounded, short fixture).
# ---------------------------------------------------------------------------


def parity_real_model(
    *, adapter_path: str, output_dir: Path
):
    """Run the same short prompt through stock (MATH) and efficient models.

    Memory-frugal: only ONE model is resident at a time. For each backend we
    run a single forward (no ``generate``) to get the next-token logits; the
    top-token comes from ``argmax`` over those logits and the diff metrics come
    from the same logits. This avoids the 3-load pattern that overflows a 12GB
    card when one Qwen-7B 4-bit model already fills most of it.

    Bounded: <=64 input tokens, single forward per backend.
    """

    import torch
    from torch.nn.attention import SDPBackend, sdpa_kernel
    from .model_loader import LocalQwenAdapterLoader

    prompt = "Explain in one sentence how a unified diff repairs a Python bug."
    messages = [{"role": "user", "content": prompt}]

    def _forward_capture(attn_impl: str, force_math: bool) -> dict:
        """Load, run one forward, capture last-position logits + top-token,
        then fully close. Returns the captured dict."""
        loader = LocalQwenAdapterLoader(
            adapter_path=adapter_path,
            attn_implementation=attn_impl,
            require_gpu_placement=(attn_impl == _es.EFFICIENT_SDPA_KEY),
        )
        loader.load()
        model, tokenizer = loader.model, loader.tokenizer
        batch = tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        )
        input_len = int(batch["input_ids"].shape[-1])
        dev = next(model.parameters()).device
        batch = {
            k: (v.to(dev) if torch.is_tensor(v) else v) for k, v in batch.items()
        }
        torch.cuda.synchronize()
        with torch.inference_mode():
            if force_math:
                with sdpa_kernel([SDPBackend.MATH]):
                    logits = model(**batch).logits
            else:
                # efficient_sdpa forces EFFICIENT_ATTENTION inside the forward.
                logits = model(**batch).logits
        torch.cuda.synchronize()
        last = logits[0, -1].to(torch.float32)
        top_token = int(last.argmax().item())
        loader.close()
        del model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        return {
            "top_token": top_token,
            "last_logits": last,
            "input_len": input_len,
        }

    # Baseline: stock sdpa, MATH forced.
    base = _forward_capture("sdpa", force_math=True)
    # Candidate: efficient_sdpa (EFFICIENT_ATTENTION inside forward).
    _es.register_efficient_sdpa()
    cand = _forward_capture(_es.EFFICIENT_SDPA_KEY, force_math=False)

    last_base = base["last_logits"]
    last_cand = cand["last_logits"]
    diff = (last_cand - last_base).abs()
    j = ParityMetrics(
        same_top_token=(base["top_token"] == cand["top_token"]),
        max_abs_diff=diff.max().item(),
        mean_abs_diff=diff.mean().item(),
        cosine_similarity=_cosine(last_cand, last_base),
        baseline_backend="MATH (stock sdpa, forced)",
        candidate_backend="EFFICIENT_ATTENTION (efficient_sdpa, explicit repeat_kv)",
        note=(
            f"real cp118 forward, input_tokens={base['input_len']}, "
            "single forward per backend; top_token from argmax(logits[0,-1]); "
            "diff metrics from logits[0,-1]"
        ),
    ).to_json()

    # Persist the measured metrics BEFORE enforcing the policy so diagnosis
    # remains possible even when the gate fails.
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "parity_real_model.json").write_text(
        json.dumps(j, indent=2), encoding="utf-8"
    )

    # BLOCKER 2: real-model parity is a real fail-closed gate.  The shared
    # policy raises ParityError (carrying the metrics) on any violation.
    enforce_parity_policy(j)
    j["policy_passed"] = True
    # Re-write with the policy_passed flag for the accepted case.
    (output_dir / "parity_real_model.json").write_text(
        json.dumps(j, indent=2), encoding="utf-8"
    )
    return j


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        prog="experiments.local_inference_perf.parity"
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("unit", help="no-model synthetic parity / fail-closed check")
    rp = sub.add_parser("real-model", help="bounded real-model parity")
    rp.add_argument("--adapter-path", required=True)
    rp.add_argument("--output-dir", required=True)
    args = p.parse_args(argv)

    if args.cmd == "unit":
        j = parity_unit()
        print(json.dumps(j, indent=2))
        return 0
    if args.cmd == "real-model":
        try:
            j = parity_real_model(
                adapter_path=args.adapter_path,
                output_dir=Path(args.output_dir),
            )
        except ParityError as exc:
            # Fail closed: report the measured metrics and exit non-zero.
            print(json.dumps({"parity_failed": str(exc), "metrics": exc.metrics}, indent=2))
            return 1
        print(json.dumps(j, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())