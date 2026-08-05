"""Deterministic data and smoke utilities for the QLoRA patch pilot.

The module intentionally does not import model-training libraries.  It creates
small, reviewable records and writes dataset contents only to an operator-owned
external output directory.  The existing evaluation verifier remains the sole
correctness authority for generated patches.
"""

from __future__ import annotations

import ast
import csv
import datetime
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping, Sequence

from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.evaluation.verifier import EvaluationVerifier

REQUIRED_FIELDS = {
    "commit",
    "old_file",
    "new_file",
    "old_contents",
    "new_contents",
    "subject",
    "message",
    "lang",
    "license",
    "repos",
}
WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[^\s]", re.UNICODE)
DIFF_HEADER_RE = re.compile(r"^(---|\+\+\+) (?:[ab]/)?(.+)$")
AUDIT_MODE_KEY = "audit_mode"
AUDIT_MODE_HUMAN_MANUAL = "human_manual"
AUDIT_MODE_INDEPENDENT_AI = "independent_ai"
AUDIT_MODES = (AUDIT_MODE_HUMAN_MANUAL, AUDIT_MODE_INDEPENDENT_AI)
MANUAL_AUDIT_FIELDS = ("manual_verdict", "manual_reason", "reviewer", "reviewed_at")
MANUAL_VERDICT_ACCEPTED = "ACCEPT"
MANUAL_VERDICT_REJECTED = "REJECT"
INDEPENDENT_AUDIT_FIELDS = (
    "audit_verdict",
    "audit_reason",
    "audit_reviewer",
    "audit_reviewer_type",
    "audit_reviewed_at",
)
INDEPENDENT_REVIEWER_TYPE = "independent_ai_reviewer"
INDEPENDENT_AUDIT_METHOD = "owner-delegated independent FirstMate audit"
FORBIDDEN_INDEPENDENT_REVIEWERS = ("agentic-coding-agent",)
HUMAN_FIELDS = ("human_verdict", "human_reason", "human_reviewer", "human_reviewed_at")
CANONICAL_AUDIT_FIELDS = (
    "global_index",
    "packet",
    "audit_index",
    "audit_method",
    "audit_verdict",
    "audit_reviewer",
    "audit_reviewer_type",
    "audit_reviewed_at",
)
INDEPENDENT_AUDIT_METHODOLOGY = "Owner-delegated independent FirstMate AI audit; not human review."


class CorpusBuildError(ValueError):
    """Raised when the frozen corpus contract cannot be satisfied."""


class StrictPatchError(ValueError):
    """Raised when model output is not exactly one authorized unified diff."""


@dataclass(frozen=True)
class Candidate:
    example_id: str
    repository: str
    commit: str
    file_path: str
    license: str
    subject: str
    old_source: str
    new_source: str
    patch: str
    prompt: list[dict[str, str]]
    completion: str
    exact_hash: str
    simhash: int

    def training_mapping(self, *, dataset_revision: str, transformation_version: str) -> dict[str, Any]:
        return {
            "schema_version": "patch-sft-v1",
            "example_id": self.example_id,
            "provenance": {
                "dataset": "bigcode/commitpackft",
                "dataset_revision": dataset_revision,
                "repository": self.repository,
                "commit": self.commit,
                "source_license": self.license,
                "file_path": self.file_path,
                "transformation_version": transformation_version,
            },
            "prompt": self.prompt,
            "completion": self.completion,
        }

    def audit_mapping(self) -> dict[str, Any]:
        return {
            "example_id": self.example_id,
            "repository": self.repository,
            "commit": self.commit,
            "file_path": self.file_path,
            "license": self.license,
            "subject": self.subject,
            "old_chars": len(self.old_source),
            "new_chars": len(self.new_source),
            "patch_chars": len(self.patch),
            "patch_sha256": sha256_text(self.patch),
        }

    def review_mapping(self) -> dict[str, Any]:
        return {
            **self.audit_mapping(),
            "old_contents": self.old_source,
            "new_contents": self.new_source,
            "patch": self.patch,
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CorpusBuildError(f"JSON root must be an object: {path}")
    return value


def iter_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CorpusBuildError(f"invalid JSONL at line {line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise CorpusBuildError(f"JSONL line {line_number} is not an object")
            yield value


def _normalized_source(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _normalized_subject(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _single_repository(value: str) -> str | None:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) != 1 or "/" not in parts[0]:
        return None
    return parts[0]


def _safe_python_path(path: str, excluded_segments: set[str]) -> bool:
    normalized = path.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if normalized.startswith("/") or ".." in pure.parts or pure.suffix.lower() != ".py":
        return False
    lowered = {part.lower() for part in pure.parts}
    if lowered & excluded_segments:
        return False
    name = pure.name.lower()
    return not (name.startswith("test_") or name.endswith("_test.py") or name.startswith("conftest"))


def _syntax_valid(source: str, path: str) -> bool:
    try:
        ast.parse(source, filename=path)
    except (SyntaxError, ValueError, TypeError):
        return False
    return True


def render_unified_diff(old_source: str, new_source: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old_source.splitlines(keepends=True),
            new_source.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="\n",
        )
    )


def _diff_stats(patch: str) -> tuple[int, int]:
    changed = 0
    hunks = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            hunks += 1
        elif (line.startswith("+") and not line.startswith("+++")) or (
            line.startswith("-") and not line.startswith("---")
        ):
            changed += 1
    return changed, hunks


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in WORD_RE.findall(text)]


def _ngrams(text: str, n: int) -> set[str]:
    tokens = _tokens(text)
    if len(tokens) < n:
        return {"\u241f".join(tokens)} if tokens else set()
    return {"\u241f".join(tokens[index : index + n]) for index in range(len(tokens) - n + 1)}


def simhash64(text: str, ngram: int = 5) -> int:
    features = sorted(_ngrams(text, ngram))
    if not features:
        return 0
    vector = [0] * 64
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(64):
            vector[bit] += 1 if value & (1 << bit) else -1
    result = 0
    for bit, score in enumerate(vector):
        if score >= 0:
            result |= 1 << bit
    return result


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return 1.0 if not union else len(left & right) / len(union)


def _prompt(system_prompt: str, user_template: str, subject: str, path: str, old_source: str) -> list[dict[str, str]]:
    user = user_template.format(
        title=subject,
        description=subject,
        allowed_write_paths=f"- {path}",
        reproduction_command="not available for source commit",
        bounded_failure="not available",
        file_path=path,
        buggy_source=old_source.rstrip("\n"),
    )
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}]


def _reject(reason: str, row_index: int, row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "row_index": row_index,
        "reason": reason,
        "commit": str(row.get("commit", ""))[:80],
        "repos": str(row.get("repos", ""))[:200],
        "file_path": str(row.get("old_file", ""))[:300],
        "license": str(row.get("license", ""))[:80],
        "subject": str(row.get("subject", ""))[:500],
        "old_contents": str(row.get("old_contents", "")),
        "new_contents": str(row.get("new_contents", "")),
    }


def filter_row(
    row: Mapping[str, Any],
    *,
    row_index: int,
    config: Mapping[str, Any],
    prompt_contract: Mapping[str, Any],
    held_out_fingerprints: Mapping[str, Any],
) -> tuple[Candidate | None, dict[str, Any] | None]:
    missing = REQUIRED_FIELDS - set(row)
    if missing:
        return None, _reject("missing_fields", row_index, row)
    if not all(isinstance(row[field], str) for field in REQUIRED_FIELDS):
        return None, _reject("non_string_field", row_index, row)
    if row["lang"].strip().lower() != "python":
        return None, _reject("not_python", row_index, row)
    license_value = row["license"].strip().lower()
    if license_value not in set(config["source_license_allowlist"]):
        return None, _reject("license_not_allowed", row_index, row)
    repository = _single_repository(row["repos"])
    if repository is None:
        return None, _reject("repository_not_single", row_index, row)
    excluded_repositories = {item.lower() for item in held_out_fingerprints.get("excluded_repositories", [])}
    if repository.lower() in excluded_repositories:
        return None, _reject("held_out_repository", row_index, row)
    old_path = row["old_file"].strip()
    new_path = row["new_file"].strip()
    if old_path != new_path:
        return None, _reject("path_changed", row_index, row)
    excluded_segments = set(config["path_rules"]["excluded_segments"])
    if not _safe_python_path(old_path, excluded_segments):
        return None, _reject("path_not_allowed", row_index, row)
    old_source = _normalized_source(row["old_contents"])
    new_source = _normalized_source(row["new_contents"])
    limits = config["content_limits"]
    if not (limits["min_chars"] <= len(old_source) <= limits["max_old_chars"]):
        return None, _reject("old_source_size", row_index, row)
    if not (limits["min_chars"] <= len(new_source) <= limits["max_new_chars"]):
        return None, _reject("new_source_size", row_index, row)
    if old_source == new_source:
        return None, _reject("no_change", row_index, row)
    if not _syntax_valid(old_source, old_path):
        return None, _reject("old_source_syntax", row_index, row)
    if not _syntax_valid(new_source, new_path):
        return None, _reject("new_source_syntax", row_index, row)
    if not old_source.endswith("\n") or not new_source.endswith("\n"):
        return None, _reject("missing_terminal_newline", row_index, row)
    subject = " ".join(row["subject"].split())
    normalized_subject = _normalized_subject(subject)
    if any(term in normalized_subject for term in config["reject_subject_terms"]):
        return None, _reject("subject_rejected", row_index, row)
    if not any(term in normalized_subject for term in config["fix_intent_terms"]):
        return None, _reject("no_fix_intent", row_index, row)
    patch = render_unified_diff(old_source, new_source, old_path)
    if not patch:
        return None, _reject("empty_diff", row_index, row)
    changed_lines, hunks = _diff_stats(patch)
    if len(patch) > limits["max_diff_chars"]:
        return None, _reject("diff_size", row_index, row)
    if changed_lines > limits["max_changed_lines"]:
        return None, _reject("changed_lines", row_index, row)
    if hunks > limits["max_hunks"]:
        return None, _reject("too_many_hunks", row_index, row)
    prompt = _prompt(prompt_contract["system_prompt"], prompt_contract["user_template"], subject, old_path, old_source)
    if len(json.dumps(prompt, ensure_ascii=False)) + len(patch) > limits["max_prompt_plus_completion_chars"]:
        return None, _reject("example_size", row_index, row)
    old_hash = sha256_text(old_source)
    new_hash = sha256_text(new_source)
    patch_hash = sha256_text(patch)
    if old_hash in set(held_out_fingerprints.get("source_hashes", [])):
        return None, _reject("held_out_exact_old", row_index, row)
    if new_hash in set(held_out_fingerprints.get("source_hashes", [])):
        return None, _reject("held_out_exact_new", row_index, row)
    if patch_hash in set(held_out_fingerprints.get("patch_hashes", [])):
        return None, _reject("held_out_exact_patch", row_index, row)
    held_ngram_sets = [set(items) for items in held_out_fingerprints.get("source_ngrams", [])]
    threshold = float(config["near_duplicate"]["held_out_jaccard_threshold"])
    source_ngrams = _ngrams(old_source + "\n" + new_source, int(config["near_duplicate"]["token_ngram"]))
    if any(jaccard(source_ngrams, target) >= threshold for target in held_ngram_sets):
        return None, _reject("held_out_near_duplicate", row_index, row)
    exact_hash = sha256_text("\0".join((repository, old_path, old_source, new_source, patch)))
    example_id = f"commitpackft:{repository}:{row['commit']}:{old_path}"
    return Candidate(
        example_id=example_id,
        repository=repository,
        commit=row["commit"].strip(),
        file_path=old_path,
        license=license_value,
        subject=subject,
        old_source=old_source,
        new_source=new_source,
        patch=patch,
        prompt=prompt,
        completion=patch,
        exact_hash=exact_hash,
        simhash=simhash64(old_source + "\n" + new_source, int(config["near_duplicate"]["token_ngram"])),
    ), None


