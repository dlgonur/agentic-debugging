from __future__ import annotations

import csv
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentic_debugger.training.patch_pilot import (
    MANUAL_VERDICT_ACCEPTED,
    MANUAL_VERDICT_REJECTED,
    CorpusBuildError,
    aggregate_lora_delta,
    build_corpus,
    create_non_held_out_verifier_smoke,
    filter_row,
    parse_unified_diff_strict,
    snapshot_trainable_lora_parameters,
    validate_completed_audits,
    validate_final_training_authorization,
)

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = ROOT / "experiments/qlora_patch_pilot_v1"


def _row(index: int, *, accepted: bool = True, repository: str | None = None) -> dict[str, str]:
    repo = repository or f"example/repository-{index:04d}"
    subject = f"Fix boundary error in counter {index}" if accepted else f"Document counter {index}"
    old = f"def counter_{index}(value):\n    return value - 1\n"
    new = f"def counter_{index}(value):\n    return value + 1\n"
    return {
        "commit": f"{index:040x}",
        "old_file": f"src/counter_{index}.py",
        "new_file": f"src/counter_{index}.py",
        "old_contents": old,
        "new_contents": new,
        "subject": subject,
        "message": subject,
        "lang": "Python",
        "license": "mit",
        "repos": repo,
    }


def _minimum_config(tmp_path: Path) -> Path:
    config = json.loads((EXPERIMENT / "transformation_config.json").read_text(encoding="utf-8"))
    config["preferred_counts"] = {"train": 5, "validation": 2}
    config["minimum_counts"] = {"train": 3, "validation": 1}
    config["audit"] = {"accepted_minimum": 2, "rejected_minimum": 1}
    path = tmp_path / "transform.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_strict_diff_parser_accepts_one_authorized_patch() -> None:
    patch = "--- a/fix.py\n+++ b/fix.py\n@@ -1 +1 @@\n-return 1\n+return 2\n"
    assert parse_unified_diff_strict(patch, ["fix.py"]) == patch


@pytest.mark.parametrize(
    "patch",
    [
        "Explanation\n--- a/fix.py\n+++ b/fix.py\n@@ -1 +1 @@\n-a\n+b\n",
        "```diff\n--- a/fix.py\n+++ b/fix.py\n@@ -1 +1 @@\n-a\n+b\n```",
        "--- a/fix.py\n+++ b/other.py\n@@ -1 +1 @@\n-a\n+b\n",
        "--- a/tests/test_fix.py\n+++ b/tests/test_fix.py\n@@ -1 +1 @@\n-a\n+b\n",
    ],
)
def test_strict_diff_parser_rejects_non_contract_output(patch: str) -> None:
    with pytest.raises(Exception):
        parse_unified_diff_strict(patch, ["fix.py"])


def test_corpus_build_is_repository_disjoint_and_external(tmp_path: Path) -> None:
    config = _minimum_config(tmp_path)
    rows = [_row(index) for index in range(7)] + [_row(100, accepted=False)]
    output = tmp_path / "output"
    summary = build_corpus(
        rows,
        repository_root=ROOT,
        output_dir=output,
        freeze_record_path=EXPERIMENT / "freeze_record.json",
        transformation_config_path=config,
        prompt_contract_path=EXPERIMENT / "prompt_contract.json",
    )
    assert summary["corpus_tier"] == "preferred"
    assert summary["train_examples"] == 5
    assert summary["validation_examples"] == 2
    train = [json.loads(line) for line in (output / "train.jsonl").read_text().splitlines()]
    validation = [json.loads(line) for line in (output / "validation.jsonl").read_text().splitlines()]
    train_repos = {row["provenance"]["repository"] for row in train}
    validation_repos = {row["provenance"]["repository"] for row in validation}
    assert train_repos.isdisjoint(validation_repos)
    assert (output / "accepted_audit.csv").exists()
    assert (output / "rejected_audit.csv").exists()
    assert (output / "external_artifacts.json").exists()


def test_duplicate_repository_keeps_only_one_example(tmp_path: Path) -> None:
    config = _minimum_config(tmp_path)
    rows = [_row(index) for index in range(6)]
    rows.append(_row(99, repository="example/repository-0000"))
    output = tmp_path / "output"
    build_corpus(
        rows,
        repository_root=ROOT,
        output_dir=output,
        freeze_record_path=EXPERIMENT / "freeze_record.json",
        transformation_config_path=config,
        prompt_contract_path=EXPERIMENT / "prompt_contract.json",
    )
    reasons = json.loads((output / "rejection_summary.json").read_text())["reasons"]
    assert reasons["repository_already_selected"] == 1


