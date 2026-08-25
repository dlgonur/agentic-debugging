from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ollama_cloud_command_adapter as adapter
from scripts import run_cookiecutter_967_pdb_proof as operator

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_result_summary_accepts_string_replay_state(monkeypatch):
    monkeypatch.setattr(
        operator,
        "replay_events",
        lambda _events: SimpleNamespace(events=[SimpleNamespace(state="Done")]),
    )
    case = {
        "events_jsonl": "event",
        "status": "UNRESOLVED",
        "controller": {"completed": True},
        "measurements": {},
        "verifier": {},
    }

    summary = operator._result_summary(case, {}, "patch")

    assert summary["replay_terminal_state"] == "Done"
    assert summary["accepted"] is False


def test_public_scaffold_exposes_recursive_merge_contract(tmp_path):
    (tmp_path / "tests").mkdir()
    operator._write_public_scaffold(tmp_path, "public problem")
    public_test = (tmp_path / "tests/test_pdb_public_config_merge.py").read_text(encoding="utf-8")
    task = json.loads((tmp_path / "task.json").read_text(encoding="utf-8"))

    assert "config.merge_configs" in public_test
    assert "recursively preserve unrelated keys" in task["description"]
    assert task["oracle"]["target_symbols"] == ["get_config", "merge_configs"]


def _image_inspect_result(argv, *, image_id=None, returncode=0, stdout=None, stderr=""):
    actual = image_id or operator.IMAGE_ID
    payload = {
        "id": actual,
        "repo_tags": ["swerebenchv2/audreyr-cookiecutter:967-ba5ba8c"],
        "repo_digests": [f"swerebenchv2/audreyr-cookiecutter@{actual}"],
        "created": "2026-04-21T20:10:30.655077799Z",
        "os": "linux",
        "architecture": "amd64",
        "labels": {"io.buildah.version": "1.38.1"},
    }
    return subprocess.CompletedProcess(
        argv,
        returncode,
        json.dumps(payload) if stdout is None and returncode == 0 else (stdout or ""),
        stderr,
    )


def test_image_gate_records_exact_provenance_and_verifies_clean_base(monkeypatch):
    calls = []

    def fake_run(argv, *, cwd=None, timeout=60.0):
        del cwd, timeout
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return _image_inspect_result(argv)
        assert argv[1] == "run"
        return subprocess.CompletedProcess(argv, 0, operator.BASE_COMMIT + "\n", "")

    monkeypatch.setattr(operator, "_run", fake_run)
    monkeypatch.setattr(operator, "_docker_context", lambda: "desktop-linux")

    evidence = operator._verify_image()

    assert evidence["category"] == "IMAGE_VERIFIED"
    assert evidence["image"] == operator.IMAGE
    assert evidence["actual_image_id"] == operator.IMAGE_ID
    assert evidence["repo_digests"] == [f"swerebenchv2/audreyr-cookiecutter@{operator.IMAGE_ID}"]
    assert evidence["os"] == "linux"
    assert evidence["architecture"] == "amd64"
    assert evidence["docker_context"] == "desktop-linux"
    assert evidence["provider_model_execution_started"] is False
    assert calls[0][0:3] == ["docker", "image", "inspect"]
    assert calls[1][0:2] == ["docker", "run"]


@pytest.mark.parametrize(
    ("stderr", "expected_category"),
    [
        ("Error response from daemon: No such image: pinned", "IMAGE_ABSENT"),
        ("Cannot connect to the Docker daemon at npipe://docker_engine", "DOCKER_UNAVAILABLE"),
        ("unexpected docker inspect failure", "IMAGE_INSPECTION_FAILED"),
    ],
)
def test_image_gate_classifies_inspect_failures_without_inference(
    monkeypatch, stderr, expected_category
):
    monkeypatch.setattr(
        operator,
        "_run",
        lambda argv, **_kwargs: _image_inspect_result(argv, returncode=1, stderr=stderr),
    )
    monkeypatch.setattr(operator, "_docker_context", lambda: "desktop-linux")

    with pytest.raises(operator.ImageVerificationError) as raised:
        operator._verify_image()

    assert raised.value.evidence["category"] == expected_category
    assert raised.value.evidence["provider_model_execution_started"] is False


def test_image_gate_classifies_wrong_id_and_malformed_output(monkeypatch):
    monkeypatch.setattr(operator, "_docker_context", lambda: "desktop-linux")

    monkeypatch.setattr(
        operator,
        "_run",
        lambda argv, **_kwargs: _image_inspect_result(
            argv, image_id="sha256:" + "1" * 64
        ),
    )
    with pytest.raises(operator.ImageVerificationError) as wrong_id:
        operator._verify_image()
    assert wrong_id.value.evidence["category"] == "IMAGE_IDENTITY_MISMATCH"
    assert wrong_id.value.evidence["actual_image_id"] == "sha256:" + "1" * 64

    monkeypatch.setattr(
        operator,
        "_run",
        lambda argv, **_kwargs: _image_inspect_result(argv, stdout="not-json"),
    )
    with pytest.raises(operator.ImageVerificationError) as malformed:
        operator._verify_image()
    assert malformed.value.evidence["category"] == "IMAGE_INSPECTION_INVALID"


