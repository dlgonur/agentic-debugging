# Experiment Freeze Record

**Experiment:** `qlora-patch-pilot-v1`
**Frozen:** `2026-08-04T19:14:00+03:00`
**Repository baseline (required ancestor):** base commit `66fb5d5` on branch `experiment/qlora-patch-pilot-v1`. The live execution HEAD must descend from `66fb5d5`; the branch name is operational metadata, not a frozen scientific identity. The freeze record intentionally does not contain a future candidate commit hash.

## Immutable identities

- Model: `Qwen/Qwen2.5-Coder-7B-Instruct` @ `c03e6d358207e414f1eca0bb1891e29f1db0e242`
- Dataset: `bigcode/commitpackft` / `python` @ `fc56fe33c030c6daa414c2b112c932b8eed085e6`
- Transformation: `commitpackft-python-patch-v1`

## Held-out tasks

| Task | Fixture tree SHA-256 | Buggy SHA-256 | Corrected SHA-256 | Gold patch SHA-256 |
|---|---|---|---|---|
| `curated-none-handling-001` | `11fcd99767052b52e786eeb9bc3947c8af0d2708322e251fb10da2166e341bec` | `24363dea4b935443e4ad1cfb78ed6d219c9e36d78c4386599226119f5f359dee` | `b4846751c8550b7fc755723903930150f3b333f726ce8b90f03ff2eedc0518ec` | `be71ec8c5bec4656c51d3e01fb87614e0f35c15a2ce72de3667af28723260976` |
| `curated-off-by-one-002` | `77acb3be4556abd20afd324eca050d8c5d0fadb42f632f649ba30b5c54da35ec` | `24cba358fff88b1c896320ba8bf8514e36416882ebd48a4bb042326fb4a7ea02` | `fe35d5951b200b43d007f90aa4e3e2de4aa257acdf4282162c23cb92c4078d1c` | `0973946b2047b81bd7d4d18a061942808ebfb2d60dc9078c2f28132298f47f1f` |
| `curated-wrong-branch-003` | `1b94c309024b782a9fa9902ebc9a1b1dd0816f6bd3e862b0396852dabe8b9393` | `0b5fa21d8fa7a39a8cd8f122713f903caeecf8f7fb88e77c5de35b7c58c95f28` | `453c8ea531489f4c835b9c3ba0c2a0a7b809181d937057d7fe21088074ca4ecb` | `a7c92c1ae7d7f2fdd4c9f037fda6bb00384bcc533362812a1398ca23e8ee196c` |
| `curated-mutation-alias-004` | `f3e73c5b9ecf182018fe139518d66f3b5c0ed9af12e104456ae304fb6208b56a` | `d5cc37ce4ff5d7617dab286b6b19343794c8a4a903f33677ca50b046cc0a199f` | `d8af473ead721dbec98d1675cea7c62c01129897e64fdd2c87d5f87020f9eb0c` | `68a2d2b2ba435bc2c41f1467f573dd66ae9d52a2df1a964f56b6da288b2cd029` |
| `curated-caller-callee-005` | `003ed4df6be9e58417f1ec2aa75821eccf065ad1614f930852aabd9115b7bba9` | `518038818a1007e9a3fa847311faf4247628891b5c1701cd74a68cf7dfb255f0` | `cfa3587ef499c765125d6170206395c90ae5d6483642a6c28016d0b48fa5232c` | `d32089310524d945cef85130e366082d48e9c08edda1e06f8874baddc6e5dcb2` |

Only hashes are recorded. Corrected source and gold patches are not serialized in tracked experiment assets.

## Gate

- Final full training: **not authorized**.
- Held-out generation: **not authorized**.
- Permitted before review: deterministic transformation, audit preparation/completion (owner-delegated independent FirstMate AI audit; not human review), one-step non-held-out QLoRA smoke, adapter save/reload, non-held-out inference, strict diff parsing, and existing-verifier smoke.
