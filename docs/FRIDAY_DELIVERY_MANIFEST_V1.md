# Friday Professor Delivery Manifest v1

**Date:** 2026-08-05
**Branch:** the original delivery bundle is accepted and integrated on `main`
at `ab464dd` (the earlier presentation plan/deck/cue delivery commit is
`456f0e9`).
**Source baseline:** `456f0e9a6576aab912f5af5980d756ff4e1e9dc3` — the accepted
presentation plan/deck/cue delivery commit. Campaign infrastructure accepted
through `0abb588`; V4 identity correction accepted through `fc7c85b`. The
original delivery bundle (manifest, preflight checklist, handoff, and
documentation corrections) is accepted at `ab464dd`. The 2026-08-06
main-repo completion hardening (campaign ledger time provenance, transport
teardown race, known wrapper/transport test failures, post-mortem PDB entry)
is accepted and integrated on `main` at `62deca4`. The current bounds-v2
revisions to this file are an uncommitted candidate built on top of
`62deca4`; their eventual integration commit is not known. On presentation
day, run from clean `main == origin/main` containing the final accepted
files.
**Presentation date:** Friday 2026-08-07
**Purpose:** the professor-facing submission package. This manifest is the
index: what the bundle contains, what evidence backs every material claim,
which exact commands to run, and what the bundle does not authorize.
**Scope boundary:** this bundle is an offline documentation and rehearsal
package. It runs no provider, no live campaign, no WSL, no BugsInPy, no
QLoRA training, no held-out generation, and no test suite beyond the focused
checks listed here. It changes no runtime code and grants no execution
authority.

---

## 1. Delivery bundle inventory

| Path | Role | SHA-256 |
|---|---|---|
| `docs/FRIDAY_DELIVERY_MANIFEST_V1.md` | This manifest and evidence index | *self-referential — recompute with the hash command below; all other bundle files are pinned* |
| `docs/FRIDAY_PREFLIGHT_CHECKLIST_V1.md` | Final preflight and rehearsal checklist | `4094f67b89e68ee1733e80ed7dddc782e526c827b7a25246beb1d92da1aa7aa0` |
| `docs/FRIDAY_STATUS_HANDOFF_V1.md` | Concise project-status handoff and post-Friday batches | `40ef80d343849bf3a6fb4fcdc093bf18e81d6efb1877e953fbcd0cd25b0e506f` |
| `docs/FRIDAY_PRESENTATION_PLAN_V1.md` | Presentation runbook, evidence table, Q&A, contingency (v1.2) | `188804dcec6d74ac82059bd3f02e9fe6dc0caa6533bffb0eb0c2cc2a128e3216` |
| `docs/FRIDAY_PRESENTATION_DECK_V1.md` | 17-slide Turkish deck, main + short track (v1.2) | `2fe6034ba1abf90ba6644ec9a141280fd0cda2602a16e0b4dfad927369a479cb` |
| `docs/FRIDAY_PRESENTATION_CUE_SHEET_V1.md` | Presenter quick reference, timings, verbatim sentences (v1.2) | `614dee288b095dc66dff949706e6a2dbf05a799376a4a9b0b3769a6702dc5461` |
| `docs/INSTRUCTOR_AGENTIC_DEBUGGING_TODO.md` | Instructor's original 27-item list (byte-identical, unchanged) | `9e97a17ea8ab6d67ae7da008a6b497dc219bec2ba79dbcf09f8b5411155e88e3` |
| `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` | Per-item status map 7/10/3/7/0 with evidence layers | `cb4bca5f28bdfbd94375477969c8dd2320b02d5206a1b7ba77410768ac6683d1` |
| `docs/FINAL_TECHNICAL_REPORT_V1.md` | Final technical report incl. 2026-08-05 revision | `b14b9534b91b794f7129639d2d090aa87ab9024787e3cac726377dfbeb76d629` |
| `docs/DEMO_GUIDE_V1.md` | Demo guide (offline demo + recorded QuixBugs entry points) | `1e327548fb594a09d253a127c950e03e8a2c591f02a47c35565eb4fe9555987b` |
| `docs/DEMO_TASK9.md` | Task 9 demonstration contract and results | `aa404e8dfde1d5b30fad2a639351fbb688b58017ee4b58816fddde44beca9773` |
| `README.md` | Project status incl. 2026-08-05 campaign/QLoRA facts and 2026-08-06 hardening | `03bd91c90f5bd498a47cf881839bc77fd2afe52cac0898632a5ecd71aa27bce9` |
| `TODO.md` | Project TODO (2026-08-05 routing note) | `de312304a96c0985ed4a9392cded22fdaf78782452ef4430a4267b462d08b3d2` |
| `docs/PROJECT_TRACKER.md` | Execution tracker incl. 2026-08-05 and 2026-08-06 entries | `14eb8501c005d94e86f0a453da5fc48bc15a21689aca4a1d1022edfd02a9a8ed` |
| `research/quixbugs/PAIRED_PILOT_V4.json` | Frozen v4 campaign contract (canonical SHA-256 `020dfc1f7b8f23aa96a4d7c7942429e306cc290906abfed5ce96cde22b90354d`; raw file bytes hash to `8c103f9dea553a8245c277341fd2f22bc285894a32017d2057819e9355c5cd29` — the canonical hash is the canonical-JSON serialization hash per project canonical JSON rules) | — |

