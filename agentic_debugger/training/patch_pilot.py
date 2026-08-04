"""Deterministic data and smoke utilities for the QLoRA patch pilot.

The module intentionally does not import model-training libraries.  It creates
small, reviewable records and writes dataset contents only to an operator-owned
external output directory.  The existing evaluation verifier remains the sole
correctness authority for generated patches.
"""

from __future__ import annotations

import ast
import csv
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


def validate_final_training_authorization(
    authorization_path: str | Path,
    *,
    repository_root: str | Path,
    corpus_dir: str | Path,
) -> dict[str, Any]:
    """Fail-closed validation of the separate final-training authorization record."""
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
    expected_audit = {
        "accepted_packet_accept": 39,
        "accepted_packet_reject": 11,
        "rejected_packet_accept": 0,
        "rejected_packet_reject": 25,
    }
    audit_result = authorization.get("audit_result", {})
    for key, value in expected_audit.items():
        if audit_result.get(key) != value:
            problems.append(f"audit_result.{key} {audit_result.get(key)!r} does not equal {value}")
    corpus = authorization.get("corpus", {})
    if corpus.get("tier") != "minimum" or corpus.get("train") != 1000 or corpus.get("validation") != 150:
        problems.append(f"corpus declaration {corpus!r} does not match the accepted 1000/150 minimum corpus")
    if authorization.get("top_up") not in (False, None):
        problems.append("top_up must not be declared")
    if corpus.get("top_up") not in (False, None):
        problems.append("corpus.top_up must not be declared")
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
    for key, section in (("prompt_contract", "prompt_contract"), ("transformation", "transformation"), ("training", "training"), ("generation", "generation")):
        configured = Path(freeze[section]["path"])
        path = configured if configured.is_absolute() else root / configured
        actual = sha256_bytes(canonical_json_bytes(load_json(path)))
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
        if summary.get("train_examples") != 1000 or summary.get("validation_examples") != 150:
            problems.append(f"corpus counts {summary.get('train_examples')}/{summary.get('validation_examples')} do not match 1000/150")
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
        "authorization_scope": FINAL_TRAINING_SCOPE,
        "authorized": True,
        "held_out_generation_authorized": False,
        "base_versus_tuned_evaluation_authorized": False,
        "authorized_by": FINAL_TRAINING_APPROVER,
        "audit_result": expected_audit,
        "corpus": {"tier": "minimum", "train": 1000, "validation": 150},
        "status": "COMPLETE",
    }
