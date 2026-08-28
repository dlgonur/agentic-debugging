# Agentic Debugger

Evidence-driven software repair for Python projects.

Agentic Debugger combines one controller, typed tools, bounded PDB sessions,
disposable workspaces, unified-diff patching, immutable event journals, and an
independent verifier. The terminal application exposes the same repair and
replay path used by the research harness.

## Quick start

Requires Python 3.11 or newer.

```powershell
python -m pip install -e ".[app,test]"
python -m agentic_debugger.ui --doctor
python -m agentic_debugger.ui
```

The first task is an offline, deterministic demo. It does not contact a model
provider. You can also run the scientific demo directly:

```powershell
python -m agentic_debugger.demo --output-dir demo-out --task-id curated-off-by-one-002
```

Configured-model and local-project sessions require a model profile supplied by
the operator. Live provider execution is always explicit.

## How it works

```text
task + policy
  -> single deterministic controller
  -> source, test, PDB, and patch tools
  -> disposable workspace
  -> independent verifier
  -> immutable events, replay, and cleanup proof
```

The verifier is the correctness authority. A model, controller, or operator
claim is never treated as proof of a repair.

## Current status

The accepted research cycle is complete. Release tag `v0.1.0` points to
`d01f7a5`. The current source also includes post-release application and public
repository cleanup.

Selected accepted evidence:

- one real Ollama Cloud product session reached `RESOLVED`, F2P 1/1 and P2P
  2/2; PDB was not exercised in that session;
- the exact-PDB 6/100, 12/100, and 18/100 ladder tasks were independently
  verifier-resolved;
- the repaired Level-32 treatment produced two authoritative resolutions in a
  frozen 15-model matrix;
- R5 resolved 5/5 curated holdout bugs with zero findings across 41 audited
  prompts;
- R6 resolved 8/8 task-disjoint QuixBugs validation tasks, without a
  matched-base causal fine-tuning claim;
- QuixBugs gold-patch 8/8 validates infrastructure only, not model ability.

See [`docs/results-index.md`](docs/results-index.md) for evidence paths and
mandatory qualifiers.

## Documentation

- [Architecture](docs/architecture/local-application-v1.md)
- [Result and evidence index](docs/results-index.md)
- [Technical synthesis](docs/final-report.md)
- [Project closeout](docs/project-closeout.md)
- [Closed roadmap](TODO.md)
- [Execution tracker](docs/project-tracker.md)
- [Experiment families](experiments/README.md)
- [Superseded material](outdated/docs-archive/)
- [Historical status log](outdated/docs-archive/status/README-historical-status-log-through-2026-08-07.md)

## Development

```powershell
python -m pytest --collect-only -q
python -m pytest tests/unit/test_public_documentation_navigation.py -q
python -m compileall agentic_debugger scripts
```

The repository includes frozen experiment evidence and operator tooling. Real
provider runs, WSL campaigns, and BugsInPy acquisition are not part of ordinary
test execution. BugsInPy remains license-gated.

## Repository map

| Path | Purpose |
|---|---|
| `agentic_debugger/agent/` | Controller, state, budgets, directives, and tools |
| `agentic_debugger/runtime/` | Workspaces, commands, patching, tests, and PDB |
| `agentic_debugger/evaluation/` | Task schema, verifier, and outcome taxonomy |
| `agentic_debugger/application/` | Sessions, worker boundary, journal, and replay |
| `agentic_debugger/ui/` | Textual terminal application |
| `experiments/` | Frozen experiment implementations and evidence |
| `analysis/` | Controlled comparisons and Level-32 analyses |
| `research/` | Literature notes and dataset provenance |
| `tests/` | Unit, integration, and golden trajectories |
| `outdated/` | Superseded material retained for provenance |

Generated trees such as `_ai-review/`, `runs/`, `operator/`, `outputs/`,
`artifacts/`, and model checkpoints are ignored and are not release evidence.

## Contributing and security

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for development rules and
[`SECURITY.md`](SECURITY.md) for private vulnerability reporting guidance.

## License

No open-source license is currently granted. The repository may be inspected,
but reuse and redistribution require permission from the copyright holder. Add
an explicit `LICENSE` before presenting the project as open source.
