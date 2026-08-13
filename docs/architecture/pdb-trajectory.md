# Post-Mortem PDB Trajectory Integration v1

## Outcome

Bounded `PdbSession.run_post_mortem` evidence now travels through the accepted
single-controller path as the existing `get_failure_trace` action:

```text
baseline reproduction
  -> get_failure_trace (PDB observation budget)
  -> disposable post-mortem PDB session
  -> strict PdbResponse in ToolResult
  -> controller Observation
  -> RunEvent action/observation pair
  -> replay + semantic projection
  -> session/workspace cleanup
```

No new event schema, controller, debugger adapter, or trajectory format was
introduced.

## Contract

- `get_failure_trace` remains legal only in `Reproduce` and is now explicitly
  charged to `BudgetKind.PDB_OBSERVATIONS`.
- The demo/runtime handler rejects before starting PDB unless the baseline
  failure was reproduced, PDB policy is enabled, a prepared disposable probe
  exists, and no PDB session/workspace is active.
- The handler calls the existing strict `PdbSession.run_post_mortem` operation
  and retains the complete `PdbResponse.to_mapping()` under
  `pdb_response`, identified as `pdb-post-mortem-v1` evidence.
- A successful tool observation is emitted only after the worker session is
  stopped and its disposable workspace is removed. Cleanup failure becomes a
  tool error, never success.
- `status=post_mortem` retains exception, bounded traceback frames, and
  innermost-frame locals. `status=exited` honestly records that no traceback
  was captured. Protocol failure fails closed.
- Existing action/observation identity linkage, JSON bounds, event projection,
  replay validation, and semantic normalization apply unchanged.

## Scope boundary

This is an offline, deterministic infrastructure completion. The default Task
9 model does not request `get_failure_trace`, so accepted default trajectories
and provider request behavior remain unchanged. No live provider, QuixBugs
campaign, WSL benchmark, BugsInPy execution, or external QLoRA work occurred.
The integration proves evidence persistence and replay, not live-model PDB
effectiveness.

## Validation

- controller/policy plus real post-mortem integration: 162 passed;
- default demo tools/model/metrics/end-to-end surface: 112 passed;
- post-mortem PDB + event/replay/golden surface: 265 passed;
- the positive integration executes real baseline reproduction and a real PDB
  worker, verifies `AttributeError` evidence for the curated None-handling
  fixture, replays the trajectory, semantically projects it, and proves cleanup;
- the negative integration proves pre-baseline rejection without starting PDB.
