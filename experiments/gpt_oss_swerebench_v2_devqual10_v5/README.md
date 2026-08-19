# GPT-OSS SWE-rebench V2 DEVQUAL-10 V5

V5 is a direct-execution development qualification treatment over the exact
immutable first-ten population and order used by V4. It changes orchestration
only: there is no prerequisite readiness bundle or ten-task infrastructure
qualification. Cheap deterministic local guards run before the first provider
request; task setup, Docker, and official verification are lazy within the
actual task lifecycle.

The interrupted V4 owner attempt stopped before provider/model inference and
is not a model-capability result. V5 does not reuse its external remnants.

Use one command, with an explicitly fresh external root:

```powershell
python scripts/gpt_oss_swerebench_v2_devqual10_v5.py execute `
  --live `
  --config-root <external-config-root> `
  --external-root <fresh-external-campaign-root>
```

There is intentionally no V5 `preflight` or `authorize` command and no V5
readiness directory.