def _rewrite_audit_csv(path: Path, *, verdict: str | None = None, missing_field: str | None = None) -> None:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        rows = list(reader)
    default = MANUAL_VERDICT_ACCEPTED if "accepted" in path.name else MANUAL_VERDICT_REJECTED
    for row in rows:
        if missing_field is not None:
            row[missing_field] = ""
        else:
            row["manual_verdict"] = verdict or default
            row["manual_reason"] = "reviewed for smoke"
            row["reviewer"] = "automated-test-fixture"
            row["reviewed_at"] = "2026-08-04T00:00:00Z"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _built_output(tmp_path: Path) -> Path:
    output = tmp_path / "output"
    build_corpus(
        [_row(index) for index in range(7)] + [_row(100, accepted=False)],
        repository_root=ROOT,
        output_dir=output,
        freeze_record_path=EXPERIMENT / "freeze_record.json",
        transformation_config_path=_minimum_config(tmp_path),
        prompt_contract_path=EXPERIMENT / "prompt_contract.json",
    )
    return output


def test_audit_gate_fails_closed_until_manual_fields_are_completed(tmp_path: Path) -> None:
    config = _minimum_config(tmp_path)
    output = _built_output(tmp_path)
    with pytest.raises(CorpusBuildError):
        validate_completed_audits(output, config)
    for filename in ("accepted_audit.csv", "rejected_audit.csv"):
        _rewrite_audit_csv(output / filename)
    assert validate_completed_audits(output, config)["status"] == "COMPLETE"


def test_audit_gate_rejects_missing_manual_field(tmp_path: Path) -> None:
    config = _minimum_config(tmp_path)
    output = _built_output(tmp_path)
    _rewrite_audit_csv(output / "accepted_audit.csv")
    _rewrite_audit_csv(output / "rejected_audit.csv", missing_field="reviewer")
    with pytest.raises(CorpusBuildError, match="missing manual audit field"):
        validate_completed_audits(output, config)


def test_audit_gate_rejects_accepted_row_without_exact_accepted_verdict(tmp_path: Path) -> None:
    config = _minimum_config(tmp_path)
    output = _built_output(tmp_path)
    _rewrite_audit_csv(output / "accepted_audit.csv", verdict=MANUAL_VERDICT_REJECTED)
    _rewrite_audit_csv(output / "rejected_audit.csv")
    with pytest.raises(CorpusBuildError, match="does not equal the required"):
        validate_completed_audits(output, config)


def test_audit_gate_rejects_rejected_row_without_exact_rejected_verdict(tmp_path: Path) -> None:
    config = _minimum_config(tmp_path)
    output = _built_output(tmp_path)
    _rewrite_audit_csv(output / "accepted_audit.csv")
    _rewrite_audit_csv(output / "rejected_audit.csv", verdict=MANUAL_VERDICT_ACCEPTED)
    with pytest.raises(CorpusBuildError, match="does not equal the required"):
        validate_completed_audits(output, config)