Hash commands (PowerShell, from the repository root):

```powershell
Get-FileHash docs\FRIDAY_DELIVERY_MANIFEST_V1.md -Algorithm SHA256
# canonical-JSON hash for manifests:
python -c "import json,hashlib;print(hashlib.sha256(json.dumps(json.load(open('research/quixbugs/PAIRED_PILOT_V4.json',encoding='utf-8')),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')).hexdigest())"
```

## 2. Evidence index (claim → tracked artifact)

All claims in the deck/cue sheet/plan trace to these tracked artifacts. The
ignored `_ai-review/` and `operator/` paths are never the durable basis of a
claim.

| Claim | Tracked evidence |
|---|---|
| Instructor scope is 27 items, unchanged | `docs/INSTRUCTOR_AGENTIC_DEBUGGING_TODO.md` |
| Status map 7 COMPLETED / 10 PARTIAL / 3 IN PROGRESS / 7 NOT STARTED / 0 BLOCKED | `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` §3 |
| Single-controller architecture, typed tools, real PDB path, independent verifier | `agentic_debugger/agent/`, `runtime/`, `skills/`, `evaluation/`; report §2 |
| Deterministic demo: 10/10 RESOLVED, F2P 10/10, P2P 22/22, localization 10/10, cleanup | `docs/DEMO_TASK9.md`, `docs/DEMO_GUIDE_V1.md` §2; `agentic_debugger/demo/` |
| Scripted stand-in; 21 scripted PDB observations; golden trajectory | `docs/DEMO_TASK9.md`; `tests/golden_trajectories/data/pdb-gated-successful-repair.json` |
| QuixBugs baselines are gold-patch, infra-only (1-task smoke; 8/8 tasks, 49/49 nodes) | `docs/QUIXBUGS_SMOKE_USAGE_V1.md`, `docs/QUIXBUGS_EIGHT_TASK_BASELINE_V1.md` |
| BugsInPy primary but license-blocked | `docs/DATASET_EVALUATION_DECISION_V1.md`, `docs/BUGSINPY_LICENSE_GATE_V1.md` |
| SWE-bench DEFER, Defects4J NO-GO | `docs/DATASET_EVALUATION_DECISION_V1.md` §2 |
| Route: OpenCode Go subscription, DeepSeek V4 Flash, protocol 1.3, v4 manifest | `CURRENT_AGENT_ROSTER.md`; `research/quixbugs/PAIRED_PILOT_V4.json` |
| Real live interaction; V4 case facts (26,139 / 38,534 bytes; `$0.007378` / `$0.012323`; zero verifier runs; zero PDB; `ABORTED/BUDGET_EXCEEDED`) | `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` §5; `TODO.md`; `docs/PROJECT_TRACKER.md` 2026-08-05; `tests/fixtures/quixbugs_v4_budget_verifier_attempt_fixture.json` + `tests/unit/test_quixbugs_v4_budget_verifier_path.py` (attempt `3b5d7488…`; Case 1 = `find-in-sorted`/`pdb-on-uncertainty` order 1; Case 2 = `find-in-sorted`/`static-baseline` order 2) |
| Earlier attempts are not valid experiments (`705aa047…`, `81f2e5d8…`, `4c7fc444…`, `fddf1e39…`, `8890ed9…`/`320550…`) | `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` §5 |
| Campaign infrastructure accepted on `main` through `0abb588`; identity correction at `fc7c85b`; focused 389 / full 3394 + 3 skipped + six known failures | `docs/PROJECT_TRACKER.md` 2026-08-05; `README.md` |
| Model selection frozen (Qwen/Qwen2.5-Coder-7B-Instruct, revision `c03e6d35…`, Apache-2.0) | `experiments/qlora_patch_pilot_v1/freeze_record.json` on `experiment/qlora-patch-pilot-v1` commit `3f0d3e7` |
| QLoRA methodology frozen; implementation accepted at `3f0d3e7`; owner suite 3457 / 3 / 36 | `experiments/qlora_patch_pilot_v1/` (configs, SMOKE_EVIDENCE, notebook); `tests/unit/test_qlora_patch_pilot.py`; `docs/PROJECT_TRACKER.md` 2026-08-05 |
| 2026-08-06 hardening: post-mortem PDB entry (TODO 6.1.3) with 90 unique focused tests; campaign ledger timestamp provenance; transport teardown race repaired; known wrapper/transport failures repaired | `README.md`; `docs/PROJECT_TRACKER.md` 2026-08-06; `tests/unit/test_pdb_post_mortem.py` |
| Full suite NOT green (recorded): 3448 passed / 3 skipped / 32 failed; the 32 failures are the pre-existing wrapper-preflight subprocess-chain family, passing 85/85 in isolation; bounds-v2 A/B reproduces the identical 32-node failure set on both the clean `62deca4` checkpoint and the candidate | `README.md`; `docs/PROJECT_TRACKER.md` 2026-08-06 |
| Real minimum-tier corpus 1,000/150, zero leakage; one-step CUDA update + adapter reload | Layer 2 (FirstMate-reviewed external evidence, not merged); status map items 9/12 |
| Independent FirstMate AI audit of 75 frozen rows: 39 ACCEPT / 36 REJECT, AI reviewer identity disclosed; corpus not modified; final training authorized 2026-08-05; no accepted artifact; held-out unauthorized | Status map §5 QLoRA boundary; plan §8; README 2026-08-05 |
| RAG NO-GO-FOR-NOW, SFT DEFER, DPO NO-GO-FOR-NOW | `docs/DATASET_EVALUATION_DECISION_V1.md` §10; `docs/MODEL_RAG_SFT_DPO_DECISION_GATE_V1.md` |
| Historical Zen matrix descriptive-only | `docs/PROJECT_TRACKER.md` [historical]; README; `CURRENT_AGENT_ROSTER.md` |

