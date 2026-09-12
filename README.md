# Agentic Debugger

A terminal application for debugging and repairing Python projects.

![Agentic Debugger terminal welcome screen](docs/assets/agentic-debugger-welcome.png)

## What it does

Agentic Debugger reproduces a reported Python bug, inspects code, tests, and runtime state, applies a candidate patch in an isolated workspace, and accepts the repair only after independent validation passes.

## Quick start

Requires Python 3.11+ and git. No GPU, model provider, or WSL is needed to install and launch.

Standard install:

```sh
git clone https://github.com/dlgonur/agentic-debugging.git
cd agentic-debugging
python -m pip install -e ".[app]"
agenticdebugger
```

Check the installation without contacting any provider:

```sh
agenticdebugger --doctor
```

On Windows, `scripts\install_windows_alias.ps1` is a convenience option that installs the `agenticdebugger` launcher globally:

```powershell
.\scripts\install_windows_alias.ps1
agenticdebugger
```

## Using the app

Launch with `agenticdebugger` from anywhere.

- Try it with no provider: curated demo tasks can run offline without contacting a model provider.
- Configure a model: press `m` (Model Providers) from Home or Session Setup. Fresh installs configure none.
- Debug your own code: press `p` (Debug Local Project), pick a clean Git repository, describe the bug, and select a live model. Local Project sessions require a live model.

## How a repair works

1. Reproduce the bug and inspect code, tests, and runtime state.
2. Diagnose the root cause.
3. Patch in an isolated workspace, never directly in your source tree.
4. Run validation tests.
5. Accept the repair only if all checks pass.
6. Local Project changes reach your repository only through the explicit Apply To Project gate.

## More information

- [Application architecture](docs/architecture/local-application-v1.md)
- [Model providers](docs/architecture/model-providers-v1.md)
- [Offline demo guide](docs/demo/guide.md)
- [Results and evidence index](docs/results-index.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## Limitations

- Local Project sessions require a configured live model; curated tasks can run offline without contacting a provider.
- Configured command-model sessions are not network-sandboxed. Run only commands you trust.

## License

Licensed under the [MIT License](LICENSE).
