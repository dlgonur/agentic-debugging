# Agentic Debugger

Evidence-driven software repair for Python projects.

![Agentic Debugger terminal welcome screen](docs/assets/agentic-debugger-welcome.png)

## What it does

Agentic Debugger investigates and repairs Python bugs in an isolated workspace, runs validation, and records evidence for independent verification.

Instead of only suggesting code, it reproduces a bug, diagnoses it with debugging tools, applies a candidate repair away from your source tree, and accepts the repair only after independent checks pass.

## Quick start

Requires Python 3.11+ and git. No GPU, model provider, or WSL is needed to install and launch.

```powershell
git clone https://github.com/dlgonur/agentic-debugging.git
cd agentic-debugging
.\scripts\install_windows_alias.ps1
agenticdebugger
```

Check the installation without contacting any provider:

```powershell
agenticdebugger --doctor
```

Alternative portable install for developers on any OS:

```powershell
python -m pip install -e ".[app]"
agenticdebugger
```

## Using the app

Launch with `agenticdebugger` from anywhere.

- Try it with no provider: start a curated task with the deterministic offline model. Offline runs contact no provider.
- Configure a model: press `m` (Model Providers) from Home or Session Setup. Fresh installs configure none.
- Debug your own code: press `p` (Debug Local Project), pick a clean Git repository, describe the bug, and select a live model. Local Project sessions require a live model.

## How a repair works

1. Reproduce the bug and inspect code, tests, and runtime state.
2. Diagnose the root cause.
3. Patch in a disposable workspace, never directly in your source tree.
4. Run validation tests.
5. Accept the repair only after an independent verifier passes.
6. Local Project changes reach your repository only through the explicit Apply To Project gate.

## More information

- [Application architecture](docs/architecture/local-application-v1.md)
- [Model providers](docs/architecture/model-providers-v1.md)
- [Offline demo guide](docs/demo/guide.md)
- [Results and evidence index](docs/results-index.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## Limitations

- Local Project sessions require a configured live model; curated tasks can run offline with the deterministic model.
- Configured command-model sessions launch a user-configured local command with that executable's host capabilities; child-process network isolation is not enforced.

## License

No open-source license is currently granted. The repository may be inspected,
but reuse and redistribution require permission from the copyright holder.