## 3. Exact commands

Network note: the deterministic demo itself has zero provider and zero
network dependency **once the environment is prepared**. Only environment
setup (`pip install -e .[test]`) may require network access — already cached
dependencies or package-index access. Complete environment setup **before**
presentation day; loss of internet on presentation day is safe only when the
environment is already prepared.

### 3.1 Setup (before presentation day)

```powershell
python --version                 # must be 3.11+
python -m pip install -e .[test] # may need package-index access or cached deps
python -c "import agentic_debugger; print('ok')"
```

### 3.2 Primary demo (presentation form — single task, both policies)

Fresh timestamped output directory per run (never reuse, delete, or
overwrite a prior output directory):

```powershell
$demoOut = "demo-out-friday-" + (Get-Date -Format "yyyyMMdd-HHmmss")
python -m agentic_debugger.demo --output-dir $demoOut --task-id curated-off-by-one-002
```

Expected: exit code 0; `cases: 2`; `results.json` aggregates 2 cases,
verifier `RESOLVED` 2/2, F2P 1/1 + P2P 2/2 per case, localization
`CORRECT_TARGET_SYMBOL`; summary sections 1/3/4; trajectories
`<case>.events.jsonl` + `.semantic.json`; `REPRODUCE.md`. Measured offline:
0 provider, 0 network; workspaces `CLEANED`; canonical fixtures unchanged.
`--output-dir` is required for every invocation including `--list-tasks`.

### 3.3 Full deterministic rehearsal (optional, before Friday only)

Fresh unique output directories per execution — never reuse, delete, or
overwrite a prior output directory (the demo refuses to reuse an occupied
output directory):

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$listOut = "demo-out-list-$stamp"
$fullOut = "demo-out-full-$stamp"
$strictOut = "demo-out-strict-$stamp"

