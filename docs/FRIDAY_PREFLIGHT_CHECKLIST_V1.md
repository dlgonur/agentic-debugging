# Friday Preflight and Rehearsal Checklist v1

**Date:** 2026-08-05
**Branch:** `goal/friday-final-delivery-v1` (candidate; see baseline note)
**Source baseline:** `456f0e9a6576aab912f5af5980d756ff4e1e9dc3` — accepted
presentation plan/deck/cue delivery commit, built from accepted source
baseline `456f0e9`; campaign infrastructure accepted through `0abb588`; V4
identity correction accepted through `fc7c85b`. This checklist is part of the
uncommitted Friday final-delivery candidate; its integration commit is not
yet known.
**Presentation:** 2026-08-07, main track 24.5 min (Q&A excluded), short track
11.5 min (Q&A excluded) — timings per `docs/FRIDAY_PRESENTATION_CUE_SHEET_V1.md`.
**Usage:** run the day before and once more on presentation day. This checklist
is the consolidated final gate; the plan's Section 11 checklist and the cue
sheet's Section 10 checklist are the per-document versions.

---

## 1. Repository and Git preflight

- [ ] Presentation baseline recorded: on presentation day, run from clean
      `main` matching `origin/main`, containing the delivery bundle files
      (once integrated) and descending from the accepted source baseline
      `456f0e9`. Do not present from the candidate branch.
- [ ] `git status` shows no tracked modifications; only ignored local files
      (`.opencode/`, `_ai-review/`, `operator/`) may be present.
- [ ] The Friday delivery bundle files exist at HEAD:
      `docs/FRIDAY_DELIVERY_MANIFEST_V1.md`,
      `docs/FRIDAY_PREFLIGHT_CHECKLIST_V1.md`,
      `docs/FRIDAY_STATUS_HANDOFF_V1.md`, plan/deck/cue sheet v1.2.
- [ ] Manifest SHA-256 table matches the actual tracked files (recompute with
      the command in the manifest if in doubt).

## 2. Environment preflight (complete before presentation day)

- [ ] `python --version` reports 3.11 or newer.
- [ ] `python -m pip install -e .[test]` completes successfully. Note: this
      step may require package-index access or already-cached dependencies —
      it is the only environment step with a potential network dependency,
      so it must be completed before presentation day.
- [ ] `python -c "import agentic_debugger"` imports.
- [ ] No network, WSL, provider, or credential requirement exists for the
      demo **once the environment is prepared** (verified: the demo runs
      fully offline). Loss of internet on presentation day is safe only when
      the environment was already prepared.

## 3. Demo rehearsal (single-task, presentation form)

- [ ] Fresh timestamped output directory used (never reuse an existing one):
      ```powershell
      $demoOut = "demo-out-friday-" + (Get-Date -Format "yyyyMMdd-HHmmss")
      python -m agentic_debugger.demo --output-dir $demoOut --task-id curated-off-by-one-002
      ```
- [ ] Exit code is 0; terminal reports `cases: 2`.
- [ ] `$demoOut\results.json` aggregates: 2 cases; verifier `RESOLVED` 2/2;
      F2P 1/1 + P2P 2/2 per case; localization `CORRECT_TARGET_SYMBOL` 2/2.
- [ ] `$demoOut\technical-evaluation-summary.md` Section 1 "Tested state"
      (HEAD, offline policy) and Section 3 table (Controller Done, Verifier
      COMPLETED/RESOLVED, Full suite PASS).
- [ ] `$demoOut\technical-evaluation-summary.md` Section 4 "Offline
      enforcement": 0 provider attempts, 0 network attempts; workspace
      cleanup `CLEANED`; canonical fixture unchanged.
- [ ] `$demoOut\trajectories\curated-off-by-one-002__pdb-on-uncertainty.events.jsonl`
      shows reproduce → understand → gate → patch → validate transitions and
      typed directives (file-read / search / test-run / patch-apply).
- [ ] Both `.semantic.json` projections exist and the run is replay-valid
      (`results.json` `trajectory.replay_valid`).
- [ ] `$demoOut\REPRODUCE.md` exists.
- [ ] Rehearsal outputs kept as local operational fallback (never the durable
      claim source). This bundle's rehearsal evidence lives at
      `_ai-review/friday-final-delivery-v1/rehearsal/demo-out-friday-20260805-170405/`
      (exit 0; 2/2 RESOLVED; F2P 2/2; P2P 4/4; 0 provider/0 network; 5 PDB
      observations on the PDB policy; workspace CLEANED; replay-valid 2/2;
      canonical fixtures unchanged 2/2).

