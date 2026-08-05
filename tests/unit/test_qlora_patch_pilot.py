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

import hashlib
import json
from pathlib import Path

import pytest

from agentic_debugger.training.patch_pilot import (
    FINAL_TRAINING_FINAL_STATUS,
    CorpusBuildError,
    create_final_training_run,
    validate_completed_audits,
    validate_final_training_authorization,
    write_external_manifest,
    write_final_run_status,
    write_payload_manifest,
)

AUTH_TEST_ANCHOR = "def _auth_fixture(tmp_path: Path) -> Path:"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ident(path: Path) -> dict:
    data = path.read_bytes()
    return {"size_bytes": len(data), "sha256": _sha256_bytes(data)}


def _auth_fixture_full(tmp_path: Path) -> dict:
    """Build a coherent mini authorization record plus real mini artifact files."""
    config = _independent_config(tmp_path)
    output = _built_output(tmp_path)
    train_path = tmp_path / "train.jsonl"
    validation_path = tmp_path / "validation.jsonl"
    train_path.write_text("\n".join(json.dumps({"x": i}) for i in range(3)) + "\n", encoding="utf-8")
    validation_path.write_text("\n".join(json.dumps({"x": i}) for i in range(2)) + "\n", encoding="utf-8")
    (output / "corpus_summary.json").write_text(
        json.dumps({"corpus_tier": "minimum", "train_examples": 3, "validation_examples": 2}) + "\n",
        encoding="utf-8",
    )
    (output / "dedup_report.json").write_text(
        json.dumps({"repository_overlap": []}) + "\n", encoding="utf-8"
    )
    completed = _write_completed_independent_audit(output)
    audit_validation = validate_completed_audits(output, config, completed_audit_path=completed)
    audit_manifest = tmp_path / "audit_manifest.json"
    audit_manifest.write_text(json.dumps({"schema_version": "independent-audit-package-v1"}) + "\n", encoding="utf-8")
    corpus_manifest = output / "external_artifacts.json"
    record = json.loads((EXPERIMENT / "final_training_authorization.json").read_text(encoding="utf-8"))
    record["corpus"] = {"tier": "minimum", "train": 3, "validation": 2, "top_up": False, "note": "test fixture"}
    record["corpus_artifacts"] = {
        "train_jsonl": {"logical_path": "corpus/train.jsonl", "rows": 3, **_ident(train_path)},
        "validation_jsonl": {"logical_path": "corpus/validation.jsonl", "rows": 2, **_ident(validation_path)},
        "corpus_manifest": {"logical_path": "corpus/external_artifacts.json", **_ident(corpus_manifest)},
    }
    record["audit_artifacts"] = {
        "completed_audit_csv": {"logical_path": "independent-audit/firstmate_independent_audit_completed.csv", **_ident(completed)},
        "completed_audit_manifest": {"logical_path": "independent-audit/firstmate_independent_audit_manifest.json", **_ident(audit_manifest)},
    }
    record["audit_result"] = {
        "total_rows": audit_validation["accepted_packet_total"] + audit_validation["rejected_packet_total"],
        "accepted_packet_rows": audit_validation["accepted_packet_total"],
        "rejected_packet_rows": audit_validation["rejected_packet_total"],
        "accepted_packet_accept": audit_validation["accepted_packet_accept"],
        "accepted_packet_reject": audit_validation["accepted_packet_reject"],
        "rejected_packet_accept": audit_validation["rejected_packet_accept"],
        "rejected_packet_reject": audit_validation["rejected_packet_reject"],
        "reviewer": "FirstMate / GPT-5.6 Thinking",
        "reviewer_type": "independent_ai_reviewer",
    }
    auth_path = tmp_path / "authorization.json"
    auth_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return {
        "auth": auth_path,
        "config": config,
        "corpus_dir": output,
        "train": train_path,
        "validation": validation_path,
        "corpus_manifest": corpus_manifest,
        "audit_csv": completed,
        "audit_manifest": audit_manifest,
    }


