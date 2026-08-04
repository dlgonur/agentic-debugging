"""Operator CLI for the bounded QLoRA patch-pilot preparation and smoke path."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agentic_debugger.training.patch_pilot import (
    build_corpus,
    create_non_held_out_verifier_smoke,
    iter_jsonl,
    parse_unified_diff_strict,
    validate_completed_audits,
    verify_freeze_record,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    corpus = sub.add_parser("build-corpus")
    corpus.add_argument("--repository-root", required=True)
    corpus.add_argument("--input-jsonl", required=True)
    corpus.add_argument("--output-dir", required=True)
    corpus.add_argument("--freeze-record", required=True)
    corpus.add_argument("--transformation-config", required=True)
    corpus.add_argument("--prompt-contract", required=True)
    audit = sub.add_parser("validate-audits")
    audit.add_argument("--output-dir", required=True)
    audit.add_argument("--transformation-config", required=True)
    parse = sub.add_parser("parse-patch")
    parse.add_argument("--patch", required=True)
    parse.add_argument("--allowed-path", action="append", required=True)
    smoke = sub.add_parser("verifier-smoke")
    smoke.add_argument("--repository-root", required=True)
    smoke.add_argument("--output", required=True)
    freeze = sub.add_parser("verify-freeze")
    freeze.add_argument("--repository-root", required=True)
    freeze.add_argument("--freeze-record", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "build-corpus":
        result = build_corpus(
            iter_jsonl(args.input_jsonl), repository_root=args.repository_root,
            output_dir=args.output_dir, freeze_record_path=args.freeze_record,
            transformation_config_path=args.transformation_config,
            prompt_contract_path=args.prompt_contract,
        )
    elif args.command == "validate-audits":
        result = validate_completed_audits(args.output_dir, args.transformation_config)
    elif args.command == "parse-patch":
        patch = Path(args.patch).read_text(encoding="utf-8")
        result = {"valid": True, "normalized_patch": parse_unified_diff_strict(patch, args.allowed_path)}
    elif args.command == "verifier-smoke":
        result = create_non_held_out_verifier_smoke(args.repository_root, args.output)
    else:
        result = verify_freeze_record(args.repository_root, args.freeze_record)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
