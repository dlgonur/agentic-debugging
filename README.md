# Agentic Debugger

Evidence-driven software repair for Python projects.

![Agentic Debugger terminal welcome screen](docs/assets/agentic-debugger-welcome.png)

Agentic Debugger is a Python 3.11+ research prototype: a single deterministic controller drives typed tools, bounded PDB sessions, disposable workspaces, unified-diff patching, immutable event journals, and an independent verifier.

## Highlights

- Debug curated tasks or a local Git project without modifying the source tree (declare custom project variables in the `ProjEnv` row; undeclared variables are not inherited).
- Inspect bounded source, tests, stack frames, locals, and safe expressions.
- Apply model-authored patches through strict path and diff validation.
- Accept repairs only after independent fail-to-pass and pass-to-pass checks.

## Quick start

```powershell
.\scripts\install_windows_alias.ps1     # install app-owned launcher once (Windows)
agenticdebugger                         # launch from anywhere (PowerShell or CMD)
agenticdebugger --doctor                # reports readiness without contacting providers
```

Both names map to one entry point in an app-owned venv on User `PATH`. `pip install -e ".[app]"` also works; bare `agentic` is left unclaimed.

Run the scientific demo directly:

```powershell
python -m agentic_debugger.demo --output-dir demo-out --task-id curated-off-by-one-002
```

List or export session history without opening the UI:

```powershell
agenticdebugger --list-sessions
agenticdebugger --export-session SESSION_ID --output session-report.md
```

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

Sessions run as deterministic offline (no provider contacted), configured command-model, or Local Project Debug. Local Project repairs run in a disposable worktree; nothing reaches the source tree except through Apply To Project gates.

## Model providers

Live execution is explicit. Model access routes through `ModelGateway` and user-configured providers, including user-defined direct-API providers (press `m`; fresh installs configure none). Credentials stay in `CredentialVault` and the OS secure store — never in source, argv, or evidence ([architecture](docs/architecture/model-providers-v1.md)).
Request-size authority belongs to the provider: oversized requests fail truthfully as provider errors, never as silent truncation. Interactive/configured sessions have no total model-request, directive, or step ceiling; progress counters are telemetry only.

## Current status

The research cycle, Local Application V1, the V2 architecture campaign, and post-V2 Tasks 34 and 41-44 (token telemetry, CLI alias, universal execution eligibility, provider-owned request size, unbounded progress) are complete; see the [closeout](docs/project-closeout.md) (2026-09-10).
Release tag `v0.1.0` marks the research release checkpoint.

Accepted evidence includes a verifier-resolved real-provider product session, three verifier-resolved exact-PDB ladder tasks, two authoritative Level-32 resolutions in a frozen 15-model matrix, a leakage-clean base-14B 5/5, and a project-tuned 7B 8/8 on task-disjoint QuixBugs validation. Scopes differ; results are not interchangeable.
See the [results and evidence index](docs/results-index.md) for evidence paths
and mandatory qualifiers.

Verify the public evidence locally with:

```powershell
python scripts/verify_public_evidence.py --output public-evidence-attestation.json
```

The gate covers one representative offline repair (verification, replay, cleanup) plus the frozen R6 chain of custody, trace regeneration, and leakage audit. It does not rerun external campaigns.

## Limitations

- Configured command-model sessions launch a user-configured local command with that executable's host capabilities; V1 does not isolate child-process network access.
- BugsInPy external evaluation remains license-gated and unexecuted.

## Documentation

- [Application architecture](docs/architecture/local-application-v1.md)
- [V2 architecture plan](docs/architecture/agentic-debugger-v2-plan.md)
- [Results and evidence index](docs/results-index.md)
- [Historical technical report (R1–R6)](docs/final-report.md)
- [Project closeout](docs/project-closeout.md)
- [Experiment families](experiments/README.md)
- [Research index](research/README.md)
- [Closed roadmap](TODO.md)
- [Superseded material](outdated/docs-archive/)
- [Historical status log](outdated/docs-archive/status/README-historical-status-log-through-2026-08-07.md)

## Development

```powershell
python -m pytest <affected-test-path> -q
python -m compileall agentic_debugger scripts
```

Generated runs, model checkpoints, provider credentials, external datasets, and review packages must not be committed. Real-provider, WSL, and license-gated dataset campaigns are not ordinary regression tests.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution rules and
[SECURITY.md](SECURITY.md) for private vulnerability reporting.

## License

No open-source license is currently granted. The repository may be inspected,
but reuse and redistribution require permission from the copyright holder.
Add an explicit `LICENSE` before presenting the project as open source.
