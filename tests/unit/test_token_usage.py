"""Token usage v1 value-contract tests (Session Token Usage Telemetry).

Covers the canonical semantics the product exposes — Input / Cached /
Output / Total with Cached a subset of Input and Total == Input + Output —
plus provider-reported-only ingestion, unknown propagation, and strict
durable-payload validation.  All fixtures are synthetic; no provider is
contacted.
"""

from __future__ import annotations

import pytest

from agentic_debugger.agent.token_usage import (
    MAX_TOKEN_COUNT,
    TOKEN_USAGE_PAYLOAD_FIELDS,
    TokenUsage,
    TokenUsageCoverage,
    coverage_from_payload,
    usage_from_payload,
    usage_from_transport,
)


class TestTransportIngestion:
    def test_prompt_completion_total_shape(self):
        usage = usage_from_transport(
            {"prompt_tokens": 8_420, "completion_tokens": 734, "total_tokens": 9_154}
        )
        assert usage.input_tokens == 8_420
        assert usage.output_tokens == 734
        assert usage.total_tokens == 9_154
        assert usage.cached_input_tokens is None

    def test_input_output_shape(self):
        usage = usage_from_transport({"input_tokens": 11, "output_tokens": 7})
        assert usage.input_tokens == 11
        assert usage.output_tokens == 7
        assert usage.total_tokens is None

    def test_cached_reported_as_subset(self):
        usage = usage_from_transport(
            {"prompt_tokens": 10_000, "completion_tokens": 2_000, "cached_input_tokens": 7_000}
        )
        assert usage.input_tokens == 10_000
        assert usage.cached_input_tokens == 7_000
        # Total is Input + Output; Cached is never added again.
        assert usage.canonical().total_tokens == 12_000

    def test_missing_usage_is_all_unknown(self):
        usage = usage_from_transport(None)
        assert usage == TokenUsage()
        assert not usage.reported
        assert usage_from_transport("nope") == TokenUsage()
        assert usage_from_transport({}) == TokenUsage()

    def test_malformed_and_negative_counts_omitted_fail_closed(self):
        usage = usage_from_transport(
            {
                "prompt_tokens": -5,
                "completion_tokens": 3.5,
                "total_tokens": True,
                "cached_input_tokens": "7000",
                "cache_write_input_tokens": -1,
            }
        )
        assert usage.input_tokens is None
        assert usage.output_tokens is None
        assert usage.total_tokens is None
        assert usage.cached_input_tokens is None
        assert usage.cache_write_input_tokens is None
        assert not usage.reported

    def test_arbitrary_provider_payload_is_ignored(self):
        usage = usage_from_transport(
            {
                "prompt_tokens": 5,
                "prompt": "must never cross this boundary",
                "completion": "neither may response text",
                "api_key": "sk-secret",
                "cost": 0.0042,
            }
        )
        assert usage.input_tokens == 5
        assert usage == TokenUsage(input_tokens=5)

    def test_value_object_rejects_invalid_counts(self):
        with pytest.raises(ValueError):
            TokenUsage(input_tokens=-1)
        with pytest.raises(ValueError):
            TokenUsage(output_tokens=1.5)
        with pytest.raises(ValueError):
            TokenUsage(total_tokens=True)
        with pytest.raises(ValueError):
            TokenUsage(cached_input_tokens=MAX_TOKEN_COUNT + 1)


class TestCanonicalSemantics:
    def test_total_is_input_plus_output_not_cached_added(self):
        usage = TokenUsage(input_tokens=10_000, cached_input_tokens=7_000, output_tokens=2_000)
        assert usage.canonical().total_tokens == 12_000

    def test_disagreeing_provider_total_replaced_by_canonical_definition(self):
        usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=100)
        assert usage.canonical().total_tokens == 15

    def test_provider_total_kept_when_input_output_unknown(self):
        usage = TokenUsage(total_tokens=500)
        assert usage.canonical().total_tokens == 500

    def test_cached_exceeding_input_becomes_unknown_not_clamped(self):
        usage = TokenUsage(input_tokens=200, cached_input_tokens=50_000, output_tokens=10)
        canonical = usage.canonical()
        assert canonical.cached_input_tokens is None
        assert canonical.input_tokens == 200
        assert canonical.total_tokens == 210

    def test_to_payload_carries_known_dimensions_only(self):
        usage = TokenUsage(
            input_tokens=220,
            output_tokens=50,
            cached_input_tokens=120,
            total_tokens=270,
            cache_write_input_tokens=30,
        )
        assert usage.to_payload() == {
            "input_tokens": 220,
            "output_tokens": 50,
            "cached_input_tokens": 120,
            "total_tokens": 270,
        }


