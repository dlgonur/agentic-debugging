"""Preference pair schema, stable identity and response-bounding tests."""

from __future__ import annotations

import hashlib
import json

import pytest

from agentic_debugger.preference.schema import (
    PREFERENCE_PAIR_SCHEMA_VERSION,
    AttemptRef,
    PreferenceInvariantError,
    PreferencePair,
    bound_response,
)


def _ref(
    attempt_id="a1",
    condition="base",
    response="chosen response",
    response_sha256=None,
    patch_sha256="a" * 64,
    source_identity="generation-artifact-v1:s" + "0" * 61,
) -> AttemptRef:
    return AttemptRef(
        attempt_id=attempt_id,
        condition_id=condition,
        model_identity="offline-deterministic-demo@rev1",
        adapter_identity=None,
        response=response,
        response_sha256=response_sha256 or hashlib.sha256(response.encode("utf-8")).hexdigest(),
        patch_sha256=patch_sha256,
        source_identity=source_identity,
        provenance={"source": "test"},
    )


def _pair(chosen=None, rejected=None, **overrides) -> PreferencePair:
    chosen = chosen if chosen is not None else _ref(attempt_id="a1", response="chosen response")
    rejected = rejected if rejected is not None else _ref(attempt_id="a2", response="rejected response")
    evidence = {"chosen": {"outcome": "RESOLVED"}, "rejected": {"outcome": "NO_OP"}}
    mapping = {
        "schema_version": PREFERENCE_PAIR_SCHEMA_VERSION,
        "pair_id": "0" * 64,
        "task_id": "curated-off-by-one-002",
        "prompt_identity": "curated-off-by-one-002:test",
        "chosen": chosen.to_mapping(),
        "rejected": rejected.to_mapping(),
        "verifier_evidence": evidence,
        "rule_id": "rule-1",
        "preference_reason": "RESOLVED beats non-RESOLVED",
        "source_comparison_identity": "exp:experiment.json",
    }
    mapping.update(overrides)
    mapping["pair_id"] = PreferencePair.identity(
        task_id=mapping["task_id"],
        prompt_identity=mapping["prompt_identity"],
        chosen=AttemptRef.from_mapping(mapping["chosen"]),
        rejected=AttemptRef.from_mapping(mapping["rejected"]),
        verifier_evidence=mapping["verifier_evidence"],
        source_comparison_identity=mapping["source_comparison_identity"],
        schema_version=mapping["schema_version"],
    )
    return PreferencePair.from_mapping(mapping)


def test_pair_identity_is_stable_and_binds_every_field():
    chosen = _ref(attempt_id="chosen", response="r1")
    rejected = _ref(attempt_id="rejected", response="r2")
    a = PreferencePair.identity(
        task_id="t", prompt_identity="p", chosen=chosen, rejected=rejected,
        verifier_evidence={"x": 1}, source_comparison_identity="src",
    )
    b = PreferencePair.identity(
        task_id="t", prompt_identity="p", chosen=chosen, rejected=rejected,
        verifier_evidence={"x": 1}, source_comparison_identity="src",
    )
    assert a == b
    assert a != PreferencePair.identity(
        task_id="t2", prompt_identity="p", chosen=chosen, rejected=rejected,
        verifier_evidence={"x": 1}, source_comparison_identity="src",
    )
    assert a != PreferencePair.identity(
        task_id="t", prompt_identity="p", chosen=chosen,
        rejected=_ref(attempt_id="rejected", response="r2-modified"),
        verifier_evidence={"x": 1}, source_comparison_identity="src",
    )
    assert a != PreferencePair.identity(
        task_id="t", prompt_identity="p", chosen=chosen, rejected=rejected,
        verifier_evidence={"x": 2}, source_comparison_identity="src",
    )
    assert a != PreferencePair.identity(
        task_id="t", prompt_identity="p", chosen=chosen, rejected=rejected,
        verifier_evidence={"x": 1}, source_comparison_identity="src2",
    )


