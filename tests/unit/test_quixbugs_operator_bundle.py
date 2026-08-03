"""Operator bundle materialization tests: authorization/config cross-binding,
observed execution-commit binding, dirty-Git and drift rejection, route drift,
template values, unknown fields, malformed paths, and contradictory
subscription/fallback assertions."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))

import quixbugs_opencode_go_adapter as adapter
import quixbugs_live_runner_v2 as runner
import quixbugs_paired_pilot as pilot


MODEL = "opencode-go/deepseek-v4-flash"
VARIANT = "max"
ATTEMPT = "quixbugs-paired-pilot-v2-attempt-" + "a" * 64
AUTH_ID = "operator-auth-20260803-001"
#: A clean descendant HEAD different from the task baseline; the bundle must
#: accept it and bind the generated artifacts to exactly this commit.
OBSERVED_HEAD = "a" * 40


def _now() -> datetime:
    return datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _completed(command: list[str], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, "")


class FakeGit:
    """Deterministic read-only Git command runner for the bundle tests.

    Expresses the execution-commit observation contract: resolved HEAD,
    existence, descent from the accepted project baseline and from the task
    lineage baseline, and the tracked/index/untracked cleanliness inventory.
    ``drift_head`` / ``drift_porcelain`` make the second observation (the
    pre-materialization recheck) differ from the first.
    """

    def __init__(
        self,
        *,
        head: str = OBSERVED_HEAD,
        exists: bool = True,
        descends_project: bool = True,
        descends_task: bool = True,
        porcelain: tuple[str, ...] = (),
        ignored_untracked: tuple[str, ...] = (),
        drift_head: str | None = None,
        drift_porcelain: tuple[str, ...] | None = None,
    ) -> None:
        self.head = head
        self.exists = exists
        self.descends_project = descends_project
        self.descends_task = descends_task
        self.porcelain = list(porcelain)
        self.ignored_untracked = set(ignored_untracked)
        self.drift_head = drift_head
        self.drift_porcelain = list(drift_porcelain) if drift_porcelain is not None else None
        self.calls: list[list[str]] = []
        self.observed_heads: list[str] = []
        self._rev_parse_calls = 0
        self._status_calls = 0

    def run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if command == ["git", "rev-parse", "HEAD"]:
            self._rev_parse_calls += 1
            head = self.drift_head if self.drift_head is not None and self._rev_parse_calls > 1 else self.head
            self.observed_heads.append(head)
            return _completed(command, stdout=head + "\n")
        if command[:3] == ["git", "cat-file", "-e"]:
            return _completed(command, returncode=0 if self.exists else 1)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            baseline = command[3]
            descends = self.descends_project if baseline == runner.ACCEPTED_BASELINE else self.descends_task
            return _completed(command, returncode=0 if descends else 1)
        if command[:3] == ["git", "status", "--porcelain"]:
            self._status_calls += 1
            lines = self.drift_porcelain if self.drift_porcelain is not None and self._status_calls > 1 else self.porcelain
            return _completed(command, stdout="\n".join(lines))
        if command[:4] == ["git", "check-ignore", "-q", "--"]:
            path = command[4]
            return _completed(command, returncode=0 if path in self.ignored_untracked else 1)
        raise AssertionError(f"unexpected git command: {command}")


def _route_evidence(now: datetime, **overrides) -> dict:
    value = {
        "schema_version": adapter.ROUTE_EVIDENCE_SCHEMA_VERSION,
        "provider": pilot.SUBSCRIPTION_ROUTE_PROVIDER,
        "model": pilot.SUBSCRIPTION_ROUTE_MODEL,
        "variant": VARIANT,
        "protocol": runner.LIVE_PROTOCOL_VERSION,
        "opencode_version": "1.18.10",
        "catalog_fingerprint": "e" * 64,
        "runtime_model_id": MODEL,
        "billing_route": "SUBSCRIPTION",
        "subscription_entitlement_confirmed": True,
        "account_status": "ACTIVE",
        "active_model_status": "ACTIVE",
        "variant_available": True,
        "input_price": 0.5,
        "output_price": 1.5,
        "provider_reported_cost": 0.0,
        "paid_fallback_used": False,
        "alternate_provider_used": False,
        "ollama_used": False,
        "zen_used": False,
        "free_tier_used": False,
        "metered_fallback_used": False,
        "paid_overage_used": False,
        "per_call_billing_used": False,
        "model_substitution_observed": False,
        "observed_at": _iso(now),
    }
    value.update(overrides)
    return value


def _write_evidence(tmp_path: Path, evidence: dict) -> Path:
    path = tmp_path / "route-evidence.json"
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _bundle(tmp_path: Path, evidence: dict, *, now: datetime | None = None, git: FakeGit | None = None, **kwargs) -> dict:
    now = now if now is not None else _now()
    evidence_path = _write_evidence(tmp_path, evidence)
    defaults = {
        "manifest_path": None,
        "route_evidence_json": evidence_path,
        "operator_authorization_id": AUTH_ID,
        "attempt_identity": ATTEMPT,
        "output_root": tmp_path / "attempts" / ATTEMPT,
        "valid_until": _iso(now + timedelta(days=7)),
        "entitlement_evidence_reference": "operator-account-observation-20260803-001",
        "python_executable": sys.executable,
        "working_directory": tmp_path,
        "operator_boundary_root": adapter.common_operator_boundary([sys.executable, tmp_path]),
        "bundle_root": tmp_path / "bundles" / ATTEMPT,
        "git_runner": (git if git is not None else FakeGit()).run,
        "now": now,
    }
    defaults.update(kwargs)
    return adapter.run_operator_bundle(**defaults)


def test_bundle_materializes_authorization_and_configuration_cross_bound(tmp_path) -> None:
    now = _now()
    result = _bundle(tmp_path, _route_evidence(now), now=now)
    assert result["materialized"] is True
    assert result["execution_commit"] == OBSERVED_HEAD
    assert result["independently_observed_head"] == OBSERVED_HEAD
    assert result["task_baseline"] == adapter.TASK_BASELINE == "618c33ff186493892665ca1233c3edd8b2eec13f"
    assert OBSERVED_HEAD != adapter.TASK_BASELINE
    assert result["provider_processes_created"] == 0
    assert result["catalog_fingerprint"] == "e" * 64
    assert result["frozen_case_ids"] == [case["case_id"] for case in pilot.load_manifest(pilot.MANIFEST_PATH_V2)["case_order"]]
    assert len(result["frozen_case_ids"]) == 6

    authorization = json.loads(Path(result["authorization_path"]).read_text(encoding="utf-8"))
    configuration = json.loads(Path(result["configuration_path"]).read_text(encoding="utf-8"))
    manifest = pilot.load_manifest(pilot.MANIFEST_PATH_V2)

    runner.validate_authorization_artifact(authorization, manifest, expected_output_root=result["output_root"], now=now)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    assert validated["authorization_hash"] == runner.authorization_hash(authorization)
    assert validated["execution_commit"] == OBSERVED_HEAD
    assert validated["catalog_fingerprint"] == authorization["expected_catalog_fingerprint"] == "e" * 64
    assert validated["runtime_model_id"] == authorization["expected_runtime_model_id"] == MODEL
    assert validated["variant"] == authorization["variant"] == VARIANT
    assert validated["opencode_version"] == authorization["expected_opencode_version"] == "1.18.10"
    assert validated["expected_account_status"] == authorization["expected_account_status"] == "ACTIVE"
    assert validated["route_class"] == pilot.AUTHORIZED_BILLING_ROUTE
    assert authorization["accepted_campaign_commit"] == OBSERVED_HEAD
    assert authorization["permitted_case_ids"] == [case["case_id"] for case in manifest["case_order"]]
    assert authorization["protocol"] == runner.LIVE_PROTOCOL_VERSION
    assert authorization["operator_authorization_id"] == AUTH_ID
    assert authorization["campaign_attempt_identity"] == ATTEMPT
    assert authorization["authorization_valid_until"] == _iso(now + timedelta(days=7))
    assert authorization["subscription_account_observation"]["evidence_reference"] == "operator-account-observation-20260803-001"

    pairs = {(validated["command"][i], validated["command"][i + 1]) for i in range(len(validated["command"]) - 1)}
    assert ("--expected-catalog-fingerprint", "e" * 64) in pairs
    assert ("--expected-runtime-model-id", MODEL) in pairs
    assert ("--expected-billing-route", "SUBSCRIPTION") in pairs


def test_bundle_observed_head_is_used_consistently_in_all_bindings(tmp_path) -> None:
    now = _now()
    evidence = _route_evidence(now)
    result = _bundle(tmp_path, evidence, now=now)
    authorization = json.loads(Path(result["authorization_path"]).read_text(encoding="utf-8"))
    configuration = json.loads(Path(result["configuration_path"]).read_text(encoding="utf-8"))
    manifest = pilot.load_manifest(pilot.MANIFEST_PATH_V2)
    verdict = runner.run_route_preflight(
        manifest, authorization, lambda: _route_evidence(now),
        now=now, attempt_identity=ATTEMPT, execution_commit=result["execution_commit"],
    )
    assert verdict.passed is True
    observation = verdict.route_observation
    assert observation["execution_commit"] == OBSERVED_HEAD
    bound = adapter.bind_adapter_configuration(configuration, manifest, authorization, observation)
    binding = adapter.build_runtime_identity_binding(authorization, observation, bound)
    assert authorization["accepted_campaign_commit"] == OBSERVED_HEAD
    assert configuration["execution_commit"] == OBSERVED_HEAD
    assert observation["execution_commit"] == OBSERVED_HEAD
    assert binding.execution_commit == OBSERVED_HEAD
    assert result["execution_commit"] == OBSERVED_HEAD
    assert result["runtime_identity_binding_fingerprint"] == binding.fingerprint()


def test_bundle_task_baseline_is_retained_only_as_lineage_requirement(tmp_path) -> None:
    now = _now()
    evidence = _route_evidence(now)
    git = FakeGit(head=OBSERVED_HEAD)
    result = _bundle(tmp_path, evidence, now=now, git=git)
    assert result["execution_commit"] == OBSERVED_HEAD != adapter.TASK_BASELINE
    authorization = json.loads(Path(result["authorization_path"]).read_text(encoding="utf-8"))
    assert authorization["accepted_campaign_commit"] == OBSERVED_HEAD != adapter.TASK_BASELINE
    assert adapter.TASK_BASELINE == "618c33ff186493892665ca1233c3edd8b2eec13f"
    assert not hasattr(adapter, "CAMPAIGN_EXECUTION_COMMIT")
    # The task baseline is still a hard lineage requirement: a HEAD that
    # descends from the project baseline but not from the task baseline is
    # rejected.
    no_lineage = FakeGit(head=OBSERVED_HEAD, descends_project=True, descends_task=False)
    with pytest.raises(runner.RepositoryStateError) as exc:
        _bundle(tmp_path, evidence, now=now, git=no_lineage)
    assert exc.value.category == "EXECUTION_COMMIT_ANCESTRY_FAILED"
    assert "task lineage baseline" in exc.value.detail
    # No rev-parse observation ever returns the task baseline as the HEAD.
    assert no_lineage.observed_heads == [OBSERVED_HEAD]
    assert all(observed != adapter.TASK_BASELINE for observed in no_lineage.observed_heads)


def test_bundle_authorization_configuration_fingerprint_is_the_same_contract(tmp_path) -> None:
    now = _now()
    evidence = _route_evidence(now)
    result = _bundle(tmp_path, evidence, now=now)
    authorization = json.loads(Path(result["authorization_path"]).read_text(encoding="utf-8"))
    configuration = json.loads(Path(result["configuration_path"]).read_text(encoding="utf-8"))
    fingerprint = evidence["catalog_fingerprint"]
    assert authorization["expected_catalog_fingerprint"] == configuration["catalog_fingerprint"] == fingerprint
    assert authorization["expected_runtime_model_id"] == configuration["runtime_model_id"] == evidence["runtime_model_id"]
    assert authorization["expected_opencode_version"] == configuration["opencode_version"] == evidence["opencode_version"]
    assert authorization["expected_account_status"] == configuration["expected_account_status"] == evidence["account_status"]


def test_bundle_rejects_nonexistent_head(tmp_path) -> None:
    git = FakeGit(head=OBSERVED_HEAD, exists=False)
    with pytest.raises(runner.RepositoryStateError) as exc:
        _bundle(tmp_path, _route_evidence(_now()), git=git)
    assert exc.value.category == "EXECUTION_COMMIT_NOT_FOUND"
    assert not (tmp_path / "bundles").exists()


def test_bundle_rejects_head_not_descending_from_project_baseline(tmp_path) -> None:
    git = FakeGit(head=OBSERVED_HEAD, descends_project=False)
    with pytest.raises(runner.RepositoryStateError) as exc:
        _bundle(tmp_path, _route_evidence(_now()), git=git)
    assert exc.value.category == "EXECUTION_COMMIT_ANCESTRY_FAILED"
    assert "accepted project baseline" in exc.value.detail


def test_bundle_rejects_dirty_or_staged_source(tmp_path) -> None:
    dirty = FakeGit(head=OBSERVED_HEAD, porcelain=(" M quixbugs_opencode_go_adapter.py",))
    with pytest.raises(runner.RepositoryStateError) as exc:
        _bundle(tmp_path, _route_evidence(_now()), git=dirty)
    assert exc.value.category == "TRACKED_STATE_DIRTY"
    assert not (tmp_path / "bundles").exists()
    staged = FakeGit(head=OBSERVED_HEAD, porcelain=("M  quixbugs_opencode_go_adapter.py",))
    with pytest.raises(runner.RepositoryStateError) as exc:
        _bundle(tmp_path, _route_evidence(_now()), git=staged)
    assert exc.value.category == "TRACKED_STATE_DIRTY"


def test_bundle_rejects_non_ignored_untracked_files_but_allows_ignored(tmp_path) -> None:
    untracked = FakeGit(head=OBSERVED_HEAD, porcelain=("?? untracked-source.py",))
    with pytest.raises(runner.RepositoryStateError) as exc:
        _bundle(tmp_path, _route_evidence(_now()), git=untracked)
    assert exc.value.category == "TRACKED_STATE_DIRTY"
    assert "untracked-source.py" in exc.value.detail
    ignored = FakeGit(head=OBSERVED_HEAD, porcelain=("?? operator/ignored-artifact.json",), ignored_untracked=("operator/ignored-artifact.json",))
    result = _bundle(tmp_path, _route_evidence(_now()), git=ignored)
    assert result["materialized"] is True


def test_bundle_rejects_head_drift_between_observation_and_materialization(tmp_path) -> None:
    drifting = FakeGit(head=OBSERVED_HEAD, drift_head="b" * 40)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="execution-commit drift"):
        _bundle(tmp_path, _route_evidence(_now()), git=drifting)
    assert not (tmp_path / "bundles" / ATTEMPT / "authorization.json").exists()
    assert not (tmp_path / "bundles" / ATTEMPT / "adapter-config.json").exists()


def test_bundle_rejects_dirty_recheck_between_observation_and_materialization(tmp_path) -> None:
    dirtying = FakeGit(head=OBSERVED_HEAD, drift_porcelain=(" M quixbugs_opencode_go_adapter.py",))
    with pytest.raises(runner.RepositoryStateError) as exc:
        _bundle(tmp_path, _route_evidence(_now()), git=dirtying)
    assert exc.value.category == "TRACKED_STATE_DIRTY"
    assert not (tmp_path / "bundles").exists()


def test_bundle_rejects_occupied_targets(tmp_path) -> None:
    now = _now()
    evidence = _route_evidence(now)
    output_root = tmp_path / "attempts" / ATTEMPT
    output_root.mkdir(parents=True)
    (output_root / "existing.txt").write_text("occupied", encoding="utf-8")
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="occupied"):
        _bundle(tmp_path, evidence, now=now, output_root=output_root)
    bundle_root = tmp_path / "bundles" / ATTEMPT
    bundle_root.mkdir(parents=True)
    (bundle_root / "leftover.json").write_text("{}", encoding="utf-8")
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="occupied"):
        _bundle(tmp_path, evidence, now=now, bundle_root=bundle_root)


def test_bundle_create_once_rejects_second_materialization(tmp_path) -> None:
    now = _now()
    evidence = _route_evidence(now)
    result = _bundle(tmp_path, evidence, now=now)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="occupied"):
        _bundle(tmp_path, evidence, now=now, bundle_root=Path(result["bundle_root"]), output_root=Path(result["output_root"]))


def test_bundle_rejects_template_values(tmp_path) -> None:
    evidence = _route_evidence(_now(), account_status="<expected account status>")
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="placeholder"):
        _bundle(tmp_path, evidence)


def test_bundle_rejects_unknown_fields(tmp_path) -> None:
    evidence = _route_evidence(_now(), unrelated_extra_field=True)
    with pytest.raises(runner.RouteEvidenceInvalid) as exc:
        _bundle(tmp_path, evidence)
    assert exc.value.reason == "UNKNOWN_FIELD"


def test_bundle_rejects_missing_acceptance_critical_field(tmp_path) -> None:
    evidence = _route_evidence(_now())
    del evidence["catalog_fingerprint"]
    with pytest.raises(runner.RouteEvidenceInvalid) as exc:
        _bundle(tmp_path, evidence)
    assert exc.value.reason == "MISSING_FIELD"


def test_bundle_rejects_route_drift(tmp_path) -> None:
    evidence = _route_evidence(_now(), variant="low")
    with pytest.raises(runner.LiveRunnerError, match="ROUTE_MISMATCH"):
        _bundle(tmp_path, evidence)
    evidence = _route_evidence(_now(), model="other-model")
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="drift"):
        _bundle(tmp_path, evidence)


def test_bundle_rejects_contradictory_subscription_fallback_assertions(tmp_path) -> None:
    evidence = _route_evidence(_now(), zen_used=True)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="did not pass the pre-provider gate"):
        _bundle(tmp_path, evidence)
    evidence = _route_evidence(_now(), billing_route="ZEN")
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="billing route"):
        _bundle(tmp_path, evidence)
    evidence = _route_evidence(_now(), subscription_entitlement_confirmed=False)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="does not confirm subscription entitlement"):
        _bundle(tmp_path, evidence)


def test_bundle_rejects_stale_evidence(tmp_path) -> None:
    now = _now()
    stale = _route_evidence(now - timedelta(hours=2))
    with pytest.raises(runner.RouteEvidenceInvalid) as exc:
        _bundle(tmp_path, stale, now=now)
    assert exc.value.reason == "STALE_TIMESTAMP"


def test_bundle_rejects_historical_zen_identity(tmp_path) -> None:
    evidence = _route_evidence(_now(), runtime_model_id=adapter.HISTORICAL_ZEN_MODEL_ID)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="historical OpenCode Zen"):
        _bundle(tmp_path, evidence)


def test_bundle_rejects_malformed_paths_and_bad_validity(tmp_path) -> None:
    now = _now()
    evidence = _route_evidence(now)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="Python executable"):
        _bundle(tmp_path, evidence, python_executable=tmp_path / "missing-python.exe")
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="working directory"):
        _bundle(tmp_path, evidence, working_directory=tmp_path / "missing-dir")
    outside = tmp_path.parent
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="outside the operator boundary"):
        _bundle(tmp_path, evidence, operator_boundary_root=outside)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="validity period"):
        _bundle(tmp_path, evidence, valid_until="not-a-timestamp")
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="later than materialization"):
        _bundle(tmp_path, evidence, valid_until=_iso(now - timedelta(days=1)))
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="attempt identity is invalid"):
        _bundle(tmp_path, evidence, attempt_identity="quixbugs-paired-pilot-v2-attempt-nope")


def test_bundle_default_bundle_root_lives_in_operator_storage(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "operator"
    monkeypatch.setattr(adapter, "OPERATOR_STORAGE", storage)
    now = _now()
    result = _bundle(tmp_path, _route_evidence(now), now=now, bundle_root=None)
    assert Path(result["bundle_root"]).resolve() == (storage / adapter.OPERATOR_BUNDLES_RELATIVE_DIR / ATTEMPT).resolve()


def test_bundle_binds_route_observation_and_binding_fingerprint(tmp_path) -> None:
    now = _now()
    result = _bundle(tmp_path, _route_evidence(now), now=now)
    authorization = json.loads(Path(result["authorization_path"]).read_text(encoding="utf-8"))
    configuration = json.loads(Path(result["configuration_path"]).read_text(encoding="utf-8"))
    manifest = pilot.load_manifest(pilot.MANIFEST_PATH_V2)
    verdict = runner.run_route_preflight(
        manifest, authorization, lambda: _route_evidence(now),
        now=now, attempt_identity=ATTEMPT, execution_commit=result["execution_commit"],
    )
    assert verdict.passed is True
    bound = adapter.bind_adapter_configuration(configuration, manifest, authorization, verdict.route_observation)
    binding = adapter.build_runtime_identity_binding(authorization, verdict.route_observation, bound)
    assert binding.fingerprint() == result["runtime_identity_binding_fingerprint"]
    assert binding.catalog_fingerprint == "e" * 64
    assert binding.execution_commit == OBSERVED_HEAD
