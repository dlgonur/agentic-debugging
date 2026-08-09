# Tuned-model interactive debugger pilot v1

This directory freezes and runs the smallest real tuned-model experiment for
professor TODO #23-25. It reuses the existing deterministic controller, PDB
worker/session, curated tasks, patch lifecycle, event projection and independent
verifier.

The real model is injected only through the existing `ModelTransport` boundary.
No tuned checkpoint is bundled here. The real command fails closed unless a
PEFT `adapter-final/` directory is supplied (`adapter_config.json` plus
`adapter_model.safetensors`).

## Preflight without a tuned adapter

```powershell
python experiments/tuned_debugger_pilot_v1/run_pilot.py --validate-only
```

This validates the frozen five-task identity, task budgets/timeouts, A/B input
identity, registry exposure and experiment contract. It performs no model load
and no benchmark run.

## Real 10-case pilot

```powershell
python experiments/tuned_debugger_pilot_v1/run_pilot.py `
  --adapter-path C:\path\to\frozen-run\adapter-final `
  --output-dir artifacts\tuned_debugger_pilot_v1\run-001
```

The runner loads the pinned Qwen2.5-Coder-7B base plus the supplied PEFT
adapter, then executes the five fixed tasks under `static-baseline` and
`pdb-on-uncertainty`. The debugger-assisted condition enables the opt-in typed
breakpoint / continue / step / next surface; the static condition receives no
PDB actions from the live contract. The debugger-assisted condition permits one
session start, at most eight accepted observation/control actions, and one stop
(ten accepted debugger actions maximum).

`pilot_report.json` is the untouched live-evaluation report.
`pilot_evidence.json` is a derived, review-oriented projection retaining the
required public evidence fields. Observable model directives are typed JSON
only; no private chain-of-thought is requested or recorded.
