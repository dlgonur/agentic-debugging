"""Professor-facing structured debugger trace export for the R6 evidence.

``professor_debug_trace_v1`` derives one clean, deterministic,
JSON-schema-validated trace per task from the REAL debugger executions of
the accepted R6 model evaluation.  This module is the deterministic exporter
behind ``docs/professor_traces``:

- PRIMARY set — the completed, accepted, contamination-safe checkpoint-30
  disjoint QuixBugs validation (8/8 independently verifier-confirmed
  RESOLVED).  This is the professor-facing R6 trace set.
- PARTIAL-HOLDOUT appendix — the two surviving completed rows of the final
  five-task curated holdout (RESOLVED and BREAKING_RESOLVED), exported under
  an explicit ``final_holdout_partial`` scope that is NEVER mixed into the
  primary success set.  The BREAKING_RESOLVED row is preserved honestly:
  F2P repaired, P2P regression remains, accepted outcome != RESOLVED.

Nothing is fabricated: every trace field is derived from the frozen
``debugger-interaction-v2-r5-evidence`` records (real model commands, real
tool actions, real stack/locals observations, real diagnoses, real candidate
hashes, real independent-verifier outcomes).  Absent evidence is exported
with explicit ``null`` / ``NOT_RECORDED`` / ``NOT_APPLICABLE`` semantics.

Anti-leakage: every exported trace passes an actual-output professor-safe
audit.  The audit derives forbidden content mechanically from the hidden
test assets (reusing the accepted ``anti_leakage`` authority) and scans the
exported trace JSON as if it were a prompt; any hidden test source, node id,
assertion expression, expected literal, oracle root cause, reference repair
snippet, or chain-of-thought reconstruction is a FAIL-CLOSED error.  Hidden
per-test node ids are therefore never exported (counts only).

Determinism: identical frozen evidence produces byte-identical traces and
indexes (stable key order, ``sort_keys=True``, ``allow_nan=False``).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Optional

from agentic_debugger.evaluation.professor_trace import (
    _sha256,
    build_trace,
    validate_trace,
)

try:  # accepted anti-leakage authority (AUDIT ONLY; never prompt-facing)
    from experiments.debugger_interaction_v2_r5.anti_leakage import (
        ForbiddenContent,
        _evidence_diagnosis_texts,
        _hidden_test_assets,
        _strip_lines,
        derive_forbidden_content,
        scan_prompt,
    )
except ImportError:  # pragma: no cover - repository layout guard
    ForbiddenContent = None  # type: ignore[assignment]
    _evidence_diagnosis_texts = None  # type: ignore[assignment]
    _hidden_test_assets = None  # type: ignore[assignment]
    _strip_lines = None  # type: ignore[assignment]
    derive_forbidden_content = None  # type: ignore[assignment]
    scan_prompt = None  # type: ignore[assignment]

SCHEMA_VERSION = "professor_debug_trace_v1"
TRACE_FILE_PREFIX = "professor_debug_trace_"

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Frozen evidence registry (reconstruction target)
#
# The exporter refuses to run against mismatched evidence.  The ``sha256``
# values below are the exact accepted R6 evidence records behind the 8/8
# validation result and the surviving partial holdout rows (the accepted
# checkpoint-selection record and the stage A/B/C eval reports pin the same
# runs via their own hashes; the evidence hashes here are additional direct
# evidence identity).
#
# Default source-root resolution is a frozen-first-match policy:
#   1. ``experiments/r6_debugger_training/runs/frozen`` (tracked pinned
#      reconstruction fixture — no GPU, no external paths);
#   2. the accepted in-repo review package ``_ai-review/R6-HARDWARE-STOP``;
#   3. the original run trees under ``C:/tmp/r6-bounded`` (live-captured).
# ---------------------------------------------------------------------------

EVIDENCE_CONTRACT_SHA256 = "5e56165d9b08d24836874711caef306f062f5d36dc4cdbb020d97e7370ca8e78"
BASE_REPOSITORY = "Qwen/Qwen2.5-Coder-7B-Instruct"
BASE_REVISION = "c03e6d358207e414f1eca0bb1891e29f1db0e242"
FINE_TUNED_CHECKPOINT = "checkpoint-30"
ADAPTER_MODEL_SHA256 = "7ef5d70ab8691ea02f005ec567901932e08fb94b28ebbfab5b175a94ebb492bd"
ADAPTER_CONFIG_SHA256 = "92ddf91e67b116a6730792722d6ee93dffeaac152901cd954389615e50cbd44e"
SELECTED_ADAPTER_PATH = (
    "experiments/r6_debugger_training/runs/r6-sft-debugger-v3/trainer/checkpoint-30"
)
TRAINING_PROVENANCE = (
    "Project SFT/QLoRA fine-tuning of the debugger (train_qlora.py, "
    "run_bounded_training.py) over the disjoint QuixBugs training split; "
    "selected by disjoint validation only (holdout_used_for_selection=false)."
)
#: Exact evaluator runtime recorded in the accepted lifecycle logs.
PYTHON_VERSION = "3.10.1"

#: Frozen accepted R6 evidence identity: task_id -> evidence sha256.
#: Exporter fails closed on any mismatch.
FROZEN_VALIDATION_EVIDENCE: dict[str, str] = {
    "quixbugs-depth-first-search": "fc019e272d1bb4f14c1251c394b3759fc97b99963bde561a2652980424114cdf",
    "quixbugs-quicksort": "b2247b182154ee6f7d46a17dc7574b8fe70e0b100f151cbb1cd5eb591809380d",
    "quixbugs-flatten": "a5f0d4bb26ad8bc60e3ad60a921820fe32d87f2eac92807568b8b924a4537778",
    "quixbugs-find-in-sorted": "5ae6fe53700780b6dadd3412c56304ac870379a18cfc25e85fbf937582d650b6",
    "quixbugs-rpn-eval": "69f1ec0025d4988eab772676ca9e1973bb11c8a49f4748b3f540e8340665e3ee",
    "quixbugs-shortest-path-length": "84d01973a08961971b896c245c223ee0bfb9c58db2dd613bf0e2ebd83d52c239",
    "quixbugs-reverse-linked-list": "8530f95043f7ca0c5e70e83506420c4131e2441a0cbc3896a3d6b492266df47a",
    "quixbugs-kth": "49c89a8d933c6406f6f80bd22004e3edb16ac615bb46dff203e5fc040a9b901c",
}

FROZEN_HOLDOUT_EVIDENCE: dict[str, str] = {
    "curated-none-handling-001": "ff01c714d1736da4eaf6c97194e42d3af043e84b4a18ab0563cbe62443566109",
    "curated-off-by-one-002": "27cae9d4e1d8f292483898c1b525287f8f155796c620e78434ad31d774eb746d",
}

FROZEN_ANCILLARY: dict[str, str] = {
    "checkpoint_selection": "f966ddee6dad353e9f2887be4a70ed4036abef0b479d80fc2d806ec2b79b9ee4",
    "stage_a_report": "3fdfd517dd379d2fdb9f74820ceaf444b4b129103477b37baf18506336905967",
    "stage_b_report": "677ab79a7513a8a00cc50f66ba9bc37e36416f458b71fce7edcf19969f03baa7",
    "stage_c_report": "f7e24be432675b94ebea6167f37a41ddcfcde276c21e891bc7ebf603c6311d85",
    "holdout_report": "ee77c88b0a44ddc8da1056a9efff13079735fabc83f08b42b32bb6f3c65d1f00",
}

#: Expected verifier outcomes for the primary set: every exported validation
#: trace must be independently verifier-confirmed RESOLVED.
VALIDATION_OUTCOMES: dict[str, str] = {
    task_id: "RESOLVED" for task_id in FROZEN_VALIDATION_EVIDENCE
}

#: Accepted partial-holdout outcomes (preserved exactly, including the
#: BREAKING_RESOLVED rejection).
HOLDOUT_OUTCOMES: dict[str, str] = {
    "curated-none-handling-001": "RESOLVED",
    "curated-off-by-one-002": "BREAKING_RESOLVED",
}

#: Tracked curated fixture directories (hidden-test assets for the audit).
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"

#: Gold repair diffs for the disjoint QuixBugs validation set.  These are
#: AUDIT-ONLY assets: the added lines of the gold diff are reference-repair
#: needles, exactly like the accepted catalog ``reference_repair`` snippets.
#: They are never exported and never shown to a model.
GOLD_DIFF_DIR = REPO_ROOT / "experiments" / "r6_debugger_training" / "gold"


class EvidenceRegistry:
    """Frozen accepted-evidence identity used by the exporter.

    The default instance pins the real accepted R6 evidence.  Tests may
    inject a synthetic registry built over synthetic evidence; the
    production default is fail-closed on the accepted hashes.
    """

    def __init__(
        self,
        *,
        validation: dict[str, str],
        final_holdout_partial: dict[str, str],
        outcomes: dict[str, str],
        ancillary: dict[str, str],
        contract_sha256: str,
        base_repository: str,
        base_revision: str,
    ) -> None:
        self.validation = validation
        self.final_holdout_partial = final_holdout_partial
        #: Accepted verifier outcomes keyed ``"<scope>:<task_id>"`` — the
        #: same task id can carry a different accepted outcome in a
        #: different scope (e.g. holdout BREAKING_RESOLVED).
        self.outcomes = outcomes
        self.ancillary = ancillary
        self.contract_sha256 = contract_sha256
        self.base_repository = base_repository
        self.base_revision = base_revision


FROZEN_REGISTRY = EvidenceRegistry(
    validation=FROZEN_VALIDATION_EVIDENCE,
    final_holdout_partial=FROZEN_HOLDOUT_EVIDENCE,
    outcomes={
        **{f"validation:{t}": "RESOLVED" for t in FROZEN_VALIDATION_EVIDENCE},
        **{f"final_holdout_partial:{t}": o for t, o in HOLDOUT_OUTCOMES.items()},
    },
    ancillary=FROZEN_ANCILLARY,
    contract_sha256=EVIDENCE_CONTRACT_SHA256,
    base_repository=BASE_REPOSITORY,
    base_revision=BASE_REVISION,
)


# ---------------------------------------------------------------------------
# Evidence source resolution
# ---------------------------------------------------------------------------


class EvidenceResolver:
    """Resolve evidence paths from one accepted source root.

    Supported root shapes (matched in order):
      - tracked frozen capsule
        ``<root>/validation/<task_id>/evidence.json``,
        ``<root>/final_holdout_partial/<task_id>/evidence.json``,
        ``<root>/ancillary/<key>.json``,
        ``<root>/quixbugs_audit_needles/<task_id>.json``,
        ``<root>/capsule_manifest.json``;
      - review package ``_ai-review/R6-HARDWARE-STOP``: holdout records in
        ``<root>/interrupted-holdout/completed-evidence/<task_id>.json`` and
        ancillary records in ``selection/`` / ``validation/`` (validation
        evidence is then read from the live run trees under
        ``C:/tmp/r6-bounded``);
      - live run tree root ``C:/tmp/r6-bounded``:
        ``<root>/v3c30-r68-{a,b,c}-7c9881/adapter-checkpoint-30/
        <task_id>/evidence.json`` and
        ``<root>/v3c30-r68-final-holdout-7c9881-f966dd/adapter-checkpoint-30/
        <task_id>/evidence.json``.

    The capsule is a DERIVED record of the accepted raw evidence: its
    identity is verified against the registry (raw evidence SHA256) via the
    capsule manifest chain of custody, so reading a capsule is never weaker
    than reading the raw record.
    """

    _LIVE_STAGE_DIRS = ("v3c30-r68-a-7c9881", "v3c30-r68-b-7c9881",
                        "v3c30-r68-c-7c9881")
    _LIVE_HOLDOUT_DIR = "v3c30-r68-final-holdout-7c9881-f966dd"
    _PKG = REPO_ROOT / "_ai-review" / "R6-HARDWARE-STOP"
    _LIVE = Path("C:/tmp/r6-bounded")

    def __init__(
        self,
        root: Path,
        *,
        pkg_root: Optional[Path] = None,
        live_root: Optional[Path] = None,
    ) -> None:
        self.root = root
        #: Optional overrides keep the fallback lookups hermetic in tests.
        self._pkg = pkg_root if pkg_root is not None else self._PKG
        self._live = live_root if live_root is not None else self._LIVE

    # -- frozen fixture shape -------------------------------------------------
    def _frozen(self, sub: str, task_id: str) -> Path:
        return self.root / sub / task_id / "evidence.json"

    def validation_evidence_path(self, task_id: str) -> Path:
        frozen = self._frozen("validation", task_id)
        if frozen.is_file():
            return frozen
        if self._live.is_dir():
            for stage_dir in self._LIVE_STAGE_DIRS:
                candidate = (
                    self._live / stage_dir / "adapter-checkpoint-30" / task_id
                    / "evidence.json"
                )
                if candidate.is_file():
                    return candidate
        package = self._pkg / "interrupted-holdout" / "completed-evidence" / f"{task_id}.json"
        if package.is_file():
            return package
        raise FileNotFoundError(
            f"validation evidence for {task_id!r} not found under {self.root}"
        )

    def holdout_evidence_path(self, task_id: str) -> Path:
        frozen = self._frozen("final_holdout_partial", task_id)
        if frozen.is_file():
            return frozen
        package = self._pkg / "interrupted-holdout" / "completed-evidence" / f"{task_id}.json"
        if package.is_file():
            return package
        if self._live.is_dir():
            candidate = (
                self._live / self._LIVE_HOLDOUT_DIR / "adapter-checkpoint-30"
                / task_id / "evidence.json"
            )
            if candidate.is_file():
                return candidate
        raise FileNotFoundError(
            f"holdout evidence for {task_id!r} not found under {self.root}"
        )

    def capsule_manifest_path(self) -> Path:
        return self.root / "capsule_manifest.json"

    def audit_needles_path(self, task_id: str) -> Path:
        return self.root / "quixbugs_audit_needles" / f"{task_id}.json"

    def ancillary_path(self, key: str) -> Optional[Path]:
        frozen = self.root / "ancillary" / f"{key}.json"
        if frozen.is_file():
            return frozen
        mapping = {
            "checkpoint_selection": self._pkg / "selection" / "checkpoint-selection.json",
            "stage_a_report": self._pkg / "validation" / "stage-a" / "eval_report.json",
            "stage_b_report": self._pkg / "validation" / "stage-b" / "eval_report.json",
            "stage_c_report": self._pkg / "validation" / "stage-c" / "eval_report.json",
            "holdout_report": self._pkg / "interrupted-holdout" / "eval_report.json",
        }
        candidate = mapping.get(key)
        if candidate is not None and candidate.is_file():
            return candidate
        return None


def resolve_default_evidence_root() -> Path:
    """Return the accepted R6 evidence root using the frozen-first-match
    policy: tracked frozen evidence capsule, then the accepted in-repo
    review package, then the live run trees."""
    frozen = (
        REPO_ROOT / "experiments" / "r6_debugger_training" / "runs" / "frozen"
    )
    if frozen.is_dir():
        return frozen
    package = REPO_ROOT / "_ai-review" / "R6-HARDWARE-STOP"
    if package.is_dir():
        return package
    live = Path("C:/tmp/r6-bounded")
    if live.is_dir():
        return live
    raise FileNotFoundError(
        "no accepted R6 evidence root found; pass --evidence-root explicitly"
    )


# ---------------------------------------------------------------------------
# Evidence identity verification (fail closed)
# ---------------------------------------------------------------------------


def _load_capsule_manifest(resolver: EvidenceResolver) -> Optional[dict[str, Any]]:
    """Load the tracked capsule manifest when the evidence root is the
    tracked frozen capsule (exact capsule_manifest.json present)."""
    path = resolver.capsule_manifest_path()
    if not path.is_file():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "r6-frozen-evidence-capsule-v1":
        raise RuntimeError("capsule manifest schema_version mismatch")
    return manifest


def _verify_capsule_chain_of_custody(
    resolver: EvidenceResolver,
    registry: EvidenceRegistry,
    evidence_paths: dict[str, Path],
) -> None:
    """Verify the tracked capsule chain of custody (fail closed).

    When the evidence root carries the capsule manifest, every capsule file
    is checked against the manifest's per-file SHA256 AND the manifest's
    per-record raw evidence SHA256 must equal the frozen registry identity.
    This makes the capsule a verified derived record of the accepted raw
    evidence — never weaker provenance, never a synthetic replacement.
    """
    manifest = _load_capsule_manifest(resolver)
    if manifest is None:
        return
    entries = manifest.get("evidence") or {}
    for key, path in evidence_paths.items():
        entry = entries.get(key)
        if entry is None:
            raise RuntimeError(f"capsule manifest entry missing for {key}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != entry.get("capsule_sha256"):
            raise RuntimeError(
                f"capsule sha256 mismatch for {key}: expected "
                f"{entry.get('capsule_sha256')}, got {actual}"
            )
        if entry.get("raw_sha256") != _registry_evidence_sha(registry, key):
            raise RuntimeError(
                f"capsule chain-of-custody mismatch for {key}: manifest raw "
                f"identity {entry.get('raw_sha256')} != frozen registry "
                f"identity"
            )
    for key, expected_sha in registry.ancillary.items():
        entry = (manifest.get("ancillary") or {}).get(key)
        if entry is None:
            raise RuntimeError(f"capsule manifest ancillary entry missing: {key}")
        path = resolver.ancillary_path(key)
        if path is None or hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
            raise RuntimeError(f"capsule ancillary mismatch for {key}")
        if entry.get("sha256") != expected_sha:
            raise RuntimeError(f"capsule ancillary identity != frozen registry: {key}")


def _registry_evidence_sha(registry: EvidenceRegistry, key: str) -> str:
    group, task_id = key.split(":", 1)
    expected = (
        registry.validation
        if group == "validation"
        else registry.final_holdout_partial
    )
    return expected[task_id]


def verify_evidence(
    resolver: EvidenceResolver,
    *,
    include_holdout: bool = True,
    registry: Optional[EvidenceRegistry] = None,
) -> dict[str, Path]:
    """Verify every accepted evidence record identity before export.

    Fails closed (``RuntimeError``) when evidence is missing, a task's
    evidence hash mismatches the frozen identity, the verifier outcome
    diverges from the accepted outcome, or the evidence treatment/model
    identity does not match the accepted frozen checkpoint-30 run.

    When the evidence root is the tracked frozen capsule, the capsule chain
    of custody (capsule SHA256 + raw-evidence SHA256 vs the registry) is
    verified as well.

    Returned paths are keyed ``f"{group}:{task_id}"``.
    """
    registry = registry or FROZEN_REGISTRY
    expected_by_group: dict[str, dict[str, str]] = {
        "validation": registry.validation,
    }
    if include_holdout:
        expected_by_group["final_holdout_partial"] = registry.final_holdout_partial

    evidence_paths: dict[str, Path] = {}
    for group, expected in expected_by_group.items():
        for task_id, expected_sha in expected.items():
            if group == "validation":
                getter = resolver.validation_evidence_path
            else:
                getter = resolver.holdout_evidence_path
            try:
                path = getter(task_id)
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"evidence missing for {group}:{task_id}: {exc}"
                ) from exc
            if not path.is_file():
                raise RuntimeError(f"evidence missing for {task_id!r}: {path}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            capsule = _load_capsule_manifest(resolver)
            if capsule is not None:
                # Capsule root: the registry hash is the RAW evidence
                # identity; the capsule file carries its own hash.
                entry = (capsule.get("evidence") or {}).get(f"{group}:{task_id}")
                if entry is None:
                    raise RuntimeError(
                        f"capsule manifest entry missing for {group}:{task_id}"
                    )
                if actual != entry.get("capsule_sha256"):
                    raise RuntimeError(
                        f"capsule sha256 mismatch for {group}:{task_id}: "
                        f"expected {entry.get('capsule_sha256')}, got {actual}"
                    )
                if entry.get("raw_sha256") != expected_sha:
                    raise RuntimeError(
                        f"capsule chain-of-custody mismatch for "
                        f"{group}:{task_id}: manifest raw identity "
                        f"{entry.get('raw_sha256')} != frozen registry "
                        f"identity {expected_sha}"
                    )
            elif actual != expected_sha:
                raise RuntimeError(
                    f"evidence identity mismatch for {group}:{task_id}: "
                    f"expected sha256 {expected_sha}, got {actual} ({path})"
                )
            evidence_paths[f"{group}:{task_id}"] = path

    for key, path in evidence_paths.items():
        group, task_id = key.split(":", 1)
        evidence = json.loads(path.read_text(encoding="utf-8"))
        verifier = evidence.get("verifier") or {}
        outcome = verifier.get("outcome")
        if key in registry.outcomes:
            expected_outcome = registry.outcomes[key]
            if outcome != expected_outcome:
                raise RuntimeError(
                    f"verifier outcome mismatch for {group}:{task_id}: "
                    f"expected {expected_outcome}, got {outcome!r}"
                )
        run_identity = evidence.get("run_identity") or {}
        contract = run_identity.get("experiment_contract_sha256")
        if contract != registry.contract_sha256:
            raise RuntimeError(
                f"treatment contract mismatch for {group}:{task_id}: "
                f"expected {registry.contract_sha256}, got {contract!r}"
            )
        base = run_identity.get("base_repository")
        revision = run_identity.get("base_revision")
        if base != registry.base_repository or revision != registry.base_revision:
            raise RuntimeError(
                f"model identity mismatch for {group}:{task_id}: "
                f"{base}@{revision} != {registry.base_repository}@{registry.base_revision}"
            )

    for key, expected_sha in registry.ancillary.items():
        path = resolver.ancillary_path(key)
        if path is None:
            raise RuntimeError(f"accepted ancillary record missing: {key}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected_sha:
            raise RuntimeError(
                f"ancillary identity mismatch for {key!r}: "
                f"expected {expected_sha}, got {actual} ({path})"
            )

    _verify_capsule_chain_of_custody(resolver, registry, evidence_paths)
    return evidence_paths


# ---------------------------------------------------------------------------
# Professor-safe leakage audit over exported output (fail closed)
# ---------------------------------------------------------------------------


def _fixture_dir_for(
    task_id: str, curated_root: Optional[Path] = None
) -> Path:
    root = curated_root or CURATED_ROOT
    fixture = root / task_id
    if fixture.is_dir() and (fixture / "task.json").is_file():
        return fixture
    raise FileNotFoundError(
        f"curated fixture for {task_id!r} missing; cannot derive audit "
        f"forbidden content (fail closed)"
    )


def _fixture_available(
    task_id: str, curated_root: Optional[Path] = None
) -> bool:
    root = curated_root or CURATED_ROOT
    fixture = root / task_id
    return fixture.is_dir() and (fixture / "task.json").is_file()


def _frozen_forbidden_content(
    task_id: str, resolver: Optional[EvidenceResolver]
) -> Optional[ForbiddenContent]:
    """Reconstruct AUDIT-ONLY forbidden content from the tracked frozen
    needle capsule (used when the local ignored quixbugs fixture is absent,
    e.g. pristine tracked-only checkouts)."""
    if resolver is None:
        return None
    path = resolver.audit_needles_path(task_id)
    if not path.is_file():
        return None
    frozen = json.loads(path.read_text(encoding="utf-8"))
    if frozen.get("schema_version") != "r6-quixbugs-audit-needles-v2":
        raise RuntimeError(f"frozen audit needles schema mismatch for {task_id}")
    fields = frozen.get("forbidden_content") or {}
    tuple_fields = {
        "f2p_node_ids", "p2p_node_ids", "hidden_test_filenames",
        "hidden_test_function_names", "hidden_test_source_lines",
        "assertion_source_lines", "expected_literals",
        "oracle_target_symbols", "reference_repair_snippets",
        "runtime_probe_call_sources", "runtime_probe_anchors",
        "runtime_probe_focus_functions", "production_source_lines",
    }
    kwargs = {
        key: (tuple(value) if key in tuple_fields and value is not None else value)
        for key, value in fields.items()
    }
    return ForbiddenContent(**kwargs)


def _gold_diff_added_lines(
    task_id: str, gold_diff_dir: Optional[Path] = None
) -> tuple[str, ...]:
    """AUDIT-ONLY: unique non-empty added lines of the tracked gold diff.

    The gold diff is the reference repair; its ``+`` lines are forbidden
    needles exactly like the accepted catalog reference-repair snippets.
    This function never feeds any model-facing path.
    """
    diff_path = (gold_diff_dir or GOLD_DIFF_DIR) / f"{task_id}.patch"
    if not diff_path.is_file():
        return ()
    lines: list[str] = []
    for line in diff_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            stripped = line[1:].strip()
            if stripped:
                lines.append(stripped)
    # De-duplicate preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for item in lines:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def derive_forbidden_content_scoped(
    task_id: str,
    fixture_dir: Path,
    *,
    gold_diff_dir: Optional[Path] = None,
) -> ForbiddenContent:
    """Derive AUDIT-ONLY forbidden content for one task, fail closed.

    - Catalog-curated tasks (the five curated holdout fixtures) reuse the
      accepted authority unchanged (hidden tests + catalog RuntimeProbe
      semantics + reference repair snippets).
    - QuixBugs validation tasks have no catalog scenario (interactive mode
      uses no RuntimeProbe), so the forbidden content is derived
      mechanically from the tracked fixture: hidden test assets, task.json
      oracle fields, and gold-diff added lines as reference-repair needles.
    """
    from agentic_debugger.demo.catalog import DemoCatalogError, scenario_for

    try:
        scenario_for(task_id)
    except DemoCatalogError:
        pass
    else:
        return derive_forbidden_content(task_id, fixture_dir)

    task_meta = json.loads(
        (fixture_dir / "task.json").read_text(encoding="utf-8")
    )
    tests = task_meta.get("tests", {})
    f2p = tuple(tests.get("fail_to_pass", []) or [])
    p2p = tuple(tests.get("pass_to_pass", []) or [])
    oracle = task_meta.get("oracle", {}) or {}
    hidden = _hidden_test_assets(fixture_dir / "tests")

    production_source_lines: list[str] = []
    module_path: Optional[str] = None
    for name in (
        (task_meta.get("constraints", {}) or {}).get("allowed_write_paths", [])
        or []
    ):
        path = fixture_dir / name
        if path.is_file() and name.endswith(".py") and not name.startswith("tests/"):
            production_source_lines.extend(_strip_lines(path.read_text(encoding="utf-8")))
            if module_path is None:
                module_path = name
    original_source_text = "\n".join(production_source_lines)

    # Gold-diff added lines are reference-repair needles only when they add
    # text absent from the original program (accepted derivation rule).
    reference_snippets = tuple(
        line for line in _gold_diff_added_lines(task_id, gold_diff_dir)
        if line not in original_source_text
    )

    public_context = (task_meta.get("title", "") or "") + " " + (
        task_meta.get("description", "") or ""
    )
    expected_literals = tuple(
        literal
        for literal in hidden["literals"]
        if len(literal) >= 3 and literal not in public_context
    )
    # Stripped source lines shorter than 3 characters (e.g. a lone "}")
    # are shared punctuation, not hidden-test evidence — the accepted
    # authority applies the same "too weak to be evidence" threshold to
    # expected literals.  The exported document is JSON, so a one- or
    # two-character punctuation needle could only fire spuriously.
    source_lines = tuple(
        line for line in hidden["source_lines"] if len(line) >= 3
    )

    return ForbiddenContent(
        task_id=task_id,
        f2p_node_ids=f2p,
        p2p_node_ids=p2p,
        hidden_test_filenames=tuple(hidden["filenames"]),
        hidden_test_function_names=tuple(hidden["function_names"]),
        hidden_test_source_lines=source_lines,
        assertion_source_lines=tuple(hidden["assertion_lines"]),
        expected_literals=expected_literals,
        oracle_root_cause_summary=oracle.get("root_cause_summary"),
        oracle_runtime_evidence_hint=oracle.get("runtime_evidence_hint"),
        oracle_bug_category=oracle.get("bug_category"),
        oracle_target_symbols=tuple(oracle.get("target_symbols", []) or []),
        reference_repair_snippets=reference_snippets,
        runtime_probe_call_sources=(),
        runtime_probe_anchors=(),
        runtime_probe_focus_functions=(),
        production_source_lines=tuple(production_source_lines),
        production_module_path=module_path,
    )


def audit_exported_text(
    text: str,
    task_id: str,
    legitimate_texts: tuple[str, ...] = (),
    *,
    curated_root: Optional[Path] = None,
    gold_diff_dir: Optional[Path] = None,
    resolver: Optional[EvidenceResolver] = None,
) -> dict[str, Any]:
    """Run the accepted anti-leakage scanner over exported trace text.

    The exported trace JSON is scanned exactly like a model prompt against
    the mechanically derived forbidden content of the fixture's hidden
    tests (hidden test source, node ids, assertion expressions, expected
    literals, oracle root cause, reference repair snippets).  Any finding
    is a fail-closed error — professor JSON must not reintroduce anything
    the clean-holdout policy protected.

    Forbidden content comes from the accepted derivation when the tracked
    fixture is present, or from the tracked frozen needle capsule when it
    is not (pristine tracked-only checkouts).

    The model-authored diagnosis text is legitimately retained in the
    export (the professor asks for the diagnosis), so it is subtracted
    before scanning, exactly as the accepted prompt audit subtracts it
    when it is rendered back into later prompts.  Everything else is
    scanned conservatively — no other subtraction.
    """
    if derive_forbidden_content is None or scan_prompt is None:  # pragma: no cover
        raise RuntimeError("accepted anti-leakage authority is not importable")
    if _fixture_available(task_id, curated_root):
        forbidden = derive_forbidden_content_scoped(
            task_id,
            _fixture_dir_for(task_id, curated_root),
            gold_diff_dir=gold_diff_dir,
        )
    else:
        forbidden = _frozen_forbidden_content(task_id, resolver)
        if forbidden is None:
            raise RuntimeError(
                f"audit forbidden content unavailable for {task_id!r}: "
                f"neither the tracked fixture nor the frozen needle capsule "
                f"is present (fail closed)"
            )
    # The task id is PUBLIC fixture identity (tracked fixture directory,
    # split manifest, task.json).  The accepted authority already excludes
    # needles present in the public title/description ("public_context"
    # rule); the task id is the same class of public context and is
    # REQUIRED by the professor-facing trace identity.  For some quixbugs
    # tasks the oracle bug category is mechanically the task id itself, so
    # the id is removed before scanning.  This cannot hide a hidden-test
    # needle: node ids / test filenames / assertion lines never contain
    # the "quixbugs-<task>" id (covered by a dedicated regression test).
    reduced = text.replace(task_id, "")
    findings = scan_prompt(
        reduced,
        forbidden,
        prompt_index=0,
        controller_state="professor_trace_export",
        legitimate_texts=legitimate_texts,
    )
    return {
        "task_id": task_id,
        "scanned_chars": len(text),
        "leakage_findings": [f.to_mapping() for f in findings],
        "passed": len(findings) == 0,
    }


def _observed_production_function_names(
    evidence: dict[str, Any],
) -> tuple[str, ...]:
    """Production function names actually observed by the debugger.

    These are real production-region frame/pause observations (the model
    legitimately saw them as stack evidence).  The accepted prompt audit
    subtracts their frame-line renderings before source-derived needle
    checks; the structured trace export subtracts the same identifiers.
    """
    task = evidence.get("task") or {}
    module_path = task.get("module_path")
    names: list[str] = []
    for line in (evidence.get("trajectory_jsonl") or "").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event_type") != "observation":
            continue
        obs = (event.get("payload") or {}).get("observation") or {}
        if obs.get("status") != "ok":
            continue
        payload = obs.get("payload") or {}
        if payload.get("script") != module_path:
            continue
        for fn in (payload.get("function"),):
            if isinstance(fn, str) and fn and fn != "<module>" and fn not in names:
                names.append(fn)
        # Real production-region stack frames observed by the debugger
        # (the accepted prompt audit subtracts exactly these frame-line
        # renderings).
        for frame in payload.get("frames") or []:
            if not isinstance(frame, dict):
                continue
            if frame.get("script") != module_path:
                continue
            fn = frame.get("function")
            if isinstance(fn, str) and fn and fn != "<module>" and fn not in names:
                names.append(fn)
    return tuple(names)


def _audit_trace(
    trace: dict[str, Any],
    task_id: str,
    evidence: dict[str, Any],
    *,
    curated_root: Optional[Path] = None,
    gold_diff_dir: Optional[Path] = None,
    resolver: Optional[EvidenceResolver] = None,
) -> dict[str, Any]:
    """Audit one exported trace with the accepted legitimate subtractions.

    Subtracted as legitimate (never as leak evidence): the model-authored
    diagnosis (the model's own audited output rendered back at PATCH time —
    the accepted audit subtracts it) and the production function names the
    debugger really observed (the accepted audit subtracts their frame-line
    renderings; the export carries the same identifiers structurally).
    Hidden test source, node ids, assertion expressions, expected literals,
    oracle root cause and reference repair are matched against the raw
    exported text and remain fail-closed.
    """
    diagnosis_texts = _evidence_diagnosis_texts(evidence)
    observed = _observed_production_function_names(evidence)
    legitimate = tuple(sorted(set(diagnosis_texts) | set(observed), key=len, reverse=True))
    return audit_exported_text(
        _stable_json(trace),
        task_id,
        legitimate_texts=legitimate,
        curated_root=curated_root,
        gold_diff_dir=gold_diff_dir,
        resolver=resolver,
    )


# ---------------------------------------------------------------------------
# Trace building for the R6 export
# ---------------------------------------------------------------------------


def _model_identity_r6() -> dict[str, Any]:
    return {
        "fine_tuned_checkpoint": FINE_TUNED_CHECKPOINT,
        "adapter_identity_sha256": ADAPTER_MODEL_SHA256,
        "training_provenance": TRAINING_PROVENANCE,
    }


def build_trace_r6(
    evidence: dict[str, Any],
    *,
    scope: str,
    model_identity: dict[str, Any],
) -> dict[str, Any]:
    """Build one professor_debug_trace_v1 trace for the R6 export.

    ``scope`` is ``validation`` or ``final_holdout_partial`` and drives the
    trace-level provenance fields.  The
    trace body is derived from the frozen evidence through the accepted
    ``professor_trace.build_trace`` primitives (same command/observation/
    localization/diagnosis/repair/verification mapping as the accepted
    shared trace builder), with these differences:

    - hidden per-test node ids are NEVER exported (verifier counts only);
    - ``evidence_scope``, debugger lifecycle, checkpoint/runtime identity,
      serialization note and workspace cleanup are added;
    """
    base = build_trace(evidence, model_identity)  # schema-validated base
    gate_chain = (evidence.get("gate_results") or {}).get("gate_chain") or {}
    controller = evidence.get("controller_result") or {}
    serialization = evidence.get("serialization_normalization") or {}
    cleanup = evidence.get("cleanup") or {}
    verifier = evidence.get("verifier") or {}

    r6_scope = scope in ("validation", "final_holdout_partial")

    # Hidden-test stack frames are protected by the clean-holdout policy.
    # The model-facing stack rendering was filtered to the target
    # production region (original source lines of the production module),
    # so the professor trace keeps exactly that region: no hidden test
    # frames, no appended harness driver frames.
    task = evidence.get("task") or {}
    module_path = task.get("module_path")
    driver_start = task.get("runtime_appended_driver_start_line")
    for entry in base["debugger_trace"]:
        frames = entry.get("frames")
        if isinstance(frames, list):
            kept = [
                f
                for f in frames
                if isinstance(f, dict)
                and f.get("file") == module_path
                and (
                    driver_start is None
                    or f.get("line") is None
                    or f["line"] < driver_start
                )
            ]
            if kept:
                entry["frames"] = kept
            else:
                entry.pop("frames", None)

    debugger_lifecycle: dict[str, Any] = {
        "entered": gate_chain.get("G1") is not None,
        "terminal_path": bool(gate_chain.get("terminal_path")),
        "production_exception_path": bool(gate_chain.get("production_exception_path")),
        "step_outside_region": bool(gate_chain.get("step_outside_region")),
        "pause_generations": {
            "G1": gate_chain.get("G1"),
            "G2": gate_chain.get("G2"),
        },
        "gate_passed": gate_chain.get("passed"),
        "gate_reason": gate_chain.get("reason"),
        "tool_observations": [
            obs.get("name")
            for line in (evidence.get("trajectory_jsonl") or "").splitlines()
            if line.strip()
            for obs in [_safe_event(line)]
            if isinstance(obs, dict)
            and obs.get("event_type") == "observation"
            and obs.get("name")
        ],
    }

    # Rebuild final verification WITHOUT hidden per-test node ids.  Counts
    # (e.g. f2p "1/1", p2p "1/2") carry the honest verifier record; the
    # model-facing clean-holdout policy protected node ids, so professor
    # JSON must not reintroduce them.
    final_verification = {
        "outcome": verifier.get("outcome"),
        "verifier_status": verifier.get("status"),
        "f2p": f"{verifier.get('f2p_passed', 0)}/{verifier.get('f2p_total', 0)}",
        "p2p": f"{verifier.get('p2p_passed', 0)}/{verifier.get('p2p_total', 0)}",
        "full_suite": (
            "PASS"
            if verifier.get("full_suite_consistent") is True
            else verifier.get("full_suite_consistent")
        ),
        "syntax_passed": verifier.get("syntax_passed"),
        "canonical_fixture_unchanged": verifier.get("canonical_fixture_unchanged"),
        "workspace_lifecycle": verifier.get("workspace_lifecycle"),
        "candidate_sha256": verifier.get("candidate_sha256"),
    }

    run_provenance = dict(base["run_provenance"])
    run_provenance.update(
        {
            "selected_adapter_path": SELECTED_ADAPTER_PATH if r6_scope else None,
            "adapter_model_sha256": ADAPTER_MODEL_SHA256 if r6_scope else None,
            "adapter_config_sha256": ADAPTER_CONFIG_SHA256 if r6_scope else None,
            "evaluator_python_version": PYTHON_VERSION if r6_scope else None,
            "controller_final_state": controller.get("final_state"),
            "controller_stop_reason": controller.get("stop_reason"),
            "model_calls": controller.get("model_calls"),
            "diagnosis_provenance": evidence.get("diagnosis_provenance"),
        }
    )

    trace = {
        "schema_version": base["schema_version"],
        "task_id": base["task_id"],
        # The evidence bug_category is copied verbatim from the fixture's
        # oracle field (oracle_bug_category), which the accepted
        # clean-holdout policy protects from the model and therefore from
        # professor-facing output.  The public task identity is the
        # task_id; the category is NOT_RECORDED for export.
        "bug_category": None,
        "evidence_scope": scope,
        "debugger_path": base["debugger_path"],
        "model": base["model"],
        "treatment": base["treatment"],
        "run_provenance": run_provenance,
        "failure_reproduction": base["failure_reproduction"],
        "debugger_lifecycle": debugger_lifecycle,
        "debugger_trace": base["debugger_trace"],
        "error_localization": base["error_localization"],
        "diagnosis": base["diagnosis"],
        "repair_attempts": base["repair_attempts"],
        "serialization_normalization": {
            "required": serialization.get("note") is not None,
            "note": serialization.get("note"),
            "verifier_input_sha256": serialization.get("verifier_input_sha256"),
            "patchmanager_input_sha256": serialization.get(
                "patchmanager_input_sha256"
            ),
        },
        "final_verification": final_verification,
        "workspace_cleanup": {
            "release_pdb": (cleanup.get("release_pdb") or [])[:],
            "workspace_cleanup": cleanup.get("workspace_cleanup"),
        },
        "claims_boundary": (
            "Derived deterministically from the real accepted R6 "
            "final-execution evidence (checkpoint-30, treatment contract "
            f"{EVIDENCE_CONTRACT_SHA256}).  No hidden test source, hidden "
            "test node id, oracle field, chain-of-thought, or fabricated "
            "localization is included; the debugger path distinguishes the "
            "production-exception path (G2=None) from the normal G2 path "
            "exactly as the accepted gate classified it."
        ),
    }
    validate_trace(trace)
    return trace


def _safe_event(line: str) -> Any:
    try:
        return json.loads(line)
    except ValueError:
        return None


def build_index_r6(
    traces: list[dict[str, Any]],
    trace_paths: dict[str, str],
    *,
    scope: str,
    source_commit_sha: str,
) -> dict[str, Any]:
    """One concise professor-facing index over one trace scope.

    ``validation_result`` is derived from the trace list (never hardcoded).
    Each ``trace_sha256`` is a CONTENT hash: SHA256 of the stable compact
    serialization of the trace object, which equals SHA256 of the same
    stable serialization of the written trace file content — so the index
    hash corresponds 1:1 to the trace file content regardless of file
    indentation.
    """
    entries = []
    for trace in traces:
        entries.append(
            {
                "task_id": trace["task_id"],
                "bug_category": trace.get("bug_category"),
                "evidence_scope": trace.get("evidence_scope"),
                "error_localization": trace["error_localization"],
                "debugger_path": trace.get("debugger_path"),
                "model_turns": len(trace["debugger_trace"]),
                "repair_attempts": len(trace["repair_attempts"]),
                "verifier_outcome": trace["final_verification"]["outcome"],
                "trace_path": trace_paths.get(trace["task_id"]),
                "trace_sha256": _sha256(
                    json.dumps(
                        trace, sort_keys=True, ensure_ascii=False, allow_nan=False
                    )
                ),
            }
        )
    index: dict[str, Any] = {
        "schema_version": f"{SCHEMA_VERSION}_index",
        "generated_from_source_commit": source_commit_sha,
        "evidence_scope": scope,
        "trace_count": len(traces),
        "traces": entries,
    }
    if scope == "validation":
        resolved = sum(
            1
            for trace in traces
            if trace["final_verification"]["outcome"] == "RESOLVED"
        )
        index["selected_fine_tuned_checkpoint"] = FINE_TUNED_CHECKPOINT
        index["base_model_revision"] = BASE_REVISION
        index["validation_cohort_identity"] = {
            "treatment_contract_sha256": EVIDENCE_CONTRACT_SHA256,
            "adapter_model_sha256": ADAPTER_MODEL_SHA256,
        }
        index["validation_result"] = f"{resolved}/{len(traces)} RESOLVED"
        index["holdout_used_for_checkpoint_selection"] = False
    return index


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _relative_posix(path: Path, base: Path) -> str:
    """Output-dir-relative POSIX path — keeps regeneration byte-identical
    regardless of the absolute output location."""
    return path.resolve().relative_to(base.resolve()).as_posix()


#: Validation stage per task — the accepted logical identity used in
#: professor-facing manifests (no machine-local capture paths).
VALIDATION_STAGE_BY_TASK: dict[str, str] = {
    "quixbugs-depth-first-search": "stage-a",
    "quixbugs-quicksort": "stage-b",
    "quixbugs-flatten": "stage-b",
    "quixbugs-find-in-sorted": "stage-c",
    "quixbugs-rpn-eval": "stage-c",
    "quixbugs-shortest-path-length": "stage-c",
    "quixbugs-reverse-linked-list": "stage-c",
    "quixbugs-kth": "stage-c",
}


def _logical_identity(
    scope: str,
    task_id: str,
    capsule_manifest: Optional[dict[str, Any]] = None,
) -> str:
    """Stable logical evidence identity for professor-facing manifests."""
    if scope == "validation":
        stage = VALIDATION_STAGE_BY_TASK.get(task_id, "validation")
        return f"validation/{stage}/{task_id}"
    if capsule_manifest is not None:
        entry = (capsule_manifest.get("evidence") or {}).get(
            f"{scope}:{task_id}"
        ) or {}
        logical = entry.get("logical_identity")
        if logical:
            return logical
    return f"final_holdout_partial/{task_id}"


def _write_json(path: Path, obj: Any) -> None:
    text = json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False)
    text += "\n"
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_professor_traces_r6(
    evidence_root: Path,
    output_dir: Path,
    *,
    include_holdout: bool = True,
    source_commit_sha: str = "4610785713832daaba6aa133374506a2d200391a",
    registry: Optional[EvidenceRegistry] = None,
    curated_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Deterministically export the professor-facing R6 trace set.

    Steps, all fail-closed:
      1. verify frozen evidence identity (hashes, outcomes, contract,
         capsule chain of custody);
      2. build one schema-validated trace per task;
      3. professor-safe leakage audit over every exported trace text;
      4. write traces, indexes, and manifests deterministically with
         portable logical evidence identities (no machine-local paths).

    ``registry`` and ``curated_root`` are injection points for focused
    tests; production always uses the frozen accepted registry and the
    tracked curated fixtures.
    """
    registry = registry or FROZEN_REGISTRY
    curated_root = curated_root or CURATED_ROOT
    resolver = EvidenceResolver(evidence_root)
    evidence_paths = verify_evidence(
        resolver,
        include_holdout=include_holdout,
        registry=registry,
    )
    capsule_manifest = _load_capsule_manifest(resolver)
    # Accepted holdout status authority (verified ancillary record).
    holdout_report_path = resolver.ancillary_path("holdout_report")
    holdout_report = (
        json.loads(holdout_report_path.read_text(encoding="utf-8"))
        if holdout_report_path is not None
        else None
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    validation_dir = output_dir / "r6_validation"
    holdout_dir = output_dir / "r6_holdout_partial"
    for subdir in (validation_dir, holdout_dir):
        subdir.mkdir(parents=True, exist_ok=True)

    artifacts: dict[str, Any] = {"traces": {}, "indexes": {}, "audits": {}}
    audit_results: dict[str, dict[str, Any]] = {}

    # --- primary validation set ---------------------------------------------
    validation_traces: list[dict[str, Any]] = []
    validation_paths: dict[str, str] = {}
    for task_id in sorted(registry.validation):
        evidence = json.loads(
            evidence_paths[f"validation:{task_id}"].read_text(encoding="utf-8")
        )
        trace = build_trace_r6(
            evidence, scope="validation", model_identity=_model_identity_r6()
        )
        audit = _audit_trace(
            trace, task_id, evidence,
            curated_root=curated_root, resolver=resolver,
        )
        audit_results[f"validation:{task_id}"] = audit
        if not audit["passed"]:
            raise RuntimeError(
                f"professor-safe audit FAILED for {task_id!r}: "
                f"{json.dumps(audit['leakage_findings'])[:500]}"
            )
        path = validation_dir / f"{TRACE_FILE_PREFIX}{task_id}.json"
        _write_json(path, trace)
        validation_traces.append(trace)
        validation_paths[task_id] = _relative_posix(path, output_dir)
        artifacts["traces"][f"validation:{task_id}"] = str(path)
    index = build_index_r6(
        validation_traces,
        validation_paths,
        scope="validation",
        source_commit_sha=source_commit_sha,
    )
    index["final_holdout_status"] = "INCOMPLETE_HARDWARE_STOP"
    index["distinct_scopes"] = (
        "The 8 validation traces in r6_validation/ are the completed "
        "contamination-safe disjoint validation cohort.  The two partial "
        "final-holdout traces in r6_holdout_partial/ are a SEPARATE, "
        "hardware-interrupted scope and are not part of this success set."
    )
    index_path = output_dir / "r6_validation_index.json"
    _write_json(index_path, index)
    artifacts["indexes"]["validation"] = str(index_path)

    # --- partial holdout appendix --------------------------------------------
    holdout_traces: list[dict[str, Any]] = []
    holdout_paths: dict[str, str] = {}
    if include_holdout:
        for task_id in sorted(registry.final_holdout_partial):
            evidence = json.loads(
                evidence_paths[f"final_holdout_partial:{task_id}"].read_text(
                    encoding="utf-8"
                )
            )
            trace = build_trace_r6(
                evidence,
                scope="final_holdout_partial",
                model_identity=_model_identity_r6(),
            )
            audit = _audit_trace(
                trace, task_id, evidence,
                curated_root=curated_root, resolver=resolver,
            )
            audit_results[f"final_holdout_partial:{task_id}"] = audit
            if not audit["passed"]:
                raise RuntimeError(
                    f"professor-safe audit FAILED for {task_id!r}: "
                    f"{json.dumps(audit['leakage_findings'])[:500]}"
                )
            path = holdout_dir / f"{TRACE_FILE_PREFIX}{task_id}.json"
            _write_json(path, trace)
            holdout_traces.append(trace)
            holdout_paths[task_id] = _relative_posix(path, output_dir)
            artifacts["traces"][f"final_holdout_partial:{task_id}"] = str(path)
        holdout_index = build_index_r6(
            holdout_traces,
            holdout_paths,
            scope="final_holdout_partial",
            source_commit_sha=source_commit_sha,
        )
        holdout_index["final_holdout_status"] = "INCOMPLETE_HARDWARE_STOP"
        holdout_index["incomplete_tasks"] = {
            "curated-wrong-branch-003": (
                "interrupted during a model request (host power loss)"
            ),
            "curated-mutation-alias-004": "not started (host power loss)",
            "curated-caller-callee-005": "not started (host power loss)",
        }
        holdout_index["breaking_resolved_row"] = (
            "curated-off-by-one-002: fail-to-pass repaired (1/1), "
            "pass-to-pass regression remains (1/2); accepted verifier "
            "outcome BREAKING_RESOLVED != RESOLVED — the independent "
            "verifier rejected an apparently useful repair."
        )
        holdout_index_path = output_dir / "r6_holdout_partial_index.json"
        _write_json(holdout_index_path, holdout_index)
        artifacts["indexes"]["final_holdout_partial"] = str(holdout_index_path)

    # --- audit report -----------------------------------------------------------
    audit_report = {
        "schema_version": "professor_trace_leakage_audit_v1",
        "generated_from_source_commit": source_commit_sha,
        "audit_authority": (
            "experiments.debugger_interaction_v2_r5.anti_leakage "
            "(accepted actual-output scanner; fail closed)"
        ),
        "scanned_documents": len(audit_results),
        "total_findings": sum(
            len(a.get("leakage_findings") or []) for a in audit_results.values()
        ),
        "passed": all(a.get("passed") is True for a in audit_results.values()),
        "per_document": audit_results,
    }
    audit_path = output_dir / "professor_safe_audit.json"
    _write_json(audit_path, audit_report)
    artifacts["audit_report"] = str(audit_path)

    # --- source-evidence manifest (portable logical identities only) ----------
    manifest: dict[str, Any] = {
        "schema_version": "professor_trace_source_evidence_manifest_v1",
        "generated_from_source_commit": source_commit_sha,
        "evidence_scope": "r6-frozen-accepted-evidence",
        "treatment_contract_sha256": EVIDENCE_CONTRACT_SHA256,
        "selected_checkpoint": FINE_TUNED_CHECKPOINT,
        "adapter_model_sha256": ADAPTER_MODEL_SHA256,
        "adapter_config_sha256": ADAPTER_CONFIG_SHA256,
        "evidence_source": (
            "tracked frozen evidence capsule "
            "experiments/r6_debugger_training/runs/frozen "
            "(raw evidence SHA256 identities; machine-local capture paths "
            "are intentionally not exported)"
        ),
        "evidence": {},
        "ancillary": {},
    }
    for scope_group, group in (
        ("validation", registry.validation),
        ("final_holdout_partial", registry.final_holdout_partial),
    ):
        for task_id, expected in sorted(group.items()):
            if scope_group == "final_holdout_partial" and not include_holdout:
                continue
            manifest["evidence"][f"{scope_group}:{task_id}"] = {
                "logical_identity": _logical_identity(
                    scope_group, task_id, capsule_manifest
                ),
                "raw_evidence_sha256": expected,
            }
    for key, expected in sorted(registry.ancillary.items()):
        manifest["ancillary"][key] = {
            "logical_identity": f"ancillary/{key}",
            "sha256": expected,
        }
    if holdout_report is not None:
        holdout_pin = registry.ancillary.get("holdout_report")
        if holdout_pin:
            manifest["holdout_status_authority"] = {
                "logical_identity": "ancillary/holdout_report",
                "run_status": holdout_report.get("run_status"),
                "sha256": holdout_pin,
            }
    manifest_path = output_dir / "source_evidence_manifest.json"
    _write_json(manifest_path, manifest)
    artifacts["manifest"] = str(manifest_path)

    # --- trace SHA manifest -----------------------------------------------------
    sha_manifest: dict[str, Any] = {
        "schema_version": "professor_trace_sha_manifest_v1",
        "generated_from_source_commit": source_commit_sha,
        "traces": {
            key: hashlib.sha256(Path(path_text).read_bytes()).hexdigest()
            for key, path_text in artifacts["traces"].items()
        },
    }
    sha_manifest_path = output_dir / "trace_sha_manifest.json"
    _write_json(sha_manifest_path, sha_manifest)
    artifacts["sha_manifest"] = str(sha_manifest_path)

    return artifacts


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Export the professor-facing R6 debugger trace set "
            "(professor_debug_trace_v1) deterministically from frozen "
            "accepted evidence"
        )
    )
    parser.add_argument(
        "--evidence-root",
        type=str,
        default=None,
        help=(
            "accepted evidence root; default resolves the tracked frozen "
            "evidence capsule, then the accepted review package, then "
            "the live run trees"
        ),
    )
    parser.add_argument("--output-dir", type=str, required=True)
    parser.add_argument(
        "--no-holdout",
        action="store_true",
        help="skip the partial final-holdout appendix",
    )
    parser.add_argument(
        "--source-commit",
        type=str,
        default="4610785713832daaba6aa133374506a2d200391a",
    )
    args = parser.parse_args()

    evidence_root = (
        Path(args.evidence_root).resolve()
        if args.evidence_root
        else resolve_default_evidence_root()
    )
    artifacts = export_professor_traces_r6(
        evidence_root,
        Path(args.output_dir),
        include_holdout=not args.no_holdout,
        source_commit_sha=args.source_commit,
    )
    print(
        json.dumps(
            {
                "status": "COMPLETE",
                "validation_trace_count": len(FROZEN_VALIDATION_EVIDENCE),
                "holdout_trace_count": (
                    0 if args.no_holdout else len(FROZEN_HOLDOUT_EVIDENCE)
                ),
                "total_trace_count": (
                    len(FROZEN_VALIDATION_EVIDENCE)
                    + (0 if args.no_holdout else len(FROZEN_HOLDOUT_EVIDENCE))
                ),
                "output_dir": str(args.output_dir),
                "manifest": artifacts["manifest"],
                "sha_manifest": artifacts["sha_manifest"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
