# QLoRA Patch Pilot v1 — Build and Smoke Evidence

## Scope completed

- Frozen model, dataset, source-license allowlist, prompt, transformation, training, generation, repository baseline, and five held-out hash identities.
- Deterministic CommitPackFT JSONL filtering and unified-diff conversion.
- Exact deduplication, SimHash near-deduplication, held-out exact/near checks, repository-unique selection, and repository-disjoint train/validation split.
- Preferred/minimum corpus gates with no filter relaxation.
- Deterministic 50-accepted and 25-rejected manual audit packets and a fail-closed audit validator.
- Strict unified-diff-only parser and one-shot generation record that refuses regeneration.
- Saved-generation verifier path that reuses the existing `EvaluationVerifier`.
- Colab notebook for pinned data download, corpus construction, manual audit gate, one-step 7B QLoRA weight update, adapter save/reload, non-held-out inference, parser smoke, verifier smoke, and external checksums.
- Hard notebook stop before final training and held-out generation.

## Executed local evidence

| Check | Result |
|---|---|
| Freeze recomputation | PASS — 24 content identities matched; baseline verified as required ancestor `66fb5d5` (25 checks, `LOCKED`) with execution HEAD, branch/detached state, and dirty state reported in the runtime record |
| Gitless snapshot freeze | PASS — snapshot without `.git` validates all 24 frozen content identities, checks 24 |
| Baseline contract | PASS — base commit passes; descendant commit passes; unrelated history fails closed; freeze record contains no future candidate commit |
| Focused unit tests | PASS — 30 passed |
| Python compile | PASS |
| Notebook JSON validation | PASS — 18 cells, 14 code cells |
| Synthetic corpus pipeline | PASS — 1,755 input; 1,725 filtered; 1,500 train; 200 validation; 30 rejected |
| Repository overlap | PASS — none |
| Held-out exact/near accepted matches | PASS — zero |
| Audit packet production | PASS — 50 accepted and 25 rejected records sampled; manual fields intentionally pending |
| Audit gate strictness | PASS — missing manual fields, non-contract verdicts, and contradictory rows fail closed |
| Terminal-newline diff gate | PASS — rows lacking a final newline are rejected deterministically |
| Empty corpus output gate | PASS — non-empty output directory fails without modification |
| Strict parser negative paths | PASS |
| Existing verifier, synthetic non-held-out task | PASS — `COMPLETED / RESOLVED`, F2P 1/1, P2P 1/1, syntax passed, canonical fixture unchanged, workspace cleaned |
| One-step LoRA aggregate smoke | IMPLEMENTED — notebook asserts aggregate positive finite delta across all trainable LoRA tensors; requires CUDA Colab execution |
| Package exports | PASS — `snapshot_trainable_lora_parameters` and `aggregate_lora_delta` exported by `agentic_debugger.training` |
| Candidate patch | PASS — repo-relative canonical patch of all 14 files; applies cleanly to a clean checkout of base commit `66fb5d5`; no absolute Windows paths in headers |
| Final training | NOT RUN — prohibited by freeze gate |
| Held-out generation | NOT RUN — prohibited by freeze gate |

## External local artifact root

`outputs/qlora_patch_pilot_v1/`

`external_artifacts.json` records each current external path, byte size, SHA-256, artifact kind, configuration identity, and provenance identity. These files are ignored and must not be committed.

## Blocked execution evidence

This execution environment has no NVIDIA GPU and does not contain the frozen Transformers/PEFT/bitsandbytes stack. It also cannot materialize the pinned CommitPackFT Python file from the Hub. Therefore the following mandatory pre-final checks are implemented but not executed here:

- actual CommitPackFT preferred/minimum corpus construction;
- manual review of 50 real accepted and 25 real rejected records;
- pinned 7B 4-bit model load;
- one-example QLoRA weight update;
- adapter save and reload;
- model inference on a non-held-out example.

These are explicit blockers. The final full training and held-out generation must remain unauthorized until a CUDA Colab run produces those records and FirstMate reviews them.