def _independent_config(tmp_path: Path) -> Path:
    config = _minimum_config(tmp_path)
    data = json.loads(config.read_text(encoding="utf-8"))
    data["audit"]["audit_mode"] = "independent_ai"
    path = tmp_path / "transform-independent.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_completed_independent_audit(
    output: Path,
    *,
    reviewer: str = "FirstMate / GPT-5.6 Thinking",
    reviewer_type: str = "independent_ai_reviewer",
    audit_method: str = "owner-delegated independent FirstMate audit",
    verdict_overrides: dict[str, str] | None = None,
    missing_field: str | None = None,
    populate_human: str | None = None,
    reverse_order: bool = False,
    tamper: object | None = None,
) -> Path:
    accepted = [json.loads(line) for line in (output / "accepted_audit_sample.jsonl").open(encoding="utf-8")]
    rejected = [json.loads(line) for line in (output / "rejected_audit_sample.jsonl").open(encoding="utf-8")]
    rows: list[dict[str, str]] = []
    for index, sample in enumerate(accepted, 1):
        rows.append({
            "global_index": str(index),
            "packet": "accepted",
            "audit_index": str(index),
            "example_id": sample["example_id"],
            "repository": sample["repository"],
            "commit": sample["commit"],
            "file_path": sample["file_path"],
            "license": sample["license"],
            "subject": sample["subject"],
            "frozen_reason": "",
            "audit_method": audit_method,
            "audit_verdict": "ACCEPT",
            "audit_reason": "coherent defect-repair example",
            "audit_reviewer": reviewer,
            "audit_reviewer_type": reviewer_type,
            "audit_reviewed_at": "2026-08-05T00:00:00+03:00",
        })
    for index, sample in enumerate(rejected, 1):
        rows.append({
            "global_index": str(len(accepted) + index),
            "packet": "rejected",
            "audit_index": str(index),
            "example_id": sample.get("example_id", ""),
            "repository": "",
            "commit": sample["commit"],
            "file_path": sample["file_path"],
            "license": sample["license"],
            "subject": sample["subject"],
            "frozen_reason": sample["reason"],
            "audit_method": audit_method,
            "audit_verdict": "REJECT",
            "audit_reason": "frozen reason verified against row fields",
            "audit_reviewer": reviewer,
            "audit_reviewer_type": reviewer_type,
            "audit_reviewed_at": "2026-08-05T00:00:00+03:00",
        })
    for row in rows:
        row.setdefault("human_verdict", "")
        row.setdefault("human_reason", "")
        row.setdefault("human_reviewer", "")
        row.setdefault("human_reviewed_at", "")
        if verdict_overrides and row["example_id"] in verdict_overrides:
            row["audit_verdict"] = verdict_overrides[row["example_id"]]
        if missing_field is not None:
            row[missing_field] = ""
        if populate_human is not None:
            row[populate_human] = "FirstMate / GPT-5.6 Thinking"
        if tamper is not None:
            tamper(row)
    if reverse_order:
        rows.reverse()
    path = output / "completed_independent_audit.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_independent_audit_gate_accepts_reject_in_accepted_packet(tmp_path: Path) -> None:
    config = _independent_config(tmp_path)
    output = _built_output(tmp_path)
    accepted = [json.loads(line) for line in (output / "accepted_audit_sample.jsonl").open(encoding="utf-8")]
    completed = _write_completed_independent_audit(
        output, verdict_overrides={accepted[0]["example_id"]: "REJECT"}
    )
    result = validate_completed_audits(output, config, completed_audit_path=completed)
    assert result["status"] == "COMPLETE"
    assert result["audit_mode"] == "independent_ai"
    assert result["accepted_packet_accept"] == 1
    assert result["accepted_packet_reject"] == 1
    assert result["rejected_packet_reject"] == 1


def test_independent_audit_gate_requires_completed_csv(tmp_path: Path) -> None:
    config = _independent_config(tmp_path)
    output = _built_output(tmp_path)
    with pytest.raises(CorpusBuildError, match="requires the completed independent audit CSV"):
        validate_completed_audits(output, config)


def test_independent_audit_gate_rejects_missing_audit_field(tmp_path: Path) -> None:
    config = _independent_config(tmp_path)
    output = _built_output(tmp_path)
    completed = _write_completed_independent_audit(output, missing_field="audit_reviewer")
    with pytest.raises(CorpusBuildError, match="missing independent audit field"):
        validate_completed_audits(output, config, completed_audit_path=completed)


def test_independent_audit_gate_rejects_coding_agent_as_reviewer(tmp_path: Path) -> None:
    config = _independent_config(tmp_path)
    output = _built_output(tmp_path)
    completed = _write_completed_independent_audit(output, reviewer="agentic-coding-agent")
    with pytest.raises(CorpusBuildError, match="not an independent reviewer"):
        validate_completed_audits(output, config, completed_audit_path=completed)


def test_independent_audit_gate_rejects_non_independent_reviewer_type(tmp_path: Path) -> None:
    config = _independent_config(tmp_path)
    output = _built_output(tmp_path)
    completed = _write_completed_independent_audit(output, reviewer_type="human_reviewer")
    with pytest.raises(CorpusBuildError, match="does not equal"):
        validate_completed_audits(output, config, completed_audit_path=completed)


def test_independent_audit_gate_rejects_populated_human_field(tmp_path: Path) -> None:
    config = _independent_config(tmp_path)
    output = _built_output(tmp_path)
    completed = _write_completed_independent_audit(output, populate_human="human_reviewer")
    with pytest.raises(CorpusBuildError, match="human field human_reviewer is populated"):
        validate_completed_audits(output, config, completed_audit_path=completed)


