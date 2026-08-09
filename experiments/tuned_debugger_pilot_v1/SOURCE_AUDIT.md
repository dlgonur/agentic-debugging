# Tuned Debugger Pilot v1 — Source Audit

Audited source baseline: `da4df9435228a0b3bfdf504baf9e71ae2fa95226`, from the independently reviewed main-repository archive with SHA-256 `5ce752757cb51eb47c260f2680ce4d0ec59d4560f3a841be80fcfc4acfa37aaa`.

## Live-model debugger surface before this pilot

The live model contract is registry-derived. `LiveModelAdapter` receives an exact `ToolRegistry`, intersects that registry with controller state, policy, PDB lifecycle and remaining PDB-observation budget, and advertises only the resulting action contracts.

Before this pilot, the actual demo/live registry exposed `start_pdb_session`, `get_stack_summary`, `get_frame_locals`, `safe_eval_expression`, and `stop_pdb_session`. `start_pdb_session` accepted no arguments and used `PdbProbe.breakpoint_line`, which `prepare_pdb_probe()` resolved from the curated scenario/fixture AST. Therefore breakpoint placement was orchestration-selected, not model-selected.

`PdbSession.continue_paused_target()` existed at the runtime layer but was not registered as a live-model action. `step` and `next` did not exist as runtime protocol/session operations. Stack, locals and safe expression evaluation were already directly model-selectable after a PDB session became active.

`ActionName.GET_FRAME` and `ActionName.INSPECT_CALLER_FRAME` existed in the controller policy, but the actual live registry did not register handlers for them, so the authoritative live contract did not advertise them.

## Minimal pilot delta

The pilot keeps the existing controller, PDB worker/session, task workspaces, patcher, events and verifier. It adds only the missing typed execution-control surface:

- `start_pdb_session {"breakpoint_line": <positive int>}` in opt-in interactive mode;
- `continue_pdb_session`;
- `step_pdb_session`;
- `next_pdb_session`.

Interactive probe preparation does not resolve a hidden AST breakpoint; it stores breakpoint sentinel `0`, and execution starts only from the model-supplied line. The default deterministic demo/live registry remains unchanged unless `interactive_debugger_controls=True` is explicitly selected.

`step` advances to the next traced line in the target script. `next` advances to the next traced line in the currently paused frame. No raw PDB terminal or arbitrary command channel is exposed.

## Local tuned-model injection

No model-specific controller change is required. `LiveModelAdapter` already depends on the `ModelTransport` interface. The pilot runner supplies a local transport that loads:

`Qwen/Qwen2.5-Coder-7B-Instruct@c03e6d358207e414f1eca0bb1891e29f1db0e242`

plus a caller-supplied frozen PEFT `adapter-final/` directory. The transport returns the same `{directive, usage}` envelope consumed by the existing live adapter. `--validate-only` never imports or loads the model stack.
