# S1-P — Professor-Requested Model-Generated Regression Test Probe

## Status

**BUILD (offline validation only) — not yet authorized for live model execution.**

## Scientific Question

> Given an explicit expected behavior specification and buggy source, can the
> frozen RAW Qwen2.5-Coder-7B model generate an executable regression test that
> exposes the bug, and can a separately model-produced repair satisfy that same
> frozen test?

This is a professor-requested auxiliary demonstration. It is **not** a
benchmark of general test-generation performance and **not** a claim that the
model discovered the specification.

## Pipeline

```
PUBLIC BEHAVIOR SPEC + buggy source
  → frozen RAW Qwen2.5-Coder-7B (one request)
  → ONE executable pytest regression test
  → FREEZE exact source + SHA-256 + raw-response provenance
  → buggy code FAILS the frozen test            (recorded)
  → one-shot model-produced fix                 (same frozen model; gold hidden)
  → fixed code evaluated against the SAME frozen test   (PASS/FAIL recorded)
  → independent EvaluationVerifier executed separately  (correctness authority)
```

The generated test is **auxiliary evidence, never the correctness authority**.
The independent `EvaluationVerifier` remains the final correctness authority.

## Claims Boundary

This probe measures:

> Given an explicit expected behavior specification and buggy source, can the
> frozen RAW model generate an executable regression test that exposes the bug,
> and can a separately model-produced repair satisfy that same frozen test?

It does **NOT** measure:

- autonomous discovery of the specification;
- correctness based solely on the generated test;
- general unit-test-generation performance.

A result where:

```
generated test: buggy FAIL
generated test: model-fixed PASS
verifier: NOT RESOLVED
```

is scientifically valid and is reported honestly.

## Frozen Variables

- Model: `Qwen/Qwen2.5-Coder-7B-Instruct @ c03e6d358207e414f1eca0bb1891e29f1db0e242`
- Condition: RAW base only (no PEFT adapter)
- RAG: OFF
- Task: `curated-none-handling-001` (`format_display_name`)
- Generation: `do_sample=False`, `max_new_tokens=1024`, `max_input_tokens=32768`
- Quantization: 4-bit NF4 with double quantization (same as S1)
- Public behavior spec: frozen (SHA-256 in contract)
- System prompts: frozen (SHA-256 in contract)
- Independent verifier: `EvaluationVerifier` (unchanged)

## Anti-Leakage Boundary

The model-facing test-generation input contains:

- the **PUBLIC BEHAVIOR SPEC** (intentionally supplied — the treatment);
- the **buggy source** of `display_name.py` (no fixed/gold code).

Strictly excluded from the model-facing input:

- the oracle (`root_cause_summary`, `runtime_evidence_hint`, `bug_category`);
- the existing fixture test source (`tests/test_display_name.py`);
- the existing failing/passing test **node names**;
- fixed/gold source and the gold patch.

The behavior spec is **not** leakage for this auxiliary probe: generating a
test from a specified expected behavior is the treatment being measured. The
model is **not** claimed to have discovered the requirement.

The fix-generation input contains the behavior spec + buggy source + the exact
frozen generated test. Gold/fixed source remains hidden.

## Retry / STOP

- **Retry budget**: initial generation + at most 2 retries = 3 attempts.
- Retries are triggered **only** by a non-extractable response or a
  non-executable generated test (collection/syntax/import error, zero or >1
  collected nodes, skip/error rather than PASS-or-FAIL).
- The system+user prompts are **frozen** (hashed). Only a deterministic
  `feedback` line changes between attempts. No prompt-tuning until pass.
- **Freeze is final at the first executable test**: its source + SHA-256 are
  frozen and used for both the buggy run and the fixed-code run. Regeneration
  based on whether buggy/fixed code passes is **not** allowed.
- If an executable generated test PASSES on buggy code: **STOP** for this probe
  and record `generated_test_did_not_encode_defect = true`. Do not regenerate.
- Fix generation is **one-shot**: no retry for a malformed diff, a failing
  generated test after patch, or a verifier failure. That outcome is valid
  evidence.

## Reuse (no new framework)

| Component | Reused from |
|---|---|
| Model transport protocol | `experiments.debugger_interaction_v2.adapter` (`ModelTransport`, `TransportResponse`, `TransportError`) |
| RAW Qwen2.5 transport | `experiments.debugger_interaction_v2.transport.LocalRawQwenTransport` |
| Offline transport doubles | `experiments.debugger_interaction_v2.transport.FakeTransport` / `FailingTransport` |
| Task loader | `agentic_debugger.evaluation.runner.load_task` |
| Oracle stripping | `agentic_debugger.evaluation.task_schema.DebugTask.agent_visible_mapping` |
| Disposable workspace | `agentic_debugger.runtime.workspace.TaskWorkspace` |
| Patch apply + path/syntax gates | `agentic_debugger.runtime.patcher.PatchManager` |
| Test execution | `agentic_debugger.runtime.test_runner.TestRunner` |
| Independent verifier (authority) | `agentic_debugger.evaluation.verifier.EvaluationVerifier` |

No production-core changes. No S1 debugger/bridge/controller/grammar changes.

## Files

| File | Purpose |
|---|---|
| `experiment_contract.json` | Frozen contract (model, task, hashes, budgets, anti-leakage, claims) |
| `test_generation.py` | Prompt assembly, extraction, freeze, executability gate, retry loop |
| `generated_test_runner.py` | Disposable-workspace runs of the frozen test (buggy + fixed) |
| `probe.py` | Orchestrator (`--validate-only` / `--run-offline` / `--run`) |
| `README.md` | This file |
| `SOURCE_AUDIT.md` | Minimal-delta audit |
| `tests/unit/test_model_generated_test_probe.py` | Offline validation tests |

## Invocation

```bash
# Validate contract/identity (no model load)
python experiments/model_generated_test_probe/probe.py --validate-only

# Offline full pipeline (deterministic FakeTransport; no model, no GPU)
python experiments/model_generated_test_probe/probe.py --run-offline --output-dir <dir>

# Live run (requires GPU + authorization; NOT run in BUILD)
python experiments/model_generated_test_probe/probe.py --run --output-dir <dir>
```

## Evidence Schema

One `evidence.json` per run containing: `run_identity` (source commit,
contract SHA-256, model identity, behavior-spec/prompt hashes),
`test_generation` (attempts, frozen test source + SHA-256 + raw-response
provenance + executability + anti-leakage flags), `buggy_run`,
`model_fixed_code` (candidate patch + SHA-256 + provenance + anti-leakage),
`generated_test_eval` (fixed-code run against the same frozen test),
`verifier` (independent `EvaluationVerifier` result — the authority), and
`summary` (stop reason + boolean flags + authority statement).