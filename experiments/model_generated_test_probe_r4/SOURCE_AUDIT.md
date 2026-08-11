# R4 — Source Audit

## Minimal-delta audit

R4 is **additive and experiment-local** against baseline `f2291df`. It adds
one new directory `experiments/model_generated_test_probe_r4/` and new test
files. It modifies **no** production core, **no** R1–R3 experiment code, and
**no** canonical fixture.

## Files added

```
experiments/model_generated_test_probe_r4/
    __init__.py
    README.md
    SOURCE_AUDIT.md
    r4_contract.json
    test_generation.py
    generated_test_runner.py
    probe.py
tests/unit/test_r4_*.py
tests/integration/test_r4_pipeline.py
```

## Production / R3 imports (read-only reuse)

The probe imports from, but does not modify:

- `agentic_debugger.evaluation.runner.load_task`
- `agentic_debugger.evaluation.task_schema.DebugTask` (`agent_visible_mapping`
  for oracle stripping; the model-facing spec section renders ONLY the title +
  description fields)
- `agentic_debugger.evaluation.verifier.EvaluationVerifier` (independent
  correctness authority — same verifier R3's runner calls)
- `agentic_debugger.runtime.workspace.TaskWorkspace` (disposable copies)
- `agentic_debugger.runtime.patcher.PatchManager` (unified-diff apply with
  `allowed_paths=["recent_window.py"]`, `denied_paths=["tests","task.json"]` —
  same path/syntax/authorization gates as the verifier; no weakening)
- `agentic_debugger.runtime.test_runner.TestRunner`
- `experiments.debugger_interaction_v2_r3.transport` (`LocalRawQwenTransport`,
  `FakeTransport`, `FailingTransport`, `BASE_REPOSITORY`, `BASE_REVISION`,
  `GENERATION_CONFIG`) — tracked at f2291df
- `experiments.debugger_interaction_v2_r3.adapter` (`TransportResponse`,
  `TransportError`, `NOT_AVAILABLE`, `NOT_RECORDED`)
- `experiments.debugger_interaction_v2_r3.serialization`
  (`normalize_hunk_counts`) for the accepted R_fix_B -> R_fix_C normalization

## Historical S1-P provenance

The R4 modules are a **bounded reimplementation** of the historical S1-P probe
(`experiments/model_generated_test_probe/` on branch
`experiment/model-generated-test-probe`, commit
`c47be60e6919626b6f431cd337d1d847a97f0722`), which is **untracked** in `main`
and is **never imported at runtime**. The bounded deltas vs S1-P:

1. task: `curated-off-by-one-002` / `recent_window.py` (was
   `curated-none-handling-001` / `display_name.py`);
2. transport protocol re-pointed to the tracked R3 adapter/transport;
3. behavioral requirements rendered ONLY from `agent_visible_mapping()`
   title+description (S1-P supplied a harness-authored PUBLIC BEHAVIOR SPEC —
   R4 amendment 1 forbids this);
4. EXACTLY ONE generation call, no retries (S1-P allowed up to 3);
5. fixed condition is the accepted R3.2 repair R_fix_C, no model repair call
   (S1-P generated a one-shot model fix whose raw patch was rejected);
6. structured buggy-FAIL classifier (compile + collect + counts + markers +
   assertion attribution) beyond S1-P's counts-based gate;
7. T_raw / T_parsed / T_written identity record; R_fix_B / R_fix_C identities
   kept distinct from generated-test identities;
8. anti-leakage checked on the FINAL rendered live prompt (not helper inputs);
9. import-boundary enforcement proving no R4 import resolves through the
   untracked S1/S1-P directories.

## What is NOT changed

- `agentic_debugger/` (controller, state machine, tool registry, PDB, runtime,
  evaluation, events, demo) — production core is untouched.
- `experiments/debugger_interaction_v2_r1/`, `_r2/`, `_r3/` — untouched.
- `agentic_debugger/datasets/curated/curated-off-by-one-002/` — the canonical
  fixture is never mutated. All work happens in disposable `TaskWorkspace`
  copies; canonical immutability is asserted by the verifier and by the
  workspace-lifecycle unit tests.

## Anti-leakage verification

`probe._check_anti_leakage` checks the **final rendered system+user prompt**
for forbidden fragments (fixture test source/node names, oracle fields,
runtime hints, R3 repair identities, patch serialization). Unit tests also
assert the spec section equals exactly the rendered title+description.

## Import-boundary verification

`probe._check_import_boundaries` statically scans every R4 package source
file for imports of the untracked `experiments.debugger_interaction_v2.` /
`experiments.model_generated_test_probe.` prefixes and verifies at runtime
that the reused R3 modules resolve to the tracked
`experiments/debugger_interaction_v2_r3/` path.

## Source provenance

The runner records (mirroring R3/S1-P precedent):

- runtime `source_commit_sha` (`git rev-parse HEAD`, fail-closed in
  `--validate-only`);
- `experiment_contract_sha256`;
- `spec_section_sha256`;
- system/user prompt SHA-256;
- T_raw / T_parsed / T_written SHA-256 and the deterministic framing relation;
- R_fix_B / R_fix_C / fingerprint SHA-256 (fail-closed identity asserts);
- exact model revision and generation config;
- anti-leakage result on the final rendered prompt;
- candidate source manifest.

BUILD itself remains uncommitted; the owner source-freezes before/at live
probe evidence is finalized.
