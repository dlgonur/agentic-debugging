# GPT-OSS SWE-rebench V2 DEVQUAL-10 V6

V6 is the direct-execution development qualification treatment over the exact
immutable first-ten population and order used by V5. It changes only the
direct adapter bootstrap, generic command-error classification, and truthful
provider-generation accounting. It does not reuse V5 external remnants.

Use one command, with an explicitly fresh external root:

```powershell
python scripts/gpt_oss_swerebench_v2_devqual10_v6.py execute `
  --live `
  --config-root <external-config-root> `
  --external-root <fresh-external-campaign-root>
```

There is intentionally no V6 `preflight` or `authorize` command and no V6
readiness directory. Provider/model inference remains explicitly authorized
only by `execute --live`.
