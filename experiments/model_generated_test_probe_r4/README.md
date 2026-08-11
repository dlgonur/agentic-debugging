# R4 — Real-Model Generated Regression Test Probe

Professor-requested capability experiment against baseline `f2291df`.

## Question

Given the buggy function and its agent-visible behavioral task/specification,
can the real RAW model generate a regression test on its **first and only
attempt** such that:

- the exact model-generated test **FAILS** the original buggy implementation
  for the intended behavioral reason, and
- the **exact same test** **PASSES** the independently verified fixed
  implementation (accepted R3.2 repair `R_fix_C`), with the independent
  `EvaluationVerifier` still reporting `COMPLETED / RESOLVED / F2P 1/1 /
  P2P 2/2`.

## Model / task

- `Qwen/Qwen2.5-Coder-7B-Instruct` @ `c03e6d358207e414f1eca0bb1891e29f1db0e242`
- `do_sample=False`, `max_new_tokens=1024`, 4-bit NF4 local transport (tracked
  R3 transport)
- `curated-off-by-one-002` / `recent_window.py`

## Usage

```text
python experiments/model_generated_test_probe_r4/probe.py --validate-only
python experiments/model_generated_test_probe_r4/probe.py --run-offline --output-dir <dir>
python experiments/model_generated_test_probe_r4/probe.py --run --output-dir <dir>   # GPU, ONE model call
```

## Key rules (see r4_contract.json)

- EXACTLY ONE generation call; any failure leaves R4 OPEN with the first
  causal boundary recorded.
- Behavioral requirements come ONLY from `agent_visible_mapping()` title +
  description; no harness-authored behavioral spec.
- T_raw / T_parsed / T_written SHA-256 identities; R_fix_B / R_fix_C kept
  distinct.
- Strictly separate BUGGY and FIXED disposable workspaces; canonical fixture
  never mutated; cleanup required.
- The generated test is auxiliary evidence; the independent verifier over the
  frozen F2P/P2P contract remains the correctness authority.
