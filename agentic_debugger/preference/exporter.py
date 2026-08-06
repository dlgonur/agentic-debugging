"""Deterministic preference-pair exporter from verifier-backed attempts.

Ordered, explicit preference rules (first deciding rule wins):

1. RESOLVED beats non-RESOLVED (``rule-1``);
2. valid patch beats invalid patch (``rule-2``);
3. stronger F2P rate beats weaker (``rule-3``);
4. with equal F2P, stronger P2P rate beats weaker (``rule-4``);
5. otherwise-equal verified attempts use fewer changed files as a tie-break
   (``rule-5``);
6. equal or incomparable attempts produce no pair.

Guards (fail-closed, never silent):

* held-out task exclusion;
* oracle-answer contamination rejection — checked against the **complete
  original bounded generation response** (the attempt's stored response,
  before the pair-storage bound is applied), with the spans recorded in the
  audit; the pair is refused, never sanitized, and the stored pair response
  can never carry answer leakage beyond the storage cutoff;
* duplicate pair identity rejection (hard error);
* same-attempt rejection;
* same-response rejection;
* no-evidence rejection when both sides lack verifier evidence or a
  response;
* stable deterministic ordering (output sorted by pair id);
* no unknown fields in the strict pair schema.

This sprint produces exporter infrastructure and a deterministic demo-scale
pair set; it does not perform DPO/RLHF.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from agentic_debugger.comparison.schema import AttemptRecord, ComparisonExperiment
from agentic_debugger.evaluation.task_schema import Oracle
from agentic_debugger.preference.leakage import contamination_spans, is_contaminated
from agentic_debugger.preference.schema import (
    PREFERENCE_PAIR_SCHEMA_VERSION,
    PreferenceError,
    PreferenceInvariantError,
    AttemptRef,
    PreferencePair,
    bound_response,
)

#: Ordered rule ids in decision order (documented).
RULE_IDS = ("rule-1", "rule-2", "rule-3", "rule-4", "rule-5")


class ExportError(PreferenceError):
    """Raised when preference pairs cannot be exported."""


def _rate(passed: Optional[int], total: Optional[int]) -> Optional[float]:
    if total is None or passed is None or total == 0:
        return None
    return passed / total


def _resolved(attempt: AttemptRecord) -> bool:
    return attempt.verifier_outcome == "RESOLVED"


def decide_preference(
    a: AttemptRecord, b: AttemptRecord
) -> Optional[Tuple[AttemptRecord, AttemptRecord, str, str]]:
    """Apply the ordered rules to one unordered attempt pair.

    Returns ``(chosen, rejected, rule_id, reason)`` or ``None`` when the two
    attempts are equal or incomparable.
    """

    # Same task is enforced by the caller.
    prompt_a = (a.provenance or {}).get("prompt_contract")
    prompt_b = (b.provenance or {}).get("prompt_contract")
    if prompt_a != prompt_b:
        return None
    if not (a.verifier_evidence or b.verifier_evidence):
        return None

    # rule-1
    ra, rb = _resolved(a), _resolved(b)
    if ra != rb:
        chosen, rejected = (a, b) if ra else (b, a)
        return chosen, rejected, "rule-1", (
            "RESOLVED verifier outcome beats non-RESOLVED outcome"
        )
    # rule-2
    va, vb = a.valid_patch, b.valid_patch
    if va != vb:
        chosen, rejected = (a, b) if va else (b, a)
        return chosen, rejected, "rule-2", (
            "strictly valid patch beats invalid or absent patch"
        )
    # rule-3
    fa, fb = _rate(a.f2p_passed, a.f2p_total), _rate(b.f2p_passed, b.f2p_total)
    if fa is not None and fb is not None and fa != fb:
        chosen, rejected = (a, b) if fa > fb else (b, a)
        return chosen, rejected, "rule-3", (
            f"stronger F2P rate ({fa:.3f} vs {fb:.3f})"
        )
    if (fa is None) != (fb is None):
        return None
    # rule-4
    pa, pb = _rate(a.p2p_passed, a.p2p_total), _rate(b.p2p_passed, b.p2p_total)
    if pa is not None and pb is not None and pa != pb:
        chosen, rejected = (a, b) if pa > pb else (b, a)
        return chosen, rejected, "rule-4", (
            f"stronger P2P rate ({pa:.3f} vs {pb:.3f})"
        )
    if (pa is None) != (pb is None):
        return None
    # rule-5
    ca, cb = a.changed_file_count, b.changed_file_count
    if ca is not None and cb is not None and ca != cb:
        chosen, rejected = (a, b) if ca < cb else (b, a)
        return chosen, rejected, "rule-5", (
            f"fewer changed files ({ca} vs {cb})"
        )
    return None


def export_preferences_from_experiment(
    experiment: ComparisonExperiment,
    *,
    task_oracles: Mapping[str, Oracle],
    held_out_task_ids: Sequence[str] = (),
    source_comparison_identity: str = "",
) -> Tuple[Tuple[PreferencePair, ...], Dict[str, Any]]:
    """Build deterministic preference pairs from one comparison experiment.

    Returns ``(pairs, audit)``.  Pairs are sorted by pair id; the audit
    records every decision and rejection, including contamination spans from
    the untruncated source responses.
    """

    if not isinstance(experiment, ComparisonExperiment):
        raise ExportError("export requires a ComparisonExperiment")
    if type(source_comparison_identity) is not str or not source_comparison_identity:
        raise ExportError("source_comparison_identity must be non-empty")
    held_out = set(held_out_task_ids)

    attempts = sorted(
        experiment.attempts,
        key=lambda a: (a.task_id, a.condition_id, a.attempt_id),
    )
    pairs: List[PreferencePair] = []
    seen_pair_ids: set[str] = set()
    audit: Dict[str, Any] = {
        "schema_version": PREFERENCE_PAIR_SCHEMA_VERSION,
        "source_comparison_identity": source_comparison_identity,
        "attempts_considered": len(attempts),
        "tasks_considered": sorted({a.task_id for a in attempts}),
        "tasks_excluded_held_out": sorted(held_out),
        "pairs_produced": 0,
        "pair_ids": [],
        "rule_counts": {rule: 0 for rule in RULE_IDS},
        "rejected": {
            "held_out_task": 0,
            "same_attempt": 0,
            "same_response": 0,
            "contamination": 0,
            "no_evidence": 0,
            "incomparable": 0,
        },
        "contamination_rejections": [],
        "per_task_pair_counts": {},
    }

    by_task: Dict[str, List[AttemptRecord]] = {}
    for attempt in attempts:
        by_task.setdefault(attempt.task_id, []).append(attempt)

    for task_id in sorted(by_task):
        if task_id in held_out:
            audit["rejected"]["held_out_task"] += len(by_task[task_id])
            continue
        if task_id not in task_oracles:
            raise ExportError(
                f"no oracle supplied for task {task_id!r}; refusing to export pairs"
            )
        oracle = task_oracles[task_id]
        task_attempts = by_task[task_id]
        task_pairs = 0
        for i in range(len(task_attempts)):
            for j in range(i + 1, len(task_attempts)):
                a, b = task_attempts[i], task_attempts[j]
                if a.attempt_id == b.attempt_id:
                    audit["rejected"]["same_attempt"] += 1
                    continue
                if a.response_text == b.response_text and a.response_text is not None:
                    audit["rejected"]["same_response"] += 1
                    continue
                if not a.response_text or not b.response_text:
                    audit["rejected"]["no_evidence"] += 1
                    continue
                if not (a.verifier_evidence or b.verifier_evidence):
                    audit["rejected"]["no_evidence"] += 1
                    continue
                # Contamination is checked against the complete original
                # bounded generation responses BEFORE any pair-storage bound.
                # Only oracle *answer* spans reject the pair; oracle
                # *identity* spans (file/symbol names any legitimate patch
                # must contain) are recorded for the audit but never reject.
                reject_a = is_contaminated(a.response_text, oracle)
                reject_b = is_contaminated(b.response_text, oracle)
                if reject_a or reject_b:
                    audit["rejected"]["contamination"] += 1
                    audit["contamination_rejections"].append(
                        {
                            "attempt_ids": [a.attempt_id, b.attempt_id],
                            "spans": {
                                "chosen_candidate": contamination_spans(
                                    a.response_text, oracle
                                ),
                                "rejected_candidate": contamination_spans(
                                    b.response_text, oracle
                                ),
                            },
                        }
                    )
                    continue
                decided = decide_preference(a, b)
                if decided is None:
                    audit["rejected"]["incomparable"] += 1
                    continue
                chosen, rejected, rule_id, reason = decided

                prompt_contract = (chosen.provenance or {}).get("prompt_contract")
                prompt_identity = f"{task_id}:{prompt_contract or 'unknown'}"
                # Stored (bounded) copies; the source responses are the
                # bounded originals verified above.
                stored_a = bound_response(a.response_text)
                stored_b = bound_response(b.response_text)

                def _ref(attempt: AttemptRecord, response: str) -> AttemptRef:
                    provenance = dict(attempt.provenance or {})
                    return AttemptRef(
                        attempt_id=attempt.attempt_id,
                        condition_id=attempt.condition_id,
                        model_identity=(
                            f"{provenance.get('model_identity')}@"
                            f"{provenance.get('model_revision')}"
                            if provenance.get("model_identity")
                            else None
                        ),
                        adapter_identity=provenance.get("adapter_identity"),
                        response=response,
                        response_sha256=attempt.response_sha256,
                        patch_sha256=attempt.patch_sha256,
                        source_identity=attempt.source_identity,
                        provenance=provenance,
                    )

                chosen_ref = _ref(chosen, stored_a if chosen is a else stored_b)
                rejected_ref = _ref(rejected, stored_b if chosen is a else stored_a)
                pair = PreferencePair(
                    schema_version=PREFERENCE_PAIR_SCHEMA_VERSION,
                    pair_id=PreferencePair.identity(
                        task_id=task_id,
                        prompt_identity=prompt_identity,
                        chosen=chosen_ref,
                        rejected=rejected_ref,
                        verifier_evidence={
                            "chosen": dict(chosen.verifier_evidence or {}),
                            "rejected": dict(rejected.verifier_evidence or {}),
                        },
                        source_comparison_identity=source_comparison_identity,
                    ),
                    task_id=task_id,
                    prompt_identity=prompt_identity,
                    chosen=chosen_ref,
                    rejected=rejected_ref,
                    verifier_evidence={
                        "chosen": dict(chosen.verifier_evidence or {}),
                        "rejected": dict(rejected.verifier_evidence or {}),
                    },
                    rule_id=rule_id,
                    preference_reason=reason,
                    source_comparison_identity=source_comparison_identity,
                )
                if pair.pair_id in seen_pair_ids:
                    raise PreferenceInvariantError(
                        f"duplicate pair identity: {pair.pair_id}"
                    )
                seen_pair_ids.add(pair.pair_id)
                pairs.append(pair)
                audit["rule_counts"][rule_id] += 1
                task_pairs += 1
        audit["per_task_pair_counts"][task_id] = task_pairs

    pairs.sort(key=lambda pair: pair.pair_id)
    audit["pairs_produced"] = len(pairs)
    audit["pair_ids"] = [pair.pair_id for pair in pairs]
    return tuple(pairs), audit


__all__ = [
    "RULE_IDS",
    "ExportError",
    "decide_preference",
    "export_preferences_from_experiment",
]
