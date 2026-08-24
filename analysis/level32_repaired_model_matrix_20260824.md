# Level-32 repaired model matrix — 2026-08-24

Task `L32-MATRIX-01` ran the complete current Level-32 live-verified roster
once, sequentially, on `audreyr__cookiecutter-967` under the repaired
`workspace-derived-official-git-diff-v1` candidate transport.

## Frozen experiment identity

- baseline controller commit: `439239e0a878adc8d5a0bcb6b7ed08ad43a060e5`
- task/base commit: `audreyr__cookiecutter-967` /
  `ba5ba8c78e97f5dc7fb4e16c588d7be037e6e5e7`
- Docker image: `docker.io/swerebenchv2/audreyr-cookiecutter:967-ba5ba8c`
- image SHA: `sha256:0bad37ac1e0a6d692a9ef417c05753b5ad45dfa8c32fd52b0f3ecabf722af8eb`
- official evaluator commit: `c71902a8cf8d2b725f63d51f199f4d3e56f68d2d`
- candidate transport: `workspace-derived-official-git-diff-v1`
- frozen contract: public issue/scaffold, exact PDB proof, existing budgets,
  directive normalization, history/reasoning policy, and official 5 F2P + 9
  P2P evaluator; no model-specific prompt changes were introduced.
- one-time integrity gate: PASS for baseline, reference, and intentionally-bad
  controls; Docker context `desktop-linux`; no infrastructure-blocked run.

The ordered eligible roster was frozen before the first provider call in the
local operator record `operator/L32-MATRIX-01-freeze.json`. It contained 15
models. `kimi-k2.7-code:cloud` (`profile_declared`) and `kimi-k3:cloud`
(`catalog`) were recorded as `SKIPPED_NOT_CURRENTLY_ELIGIBLE`; neither was
called.

## Leaderboard

`PDB` is successful/failed observations. `Official tests` is whether official
test execution was proven. A protocol failure is ranked below semantic
rejection even when a candidate reached official evaluation.

| Rank | Model | Revision | PDB | Raw patch | Canonical | Official tests | F2P | P2P failed | Accepted | Classification |
| ---: | --- | ---: | --- | --- | --- | --- | ---: | ---: | --- | --- |
| 1 | `glm-5.1:cloud` | 2 | 3/0 | yes | yes | proven | 5/5 | 0/9 | true | AUTHORITATIVE_RESOLVED |
| 2 | `glm-5.2:cloud` | 12 | 3/0 | yes | yes | proven | 5/5 | 0/9 | true | AUTHORITATIVE_RESOLVED |
| 3 | `gpt-oss:120b-cloud` | 4 | 3/0 | yes | yes | proven | 4/5 | 1/9 | false | SEMANTIC_REJECTION |
| 4 | `gpt-oss:20b-cloud` | 1 | 3/0 | yes | yes | proven | 0/5 | 9/9 | false | MODEL_PROTOCOL_FAILURE |
| 5 | `deepseek-v4-flash:cloud` | 2 | 3/0 | no | no | no | — | — | false | MODEL_PROTOCOL_FAILURE |
| 6 | `deepseek-v4-pro:cloud` | 4 | 3/0 | no | no | no | — | — | false | MODEL_PROTOCOL_FAILURE |
| 7 | `kimi-k2.6:cloud` | 8 | 3/0 | yes | yes | proven | 4/5 | 0/9 | false | MODEL_PROTOCOL_FAILURE |
| 8 | `minimax-m2.7:cloud` | 2 | 3/0 | no | no | no | — | — | false | MODEL_PROTOCOL_FAILURE |
| 9 | `minimax-m3:cloud` | 3 | 0/0 | no | no | no | — | — | false | MODEL_PROTOCOL_FAILURE |
| 10 | `nemotron-3-nano:30b-cloud` | 3 | 3/0 | no | no | no | — | — | false | MODEL_PROTOCOL_FAILURE |
| 11 | `nemotron-3-super:cloud` | 3 | 3/0 | yes | yes | proven | 4/5 | 1/9 | false | MODEL_PROTOCOL_FAILURE |
| 12 | `nemotron-3-ultra:cloud` | 3 | 3/0 | no | no | no | — | — | false | MODEL_PROTOCOL_FAILURE |
| 13 | `qwen3.5:cloud` | 3 | 3/0 | no | no | no | — | — | false | MODEL_PROTOCOL_FAILURE |
| 14 | `gemma4:31b-cloud` | 4 | 3/0 | no | no | no | — | — | false | MODEL_PROTOCOL_FAILURE |
| 15 | `mistral-large-3:675b-cloud` | 3 | 3/0 | yes | yes | proven | 4/5 | 1/9 | false | MODEL_PROTOCOL_FAILURE |