## 4. Optional full rehearsal (10 cases, strict)

- [ ] Only when a clean 10/10 is certain; each execution uses a fresh unique
      output directory (never reuse, delete, or overwrite a prior output):
      ```powershell
      $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
      $fullOut = "demo-out-full-$stamp"
      $strictOut = "demo-out-strict-$stamp"

      python -m agentic_debugger.demo --output-dir $fullOut
      python -m agentic_debugger.demo --output-dir $strictOut --strict
      ```
- [ ] Exit 0; 10 cases; all verifier `COMPLETED`/`RESOLVED`; F2P 10/10;
      P2P 22/22; localization `CORRECT_TARGET_SYMBOL` 10/10.
- [ ] A `--strict` failure is a regression signal — do not force it
      (`docs/DEMO_GUIDE_V1.md` Section 6). Fall back to the recorded evidence.

## 5. QLoRA field check

- [ ] Every Section 8B field of `docs/FRIDAY_PRESENTATION_PLAN_V1.md` is
      `PENDING — DO NOT INFER` unless an accepted checkpoint record exists.
- [ ] The prescribed pending-results sentence is ready (plan Section 8,
      cue sheet Section 7).
- [ ] Audit wording is exactly "owner-delegated independent FirstMate AI
      audit; not human review"; 75 rows / 39 ACCEPT / 36 REJECT; corpus
      acceptance shown as pending; no "human audit / manual audit / human
      sign-off" phrasing.
- [ ] Historical freeze flags at `3f0d3e7` are labeled historical, never
      current-authorization evidence.

## 6. Content and claims gate

- [ ] "May be said" list (plan Section 6) scanned; every claim used maps to
      the evidence index (manifest Section 2 / plan Section 4).
- [ ] "Must not be said" list (plan Section 7, cue sheet Section 8) scanned:
      no live-repair, PDB-benefit, gold-patch-as-model-performance, invented
      training metrics, BugsInPy execution, or RAG/DPO implementation claims.
- [ ] Numbers checked for consistency across deck, cue sheet, plan, and
      handoff: status counts 7/10/3/7/0; demo 10/10 + 22/22; V4 26,139 /
      38,534 bytes and `$0.007378` / `$0.012323`; campaign 389 / 3394+3+6;
      QLoRA 3457 / 3 / 36, 75 rows, 39 / 36.
- [ ] Turkish-language boundary: deck and cue sheet are Turkish; all technical
      terms keep the established English forms; no invented numbers in either
      language.

## 7. Offline availability

- [ ] Deck, cue sheet, plan, manifest, handoff, and the demo rehearsal outputs
      are reachable offline (no internet dependency for demo or evidence).
- [ ] Tracked evidence files for the V4 account and QLoRA statements are
      reachable (`research/quixbugs/PAIRED_PILOT_V4.json`, status map Section
      5, plan Section 8A) without the ignored review packages.
- [ ] Internet-loss safety confirmed: environment already prepared
      (`pip install -e .[test]` done before presentation day); demo and all
      evidence are local.

## 8. Timing rehearsal

- [ ] Main track: segments 1–7 within ~20 min talk; demo inside segment 8;
      total 24.5 min (Q&A excluded).
- [ ] Short track: 11.5 min (Q&A excluded) rehearsed with the exact slide
      order of deck Annex A.
- [ ] Slide 16 transition and return sentences rehearsed verbatim (cue sheet
      Sections 3 and 6).

## 9. Presentation-day sequence (executable summary)

1. `git status` clean on `main` matching `origin/main`, containing the
   delivery bundle files and descending from `456f0e9`.
2. `python -m pip install -e .[test]` (only if the environment changed after
   preparation; may need package-index access or cached dependencies).
3. Open deck; run the main or short track per timing.
4. At slide 16, run the single-task demo with a fresh timestamped `$demoOut`;
   open `results.json`, `technical-evaluation-summary.md`, the PDB-policy
   `events.jsonl`, and the `.semantic.json`.
5. State the scripted stand-in boundary during the demo.
6. On demo failure: re-check install; retry once with a fresh `$demoOut`;
   otherwise present the preserved rehearsal outputs as local operational
   fallback (never a durable claim source). Never weaken verifier, budget,
   or regression gates.
7. On QLoRA questions: recite the prescribed sentence; do not predict values.
8. Close with roadmap (three measurable next steps) and thank the audience.
