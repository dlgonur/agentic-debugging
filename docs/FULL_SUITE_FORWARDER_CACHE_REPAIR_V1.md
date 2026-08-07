# Full-Suite Forwarder Cache Repair v1

## Outcome

The previously recorded OpenCode wrapper full-suite failure family is fixed.
The failures were not caused by generic OS resource pressure. They were an
order-dependent collision in the test-only compiled native forwarder cache.

`scripts/opencode_go_synthetic_executable.py` kept an in-memory cache keyed by
`(interpreter, target_script)`, but every key compiled to the same
`%TEMP%/opencode-go-forwarder-<pid>/opencode.exe`. PowerShell `Add-Type` left
the first assembly at that path in place. A later cache entry therefore
pointed to the first target's executable.

Under full-suite order, the operator-route preflight integration first built
a fake that reported OpenCode `1.18.10`. The later OpenCode Go fixtures asked
for the `1.0.0` synthetic target but copied the stale `1.18.10` forwarder.
The real wrapper correctly failed closed on native/launcher version drift.
An isolated test process built only the requested target, explaining why the
same nodes passed alone.

## Repair

Each cache miss now compiles in a unique `mkdtemp` build directory registered
for process-exit cleanup. The on-disk executable therefore has the same
target identity as its in-memory cache key, and PID reuse cannot inherit an
old assembly. The production OpenCode wrapper, native-executable resolver,
version proof, route checks, authorization, and transport behavior are
unchanged.

An explicit regression builds two different target scripts in one process,
runs both compiled executables, and proves that each retains its own output.
The collection-warning cleanup in the same maintenance pass marks production
`TestRunKind`, `TestRunResult`, and `TestRunner` contracts as non-tests,
escapes two Windows path docstrings, and renames a tuple-returning RAG helper
so pytest no longer collects it as a test.

## Validation

- exact cross-target ordering reproduction plus the previously failing
  campaign node: 5 passed;
- affected wrapper, transport, case-runner, and protocol surface: 156 passed;
- post-cache-fix full suite: 3733 passed, 3 skipped, 1 warning in 1417.80 s;
- the sole warning was then removed; RAG schema plus cache regression: 28
  passed, final collection: 3735 tests with no warnings;
- `compileall` and `git diff --check`: clean.

No provider, network route, live campaign, WSL benchmark, BugsInPy source,
QLoRA repository, training, or held-out generation was used.