def build_held_out_fingerprints(repository_root: str | Path, freeze_record: Mapping[str, Any], *, include_private_content: bool = True) -> dict[str, Any]:
    root = Path(repository_root)
    source_hashes: set[str] = set()
    patch_hashes: set[str] = set()
    source_ngrams: list[list[str]] = []
    if include_private_content:
        from agentic_debugger.demo.catalog import build_reference_patch, scenario_for
        for record in freeze_record["held_out_tasks"]:
            fixture = root / record["fixture_path"]
            scenario = scenario_for(record["task_id"])
            target = fixture / scenario.reference_repair.target_path
            buggy = target.read_text(encoding="utf-8")
            corrected = buggy.replace(scenario.reference_repair.old_snippet, scenario.reference_repair.new_snippet, 1)
            patch = build_reference_patch(buggy, scenario.reference_repair)
            if sha256_text(buggy) != record["buggy_target_sha256"]:
                raise CorpusBuildError(f"held-out buggy hash drift: {record['task_id']}")
            if sha256_text(corrected) != record["corrected_target_sha256"]:
                raise CorpusBuildError(f"held-out corrected hash drift: {record['task_id']}")
            if sha256_text(patch) != record["gold_patch_sha256"]:
                raise CorpusBuildError(f"held-out patch hash drift: {record['task_id']}")
            source_hashes.update((sha256_text(buggy), sha256_text(corrected)))
            patch_hashes.add(sha256_text(patch))
            source_ngrams.append(sorted(_ngrams(buggy + "\n" + corrected, 5)))
    return {
        "source_hashes": sorted(source_hashes),
        "patch_hashes": sorted(patch_hashes),
        "source_ngrams": source_ngrams,
        "excluded_repositories": [
            "bigcode-project/quixbugs",
            "jkoppel/quixbugs",
            "soarsmu/bugsinpy",
            "princeton-nlp/swe-bench",
        ],
    }


def _near_duplicate_filter(candidates: list[Candidate], max_hamming: int) -> tuple[list[Candidate], list[dict[str, Any]]]:
    accepted: list[Candidate] = []
    rejected: list[dict[str, Any]] = []
    buckets: dict[tuple[int, int], list[Candidate]] = defaultdict(list)
    for candidate in sorted(candidates, key=lambda item: item.exact_hash):
        duplicate_of: Candidate | None = None
        for band in range(4):
            key = (band, (candidate.simhash >> (band * 16)) & 0xFFFF)
            for existing in buckets[key]:
                if hamming_distance(candidate.simhash, existing.simhash) <= max_hamming:
                    duplicate_of = existing
                    break
            if duplicate_of is not None:
                break
        if duplicate_of is not None:
            rejected.append({**candidate.review_mapping(), "reason": "near_duplicate", "duplicate_of": duplicate_of.example_id})
            continue
        accepted.append(candidate)
        for band in range(4):
            buckets[(band, (candidate.simhash >> (band * 16)) & 0xFFFF)].append(candidate)
    return accepted, rejected


def _stable_key(candidate: Candidate, seed: int) -> str:
    return sha256_text(f"{seed}\0{candidate.repository}\0{candidate.commit}\0{candidate.file_path}")