## Candidate artifact and official evidence

The following fields are projected from each run's redacted result records;
`—` means the model did not retain a candidate or did not reach the official
evaluator.

| Model | Raw SHA-256 | Canonical SHA-256 | Semantic equivalence | Official application | Test execution proven |
| --- | --- | --- | --- | --- | --- |
| `gpt-oss:20b-cloud` | `cae328767cc3dd624f27f5744400a493f77dce03fccfb9602fe8132de4852dbf` | `757e1b237ac8c9055c77a506e4d4f9587f44ad779acbca58d58fd622bce50a2a` | true | succeeded | true |
| `gpt-oss:120b-cloud` | `cc1f68b692dd6db31b893459ebad3f5749d275a1503028efab5c76e866d53439` | `dce5ed154d8592796c8c1a5c12884e34e16b0860b267a27a52512f83068a531f` | true | succeeded | true |
| `glm-5.1:cloud` | `21e82e876c6def6bdf01b07add992cf65492b24d71c987a3b900704ddb9b2dc0` | `16bf4a14af7b02cf2f0db124f3097ef49862f952f684da2b124f884051ed543e` | true | succeeded | true |
| `glm-5.2:cloud` | `d21c09fd9d3091be0029077385047c5ac3b5c6d41f1688bbda036cca80181ffc` | `035b1d64b5ba2f20f3d05ddcd59f463e17f320a47012c15735df94d6b1734dcc` | true | succeeded | true |
| `deepseek-v4-flash:cloud` | — | — | — | — | false |
| `deepseek-v4-pro:cloud` | — | — | — | — | false |
| `kimi-k2.6:cloud` | `b3ddcce6e5cee4b76a37bf0dc30ac1e710d60f36537c0a0282e691672847b1d1` | `34974830e91c85af941fe028ea24e7dc3f4aa30e327b41896cb49a1fbc2c18b9` | true | succeeded | true |
| `minimax-m2.7:cloud` | — | — | — | — | false |
| `minimax-m3:cloud` | — | — | — | — | false |
| `nemotron-3-nano:30b-cloud` | — | — | — | — | false |
| `nemotron-3-super:cloud` | `239bd54cd2dbcd17cf1d3961d1c5c896fc7bd4c842e6ad90d6623ca367b12802` | `11b4e4c0eebe3dd4b0a40fec945fa18dff8bf3a5e178f5659893834f75b4745d` | true | succeeded | true |
| `nemotron-3-ultra:cloud` | — | — | — | — | false |
| `qwen3.5:cloud` | — | — | — | — | false |
| `gemma4:31b-cloud` | — | — | — | — | false |
| `mistral-large-3:675b-cloud` | `39876ea8630321e8705b56ebdb2b827654a9f39ba35ff021d314f60828a8c2ec` | `13ebb41c719ff317ce430704c5f5a07e741a8e7febcf9eca39b6cad9d394ec84` | true | succeeded | true |

## Per-model execution and provenance

Every attempted model had provider/model execution started. The upstream
identities, treatment fingerprints, and bounded provider errors were:

