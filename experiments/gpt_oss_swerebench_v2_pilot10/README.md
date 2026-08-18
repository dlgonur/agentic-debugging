# GPT-OSS SWE-rebench V2 Pilot-10

Frozen evaluation path for Ollama Cloud `gpt-oss:20b-cloud` on the
canonical SWE-rebench V2 **clean + <=32K repository-disjoint validation**
population. This family is infrastructure and experiment-freeze only.

Provider inference is **not** authorized here. The next accepted task must
execute the frozen Pilot-10 without changing the manifest, prompt contract,
controller, verifier, budgets, or task selection.

## Scientific questions

1. Overall repair: did the independent verifier classify the task RESOLVED?
2. Debugger-assisted repair: was real PDB evidence exercised, and if so did
   the independently verified repair reach RESOLVED?

A verifier-resolved run with `PDB NOT EXERCISED` is a valid repair/product
result and is **not** debugger-assisted evidence.

The historical product session `sess-20260817-103258-3d1193` on
`curated-none-handling-001` is preserved as product evidence only. It is
not in this denominator.

## Canonical population

Authority is the already-frozen B15 mask:

`artifacts/swe_rebench_v2_corpus/runs/run_2026-08-08_rev-475dd5e8_b15_contract_v1/validation_cd_clean_le32k_mask.json`

That mask is the intersection of:

- B13 repository-disjoint validation (150 tasks / 40 repos, seed `20260808`);
- B15 C/D-clean validation (142);
- B15 exact-tokenizer <=32K eligibility (135).

Verified count: **128**.

The B14/B15 oracle-file-localized SFT JSONL is **not** the evaluation
context.

## Frozen artifacts

- `frozen/population.json`
- `frozen/full_ordering.json` — entire 128-task order
- `frozen/pilot10_manifest.json` — first 10 of that order, 10 repos
- `frozen/execution_contract.json`
- `frozen/artifact_hashes.json`

Selection: SHA-256(`seed`, `instance_id`, `repo_canonical`) with seed
`gpt-oss-swerebench-v2-eval-20260818`, then first-seen repository diversity,
then remaining same-repo tasks. No outcome, gold, or preference input.

## Execution contract (next task)

See `frozen/execution_contract.json` (v2, repaired before first
provider inference). Headline values:

- Ollama Cloud / `ollama-cloud-gpt-oss-20b` / `gpt-oss:20b-cloud` / `gpt-oss:20b`
- protocol 1.3; policy `pdb-on-uncertainty`; one attempt per task
- adapter retry 0, fallback 0
- official SWE-rebench Docker evaluator, through
  `OfficialSWERebenchVerifier`, as the sole correctness authority
- parent baseline `9a47001` plus frozen `harness_content_sha256`; actual
  clean execution HEAD is recorded at authorization/runtime and need not
  equal the parent baseline
- executor-owned fresh external root per campaign under
  `%LOCALAPPDATA%\agentic-debugging\gpt-oss-swerebench-v2-pilot10`

No official public reproduction command exists. The model must supply a
`public_target` that already exists in the base-commit workspace. Hidden
FAIL_TO_PASS/PASS_TO_PASS identities stay verifier-private.

The next authorized task can use the real Local Application configured-command
worker through `scripts/gpt_oss_swerebench_v2_pilot10.py execute
--provider-authorized`; it first runs the zero-provider `authorize` gate. The
model-side public pytest target is executed through the verified official-image
dependency boundary, while private `test_patch`/hidden identities are passed
only to the candidate verifier.

## PDB honesty

Pilot-10 uses Option B: overall repair is the treatment and PDB is
`PDB UNAVAILABLE BY TREATMENT CONTRACT`. The current direct-file launcher is
not coupled to the model-selected failing pytest process, so a direct pause
cannot be called bug-relevant debugger evidence.

A separate authorized treatment must implement failing-runtime-coupled PDB
before claiming `REAL PDB EXERCISED` or debugger-assisted resolution. Its
requirements are frozen in `frozen/pdb_required_treatment_contract.json`.

A distinct future contract is
`frozen/pdb_required_treatment_contract.json`.

## Commands

```powershell
python scripts/gpt_oss_swerebench_v2_pilot10.py freeze
python scripts/gpt_oss_swerebench_v2_pilot10.py validate
python scripts/gpt_oss_swerebench_v2_pilot10.py preflight
python scripts/gpt_oss_swerebench_v2_pilot10.py authorize `
  --config-root "$env:LOCALAPPDATA\agentic-debugging" `
  --external-root "$env:LOCALAPPDATA\agentic-debugging\gpt-oss-swerebench-v2-pilot10"
python scripts/gpt_oss_swerebench_v2_pilot10.py smoke `
  --output-path "$env:TEMP\gpt-oss-swerebench-v2-zero-provider-smoke.json"
```

`authorize` is the zero-provider gate and requires the target root to be
nonexistent; the later executor creates it exactly once. `execute` remains
fail-closed unless invoked with the explicit `--provider-authorized` flag
after successful authorization.
