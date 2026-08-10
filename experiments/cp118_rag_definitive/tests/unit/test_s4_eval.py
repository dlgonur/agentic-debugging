"""S4 — primary evaluation semantics tests (Amendment 3, Repair Pass 1).

Offline: import the frozen CPU evaluator and the S4 eval module, verify the
frozen functions are loadable, that the S4 orchestration mirrors the frozen
row vocabulary, that P2P is declared NOT_RECORDED, that the frozen protocol
source is hash-pinned fail-closed (Blocker 3), and that the real
``run_cmd`` contract is the CompletedProcess API used by the orchestration
(Blocker 1).  The real offline evaluator smoke (one frozen task, canned
candidate, no model) exercises the full subprocess path.  No model, no GPU.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
REAL_CHECKOUT = REPO_ROOT / "tmp" / "s4" / "QuixBugs"


def _load_s4_eval():
    from experiments.cp118_rag_definitive import s4_eval

    return s4_eval


def test_frozen_eval_module_loads_and_exposes_semantics():
    ev = _load_s4_eval()._load_frozen_eval()
    for name in (
        "strict_compliance",
        "extract_patch_semantic_v121",
        "parse_files_independent",
        "parse_symbols_independent",
        "symbol_ground_truth",
        "canonicalize_transcript",
        "median",
    ):
        assert callable(getattr(ev, name))


def test_frozen_strict_and_extract_semantics_apply():
    ev = _load_s4_eval()._load_frozen_eval()
    good = (
        "PATCH\n"
        "--- a/python_programs/gcd.py\n+++ b/python_programs/gcd.py\n"
        "@@ -1,4 +1,4 @@\n"
        " def gcd(a, b):\n-    while a != 0:\n+    while b != 0:\n"
        "FILES\npython_programs/gcd.py\n"
        "SYMBOLS\npython_programs/gcd.py::gcd\n"
        "ROOT_CAUSE\nOff-by-one in the loop condition.\n"
    )
    comp = ev.strict_compliance(good)
    assert comp["strict_compliant"] is True
    sem = ev.extract_patch_semantic_v121(good)
    assert sem["recognizable_diff"] is True
    assert sem["normalized_patch"] is not None
    files, parseable, _ = ev.parse_files_independent(good)
    assert parseable and "python_programs/gcd.py" in files
    symbols, sym_ok, _ = ev.parse_symbols_independent(good)
    assert sym_ok and "python_programs/gcd.py::gcd" in symbols


def test_contract_declares_p2p_not_recorded():
    contract = json.loads(
        (REPO_ROOT / "experiments/cp118_rag_definitive/s4_contract.json")
        .read_text(encoding="utf-8"))
    assert contract["evaluation"]["p2p"] == "NOT_RECORDED"
    assert contract["evaluation"]["f2p"] == (
        "test_pass (supplied-oracle designated visible test) — "
        "the frozen RESOLVED basis"
    )


def test_s4_eval_single_condition_summary_mirrors_frozen():
    s4 = _load_s4_eval()
    rows = [
        {"model": "m", "strict_compliant": True, "recognizable_diff": True,
         "semantic_patch_extracted": True, "evaluator_loss": False,
         "patch_apply": True, "test_pass": True,
         "target_file_localized": True, "target_symbol_localized": True,
         "symbol_ground_truth_measurable": True, "truncated_at_budget": False,
         "generation_latency_s": 1.0, "prompt_tokens": 100,
         "output_tokens": 50, "peak_allocated_gib": 4.0},
        {"model": "m", "strict_compliant": False, "recognizable_diff": False,
         "semantic_patch_extracted": False, "evaluator_loss": False,
         "patch_apply": False, "test_pass": False,
         "target_file_localized": False, "target_symbol_localized": False,
         "symbol_ground_truth_measurable": True, "truncated_at_budget": True,
         "generation_latency_s": 2.0, "prompt_tokens": 200,
         "output_tokens": 4096, "peak_allocated_gib": 4.5},
    ]
    summary = s4._summarize_one(rows, "m", s4._load_frozen_eval())
    assert summary["strict_compliance_count"] == 1
    assert summary["patch_apply_count"] == 1
    assert summary["supplied_oracle_resolved_count"] == 1
    assert summary["truncation_count"] == 1
    assert summary["p2p"] == "NOT_RECORDED"
    assert summary["n"] == 2


# ---------------------------------------------------------------------------
# Repair Pass 1, Blocker 3 — frozen protocol source hash pinning
# ---------------------------------------------------------------------------


def test_frozen_cpu_eval_source_pin_positive():
    """The on-disk frozen CPU evaluator matches the contract-pinned SHA-256
    (verified fail-closed before import)."""

    s4 = _load_s4_eval()
    path = Path(s4.FROZEN_EVAL_SCRIPT)
    assert path.is_file()
    s4._verify_frozen_script(path, "cpu_eval_script_sha256")  # no raise
    # The loaded module is the hash-pinned file.
    ev = s4._load_frozen_eval()
    assert callable(ev.strict_compliance)


def test_frozen_cpu_eval_source_pin_negative(tmp_path):
    """A drifted frozen source must fail closed before import."""

    s4 = _load_s4_eval()
    bad = tmp_path / "drifted_eval.py"
    bad.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(s4.EvalError, match="frozen source drift"):
        s4._verify_frozen_script(bad, "cpu_eval_script_sha256")


def test_contract_pins_both_frozen_protocol_sources():
    contract = json.loads(
        (REPO_ROOT / "experiments/cp118_rag_definitive/s4_contract.json")
        .read_text(encoding="utf-8"))
    sources = contract["protocol"]["frozen_sources"]
    for key, rel in (
        ("cpu_eval_script_sha256", sources["cpu_eval_script_path"]),
        ("gpu_generate_script_sha256", sources["gpu_generate_script_path"]),
    ):
        import hashlib

        digest = hashlib.sha256(
            (REPO_ROOT / rel).read_bytes()).hexdigest()
        assert digest == sources[key], f"{key} drift"


# ---------------------------------------------------------------------------
# Repair Pass 1, Blocker 1 — run_cmd contract regression
# ---------------------------------------------------------------------------


def test_run_cmd_contract_is_completed_process():
    """The real helper returns subprocess.CompletedProcess; every eval
    orchestration site reads .returncode / .stdout / .stderr (the tuple
    unpacking bug is gone)."""

    from experiments.cp118_rag_definitive.s4_quixbugs import run_cmd

    proc = run_cmd([sys.executable, "-c", "print('ok')"])
    assert isinstance(proc, subprocess.CompletedProcess)
    assert proc.returncode == 0
    assert "ok" in proc.stdout

    s4 = _load_s4_eval()
    source = Path(s4.__file__).read_text(encoding="utf-8")
    assert "rc_bug, _ = run_cmd" not in source
    assert "rc, out = run_cmd" not in source
    assert ".returncode" in source


# ---------------------------------------------------------------------------
# Real offline evaluator smoke (engineering validation only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not REAL_CHECKOUT.is_dir() or not (REAL_CHECKOUT / ".git").is_dir(),
    reason="frozen QuixBugs checkout not present under tmp/s4",
)
def test_real_offline_eval_smoke(tmp_path):
    """The real s4_eval orchestration with the real run_cmd interface and a
    canned non-model candidate on a real frozen QuixBugs task (gcd).

    Exercises: frozen evaluator load (hash-pinned), oracle sanity
    subprocess handling (pytest buggy-fails / --correct passes), git
    apply --check / apply subprocess handling, visible pytest execution,
    and row/summary production.  This test fails on the pre-repair
    candidate (tuple-unpacking of CompletedProcess)."""

    from experiments.cp118_rag_definitive.s4_runner import (
        RunLogger,
        smoke_eval_stage,
    )

    out_dir = tmp_path / "smoke"
    args = argparse.Namespace(
        output_dir=str(out_dir),
        quixbugs_root=str(REPO_ROOT / "tmp" / "s4"),
        adapter_path="unused-by-smoke",
        count_tokens=False,
    )
    smoke_eval_stage(args, RunLogger(out_dir / "runner_stdout.log"))

    rows = list(csv.DictReader(
        open(out_dir / "details.csv", encoding="utf-8")))
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == "quixbugs-gcd"
    assert row["strict_compliant"] == "True"
    assert row["patch_apply"] == "True"
    assert row["test_pass"] == "True"
    assert row["semantic_failure_stage"] == "resolved_supplied_oracle"
    summary = list(csv.DictReader(
        open(out_dir / "summary.csv", encoding="utf-8")))
    assert summary[0]["supplied_oracle_resolved_count"] == "1"
    assert summary[0]["p2p"] == "NOT_RECORDED"