def test_independent_audit_gate_rejects_reordered_rows(tmp_path: Path) -> None:
    config = _independent_config(tmp_path)
    output = _built_output(tmp_path)
    completed = _write_completed_independent_audit(output, reverse_order=True)
    with pytest.raises(CorpusBuildError, match="drift"):
        validate_completed_audits(output, config, completed_audit_path=completed)


def test_independent_audit_gate_rejects_unknown_verdict(tmp_path: Path) -> None:
    config = _independent_config(tmp_path)
    output = _built_output(tmp_path)
    accepted = [json.loads(line) for line in (output / "accepted_audit_sample.jsonl").open(encoding="utf-8")]
    completed = _write_completed_independent_audit(
        output, verdict_overrides={accepted[0]["example_id"]: "UNCLEAR"}
    )
    with pytest.raises(CorpusBuildError, match="not ACCEPT or REJECT"):
        validate_completed_audits(output, config, completed_audit_path=completed)


def test_independent_audit_mode_pending_status_in_build(tmp_path: Path) -> None:
    config = _independent_config(tmp_path)
    output = tmp_path / "output"
    build_corpus(
        [_row(index) for index in range(7)] + [_row(100, accepted=False)],
        repository_root=ROOT,
        output_dir=output,
        freeze_record_path=EXPERIMENT / "freeze_record.json",
        transformation_config_path=config,
        prompt_contract_path=EXPERIMENT / "prompt_contract.json",
    )
    summary = json.loads((output / "audit_summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "PENDING_INDEPENDENT_AUDIT"
    assert summary["audit_mode"] == "independent_ai"


def _independent_validated(tmp_path: Path) -> tuple[Path, Path]:
    config = _independent_config(tmp_path)
    output = _built_output(tmp_path)
    completed = _write_completed_independent_audit(output)
    assert validate_completed_audits(output, config, completed_audit_path=completed)["status"] == "COMPLETE"
    return config, output


def test_independent_audit_rejects_wrong_packet_label(tmp_path: Path) -> None:
    config, output = _independent_validated(tmp_path)
    completed = _write_completed_independent_audit(output, tamper=lambda row: row.update(packet="reviewed"))
    with pytest.raises(CorpusBuildError, match="packet label is not 'accepted'"):
        validate_completed_audits(output, config, completed_audit_path=completed)


def test_independent_audit_rejects_wrong_global_index(tmp_path: Path) -> None:
    config, output = _independent_validated(tmp_path)
    completed = _write_completed_independent_audit(output, tamper=lambda row: row.update(global_index="99"))
    with pytest.raises(CorpusBuildError, match="index fields drift"):
        validate_completed_audits(output, config, completed_audit_path=completed)


def test_independent_audit_rejects_wrong_audit_index(tmp_path: Path) -> None:
    config, output = _independent_validated(tmp_path)
    completed = _write_completed_independent_audit(output, tamper=lambda row: row.update(audit_index="7"))
    with pytest.raises(CorpusBuildError, match="index fields drift"):
        validate_completed_audits(output, config, completed_audit_path=completed)


def test_independent_audit_rejects_missing_audit_method(tmp_path: Path) -> None:
    config, output = _independent_validated(tmp_path)
    completed = _write_completed_independent_audit(output, audit_method="")
    with pytest.raises(CorpusBuildError, match="not canonical"):
        validate_completed_audits(output, config, completed_audit_path=completed)


def test_independent_audit_rejects_wrong_audit_method(tmp_path: Path) -> None:
    config, output = _independent_validated(tmp_path)
    completed = _write_completed_independent_audit(output, audit_method="human manual audit")
    with pytest.raises(CorpusBuildError, match="audit_method"):
        validate_completed_audits(output, config, completed_audit_path=completed)


def test_independent_audit_rejects_whitespace_padded_verdict(tmp_path: Path) -> None:
    config, output = _independent_validated(tmp_path)
    completed = _write_completed_independent_audit(output, tamper=lambda row: row.update(audit_verdict=" ACCEPT"))
    with pytest.raises(CorpusBuildError, match="not canonical"):
        validate_completed_audits(output, config, completed_audit_path=completed)


def test_independent_audit_rejects_whitespace_padded_reviewer_type(tmp_path: Path) -> None:
    config, output = _independent_validated(tmp_path)
    completed = _write_completed_independent_audit(
        output, tamper=lambda row: row.update(audit_reviewer_type=" independent_ai_reviewer")
    )
    with pytest.raises(CorpusBuildError, match="not canonical"):
        validate_completed_audits(output, config, completed_audit_path=completed)


def test_existing_verifier_non_held_out_smoke(tmp_path: Path) -> None:
    result = create_non_held_out_verifier_smoke(ROOT, tmp_path / "smoke.json")
    assert result["held_out_task_used"] is False
    assert result["status"] == "COMPLETED"
    assert result["outcome"] == "RESOLVED"
    assert result["canonical_fixture_unchanged"] is True
    assert result["workspace_cleaned"] is True


def test_freeze_record_recomputes_without_drift() -> None:
    from agentic_debugger.training.patch_pilot import verify_freeze_record
    result = verify_freeze_record(ROOT, EXPERIMENT / "freeze_record.json")
    assert result["status"] == "LOCKED"
    assert result["checks"] == 25
    assert result["failed"] == []
    actual_head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert result["runtime"]["execution_head"] == actual_head
    detached = (
        subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=ROOT, capture_output=True).returncode != 0
    )
    assert result["runtime"]["detached"] is detached
    assert isinstance(result["runtime"]["dirty"], bool)


def _freeze_snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative in ("prompt_contract.json", "transformation_config.json", "training_config.json", "generation_config.json"):
        source = EXPERIMENT / relative
        target = root / "experiments/qlora_patch_pilot_v1" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for fixture in (
        "curated-none-handling-001",
        "curated-off-by-one-002",
        "curated-wrong-branch-003",
        "curated-mutation-alias-004",
        "curated-caller-callee-005",
    ):
        shutil.copytree(ROOT / f"agentic_debugger/datasets/curated/{fixture}", root / f"agentic_debugger/datasets/curated/{fixture}")
    return root


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=test-fixture", "-c", "user.email=test-fixture@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


def _commit_snapshot(root: Path, *, message: str = "snapshot") -> str:
    _git("init", "-q", "-b", "main", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", message, cwd=root)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def _freeze_record_with_base(base_commit: str, target: Path) -> Path:
    record = json.loads((EXPERIMENT / "freeze_record.json").read_text(encoding="utf-8"))
    record["repository_baseline"] = {**record["repository_baseline"], "base_commit": base_commit, "relationship": "required_ancestor"}
    target.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return target


def test_freeze_contract_base_commit_passes(tmp_path: Path) -> None:
    from agentic_debugger.training.patch_pilot import verify_freeze_record
    root = _freeze_snapshot(tmp_path)
    base = _commit_snapshot(root)
    record = _freeze_record_with_base(base, tmp_path / "freeze_record.json")
    result = verify_freeze_record(root, record)
    assert result["status"] == "LOCKED"
    assert result["checks"] == 25
    assert result["runtime"]["execution_head"] == base[:7]
    assert result["runtime"]["detached"] is False
    assert result["runtime"]["dirty"] is False


def test_freeze_contract_descendant_commit_passes(tmp_path: Path) -> None:
    from agentic_debugger.training.patch_pilot import verify_freeze_record
    root = _freeze_snapshot(tmp_path)
    base = _commit_snapshot(root)
    (root / "candidate_commit_marker.txt").write_text("committed candidate marker", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "committed candidate", cwd=root)
    record = _freeze_record_with_base(base, tmp_path / "freeze_record.json")
    result = verify_freeze_record(root, record)
    assert result["status"] == "LOCKED"
    assert result["checks"] == 25
    assert result["runtime"]["dirty"] is False


def test_freeze_contract_unrelated_history_fails(tmp_path: Path) -> None:
    from agentic_debugger.training.patch_pilot import verify_freeze_record
    first = _freeze_snapshot(tmp_path)
    base = _commit_snapshot(first)
    unrelated = _freeze_snapshot(tmp_path / "unrelated")
    _commit_snapshot(unrelated, message="unrelated history")
    record = _freeze_record_with_base(base, tmp_path / "freeze_record.json")
    with pytest.raises(CorpusBuildError, match="required_ancestor"):
        verify_freeze_record(unrelated, record)


def test_freeze_contract_gitless_snapshot_validates_24_identities(tmp_path: Path) -> None:
    from agentic_debugger.training.patch_pilot import verify_freeze_record
    root = _freeze_snapshot(tmp_path)
    record = _freeze_record_with_base("66fb5d5", tmp_path / "freeze_record.json")
    result = verify_freeze_record(root, record)
    assert result["status"] == "LOCKED"
    assert result["checks"] == 24
    assert result["failed"] == []
    assert result["runtime"] == {"execution_head": None, "execution_branch": None, "detached": None, "dirty": None}


def test_package_exports_lora_delta_helpers() -> None:
    from agentic_debugger.training import aggregate_lora_delta, snapshot_trainable_lora_parameters
    assert callable(snapshot_trainable_lora_parameters)
    assert callable(aggregate_lora_delta)


def test_generation_record_is_write_once(tmp_path: Path) -> None:
    from agentic_debugger.training.patch_pilot import record_generation_once
    kwargs = dict(
        output_dir=tmp_path,
        condition="base_7b",
        task_id="non-heldout-smoke-001",
        raw_output="--- a/fix.py\n+++ b/fix.py\n@@ -1 +1 @@\n-a\n+b\n",
        prompt_sha256="1" * 64,
        generation_config_sha256="2" * 64,
        model_repository="example/model",
        model_revision="3" * 40,
        adapter_sha256=None,
    )
    first = record_generation_once(**kwargs)
    assert first["generation_count"] == 1
    with pytest.raises(CorpusBuildError):
        record_generation_once(**kwargs)


def test_notebook_retains_scientific_stop_gate() -> None:
    notebook = json.loads((EXPERIMENT / "colab/agentic_debugging_qlora_pilot.ipynb").read_text(encoding="utf-8"))
    sources = "\n".join("".join(cell.get("source", [])) if isinstance(cell.get("source"), list) else str(cell.get("source", "")) for cell in notebook["cells"])
    assert "Qwen2.5-Coder-1.5B" not in sources
    assert "FINAL_TRAINING_AUTHORIZED = False" in sources
    assert "HELD_OUT_GENERATION_AUTHORIZED = False" in sources
    assert "STOPPED_BEFORE_FINAL_TRAINING_AND_HELD_OUT_GENERATION" in sources


def test_notebook_retains_refreshed_baseline_and_aggregate_lora_smoke() -> None:
    notebook = json.loads((EXPERIMENT / "colab/agentic_debugging_qlora_pilot.ipynb").read_text(encoding="utf-8"))
    sources = "\n".join("".join(cell.get("source", [])) if isinstance(cell.get("source"), list) else str(cell.get("source", "")) for cell in notebook["cells"])
    assert "'base_commit'" in sources
    assert "'required_ancestor'" in sources
    assert "'66fb5d5'" in sources
    assert "0ed8e74" not in sources
    assert "repository_verification" in sources
    assert "snapshot_trainable_lora_parameters" in sources
    assert "aggregate_lora_delta" in sources
    for field in ("trainable_tensors_checked", "changed_tensors", "aggregate_delta_l2", "delta_finite"):
        assert field in sources
    assert "No LoRA tensor changed during the smoke step." in sources
    assert "Aggregate LoRA delta is not positive." in sources


def test_newline_complete_row_renders_valid_diff() -> None:
    from agentic_debugger.training.patch_pilot import load_json
    config = load_json(EXPERIMENT / "transformation_config.json")
    prompt_contract = load_json(EXPERIMENT / "prompt_contract.json")
    held = {"source_hashes": [], "patch_hashes": [], "source_ngrams": [], "excluded_repositories": []}
    candidate, rejection = filter_row(
        _row(1), row_index=0, config=config, prompt_contract=prompt_contract, held_out_fingerprints=held
    )
    assert rejection is None
    assert candidate is not None
    assert candidate.patch.endswith("\n")
    assert parse_unified_diff_strict(candidate.patch, [candidate.file_path]) == candidate.patch


@pytest.mark.parametrize("old_trailing", [False, True])
@pytest.mark.parametrize("new_trailing", [False, True])
def test_missing_terminal_newline_row_is_rejected(old_trailing: bool, new_trailing: bool) -> None:
    from agentic_debugger.training.patch_pilot import load_json
    config = load_json(EXPERIMENT / "transformation_config.json")
    prompt_contract = load_json(EXPERIMENT / "prompt_contract.json")
    held = {"source_hashes": [], "patch_hashes": [], "source_ngrams": [], "excluded_repositories": []}
    row = _row(2)
    row["old_contents"] = row["old_contents"] if old_trailing else "def counter_2(value):\n    return value - 1"
    row["new_contents"] = row["new_contents"] if new_trailing else "def counter_2(value):\n    return value + 1"
    candidate, rejection = filter_row(
        row, row_index=0, config=config, prompt_contract=prompt_contract, held_out_fingerprints=held
    )
    if old_trailing and new_trailing:
        assert candidate is not None
        assert rejection is None
    else:
        assert candidate is None
        assert rejection is not None
        assert rejection["reason"] == "missing_terminal_newline"


def test_no_malformed_completion_enters_train_or_validation(tmp_path: Path) -> None:
    rows = [_row(index) for index in range(7)]
    bad_old = _row(40)
    bad_old["old_contents"] = "def counter_40(value):\n    return value - 1"
    bad_new = _row(41)
    bad_new["new_contents"] = "def counter_41(value):\n    return value + 1"
    rows.extend([bad_old, bad_new, _row(100, accepted=False)])
    output = tmp_path / "output"
    build_corpus(
        rows,
        repository_root=ROOT,
        output_dir=output,
        freeze_record_path=EXPERIMENT / "freeze_record.json",
        transformation_config_path=_minimum_config(tmp_path),
        prompt_contract_path=EXPERIMENT / "prompt_contract.json",
    )
    reasons = json.loads((output / "rejection_summary.json").read_text())["reasons"]
    assert reasons["missing_terminal_newline"] == 2
    bad_commits = {f"{index:040x}" for index in (40, 41)}
    accepted = [json.loads(line) for line in (output / "accepted_rows.jsonl").read_text().splitlines()]
    assert not any(row["commit"] in bad_commits for row in accepted)
    for filename in ("train.jsonl", "validation.jsonl"):
        for line in (output / filename).read_text().splitlines():
            completion = json.loads(line)["completion"]
            assert completion.endswith("\n")
            assert parse_unified_diff_strict(completion, [json.loads(line)["provenance"]["file_path"]]) == completion


def test_corpus_build_requires_absent_or_empty_output_directory(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "stale.txt").write_text("stale", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="absent or empty"):
        build_corpus(
            [_row(index) for index in range(7)],
            repository_root=ROOT,
            output_dir=output,
            freeze_record_path=EXPERIMENT / "freeze_record.json",
            transformation_config_path=_minimum_config(tmp_path),
            prompt_contract_path=EXPERIMENT / "prompt_contract.json",
        )
    assert (output / "stale.txt").read_text(encoding="utf-8") == "stale"
    assert not (output / "train.jsonl").exists()
    (output / "stale.txt").unlink()
    summary = build_corpus(
        [_row(index) for index in range(7)],
        repository_root=ROOT,
        output_dir=output,
        freeze_record_path=EXPERIMENT / "freeze_record.json",
        transformation_config_path=_minimum_config(tmp_path),
        prompt_contract_path=EXPERIMENT / "prompt_contract.json",
    )
    assert summary["train_examples"] == 5


class _FakeModel:
    def __init__(self, params: dict[str, object]) -> None:
        self._params = dict(params)

    def named_parameters(self):
        return self._params.items()


def _fake_lora_models(*, changed: bool) -> tuple[_FakeModel, _FakeModel]:
    import torch

    before = {
        "base.weight": torch.ones(2, 2, requires_grad=False),
        "q_proj.lora_A.default": torch.zeros(4, 4, requires_grad=True),
        "q_proj.lora_B.default": torch.zeros(4, 4, requires_grad=True),
        "v_proj.lora_A.default": torch.eye(2, requires_grad=True),
    }
    after = {name: value.clone() for name, value in before.items()}
    if changed:
        after["q_proj.lora_A.default"][0, 0] = 1.0
    return _FakeModel(before), _FakeModel(after)


def test_lora_delta_aggregate_requires_positive_finite_delta() -> None:
    before_model, after_model = _fake_lora_models(changed=True)
    snapshot = snapshot_trainable_lora_parameters(before_model)
    assert set(snapshot) == {"q_proj.lora_A.default", "q_proj.lora_B.default", "v_proj.lora_A.default"}
    result = aggregate_lora_delta(snapshot, after_model)
    assert result["trainable_tensors_checked"] == 3
    assert result["changed_tensors"] == 1
    assert result["aggregate_delta_l2"] == pytest.approx(1.0)
    assert result["delta_finite"] is True


def test_lora_delta_aggregate_fails_closed_on_zero_or_missing_tensors() -> None:
    before_model, unchanged_model = _fake_lora_models(changed=False)
    snapshot = snapshot_trainable_lora_parameters(before_model)
    result = aggregate_lora_delta(snapshot, unchanged_model)
    assert result["changed_tensors"] == 0
    assert result["aggregate_delta_l2"] == 0.0
    assert result["delta_finite"] is True
    empty = aggregate_lora_delta({}, before_model)
    assert empty["trainable_tensors_checked"] == 0
    assert empty["aggregate_delta_l2"] is None
    assert empty["delta_finite"] is False

def _auth_fixture(tmp_path: Path) -> Path:
    source = json.loads((EXPERIMENT / "final_training_authorization.json").read_text(encoding="utf-8"))
    path = tmp_path / "final_training_authorization.json"
    path.write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
    return path


def _auth_corpus_fixture(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    (corpus / "corpus_summary.json").write_text(json.dumps({
        "corpus_tier": "minimum", "train_examples": 1000, "validation_examples": 150,
    }, indent=2) + "\n", encoding="utf-8")
    (corpus / "dedup_report.json").write_text(json.dumps({
        "repository_overlap": [], "held_out_exact_matches_accepted": 0, "held_out_near_matches_accepted": 0,
    }, indent=2) + "\n", encoding="utf-8")
    return corpus


def _mutate_auth(tmp_path: Path, **changes: object) -> Path:
    data = json.loads(_auth_fixture(tmp_path).read_text(encoding="utf-8"))
    for key, value in changes.items():
        data[key] = value
    path = tmp_path / "mutated_authorization.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _validate_auth(tmp_path: Path, path: Path) -> dict:
    return validate_final_training_authorization(path, repository_root=ROOT, corpus_dir=_auth_corpus_fixture(tmp_path))


def test_final_training_authorization_valid(tmp_path: Path) -> None:
    result = _validate_auth(tmp_path, _auth_fixture(tmp_path))
    assert result["status"] == "COMPLETE"
    assert result["authorization_scope"] == "final_training_only"
    assert result["authorized"] is True
    assert result["held_out_generation_authorized"] is False


def test_final_training_authorization_missing_record(tmp_path: Path) -> None:
    with pytest.raises(CorpusBuildError, match="authorization record missing"):
        _validate_auth(tmp_path, tmp_path / "does-not-exist.json")


def test_final_training_authorization_rejects_unauthorized(tmp_path: Path) -> None:
    path = _mutate_auth(tmp_path, authorized=False)
    with pytest.raises(CorpusBuildError, match="authorized is not true"):
        _validate_auth(tmp_path, path)


def test_final_training_authorization_rejects_held_out_true(tmp_path: Path) -> None:
    path = _mutate_auth(tmp_path, held_out_generation_authorized=True)
    with pytest.raises(CorpusBuildError, match="held_out_generation_authorized is not false"):
        _validate_auth(tmp_path, path)


def test_final_training_authorization_rejects_wrong_experiment(tmp_path: Path) -> None:
    path = _mutate_auth(tmp_path, experiment_id="some-other-experiment")
    with pytest.raises(CorpusBuildError, match="experiment_id"):
        _validate_auth(tmp_path, path)


def test_final_training_authorization_rejects_wrong_approver(tmp_path: Path) -> None:
    path = _mutate_auth(tmp_path, authorized_by="agentic-coding-agent")
    with pytest.raises(CorpusBuildError, match="authorized_by"):
        _validate_auth(tmp_path, path)


def test_final_training_authorization_rejects_config_hash_drift(tmp_path: Path) -> None:
    data = json.loads(_auth_fixture(tmp_path).read_text(encoding="utf-8"))
    data["configuration_identities"]["training"] = "0" * 64
    path = tmp_path / "drifted.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="configuration_identity.training"):
        _validate_auth(tmp_path, path)


def test_final_training_authorization_rejects_audit_result_drift(tmp_path: Path) -> None:
    data = json.loads(_auth_fixture(tmp_path).read_text(encoding="utf-8"))
    data["audit_result"]["accepted_packet_accept"] = 50
    path = tmp_path / "audit-drift.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="audit_result.accepted_packet_accept"):
        _validate_auth(tmp_path, path)


def test_final_training_authorization_rejects_corpus_count_drift(tmp_path: Path) -> None:
    corpus = _auth_corpus_fixture(tmp_path)
    (corpus / "corpus_summary.json").write_text(json.dumps({
        "corpus_tier": "minimum", "train_examples": 1500, "validation_examples": 200,
    }) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="corpus counts"):
        validate_final_training_authorization(_auth_fixture(tmp_path), repository_root=ROOT, corpus_dir=corpus)


def test_final_training_authorization_rejects_top_up_claim(tmp_path: Path) -> None:
    data = json.loads(_auth_fixture(tmp_path).read_text(encoding="utf-8"))
    data["corpus"] = {**data["corpus"], "top_up": True}
    path = tmp_path / "topup.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="top_up"):
        _validate_auth(tmp_path, path)