python -m agentic_debugger.demo --output-dir $listOut --list-tasks
python -m agentic_debugger.demo --output-dir $fullOut
python -m agentic_debugger.demo --output-dir $strictOut --strict
```

### 3.4 Reproduction of the recorded evidence (read-only; do not re-run WSL)

- Demo guide: `docs/DEMO_GUIDE_V1.md` (Sections 2–4).
- Recorded QuixBugs smoke/baseline commands (accepted campaigns — do not
  re-run casually):
  ```powershell
  python scripts/quixbugs_live_smoke.py
  python scripts/quixbugs_eight_task_baseline.py --skip-excluded
  ```
- V4 account: `docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md` §5;
  `research/quixbugs/PAIRED_PILOT_V4.json`; replay path
  `tests/fixtures/quixbugs_v4_budget_verifier_attempt_fixture.json` +
  `tests/unit/test_quixbugs_v4_budget_verifier_path.py`.

### 3.5 Fallback commands

| Situation | Fallback |
|---|---|
| Demo command fails (exit 1) | Re-run install; retry the single-task form with a fresh `$demoOut`; then present the preserved rehearsal outputs (`_ai-review/friday-final-delivery-v1/rehearsal/…`) as local operational fallback only. Never weaken verifier/budget gates. |
| `--strict` fails | Regression signal per `docs/DEMO_GUIDE_V1.md` §6; do not force; use recorded evidence. |
| No internet / no provider | Primary demo needs neither; V4 account is fully tracked locally. |
| QLoRA questions | Prescribed sentence (plan §8, cue sheet §7); no predicted values. |

### 3.6 Presentation-day sequence

1. Git check: clean `main` matching `origin/main`, containing the delivery
   bundle files (once integrated) and descending from the accepted source
   baseline `456f0e9`; clean tracked tree. Do not present from the candidate
   branch.
2. Deck open; main track (24.5 min) or short track (11.5 min).
3. Slide 16: run §3.2 demo; open `results.json`, summary, PDB-policy
   `events.jsonl`, `.semantic.json`; state the scripted stand-in boundary.
4. Recorded V4 evidence: present as recorded experiment, never live.
5. QLoRA: prescribed sentence; all §8B fields `PENDING — DO NOT INFER`.
6. Close: three measurable next steps (final-training artifact review and
   corpus acceptance; held-out comparison; authorized six-case campaign).

## 4. Fresh rehearsal evidence (this bundle, 2026-08-05)

Run: `python -m agentic_debugger.demo --output-dir demo-out-friday-20260805-170405
--task-id curated-off-by-one-002` — exit 0, `cases: 2`; artifacts preserved at
`_ai-review/friday-final-delivery-v1/rehearsal/demo-out-friday-20260805-170405/`.

| Check | Result |
|---|---|
| Verifier outcomes | RESOLVED 2/2 (both policies) |
| F2P / P2P | 2/2 (1/1 per case) / 4/4 (2/2 per case) |
| Localization | `CORRECT_TARGET_SYMBOL` 2/2 |
| Full suite | PASS 2/2 |
| PDB observations (PDB policy) | 5/5 succeeded; static policy 0 |
| Offline guard (measured) | 0 provider, 0 network |
| Workspace lifecycle | CLEANED 2/2; canonical fixtures unchanged 2/2 |
| Trajectory replay | valid 2/2; `.semantic.json` present 2/2 |
| Environment recorded | HEAD `456f0e9a6576aab912f5af5980d756ff4e1e9dc3`, branch `goal/friday-final-delivery-v1`, model backend `offline-deterministic-demo` |

## 5. Boundaries and authorization gates

- This bundle authorizes no provider/model execution, no live campaign, no
  WSL execution, no BugsInPy acquisition/execution, no QLoRA/Colab run, no
  held-out generation, and no full-suite run.
- QLoRA final training has no accepted artifact; all result fields remain
  `PENDING — DO NOT INFER`; the historical freeze flags at `3f0d3e7` are not
  current-authorization evidence.
- Do not expose gold patches, oracle fields, corrected source, or test
  answers to any evaluation model.
- Do not reuse `_ai-review/` or `operator/` content as durable evidence.
