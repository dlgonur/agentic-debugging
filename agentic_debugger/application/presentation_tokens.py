"""Token-usage folding for the presentation projection.

``SessionTokenUsage`` is the cumulative provider-reported token usage over
completed requests, reduced exclusively from durable
``model.request_completed`` events, so live and replay derive the identical
state.  Partial request coverage preserves the known provider-reported
subtotal/lower bound without claiming exactness.

Dependency rule: pure data + fold logic only; imports the token-usage
payload helpers from :mod:`agentic_debugger.agent.token_usage`.  No I/O,
no controller/verifier/PDB/patch/model state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from agentic_debugger.agent.token_usage import (
    TokenUsage,
    TokenUsageCoverage,
    coverage_from_payload,
    usage_from_payload,
)


@dataclass(frozen=True)
class SessionTokenUsage:
    """Cumulative provider-reported token usage over completed requests.

    Reduced exclusively from durable ``model.request_completed`` events,
    so live and replay derive the identical state.  Partial request
    coverage preserves the known provider-reported subtotal/lower bound
    without claiming exactness; cumulative counts are never wiped to
    ``None`` when another request lacks usage.  Per-dimension request
    and completeness counts track coverage truth so that partial subtotals
    and complete totals can be truthfully distinguished in live and replay views.
    """

    input_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    requests_completed: int = 0
    requests_with_usage: int = 0
    requests_with_input: Optional[int] = None
    requests_with_cached: Optional[int] = None
    requests_with_output: Optional[int] = None
    requests_with_total: Optional[int] = None
    requests_complete_input: Optional[int] = None
    requests_complete_cached: Optional[int] = None
    requests_complete_output: Optional[int] = None
    requests_complete_total: Optional[int] = None

    def __post_init__(self) -> None:
        if self.requests_with_input is None:
            object.__setattr__(
                self,
                "requests_with_input",
                self.requests_with_usage if self.input_tokens is not None else 0,
            )
        if self.requests_with_cached is None:
            object.__setattr__(
                self,
                "requests_with_cached",
                self.requests_with_usage if self.cached_input_tokens is not None else 0,
            )
        if self.requests_with_output is None:
            object.__setattr__(
                self,
                "requests_with_output",
                self.requests_with_usage if self.output_tokens is not None else 0,
            )
        if self.requests_with_total is None:
            object.__setattr__(
                self,
                "requests_with_total",
                self.requests_with_usage if self.total_tokens is not None else 0,
            )
        if self.requests_complete_input is None:
            object.__setattr__(
                self,
                "requests_complete_input",
                self.requests_with_input or 0,
            )
        if self.requests_complete_cached is None:
            object.__setattr__(
                self,
                "requests_complete_cached",
                self.requests_with_cached or 0,
            )
        if self.requests_complete_output is None:
            object.__setattr__(
                self,
                "requests_complete_output",
                self.requests_with_output or 0,
            )
        if self.requests_complete_total is None:
            object.__setattr__(
                self,
                "requests_complete_total",
                self.requests_with_total or 0,
            )

    @property
    def usage_available(self) -> bool:
        """True once at least one request reported usable usage."""
        return self.requests_with_usage > 0

    @property
    def complete(self) -> bool:
        """True when every completed request reported usage."""
        return (
            self.requests_completed > 0
            and self.requests_with_usage == self.requests_completed
        )

    def _dimension_complete_requests(self, dimension: str) -> int:
        alias_map = {
            "input": "requests_complete_input",
            "input_tokens": "requests_complete_input",
            "cached": "requests_complete_cached",
            "cached_input": "requests_complete_cached",
            "cached_input_tokens": "requests_complete_cached",
            "output": "requests_complete_output",
            "output_tokens": "requests_complete_output",
            "total": "requests_complete_total",
            "total_tokens": "requests_complete_total",
        }
        attr = alias_map.get(dimension, dimension)
        val = getattr(self, attr, 0)
        return val if isinstance(val, int) else 0

    def is_complete(self, dimension: str) -> bool:
        """True if the dimension was reported and complete on every completed request."""
        if self.requests_completed == 0:
            return False
        comp = self._dimension_complete_requests(dimension)
        return comp == self.requests_completed and comp > 0

    def is_partial(self, dimension: str) -> bool:
        """True if the dimension has a known subtotal but incomplete request coverage."""
        if self.requests_completed == 0:
            return False
        dim_count_map = {
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
        count = getattr(self, dim_count_map.get(dimension, dimension), None)
        if count is None:
            return False
        return not self.is_complete(dimension)

    @property
    def input_complete(self) -> bool:
        return self.is_complete("input")

    @property
    def cached_complete(self) -> bool:
        return self.is_complete("cached")

    @property
    def output_complete(self) -> bool:
        return self.is_complete("output")

    def _dimension_requests(self, dimension: str) -> int:
        alias_map = {
            "input": "requests_with_input",
            "input_tokens": "requests_with_input",
            "cached": "requests_with_cached",
            "cached_input": "requests_with_cached",
            "cached_input_tokens": "requests_with_cached",
            "output": "requests_with_output",
            "output_tokens": "requests_with_output",
            "total": "requests_with_total",
            "total_tokens": "requests_with_total",
        }
        attr = alias_map.get(dimension, dimension)
        val = getattr(self, attr, 0)
        return val if isinstance(val, int) else 0

    @property
    def effective_total_tokens(self) -> Optional[int]:
        """Canonical total: ``total_tokens`` or input + output when known."""
        if self.total_tokens is not None:
            if self.input_tokens is not None and self.output_tokens is not None:
                return max(self.total_tokens, self.input_tokens + self.output_tokens)
            return self.total_tokens
        if self.input_tokens is not None and self.output_tokens is not None:
            return self.input_tokens + self.output_tokens
        return None

    @property
    def total_complete(self) -> bool:
        """True when total tokens has complete coverage across completed requests."""
        if self.requests_completed == 0:
            return False
        if self.is_complete("total"):
            return True
        if self.is_complete("input") and self.is_complete("output"):
            return True
        return False

    def _fold(
        self,
        usage: Optional[TokenUsage],
        coverage: Optional[TokenUsageCoverage] = None,
    ) -> "SessionTokenUsage":
        completed = self.requests_completed + 1
        if usage is None or not usage.reported:
            return SessionTokenUsage(
                input_tokens=self.input_tokens,
                cached_input_tokens=self.cached_input_tokens,
                output_tokens=self.output_tokens,
                total_tokens=self.total_tokens,
                requests_completed=completed,
                requests_with_usage=self.requests_with_usage,
                requests_with_input=self.requests_with_input,
                requests_with_cached=self.requests_with_cached,
                requests_with_output=self.requests_with_output,
                requests_with_total=self.requests_with_total,
                requests_complete_input=self.requests_complete_input,
                requests_complete_cached=self.requests_complete_cached,
                requests_complete_output=self.requests_complete_output,
                requests_complete_total=self.requests_complete_total,
            )

        cov = coverage if coverage is not None else TokenUsageCoverage()

        def _sum(left: Optional[int], right: Optional[int]) -> Optional[int]:
            if right is None:
                return left
            if left is None:
                return right
            return left + right

        def _sum_req(current: Optional[int], val: Optional[int]) -> int:
            base = current or 0
            return base + 1 if val is not None else base

        def _sum_comp(current: Optional[int], val: Optional[int], is_comp: bool) -> int:
            base = current or 0
            return base + 1 if (val is not None and is_comp) else base

        return SessionTokenUsage(
            input_tokens=_sum(self.input_tokens, usage.input_tokens),
            cached_input_tokens=_sum(self.cached_input_tokens, usage.cached_input_tokens),
            output_tokens=_sum(self.output_tokens, usage.output_tokens),
            total_tokens=_sum(self.total_tokens, usage.total_tokens),
            requests_completed=completed,
            requests_with_usage=self.requests_with_usage + 1,
            requests_with_input=_sum_req(self.requests_with_input, usage.input_tokens),
            requests_with_cached=_sum_req(self.requests_with_cached, usage.cached_input_tokens),
            requests_with_output=_sum_req(self.requests_with_output, usage.output_tokens),
            requests_with_total=_sum_req(self.requests_with_total, usage.total_tokens),
            requests_complete_input=_sum_comp(self.requests_complete_input, usage.input_tokens, cov.input_tokens),
            requests_complete_cached=_sum_comp(self.requests_complete_cached, usage.cached_input_tokens, cov.cached_input_tokens),
            requests_complete_output=_sum_comp(self.requests_complete_output, usage.output_tokens, cov.output_tokens),
            requests_complete_total=_sum_comp(self.requests_complete_total, usage.total_tokens, cov.total_tokens),
        )


def _fold_request_usage(
    state: SessionTokenUsage, payload: Mapping[str, Any]
) -> SessionTokenUsage:
    block = payload.get("token_usage")
    if not isinstance(block, Mapping):
        return state._fold(None)
    cov_block = payload.get("token_usage_coverage")
    coverage = None
    if isinstance(cov_block, Mapping):
        try:
            coverage = coverage_from_payload(cov_block, block)
        except ValueError:
            coverage = None
    try:
        usage = usage_from_payload(block, coverage=coverage)
    except ValueError:
        # Defensive only: journal events were already schema-validated at
        # write time; an unusable block degrades to no-usage coverage
        # rather than failing the reduction.
        return state._fold(None)
    return state._fold(usage, coverage)