def _select_repository_unique(candidates: list[Candidate], seed: int) -> tuple[list[Candidate], list[dict[str, Any]]]:
    by_repo: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_repo[candidate.repository].append(candidate)
    selected: list[Candidate] = []
    rejected: list[dict[str, Any]] = []
    for repository in sorted(by_repo):
        ordered = sorted(by_repo[repository], key=lambda item: _stable_key(item, seed))
        selected.append(ordered[0])
        for item in ordered[1:]:
            rejected.append({**item.review_mapping(), "reason": "repository_already_selected", "selected_example": ordered[0].example_id})
    return selected, rejected


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _write_audit_csv(path: Path, rows: Sequence[Mapping[str, Any]], *, accepted: bool) -> None:
    fieldnames = [
        "audit_index", "example_id", "repository", "commit", "file_path", "license", "subject",
        "reason", "old_chars", "new_chars", "patch_chars", "patch_sha256",
        "manual_verdict", "manual_reason", "reviewer", "reviewed_at",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for index, row in enumerate(rows, 1):
            value = dict(row)
            value["audit_index"] = index
            value.setdefault("reason", "accepted" if accepted else "")
            value.update({"manual_verdict": "", "manual_reason": "", "reviewer": "", "reviewed_at": ""})
            writer.writerow(value)


def _sample_rows(rows: Sequence[Mapping[str, Any]], count: int, seed: int, label: str) -> list[Mapping[str, Any]]:
    ordered = sorted(rows, key=lambda row: sha256_text(f"{seed}\0{label}\0{json.dumps(row, sort_keys=True, ensure_ascii=False)}"))
    return ordered[:count]


def build_corpus(
    rows: Iterable[Mapping[str, Any]],
    *,
    repository_root: str | Path,
    output_dir: str | Path,
    freeze_record_path: str | Path,
    transformation_config_path: str | Path,
    prompt_contract_path: str | Path,
) -> dict[str, Any]:
    output = Path(output_dir)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise CorpusBuildError(f"corpus output directory must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    freeze = load_json(freeze_record_path)
    config = load_json(transformation_config_path)
    prompt_contract = load_json(prompt_contract_path)
    held = build_held_out_fingerprints(repository_root, freeze)
    candidates: list[Candidate] = []
    rejected: list[dict[str, Any]] = []
    seen_exact: set[str] = set()
    input_count = 0
    for row_index, row in enumerate(rows):
        input_count += 1
        candidate, rejection = filter_row(
            row, row_index=row_index, config=config, prompt_contract=prompt_contract, held_out_fingerprints=held
        )
        if rejection is not None:
            rejected.append(rejection)
            continue
        assert candidate is not None
        if candidate.exact_hash in seen_exact:
            rejected.append({**candidate.review_mapping(), "reason": "exact_duplicate"})
            continue
        seen_exact.add(candidate.exact_hash)
        candidates.append(candidate)
    candidates, near_rejected = _near_duplicate_filter(candidates, int(config["near_duplicate"]["max_hamming_distance"]))
    rejected.extend(near_rejected)
    candidates, repository_rejected = _select_repository_unique(candidates, int(config["seed"]))
    rejected.extend(repository_rejected)
    candidates = sorted(candidates, key=lambda item: _stable_key(item, int(config["seed"])))
    preferred = config["preferred_counts"]
    minimum = config["minimum_counts"]
    preferred_total = preferred["train"] + preferred["validation"]
    minimum_total = minimum["train"] + minimum["validation"]
    if len(candidates) >= preferred_total:
        train_count, validation_count, corpus_tier = preferred["train"], preferred["validation"], "preferred"
    elif len(candidates) >= minimum_total:
        train_count, validation_count, corpus_tier = minimum["train"], minimum["validation"], "minimum"
    else:
        raise CorpusBuildError(
            f"filter quality retained {len(candidates)} repository-unique examples; "
            f"at least {minimum_total} are required without weakening filters"
        )
    selected = candidates[: train_count + validation_count]
    validation = selected[:validation_count]
    train = selected[validation_count:]
    if {item.repository for item in train} & {item.repository for item in validation}:
        raise CorpusBuildError("repository overlap between train and validation")
    dataset_revision = freeze["dataset"]["revision"]
    transformation_version = config["version"]
    train_rows = [item.training_mapping(dataset_revision=dataset_revision, transformation_version=transformation_version) for item in train]
    validation_rows = [item.training_mapping(dataset_revision=dataset_revision, transformation_version=transformation_version) for item in validation]
    _write_jsonl(output / "train.jsonl", train_rows)
    _write_jsonl(output / "validation.jsonl", validation_rows)
    _write_jsonl(output / "accepted_rows.jsonl", [item.review_mapping() for item in selected])
    _write_jsonl(output / "rejected_rows.jsonl", rejected)
    rejection_counts = dict(sorted(Counter(str(item.get("reason", "unknown")) for item in rejected).items()))
    _write_json(output / "rejection_summary.json", {"input_rows": input_count, "rejected_rows": len(rejected), "reasons": rejection_counts})
    dedup_report = {
        "exact_unique_before_near_dedup": len(seen_exact),
        "near_duplicate_rejections": len(near_rejected),
        "repository_duplicate_rejections": len(repository_rejected),
        "selected_repository_count": len(selected),
        "train_repository_count": len({item.repository for item in train}),
        "validation_repository_count": len({item.repository for item in validation}),
        "repository_overlap": [],
        "held_out_exact_matches_accepted": 0,
        "held_out_near_matches_accepted": 0,
    }
    _write_json(output / "dedup_report.json", dedup_report)
    accepted_audit = _sample_rows([item.review_mapping() for item in selected], int(config["audit"]["accepted_minimum"]), int(config["seed"]), "accepted")
    rejected_audit = _sample_rows(rejected, int(config["audit"]["rejected_minimum"]), int(config["seed"]), "rejected")
    _write_jsonl(output / "accepted_audit_sample.jsonl", accepted_audit)
    _write_jsonl(output / "rejected_audit_sample.jsonl", rejected_audit)
    _write_audit_csv(output / "accepted_audit.csv", accepted_audit, accepted=True)
    _write_audit_csv(output / "rejected_audit.csv", rejected_audit, accepted=False)
    pending_status = (
        "PENDING_INDEPENDENT_AUDIT" if _audit_mode(config) == AUDIT_MODE_INDEPENDENT_AI else "PENDING_MANUAL_REVIEW"
    )
    _write_json(output / "audit_summary.json", {
        "audit_mode": _audit_mode(config),
        "accepted_required": config["audit"]["accepted_minimum"],
        "accepted_sampled": len(accepted_audit),
        "accepted_completed": 0,
        "rejected_required": config["audit"]["rejected_minimum"],
        "rejected_sampled": len(rejected_audit),
        "rejected_completed": 0,
        "status": pending_status,
    })
    summary = {
        "schema_version": "corpus-build-summary-v1",
        "corpus_tier": corpus_tier,
        "input_rows": input_count,
        "filtered_candidates": len(candidates),
        "train_examples": len(train_rows),
        "validation_examples": len(validation_rows),
        "train_repositories": len({item.repository for item in train}),
        "validation_repositories": len({item.repository for item in validation}),
        "rejection_summary_path": "rejection_summary.json",
        "dedup_report_path": "dedup_report.json",
        "audit_status": pending_status,
    }
    _write_json(output / "corpus_summary.json", summary)
    write_external_manifest(output, configuration_identity=freeze["transformation"]["sha256"], provenance_identity=f"bigcode/commitpackft@{dataset_revision}", artifact_kind_prefix="corpus")
    return summary


def _audit_mode(config: Mapping[str, Any]) -> str:
    mode = config.get("audit", {}).get(AUDIT_MODE_KEY, AUDIT_MODE_HUMAN_MANUAL)
    if mode not in AUDIT_MODES:
        raise CorpusBuildError(f"unknown audit_mode {mode!r}; supported: {AUDIT_MODES}")
    return mode


def _load_audit_csv(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise CorpusBuildError(f"audit file missing: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _verify_audit_identities(output: Path, rows: Sequence[Mapping[str, Any]], config: Mapping[str, Any]) -> None:
    """Bind the completed independent audit rows to the frozen packet samples."""
    accepted_minimum = int(config["audit"]["accepted_minimum"])
    rejected_minimum = int(config["audit"]["rejected_minimum"])
    if len(rows) != accepted_minimum + rejected_minimum:
        raise CorpusBuildError(
            f"independent audit has {len(rows)} rows; required {accepted_minimum + rejected_minimum}"
        )
    accepted_samples = _load_jsonl_rows(output / "accepted_audit_sample.jsonl")
    rejected_samples = _load_jsonl_rows(output / "rejected_audit_sample.jsonl")
    if len(accepted_samples) != accepted_minimum or len(rejected_samples) != rejected_minimum:
        raise CorpusBuildError("frozen audit sample packets do not match the configured minimum counts")
    for index, (row, sample) in enumerate(zip(rows[:accepted_minimum], accepted_samples), 1):
        if _exact_index(row, "global_index", index) or _exact_index(row, "audit_index", index):
            raise CorpusBuildError(
                f"independent audit accepted row {index}: packet index fields drift from frozen order"
            )
        if str(row.get("packet", "")).strip() != "accepted":
            raise CorpusBuildError(f"independent audit accepted row {index}: packet label is not 'accepted'")
        for key in ("example_id", "repository", "commit", "file_path", "license", "subject"):
            if str(row.get(key, "")) != str(sample.get(key, "")):
                raise CorpusBuildError(
                    f"independent audit accepted row {index}: {key} identity drift from frozen packet"
                )
    for index, (row, sample) in enumerate(zip(rows[accepted_minimum:], rejected_samples), 1):
        global_index = accepted_minimum + index
        if _exact_index(row, "global_index", global_index) or _exact_index(row, "audit_index", index):
            raise CorpusBuildError(
                f"independent audit rejected row {index}: packet index fields drift from frozen order"
            )
        if str(row.get("packet", "")).strip() != "rejected":
            raise CorpusBuildError(f"independent audit rejected row {index}: packet label is not 'rejected'")
        for key in ("commit", "file_path", "license", "subject"):
            if str(row.get(key, "")) != str(sample.get(key, "")):
                raise CorpusBuildError(
                    f"independent audit rejected row {index}: {key} identity drift from frozen packet"
                )
        if str(row.get("frozen_reason", "")) != str(sample.get("reason", "")):
            raise CorpusBuildError(
                f"independent audit rejected row {index}: frozen reason drift from packet"
            )


def _exact_index(row: Mapping[str, Any], field: str, expected: int) -> bool:
    value = str(row.get(field, "")).strip()
    try:
        return int(value) != expected
    except ValueError:
        return True


def _load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise CorpusBuildError(f"audit sample file missing: {path}")
    return [value for value in iter_jsonl(path)]


def _validate_independent_audit(
    output: Path, config: Mapping[str, Any], completed_audit_path: str | Path | None
) -> dict[str, Any]:
    if completed_audit_path is None:
        raise CorpusBuildError(
            "independent_ai audit mode requires the completed independent audit CSV "
            "(--completed-audit); the corpus packet CSVs are not authoritative in this mode"
        )
    rows = _load_audit_csv(Path(completed_audit_path))
    _verify_audit_identities(output, rows, config)
    problems: list[str] = []
    for index, row in enumerate(rows, 1):
        for field in CANONICAL_AUDIT_FIELDS:
            value = str(row.get(field, ""))
            if value != value.strip() or not value.strip():
                problems.append(
                    f"row {index}: field {field} is missing or not canonical (leading/trailing "
                    "whitespace is rejected)"
                )
        missing = [field for field in INDEPENDENT_AUDIT_FIELDS if not str(row.get(field, "")).strip()]
        if missing:
            problems.append(f"row {index}: missing independent audit field(s) {missing}")
            continue
        if row["audit_method"].strip() != INDEPENDENT_AUDIT_METHOD:
            problems.append(
                f"row {index}: audit_method {row['audit_method'].strip()!r} "
                f"does not equal {INDEPENDENT_AUDIT_METHOD!r}"
            )
        if row["audit_verdict"].strip() not in (MANUAL_VERDICT_ACCEPTED, MANUAL_VERDICT_REJECTED):
            problems.append(f"row {index}: audit_verdict {row['audit_verdict'].strip()!r} is not ACCEPT or REJECT")
        if row["audit_reviewer_type"].strip() != INDEPENDENT_REVIEWER_TYPE:
            problems.append(
                f"row {index}: audit_reviewer_type {row['audit_reviewer_type'].strip()!r} "
                f"does not equal {INDEPENDENT_REVIEWER_TYPE!r}"
            )
        if any(
            token in row["audit_reviewer"].strip().lower()
            for token in FORBIDDEN_INDEPENDENT_REVIEWERS
        ):
            problems.append(f"row {index}: audit_reviewer is not an independent reviewer")
        for field in HUMAN_FIELDS:
            if str(row.get(field, "")).strip():
                problems.append(
                    f"row {index}: human field {field} is populated; audit_* fields are never "
                    "translated into human_* fields"
                )
    if problems:
        raise CorpusBuildError("independent audit validation failed: " + "; ".join(problems))
    accepted_rows = rows[: int(config["audit"]["accepted_minimum"])]
    rejected_rows = rows[int(config["audit"]["accepted_minimum"]):]
    counts = {
        "audit_mode": AUDIT_MODE_INDEPENDENT_AI,
        "methodology": INDEPENDENT_AUDIT_METHODOLOGY,
        "accepted_packet_total": len(accepted_rows),
        "accepted_packet_accept": sum(r["audit_verdict"] == MANUAL_VERDICT_ACCEPTED for r in accepted_rows),
        "accepted_packet_reject": sum(r["audit_verdict"] == MANUAL_VERDICT_REJECTED for r in accepted_rows),
        "rejected_packet_total": len(rejected_rows),
        "rejected_packet_accept": sum(r["audit_verdict"] == MANUAL_VERDICT_ACCEPTED for r in rejected_rows),
        "rejected_packet_reject": sum(r["audit_verdict"] == MANUAL_VERDICT_REJECTED for r in rejected_rows),
        "reviewer_identities": sorted({r["audit_reviewer"] for r in rows}),
        "reviewer_types": sorted({r["audit_reviewer_type"] for r in rows}),
        "completed_audit_path": str(completed_audit_path),
        "status": "COMPLETE",
    }
    _write_json(output / "audit_summary.json", counts)
    write_external_manifest(
        output,
        configuration_identity=sha256_bytes(canonical_json_bytes(config)),
        provenance_identity="bigcode/commitpackft@frozen-revision",
        artifact_kind_prefix="corpus-audit",
    )
    return counts


def validate_completed_audits(
    output_dir: str | Path,
    transformation_config_path: str | Path,
    completed_audit_path: str | Path | None = None,
) -> dict[str, Any]:
    output = Path(output_dir)
    config = load_json(transformation_config_path)
    mode = _audit_mode(config)
    if mode == AUDIT_MODE_INDEPENDENT_AI:
        return _validate_independent_audit(output, config, completed_audit_path)
    counts: dict[str, int] = {}
    problems: list[str] = []
    for label, filename in (("accepted", "accepted_audit.csv"), ("rejected", "rejected_audit.csv")):
        path = output / filename
        if not path.is_file():
            raise CorpusBuildError(f"audit file missing: {path}")
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        expected_verdict = MANUAL_VERDICT_ACCEPTED if label == "accepted" else MANUAL_VERDICT_REJECTED
        completed = 0
        for index, row in enumerate(rows, 1):
            missing = [field for field in MANUAL_AUDIT_FIELDS if not str(row.get(field, "")).strip()]
            if missing:
                problems.append(f"{filename} row {index}: missing manual audit field(s) {missing}")
                continue
            if row["manual_verdict"].strip() != expected_verdict:
                problems.append(
                    f"{filename} row {index}: manual_verdict {row['manual_verdict'].strip()!r} "
                    f"does not equal the required {expected_verdict!r}"
                )
                continue
            completed += 1
        required = int(config["audit"][f"{label}_minimum"])
        if completed < required:
            problems.append(f"{label} audit has {completed} completed rows; {required} required")
        counts[f"{label}_completed"] = completed
    if problems:
        raise CorpusBuildError("audit validation failed: " + "; ".join(problems))
    result = {**counts, "status": "COMPLETE"}
    _write_json(output / "audit_summary.json", result)
    write_external_manifest(output, configuration_identity=sha256_bytes(canonical_json_bytes(config)), provenance_identity="bigcode/commitpackft@frozen-revision", artifact_kind_prefix="corpus-audit")
    return result


def parse_unified_diff_strict(text: str, allowed_paths: Sequence[str]) -> str:
    if not isinstance(text, str) or not text:
        raise StrictPatchError("model output must be a non-empty string")
    if text.startswith("```") or "```" in text:
        raise StrictPatchError("Markdown fences are prohibited")
    if not text.startswith("--- "):
        raise StrictPatchError("output contains prose or lacks unified-diff header")
    lines = text.splitlines()
    old_headers = [line for line in lines if line.startswith("--- ")]
    new_headers = [line for line in lines if line.startswith("+++ ")]
    if len(old_headers) != 1 or len(new_headers) != 1:
        raise StrictPatchError("exactly one file diff is required")
    if not any(line.startswith("@@") for line in lines):
        raise StrictPatchError("diff must contain at least one hunk")
    paths: list[str] = []
    for header in (old_headers[0], new_headers[0]):
        match = DIFF_HEADER_RE.fullmatch(header)
        if match is None:
            raise StrictPatchError("malformed diff header")
        path = match.group(2).replace("\\", "/")
        if path == "/dev/null" or path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise StrictPatchError("absolute, deletion, or traversing paths are prohibited")
        paths.append(path)
    if paths[0] != paths[1]:
        raise StrictPatchError("renames are prohibited")
    normalized_allowed = {path.replace("\\", "/") for path in allowed_paths}
    if paths[0] not in normalized_allowed:
        raise StrictPatchError(f"path is not authorized: {paths[0]}")
    return text if text.endswith("\n") else text + "\n"


def write_external_manifest(
    output_dir: str | Path,
    *,
    configuration_identity: str,
    provenance_identity: str = "local-non-held-out-smoke",
    artifact_kind_prefix: str = "external",
) -> dict[str, Any]:
    output = Path(output_dir)
    artifacts: list[dict[str, Any]] = []
    for path in sorted(item for item in output.rglob("*") if item.is_file() and item.name != "external_artifacts.json"):
        relative = path.relative_to(output).as_posix()
        artifacts.append({
            "path": relative,
            "size_bytes": path.stat().st_size,
            "sha256": sha256_bytes(path.read_bytes()),
            "artifact_kind": f"{artifact_kind_prefix}:{relative.split('/', 1)[0]}",
            "configuration_identity": configuration_identity,
            "provenance_identity": provenance_identity,
        })
    manifest = {
        "schema_version": "external-artifact-manifest-v1",
        "external_root": str(output),
        "configuration_identity": configuration_identity,
        "provenance_identity": provenance_identity,
        "artifacts": artifacts,
    }
    _write_json(output / "external_artifacts.json", manifest)
    return manifest


def snapshot_trainable_lora_parameters(model: Any) -> dict[str, Any]:
    """Snapshot every trainable LoRA parameter as detached CPU tensors."""
    return {
        name: parameter.detach().float().cpu().clone()
        for name, parameter in model.named_parameters()
        if "lora_" in name and parameter.requires_grad
    }


def aggregate_lora_delta(before: Mapping[str, Any], model: Any) -> dict[str, Any]:
    """Aggregate the post-step delta across all trainable LoRA tensors."""
    if not before:
        return {
            "trainable_tensors_checked": 0,
            "changed_tensors": 0,
            "aggregate_delta_l2": None,
            "delta_finite": False,
        }
    after = dict(model.named_parameters())
    changed = 0
    squared_total = 0.0
    for name, before_value in before.items():
        delta = after[name].detach().float().cpu() - before_value
        squared = float((delta * delta).sum())
        squared_total += squared
        if squared != 0.0:
            changed += 1
    aggregate_l2 = math.sqrt(squared_total)
    return {
        "trainable_tensors_checked": len(before),
        "changed_tensors": changed,
        "aggregate_delta_l2": aggregate_l2,
        "delta_finite": math.isfinite(aggregate_l2),
    }


def create_non_held_out_verifier_smoke(repository_root: str | Path, output_path: str | Path) -> dict[str, Any]:
    source_root = Path(repository_root)
    with tempfile.TemporaryDirectory(prefix="qlora_patch_smoke_") as temporary:
        root = Path(temporary)
        fixture = root / "agentic_debugger/datasets/curated/non-heldout-smoke-001"
        tests_dir = fixture / "tests"
        tests_dir.mkdir(parents=True)
        (fixture / "counter.py").write_text(
            "def next_value(value: int) -> int:\n    return value - 1\n", encoding="utf-8"
        )
        (tests_dir / "test_counter.py").write_text(
            "from counter import next_value\n\n"
            "def test_next_value_advances():\n    assert next_value(4) == 5\n\n"
            "def test_result_remains_integer():\n    assert isinstance(next_value(0), int)\n",
            encoding="utf-8",
        )
        task_mapping = {
            "schema_version": "1.0",
            "task_id": "non-heldout-smoke-001",
            "title": "Advance a numeric counter",
            "description": "A non-held-out synthetic fixture used only to test the existing verifier integration.",
            "language": "python",
            "fixture_path": "agentic_debugger/datasets/curated/non-heldout-smoke-001",
            "reproduction": {"argv": ["python", "-m", "pytest", "tests/test_counter.py::test_next_value_advances", "-q", "-p", "no:cacheprovider"], "cwd": ".", "timeout_seconds": 10, "expected_exit_code": 1},
            "tests": {"fail_to_pass": ["tests/test_counter.py::test_next_value_advances"], "pass_to_pass": ["tests/test_counter.py::test_result_remains_integer"], "full_suite_argv": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"], "timeout_seconds": 20},
            "constraints": {"allowed_write_paths": ["counter.py"], "denied_write_paths": ["tests", "task.json"], "network_allowed": False, "external_services_allowed": False, "max_patch_attempts": 1, "max_test_runs": 5, "max_pdb_observations": 0},
            "oracle": {"bug_category": "synthetic arithmetic error", "target_files": ["counter.py"], "target_symbols": ["next_value"], "root_cause_summary": "The counter decrements instead of incrementing.", "runtime_evidence_hint": "Observe the returned value for input 4."},
            "tags": ["synthetic", "non-held-out", "smoke"],
        }
        (fixture / "task.json").write_text(json.dumps(task_mapping, indent=2) + "\n", encoding="utf-8")
        patch = "".join(difflib.unified_diff(
            ["def next_value(value: int) -> int:\n", "    return value - 1\n"],
            ["def next_value(value: int) -> int:\n", "    return value + 1\n"],
            fromfile="a/counter.py", tofile="b/counter.py", lineterm="\n",
        ))
        parse_unified_diff_strict(patch, ["counter.py"])
        task = DebugTask.from_file(str(fixture / "task.json"))
        result = EvaluationVerifier(str(root), workspace_parent=temporary).evaluate(task, patch)
        mapping = result.to_mapping()
        smoke = {
            "task_id": mapping["task_id"],
            "status": mapping["status"],
            "outcome": mapping["outcome"],
            "stop_reason": mapping["stop_reason"],
            "f2p": [mapping["f2p_passed"], mapping["f2p_total"]],
            "p2p": [mapping["p2p_passed"], mapping["p2p_total"]],
            "patch_applied": mapping["patch_application"]["success"],
            "syntax_passed": mapping["syntax"]["passed"],
            "canonical_fixture_unchanged": mapping["workspace"]["canonical_fixture_unchanged"],
            "workspace_cleaned": mapping["workspace"]["cleaned"],
            "held_out_task_used": False,
            "source_repository_root": str(source_root),
        }
        _write_json(Path(output_path), smoke)
        return smoke


def _execution_git_runtime(root: Path) -> dict[str, Any]:
    runtime: dict[str, Any] = {"execution_head": None, "execution_branch": None, "detached": None, "dirty": None}
    if not (root / ".git").exists():
        return runtime

    def _git(*args: str) -> str | None:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None

    runtime["execution_head"] = _git("rev-parse", "--short", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    runtime["execution_branch"] = None if branch == "HEAD" else branch
    runtime["detached"] = (branch == "HEAD") if branch is not None else None
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True)
    runtime["dirty"] = bool(status.stdout.strip()) if status.returncode == 0 else None
    return runtime


def verify_freeze_record(repository_root: str | Path, freeze_record_path: str | Path) -> dict[str, Any]:
    """Recompute every local freeze identity without exposing held-out answers."""
    root = Path(repository_root)
    freeze_path = Path(freeze_record_path)
    freeze = load_json(freeze_path)
    experiment_dir = freeze_path.parent
    checks: list[dict[str, Any]] = []
    for key, section in (("prompt_contract", "prompt_contract"), ("transformation", "transformation"), ("training", "training"), ("generation", "generation")):
        record = freeze[section]
        configured = Path(record["path"])
        path = configured if configured.is_absolute() else root / configured
        actual = sha256_bytes(canonical_json_bytes(load_json(path)))
        expected = record["sha256"]
        checks.append({"identity": key, "expected_sha256": expected, "actual_sha256": actual, "match": actual == expected})
    from agentic_debugger.demo.catalog import build_reference_patch, scenario_for
    for record in freeze["held_out_tasks"]:
        fixture = root / record["fixture_path"]
        tree_digest = hashlib.sha256()
        for path in sorted(item for item in fixture.rglob("*") if item.is_file()):
            if path.suffix == ".pyc" and path.parent.name == "__pycache__":
                continue
            tree_digest.update(path.relative_to(fixture).as_posix().encode("utf-8"))
            tree_digest.update(b"\0")
            tree_digest.update(path.read_bytes())
            tree_digest.update(b"\0")
        scenario = scenario_for(record["task_id"])
        target = fixture / scenario.reference_repair.target_path
        buggy = target.read_text(encoding="utf-8")
        corrected = buggy.replace(scenario.reference_repair.old_snippet, scenario.reference_repair.new_snippet, 1)
        patch = build_reference_patch(buggy, scenario.reference_repair)
        identities = {
            "buggy_fixture_tree_sha256": tree_digest.hexdigest(),
            "buggy_target_sha256": sha256_text(buggy),
            "corrected_target_sha256": sha256_text(corrected),
            "gold_patch_sha256": sha256_text(patch),
        }
        for name, actual in identities.items():
            expected = record[name]
            checks.append({"identity": f"{record['task_id']}:{name}", "expected_sha256": expected, "actual_sha256": actual, "match": actual == expected})
    runtime = _execution_git_runtime(root)
    if (root / ".git").exists():
        base_commit = freeze["repository_baseline"]["base_commit"]
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", base_commit, "HEAD"], cwd=root, capture_output=True, text=True)
        is_ancestor = ancestor.returncode == 0
        checks.append({
            "identity": f"repository_baseline:required_ancestor({base_commit})",
            "expected": f"{base_commit} is an ancestor of the execution HEAD",
            "actual": "ancestor" if is_ancestor else "not-ancestor",
            "match": is_ancestor,
        })
    failed = [check["identity"] for check in checks if not check["match"]]
    if failed:
        raise CorpusBuildError(f"freeze identity drift: {failed}")
    return {"status": "LOCKED", "checks": len(checks), "failed": [], "runtime": runtime}


def build_task_prompt_record(
    task: DebugTask,
    *,
    fixture_root: str | Path,
    bounded_failure: str,
    prompt_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one oracle-free prompt record from a frozen task and buggy files."""
    if len(task.constraints.allowed_write_paths) != 1:
        raise CorpusBuildError("the pilot prompt contract requires exactly one allowed path")
    file_path = task.constraints.allowed_write_paths[0]
    source_path = Path(fixture_root) / file_path
    buggy_source = source_path.read_text(encoding="utf-8")
    failure = bounded_failure[: int(prompt_contract["failure_output_max_chars"])]
    user = prompt_contract["user_template"].format(
        title=task.title,
        description=task.description,
        allowed_write_paths=f"- {file_path}",
        reproduction_command=" ".join(task.reproduction.argv),
        bounded_failure=failure,
        file_path=file_path,
        buggy_source=buggy_source.rstrip("\n"),
    )
    prompt = [
        {"role": "system", "content": prompt_contract["system_prompt"]},
        {"role": "user", "content": user},
    ]
    return {
        "schema_version": "held-out-prompt-record-v1",
        "task_id": task.task_id,
        "allowed_write_paths": list(task.constraints.allowed_write_paths),
        "prompt": prompt,
        "prompt_sha256": sha256_bytes(canonical_json_bytes(prompt)),
        "buggy_source_sha256": sha256_text(buggy_source),
        "oracle_serialized": False,
    }


def record_generation_once(
    output_dir: str | Path,
    *,
    condition: str,
    task_id: str,
    raw_output: str,
    prompt_sha256: str,
    generation_config_sha256: str,
    model_repository: str,
    model_revision: str,
    adapter_sha256: str | None,
) -> dict[str, Any]:
    """Persist one immutable raw generation and refuse regeneration."""
    target = Path(output_dir) / condition / task_id
    record_path = target / "generation_record.json"
    raw_path = target / "raw_output.txt"
    if record_path.exists() or raw_path.exists():
        raise CorpusBuildError(f"generation already exists for {condition}/{task_id}; regeneration is prohibited")
    target.mkdir(parents=True, exist_ok=False)
    raw_path.write_text(raw_output, encoding="utf-8")
    record = {
        "schema_version": "generation-record-v1",
        "condition": condition,
        "task_id": task_id,
        "prompt_sha256": prompt_sha256,
        "generation_config_sha256": generation_config_sha256,
        "model_repository": model_repository,
        "model_revision": model_revision,
        "adapter_sha256": adapter_sha256,
        "raw_output_path": raw_path.name,
        "raw_output_size_bytes": raw_path.stat().st_size,
        "raw_output_sha256": sha256_bytes(raw_path.read_bytes()),
        "generation_count": 1,
    }
    _write_json(record_path, record)
    return record


def verify_saved_raw_output(
    *,
    repository_root: str | Path,
    task_path: str | Path,
    raw_output_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Parse and verify a saved generation without invoking a model."""
    task = DebugTask.from_file(str(task_path))
    raw_path = Path(raw_output_path)
    raw = raw_path.read_text(encoding="utf-8")
    patch = parse_unified_diff_strict(raw, task.constraints.allowed_write_paths)
    result = EvaluationVerifier(str(repository_root)).evaluate(task, patch)
    mapping = result.to_mapping()
    record = {
        "schema_version": "saved-generation-verification-v1",
        "task_id": task.task_id,
        "raw_output_sha256": sha256_bytes(raw_path.read_bytes()),
        "verifier_result": mapping,
    }
    _write_json(Path(output_path), record)
    return record

FINAL_TRAINING_AUTH_SCHEMA = "final-training-authorization-v1"
FINAL_TRAINING_SCOPE = "final_training_only"
FINAL_TRAINING_APPROVER = "FirstMate / GPT-5.6 Thinking"
FINAL_TRAINING_AUTH_TYPE = "owner-delegated FirstMate gate"
FINAL_TRAINING_FINAL_STATUS = "FINAL_TRAINING_COMPLETE_AWAITING_FIRSTMATE_REVIEW"
FINAL_TRAINING_MANIFEST_SCHEMA = "external-artifact-manifest-v1"
PAYLOAD_MANIFEST_EXCLUDED_NAMES = {
    "external_artifacts.json",
    "final_training_summary.json",
    "run_status.json",
    "RUN_COMPLETE",
    "INCOMPLETE",
}
TEMP_FILE_SUFFIXES = (".tmp", ".part", "~")
FINAL_TRAINING_SUMMARY_SCHEMA_VERSION = "final-training-summary-v1"
FINAL_TRAINING_EXPERIMENT_ID = "qlora-patch-pilot-v1"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
GIT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{7,40}")
GIT_COMMIT_FULL_PATTERN = re.compile(r"[0-9a-f]{40}")
AUDIT_RESULT_KEYS = (
    "total_rows",
    "accepted_packet_rows",
    "rejected_packet_rows",
    "accepted_packet_accept",
    "accepted_packet_reject",
    "rejected_packet_accept",
    "rejected_packet_reject",
)
CONFIGURATION_IDENTITY_KEYS = ("prompt_contract", "transformation", "training", "generation")
RUNTIME_IDENTITY_KEYS = (
    "python",
    "torch",
    "cuda_available",
    "cuda_runtime",
    "gpu",
    "gpu_total_memory_bytes",
    "packages",
    "repository_verification",
)
RELOAD_VERIFICATION_KEYS = ("adapter_reloaded", "adapter_path", "verified_at_utc")
TRAINER_STATE_KEYS = ("global_step", "epoch", "log_history")
TOKENIZER_ARTIFACT_NAMES = ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "added_tokens.json")

# Central summary provenance contract shared by production code and tests.
# The completion helper refuses FINAL status unless every mandated field is
# present and satisfies its declared kind; exact-value and cross-record checks
# are applied by write_final_run_status against run_context.json, the bound
# authorization record, the payload manifest, and reload_verification.json.
FINAL_TRAINING_SUMMARY_CONTRACT: dict[str, dict[str, Any]] = {
    "schema_version": {"kind": "exact_string", "value": FINAL_TRAINING_SUMMARY_SCHEMA_VERSION},
    "experiment_id": {"kind": "exact_string", "value": FINAL_TRAINING_EXPERIMENT_ID},
    "run_id": {"kind": "run_id"},
    "final_status": {"kind": "exact_string", "value": FINAL_TRAINING_FINAL_STATUS},
    "manifest_sha256": {"kind": "sha256"},
    "started_at_utc": {"kind": "nonempty_string"},
    "completed_at_utc": {"kind": "nonempty_string"},
    "repository_commit": {"kind": "git_commit"},
    "required_ancestor": {"kind": "git_commit"},
    "authorization_path": {"kind": "nonempty_string"},
    "authorization_sha256": {"kind": "sha256"},
    "completed_audit_csv_path": {"kind": "nonempty_string"},
    "completed_audit_csv_sha256": {"kind": "sha256"},
    "completed_audit_manifest_path": {"kind": "nonempty_string"},
    "completed_audit_manifest_sha256": {"kind": "sha256"},
    "train_jsonl_path": {"kind": "nonempty_string"},
    "train_jsonl_sha256": {"kind": "sha256"},
    "train_jsonl_bytes": {"kind": "nonnegative_int"},
    "train_rows": {"kind": "positive_int"},
    "validation_jsonl_path": {"kind": "nonempty_string"},
    "validation_jsonl_sha256": {"kind": "sha256"},
    "validation_jsonl_bytes": {"kind": "nonnegative_int"},
    "validation_rows": {"kind": "positive_int"},
    "corpus_manifest_path": {"kind": "nonempty_string"},
    "corpus_manifest_sha256": {"kind": "sha256"},
    "corpus_manifest_bytes": {"kind": "nonnegative_int"},
    "model_repository": {"kind": "nonempty_string"},
    "model_revision": {"kind": "git_commit_full"},
    "configuration_identities": {
        "kind": "dict",
        "exact_keys": CONFIGURATION_IDENTITY_KEYS,
        "values": "sha256",
    },
    "audit_mode": {"kind": "exact_string", "value": AUDIT_MODE_INDEPENDENT_AI},
    "audit_result": {"kind": "dict", "exact_keys": AUDIT_RESULT_KEYS, "values": "nonnegative_int"},
    "reviewer_identity": {"kind": "nonempty_string"},
    "reviewer_type": {"kind": "exact_string", "value": INDEPENDENT_REVIEWER_TYPE},
    "no_top_up": {"kind": "exact_bool", "value": True},
    "train_loss": {"kind": "finite_number"},
    "elapsed_seconds": {"kind": "nonnegative_number"},
    "peak_cuda_memory_allocated_bytes": {"kind": "nonnegative_int"},
    "peak_cuda_memory_reserved_bytes": {"kind": "nonnegative_int"},
    "train_examples": {"kind": "positive_int"},
    "validation_examples": {"kind": "positive_int"},
    "epochs": {"kind": "positive_number"},
    "trainer_state": {"kind": "dict", "required_keys": TRAINER_STATE_KEYS},
    "trainer_state_identity": {"kind": "sha256"},
    "training_log_identity": {"kind": "sha256"},
    "tokenizer_identities": {"kind": "identity_map"},
    "runtime": {"kind": "runtime_identity"},
    "adapter_identities": {"kind": "identity_map"},
    "reload_verification": {"kind": "reload_result"},
    "held_out_generation_authorized": {"kind": "exact_bool", "value": False},
    "held_out_accessed": {"kind": "exact_bool", "value": False},
}
REQUIRED_SUMMARY_FIELDS = tuple(FINAL_TRAINING_SUMMARY_CONTRACT)  # compatibility alias
EXPECTED_ARTIFACT_LOGICAL_PATHS = {
    "train_jsonl": "corpus/train.jsonl",
    "validation_jsonl": "corpus/validation.jsonl",
    "corpus_manifest": "corpus/external_artifacts.json",
    "completed_audit_csv": "independent-audit/firstmate_independent_audit_completed.csv",
    "completed_audit_manifest": "independent-audit/firstmate_independent_audit_manifest.json",
}


def validate_final_training_authorization(
    authorization_path: str | Path,
    *,
    repository_root: str | Path,
    corpus_dir: str | Path,
    transformation_config_path: str | Path,
    train_jsonl: str | Path,
    validation_jsonl: str | Path,
    corpus_manifest: str | Path,
    completed_audit_csv: str | Path,
    completed_audit_manifest: str | Path,
) -> dict[str, Any]:
    """Fail-closed validation of the separate final-training authorization record.

    Every corpus and audit artifact identity is recomputed from the supplied
    files; a structurally valid JSON record alone never reports COMPLETE.
    """
    root = Path(repository_root)
    authorization_path = Path(authorization_path)
    if not authorization_path.is_file():
        raise CorpusBuildError(f"final-training authorization record missing: {authorization_path}")
    authorization = load_json(authorization_path)
    freeze_path = root / "experiments/qlora_patch_pilot_v1/freeze_record.json"
    freeze = load_json(freeze_path)

    problems: list[str] = []
    if authorization.get("schema_version") != FINAL_TRAINING_AUTH_SCHEMA:
        problems.append(f"schema_version {authorization.get('schema_version')!r} is not {FINAL_TRAINING_AUTH_SCHEMA!r}")
    if authorization.get("experiment_id") != freeze["experiment_id"]:
        problems.append(f"experiment_id {authorization.get('experiment_id')!r} does not match the freeze record")
    if authorization.get("authorization_scope") != FINAL_TRAINING_SCOPE:
        problems.append(f"authorization_scope {authorization.get('authorization_scope')!r} is not {FINAL_TRAINING_SCOPE!r}")
    if authorization.get("authorized") is not True:
        problems.append("authorized is not true")
    if authorization.get("held_out_generation_authorized") is not False:
        problems.append("held_out_generation_authorized is not false")
    if authorization.get("base_versus_tuned_evaluation_authorized") is not False:
        problems.append("base_versus_tuned_evaluation_authorized is not false")
    if authorization.get("authorized_by") != FINAL_TRAINING_APPROVER:
        problems.append(f"authorized_by {authorization.get('authorized_by')!r} is not {FINAL_TRAINING_APPROVER!r}")
    if authorization.get("authorization_type") != FINAL_TRAINING_AUTH_TYPE:
        problems.append(f"authorization_type {authorization.get('authorization_type')!r} is not {FINAL_TRAINING_AUTH_TYPE!r}")
    if not str(authorization.get("methodology", "")).startswith(INDEPENDENT_AUDIT_METHODOLOGY):
        problems.append("methodology disclosure missing or incorrect")
    if authorization.get("audit_mode") != AUDIT_MODE_INDEPENDENT_AI:
        problems.append(f"audit_mode {authorization.get('audit_mode')!r} is not independent_ai")

    declared_artifacts = authorization.get("corpus_artifacts", {})
    corpus_artifact_identities: dict[str, dict[str, Any]] = {}
    for kind, path in (
        ("train_jsonl", train_jsonl),
        ("validation_jsonl", validation_jsonl),
        ("corpus_manifest", corpus_manifest),
    ):
        observed = _verify_bound_artifact(
            declared_artifacts.get(kind), Path(path), kind, problems,
            expected_logical_path=EXPECTED_ARTIFACT_LOGICAL_PATHS[kind],
        )
        corpus_artifact_identities[kind] = observed

    declared_audit_artifacts = authorization.get("audit_artifacts", {})
    audit_artifact_identities: dict[str, dict[str, Any]] = {}
    for kind, path in (
        ("completed_audit_csv", completed_audit_csv),
        ("completed_audit_manifest", completed_audit_manifest),
    ):
        observed = _verify_bound_artifact(
            declared_audit_artifacts.get(kind), Path(path), kind, problems,
            expected_logical_path=EXPECTED_ARTIFACT_LOGICAL_PATHS[kind],
        )
        audit_artifact_identities[kind] = observed

    try:
        audit_validation = validate_completed_audits(
            corpus_dir, transformation_config_path, completed_audit_path=completed_audit_csv
        )
    except CorpusBuildError as exc:
        problems.append(f"completed audit validation failed: {exc}")
        audit_validation = None
    audit_result = authorization.get("audit_result", {})
    expected_audit_counts: dict[str, Any] = {}
    if audit_validation is not None:
        expected_audit_counts = {
            "total_rows": audit_validation["accepted_packet_total"] + audit_validation["rejected_packet_total"],
            "accepted_packet_rows": audit_validation["accepted_packet_total"],
            "rejected_packet_rows": audit_validation["rejected_packet_total"],
            "accepted_packet_accept": audit_validation["accepted_packet_accept"],
            "accepted_packet_reject": audit_validation["accepted_packet_reject"],
            "rejected_packet_accept": audit_validation["rejected_packet_accept"],
            "rejected_packet_reject": audit_validation["rejected_packet_reject"],
        }
        for key, value in expected_audit_counts.items():
            if audit_result.get(key) != value:
                problems.append(f"audit_result.{key} {audit_result.get(key)!r} does not match the validated {value}")
    else:
        problems.append("completed audit could not be validated; audit counts not verified")
    if audit_result.get("reviewer") != FINAL_TRAINING_APPROVER:
        problems.append(f"audit_result.reviewer {audit_result.get('reviewer')!r} is not {FINAL_TRAINING_APPROVER!r}")
    if audit_result.get("reviewer_type") != INDEPENDENT_REVIEWER_TYPE:
        problems.append(f"audit_result.reviewer_type {audit_result.get('reviewer_type')!r} is not {INDEPENDENT_REVIEWER_TYPE!r}")

    corpus = authorization.get("corpus", {})
    if corpus.get("tier") != "minimum":
        problems.append(f"corpus.tier {corpus.get('tier')!r} is not minimum")
    if authorization.get("top_up") not in (False, None):
        problems.append("top_up must not be declared")
    if corpus.get("top_up") not in (False, None):
        problems.append("corpus.top_up must not be declared")
    train_rows = corpus_artifact_identities["train_jsonl"].get("rows", 0)
    validation_rows = corpus_artifact_identities["validation_jsonl"].get("rows", 0)
    if corpus.get("train") != train_rows:
        problems.append(f"corpus.train {corpus.get('train')!r} does not match the bound train rows {train_rows}")
    if corpus.get("validation") != validation_rows:
        problems.append(f"corpus.validation {corpus.get('validation')!r} does not match the bound validation rows {validation_rows}")

    model = authorization.get("model", {})
    training = load_json(root / freeze["training"]["path"])
    if model.get("repository") != training["model_repository"] or model.get("revision") != training["model_revision"]:
        problems.append("model repository/revision does not match the frozen training config")
    dataset = authorization.get("dataset", {})
    if dataset.get("repository") != freeze["dataset"]["repository"] or dataset.get("revision") != freeze["dataset"]["revision"]:
        problems.append("dataset repository/revision does not match the freeze record")
    if authorization.get("required_repository_ancestor") != freeze["repository_baseline"]["base_commit"]:
        problems.append("required_repository_ancestor does not match the freeze baseline")
    declared = authorization.get("configuration_identities", {})
    configuration_identities: dict[str, str] = {}
    for key, section in (("prompt_contract", "prompt_contract"), ("transformation", "transformation"), ("training", "training"), ("generation", "generation")):
        configured = Path(freeze[section]["path"])
        path = configured if configured.is_absolute() else root / configured
        actual = sha256_bytes(canonical_json_bytes(load_json(path)))
        configuration_identities[key] = actual
        if declared.get(key) != actual:
            problems.append(f"configuration_identity.{key} {declared.get(key)!r} does not match the recomputed {actual}")
        if freeze[section]["sha256"] != actual:
            problems.append(f"freeze configuration identity {key} drifted from the recomputed value")
    if not str(authorization.get("issued_at", "")).strip():
        problems.append("issued_at is missing")
    if not authorization.get("authorizes_training_only_statement"):
        problems.append("training-only statement missing")

    corpus_summary_path = Path(corpus_dir) / "corpus_summary.json"
    dedup_path = Path(corpus_dir) / "dedup_report.json"
    if not corpus_summary_path.is_file():
        problems.append(f"corpus summary missing: {corpus_summary_path}")
    else:
        summary = load_json(corpus_summary_path)
        if summary.get("train_examples") != train_rows or summary.get("validation_examples") != validation_rows:
            problems.append(
                f"corpus summary counts {summary.get('train_examples')}/{summary.get('validation_examples')} "
                f"do not match the bound {train_rows}/{validation_rows}"
            )
    if not dedup_path.is_file():
        problems.append(f"dedup report missing: {dedup_path}")
    else:
        dedup = load_json(dedup_path)
        if dedup.get("repository_overlap") not in ([], None):
            problems.append("repository overlap is not empty")

    if problems:
        raise CorpusBuildError("final-training authorization validation failed: " + "; ".join(problems))
    return {
        "schema_version": FINAL_TRAINING_AUTH_SCHEMA,
        "experiment_id": freeze["experiment_id"],
        "authorization_path": str(authorization_path),
        "authorization_sha256": sha256_bytes(authorization_path.read_bytes()),
        "authorization_scope": FINAL_TRAINING_SCOPE,
        "authorized": True,
        "held_out_generation_authorized": False,
        "base_versus_tuned_evaluation_authorized": False,
        "authorized_by": FINAL_TRAINING_APPROVER,
        "authorization_type": FINAL_TRAINING_AUTH_TYPE,
        "methodology": INDEPENDENT_AUDIT_METHODOLOGY,
        "audit_mode": AUDIT_MODE_INDEPENDENT_AI,
        "repository_identities": {
            "required_ancestor": freeze["repository_baseline"]["base_commit"],
            "freeze_sha256": sha256_bytes(canonical_json_bytes(freeze)),
        },
        "configuration_identities": configuration_identities,
        "corpus_artifact_identities": corpus_artifact_identities,
        "audit_artifact_identities": audit_artifact_identities,
        "row_counts": {"train": train_rows, "validation": validation_rows},
        "audit_counts": expected_audit_counts,
        "reviewer_identity": audit_result.get("reviewer"),
        "reviewer_type": audit_result.get("reviewer_type"),
        "corpus": {"tier": "minimum", "train": train_rows, "validation": validation_rows, "top_up": False},
        "status": "COMPLETE",
    }


def _verify_bound_artifact(
    declared: Mapping[str, Any] | None,
    path: Path,
    kind: str,
    problems: list[str],
    *,
    expected_logical_path: str,
) -> dict[str, Any]:
    """Recompute the artifact's identity from the actual file and bind it to the declaration."""
    if declared is None or not isinstance(declared, Mapping):
        problems.append(f"authorization declares no {kind} artifact binding")
        return {"path": str(path)}
    if not path.is_file():
        problems.append(f"{kind} artifact missing: {path}")
        return {"path": str(path)}
    declared_hash = str(declared.get("sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", declared_hash):
        problems.append(f"{kind}: declared sha256 is missing or malformed")
    actual_hash = sha256_bytes(path.read_bytes())
    logical_path = str(declared.get("logical_path", ""))
    if not _canonical_relative_logical_path(logical_path):
        problems.append(f"{kind}: logical_path is missing, absolute, traversing, or non-canonical")
    elif logical_path != expected_logical_path:
        problems.append(
            f"{kind}: logical_path {logical_path!r} does not equal the exact canonical "
            f"identity {expected_logical_path!r}"
        )
    observed: dict[str, Any] = {
        "path": str(path),
        "logical_path": logical_path,
        "size_bytes": path.stat().st_size,
        "sha256": actual_hash,
    }
    if declared_hash and declared_hash != actual_hash:
        problems.append(f"{kind} sha256 mismatch: declared {declared_hash}, actual {actual_hash}")
    declared_size = declared.get("size_bytes")
    if declared_size is not None and int(declared_size) != path.stat().st_size:
        problems.append(
            f"{kind} byte-size mismatch: declared {declared_size}, actual {path.stat().st_size}"
        )
    if "rows" in declared:
        actual_rows = sum(1 for line in path.open("r", encoding="utf-8") if line.strip())
        observed["rows"] = actual_rows
        if int(declared["rows"]) != actual_rows:
            problems.append(
                f"{kind} row-count mismatch: declared {declared['rows']}, actual {actual_rows}"
            )
    return observed


def _canonical_relative_logical_path(value: str) -> bool:
    """A logical artifact path must be canonical: relative posix, no traversal, no non-canonical separators."""
    if not value or "\\" in value:
        return False
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts or "//" in value:
        return False
    return pure.as_posix() == value


def _is_payload_artifact(relative: PurePosixPath) -> bool:
    if relative.name in PAYLOAD_MANIFEST_EXCLUDED_NAMES:
        return False
    return not relative.name.endswith(TEMP_FILE_SUFFIXES)


def _collect_payload_artifacts(run_dir: Path) -> list[tuple[PurePosixPath, Path]]:
    """Deterministic immutable-payload selection within the active run directory."""
    artifacts: list[tuple[PurePosixPath, Path]] = []
    for path in sorted(item for item in run_dir.rglob("*") if item.is_file()):
        relative = path.relative_to(run_dir).as_posix()
        pure = PurePosixPath(relative)
        if _is_payload_artifact(pure):
            artifacts.append((pure, path))
    return artifacts


REQUIRED_PAYLOAD_FILES = (
    "run_context.json",
    "trainer_state.json",
    "training_log_history.json",
    "runtime_environment.json",
    "memory_timing.json",
    "reload_verification.json",
)
ADAPTER_WEIGHT_GLOBS = ("adapter-*/adapter_model.safetensors", "adapter-*/adapter_model.bin")
ADAPTER_METADATA_GLOBS = (
    "adapter-*/adapter_config.json",
    "adapter-*/tokenizer.json",
    "adapter-*/tokenizer_config.json",
)


def _validate_required_payload_set(directory: Path, problems: list[str]) -> None:
    """Fail closed unless the active run contains the complete required payload set."""
    for filename in REQUIRED_PAYLOAD_FILES:
        path = directory / filename
        if not path.is_file():
            problems.append(f"missing required payload {filename}")
        elif path.stat().st_size == 0:
            problems.append(f"required payload {filename} is zero-byte")
    weight_matches: list[Path] = []
    for pattern in ADAPTER_WEIGHT_GLOBS:
        weight_matches.extend(sorted(directory.glob(pattern)))
    if not weight_matches:
        problems.append(
            "missing required adapter weight artifact "
            "(adapter-*/adapter_model.safetensors or adapter-*/adapter_model.bin)"
        )
    elif len(weight_matches) > 1:
        problems.append(
            "ambiguous adapter-weight selection: "
            + ", ".join(str(path.relative_to(directory)) for path in weight_matches)
        )
    else:
        if weight_matches[0].stat().st_size == 0:
            problems.append("required adapter weight artifact is zero-byte")
        adapter_dir = weight_matches[0].parent
        for pattern in ADAPTER_METADATA_GLOBS:
            matches = sorted(directory.glob(pattern))
            if not matches:
                problems.append(f"missing required adapter metadata artifact {pattern}")
            elif len(matches) > 1:
                problems.append(
                    f"ambiguous adapter metadata artifact {pattern}: "
                    + ", ".join(str(path.relative_to(directory)) for path in matches)
                )
            else:
                if matches[0].stat().st_size == 0:
                    problems.append(f"required adapter metadata artifact {pattern} is zero-byte")
                if matches[0].parent != adapter_dir:
                    problems.append(
                        f"adapter metadata artifact {pattern} is not inside the "
                        f"adapter weight directory {adapter_dir.name}"
                    )


def write_payload_manifest(
    run_dir: str | Path,
    *,
    configuration_identity: str,
    provenance_identity: str,
) -> dict[str, Any]:
    """Write external_artifacts.json covering only immutable run payload artifacts.

    Fails closed unless the active run contains the complete required payload
    set (run context, adapter weights plus reload metadata, tokenizer artifacts,
    trainer state, log history, runtime environment, memory/timing record, and
    reload evidence).
    """
    directory = Path(run_dir)
    if not directory.is_dir():
        raise CorpusBuildError(f"run directory missing: {directory}")
    problems: list[str] = []
    _validate_required_payload_set(directory, problems)
    if problems:
        raise CorpusBuildError("required payload validation failed: " + "; ".join(problems))
    artifacts: list[dict[str, Any]] = []
    for relative, path in _collect_payload_artifacts(directory):
        artifacts.append({
            "path": relative.as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_bytes(path.read_bytes()),
            "artifact_kind": f"final-training-payload:{relative.parts[0]}",
            "configuration_identity": configuration_identity,
            "provenance_identity": provenance_identity,
        })
    manifest = {
        "schema_version": FINAL_TRAINING_MANIFEST_SCHEMA,
        "external_root": str(directory),
        "configuration_identity": configuration_identity,
        "provenance_identity": provenance_identity,
        "artifacts": artifacts,
    }
    _write_json(directory / "external_artifacts.json", manifest)
    return manifest


def create_final_training_run(
    runs_root: str | Path,
    authorization_path: str | Path,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Create one fresh isolated final-training run directory; never reuse output."""
    auth_path = Path(authorization_path)
    if not auth_path.is_file():
        raise CorpusBuildError(f"final-training authorization record missing: {auth_path}")
    authorization_sha256 = sha256_bytes(auth_path.read_bytes())
    if run_id is None:
        run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + authorization_sha256[:8]
    run_dir = Path(runs_root) / run_id
    if run_dir.exists():
        raise CorpusBuildError(f"run directory already exists; a fresh run id is required: {run_dir}")
    run_dir.mkdir(parents=True)
    run_context = {
        "run_id": run_id,
        "authorization_path": str(auth_path),
        "authorization_sha256": authorization_sha256,
        "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": "INCOMPLETE",
    }
    _write_json(run_dir / "run_context.json", run_context)
    (run_dir / "INCOMPLETE").write_text("INCOMPLETE run; see run_status.json\n", encoding="utf-8")
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "authorization_sha256": authorization_sha256,
        "run_context": run_context,
    }


def write_final_run_status(
    run_dir: str | Path,
    *,
    manifest_sha256: str,
    summary_sha256: str,
) -> dict[str, Any]:
    """Authoritative fail-closed completion for a final-training run.

    Validates the complete run package: run identity (run_context.json bound to
    the active directory name), the required payload set, the immutable payload
    manifest, the full summary provenance contract, reload evidence cross-check,
    authorization cross-check, and the held-out boundary. Only then writes
    run_status.json, writes RUN_COMPLETE last, and only then removes INCOMPLETE.
    """
    directory = Path(run_dir)
    if not directory.is_dir():
        raise CorpusBuildError(f"run directory missing: {directory}")
    incomplete = directory / "INCOMPLETE"
    run_complete = directory / "RUN_COMPLETE"
    manifest_path = directory / "external_artifacts.json"
    summary_path = directory / "final_training_summary.json"
    run_status_path = directory / "run_status.json"

    problems: list[str] = []
    if not incomplete.is_file():
        problems.append("INCOMPLETE marker is missing; the run is not in an active incomplete state")
    if run_complete.exists():
        problems.append("RUN_COMPLETE already exists; a second completion attempt is prohibited")
    if not manifest_path.is_file():
        problems.append(f"payload manifest missing: {manifest_path}")
    if not SHA256_PATTERN.fullmatch(manifest_sha256):
        problems.append("supplied manifest sha256 is not canonical lowercase SHA-256")
    if not summary_path.is_file():
        problems.append(f"final summary missing: {summary_path}")
    if not SHA256_PATTERN.fullmatch(summary_sha256):
        problems.append("supplied summary sha256 is not canonical lowercase SHA-256")

    active_run_id = _validate_run_identity(directory, problems)
    _validate_required_payload_set(directory, problems)

    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        actual_manifest_sha = sha256_bytes(manifest_path.read_bytes())
        if actual_manifest_sha != manifest_sha256:
            problems.append(
                f"manifest sha256 mismatch: supplied {manifest_sha256}, recomputed {actual_manifest_sha}"
            )
        try:
            manifest = load_json(manifest_path)
        except CorpusBuildError as exc:
            problems.append(f"manifest is not valid JSON: {exc}")
            manifest = {}
        if manifest.get("schema_version") != FINAL_TRAINING_MANIFEST_SCHEMA:
            problems.append(f"manifest schema_version is not {FINAL_TRAINING_MANIFEST_SCHEMA!r}")
        if str(manifest.get("external_root", "")) != str(directory):
            problems.append("manifest external_root does not match the active run directory")
        manifested = manifest.get("artifacts", [])
        expected_payload = _collect_payload_artifacts(directory)
        expected_paths = [relative.as_posix() for relative, _ in expected_payload]
        manifested_paths = [str(entry.get("path", "")) for entry in manifested]
        if manifested_paths != expected_paths:
            problems.append("manifest does not describe exactly the active-run immutable payload artifacts")
        for entry in manifested:
            relative = str(entry.get("path", ""))
            if not _canonical_relative_logical_path(relative):
                problems.append(f"manifest artifact path is not canonical: {relative!r}")
                continue
            artifact_path = directory / relative
            if not artifact_path.is_file():
                problems.append(f"manifest artifact missing: {relative}")
                continue
            actual_size = artifact_path.stat().st_size
            actual_hash = sha256_bytes(artifact_path.read_bytes())
            if int(entry.get("size_bytes", -1)) != actual_size:
                problems.append(f"manifest artifact size mismatch: {relative}")
            if str(entry.get("sha256", "")) != actual_hash:
                problems.append(f"manifest artifact sha256 mismatch: {relative}")

    if summary_path.is_file():
        actual_summary_sha = sha256_bytes(summary_path.read_bytes())
        if actual_summary_sha != summary_sha256:
            problems.append(
                f"summary sha256 mismatch: supplied {summary_sha256}, recomputed {actual_summary_sha}"
            )
        try:
            summary = load_json(summary_path)
        except CorpusBuildError as exc:
            problems.append(f"summary is not valid JSON: {exc}")
            summary = {}
        validate_final_training_summary_contract(summary, problems)
        if summary.get("run_id") != active_run_id:
            problems.append(f"summary run_id does not match the active run {active_run_id!r}")
        if summary.get("manifest_sha256") != manifest_sha256:
            problems.append("summary does not reference the exact manifest sha256")
        if active_run_id is not None:
            _validate_summary_authorization_crosscheck(summary, directory, problems)
            _validate_reload_crosscheck(summary, active_run_id, directory, problems)
        _validate_summary_payload_identities(summary, directory, manifest, problems)

    if problems:
        raise CorpusBuildError("final run completion validation failed: " + "; ".join(problems))

    run_status = {
        "run_id": active_run_id,
        "status": "COMPLETE",
        "final_status": FINAL_TRAINING_FINAL_STATUS,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "final_summary_path": str(summary_path),
        "final_summary_sha256": summary_sha256,
        "reload_verification": summary.get("reload_verification"),
        "completed_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "held_out_generation_authorized": False,
        "held_out_accessed": False,
    }
    _write_json_atomic(run_status_path, run_status)
    verified = load_json(run_status_path)
    if verified.get("run_id") != active_run_id or verified.get("manifest_sha256") != manifest_sha256:
        raise CorpusBuildError("run_status.json verification failed after write")
    run_status_sha = sha256_bytes(run_status_path.read_bytes())
    _write_json_atomic(
        run_complete,
        {
            "run_id": active_run_id,
            "status": "COMPLETE",
            "final_status": FINAL_TRAINING_FINAL_STATUS,
            "manifest_sha256": manifest_sha256,
            "final_summary_sha256": summary_sha256,
            "run_status_sha256": run_status_sha,
        },
    )
    marker = load_json(run_complete)
    if marker.get("run_id") != active_run_id or marker.get("run_status_sha256") != run_status_sha:
        raise CorpusBuildError("RUN_COMPLETE marker verification failed after write")
    if incomplete.is_file():
        incomplete.unlink()
    return run_status


_MISSING = object()


def _canonical_run_id(value: Any) -> bool:
    """A run id must be a non-empty canonical string usable as a directory name."""
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and "/" not in value
        and "\\" not in value
    )


def validate_final_training_summary_contract(summary: Mapping[str, Any], problems: list[str]) -> None:
    """Append violations of the complete final-training summary provenance contract."""
    for field, spec in FINAL_TRAINING_SUMMARY_CONTRACT.items():
        value = summary.get(field, _MISSING)
        if value is _MISSING:
            problems.append(f"summary is missing required field {field}")
            continue
        if spec["kind"] == "exact_bool" and value is not spec["value"]:
            problems.append(f"summary field {field} is not {'true' if spec['value'] else 'false'}")
            continue
        detail = _summary_field_error(field, spec, value)
        if detail is not None:
            problems.append(f"summary field {field} is invalid: {detail}")


def _summary_field_error(field: str, spec: Mapping[str, Any], value: Any) -> str | None:
    kind = spec["kind"]
    if kind == "exact_string":
        return None if value == spec["value"] else f"does not equal {spec['value']!r}"
    if kind == "exact_bool":
        return None if value is spec["value"] else f"is not {'true' if spec['value'] else 'false'}"
    if kind == "nonempty_string":
        if isinstance(value, str) and value == value.strip() and value:
            return None
        return "must be a non-empty canonical string"
    if kind == "sha256":
        if isinstance(value, str) and SHA256_PATTERN.fullmatch(value):
            return None
        return "must be a canonical lowercase SHA-256"
    if kind == "git_commit":
        if isinstance(value, str) and GIT_COMMIT_PATTERN.fullmatch(value):
            return None
        return "must be a canonical git commit identity"
    if kind == "git_commit_full":
        if isinstance(value, str) and GIT_COMMIT_FULL_PATTERN.fullmatch(value):
            return None
        return "must be a canonical full git commit identity"
    if kind == "run_id":
        if _canonical_run_id(value):
            return None
        return "must be a non-empty canonical run id"
    if kind == "nonnegative_int":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return "must be a non-negative integer"
        return None
    if kind == "positive_int":
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return "must be a positive integer"
        return None
    if kind == "finite_number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return "must be a finite number"
        return None
    if kind == "nonnegative_number":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            return "must be a non-negative finite number"
        return None
    if kind == "positive_number":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            return "must be a positive finite number"
        return None
    if kind == "dict":
        return _mapping_field_error(spec, value)
    if kind == "identity_map":
        return _identity_map_field_error(value)
    if kind == "runtime_identity":
        return _runtime_identity_field_error(value)
    if kind == "reload_result":
        if not isinstance(value, Mapping):
            return "must be an object"
        if value.get("adapter_reloaded") is not True:
            return "reload verification is not explicitly successful"
        missing = [key for key in RELOAD_VERIFICATION_KEYS if key not in value]
        if missing:
            return f"missing required reload verification key(s) {missing}"
        return None
    raise AssertionError(f"unknown summary contract kind {kind!r} for field {field}")


def _mapping_field_error(spec: Mapping[str, Any], value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return "must be an object"
    missing = [key for key in spec.get("required_keys", ()) if key not in value]
    if missing:
        return f"missing required key(s) {missing}"
    exact_keys = spec.get("exact_keys")
    if exact_keys is not None and set(value) != set(exact_keys):
        return f"keys are not exactly {sorted(exact_keys)}"
    values_kind = spec.get("values")
    if values_kind == "sha256":
        for key, item in value.items():
            if not isinstance(item, str) or not SHA256_PATTERN.fullmatch(item):
                return f"value {key!r} is not a canonical lowercase SHA-256"
    elif values_kind == "nonnegative_int":
        for key, item in value.items():
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                return f"value {key!r} is not a non-negative integer"
    elif values_kind is not None:
        raise AssertionError(f"unknown dict value kind {values_kind!r}")
    return None


def _identity_map_field_error(value: Any) -> str | None:
    if not isinstance(value, Mapping) or not value:
        return "must be a non-empty object of artifact identities"
    for name, identity in value.items():
        if not isinstance(name, str) or not _canonical_relative_logical_path(name):
            return f"artifact name {name!r} is not a canonical relative path"
        if not isinstance(identity, Mapping):
            return f"artifact {name!r} identity is not an object"
        size = identity.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return f"artifact {name!r} size_bytes is not a non-negative integer"
        digest = identity.get("sha256")
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            return f"artifact {name!r} sha256 is not a canonical lowercase SHA-256"
    return None


def _runtime_identity_field_error(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return "must be an object"
    missing = [key for key in RUNTIME_IDENTITY_KEYS if key not in value]
    if missing:
        return f"missing required runtime identity key(s) {missing}"
    if not isinstance(value.get("cuda_available"), bool):
        return "cuda_available must be a boolean"
    packages = value.get("packages")
    if not isinstance(packages, Mapping) or not packages:
        return "packages must be a non-empty object"
    return None


def _validate_run_identity(directory: Path, problems: list[str]) -> str | None:
    """Run identity must come from run_context.json and match the active directory."""
    run_context_path = directory / "run_context.json"
    if not run_context_path.is_file():
        problems.append("run_context.json is missing; the active run identity is unavailable")
        return None
    try:
        run_context = load_json(run_context_path)
    except CorpusBuildError as exc:
        problems.append(f"run_context.json is not valid JSON: {exc}")
        return None
    run_id = run_context.get("run_id")
    if not _canonical_run_id(run_id):
        problems.append(f"run_context run_id {run_id!r} is missing, empty, or not a canonical run id")
        return None
    if run_id != directory.name:
        problems.append(
            f"run_context run_id {run_id!r} does not equal the active run directory name {directory.name!r}"
        )
    if not isinstance(run_context.get("authorization_path"), str) or not run_context["authorization_path"].strip():
        problems.append("run_context authorization_path is missing")
    if (
        not isinstance(run_context.get("authorization_sha256"), str)
        or not SHA256_PATTERN.fullmatch(run_context["authorization_sha256"])
    ):
        problems.append("run_context authorization_sha256 is missing or malformed")
    return run_id


def _validate_summary_authorization_crosscheck(
    summary: Mapping[str, Any], directory: Path, problems: list[str]
) -> None:
    """Bind the summary provenance claims to the authorization record bound in run_context."""
    run_context_path = directory / "run_context.json"
    if not run_context_path.is_file():
        return
    try:
        run_context = load_json(run_context_path)
    except CorpusBuildError:
        return
    auth_path_value = run_context.get("authorization_path")
    auth_sha256 = run_context.get("authorization_sha256")
    if (
        not isinstance(auth_path_value, str)
        or not auth_path_value.strip()
        or not isinstance(auth_sha256, str)
    ):
        return
    auth_path = Path(auth_path_value)
    if not auth_path.is_file():
        problems.append("run_context authorization record is missing")
        return
    if sha256_bytes(auth_path.read_bytes()) != auth_sha256:
        problems.append("run_context authorization record drifted from its recorded sha256")
        return
    try:
        authorization = load_json(auth_path)
    except CorpusBuildError as exc:
        problems.append(f"run_context authorization record is not valid JSON: {exc}")
        return
    drift: list[str] = []
    if summary.get("authorization_path") != auth_path_value:
        drift.append("authorization_path")
    if summary.get("authorization_sha256") != auth_sha256:
        drift.append("authorization_sha256")
    if summary.get("required_ancestor") != authorization.get("required_repository_ancestor"):
        drift.append("required_ancestor")
    model = authorization.get("model", {})
    if summary.get("model_repository") != model.get("repository") or summary.get("model_revision") != model.get("revision"):
        drift.append("model repository/revision")
    if summary.get("configuration_identities") != authorization.get("configuration_identities"):
        drift.append("configuration_identities")
    if summary.get("audit_mode") != authorization.get("audit_mode"):
        drift.append("audit_mode")
    summary_audit = summary.get("audit_result") or {}
    authorization_audit = authorization.get("audit_result") or {}
    for key in AUDIT_RESULT_KEYS:
        if summary_audit.get(key) != authorization_audit.get(key):
            drift.append(f"audit_result.{key}")
            break
    if summary.get("reviewer_identity") != authorization_audit.get("reviewer"):
        drift.append("reviewer_identity")
    if summary.get("reviewer_type") != authorization_audit.get("reviewer_type"):
        drift.append("reviewer_type")
    corpus = authorization.get("corpus", {})
    if authorization.get("top_up") not in (False, None) or corpus.get("top_up") not in (False, None):
        drift.append("top_up")
    declared = authorization.get("corpus_artifacts", {})
    for kind, hash_key, size_key, rows_key in (
        ("train_jsonl", "train_jsonl_sha256", "train_jsonl_bytes", "train_rows"),
        ("validation_jsonl", "validation_jsonl_sha256", "validation_jsonl_bytes", "validation_rows"),
    ):
        entry = declared.get(kind, {})
        if summary.get(hash_key) != entry.get("sha256"):
            drift.append(hash_key)
        if summary.get(size_key) != entry.get("size_bytes"):
            drift.append(size_key)
        if "rows" in entry and summary.get(rows_key) != entry.get("rows"):
            drift.append(rows_key)
    manifest_entry = declared.get("corpus_manifest", {})
    if summary.get("corpus_manifest_sha256") != manifest_entry.get("sha256"):
        drift.append("corpus_manifest_sha256")
    if summary.get("corpus_manifest_bytes") != manifest_entry.get("size_bytes"):
        drift.append("corpus_manifest_bytes")
    audit_declared = authorization.get("audit_artifacts", {})
    if summary.get("completed_audit_csv_sha256") != audit_declared.get("completed_audit_csv", {}).get("sha256"):
        drift.append("completed_audit_csv_sha256")
    if summary.get("completed_audit_manifest_sha256") != audit_declared.get("completed_audit_manifest", {}).get("sha256"):
        drift.append("completed_audit_manifest_sha256")
    if drift:
        problems.append("summary provenance contradicts the bound authorization record: " + "; ".join(drift))


def _adapter_dir_relative(directory: Path, adapter_path: Any) -> str | None:
    """Canonical posix path of the adapter directory relative to the active run, or None."""
    if not isinstance(adapter_path, str) or not adapter_path.strip():
        return None
    candidate = Path(adapter_path)
    if not candidate.is_absolute():
        candidate = directory / candidate
    try:
        resolved = candidate.resolve()
        root = directory.resolve()
        if not resolved.is_relative_to(root):
            return None
    except (OSError, ValueError):
        return None
    return candidate.relative_to(directory).as_posix()


def _validate_reload_crosscheck(
    summary: Mapping[str, Any], active_run_id: str, directory: Path, problems: list[str]
) -> None:
    """Bind the summary reload claim to the manifested reload_verification.json payload."""
    reload_path = directory / "reload_verification.json"
    if not reload_path.is_file() or reload_path.stat().st_size == 0:
        return
    try:
        reload_record = json.loads(reload_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        problems.append(f"reload_verification.json is not valid JSON: {exc}")
        return
    if not isinstance(reload_record, dict):
        problems.append("reload_verification.json must contain a JSON object")
        return
    if reload_record.get("run_id") != active_run_id:
        problems.append(
            f"reload evidence run_id {reload_record.get('run_id')!r} does not match the active run {active_run_id!r}"
        )
    if reload_record.get("adapter_reloaded") is not True:
        problems.append("reload evidence does not confirm adapter reload success")
    if _adapter_dir_relative(directory, reload_record.get("adapter_path")) is None:
        problems.append("reload evidence adapter_path is missing or outside the active run directory")
    summary_reload = summary.get("reload_verification")
    if not isinstance(summary_reload, dict) or summary_reload.get("adapter_reloaded") is not True:
        problems.append("reload verification is not explicitly successful")
    elif reload_record.get("adapter_reloaded") is not True:
        problems.append("summary reload claim contradicts the manifested reload evidence")
    if isinstance(summary_reload, dict) and summary_reload.get("run_id") != active_run_id:
        problems.append("summary reload verification run_id does not match the active run")


def _validate_summary_payload_identities(
    summary: Mapping[str, Any], directory: Path, manifest: Mapping[str, Any], problems: list[str]
) -> None:
    """Reject summary identities that do not match the manifested payload hashes and sizes."""
    by_path = {str(entry.get("path")): entry for entry in manifest.get("artifacts", [])}
    drift: list[str] = []
    summary_reload = summary.get("reload_verification")
    adapter_rel = (
        _adapter_dir_relative(directory, summary_reload.get("adapter_path"))
        if isinstance(summary_reload, dict)
        else None
    )
    if adapter_rel is not None:
        for name, identity in (summary.get("adapter_identities") or {}).items():
            _check_manifest_identity(by_path, f"{adapter_rel}/{name}", identity, f"adapter_identities[{name}]", drift)
        for name, identity in (summary.get("tokenizer_identities") or {}).items():
            _check_manifest_identity(by_path, f"{adapter_rel}/{name}", identity, f"tokenizer_identities[{name}]", drift)
    _check_manifest_identity(
        by_path,
        "trainer_state.json",
        {"sha256": summary.get("trainer_state_identity"), "size_bytes": None},
        "trainer_state_identity",
        drift,
    )
    _check_manifest_identity(
        by_path,
        "training_log_history.json",
        {"sha256": summary.get("training_log_identity"), "size_bytes": None},
        "training_log_identity",
        drift,
    )
    if drift:
        problems.append("summary identities do not match the manifested payload hashes and sizes: " + "; ".join(drift))


def _check_manifest_identity(
    by_path: Mapping[str, Mapping[str, Any]],
    manifest_path: str,
    identity: Any,
    label: str,
    drift: list[str],
) -> None:
    if not isinstance(identity, Mapping):
        drift.append(f"{label} is not an identity object")
        return
    entry = by_path.get(manifest_path)
    if entry is None:
        drift.append(f"{label} references {manifest_path!r} which is not manifested")
        return
    if str(identity.get("sha256", "")) != str(entry.get("sha256", "")):
        drift.append(f"{label} sha256 does not match the manifested payload")
    size = identity.get("size_bytes")
    if size is not None and int(size) != int(entry.get("size_bytes", -1)):
        drift.append(f"{label} size does not match the manifested payload")


def _write_json_atomic(path: Path, value: Any) -> None:
    """Write a JSON record via a temporary file and atomic replace."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
