# Deterministic selection rule

Seed: `gpt-oss-swerebench-v2-eval-20260818`

Algorithm: `sha256-seed-instance-repo-then-repo-diverse-first-seen-v1`

1. Load the B15 authority mask `validation_cd_clean_le32k_mask.json` (128 ids).
2. Join each id to the B13 validation manifest (`repo`, `repo_canonical`, `base_commit`).
3. Score `SHA-256(seed || "\n" || instance_id || "\n" || repo_canonical || "\n")`.
4. Sort by that key, then `instance_id`.
5. Walk the sorted list and emit the first occurrence of each repository.
6. Append remaining same-repository tasks in the same score order.
7. Pilot-10 is the first 10 rows of that full order.

Inputs are only admissible metadata. Gold patches, hidden tests, prior
model outcomes, and manual preference are not used.
