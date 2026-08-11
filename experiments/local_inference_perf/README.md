# cp118 Efficient-SDPA Local Inference Optimization v1

A small, reusable, **fail-closed** package that turns the already-proven
Windows + Qwen2.5-Coder-7B efficient-attention workaround into a benchmark /
parity harness. It does **not** redesign the workaround; it packages it.

## What this is

On this Windows torch 2.10 dev + cu128 build, `torch.backends.cuda.
is_flash_attention_available()` is `False`, so the stock Transformers `sdpa`
path for Qwen2.5-Coder-7B (28 Q heads / 4 KV heads, `enable_gqa=True`) falls to
the slow **MATH** SDPA backend. The proven workaround explicitly expands KV
with `repeat_kv` (4 → 28 heads) and forces `SDPBackend.EFFICIENT_ATTENTION`,
wired in through the Transformers functional attention interface.

Measured (already demonstrated, not regenerated here):

| case          | stock (MATH)   | efficient (this pkg) |
|---------------|----------------|----------------------|
| 6079 + 1      | 301.4 s / 15.3 GiB | 3.56 s / 7.2 GiB |
| 6079 + 256    | 375–539 s      | 59.9 s / 7.2 GiB     |
| 6079 + 1024   | —              | 191.4 s / 7.2 GiB    |

Numerical parity on a 50-token real cp118 forward: `same_top_token=True`,
`max_abs_diff=0.125`, `mean_abs_diff=0.013`, `cosine=0.9999645`.

## Scope / what it does NOT touch

- Frozen S4 scientific run (`experiments/cp118_rag_definitive/**`) — **never
  imported or modified**.
- `agentic_debugger/**`, `pyproject.toml`, `AGENTS.md`, historical evidence —
  untouched.
- This package duplicates the pinned base repo/revision constants rather than
  importing them from S4, so the frozen run stays isolated.

## Files

| file | purpose |
|------|---------|
| `efficient_sdpa.py` | reusable attention helper + registration (fail-closed) |
| `env_report.py` | environment/telemetry capture (dual flash flags) |
| `model_loader.py` | lazy, fail-closed load; `device_map="auto"` + placement guard |
| `benchmark.py` | deterministic benchmark harness (stock + efficient backends) |
| `parity.py` | stock MATH-SDPA reference vs efficient-repeat candidate |
| `tests/` | no-model compatibility tests + 3.10 CUDA unit tests |

## Fail-closed contract

- Registration requires CUDA available, `SDPBackend.EFFICIENT_ATTENTION`
  present, `sdpa_kernel` importable, and a one-shot backend probe that
  actually runs EFFICIENT_ATTENTION.
- The forward wraps SDPA in `with sdpa_kernel([EFFICIENT_ATTENTION]):` with
  **no fallback list** — if the efficient backend cannot run, it raises; it
  never silently falls back to MATH when the optimized mode was requested.
- Registry conflict guard: absent key → register; same key + same impl →
  no-op; same key + different impl → `EfficientSdpaError`. Applied to both the
  attention and mask registries.
- Optimized benchmark mode fails closed if `hf_device_map` contains CPU or
  disk/offload; it never silently switches to `device_map={"":0}`.
- Transformers 4.57.3 is the **tested/supported** version. We do not claim
  generic `>=4.57` compatibility (internal integration symbols may drift).
  Torch is capability-gated, not version-pinned.

## Import discipline

The package is import-tolerant in a torch-less Python: no torch/transformers/
peft/bitsandbytes imports at module import time. All heavy imports are lazy
(inside `register_efficient_sdpa()`, `load()`, forward call sites). The no-model
tests run under any Python.

## Run

```bash
# no-model compatibility tests (any Python with pytest)
python -m pytest experiments/local_inference_perf/tests/test_compatibility_no_model.py -q

# 3.10 CUDA unit tests (requires torch + transformers + CUDA)
python -m pytest experiments/local_inference_perf/tests/test_efficient_sdpa_unit.py -q

# compile
python -m compileall experiments/local_inference_perf

# no-model synthetic parity / fail-closed check
python -m experiments.local_inference_perf.parity unit

# bounded real-model parity (after review; 3.10 env)
python -m experiments.local_inference_perf.parity real-model \
    --adapter-path <cp118 adapter dir> --output-dir parity-out

# benchmark (default = efficient backend)
python -m experiments.local_inference_perf.benchmark \
    --adapter-path <cp118 adapter dir> --output-dir bench-out --backend efficient

# stock reference backend (explicit only; do not rerun expensive stock long-gen)
python -m experiments.local_inference_perf.benchmark \
    --adapter-path <cp118 adapter dir> --output-dir bench-out --backend stock
```

## Adapter / base

- Base: `Qwen/Qwen2.5-Coder-7B-Instruct` @
  `c03e6d358207e414f1eca0bb1891e29f1db0e242`.
- Adapter: the verified cp118 PEFT/QLoRA adapter (NF4, double-quant, BF16
  compute), loaded with `PeftModel.from_pretrained(base, adapter, is_trainable=False)`.
- Generation: greedy (`do_sample=False, num_beams=1`), `use_cache=True`.

## Metric naming

The per-cell result stores `end_to_end_output_tokens_per_second`
(`output_tokens / total_elapsed_s`), explicitly end-to-end (prefill + decode),
**not** "decode throughput". An optional `approx_decode_output_tokens_per_second`
is derived from paired 1-token / N-token cells and labelled approximate with its
formula documented in `benchmark.py`.