def _validate_auth_fixture(tmp_path: Path, fixture: dict) -> dict:
    return validate_final_training_authorization(
        fixture["auth"],
        repository_root=ROOT,
        corpus_dir=fixture["corpus_dir"],
        transformation_config_path=fixture["config"],
        train_jsonl=fixture["train"],
        validation_jsonl=fixture["validation"],
        corpus_manifest=fixture["corpus_manifest"],
        completed_audit_csv=fixture["audit_csv"],
        completed_audit_manifest=fixture["audit_manifest"],
    )


def _write_auth(tmp_path: Path, fixture: dict, **mutations) -> Path:
    record = json.loads(fixture["auth"].read_text(encoding="utf-8"))
    for key, value in mutations.items():
        record[key] = value
    path = tmp_path / "mutated-auth.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def test_final_training_auth_valid_with_real_file_identities(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    result = _validate_auth_fixture(tmp_path, fixture)
    assert result["status"] == "COMPLETE"
    assert result["authorization_sha256"]
    assert result["row_counts"] == {"train": 3, "validation": 2}
    assert result["audit_counts"]["total_rows"] == 3
    assert result["corpus_artifact_identities"]["train_jsonl"]["sha256"] == _sha256_bytes(fixture["train"].read_bytes())
    assert result["audit_artifact_identities"]["completed_audit_csv"]["sha256"] == _sha256_bytes(fixture["audit_csv"].read_bytes())


