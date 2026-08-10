# S1-P — Source Audit

## Minimal-delta audit

This experiment is **additive and experiment-local**. It adds a new directory
`experiments/model_generated_test_probe/` and one new test file. It modifies
**no** production core and **no** S1 debugger code.

## Files added

```
experiments/model_generated_test_probe/
    README.md
    SOURCE_AUDIT.md
    experiment_contract.json
    test_generation.py
    generated_test_runner.py
    probe.py
tests/unit/test_model_generated_test_probe.py
```

## Production / S1 imports (read-only reuse)

The probe imports from, but does not modify:

- `agentic_debugger.evaluation.runner.load_task`
- `agentic_debugger.evaluation.task_schema.DebugTask` (`agent_visible_mapping`
  for oracle stripping — same helper S1's `_build_task_description` uses)
- `agentic_debugger.evaluation.verifier.EvaluationVerifier` (independent
  correctness authority — same verifier S1's runner calls)
- `agentic_debugger.runtime.workspace.TaskWorkspace` (disposable copies)
- `agentic_debugger.runtime.patcher.PatchManager` (unified-diff apply with
  `allowed_paths=["display_name.py"]`, `denied_paths=["tests","task.json"]` —
  same path/syntax/authorization gates as the verifier; no weakening)
- `agentic_debugger.runtime.test_runner.TestRunner`
- `experiments.debugger_interaction_v2.adapter` (`ModelTransport`,
  `TransportResponse`, `TransportError`, `NOT_AVAILABLE`, `NOT_RECORDED`)
- `experiments.debugger_interaction_v2.transport` (`LocalRawQwenTransport`,
  `FakeTransport`, `FailingTransport`, `BASE_REPOSITORY`, `BASE_REVISION`,
  `GENERATION_CONFIG`)

No `transport_shim.py` is created (per amendment 4): S1 transport types are
imported directly.

## What is NOT changed

- `experiments/debugger_interaction_v2/bridge.py` — S1's 17-command grammar is
  untouched. No test-generation command is added to S1's grammar.
- `experiments/debugger_interaction_v2/adapter.py`, `transport.py`,
  `runner.py`, `experiment_contract.json` — S1 is untouched.
- `agentic_debugger/` (controller, state machine, tool registry, PDB, runtime,
  evaluation, events, demo) — production core is untouched.
- `agentic_debugger/datasets/curated/curated-none-handling-001/` — the
  canonical fixture is never mutated. All work happens in disposable
  `TaskWorkspace` copies; canonical immutability is asserted by the verifier
  and by the unit test `TestCanonicalImmutability`.

## Anti-leakage verification

The test-generation prompt is assembled by `build_generation_user_prompt`,
which uses `DebugTask.agent_visible_mapping()` (deletes the oracle and
`fixed_revision`) and includes only the public behavior spec + buggy source.
Unit tests (`TestPromptAntiLeakage`) assert the oracle, fixture test source,
and test node names are absent.

## No new framework

The probe introduces no new controller, verifier, patcher, workspace system,
transport, or model-loading logic. It reuses the accepted S1 transport
protocol and doubles, the production workspace/patcher/test-runner, and the
production independent verifier.

## Source provenance

The runner records (mirroring S1 precedent):

- runtime `source_commit_sha` (`git rev-parse HEAD`, fail-closed in
  `--validate-only`);
- `experiment_contract_sha256`;
- `behavior_spec_sha256`;
- system/user prompt SHA-256;
- generated-test SHA-256;
- raw-response SHA-256;
- candidate-patch SHA-256;
- exact model revision and generation config;
- anti-leakage flags.

BUILD itself remains uncommitted. The owner source-freeze commits before any
live probe.