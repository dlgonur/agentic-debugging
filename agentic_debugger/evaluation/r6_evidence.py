"""R6 frozen evidence registry, resolution, and chain-of-custody verification.

This module owns the scientific evidence-identity layer of the R6
professor trace export: the frozen registry constants (base model,
adapter identity, training provenance, frozen validation/holdout/
ancillary records), the :class:`EvidenceRegistry` reconstruction
target, the :class:`EvidenceResolver` with frozen-first-match
source-root policy, and the capsule manifest + chain-of-custody
verification (:func:`verify_evidence`).  Evidence identity is exact;
hashes are never recomputed to force a pass.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Frozen evidence registry (reconstruction target)
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