def test_final_training_auth_missing_authorization(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    missing = tmp_path / "nope.json"
    with pytest.raises(CorpusBuildError, match="authorization record missing"):
        validate_final_training_authorization(
            missing, repository_root=ROOT, corpus_dir=fixture["corpus_dir"],
            transformation_config_path=fixture["config"], train_jsonl=fixture["train"],
            validation_jsonl=fixture["validation"], corpus_manifest=fixture["corpus_manifest"],
            completed_audit_csv=fixture["audit_csv"], completed_audit_manifest=fixture["audit_manifest"],
        )


def test_final_training_auth_missing_train_artifact(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    fixture["train"].unlink()
    with pytest.raises(CorpusBuildError, match="train_jsonl artifact missing"):
        _validate_auth_fixture(tmp_path, fixture)


def test_final_training_auth_train_sha_mismatch(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    fixture["train"].write_text("\n".join(json.dumps({"x": i}) for i in range(3, 6)) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="train_jsonl sha256 mismatch"):
        _validate_auth_fixture(tmp_path, fixture)


def test_final_training_auth_validation_sha_mismatch(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    fixture["validation"].write_text("\n".join(json.dumps({"x": i}) for i in range(2, 4)) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="validation_jsonl sha256 mismatch"):
        _validate_auth_fixture(tmp_path, fixture)


def test_final_training_auth_corpus_manifest_sha_mismatch(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    fixture["corpus_manifest"].write_text('{"tampered": true}\n', encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="corpus_manifest sha256 mismatch"):
        _validate_auth_fixture(tmp_path, fixture)


def test_final_training_auth_train_row_count_mismatch(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    fixture["train"].write_text("\n".join(json.dumps({"x": i}) for i in range(4)) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="train_jsonl row-count mismatch"):
        _validate_auth_fixture(tmp_path, fixture)


def test_final_training_auth_validation_row_count_mismatch(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    fixture["validation"].write_text("\n".join(json.dumps({"x": i}) for i in range(3)) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="validation_jsonl row-count mismatch"):
        _validate_auth_fixture(tmp_path, fixture)


def test_final_training_auth_train_byte_size_mismatch(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    record = json.loads(fixture["auth"].read_text(encoding="utf-8"))
    record["corpus_artifacts"]["train_jsonl"]["size_bytes"] = record["corpus_artifacts"]["train_jsonl"]["size_bytes"] + 1
    path = tmp_path / "size-mut.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="train_jsonl byte-size mismatch"):
        validate_final_training_authorization(
            path, repository_root=ROOT, corpus_dir=fixture["corpus_dir"],
            transformation_config_path=fixture["config"], train_jsonl=fixture["train"],
            validation_jsonl=fixture["validation"], corpus_manifest=fixture["corpus_manifest"],
            completed_audit_csv=fixture["audit_csv"], completed_audit_manifest=fixture["audit_manifest"],
        )


def test_final_training_auth_audit_csv_sha_mismatch(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    with fixture["audit_csv"].open("a", encoding="utf-8") as handle:
        handle.write("tampered\n")
    with pytest.raises(CorpusBuildError, match="completed_audit_csv sha256 mismatch"):
        _validate_auth_fixture(tmp_path, fixture)


def test_final_training_auth_audit_manifest_sha_mismatch(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    fixture["audit_manifest"].write_text('{"tampered": true}\n', encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="completed_audit_manifest sha256 mismatch"):
        _validate_auth_fixture(tmp_path, fixture)


def test_final_training_auth_audit_reviewer_mismatch(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    record = json.loads(fixture["auth"].read_text(encoding="utf-8"))
    record["audit_result"]["reviewer"] = "agentic-coding-agent"
    path = tmp_path / "rev.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="reviewer"):
        validate_final_training_authorization(
            path, repository_root=ROOT, corpus_dir=fixture["corpus_dir"],
            transformation_config_path=fixture["config"], train_jsonl=fixture["train"],
            validation_jsonl=fixture["validation"], corpus_manifest=fixture["corpus_manifest"],
            completed_audit_csv=fixture["audit_csv"], completed_audit_manifest=fixture["audit_manifest"],
        )


def test_final_training_auth_audit_reviewer_type_mismatch(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    record = json.loads(fixture["auth"].read_text(encoding="utf-8"))
    record["audit_result"]["reviewer_type"] = "human_reviewer"
    path = tmp_path / "revtype.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="reviewer_type"):
        validate_final_training_authorization(
            path, repository_root=ROOT, corpus_dir=fixture["corpus_dir"],
            transformation_config_path=fixture["config"], train_jsonl=fixture["train"],
            validation_jsonl=fixture["validation"], corpus_manifest=fixture["corpus_manifest"],
            completed_audit_csv=fixture["audit_csv"], completed_audit_manifest=fixture["audit_manifest"],
        )


def test_final_training_auth_audit_result_mismatch(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    record = json.loads(fixture["auth"].read_text(encoding="utf-8"))
    record["audit_result"]["accepted_packet_accept"] = 0
    path = tmp_path / "auditres.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="audit_result.accepted_packet_accept"):
        validate_final_training_authorization(
            path, repository_root=ROOT, corpus_dir=fixture["corpus_dir"],
            transformation_config_path=fixture["config"], train_jsonl=fixture["train"],
            validation_jsonl=fixture["validation"], corpus_manifest=fixture["corpus_manifest"],
            completed_audit_csv=fixture["audit_csv"], completed_audit_manifest=fixture["audit_manifest"],
        )


def test_final_training_auth_malformed_hash_rejected(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    record = json.loads(fixture["auth"].read_text(encoding="utf-8"))
    record["corpus_artifacts"]["train_jsonl"]["sha256"] = "not-a-hash"
    path = tmp_path / "badhash.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="malformed"):
        validate_final_training_authorization(
            path, repository_root=ROOT, corpus_dir=fixture["corpus_dir"],
            transformation_config_path=fixture["config"], train_jsonl=fixture["train"],
            validation_jsonl=fixture["validation"], corpus_manifest=fixture["corpus_manifest"],
            completed_audit_csv=fixture["audit_csv"], completed_audit_manifest=fixture["audit_manifest"],
        )


def test_final_training_auth_changed_corpus_identity_same_counts(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    fixture["train"].write_text("\n".join(json.dumps({"y": i}) for i in range(3)) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="train_jsonl sha256 mismatch"):
        _validate_auth_fixture(tmp_path, fixture)


def test_final_training_auth_top_up_claim_fails(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    record = json.loads(fixture["auth"].read_text(encoding="utf-8"))
    record["corpus"]["top_up"] = True
    path = tmp_path / "topup.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="top_up"):
        validate_final_training_authorization(
            path, repository_root=ROOT, corpus_dir=fixture["corpus_dir"],
            transformation_config_path=fixture["config"], train_jsonl=fixture["train"],
            validation_jsonl=fixture["validation"], corpus_manifest=fixture["corpus_manifest"],
            completed_audit_csv=fixture["audit_csv"], completed_audit_manifest=fixture["audit_manifest"],
        )


def test_final_training_auth_held_out_true_fails(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    path = _write_auth(tmp_path, fixture, held_out_generation_authorized=True)
    fixture["auth"] = path
    with pytest.raises(CorpusBuildError, match="held_out_generation_authorized is not false"):
        _validate_auth_fixture(tmp_path, fixture)


def test_final_training_run_dir_rejects_existing_nonempty(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "run-x").mkdir()
    (runs / "run-x" / "leftover.txt").write_text("old", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="already exists"):
        create_final_training_run(runs, fixture["auth"], run_id="run-x")


def test_final_training_run_dir_initializes_once(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    runs = tmp_path / "runs"
    created = create_final_training_run(runs, fixture["auth"], run_id="run-y")
    assert created["run_id"] == "run-y"
    assert (runs / "run-y" / "run_context.json").is_file()
    assert (runs / "run-y" / "INCOMPLETE").is_file()
    assert json.loads((runs / "run-y" / "run_context.json").read_text(encoding="utf-8"))["status"] == "INCOMPLETE"


def test_final_training_run_dir_second_initialization_same_id_fails(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    runs = tmp_path / "runs"
    create_final_training_run(runs, fixture["auth"], run_id="run-z")
    with pytest.raises(CorpusBuildError, match="already exists"):
        create_final_training_run(runs, fixture["auth"], run_id="run-z")


def test_manifest_collection_restricted_to_active_run_dir(tmp_path: Path) -> None:
    active = tmp_path / "active"
    active.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (active / "a.txt").write_text("a", encoding="utf-8")
    (other / "b.txt").write_text("b", encoding="utf-8")
    manifest = write_external_manifest(active, configuration_identity="0" * 64, provenance_identity="test")
    paths = [artifact["path"] for artifact in manifest["artifacts"]]
    assert paths == ["a.txt"]
    assert "b.txt" not in paths

def _completion_run(tmp_path: Path) -> dict:
    """Build a valid run package: immutable payloads, payload manifest, once-finalized summary."""
    fixture = _auth_fixture_full(tmp_path)
    runs = tmp_path / "runs"
    created = create_final_training_run(runs, fixture["auth"], run_id="run-t")
    run_dir = Path(created["run_dir"])
    (run_dir / "adapter-final").mkdir()
    (run_dir / "adapter-final" / "adapter_model.safetensors").write_bytes(b"adapter-bytes-1")
    (run_dir / "tokenizer.json").write_text('{"tok": 1}\n', encoding="utf-8")
    (run_dir / "trainer_state.json").write_text('{"global_step": 1}\n', encoding="utf-8")
    (run_dir / "training_log_history.json").write_text("[]\n", encoding="utf-8")
    (run_dir / "runtime_environment.json").write_text('{"gpu": "T4"}\n', encoding="utf-8")
    (run_dir / "memory_timing.json").write_text('{"elapsed_seconds": 1.0}\n', encoding="utf-8")
    (run_dir / "reload_verification.json").write_text('{"adapter_reloaded": true}\n', encoding="utf-8")
    write_payload_manifest(run_dir, configuration_identity="0" * 64, provenance_identity="test")
    manifest_sha256 = _sha256_bytes((run_dir / "external_artifacts.json").read_bytes())
    summary = {
        "schema_version": "final-training-summary-v1",
        "experiment_id": "qlora-patch-pilot-v1",
        "run_id": "run-t",
        "final_status": FINAL_TRAINING_FINAL_STATUS,
        "manifest_sha256": manifest_sha256,
        "reload_verification": {"adapter_reloaded": True},
        "held_out_generation_authorized": False,
        "held_out_accessed": False,
        "train_loss": 0.5,
    }
    (run_dir / "final_training_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_sha256 = _sha256_bytes((run_dir / "final_training_summary.json").read_bytes())
    return {
        "run_dir": run_dir,
        "manifest_sha256": manifest_sha256,
        "summary_sha256": summary_sha256,
    }


def _rewrite_summary(run_dir: Path, summary: dict) -> str:
    (run_dir / "final_training_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _sha256_bytes((run_dir / "final_training_summary.json").read_bytes())


def _complete(pkg: dict, **kwargs: str) -> dict:
    return write_final_run_status(pkg["run_dir"], manifest_sha256=kwargs.get("manifest_sha256", pkg["manifest_sha256"]), summary_sha256=kwargs.get("summary_sha256", pkg["summary_sha256"]))


def test_completion_valid_package_succeeds(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    result = _complete(pkg)
    assert result["status"] == "COMPLETE"
    assert result["final_status"] == FINAL_TRAINING_FINAL_STATUS


def test_completion_missing_manifest_fails(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    (pkg["run_dir"] / "external_artifacts.json").unlink()
    with pytest.raises(CorpusBuildError, match="payload manifest missing"):
        _complete(pkg)


def test_completion_malformed_manifest_sha_fails(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    with pytest.raises(CorpusBuildError, match="not canonical"):
        _complete(pkg, manifest_sha256="xyz")


def test_completion_fake_manifest_sha_fails(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    with pytest.raises(CorpusBuildError, match="manifest sha256 mismatch"):
        _complete(pkg, manifest_sha256="0" * 64)


def test_completion_stale_manifest_fails(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    (pkg["run_dir"] / "adapter-final" / "adapter_model.safetensors").write_bytes(b"modified-after-manifest")
    with pytest.raises(CorpusBuildError, match="manifest artifact sha256 mismatch"):
        _complete(pkg)


def test_completion_missing_summary_fails(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    (pkg["run_dir"] / "final_training_summary.json").unlink()
    with pytest.raises(CorpusBuildError, match="final summary missing"):
        _complete(pkg)


def test_completion_stale_summary_hash_fails(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    with pytest.raises(CorpusBuildError, match="summary sha256 mismatch"):
        _complete(pkg, summary_sha256="0" * 64)


def test_completion_summary_wrong_manifest_hash_fails(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    summary = json.loads((pkg["run_dir"] / "final_training_summary.json").read_text(encoding="utf-8"))
    summary["manifest_sha256"] = "1" * 64
    summary_sha256 = _rewrite_summary(pkg["run_dir"], summary)
    with pytest.raises(CorpusBuildError, match="does not reference the exact manifest sha256"):
        _complete(pkg, summary_sha256=summary_sha256)


def test_completion_unsupported_final_status_fails(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    summary = json.loads((pkg["run_dir"] / "final_training_summary.json").read_text(encoding="utf-8"))
    summary["final_status"] = "SOMETHING_ELSE"
    summary_sha256 = _rewrite_summary(pkg["run_dir"], summary)
    with pytest.raises(CorpusBuildError, match="final_status"):
        _complete(pkg, summary_sha256=summary_sha256)


def test_completion_reload_false_fails(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    summary = json.loads((pkg["run_dir"] / "final_training_summary.json").read_text(encoding="utf-8"))
    summary["reload_verification"] = {"adapter_reloaded": False}
    summary_sha256 = _rewrite_summary(pkg["run_dir"], summary)
    with pytest.raises(CorpusBuildError, match="reload verification is not explicitly successful"):
        _complete(pkg, summary_sha256=summary_sha256)


def test_completion_held_out_accessed_true_fails(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    summary = json.loads((pkg["run_dir"] / "final_training_summary.json").read_text(encoding="utf-8"))
    summary["held_out_accessed"] = True
    summary_sha256 = _rewrite_summary(pkg["run_dir"], summary)
    with pytest.raises(CorpusBuildError, match="held_out_accessed is not false"):
        _complete(pkg, summary_sha256=summary_sha256)


def test_completion_premature_missing_required_field_fails(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    summary = json.loads((pkg["run_dir"] / "final_training_summary.json").read_text(encoding="utf-8"))
    del summary["train_loss"]
    del summary["reload_verification"]
    summary_sha256 = _rewrite_summary(pkg["run_dir"], summary)
    with pytest.raises(CorpusBuildError, match="missing required field"):
        _complete(pkg, summary_sha256=summary_sha256)


def test_completion_no_run_complete_after_failure(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    with pytest.raises(CorpusBuildError):
        _complete(pkg, manifest_sha256="0" * 64)
    assert not (pkg["run_dir"] / "RUN_COMPLETE").exists()


def test_completion_incomplete_remains_after_failure(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    with pytest.raises(CorpusBuildError):
        _complete(pkg, manifest_sha256="0" * 64)
    assert (pkg["run_dir"] / "INCOMPLETE").is_file()


def test_completion_success_writes_status_and_marker(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    _complete(pkg)
    run_status = json.loads((pkg["run_dir"] / "run_status.json").read_text(encoding="utf-8"))
    assert run_status["status"] == "COMPLETE"
    assert run_status["manifest_sha256"] == pkg["manifest_sha256"]
    assert run_status["final_summary_sha256"] == pkg["summary_sha256"]
    assert (pkg["run_dir"] / "RUN_COMPLETE").is_file()
    marker = json.loads((pkg["run_dir"] / "RUN_COMPLETE").read_text(encoding="utf-8"))
    assert marker["run_id"] == "run-t"
    assert marker["manifest_sha256"] == pkg["manifest_sha256"]


def test_completion_success_removes_incomplete(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    _complete(pkg)
    assert not (pkg["run_dir"] / "INCOMPLETE").exists()


def test_completion_second_attempt_fails(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    _complete(pkg)
    with pytest.raises(CorpusBuildError, match="second completion attempt"):
        _complete(pkg)


def test_payload_manifest_excludes_control_files(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    manifest = json.loads((pkg["run_dir"] / "external_artifacts.json").read_text(encoding="utf-8"))
    paths = {entry["path"] for entry in manifest["artifacts"]}
    for excluded in ("external_artifacts.json", "final_training_summary.json", "run_status.json", "RUN_COMPLETE", "INCOMPLETE"):
        assert excluded not in paths
    assert "adapter-final/adapter_model.safetensors" in paths
    assert "reload_verification.json" in paths


def test_payload_manifest_excludes_other_run_files(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    other = tmp_path / "other-run"
    other.mkdir()
    (other / "foreign.bin").write_bytes(b"foreign")
    manifest = json.loads((pkg["run_dir"] / "external_artifacts.json").read_text(encoding="utf-8"))
    paths = {entry["path"] for entry in manifest["artifacts"]}
    assert "foreign.bin" not in paths
    manifest["artifacts"].append({"path": "foreign.bin", "size_bytes": 7, "sha256": "0" * 64})
    (pkg["run_dir"] / "external_artifacts.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="exactly the active-run immutable payload"):
        _complete(pkg, manifest_sha256=_sha256_bytes((pkg["run_dir"] / "external_artifacts.json").read_bytes()))


def test_completion_does_not_rewrite_summary(tmp_path: Path) -> None:
    pkg = _completion_run(tmp_path)
    summary_path = pkg["run_dir"] / "final_training_summary.json"
    before = summary_path.read_bytes()
    _complete(pkg)
    assert summary_path.read_bytes() == before


def _mutate_logical_path(tmp_path: Path, section: str, kind: str, new_path: str) -> Path:
    fixture = _auth_fixture_full(tmp_path)
    record = json.loads(fixture["auth"].read_text(encoding="utf-8"))
    record[section][kind]["logical_path"] = new_path
    path = tmp_path / "logical-drift.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "section,kind,new_path,expected",
    [
        ("corpus_artifacts", "train_jsonl", "somewhere/other-file.jsonl", "train_jsonl"),
        ("corpus_artifacts", "validation_jsonl", "somewhere/other-file.jsonl", "validation_jsonl"),
        ("corpus_artifacts", "corpus_manifest", "somewhere/other-manifest.json", "corpus_manifest"),
        ("audit_artifacts", "completed_audit_csv", "somewhere/other-audit.csv", "completed_audit_csv"),
        ("audit_artifacts", "completed_audit_manifest", "somewhere/other-audit-manifest.json", "completed_audit_manifest"),
    ],
)
def test_logical_path_drift_fails(tmp_path: Path, section: str, kind: str, new_path: str, expected: str) -> None:
    fixture = _auth_fixture_full(tmp_path)
    record = json.loads(fixture["auth"].read_text(encoding="utf-8"))
    record[section][kind]["logical_path"] = new_path
    path = tmp_path / "logical-drift.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match=f"{expected}.*exact canonical"):
        validate_final_training_authorization(
            path, repository_root=ROOT, corpus_dir=fixture["corpus_dir"],
            transformation_config_path=fixture["config"], train_jsonl=fixture["train"],
            validation_jsonl=fixture["validation"], corpus_manifest=fixture["corpus_manifest"],
            completed_audit_csv=fixture["audit_csv"], completed_audit_manifest=fixture["audit_manifest"],
        )


def test_logical_path_backslash_fails(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    record = json.loads(fixture["auth"].read_text(encoding="utf-8"))
    record["corpus_artifacts"]["train_jsonl"]["logical_path"] = "corpus\\train.jsonl"
    path = tmp_path / "backslash.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(CorpusBuildError, match="non-canonical"):
        validate_final_training_authorization(
            path, repository_root=ROOT, corpus_dir=fixture["corpus_dir"],
            transformation_config_path=fixture["config"], train_jsonl=fixture["train"],
            validation_jsonl=fixture["validation"], corpus_manifest=fixture["corpus_manifest"],
            completed_audit_csv=fixture["audit_csv"], completed_audit_manifest=fixture["audit_manifest"],
        )


def test_exact_canonical_logical_paths_pass(tmp_path: Path) -> None:
    fixture = _auth_fixture_full(tmp_path)
    result = _validate_auth_fixture(tmp_path, fixture)
    assert result["corpus_artifact_identities"]["train_jsonl"]["logical_path"] == "corpus/train.jsonl"
    assert result["corpus_artifact_identities"]["validation_jsonl"]["logical_path"] == "corpus/validation.jsonl"
    assert result["corpus_artifact_identities"]["corpus_manifest"]["logical_path"] == "corpus/external_artifacts.json"
    assert result["audit_artifact_identities"]["completed_audit_csv"]["logical_path"] == "independent-audit/firstmate_independent_audit_completed.csv"
    assert result["audit_artifact_identities"]["completed_audit_manifest"]["logical_path"] == "independent-audit/firstmate_independent_audit_manifest.json"


def test_notebook_uses_write_final_run_status() -> None:
    nb = json.loads((EXPERIMENT / "colab/agentic_debugging_qlora_final_training.ipynb").read_text(encoding="utf-8"))
    code = "\n".join("".join(cell["source"]) for cell in nb["cells"] if cell["cell_type"] == "code")
    assert "write_final_run_status(RUN_DIR, manifest_sha256=manifest_sha256, summary_sha256=summary_sha256)" in code


def test_notebook_no_duplicate_manual_completion_sequence() -> None:
    nb = json.loads((EXPERIMENT / "colab/agentic_debugging_qlora_final_training.ipynb").read_text(encoding="utf-8"))
    code = "\n".join("".join(cell["source"]) for cell in nb["cells"] if cell["cell_type"] == "code")
    assert "'run_status.json').write_text" not in code
    assert "'RUN_COMPLETE').write_text" not in code
    assert "'INCOMPLETE').unlink" not in code
    assert code.count("'final_training_summary.json').write_text") == 1