class TestAggregation:
    def test_retry_and_repair_attempts_all_count(self):
        # Acceptance example: attempt 1 (100/40/20) + attempt 2 (120/80/30)
        attempt_1 = usage_from_transport(
            {"prompt_tokens": 100, "completion_tokens": 20, "cached_input_tokens": 40}
        )
        attempt_2 = usage_from_transport(
            {"prompt_tokens": 120, "completion_tokens": 30, "cached_input_tokens": 80}
        )
        logical = attempt_1.added(attempt_2).canonical()
        assert logical.input_tokens == 220
        assert logical.cached_input_tokens == 120
        assert logical.output_tokens == 50
        assert logical.total_tokens == 270

    def test_unknown_propagates_never_zero(self):
        reported = usage_from_transport({"prompt_tokens": 100, "completion_tokens": 20})
        completed_without_usage = usage_from_transport(None)
        logical = reported.added(completed_without_usage)
        assert logical.input_tokens is None
        assert logical.output_tokens is None
        assert logical.total_tokens is None

    def test_missing_dimension_poisons_only_that_dimension(self):
        first = usage_from_transport(
            {"prompt_tokens": 100, "completion_tokens": 20, "cached_input_tokens": 40}
        )
        second = usage_from_transport({"prompt_tokens": 120, "completion_tokens": 30})
        logical = first.added(second).canonical()
        assert logical.input_tokens == 220
        assert logical.output_tokens == 50
        assert logical.total_tokens == 270
        assert logical.cached_input_tokens is None


class TestDurablePayloadValidation:
    def test_valid_payload_round_trips(self):
        block = {"input_tokens": 220, "output_tokens": 50, "cached_input_tokens": 120, "total_tokens": 270}
        assert usage_from_payload(block).to_payload() == block

    def test_partial_dimensions_valid(self):
        usage = usage_from_payload({"input_tokens": 5, "output_tokens": 3})
        assert usage.input_tokens == 5
        assert usage.output_tokens == 3
        assert usage.total_tokens is None

    @pytest.mark.parametrize(
        "block",
        (
            {},
            {"input_tokens": -1},
            {"input_tokens": 1.5},
            {"input_tokens": True},
            {"input_tokens": "220"},
            {"input_tokens": 5, "output_tokens": 3, "total_tokens": 9},
            {"input_tokens": 5, "cached_input_tokens": 6},
            {"prompt_tokens": 5},
            {"input_tokens": 5, "reasoning_tokens": 2},
            "not-a-mapping",
            ["input_tokens", 5],
        ),
    )
    def test_invalid_payloads_fail_closed(self, block):
        with pytest.raises(ValueError):
            usage_from_payload(block)

    def test_payload_fields_are_counts_only(self):
        assert TOKEN_USAGE_PAYLOAD_FIELDS == (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "total_tokens",
        )


class TestTokenUsageCoverage:
    def test_default_coverage_is_all_complete(self):
        cov = TokenUsageCoverage()
        assert cov.input_tokens is True
        assert cov.output_tokens is True
        assert cov.cached_input_tokens is True
        assert cov.total_tokens is True
        assert cov.is_complete("input_tokens")
        assert not cov.is_partial("input_tokens")
        assert cov.to_payload() == {
            "input_tokens": True,
            "output_tokens": True,
            "cached_input_tokens": True,
            "total_tokens": True,
        }

    def test_partial_dimension_reporting(self):
        cov = TokenUsageCoverage(input_tokens=True, cached_input_tokens=False, output_tokens=True, total_tokens=True)
        assert cov.is_complete("input")
        assert cov.is_partial("cached")
        assert not cov.is_complete("cached")
        assert cov.to_payload() == {
            "input_tokens": True,
            "output_tokens": True,
            "cached_input_tokens": False,
            "total_tokens": True,
        }

    def test_to_payload_filters_for_reported_fields(self):
        cov = TokenUsageCoverage(input_tokens=False, output_tokens=False, cached_input_tokens=False, total_tokens=False)
        assert cov.to_payload(for_fields={"input_tokens": 100, "total_tokens": 100}) == {
            "input_tokens": False,
            "total_tokens": False,
        }

    def test_coverage_rejects_non_boolean_values(self):
        with pytest.raises(ValueError):
            TokenUsageCoverage(input_tokens=1)  # type: ignore
        with pytest.raises(ValueError):
            TokenUsageCoverage(cached_input_tokens="true")  # type: ignore
        with pytest.raises(ValueError):
            TokenUsageCoverage(output_tokens=None)  # type: ignore

    def test_coverage_from_payload_roundtrip(self):
        payload = {"input_tokens": True, "cached_input_tokens": False, "output_tokens": True, "total_tokens": True}
        usage_block = {"input_tokens": 220, "cached_input_tokens": 40, "output_tokens": 50, "total_tokens": 270}
        cov = coverage_from_payload(payload, usage_block)
        assert cov.input_tokens is True
        assert cov.cached_input_tokens is False
        assert cov.output_tokens is True
        assert cov.total_tokens is True

    @pytest.mark.parametrize(
        "block,usage_block",
        (
            ({}, {"input_tokens": 10}),
            ({"input_tokens": 1}, {"input_tokens": 10}),
            ({"input_tokens": "true"}, {"input_tokens": 10}),
            ({"unknown_field": True}, {"input_tokens": 10}),
            ({"cached_input_tokens": True}, {"input_tokens": 10}),  # cached not in usage_block
            ("not-a-mapping", {"input_tokens": 10}),
        ),
    )
    def test_invalid_coverage_payloads_fail_closed(self, block, usage_block):
        with pytest.raises(ValueError):
            coverage_from_payload(block, usage_block)