| Model | Upstream | Treatment fingerprint | Provider error / terminal boundary |
| --- | --- | --- | --- |
| `gpt-oss:20b-cloud` | `gpt-oss:20b` | `044cbc91ef494cc3777054f3d76349fb58b97f098182ba9464694bfcfcb5d094` | `timeout`; provider protocol failure |
| `gpt-oss:120b-cloud` | `gpt-oss:120b` | `e33e43c107d529bd7da3ee76dd461f3eb4d9b9ec3e63895f97fdb7b551cb21e9` | controller completed; official semantic rejection |
| `glm-5.1:cloud` | `glm-5.1` | `26040433b1eb0569d25528b49c5e973891c431feac59cf9e3d3e5ac8bd56b46d` | authoritative resolved |
| `glm-5.2:cloud` | `glm-5.2` | `633bb6885072229b999e9dd4da7de496e6bb20cb495359a9560a637937f1025c` | authoritative resolved |
| `deepseek-v4-flash:cloud` | `deepseek-v4-flash` | `7ef36c07d3eafd8c1df52189e5d41b3aff0ace7944a3feaddc8dbf049847793f` | 40-request limit; no candidate |
| `deepseek-v4-pro:cloud` | `deepseek-v4-pro` | `6568da38887abea1d4f2ca56f8ad6cdaa4278e7ffc68147f2ebff0079c6c11b7` | `request_too_large`; no candidate |
| `kimi-k2.6:cloud` | `kimi-k2.6` | `bd7c7a5dfc13e2b5f21c0ac36a6d849fcd20957fb3e1175c97662e62a633d16d` | elapsed-time limit before controller completion |
| `minimax-m2.7:cloud` | `minimax-m2.7` | `d53f7ee81124086f73399e0b2cfa329b2bde3cadfc2762c595eb49362c17592b` | 40-request limit; five directive rejections |
| `minimax-m3:cloud` | `minimax-m3` | `551a0b4ec74878bea5eba90bede31503823b0f046871b5d05f2516fa12faecb0` | directive rejection; no PDB |
| `nemotron-3-nano:30b-cloud` | `nemotron-3-nano:30b` | `f9eeabe5761920eb818f4296c986a17e9b3a1bb384e1b69a7e942b45d3089ef3` | 40-request limit; one directive rejection |
| `nemotron-3-super:cloud` | `nemotron-3-super` | `cb84d0b4dddff972ee490cede26f5940cae21492fe6377263e741276178fc1e9` | 40-request limit before controller completion |
| `nemotron-3-ultra:cloud` | `nemotron-3-ultra` | `abe61d4214f5609f7c215464b281d88af107451d701bb6140970be8cdc399460` | directive rejection; no candidate |
| `qwen3.5:cloud` | `qwen3.5` | `e30ebe18e5a21cdc25805577fca81d482e11cb834405fcf32fbaf5402141f8d3` | 40-request limit; no candidate |
| `gemma4:31b-cloud` | `gemma4:31b` | `c16e64afaaf4fc2367be109d433808ef2611918626989a613de2be56e46b3f8c` | 40-request limit; no candidate |
| `mistral-large-3:675b-cloud` | `mistral-large-3:675b` | `0c2a27f20a2002121cb37018a39e78e0e2534ce6188e5a1d032dbdc431c91004` | 40-request limit before controller completion |

Raw/canonical SHA-256 pairs for candidates that materialized and passed the
transport boundary are retained in each run's `result.json`; official test
execution was proven for GPT-OSS 20B, GPT-OSS 120B, GLM 5.1, GLM 5.2, Kimi
K2.6, Nemotron 3 Super, and Mistral Large 3. No candidate-materialization
failure occurred and no hidden test identity or gold implementation was
inspected.

## Summary statistics

- eligible models: **15**
- attempted models / provider-model calls: **15 / 15**
- authoritative resolutions: **2**
- semantic rejections: **1**
- protocol failures: **12**
- candidate-materialization failures: **0**
- infrastructure blocks: **0**
- models reaching proven official tests: **7**
- authoritative Level-32 success rate: **2/15 (13.3%)**

Model swapping worked without implementation, prompt, profile, parser,
evaluator, or treatment-contract changes. The matrix therefore preserves one
homogeneous repaired treatment; the two GLM resolutions are independent
model treatments, not a rewrite of GLM 5.2 V11 or any historical run.
