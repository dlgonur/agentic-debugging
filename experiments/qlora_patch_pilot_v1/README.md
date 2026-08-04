# QLoRA Python Patch Pilot v1

This directory contains only repository-trackable reproducibility assets for the bounded Colab pilot. Dataset contents, model caches, adapters, generations, patches, raw verifier records, and runtime logs must remain under the ignored `outputs/qlora_patch_pilot_v1/` tree or an equivalent Google Drive directory.

## Frozen experiment

- Base and tuned condition: `Qwen/Qwen2.5-Coder-7B-Instruct` at `c03e6d358207e414f1eca0bb1891e29f1db0e242`.
- Training source: `bigcode/commitpackft`, Python data at `fc56fe33c030c6daa414c2b112c932b8eed085e6`.
- Repository baseline: base commit `66fb5d5` on branch `experiment/qlora-patch-pilot-v1`; the live execution HEAD must descend from it (`required_ancestor`). The branch name is operational metadata, not a scientific identity, and the record intentionally does not contain the candidate's own future commit.
- Final training and held-out generation are disabled until FirstMate approves the freeze and smoke evidence.

## Tracked assets

- `freeze_record.json` — model, dataset, held-out hashes, configuration identities, and scientific gates.
- `prompt_contract.json` — identical base/tuned request contract.
- `transformation_config.json` — deterministic filtering, splitting, deduplication, and audit rules.
- `training_config.json` — QLoRA configuration.
- `generation_config.json` — deterministic one-candidate decoding.
- `external_artifact_manifest.template.json` — metadata contract for ignored artifacts.
- `colab/agentic_debugging_qlora_pilot.ipynb` — preparation and smoke notebook; final cells are gated.

## Commands

```bash
python scripts/qlora_patch_pilot.py verify-freeze \
  --repository-root . \
  --freeze-record experiments/qlora_patch_pilot_v1/freeze_record.json

python scripts/qlora_patch_pilot.py build-corpus \
  --repository-root . \
  --input-jsonl /content/drive/MyDrive/agentic-debugging/commitpackft-python.jsonl \
  --output-dir /content/drive/MyDrive/agentic-debugging/qlora_patch_pilot_v1/corpus \
  --freeze-record experiments/qlora_patch_pilot_v1/freeze_record.json \
  --transformation-config experiments/qlora_patch_pilot_v1/transformation_config.json \
  --prompt-contract experiments/qlora_patch_pilot_v1/prompt_contract.json

python scripts/qlora_patch_pilot.py validate-audits \
  --output-dir /content/drive/MyDrive/agentic-debugging/qlora_patch_pilot_v1/corpus \
  --transformation-config experiments/qlora_patch_pilot_v1/transformation_config.json

python scripts/qlora_patch_pilot.py verifier-smoke \
  --repository-root . \
  --output outputs/qlora_patch_pilot_v1/smoke/verifier_smoke.json
```

## Corpus gate

Preferred: 1,500 train and 200 validation examples. Accepted minimum: 1,000 train and 150 validation. Filters are never weakened to reach a count. The generated audit forms must contain at least 50 completed accepted-example reviews and 25 completed rejected-example reviews before training.

## Final evaluation gate

The notebook creates one base output and one tuned output per held-out task with the same prompt and generation configuration. It forbids regeneration after outcomes are observed. A verifier-only rerun of a saved patch remains permitted.
