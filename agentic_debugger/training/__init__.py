"""Bounded training and evaluation utilities for the Colab patch pilot."""

from .patch_pilot import (
    CorpusBuildError,
    StrictPatchError,
    aggregate_lora_delta,
    build_corpus,
    parse_unified_diff_strict,
    record_generation_once,
    snapshot_trainable_lora_parameters,
    verify_freeze_record,
    verify_saved_raw_output,
    write_external_manifest,
)

__all__ = [
    "CorpusBuildError",
    "StrictPatchError",
    "aggregate_lora_delta",
    "build_corpus",
    "parse_unified_diff_strict",
    "record_generation_once",
    "snapshot_trainable_lora_parameters",
    "verify_freeze_record",
    "verify_saved_raw_output",
    "write_external_manifest",
]
