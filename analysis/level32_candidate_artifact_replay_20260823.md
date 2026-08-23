# Level-32 candidate-artifact replay evidence

This compact record is the durable summary for the provider-free replay of the
repaired `workspace-derived-official-git-diff-v1` candidate transport.

Frozen identities:

- task: `audreyr__cookiecutter-967` / `swr-audreyr-cookiecutter-967-pdb`
- base commit: `ba5ba8c78e97f5dc7fb4e16c588d7be037e6e5e7`
- evaluator commit: `c71902a8cf8d2b725f63d51f199f4d3e56f68d2d`
- evaluator image: `docker.io/swerebenchv2/audreyr-cookiecutter:967-ba5ba8c`
- image ID: `sha256:0bad37ac1e0a6d692a9ef417c05753b5ad45dfa8c32fd52b0f3ecabf722af8eb`
- dataset revision: `475dd5e8703bb5fb22dd3c60b5d038b019eba1e0`
- dataset parquet SHA-256: `0e0bf9355f892ad74ae98d4e1c404f39fd6654a8e351ee3e6ab162e4a64cd3ad`

Replay population was 18 retained candidates. Sixteen reached official test
execution, two failed closed during raw candidate materialization, and zero
reached authoritative 5/5 fail-to-pass plus 0 pass-to-pass failures. The
integrity gate was `PASS`; provider/model calls were `0`.

`F2P` and `P2P` are aggregate counts only. `official_test_execution_proven`
is the boundary required before interpreting an aggregate result semantically.

| Candidate treatment | F2P | P2P failed | Official execution proven | Raw patch SHA-256 | Canonical patch SHA-256 |
| --- | ---: | ---: | :---: | --- | --- |
| deepseek-v4-flash-v1 | 4/5 | 1/9 | true | `8f7f1d2fc0bce83e4aa2bc63f3b39e8d03ed8ac2fefeb066aaab7d5e380a46ec` | `af36b0037ee7f89da7b27b2118792d1f5a25cd3e072d6f047957f4205d480cea` |
| deepseek-v4-pro-v1 | 0/5 | 0/9 | true | `aeb9471fd87dd5d0916c51ec6c7a97b2f5a516f1390c4bdf92a8c8b44d1c5676` | `1739a9b8facf410fd02bfb2f8ed1d562fd381b8f953c72be0df093747cd79c29` |
| gemma4-31b-v3 | 4/5 | 1/9 | true | `9b8d17295fb601576386aa01c0c8edc9265d31968f8453fcd00337eb31ae840b` | `58843d8ca9a62622dffcfb9f992732ad997287309f2a43ec255a98f12780085c` |
| glm-5.1-v1 | 4/5 | 1/9 | true | `0f46f0159acb469590137aff1d4696ca8b3c9370a9da933cf1a114f08d23678f` | `77d407039ba1e565851fdb84519f78998ca4217ea8e9394bbe5531eb3b80883c` |
| glm-5.2-v1 | 4/5 | 0/9 | true | `439e49d99bfd81a8b33d68686e666c29edd700ce09e0218f1dba5941318ad6ac` | `6d7b2dec92e2d64d39bf1d9e40cb9864bd9303ed6fd23701e861b897a5ab8303` |
| gpt-oss-120b-v2 | 0/5 | 0/9 | true | `da0533ebaca041268ac8a09cb15f40749f7df660de8b6cc8a3711f1da4747940` | `5777c69273cdc24b6efb618884ab8cd331569c5190c66c486981b511fa128c37` |
| gpt-oss-120b-v3 | 4/5 | 0/9 | true | `60194bf653801a201b6ed644eefe36246a94b6437abc76e0c9993c3de75ad0d9` | `86c34c6160d0ae99f425263068785e4a6d82050bebfe808f4923e8ec3f1e6b46` |
| gpt-oss-v3 | 4/5 | 0/9 | true | `319c53a3414109f6000200730f0713d6eb8a5455c085482eed570068c7669a3c` | `6c3a4f349c2dfae1a92ffed5afc8c0e4869d4c9324241d861a76332440b24a4a` |
| kimi-k2.6-v2 | 4/5 | 0/9 | true | `dc3e4c9013de684ba05067c82cc483120545ede6c388e74818bfccd97c380d81` | `9e2b8d3043d7b50318ee8155bdd276a385bfa9012c94fa2f0fa73f2b51328df4` |
| kimi-k2.6-v7 | 4/5 | 0/9 | true | `bd0482c5b3eac6fe317bf77c84b3b5ca76d298a29be1e6b9c85f2dd229d84c9f` | `e981a781941d1243b157096449af6217a970674cb62689792d32f0f421acf457` |
| minimax-m2.7-v1 | 4/5 | 1/9 | true | `a1950352283256f4269dde390011b982b3cfcc0675220536004ea3fd169ad2c3` | `4140e914dc79dc0fece417dfd8844db3ff96c8958fc2276d496f117b83ddeb3d` |
| mistral tool-accepted recovery-v1 | 0/5 | 3/9 | true | `843ea99b49e98d0bf40684a18dfdf821277a76914e55627446a7367e76baf4c7` | `1b5b2a2e2aef0ceb5688b3365af33dbfcb5626d4dc3d4c77c7d964489f0a4608` |
| mistral-large-3-675b-v2 | — | — | false | `d9e097cbcf921cda68f231a4abd125731e56c02ac63a7141877c52c41df6f7bb` | — |
| nemotron-3-super-v1 | 0/5 | 0/9 | true | `eacd01597fbce4dbfb7ae187bb5b2d756c315b6971f6ebf5d4783bac971a337a` | `3dd3843a6f24e9c334ec878a02d9948a15e1d2f818e966be4972b9424d304469` |
| nemotron-3-super-v2 | — | — | false | `b07ed0beabf40e5afec8148b1c64fdf131a3f2c0546efd935261f8092399d7ae` | — |
| nemotron-3-ultra-v2 | 4/5 | 0/9 | true | `a07efb96604ba787ebaf636a1e6293bc490208ad7a283d03e8e33f10cd9fa613` | `cdb58d4489f8e5efd6dc18ee79fdfbffa386248b0e7250318285f8315f31df15` |
| qwen3.5-v1 | 0/5 | 0/9 | true | `3a1fc277426a6c30d4bbcc4cd34cd9a94909ab463c86296cca339d0b5fe53d4d` | `e6a4f87a62856b770fe1757f93b39ddd1ff00a2e865300d4c031624c26b3b3de` |
| qwen3.5-v2 | 4/5 | 1/9 | true | `645c9383aedaea1b35d3b586e078e4f94fbdf7ee4eb323b0d4c5fdc8674f7706` | `76b0e6117e29419ba6af07c51730098a47e10453da14b6972d55860a4f98f0bc` |

The two false rows failed closed before official evaluation: one had a hunk
count/body mismatch and the other lacked the final `+++` header. Historical
source directories and their original `candidate.patch`, live results, and
summaries were snapshotted and not rewritten; any recovery/replay artifacts
belong in a fresh destination directory.
