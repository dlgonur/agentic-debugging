"""Provider-reported token usage: the single typed, safe value contract.

Token usage v1 semantics (Session Token Usage Telemetry):

- ``input_tokens`` is the complete effective input count under the
  provider's own usage semantics (for Anthropic-style routes that means
  base input plus cache-read plus cache-write, because the provider
  reports those as disjoint input buckets).
- ``cached_input_tokens`` is the cache-read/hit portion and is always a
  subset of ``input_tokens``; it is never added to input again.
- ``output_tokens`` is the provider-reported output/completion count.
- ``total_tokens`` is defined as ``input_tokens + output_tokens``.

This is a SAFE value contract: it carries counts only.  It must never
carry prompts, completions, keys, credentials, headers, endpoints, or
request/response bodies, and it accepts no arbitrary provider payload —
unknown keys are ignored on ingestion and rejected at the durable-event
boundary.  Every count is a non-negative bounded integer.

``None`` always means the provider did not report that dimension
(unknown).  Unknown is never silently treated as zero, and no token
count is ever estimated locally: provider-reported usage only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Optional

#: Hard bound for any single provider-reported token count.  Far above any
#: real request/response size while keeping every count a bounded,
#: JSON-safe integer.
MAX_TOKEN_COUNT = 1_000_000_000_000

#: Dimensions carried by the durable ``model.request_completed`` usage
#: block (canonical order).  ``cache_write_input_tokens`` is retained
#: internally by the accepted usage authority but is not a primary v1
#: event/UI dimension.
TOKEN_USAGE_PAYLOAD_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "total_tokens",
)

#: Transport-level ``usage`` mapping aliases accepted on ingestion.  The
#: flat ``prompt_tokens``/``completion_tokens`` names are the historical
#: transport contract; provider adapters additionally report
#: ``cached_input_tokens``/``cache_write_input_tokens`` when the route
#: truthfully knows them.
TRANSPORT_INPUT_KEYS = ("prompt_tokens", "input_tokens")
TRANSPORT_OUTPUT_KEYS = ("completion_tokens", "output_tokens")
TRANSPORT_CACHED_KEYS = ("cached_input_tokens",)
TRANSPORT_CACHE_WRITE_KEYS = ("cache_write_input_tokens",)
TRANSPORT_TOTAL_KEYS = ("total_tokens",)

_ALL_DIMENSIONS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "total_tokens",
    "cache_write_input_tokens",
)


def _valid_count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_TOKEN_COUNT


def _first_valid_count(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Optional[int]:
    for key in keys:
        value = mapping.get(key)
        if _valid_count(value):
            return value
    return None


@dataclass(frozen=True)
class TokenUsage:
    """Non-negative, bounded token counts reported by a provider.

    Cross-field semantics (``cached_input_tokens <= input_tokens`` and
    ``total_tokens == input_tokens + output_tokens``) are enforced at the
    durable-event boundary (:func:`usage_from_payload`) and produced by
    :meth:`canonical`; they are deliberately not enforced on raw
    ingestion because a provider payload can be internally inconsistent,
    and an ingestion-time crash on provider data would fail the session
    for a telemetry glitch.
    """

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cache_write_input_tokens: Optional[int] = None

    def __post_init__(self) -> None:
        for name in _ALL_DIMENSIONS:
            value = getattr(self, name)
            if value is not None and not _valid_count(value):
                raise ValueError(
                    f"token usage field {name} must be a non-negative bounded integer or None"
                )

    @property
    def reported(self) -> bool:
        """True when at least one dimension is known."""
        return any(getattr(self, name) is not None for name in _ALL_DIMENSIONS)

    def canonical(
        self, coverage: Optional["TokenUsageCoverage"] = None
    ) -> "TokenUsage":
        """Return the canonical, event-ready usage value.

        When coverage is None (or both input and output are complete),
        ``total_tokens`` is defined as input + output: when both are
        known, the canonical total is that sum and a disagreeing provider
        total cannot be represented under this contract.

        When coverage indicates partial input or output, the exact total
        (or truthful lower bound) is preserved, provided it is not less
        than the sum of known component lower bounds.

        Cached input tokens cannot exceed input tokens when input is
        complete; when input is partial, cached tokens may exceed the
        partial input subtotal.
        """
        usage = self
        if coverage is None:
            if (
                usage.cached_input_tokens is not None
                and usage.input_tokens is not None
                and usage.cached_input_tokens > usage.input_tokens
            ):
                usage = replace(usage, cached_input_tokens=None)
            if usage.input_tokens is not None and usage.output_tokens is not None:
                usage = replace(usage, total_tokens=usage.input_tokens + usage.output_tokens)
            return usage

        input_complete = coverage.is_complete("input_tokens")
        output_complete = coverage.is_complete("output_tokens")
        total_complete = coverage.is_complete("total_tokens")

        # Coverage contradiction:
        # If input and output are both complete, and total is reported,
        # total coverage cannot be partial.
        if (
            input_complete
            and output_complete
            and usage.total_tokens is not None
            and not total_complete
        ):
            raise ValueError(
                "total_tokens coverage cannot be partial when both input_tokens and output_tokens are complete"
            )

        if input_complete:
            if (
                usage.cached_input_tokens is not None
                and usage.input_tokens is not None
                and usage.cached_input_tokens > usage.input_tokens
            ):
                usage = replace(usage, cached_input_tokens=None)

        if input_complete and output_complete:
            if usage.input_tokens is not None and usage.output_tokens is not None:
                expected_total = usage.input_tokens + usage.output_tokens
                if usage.total_tokens is not None and usage.total_tokens != expected_total:
                    raise ValueError(
                        f"token usage total_tokens ({usage.total_tokens}) must equal "
                        f"input_tokens + output_tokens ({expected_total})"
                    )
                usage = replace(usage, total_tokens=expected_total)
        else:
            min_components = (usage.input_tokens or 0) + (usage.output_tokens or 0)
            if usage.total_tokens is not None:
                if total_complete:
                    if usage.total_tokens < min_components:
                        raise ValueError(
                            f"exact total_tokens ({usage.total_tokens}) cannot be less than "
                            f"sum of known component lower bounds ({min_components})"
                        )
                else:
                    if usage.total_tokens < min_components:
                        usage = replace(usage, total_tokens=min_components)
            elif min_components > 0 or (usage.input_tokens is not None or usage.output_tokens is not None):
                usage = replace(usage, total_tokens=min_components)

        return usage

    def added(self, other: "TokenUsage") -> "TokenUsage":
        """Per-dimension sum with unknown propagation.

        A dimension unknown on either side stays unknown on the sum; it is
        never silently treated as a zero contribution.
        """

        def _sum(left: Optional[int], right: Optional[int]) -> Optional[int]:
            if left is None or right is None:
                return None
            return left + right

        return TokenUsage(
            input_tokens=_sum(self.input_tokens, other.input_tokens),
            output_tokens=_sum(self.output_tokens, other.output_tokens),
            cached_input_tokens=_sum(self.cached_input_tokens, other.cached_input_tokens),
            total_tokens=_sum(self.total_tokens, other.total_tokens),
            cache_write_input_tokens=_sum(
                self.cache_write_input_tokens, other.cache_write_input_tokens
            ),
        )

    def to_payload(
        self, coverage: Optional["TokenUsageCoverage"] = None
    ) -> dict[str, int]:
        """Canonical durable-event block: counts only, known fields only."""
        canonical = self.canonical(coverage=coverage)
        return {
            field: getattr(canonical, field)
            for field in TOKEN_USAGE_PAYLOAD_FIELDS
            if getattr(canonical, field) is not None
        }


def usage_from_transport(value: Any) -> TokenUsage:
    """Normalize a transport/provider ``usage`` mapping (ingestion side).

    A non-mapping (including ``None``) means the transport reported no
    usage: every dimension stays unknown.  Each dimension is taken from
    its accepted aliases only when the provider reported a valid
    non-negative integer; malformed values leave that dimension unknown
    rather than zero.  All other keys (cost, telemetry, arbitrary
    provider payload) are ignored and never cross this boundary.
    """
    if not isinstance(value, Mapping):
        return TokenUsage()
    return TokenUsage(
        input_tokens=_first_valid_count(value, TRANSPORT_INPUT_KEYS),
        output_tokens=_first_valid_count(value, TRANSPORT_OUTPUT_KEYS),
        cached_input_tokens=_first_valid_count(value, TRANSPORT_CACHED_KEYS),
        total_tokens=_first_valid_count(value, TRANSPORT_TOTAL_KEYS),
        cache_write_input_tokens=_first_valid_count(value, TRANSPORT_CACHE_WRITE_KEYS),
    )


def validate_usage_with_coverage(
    usage: TokenUsage,
    coverage: Optional["TokenUsageCoverage" | Mapping[str, Any]] = None,
) -> TokenUsage:
    """Validate a TokenUsage instance against its optional coverage authority.

    Enforces coverage-aware cross-field rules:
    - When coverage is None (historical / no-coverage):
      - ``total_tokens == input_tokens + output_tokens`` when all three exist.
      - ``cached_input_tokens <= input_tokens`` when both exist.
      - ``total_tokens >= (input_tokens or 0) + (output_tokens or 0)`` when total exists.
    - When coverage is present:
      - ``total_tokens >= (input_tokens or 0) + (output_tokens or 0)`` when total exists.
      - If input and output are both complete, ``total_tokens == input_tokens + output_tokens``
        when all three exist.
      - If input is complete, ``cached_input_tokens <= input_tokens`` when both exist.
      - If input is partial, cached tokens may exceed the partial input lower bound.
    """
    min_components = (usage.input_tokens or 0) + (usage.output_tokens or 0)
    if usage.total_tokens is not None and usage.total_tokens < min_components:
        raise ValueError(
            f"token usage total_tokens ({usage.total_tokens}) cannot be less than "
            f"sum of known component lower bounds ({min_components})"
        )

    if coverage is not None:
        usage_fields = {
            f: getattr(usage, f)
            for f in TOKEN_USAGE_PAYLOAD_FIELDS
            if getattr(usage, f) is not None
        }
        cov = (
            coverage_from_payload(coverage, usage_fields)
            if isinstance(coverage, Mapping)
            else coverage
        )
        if not isinstance(cov, TokenUsageCoverage):
            raise ValueError("coverage must be a TokenUsageCoverage or mapping")

        input_complete = cov.is_complete("input_tokens")
        output_complete = cov.is_complete("output_tokens")
        total_complete = cov.is_complete("total_tokens")

        if (
            input_complete
            and output_complete
            and usage.total_tokens is not None
            and not total_complete
        ):
            raise ValueError(
                "total_tokens coverage cannot be partial when both input_tokens and output_tokens are complete"
            )

        if input_complete and output_complete:
            if (
                usage.total_tokens is not None
                and usage.input_tokens is not None
                and usage.output_tokens is not None
                and usage.total_tokens != usage.input_tokens + usage.output_tokens
            ):
                raise ValueError(
                    "token usage total_tokens must equal input_tokens + output_tokens"
                )

        if input_complete:
            if (
                usage.cached_input_tokens is not None
                and usage.input_tokens is not None
                and usage.cached_input_tokens > usage.input_tokens
            ):
                raise ValueError(
                    "token usage cached_input_tokens must not exceed input_tokens"
                )
    else:
        if (
            usage.total_tokens is not None
            and usage.input_tokens is not None
            and usage.output_tokens is not None
            and usage.total_tokens != usage.input_tokens + usage.output_tokens
        ):
            raise ValueError(
                "token usage total_tokens must equal input_tokens + output_tokens"
            )
        if (
            usage.cached_input_tokens is not None
            and usage.input_tokens is not None
            and usage.cached_input_tokens > usage.input_tokens
        ):
            raise ValueError(
                "token usage cached_input_tokens must not exceed input_tokens"
            )

    return usage


def usage_from_payload(
    value: Any,
    coverage: Optional["TokenUsageCoverage" | Mapping[str, Any]] = None,
) -> TokenUsage:
    """Strictly validate a durable ``token_usage`` payload block.

    Fails closed (raises ``ValueError``) on: non-mapping values, empty
    blocks, unknown fields, non-integer/negative/unbounded/bool counts,
    and violated cross-field semantics.

    Cross-field arithmetic is coverage-aware:
    - Historical / no-coverage events: reported fields are treated as
      exact (``total_tokens == input_tokens + output_tokens`` when all
      three exist; ``cached_input_tokens <= input_tokens`` when both exist).
    - With coverage:
      - If input and output are complete, ``total_tokens == input_tokens + output_tokens``
        is enforced.
      - If input or output is partial, an exact total (or lower bound)
        is valid provided ``total_tokens >= input_lower_bound + output_lower_bound``.
      - If input is complete, ``cached_input_tokens <= input_tokens`` is enforced.
      - If input is partial, cached tokens may exceed the partial input lower bound.

    Presence is detected via ``get`` rather than ``in`` because journal
    events freeze payloads into tuple-backed mappings whose tuple
    containment semantics do not implement key membership.
    """
    if not isinstance(value, Mapping):
        raise ValueError("token usage block must be a mapping of token counts")
    unknown = set(value) - set(TOKEN_USAGE_PAYLOAD_FIELDS)
    if unknown:
        raise ValueError(f"unknown token usage fields: {sorted(unknown)}")
    absent = object()
    fields: dict[str, int] = {}
    for field in TOKEN_USAGE_PAYLOAD_FIELDS:
        count = value.get(field, absent)
        if count is absent:
            continue
        if not _valid_count(count):
            raise ValueError(f"token usage field {field} must be a non-negative integer")
        fields[field] = count
    if not fields:
        raise ValueError("token usage block must report at least one dimension")
    usage = TokenUsage(**fields)
    return validate_usage_with_coverage(usage, coverage)


@dataclass(frozen=True)
class TokenUsageCoverage:
    """Per-dimension completeness for provider-reported token usage.

    For each dimension reported in :class:`TokenUsage`:
    - ``True`` indicates complete coverage across all provider-completed
      transport attempts for the logical model request.
    - ``False`` indicates a truthful lower bound / subtotal because at
      least one provider-completed attempt did not report that dimension
      or lacked usage entirely.
    """

    input_tokens: bool = True
    output_tokens: bool = True
    cached_input_tokens: bool = True
    total_tokens: bool = True

    def __post_init__(self) -> None:
        for field in TOKEN_USAGE_PAYLOAD_FIELDS:
            val = getattr(self, field)
            if type(val) is not bool:
                raise ValueError(
                    f"token usage coverage field {field} must be a boolean"
                )

    def is_complete(self, dimension: str) -> bool:
        alias_map = {
            "input": "input_tokens",
            "input_tokens": "input_tokens",
            "cached": "cached_input_tokens",
            "cached_input": "cached_input_tokens",
            "cached_input_tokens": "cached_input_tokens",
            "output": "output_tokens",
            "output_tokens": "output_tokens",
            "total": "total_tokens",
            "total_tokens": "total_tokens",
        }
        attr = alias_map.get(dimension, dimension)
        return bool(getattr(self, attr, True))

    def is_partial(self, dimension: str) -> bool:
        return not self.is_complete(dimension)

    def to_payload(
        self, for_fields: Optional[Mapping[str, Any] | Iterable[str]] = None
    ) -> dict[str, bool]:
        """Canonical event block: bools only, known fields only."""
        allowed = (
            set(for_fields.keys())
            if isinstance(for_fields, Mapping)
            else set(for_fields)
            if for_fields is not None
            else set(TOKEN_USAGE_PAYLOAD_FIELDS)
        )
        return {
            field: getattr(self, field)
            for field in TOKEN_USAGE_PAYLOAD_FIELDS
            if field in allowed
        }


def coverage_from_payload(
    value: Any, usage_block: Optional[Mapping[str, Any]] = None
) -> TokenUsageCoverage:
    """Strictly validate a durable ``token_usage_coverage`` payload block.

    Fails closed on non-mapping values, empty blocks, unknown fields,
    non-bool values, or fields not reported in the corresponding
    ``token_usage`` block.
    """
    if not isinstance(value, Mapping):
        raise ValueError("token usage coverage block must be a mapping of boolean flags")
    unknown = set(value) - set(TOKEN_USAGE_PAYLOAD_FIELDS)
    if unknown:
        raise ValueError(f"unknown token usage coverage fields: {sorted(unknown)}")
    if not value:
        raise ValueError("token usage coverage block must report at least one dimension")
    if usage_block is not None:
        disallowed = set(value) - set(usage_block)
        if disallowed:
            raise ValueError(
                f"token usage coverage fields not in token_usage block: {sorted(disallowed)}"
            )
        if (
            value.get("input_tokens") is True
            and value.get("output_tokens") is True
            and "total_tokens" in usage_block
            and value.get("total_tokens") is False
        ):
            raise ValueError(
                "total_tokens coverage cannot be partial when both input_tokens and output_tokens are complete"
            )
    absent = object()
    fields: dict[str, bool] = {}
    for field in TOKEN_USAGE_PAYLOAD_FIELDS:
        flag = value.get(field, absent)
        if flag is absent:
            continue
        if type(flag) is not bool:
            raise ValueError(f"token usage coverage field {field} must be a boolean")
        fields[field] = flag
    return TokenUsageCoverage(**fields)


__all__ = [
    "MAX_TOKEN_COUNT",
    "TOKEN_USAGE_PAYLOAD_FIELDS",
    "TokenUsage",
    "TokenUsageCoverage",
    "coverage_from_payload",
    "usage_from_payload",
    "usage_from_transport",
    "validate_usage_with_coverage",
]