def test_pair_round_trip_and_strictness():
    pair = _pair()
    mapping = pair.to_mapping()
    reloaded = PreferencePair.from_mapping(mapping)
    assert reloaded == pair
    mapping["extra"] = 1
    with pytest.raises(PreferenceInvariantError):
        PreferencePair.from_mapping(mapping)


def test_pair_id_tampering_is_rejected_on_load():
    pair = _pair()
    mapping = pair.to_mapping()
    mapping["pair_id"] = "f" * 64
    with pytest.raises(PreferenceInvariantError):
        PreferencePair.from_mapping(mapping)
    # Tampering any identity input changes the recomputed pair id.
    mapping = pair.to_mapping()
    mapping["chosen"]["response"] = "tampered response"
    with pytest.raises(PreferenceInvariantError):
        PreferencePair.from_mapping(mapping)


def test_pair_rejects_same_attempt_and_same_response():
    with pytest.raises(PreferenceInvariantError):
        _pair(chosen=_ref(attempt_id="a1", response="r1"),
              rejected=_ref(attempt_id="a1", response="r2"))
    with pytest.raises(PreferenceInvariantError):
        _pair(chosen=_ref(attempt_id="a1", response="same"),
              rejected=_ref(attempt_id="a2", response="same"))


def test_pair_rejects_unknown_schema_version():
    with pytest.raises(PreferenceInvariantError):
        _pair(schema_version="preference-pair-v0")


def test_pair_rejects_bad_identifiers():
    with pytest.raises(PreferenceInvariantError):
        _pair(chosen=_ref(attempt_id="bad id!", response="r1"))
    with pytest.raises(PreferenceInvariantError):
        _pair(rejected=_ref(attempt_id="a2", condition="Bad", response="r2"))


def test_attempt_ref_response_hash_must_match():
    with pytest.raises(PreferenceInvariantError):
        _ref(response="some response", response_sha256="0" * 64)


def test_attempt_ref_stored_response_cap_is_enforced():
    from agentic_debugger.preference.schema import MAX_PAIR_RESPONSE_BYTES

    with pytest.raises(PreferenceInvariantError):
        _ref(response="x" * (MAX_PAIR_RESPONSE_BYTES + 1))


def test_attempt_ref_source_identity_required():
    with pytest.raises(PreferenceInvariantError):
        _ref(source_identity="")


def test_bound_response_is_explicit_not_silent():
    from agentic_debugger.preference.schema import MAX_PAIR_RESPONSE_BYTES

    text = "x" * (MAX_PAIR_RESPONSE_BYTES + 100)
    bounded = bound_response(text)
    assert bounded.endswith("[response-truncated]\n")
    assert len(bounded.encode("utf-8")) <= MAX_PAIR_RESPONSE_BYTES
    short = bound_response("short")
    assert short == "short"


@pytest.mark.parametrize("char", ["\u00e9", "\u20ac", "\U0001f600"])
def test_bound_response_utf8_boundary_no_split_no_replacement(char):
    """Two-, three-, and four-byte characters at the boundary: no code-point
    split, no replacement-character expansion, exact byte cap never
    exceeded."""
    from agentic_debugger.preference.schema import MAX_PAIR_RESPONSE_BYTES

    cap = MAX_PAIR_RESPONSE_BYTES
    text = char * (cap + 10)
    bounded = bound_response(text)
    encoded = bounded.encode("utf-8")
    assert len(encoded) <= cap
    assert bounded.endswith("[response-truncated]\n")
    assert "\ufffd" not in bounded
    # Exact cap boundary: no truncation.
    exact = bound_response("a" * cap)
    assert len(exact.encode("utf-8")) == cap


def test_pair_identity_is_stable():
    chosen = _ref(attempt_id="chosen", response="r1")
    rejected = _ref(attempt_id="rejected", response="r2")
    a = PreferencePair.identity(
        task_id="t", prompt_identity="p", chosen=chosen, rejected=rejected,
        verifier_evidence={}, source_comparison_identity="src",
    )
    b = PreferencePair.identity(
        task_id="t", prompt_identity="p", chosen=chosen, rejected=rejected,
        verifier_evidence={}, source_comparison_identity="src",
    )
    assert a == b