def test_image_gate_persists_success_and_failure_evidence(tmp_path, monkeypatch):
    success = {
        "schema_version": operator.IMAGE_GATE_EVIDENCE_SCHEMA_VERSION,
        "category": "IMAGE_VERIFIED",
        "provider_model_execution_started": False,
    }
    monkeypatch.setattr(operator, "_verify_image", lambda: success)
    operator._verify_image_and_record(tmp_path)
    assert json.loads((tmp_path / "image-verification.json").read_text())["category"] == "IMAGE_VERIFIED"

    failure = operator.ImageVerificationError(
        "absent",
        {"schema_version": operator.IMAGE_GATE_EVIDENCE_SCHEMA_VERSION, "category": "IMAGE_ABSENT"},
    )
    monkeypatch.setattr(operator, "_verify_image", lambda: (_ for _ in ()).throw(failure))
    with pytest.raises(operator.ImageVerificationError):
        operator._verify_image_and_record(tmp_path)
    assert json.loads((tmp_path / "image-verification.json").read_text())["category"] == "IMAGE_ABSENT"


def test_official_evaluator_adds_only_terminal_patch_newline(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, *, cwd=None, timeout=60.0):
        del cwd, timeout
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, operator.EVALUATOR_COMMIT + "\n", "")
        report_path = tmp_path / "official-private-report.json"
        report_path.write_text(
            json.dumps(
                {
                    "total": 1,
                    "all_ok": False,
                        "items": [
                            {
                                "instance_id": operator.INSTANCE_ID,
                                "passed_match": False,
                            "exit_code": 1,
                            "from_fail_to_pass": [],
                            "failed_from_pass_to_pass": ["p2p"],
                            "error": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        spec_path = tmp_path / "official-private-spec.json"
        captured.update(json.loads(spec_path.read_text(encoding="utf-8"))[0])
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(operator, "_run", fake_run)
    row = {
        "instance_id": operator.INSTANCE_ID,
        "repo": "audreyr/cookiecutter",
        "base_commit": operator.BASE_COMMIT,
        "image_name": operator.IMAGE,
        "patch": "reference patch\n",
        "test_patch": "private\n",
        "FAIL_TO_PASS": [f"f{index}" for index in range(operator.F2P_COUNT)],
        "PASS_TO_PASS": [f"p{index}" for index in range(operator.P2P_COUNT)],
        "install_config": {"test_cmd": ["pytest"], "log_parser": "pytest"},
        "problem_statement": "public",
        "language": "python",
        "license": "MIT",
    }

    safe = operator._official_evaluate(row, "candidate", tmp_path)

    assert captured["patch"] == "candidate\n"
    assert safe["candidate_patch_normalization"] == "terminal-newline-added"
    assert safe["raw_candidate_patch_sha256"] != safe["evaluated_candidate_patch_sha256"]


def test_candidate_selection_uses_replayed_tool_success_not_last_parsed_proposal():
    path = REPOSITORY_ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-mistral-large-3-675b-cloud-v2/live-results.json"
    case = json.loads(path.read_text(encoding="utf-8"))
    candidate = operator._candidate_patch_record(case)
    assert candidate["tool_accepted"] is True
    assert candidate["action_event_sequence"] == 35
    assert candidate["observation_event_sequence"] == 36
    assert candidate["patch_sha256"] == "843ea99b49e98d0bf40684a18dfdf821277a76914e55627446a7367e76baf4c7"
    assert hashlib.sha256(candidate["patch"].encode("utf-8")).hexdigest() == candidate["patch_sha256"]
    final_proposal = case["evidence"]["observable_model_directive_attempts"][-1]["directive"]["arguments"]["patch"]
    assert hashlib.sha256(final_proposal.encode("utf-8")).hexdigest() == (
        "d9e097cbcf921cda68f231a4abd125731e56c02ac63a7141877c52c41df6f7bb"
    )
    assert final_proposal != candidate["patch"]


def test_candidate_selection_rejects_tampered_tool_success_hash():
    path = REPOSITORY_ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-mistral-large-3-675b-cloud-v2/live-results.json"
    case = json.loads(path.read_text(encoding="utf-8"))
    events = [json.loads(line) for line in case["events_jsonl"].splitlines() if line.strip()]
    accepted = next(
        event for event in events
        if event["event_type"] == "observation"
        and event["payload"]["observation"]["name"] == "apply_patch"
        and event["payload"]["observation"]["status"] == "ok"
    )
    accepted["payload"]["observation"]["payload"]["patch_sha256"] = "0" * 64
    case["events_jsonl"] = "".join(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n" for event in events)
    with pytest.raises(operator.ProofError, match="disagrees with action bytes"):
        operator._candidate_patch_record(case)


def test_candidate_selection_tracks_replacement_and_successful_revert(monkeypatch):
    first_patch = "first patch"
    second_patch = "second patch"
    events = [
        SimpleNamespace(
            event_type="action",
            sequence=1,
            payload={"action": {"name": "apply_patch", "action_id": "apply-1", "arguments": {"patch": first_patch}}},
        ),
        SimpleNamespace(
            event_type="observation",
            sequence=2,
            payload={
                "observation": {
                    "name": "apply_patch",
                    "status": "ok",
                    "action_id": "apply-1",
                    "observation_id": "observation-1",
                    "payload": {
                        "applied": True,
                        "patch_sha256": hashlib.sha256(first_patch.encode("utf-8")).hexdigest(),
                    },
                }
            },
        ),
        SimpleNamespace(
            event_type="action",
            sequence=3,
            payload={"action": {"name": "apply_patch", "action_id": "apply-2", "arguments": {"patch": second_patch}}},
        ),
        SimpleNamespace(
            event_type="observation",
            sequence=4,
            payload={
                "observation": {
                    "name": "apply_patch",
                    "status": "ok",
                    "action_id": "apply-2",
                    "observation_id": "observation-2",
                    "payload": {
                        "applied": True,
                        "patch_sha256": hashlib.sha256(second_patch.encode("utf-8")).hexdigest(),
                    },
                }
            },
        ),
    ]
    monkeypatch.setattr(operator, "replay_events", lambda _events: SimpleNamespace(events=events))
    assert operator._candidate_patch_record({"events_jsonl": "replayed"})["patch"] == second_patch

    events.append(
        SimpleNamespace(
            event_type="observation",
            sequence=5,
            payload={
                "observation": {
                    "name": "revert_patch",
                    "status": "ok",
                    "action_id": "revert-1",
                    "observation_id": "observation-3",
                    "payload": {"reverted": True},
                }
            },
        )
    )
    with pytest.raises(operator.ProofError, match="did not retain a tool-accepted active candidate"):
        operator._candidate_patch_record({"events_jsonl": "replayed"})


def test_recovery_refuses_mismatched_candidate_without_overwriting_or_evaluating(tmp_path, monkeypatch):
    source = tmp_path / "historical"
    output = tmp_path / "repaired"
    source.mkdir()
    (source / "live-results.json").write_text("{}\n", encoding="utf-8")
    patch_path = source / "candidate.patch"
    patch_path.write_text("historical mismatch", encoding="utf-8")
    monkeypatch.setattr(operator, "_load_official_row", lambda: {})
    monkeypatch.setattr(operator, "_verify_image", lambda: None)
    monkeypatch.setattr(
        operator,
        "_candidate_patch_record",
        lambda _case: {"patch": "tool-accepted patch", "patch_sha256": "0" * 64},
    )
    evaluated = False

    def fail_if_evaluated(*_args, **_kwargs):
        nonlocal evaluated
        evaluated = True
        raise AssertionError("official evaluator must not run on an identity mismatch")

    monkeypatch.setattr(operator, "_official_evaluate", fail_if_evaluated)

    with pytest.raises(operator.ProofError, match="disagrees with replayed tool-success evidence"):
        operator.main(
            [
                "--model",
                "mistral-large-3:675b-cloud",
                "--treatment-revision",
                "2",
                "--output-dir",
                str(output),
                "--recover-existing",
                "--recovery-source-dir",
                str(source),
            ]
        )

    assert patch_path.read_text(encoding="utf-8") == "historical mismatch"
    assert not output.exists()
    assert evaluated is False


def test_recovery_success_writes_only_fresh_destination_and_assigns_new_treatment(tmp_path, monkeypatch):
    source = tmp_path / "historical"
    output = tmp_path / "repaired"
    source.mkdir()
    live_bytes = b'{"historical": true}\n'
    raw_patch = "tool-accepted patch\n"
    (source / "live-results.json").write_bytes(live_bytes)
    (source / "candidate.patch").write_bytes(raw_patch.encode("utf-8"))
    (source / "historical-result.json").write_bytes(b"historical evidence\n")
    before = {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*") if path.is_file()}

    class FakeArtifact:
        patch = "canonical official patch\n"

        def to_mapping(self):
            return {"artifact_name": "candidate-official.patch", "patch": self.patch}

    monkeypatch.setattr(operator, "_resolve_model_or_fail", lambda model: (model, None))
    monkeypatch.setattr(operator, "_load_official_row", lambda: {"problem_statement": "public"})
    monkeypatch.setattr(
        operator,
        "_candidate_patch_record",
        lambda _case: {"patch": raw_patch, "patch_sha256": "raw-hash"},
    )
    monkeypatch.setattr(operator, "_verify_image_and_record", lambda destination: (destination / "image-verification.json").write_text("{}\n"))
    monkeypatch.setattr(operator, "_copy_image_source", lambda _destination: None)
    monkeypatch.setattr(operator, "_write_public_scaffold", lambda *_args: None)
    monkeypatch.setattr(operator, "_canonicalize_level32_candidate", lambda *_args, **_kwargs: FakeArtifact())
    monkeypatch.setattr(
        operator,
        "_official_evaluate",
        lambda _row, patch, _private, **kwargs: {
            "all_ok": False,
            "official_test_execution_proven": True,
            "evaluated_patch": patch,
            "raw_patch": kwargs["raw_patch"],
        },
    )
    monkeypatch.setattr(
        operator,
        "_result_summary",
        lambda _case, _official, _patch, **kwargs: {
            "accepted": False,
            "treatment_id": kwargs["treatment_id"],
        },
    )

    assert operator.main(
        [
            "--model",
            "mistral-large-3:675b-cloud",
            "--treatment-revision",
            "2",
            "--output-dir",
            str(output),
            "--recover-existing",
            "--recovery-source-dir",
            str(source),
        ]
    ) == 1

    after = {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    assert after == before
    assert (output / "candidate.patch").read_bytes() == raw_patch.encode("utf-8")
    assert (output / "candidate-official.patch").read_text(encoding="utf-8") == "canonical official patch\n"
    assert json.loads((output / "result.json").read_text(encoding="utf-8"))["treatment_id"] == (
        "pdb-capability-level32-cookiecutter-967-mistral-large-3-675b-cloud-v2-"
        "workspace-derived-official-git-diff-v1"
    )
    assert (output / "official-verifier-summary.json").is_file()


def test_selectable_model_default_and_treatment_identity():
    # Default alias and repaired candidate-transport treatment must not drift.
    assert operator.MODEL == operator.DEFAULT_MODEL == "gpt-oss:20b-cloud"
    assert operator._treatment_id_for_model("gpt-oss:20b-cloud") == operator.TREATMENT_ID
    assert operator.CANDIDATE_TRANSPORT_ID in operator.TREATMENT_ID
    assert operator._treatment_id_for_model("gpt-oss:120b-cloud") == (
        "pdb-capability-level32-cookiecutter-967-gpt-oss-120b-v1-"
        "workspace-derived-official-git-diff-v1"
    )
    assert operator._treatment_id_for_model("qwen3.5:cloud") == (
        "pdb-capability-level32-cookiecutter-967-qwen3.5-cloud-v1-"
        "workspace-derived-official-git-diff-v1"
    )
    # Treatment identity is distinct per model.
    assert operator._treatment_id_for_model("gpt-oss:120b-cloud") != operator._treatment_id_for_model("gpt-oss:20b-cloud")
    assert operator._treatment_id_for_model("gpt-oss:120b-cloud") != operator._treatment_id_for_model("qwen3.5:cloud")
    assert operator._treatment_id_for_model("gpt-oss:120b-cloud", 2) == (
        "pdb-capability-level32-cookiecutter-967-gpt-oss-120b-v2-"
        "workspace-derived-official-git-diff-v1"
    )
    assert operator._treatment_id_for_model("qwen3.5:cloud", 3) == (
        "pdb-capability-level32-cookiecutter-967-qwen3.5-cloud-v3-"
        "workspace-derived-official-git-diff-v1"
    )
    assert operator._treatment_id_for_model("minimax-m3:cloud", 2) == (
        "pdb-capability-level32-cookiecutter-967-minimax-m3-cloud-v2-"
        "workspace-derived-official-git-diff-v1"
    )


def test_shared_image_gate_runs_before_model_adapter_setup(tmp_path, monkeypatch):
    representatives = (
        "gpt-oss:120b-cloud",
        "qwen3.5:cloud",
        "minimax-m3:cloud",
        "glm-5.1:cloud",
    )
    gate_calls = []

    monkeypatch.setattr(operator, "_resolve_model_or_fail", lambda model: (model, object()))
    monkeypatch.setattr(operator, "_require_treatment_eligible", lambda _model: object())
    monkeypatch.setattr(operator, "_load_official_row", lambda: {})

    def fake_gate(output):
        gate_calls.append(output.name)
        evidence = {
            "schema_version": operator.IMAGE_GATE_EVIDENCE_SCHEMA_VERSION,
            "category": "IMAGE_VERIFIED",
            "provider_model_execution_started": False,
        }
        operator._write_image_verification(output, evidence)
        return evidence

    monkeypatch.setattr(operator, "_verify_image_and_record", fake_gate)

    def fail_if_adapter_configured(*_args, **_kwargs):
        raise AssertionError("model adapter setup occurred after the shared gate")

    monkeypatch.setattr(operator, "_adapter_config", fail_if_adapter_configured)

    for index, model in enumerate(representatives):
        with pytest.raises(AssertionError, match="shared gate"):
            operator.main(
                [
                    "--model",
                    model,
                    "--treatment-revision",
                    "2",
                    "--output-dir",
                    str(tmp_path / f"output-{index}"),
                    "--live",
                    "--confirm-live-model-access",
                ]
            )

    assert len(gate_calls) == len(representatives)


def test_frozen_model_fresh_revisions_keep_transport_and_other_models_get_fresh_identity():
    import pytest

    assert operator._treatment_id_for_model("gpt-oss:20b-cloud", 2).endswith(
        "gpt-oss-20b-cloud-v2-workspace-derived-official-git-diff-v1"
    )
    with pytest.raises(operator.ProofError, match="positive integer"):
        operator._treatment_id_for_model("qwen3.5:cloud", 0)


def test_unknown_model_fails_closed():
    import pytest

    with pytest.raises(Exception):
        operator._resolve_model_or_fail("ollama-cloud/nemotron-3-nano:30b")
    with pytest.raises(Exception):
        operator._resolve_model_or_fail("unknown:cloud")


def test_script_entrypoint_registers_fallback_adapter_module(tmp_path):
    repository_root = Path(__file__).resolve().parents[2]
    script = repository_root / "scripts" / "run_cookiecutter_967_pdb_proof.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--model",
            "gpt-oss:120b-cloud",
            "--treatment-revision",
            "2",
            "--output-dir",
            str(tmp_path / "fallback-loader-output"),
        ],
        cwd=repository_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "live selection and explicit model-access confirmation are required" in result.stderr
    assert "dataclass" not in result.stderr


def test_fallback_adapter_loader_exposes_model_provenance():
    module = operator._load_ollama_adapter_module("test_cookiecutter_adapter_loader")
    spec = module.resolve_cloud_model("gpt-oss:120b-cloud")
    assert spec.transport_verified is True
    assert len(module.transport_config_fingerprint(spec)) == 64
    assert operator._model_provenance("gpt-oss:120b-cloud")["transport_config_fingerprint"] == module.transport_config_fingerprint(spec)


def test_adapter_config_carries_selected_model_alias(tmp_path):
    config = operator._adapter_config(tmp_path, model="gpt-oss:120b-cloud")
    assert config.model_name == "gpt-oss:120b-cloud"
    assert "--model" in config.command
    assert config.command[config.command.index("--model") + 1] == "gpt-oss:120b-cloud"
    # Default still gpt-oss:20b
    default = operator._adapter_config(tmp_path)
    assert default.model_name == "gpt-oss:20b-cloud"


def test_kimi_timeout_repair_is_model_specific_and_bounded(tmp_path):
    config = operator._adapter_config(tmp_path, model="kimi-k2.6:cloud")
    assert config.command[config.command.index("--timeout") + 1] == "45"
    assert config.request_timeout_seconds == 75.0


def test_nemotron_super_timeout_repair_is_model_specific_and_bounded(tmp_path):
    config = operator._adapter_config(tmp_path, model="nemotron-3-super:cloud")
    assert config.command[config.command.index("--timeout") + 1] == "45"
    assert config.request_timeout_seconds == 75.0


def test_nemotron_ultra_timeout_repair_is_model_specific_and_bounded(tmp_path):
    config = operator._adapter_config(tmp_path, model="nemotron-3-ultra:cloud")
    assert config.command[config.command.index("--timeout") + 1] == "45"
    assert config.request_timeout_seconds == 75.0


def test_level32_live_gate_rejects_unqualified_model(monkeypatch, tmp_path):
    # Models without a completed transport qualification must be blocked from
    # entering the live operator, including profile-declared models.
    # This is provider-free: we fail before any preflight/Docker work.
    import pytest

    # Simulate the gate check the operator performs before any I/O
    for alias in ("kimi-k2.7-code:cloud",):
        with pytest.raises(operator.ProofError, match="not yet live-transport eligible"):
            operator._require_treatment_eligible(alias)

    # Verified models pass
    assert operator._require_treatment_eligible("gpt-oss:20b-cloud") is not None
    assert operator._require_treatment_eligible("nemotron-3-nano:30b-cloud") is not None
    assert operator._require_treatment_eligible("glm-5.1:cloud") is not None
    assert operator._require_treatment_eligible("glm-5.2:cloud") is not None

    # Verify operator main path also gate-checks before touching disk
    out = tmp_path / "level32-test-gate-output"
    with pytest.raises(operator.ProofError):
        operator.main(["--model", "kimi-k2.7-code:cloud", "--output-dir", str(out), "--live", "--confirm-live-model-access"])
    assert not out.exists()


def test_glm_5_2_promotion_preserves_qualified_profile_and_prepares_v1_treatment():
    spec = adapter.resolve_cloud_model("glm-5.2:cloud")
    assert spec.local_alias == "glm-5.2:cloud"
    assert spec.upstream_model == "glm-5.2"
    assert spec.effective_tags_remote_model == "glm-5.2"
    assert spec.family == "glm5.2"
    assert spec.parameter_count == 756162687872
    assert spec.context_length == 1000000
    assert spec.capabilities == ("completion", "thinking", "tools")
    assert spec.transport_profile_declared is True
    assert spec.transport_verified is True
    assert spec.readiness == "live_verified"
    assert adapter.is_live_transport_ready(spec) is True
    assert adapter.is_treatment_eligible(spec) is True
    assert spec.thinking_level is None
    assert spec.idle_timeout_seconds == 20.0
    assert spec.request_timeout_seconds == 60.0
    assert adapter.transport_config_fingerprint(spec) == (
        "0685fad3a22efa7ba8a4776729f2f552e89d66f1032c9ad1fcb344557759dad9"
    )
    assert operator._treatment_id_for_model("glm-5.2:cloud", 1) == (
        "pdb-capability-level32-cookiecutter-967-glm-5.2-cloud-v1-"
        "workspace-derived-official-git-diff-v1"
    )
    assert operator._treatment_fingerprint("glm-5.2:cloud", operator.LEVEL32_TREATMENT_BUDGET) == (
        "633bb6885072229b999e9dd4da7de496e6bb20cb495359a9560a637937f1025c"
    )
    artifact = REPOSITORY_ROOT / "experiments/pdb_capability_ladder/transport_qualifications/glm-5.2-v2.json"
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == (
        "8c04b307269c8be3768010265ca8411017a8864decfceaea0eaa5fd6b3a136ab"
    )
    assert adapter.resolve_cloud_model("glm-5.1:cloud").local_alias != spec.local_alias
    assert adapter.resolve_cloud_model("glm-5.1:cloud").upstream_model == "glm-5.1"
    result = REPOSITORY_ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-glm-5.2-cloud-v1/result.json"
    assert result.is_file()
    assert json.loads(result.read_text(encoding="utf-8"))["task"]["treatment_id"] == (
        "pdb-capability-level32-cookiecutter-967-glm-5.2-cloud-v1"
    )


def test_glm_5_2_promotion_changes_only_its_registry_entry():
    import dataclasses

    promoted = {alias: dataclasses.asdict(spec) for alias, spec in adapter.CLOUD_MODELS.items()}
    pre_promotion = dict(promoted)
    glm = dataclasses.replace(
        adapter.CLOUD_MODELS["glm-5.2:cloud"],
        transport_profile_declared=False,
        transport_verified=False,
    )
    pre_promotion["glm-5.2:cloud"] = dataclasses.asdict(glm)

    assert set(pre_promotion) == set(promoted)
    assert all(
        pre_promotion[alias] == promoted[alias]
        for alias in pre_promotion
        if alias != "glm-5.2:cloud"
    )
    assert pre_promotion["glm-5.2:cloud"]["transport_profile_declared"] is False
    assert pre_promotion["glm-5.2:cloud"]["transport_verified"] is False
    assert promoted["glm-5.2:cloud"]["transport_profile_declared"] is True
    assert promoted["glm-5.2:cloud"]["transport_verified"] is True


def test_glm_5_1_qualification_promotion_is_eligible():
    spec = adapter.resolve_cloud_model("glm-5.1:cloud")
    assert spec.transport_profile_declared is True
    assert spec.transport_verified is True
    assert spec.thinking_level is None
    assert spec.readiness == "live_verified"
    assert adapter.is_treatment_eligible(spec) is True
    assert adapter.transport_config_fingerprint(spec) == (
        "fb1fb2cc6e3525586b34565afc1dc43cb416acdf06738af2c70d014ee576a4a4"
    )


def test_gpt_oss_120b_qualification_promotion_is_eligible():
    spec = adapter.resolve_cloud_model("gpt-oss:120b-cloud")
    assert spec.transport_profile_declared is True
    assert spec.transport_verified is True
    assert spec.readiness == "live_verified"
    assert adapter.is_live_transport_ready(spec) is True
    assert adapter.is_treatment_eligible(spec) is True
    # The pre-promotion qualification identity is still distinct from the
    # effective promoted treatment configuration.
    import dataclasses

    declared = dataclasses.replace(spec, transport_verified=False)
    assert declared.readiness == "profile_declared"
    assert adapter.transport_config_fingerprint(spec) != adapter.transport_config_fingerprint(declared)


def test_operator_parser_model_selection():
    parser = operator.build_parser()
    args = parser.parse_args(["--model", "gpt-oss:120b-cloud", "--output-dir", "out"])
    assert args.model == "gpt-oss:120b-cloud"
    args2 = parser.parse_args(["--output-dir", "out"])
    assert args2.model is None  # defaults to MODEL at runtime
    args3 = parser.parse_args(["--model", "gpt-oss:120b-cloud", "--treatment-revision", "2"])
    assert args3.treatment_revision == 2


def test_operator_exposes_list_models():
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        operator.main(["--list-models"])
    text = buf.getvalue()
    assert "gpt-oss:20b-cloud" in text
    assert "gpt-oss:120b-cloud" in text
    assert "qwen3.5:cloud" in text


def test_result_summary_binds_treatment_identity():
    import json as _json

    seen = {}

    def fake_replay(events_jsonl):
        seen["events"] = events_jsonl
        return SimpleNamespace(events=[SimpleNamespace(state="Done")])

    import scripts.run_cookiecutter_967_pdb_proof as op

    orig = op.replay_events
    op.replay_events = fake_replay
    try:
        case = {
            "events_jsonl": "evt",
            "status": "RESOLVED",
            "controller": {"completed": True},
            "measurements": {},
            "verifier": {"outcome": "RESOLVED", "workspace_cleaned": True, "canonical_fixture_unchanged": True},
        }
        s = operator._result_summary(
            case, {"all_ok": True, "passed_match": True, "fail_to_pass_passed": 5, "pass_to_pass_failed": 0, "process_exit_code": 0}, "patch",
            model="gpt-oss:120b-cloud", treatment_id="pdb-capability-level32-cookiecutter-967-gpt-oss-120b-v1",
        )
        assert s["model"] == "gpt-oss:120b-cloud"
        assert s["treatment_id"] == "pdb-capability-level32-cookiecutter-967-gpt-oss-120b-v1"
        assert s["task"]["treatment_id"] == s["treatment_id"]
        assert s["transport_config_fingerprint"] == adapter.transport_config_fingerprint(
            adapter.resolve_cloud_model("gpt-oss:120b-cloud")
        )
        # Fingerprint changes when thinking/config changes.
        fp20 = adapter.transport_config_fingerprint(adapter.resolve_cloud_model("gpt-oss:20b-cloud"))
        fp120 = adapter.transport_config_fingerprint(adapter.resolve_cloud_model("gpt-oss:120b-cloud"))
        assert fp20 != fp120
    finally:
        op.replay_events = orig


def test_transport_fingerprint_stability_and_model_binding():
    fp = adapter.transport_config_fingerprint(adapter.resolve_cloud_model("gpt-oss:20b-cloud"))
    assert len(fp) == 64  # sha256 hex
    # Same model yields same fingerprint.
    assert fp == adapter.transport_config_fingerprint(adapter.resolve_cloud_model("gpt-oss:20b-cloud"))
    # Different model yields different fingerprint.
    assert fp != adapter.transport_config_fingerprint(adapter.resolve_cloud_model("qwen3.5:cloud"))


def test_adapter_rejects_unknown_alias_provider_free():
    import pytest

    with pytest.raises(adapter.OllamaAdapterError) as exc_info:
        adapter.resolve_cloud_model("kimi-k2.5:cloud")
    assert exc_info.value.kind == "configuration"
    with pytest.raises(adapter.OllamaAdapterError):
        adapter.resolve_cloud_model("ollama-cloud/nemotron-3-nano:30b")


def test_official_evaluator_rejects_identity_mismatch_before_execution(tmp_path, monkeypatch):
    row = {
        "instance_id": operator.INSTANCE_ID,
        "repo": "audreyr/cookiecutter",
        "base_commit": operator.BASE_COMMIT,
        "image_name": "wrong/image:tag",
        "test_patch": "test patch\n",
        "patch": "+ valid reference\n",
        "FAIL_TO_PASS": [f"f{index}" for index in range(operator.F2P_COUNT)],
        "PASS_TO_PASS": [f"p{index}" for index in range(operator.P2P_COUNT)],
        "install_config": {"test_cmd": ["pytest"], "log_parser": "pytest"},
        "problem_statement": "public",
        "language": "python",
        "license": "MIT",
    }
    monkeypatch.setattr(operator, "_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("evaluator ran")))
    with pytest.raises(operator.ProofError, match="image_name"):
        operator._official_evaluate(row, "candidate", tmp_path)


def test_official_result_summary_redacts_hidden_test_identity():
    summary = operator._redacted_test_summary(
        {
            "instance_id": operator.INSTANCE_ID,
            "from_fail_to_pass": ["hidden/f2p::test_secret"],
            "failed_from_pass_to_pass": ["hidden/p2p::test_secret"],
            "error": "private evaluator traceback",
        },
        f2p_total=operator.F2P_COUNT,
        p2p_total=operator.P2P_COUNT,
    )
    serialized = json.dumps(summary, sort_keys=True)
    assert "hidden/f2p" not in serialized
    assert "hidden/p2p" not in serialized
    assert "traceback" not in serialized
    assert summary["identity_retained"] is False
    assert summary["index_semantics"] == (
        "not retained; only aggregate status counts are authoritative"
    )
    assert summary["fail_to_pass"]["error"] == operator.F2P_COUNT
    assert summary["pass_to_pass"]["error"] == operator.P2P_COUNT
    assert "tests" not in summary["fail_to_pass"]
    assert "tests" not in summary["pass_to_pass"]


def test_official_patch_application_failure_is_not_called_semantic():
    official = {
        "all_ok": False,
        "error_present": False,
        "candidate_patch_application_failure": True,
    }
    case = {
        "status": operator.LiveCaseStatus.RESOLVED.value,
        "measurements": {
            "provider_error_kinds": [],
            "model_request_count": 1,
            "model_response_count": 1,
        },
    }
    assert operator._classify_level32_case(case, official) == (
        "official_candidate_patch_application_failure"
    )


def test_unproven_official_execution_is_not_called_semantic_rejection():
    case = {
        "status": operator.LiveCaseStatus.RESOLVED.value,
        "measurements": {"provider_error_kinds": [], "model_request_count": 1, "model_response_count": 1},
    }
    assert operator._classify_level32_case(
        case,
        {
            "all_ok": False,
            "error_present": False,
            "fail_to_pass_passed": 0,
            "pass_to_pass_failed": 9,
            "official_test_execution_proven": False,
        },
    ) == "official_test_execution_unproven"


def test_proven_official_execution_is_a_semantic_rejection():
    case = {
        "status": operator.LiveCaseStatus.RESOLVED.value,
        "measurements": {"provider_error_kinds": [], "model_request_count": 1, "model_response_count": 1},
    }
    assert operator._classify_level32_case(
        case,
        {"all_ok": False, "error_present": False, "official_test_execution_proven": True},
    ) == "official_rejection_semantic"


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("candidate_materialization_failure", "candidate_not_materialized"),
        ("candidate_canonicalization_failure", "canonical_official_patch_unavailable"),
    ],
)
def test_pretest_candidate_boundaries_have_distinct_classifications(field, expected):
    case = {field: True, "measurements": {"provider_error_kinds": []}}
    assert operator._classify_level32_case(case, None) == expected


def test_level32_fixture_classification_distinguishes_glm_and_deepseek_without_provider_calls(monkeypatch):
    glm = json.loads(
        (REPOSITORY_ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-glm-5.2-cloud-v1/result.json").read_text(encoding="utf-8")
    )
    deepseek = json.loads(
        (REPOSITORY_ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-deepseek-v4-pro-cloud-v1/result.json").read_text(encoding="utf-8")
    )
    monkeypatch.setattr(operator, "_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider/evaluator call")))
    assert operator._classify_level32_case(glm, glm["official_verifier"]) == (
        "official_test_execution_unproven"
    )
    assert operator._classify_level32_case(deepseek, deepseek["official_verifier"]) == (
        "incomplete_provider_model_transport_failure"
    )
    parser_rejection = dict(deepseek)
    parser_rejection["measurements"] = dict(deepseek["measurements"])
    parser_rejection["measurements"]["model_response_count"] = parser_rejection["measurements"]["model_request_count"]
    assert operator._classify_level32_case(parser_rejection, None) == "provider_parser_rejection"


def test_integrity_gate_is_provider_free_and_requires_all_controls(tmp_path, monkeypatch):
    row = {
        "instance_id": operator.INSTANCE_ID,
        "repo": "audreyr/cookiecutter",
        "base_commit": operator.BASE_COMMIT,
        "image_name": operator.IMAGE,
        "test_patch": "test patch\n",
        "patch": "+ deepcopy reference\n",
        "FAIL_TO_PASS": [f"f{index}" for index in range(operator.F2P_COUNT)],
        "PASS_TO_PASS": [f"p{index}" for index in range(operator.P2P_COUNT)],
        "install_config": {"test_cmd": ["pytest"], "log_parser": "pytest"},
        "problem_statement": "public",
        "language": "python",
        "license": "MIT",
    }
    monkeypatch.setattr(operator, "_verify_image", lambda: {"category": "IMAGE_VERIFIED"})
    monkeypatch.setattr(operator, "_load_official_row", lambda: row)
    monkeypatch.setattr(operator, "_resolve_model_or_fail", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider setup")))

    def fake_evaluate(_row, patch, _private, *, expose_candidate_hashes=True):
        del expose_candidate_hashes
        if patch == row["patch"]:
            return {"instance_id": operator.INSTANCE_ID, "base_commit": operator.BASE_COMMIT, "process_exit_code": 0, "all_ok": True, "passed_match": True, "fail_to_pass_passed": 5, "pass_to_pass_failed": 0, "error_present": False}
        if patch == "this is intentionally not a unified diff":
            return {"instance_id": operator.INSTANCE_ID, "base_commit": operator.BASE_COMMIT, "process_exit_code": 1, "all_ok": False, "passed_match": False, "fail_to_pass_passed": 0, "pass_to_pass_failed": 9, "error_present": True}
        return {"instance_id": operator.INSTANCE_ID, "base_commit": operator.BASE_COMMIT, "process_exit_code": 1, "all_ok": False, "passed_match": False, "fail_to_pass_passed": 0, "pass_to_pass_failed": 0, "error_present": False, "redacted_test_summary": {"fail_to_pass": {"failed": operator.F2P_COUNT}}}

    monkeypatch.setattr(operator, "_official_evaluate", fake_evaluate)
    output = tmp_path / "gate"
    assert operator._run_integrity_gate(output) == 0
    report = json.loads((output / "integrity-gate.json").read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert report["provider_model_execution_started"] is False
    assert report["control_acceptance"] == {"baseline": True, "reference": True, "intentionally_bad": True}
