"""Deterministic local inference benchmark for the cp118 efficient-SDPA
optimization.

Runs a fixed prompt-class x generation matrix and records a JSON result per
cell plus an index.  Supports both backends (amendment 2):

* ``--backend efficient`` (default, the optimized mode): explicit
  ``repeat_kv`` + ``SDPBackend.EFFICIENT_ATTENTION`` via the registered
  ``"efficient_sdpa"`` attention key.
* ``--backend stock``: stock Transformers ``"sdpa"`` (native GQA ->
  ``enable_gqa=True`` -> fused backend unavailable -> MATH SDPA).  Used only
  when explicitly requested; we do not rerun expensive stock long-context
  generations during BUILD.

Metric naming (amendment 3): the per-cell record stores
``end_to_end_output_tokens_per_second`` (``output_tokens / total_elapsed_s``),
explicitly labelled as end-to-end (prefill + decode), **not** "decode
throughput".  An optional approximate decode throughput, when a paired 1-token
cell is available, is recorded as ``approx_decode_output_tokens_per_second``
with its formula documented in the result.

Run as a module from the repo root:

    python -m experiments.local_inference_perf.benchmark \
        --adapter-path <path> --output-dir bench-out --backend efficient

Importing this module never imports torch/transformers.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import efficient_sdpa as _es
from .env_report import capture_environment
from .model_loader import LocalQwenAdapterLoader

# ---------------------------------------------------------------------------
# Deterministic prompt fixtures.
# ---------------------------------------------------------------------------

#: Fixed seed text for prompt construction (deterministic, no RNG).
_SEED = (
    "def reconstruct_bug(failing_test, repository_tree, source_files):\n"
    "    '''Analyze the failing test output, locate the fault, and emit a "
    "unified diff that repairs it while preserving pass-to-pass tests.'''\n"
    "    # Step 1: parse the failing test output for the assertion and traceback.\n"
    "    # Step 2: map the traceback frame to a repository-relative source path.\n"
    "    # Step 3: read the suspect symbol and form a causal hypothesis.\n"
    "    # Step 4: propose a minimal unified diff; verify it does not break "
    "pass-to-pass checks.\n"
    "    return patch\n"
)

#: Matrix prompt classes: (name, target_token_count).  The prompt is built to
#: reach approximately this many *tokens* (measured by the tokenizer), not
#: chars — Qwen BPE compresses repetitive text heavily, so a char target of
#: 6000 produced only ~1343 tokens.  The actual token count is still measured
#: and recorded from the tokenizer after assembly.
PROMPT_CLASSES = (
    ("short", 50),
    ("medium", 1000),
    ("long", 6000),
)

#: Default generation matrix: (prompt_class, max_new_tokens).
DEFAULT_MATRIX = (
    ("short", 1),
    ("short", 256),
    ("medium", 1),
    ("medium", 256),
    ("long", 1),
    ("long", 256),
)

LONG_GEN_CELL = ("long", 1024)  # only with --include-long-gen


def build_prompt(target_chars: int) -> str:
    """Build a deterministic prompt of approximately ``target_chars`` chars by
    repeating the seed and truncating.

    Kept for backwards compatibility / unit tests, but the benchmark uses
    :func:`build_prompt_tokens` (token-targeted) so the prompt classes hit
    their intended token counts.
    """

    if target_chars <= len(_SEED):
        return _SEED[:target_chars]
    reps = (target_chars + len(_SEED) - 1) // len(_SEED)
    text = (_SEED * reps)[:target_chars]
    return text


def build_prompt_tokens(target_tokens: int, tokenizer: Any) -> str:
    """Build a deterministic prompt of approximately ``target_tokens`` tokens.

    Repeats the seed and measures the actual token count with the tokenizer,
    growing the text until the token count is >= target (then trims one seed
    block at a time to avoid overshooting too far).  Bounded above by the
    frozen ``MAX_PROMPT_TOKENS`` budget (24 576).
    """

    # Start with enough seed repetitions to likely exceed the target, then
    # binary-search-trim.  Empirically ~1.5 chars/token for this seed, so size
    # the initial guess generously.
    max_prompt_tokens = 24_576
    cap = min(max_prompt_tokens, target_tokens * 3)  # safety upper bound
    text = ""
    reps = 0
    # Grow until we reach the target token count.
    while True:
        reps += 1
        text = (_SEED * reps)
        n = len(tokenizer.encode(text, add_special_tokens=False))
        if n >= target_tokens or n >= cap or reps > 10_000:
            break
    # Trim down by removing seed blocks until removing one more would drop
    # below the target; this keeps us just above target_tokens.
    while reps > 1:
        candidate = _SEED * (reps - 1)
        if len(tokenizer.encode(candidate, add_special_tokens=False)) >= target_tokens:
            text = candidate
            reps -= 1
        else:
            break
    return text


# ---------------------------------------------------------------------------
# Backend selection.
# ---------------------------------------------------------------------------


@dataclass
class BackendSpec:
    name: str
    attn_implementation: str
    register_first: bool
    require_gpu_placement: bool
    label: str


def resolve_backend(name: str) -> BackendSpec:
    name = name.lower()
    if name == "efficient":
        return BackendSpec(
            name="efficient",
            attn_implementation=_es.EFFICIENT_SDPA_KEY,
            register_first=True,
            require_gpu_placement=True,
            label="EFFICIENT_ATTENTION via efficient_sdpa (explicit repeat_kv)",
        )
    if name == "stock":
        return BackendSpec(
            name="stock",
            attn_implementation="sdpa",
            register_first=False,
            require_gpu_placement=False,  # stock is the reference; allow it
            label="stock sdpa (native GQA -> MATH SDPA in this env)",
        )
    raise ValueError(f"unknown backend {name!r}; expected 'efficient' or 'stock'")


# ---------------------------------------------------------------------------
# Benchmark runner.
# ---------------------------------------------------------------------------


@dataclass
class CellResult:
    prompt_class: str
    max_new_tokens: int
    actual_input_tokens: int
    output_tokens: int
    total_elapsed_s: float
    peak_allocated_mib: float
    reserved_mib: float
    free_cuda_mib_before: float
    free_cuda_mib_after: float
    model_load_time_s: float
    backend: str
    attn_implementation: str
    use_cache: bool
    device_map: Dict[str, str]
    end_to_end_output_tokens_per_second: float
    approx_decode_output_tokens_per_second: Optional[float] = field(default=None)
    text_preview: str = field(default="")

    def to_json(self) -> Dict[str, Any]:
        d = {
            "prompt_class": self.prompt_class,
            "max_new_tokens": self.max_new_tokens,
            "actual_input_tokens": self.actual_input_tokens,
            "output_tokens": self.output_tokens,
            "total_elapsed_s": self.total_elapsed_s,
            "end_to_end_output_tokens_per_second": self.end_to_end_output_tokens_per_second,
            "approx_decode_output_tokens_per_second": self.approx_decode_output_tokens_per_second,
            "peak_allocated_mib": self.peak_allocated_mib,
            "reserved_mib": self.reserved_mib,
            "free_cuda_mib_before": self.free_cuda_mib_before,
            "free_cuda_mib_after": self.free_cuda_mib_after,
            "model_load_time_s": self.model_load_time_s,
            "backend": self.backend,
            "attn_implementation": self.attn_implementation,
            "use_cache": self.use_cache,
            "device_map": self.device_map,
            "text_preview": self.text_preview[:200],
        }
        return d


def _free_mib(torch: Any) -> float:
    try:
        if not torch.cuda.is_available():
            return float("nan")
        free, _total = torch.cuda.mem_get_info()
        return free / (1024 ** 2)
    except Exception:  # noqa: BLE001
        return float("nan")


def run_matrix(
    *,
    adapter_path: str,
    backend: BackendSpec,
    matrix: List[tuple],
    output_dir: Path,
) -> Dict[str, Any]:
    """Load the model once, run the matrix, write per-cell JSON + an index."""

    import torch  # lazy

    if backend.register_first:
        _es.register_efficient_sdpa()

    loader = LocalQwenAdapterLoader(
        adapter_path=adapter_path,
        attn_implementation=backend.attn_implementation,
        require_gpu_placement=backend.require_gpu_placement,
    )
    load_meta = loader.load()
    model, tokenizer = loader.model, loader.tokenizer

    env = capture_environment(model=model)
    env["backend_label"] = backend.label
    env["loader_meta"] = load_meta

    output_dir.mkdir(parents=True, exist_ok=True)
    results: List[CellResult] = []

    for prompt_class, max_new in matrix:
        target_tokens = dict(PROMPT_CLASSES)[prompt_class]
        prompt_text = build_prompt_tokens(target_tokens, tokenizer)
        cell = _run_cell(
            model=model,
            tokenizer=tokenizer,
            torch=torch,
            prompt_text=prompt_text,
            prompt_class=prompt_class,
            max_new_tokens=max_new,
            backend=backend,
            model_load_time_s=loader.load_seconds or 0.0,
            device_map=loader.resolved_device_map or {},
        )
        results.append(cell)
        # Write per-cell JSON immediately (incremental evidence).
        cell_path = output_dir / f"cell_{backend.name}_{prompt_class}_{max_new}.json"
        cell_path.write_text(
            json.dumps(cell.to_json(), indent=2), encoding="utf-8"
        )

    # Approximate decode throughput from paired 1/N cells (amendment 3).
    _fill_approx_decode(results)

    index = {
        "backend": backend.name,
        "backend_label": backend.label,
        "matrix": [list(c) for c in matrix],
        "environment": env,
        "results": [r.to_json() for r in results],
    }
    (output_dir / f"index_{backend.name}.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )

    loader.close()
    return index


def _run_cell(
    *,
    model: Any,
    tokenizer: Any,
    torch: Any,
    prompt_text: str,
    prompt_class: str,
    max_new_tokens: int,
    backend: BackendSpec,
    model_load_time_s: float,
    device_map: Dict[str, str],
) -> CellResult:
    messages = [{"role": "user", "content": prompt_text}]
    batch = tokenizer.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    )
    input_len = int(batch["input_ids"].shape[-1])
    device = next(model.parameters()).device
    batch = {
        k: (v.to(device) if torch.is_tensor(v) else v) for k, v in batch.items()
    }

    torch.cuda.reset_peak_memory_stats()
    free_before = _free_mib(torch)
    t0 = time.perf_counter()
    with torch.inference_mode():
        out = model.generate(
            **batch,
            do_sample=False,
            num_beams=1,
            max_new_tokens=max_new_tokens,
        )
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - t0

    new_ids = out[0][input_len:]
    text = tokenizer.decode(new_ids, skip_special_tokens=True)

    peak_alloc = torch.cuda.max_memory_allocated() / (1024 ** 2)
    reserved = torch.cuda.max_memory_reserved() / (1024 ** 2)
    free_after = _free_mib(torch)
    out_tokens = int(new_ids.shape[-1])

    e2e = (out_tokens / elapsed) if elapsed > 0 else float("nan")

    use_cache = bool(getattr(model.config, "use_cache", False))

    return CellResult(
        prompt_class=prompt_class,
        max_new_tokens=max_new_tokens,
        actual_input_tokens=input_len,
        output_tokens=out_tokens,
        total_elapsed_s=elapsed,
        peak_allocated_mib=peak_alloc,
        reserved_mib=reserved,
        free_cuda_mib_before=free_before,
        free_cuda_mib_after=free_after,
        model_load_time_s=model_load_time_s,
        backend=backend.label,
        attn_implementation=backend.attn_implementation,
        use_cache=use_cache,
        device_map=device_map,
        end_to_end_output_tokens_per_second=e2e,
        text_preview=text,
    )


def _fill_approx_decode(results: List[CellResult]) -> None:
    """When a prompt_class has both a 1-token and an N-token cell, derive an
    APPROXIMATE decode throughput and label it as approximate with its
    formula.

    Formula: approx_decode_tps = (N - 1) / (t_N - t_1)
    Rationale: the 1-token cell is (prefill + 1 decode step); subtracting it
    from the N-token cell removes the prefill, leaving (N-1) decode steps.
    This is approximate because prefill cost and per-step cost are not perfectly
    separable, and because CUDA timing includes kernel launch overhead.
    """

    by_class: Dict[str, Dict[int, CellResult]] = {}
    for r in results:
        by_class.setdefault(r.prompt_class, {})[r.max_new_tokens] = r
    for cls, cells in by_class.items():
        one = cells.get(1)
        if not one:
            continue
        for n, cell in cells.items():
            if n == 1:
                continue
            t1 = one.total_elapsed_s
            tn = cell.total_elapsed_s
            if tn > t1 and cell.output_tokens > 1:
                approx = (cell.output_tokens - 1) / (tn - t1)
                cell.approx_decode_output_tokens_per_second = approx


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _build_matrix(args: argparse.Namespace) -> List[tuple]:
    matrix = list(DEFAULT_MATRIX)
    if args.include_long_gen:
        matrix.append(LONG_GEN_CELL)
    if args.only_prompt:
        matrix = [c for c in matrix if c[0] in args.only_prompt]
    if args.only_gen:
        matrix = [c for c in matrix if c[1] in args.only_gen]
    # dedupe preserving order
    seen = set()
    out = []
    for c in matrix:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="experiments.local_inference_perf.benchmark",
        description="cp118 efficient-SDPA local inference benchmark",
    )
    p.add_argument("--adapter-path", required=True, help="path to cp118 adapter dir")
    p.add_argument("--output-dir", required=True, help="output directory for JSON")
    p.add_argument(
        "--backend",
        choices=("efficient", "stock"),
        default="efficient",
        help="efficient=optimized EFFICIENT_ATTENTION+repeat_kv (default); "
        "stock=reference sdpa path",
    )
    p.add_argument(
        "--include-long-gen",
        action="store_true",
        help="add long+1024 cell (off by default; already manually demonstrated)",
    )
    p.add_argument(
        "--only-prompt",
        nargs="*",
        choices=("short", "medium", "long"),
        help="restrict to these prompt classes",
    )
    p.add_argument(
        "--only-gen",
        type=int,
        nargs="*",
        help="restrict to these max_new_tokens values",
    )
    args = p.parse_args(argv)

    backend = resolve_backend(args.backend)
    matrix = _build_matrix(args)
    if not matrix:
        print("no benchmark cells selected", file=os.sys.stderr)
        return 2

    index = run_matrix(
        adapter_path=args.adapter_path,
        backend=backend,
        matrix=matrix,
        output_dir=Path(args.output_dir),
    )
    print(
        f"wrote {args.output_dir}/index_{backend.name}.json with "
        f"{len(index['results'])} cells"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())