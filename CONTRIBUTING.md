# Contributing

Thank you for helping improve Agentic Debugger.

## Before opening a change

- Use Python 3.11 or newer.
- Install development dependencies with `python -m pip install -e ".[app,test]"`.
- Keep the single-controller, Python/PDB-first architecture intact.
- Do not weaken verifier, path, timeout, containment, or cleanup checks to make
  a case pass.
- Never commit credentials, model checkpoints, external datasets, generated
  run directories, or `_ai-review/` material.

## Validation

Run the smallest focused test set that covers the change. For controller,
runtime, PDB, patch, or verifier changes, also run the deterministic demo when
appropriate:

```powershell
python -m pytest <affected-test-path> -q
python -m agentic_debugger.demo --output-dir demo-out --task-id curated-off-by-one-002
```

Include the exact commands and results in the pull request. Distinguish
infrastructure results, model results, and scientific claims.

## External execution

Live model routes, WSL benchmark campaigns, and license-gated datasets require
explicit authorization. Synthetic transports and gold patches do not establish
model debugging ability.

## Pull requests

Keep each change coherent. Explain the outcome, affected paths, validation, and
known limitations. Preserve unrelated work and frozen evidence.
