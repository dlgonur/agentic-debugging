# QuixBugs research contracts

Frozen manifests and paired-pilot contracts for QuixBugs Python. These are
evaluation-infrastructure artifacts, not model-performance claims.

## Gold / smoke infrastructure (accepted)

Eight-task no-model gold baseline (`gcd`, `bucketsort`, `find_in_sorted`,
`flatten`, `kth`, `hanoi`, `is_valid_parenthesization`, `kheapsort`) on
pinned revision `4257f44b0ff1181dedaedee6a447e133219fcebf`: 8/8 selected
tasks solved by applying the literal upstream buggy→corrected diff. See
`docs/datasets/quixbugs/baseline-8-task.md` and
`docs/datasets/quixbugs/smoke-guide.md`. This validates containment and
the verifier path only.

Per-task smoke manifests in this directory (`GCD_SMOKE_MANIFEST_V1.json`,
…) are the frozen task identities for those infrastructure runs.

## Paired static-versus-PDB campaign (optional)

Versioned paired-pilot manifests:

| File | Role |
| --- | --- |
| `PAIRED_PILOT_V1.json` | Historical Zen/free-tier three-task, six-case design |
| `PAIRED_PILOT_V2.json` | Same tasks; OpenCode Go subscription route contract |
| `PAIRED_PILOT_V3.json` | Adds `VALIDATION_NOT_REACHED` terminal + provenance |
| `PAIRED_PILOT_V4.json` | Current frozen contract (canonical SHA-256 `020dfc1f7b8f23aa96a4d7c7942429e306cc290906abfed5ce96cde22b90354d`) |

Authorization and adapter templates here are non-authorizing schema
references. Real operator artifacts live in ignored `operator/`.

## Accepted campaign status

The historical Authorized Six-Case Live Campaign is
**RETAIN_OPTIONAL / OWNER-AUTHORIZED** (`docs/project-closeout.md` §9).
It is not required for Local Application V1 or the accepted R1–R6
closeout. The frozen OpenCode Go V4 path is preserved evidence, not the
current product route. The 2026-08-17 Ollama Cloud session
(`sess-20260817-103258-3d1193`) did not record PDB and does not supersede
the paired static-versus-PDB question. Do not mutate `PAIRED_PILOT_V4.json`
onto Ollama.

## Authoritative sources

- This directory's JSON contracts
- `docs/datasets/quixbugs/`
- `docs/project-closeout.md` §9
- `docs/results-index.md`
